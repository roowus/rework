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

The wait has NO deadline (ADR 0011). A blocked role used to be told, after 45
minutes, to "proceed with your best judgment", which turned silence into an
unverified guess. Now it parks until an answer lands, an abort/cancel arrives,
or the daemon stops it, and it hands both its concurrency slots back while it
waits (`RoleBuildCtx.parked`) so a parked role costs nothing. The ask id is
derived from the run and the question, so a restart that replays the tool
consumes an answer written while the daemon was down instead of asking again.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
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

POLL_S = 1.0


def _ask_id(run_id: str, question: str, seq: int = 0) -> str:
    """A stable id for one ask on one run.

    Deterministic so a daemon restart can recognise a question it already wrote.
    `seq` distinguishes a later ask of the same text from the first (a role may
    legitimately ask again once it has had an answer); `0` is the first ask.
    """
    digest = hashlib.sha256(f"{run_id}\n{question}\n{seq}".encode()).hexdigest()
    return f"ask-{digest[:12]}"


class EscalationAwareExecutor:
    """Wraps a ToolExecutor; answers `ask_orchestrator`, delegates the rest.

    `expose_ask` is the role saying it can be blocked: the wrapping is a
    deliberate choice at build time, not a side effect of the toolloop passing
    `allowed=None`. A wrapper that should only answer (never advertise) sets it
    False."""

    def __init__(
        self, inner: Any, ctx: Any, poll_s: float = POLL_S, *, expose_ask: bool = True
    ) -> None:
        self._inner = inner
        self._ctx = ctx
        self._poll = poll_s
        self._expose_ask = expose_ask
        # Ask ids this executor has already returned an answer for. Distinguishes
        # a legitimate repeat ask from a restart replay (see _ask_orchestrator).
        self._served: set[str] = set()

    def __getattr__(self, item: str) -> Any:
        return getattr(self._inner, item)

    def specs(self, allowed: set[str] | None = None) -> list[ToolSpec]:
        base = self._inner.specs(allowed)
        if not self._expose_ask:
            return base
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
        run = await store.get_run(run_id)
        if run is None or run.status in (RunStatus.ABORTED, RunStatus.FAILED):
            return "the orchestrator stopped this run"
        current = run.escalation or {}

        # Two cases look identical on the row and must behave differently:
        #
        # 1. A REPLAY. The daemon restarted while the role was blocked, so the
        #    tool call runs again. An answer may have been written while the
        #    daemon was down, and the role never received it: consume it.
        # 2. A REPEAT ASK. The role got its answer, continued, and asked the
        #    same thing again. The row still holds the old answered ask; it
        #    must NOT be handed back a second time.
        #
        # `_served` is what separates them: it remembers the asks this executor
        # returned, so a repeat is recognised in-process. A replay is by
        # definition an ask this instance never served (a fresh process has an
        # empty set), which is exactly the case that must consume the answer.
        open_ask = bool(current.get("id")) and current.get("answer") is None
        answered_replay = (
            bool(current.get("id"))
            and current.get("answer") is not None
            and str(current["id"]) not in self._served
        )
        if open_ask:
            ask_id = str(current["id"])
        elif answered_replay and current.get("question") == question[:2000]:
            ask_id = str(current["id"])
        else:
            # a new question (or a genuine repeat): a fresh id, never colliding
            # with the one already on the row
            ask_id = _ask_id(run_id, question, len(self._served))
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
        async with self._ctx.parked():
            while True:
                await asyncio.sleep(self._poll)
                run = await store.get_run(run_id)
                if run is None or run.status in (RunStatus.ABORTED, RunStatus.FAILED):
                    return "the orchestrator stopped this run"
                esc = run.escalation or {}
                if esc.get("id") == ask_id and esc.get("answer") is not None:
                    self._served.add(ask_id)
                    return f"orchestrator: {esc['answer']}"

    async def _ask_owner(self, question: str) -> str:
        """Legacy (non-orchestrated) missions: the owner answers via a gate."""
        gate = await self._ctx.gates.request(
            self._ctx.mission.id,
            kind=GateKind.QUESTION,
            question=question[:2000],
            for_run=self._ctx.run.id,
            spec={"placeholder": "your answer"},
        )
        async with self._ctx.parked():
            while True:
                await asyncio.sleep(self._poll)
                row = await self._ctx.store.get_gate(gate.id)
                if row is not None and row.status != GateStatus.OPEN:
                    return f"owner: {answer_text(row)}"
                run = await self._ctx.store.get_run(self._ctx.run.id)
                if run is None or run.status in (RunStatus.ABORTED, RunStatus.FAILED):
                    return "the orchestrator stopped this run"


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
