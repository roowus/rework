"""The free agentic loop node: model calls tools until it stops calling them.

One graph node containing N internal model calls. Tradeoff (deliberate): the
checkpointer captures node boundaries, so a crash mid-loop replays the loop's
model calls from node entry. Per-turn graph nodes would checkpoint every model
call but complicate every role graph; revisit if crash costs show up (the
budget ledger records what actually ran, so replay cost is visible).

The iteration guard is runaway protection, not steering: it sits far above
normal use (ADR 0003: no hardcoded turn steering; the budget ledger enforces).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from rework.kernel.budget.ledger import Budget, guard
from rework.kernel.llm.catalog import ModelCatalog, ModelProvider
from rework.kernel.llm.types import ChatRequest
from rework.kernel.state.records import Charge
from rework.kernel.state.store import StateStore
from rework.nodes.tools import ToolExecutor

log = logging.getLogger("rework.nodes")

DEFAULT_GUARD_ITERATIONS = 64


def _short_args(args: dict[str, Any], limit: int = 160) -> str:
    """One-line rendering of tool arguments for the trajectory view."""
    parts = []
    for key, value in (args or {}).items():
        text = str(value).replace("\n", " ")
        if len(text) > 60:
            text = text[:57] + "..."
        parts.append(f"{key}={text}")
    joined = ", ".join(parts)
    return joined[:limit]


def _is_error(output: str | None) -> bool:
    """Heuristic: executors return error text, not exceptions."""
    if not output:
        return False
    head = output[:200].lower()
    return head.startswith("error") or "traceback" in head or "not found" in head


ToolLoopNode = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


# how long a role at the budget cap waits for the burn gate to put the mission
# on hold before giving up (the gate is checked on the engine's poll interval)
BUDGET_HOLD_GRACE_S = 30.0


async def _current_budget(store: StateStore, mission_id: str, budget: Budget) -> Budget:
    """The role's cap, raised to the mission's CURRENT plan cap: the owner can
    extend a mission's budget at the burn gate after this role was built."""
    mission = await store.get_mission(mission_id)
    cap = (((mission.plan if mission else None) or {}).get("budget") or {}).get("max_usd")
    if cap is None or budget.max_usd is None or float(cap) <= budget.max_usd:
        return budget
    return Budget(
        max_usd=float(cap),
        max_input_tokens=budget.max_input_tokens,
        max_output_tokens=budget.max_output_tokens,
    )


async def _budget_exceeded(store: StateStore, mission_id: str, budget: Budget) -> str | None:
    spend = await store.mission_spend(mission_id)
    try:
        guard(spend, await _current_budget(store, mission_id, budget))
    except Exception as exc:
        return str(exc)
    return None


async def _wait_for_budget(store: StateStore, mission_id: str, budget: Budget) -> bool:
    """At the cap, PAUSE instead of quitting: the burn gate puts the mission on
    hold and asks the owner. Returns True when the budget was raised (carry
    on), False when the mission was cancelled or nobody held it within the
    grace period (the old stop behaviour). Found while monitoring a live
    mission: roles quitting at the cap raced the gate meant to ask first."""
    import asyncio

    from rework.kernel.state.records import MissionStatus

    waited = 0.0
    while True:
        if await _budget_exceeded(store, mission_id, budget) is None:
            return True
        mission = await store.get_mission(mission_id)
        if mission is None or mission.status == MissionStatus.CANCELLED:
            return False
        if mission.status != MissionStatus.ON_HOLD:
            waited += 1.0
            if waited > BUDGET_HOLD_GRACE_S:
                return False
        await asyncio.sleep(1.0)


def make_toolloop_node(
    provider: ModelProvider,
    catalog: ModelCatalog,
    catalog_id: str,
    executor: ToolExecutor,
    system_prompt: str,
    store: StateStore,
    mission_id: str,
    run_id: str,
    budget: Budget,
    max_iterations: int = DEFAULT_GUARD_ITERATIONS,
    temperature: float | None = None,
    effort: str | None = None,
    bus: Any | None = None,
    on_start: Any | None = None,
) -> ToolLoopNode:
    """`bus` (optional) turns on TRAJECTORY events: one per model turn and one
    per tool call, so the UI can show the agent loop the way Claude Code and
    pi do instead of a bare 'running'. Without it the loop is silent (tests)."""
    entry = catalog.resolve(catalog_id)

    async def emit(kind: str, payload: dict[str, Any]) -> None:
        if bus is None:
            return
        from rework.kernel.state.records import EventRecord, EventType

        await bus.publish(
            EventRecord(
                mission_id=mission_id,
                run_id=run_id,
                type=EventType.PROGRESS,
                payload={"trajectory": kind, **payload},
            )
        )

    async def charge(result_usage, model: str) -> None:
        await store.charge(
            Charge(
                mission_id=mission_id,
                run_id=run_id,
                model=model,
                input_tokens=result_usage.input_tokens,
                output_tokens=result_usage.output_tokens,
                cost_usd=entry.estimate_cost(result_usage.input_tokens, result_usage.output_tokens),
            )
        )

    async def node(state: dict[str, Any]) -> dict[str, Any]:
        # RESUME HOOK: a graph resumed from a checkpoint re-enters at the node
        # AFTER the one that did setup, so one-time binding (e.g. the coder's
        # worktree executor) is missing. `on_start` re-establishes it.
        if on_start is not None:
            await on_start()
        messages: list[dict[str, Any]] = list(state.get("messages") or [])
        turns = 0
        stop_note: str | None = None
        specs = executor.specs()

        for _ in range(max_iterations):
            exceeded = await _budget_exceeded(store, mission_id, budget)
            if exceeded is not None and not await _wait_for_budget(store, mission_id, budget):
                stop_note = f"stopped: {exceeded}"
                break
            result = await provider.complete(
                ChatRequest(
                    model=entry.model,
                    system=system_prompt,
                    messages=messages,
                    tools=specs,
                    temperature=temperature,
                    max_tokens=entry.max_output,
                    effort=effort,
                )
            )
            await charge(result.usage, entry.model)
            turns += 1
            await emit(
                "turn",
                {
                    "turn": turns,
                    "text": (result.text or "")[:400],
                    "tools": [tc.name for tc in result.tool_calls],
                    "tokens": result.usage.input_tokens + result.usage.output_tokens,
                },
            )
            messages.append(
                {
                    "role": "assistant",
                    "content": result.text,
                    "tool_calls": [
                        {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                        for tc in result.tool_calls
                    ],
                }
            )
            if not result.tool_calls:
                return {"messages": messages, "loop_turns": turns, "final_text": result.text}
            for tc in result.tool_calls:
                await emit(
                    "tool_call",
                    {"name": tc.name, "arguments": _short_args(tc.arguments)},
                )
                started = time.perf_counter()
                out = await executor.execute(tc.name, dict(tc.arguments))
                log.info(
                    "tool %s%s",
                    tc.name,
                    " (error)" if _is_error(out) else "",
                    extra={"tool": tc.name, "ms": int((time.perf_counter() - started) * 1000)},
                )
                await emit(
                    "tool_result",
                    {
                        "name": tc.name,
                        "result": (out or "")[:400],
                        "error": out[:200] if _is_error(out) else "",
                    },
                )
                messages.append(
                    {"role": "tool", "tool_call_id": tc.id, "name": tc.name, "content": out}
                )
        if stop_note is None:
            stop_note = f"stopped: iteration guard ({max_iterations})"
        return {"messages": messages, "loop_turns": turns, "final_text": stop_note}

    return node
