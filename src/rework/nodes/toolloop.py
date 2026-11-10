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

import json
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
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


# -- transcript compaction ------------------------------------------------------
# Every turn resends the whole transcript, so old tool output is paid for again
# and again (found live: roles at 35-45k tokens per turn, 2.4M input tokens for
# a one-line fix). Past COMPACT_AT characters, tool results older than the last
# KEEP_RECENT are replaced by a one-line stub. It happens in one step, so the
# prefix stays identical (prompt-cache friendly) until the next compaction.
# Tuned on dogfood #2: at 60k/6 a coder re-read dashboard.py ~7 times because
# its earlier reads had been elided; with prompt caching (~85% hits) keeping
# context is cheap and re-reading is not.
MAX_CUT_OFFS = 2  # continuations asked for when a reply hits max_tokens
PROGRESS_CHECK_MIN = 10  # turn budgets this small get no halfway nudge
COMPACT_AT = 160_000
KEEP_RECENT = 12
STUB_MIN = 600


def _size(messages: list[dict[str, Any]]) -> int:
    return sum(len(str(m.get("content") or "")) for m in messages)


def _compact(messages: list[dict[str, Any]]) -> int:
    """Elide old tool results in place; returns how many were elided."""
    if _size(messages) <= COMPACT_AT:
        return 0
    tool_idx = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    elided = 0
    for i in tool_idx[:-KEEP_RECENT]:
        body = str(messages[i].get("content") or "")
        if len(body) < STUB_MIN or body.startswith("[elided"):
            continue
        first = body.strip().splitlines()[0][:160] if body.strip() else ""
        messages[i] = {
            **messages[i],
            "content": f"[elided {len(body)} chars of {messages[i].get('name', 'tool')} output; "
            f"first line: {first!r}. Re-run the tool if you need it again.]",
        }
        elided += 1
    return elided


# -- loop resume -------------------------------------------------------------------


def _load_loop(
    path: Path | None, initial: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], int] | None:
    """A saved transcript for THIS run (same opening message), else None."""
    if path is None or not path.is_file():
        return None
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
        messages = saved["messages"]
        if initial and messages and messages[0].get("content") != initial[0].get("content"):
            return None
        # a transcript must not end on an assistant tool call without results
        while (
            messages and messages[-1].get("role") == "assistant" and messages[-1].get("tool_calls")
        ):
            messages.pop()
        return messages, int(saved.get("turns") or 0)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _save_loop(path: Path | None, messages: list[dict[str, Any]], turns: int) -> None:
    if path is None:
        return
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"turns": turns, "messages": messages}, default=str))
        tmp.replace(path)
    except OSError:
        log.warning("could not save loop transcript to %s", path)


def _clear_loop(path: Path | None) -> None:
    if path is not None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


