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

ToolLoopNode = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


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
) -> ToolLoopNode:
    entry = catalog.resolve(catalog_id)

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
        messages: list[dict[str, Any]] = list(state.get("messages") or [])
        turns = 0
        stop_note: str | None = None
        specs = executor.specs()

        for _ in range(max_iterations):
            spend = await store.mission_spend(mission_id)
            try:
                guard(spend, budget)
            except Exception as exc:
                stop_note = f"stopped: {exc}"
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
                out = await executor.execute(tc.name, dict(tc.arguments))
                messages.append(
                    {"role": "tool", "tool_call_id": tc.id, "name": tc.name, "content": out}
                )
        if stop_note is None:
            stop_note = f"stopped: iteration guard ({max_iterations})"
        return {"messages": messages, "loop_turns": turns, "final_text": stop_note}

    return node
