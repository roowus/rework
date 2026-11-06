"""Roles ask the orchestrator: a BLOCKING tool, not an interrupt.

`ask_orchestrator(question, kind)` writes the question onto the role's run row
(`Run.escalation`), publishes ESCALATED, then waits until an answer appears on
the same row. On an orchestrated mission (ADR 0007) the orchestrator sees the
question in `wait()` and answers it itself or asks the owner; on a legacy
mission the tool opens a question gate for the owner directly.

Why not interrupt(): a role's whole agent loop is ONE graph node, so an
interrupt mid-loop would replay every earlier turn on resume (ADR 0002). A
blocking tool call keeps the loop's state in memory and needs no replay.

The row is the source of truth (CLAUDE.md rule 3): any process can answer by
writing `escalation.answer`, and the asking role polls for it.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from rework.kernel.llm.types import ToolSpec
from rework.kernel.state.records import EventRecord, EventType, GateKind, GateStatus, RunStatus

ASK_SPEC = ToolSpec(
    name="ask_orchestrator",
    description=(
        "Ask the mission's orchestrator when you are genuinely blocked or need a decision "
        "you should not make alone (scope, intent, missing access or context). Blocks until "
        "answered. Do not ask about things you can find out yourself."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "One clear question with context."},
            "kind": {
                "type": "string",
                "enum": ["question", "blocked", "needs_context", "needs_model"],
            },
        },
        "required": ["question"],
    },
)

ANSWER_TIMEOUT_S = 45 * 60
POLL_S = 1.0


class EscalationAwareExecutor:
    """Wraps a ToolExecutor; answers `ask_orchestrator`, delegates the rest."""

    def __init__(self, inner: Any, ctx: Any, poll_s: float = POLL_S) -> None:
        self._inner = inner
        self._ctx = ctx
        self._poll = poll_s

    def __getattr__(self, item: str) -> Any:
        return getattr(self._inner, item)

    def specs(self, allowed: set[str] | None = None) -> list[ToolSpec]:
        base = self._inner.specs(allowed)
        if allowed is not None and ASK_SPEC.name not in allowed:
            return base
        return [*base, ASK_SPEC]

    async def execute(self, name: str, args: dict[str, Any]) -> str:
        if name != ASK_SPEC.name:
            return str(await self._inner.execute(name, args))
        question = str(args.get("question") or "").strip()
        if not question:
            return "error: a question is required"
        kind = str(args.get("kind") or "question")
        mission = await self._ctx.store.get_mission(self._ctx.mission.id)
        if mission is not None and (mission.plan or {}).get("orchestrator"):
            return await self._ask_orchestrator(question, kind)
        return await self._ask_owner(question)

    async def _ask_orchestrator(self, question: str, kind: str) -> str:
        store = self._ctx.store
        run_id = self._ctx.run.id
        ask_id = uuid.uuid4().hex[:10]
        payload = {
            "id": ask_id,
            "kind": kind,
            "question": question[:2000],
            "asked_at": time.time(),
            "answer": None,
        }
        await store.update_run(run_id, escalation=payload)
        await self._ctx.bus.publish(
            EventRecord(
                mission_id=self._ctx.mission.id,
                run_id=run_id,
                type=EventType.ESCALATED,
                payload={"kind": kind, "question": question[:500]},
            )
        )
        deadline = time.monotonic() + ANSWER_TIMEOUT_S
        while time.monotonic() < deadline:
            await asyncio.sleep(self._poll)
            run = await store.get_run(run_id)
            if run is None or run.status in (RunStatus.ABORTED, RunStatus.FAILED):
                return "the orchestrator stopped this run"
            esc = run.escalation or {}
            if esc.get("id") == ask_id and esc.get("answer") is not None:
                return f"orchestrator: {esc['answer']}"
        return "no answer within the time limit; proceed with your best judgment and say so"

    async def _ask_owner(self, question: str) -> str:
        """Legacy (non-orchestrated) missions: the owner answers via a gate."""
        gate = await self._ctx.gates.request(
            self._ctx.mission.id,
            kind=GateKind.QUESTION,
            question=question[:2000],
            for_run=self._ctx.run.id,
            spec={"placeholder": "your answer"},
        )
        deadline = time.monotonic() + ANSWER_TIMEOUT_S
        while time.monotonic() < deadline:
            await asyncio.sleep(self._poll)
            row = await self._ctx.store.get_gate(gate.id)
            if row is not None and row.status != GateStatus.OPEN:
                return f"owner: {answer_text(row)}"
        return "no answer within the time limit; proceed with your best judgment and say so"


def answer_text(gate: Any) -> str:
    """A decided gate as one line a model can act on."""
    decision = gate.decision or {}
    approved = gate.status == GateStatus.APPROVED
    text = (
        decision.get("answer")
        or decision.get("choice")
        or decision.get("feedback")
        or decision.get("note")
        or gate.decision_note
        or ""
    )
    if gate.kind == GateKind.TASK:
        done = decision.get("done", approved)
        return ("done" if done else "not done") + (f": {text}" if text else "")
    verdict = "approved" if approved else "rejected"
    return f"{verdict}: {text}" if text else verdict