async def _take_redirect(store: StateStore, run_id: str) -> str:
    """Read-and-clear the run's pending redirect instruction."""
    run = await store.get_run(run_id)
    text = (run.redirect or "").strip() if run is not None else ""
    if text:
        await store.update_run(run_id, redirect="")
    return text


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
    resume_file: Path | None = None,
    progress_check: Any | None = None,
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
                cached_tokens=getattr(result_usage, "cached_tokens", 0) or 0,
                cost_usd=entry.estimate_cost(
                    result_usage.input_tokens,
                    result_usage.output_tokens,
                    getattr(result_usage, "cached_tokens", 0) or 0,
                ),
            )
        )

    async def _handoff_note(messages: list[dict[str, Any]]) -> str:
        ask = [
            *messages,
            {
                "role": "user",
                "content": "You are out of turns. Write a handoff note for whoever "
                "continues this work (no tool calls): 1) what you changed (files), "
                "2) what you learned that they need (exact files, line numbers, "
                "functions, how to run the relevant tests), 3) what remains, in order.",
            },
        ]
        try:
            _compact(ask)
            result = await provider.complete(
                ChatRequest(
                    model=entry.model,
                    system=system_prompt,
                    messages=ask,
                    temperature=temperature,
                    max_tokens=min(entry.max_output, 2048),
                    session_id=run_id,
                )
            )
            await charge(result.usage, entry.model)
            return (result.text or "").strip()
        except Exception as exc:  # the note is a courtesy; never fail the run
            log.warning("handoff note failed: %s", exc)
            return ""

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
        # the orchestrator may give this run a turn budget (cost control)
        run_row = await store.get_run(run_id)
        limit = int(((run_row.context if run_row else None) or {}).get("max_turns") or 0)
        limit = min(limit, max_iterations) if limit > 0 else max_iterations
        # RESUME (pause, budget gate, daemon restart): the loop is ONE graph
        # node, so a re-entered node used to start over at turn 1 and pay for
        # every earlier turn again. The transcript is saved after each turn.
        saved = _load_loop(resume_file, messages)
        if saved is not None:
            messages, turns = saved
            await emit("turn", {"turn": turns, "text": f"resumed at turn {turns}"})
        elif limit < max_iterations and messages:
            messages[-1] = {
                **messages[-1],
                "content": f"{messages[-1].get('content') or ''}\n\nTurn budget: about "
                f"{limit} tool rounds. Plan for it and finish with your summary before it "
                "runs out.",
            }

        cut_offs = 0
        first_cut = len(messages)
        while turns < limit:
            if not cut_offs:
                first_cut = len(messages)
            # the orchestrator can steer a running role (ADR 0007 redirect):
            # the instruction arrives on the run row and is read once
            redirect = await _take_redirect(store, run_id)
            if redirect:
                messages.append({"role": "user", "content": f"[Orchestrator redirect] {redirect}"})
                await emit("turn", {"turn": turns, "text": f"redirected: {redirect[:300]}"})
            exceeded = await _budget_exceeded(store, mission_id, budget)
            if exceeded is not None and not await _wait_for_budget(store, mission_id, budget):
                stop_note = f"stopped: {exceeded}"
                break
            if (
                progress_check is not None
                and limit >= PROGRESS_CHECK_MIN
                and turns == limit // 2
                and not await progress_check(state)
            ):
                # found live: flash coders spent whole budgets exploring and
                # handed off with nothing edited; a prompt line did not hold
                messages.append(
                    {
                        "role": "user",
                        "content": f"[Progress check: {turns} of {limit} tool rounds used and "
                        "nothing is changed yet. Stop exploring and make the change now with "
                        "what you know; verify after.]",
                    }
                )
                await emit("turn", {"turn": turns, "text": "progress check: nothing edited yet"})
            if limit - turns == 2:
                messages.append(
                    {
                        "role": "user",
                        "content": "[2 tool rounds left in your budget: wrap up and give "
                        "your final summary now.]",
                    }
                )
            _compact(messages)
            result = await provider.complete(
                ChatRequest(
                    model=entry.model,
                    system=system_prompt,
                    messages=messages,
                    tools=specs,
                    temperature=temperature,
                    max_tokens=entry.max_output,
                    effort=effort,
                    session_id=run_id,
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
                if result.stop_reason == "max_tokens" and cut_offs < MAX_CUT_OFFS:
                    # found live: 8192-token investigator reports were cut at
                    # the limit and accepted as final
                    cut_offs += 1
                    messages.append(
                        {
                            "role": "user",
                            "content": "[Your reply hit the output limit and was cut off. "
                            "Continue exactly where it stopped, and keep the rest short.]",
                        }
                    )
                    await emit("turn", {"turn": turns, "text": "reply cut off; continuing"})
                    continue
                if cut_offs:
                    # the final answer is everything after the first cut-off
                    parts = [
                        str(m.get("content") or "")
                        for m in messages[first_cut:]
                        if m.get("role") == "assistant"
                    ]
                    final = "".join(parts)
                else:
                    final = result.text
                _clear_loop(resume_file)
                return {"messages": messages, "loop_turns": turns, "final_text": final}
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
            _save_loop(resume_file, messages, turns)
        if stop_note is None:
            stop_note = f"stopped: turn budget ({limit}) reached"
            # HANDOFF NOTE (found on dogfood #2): a role cut off at its budget
            # handed over only this stop line, so every continuation started
            # by re-reading the same files. One tools-free call turns what it
            # learned into a note the next attempt starts from.
            handoff = await _handoff_note(messages)
            if handoff:
                stop_note = f"{stop_note}\n\n{handoff}"
                # in the transcript too: report writers read messages, not this
                messages.append({"role": "assistant", "content": handoff})
        _clear_loop(resume_file)
        return {"messages": messages, "loop_turns": turns, "final_text": stop_note}

    return node
