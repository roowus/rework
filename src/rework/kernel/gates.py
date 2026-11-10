"""GateManager: human approval gates.

Two halves: the engine side (`request`) creates the gate row and returns it so the
pausing node can `interrupt()` immediately after (interrupt FIRST side effect;
CLAUDE.md rule 1; the row write is idempotent-ish bookkeeping that precedes it);
the client side (`decide`) is what `rework approve` calls, from any machine
sharing the database. The gate row is the single source of truth: the engine
resumes from the row, never from memory, so decisions made while the daemon was
down are honored (boot recovery reads open gates before the first astream).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from rework.kernel.events.bus import EventBus
from rework.kernel.state.records import (
    EventRecord,
    EventType,
    Gate,
    GateKind,
    GateStatus,
    new_id,
)
from rework.kernel.state.store import StateStore

log = logging.getLogger("rework.gates")


_notify_tasks: set[asyncio.Task[None]] = set()


def notify_owner(gate: Gate) -> asyncio.Task[None] | None:
    """Run REWORK_NOTIFY_CMD when a gate opens, fire-and-forget. Found live: a
    budget gate waited 4.5 hours because nobody was looking at the TUI. The
    command gets REWORK_GATE_ID / _KIND / _QUESTION and REWORK_MISSION_ID in its
    environment, e.g. a macOS banner:

        REWORK_NOTIFY_CMD='osascript -e "display notification (system attribute
        \"REWORK_GATE_QUESTION\") with title \"rework needs you\""'

    or a phone push: curl -d "$REWORK_GATE_QUESTION" ntfy.sh/<topic>. A slow or
    broken notifier never blocks or fails the mission (30s cap, errors logged)."""
    command = os.environ.get("REWORK_NOTIFY_CMD", "").strip()
    if not command:
        return None
    env = {
        **os.environ,
        "REWORK_GATE_ID": gate.id,
        "REWORK_GATE_KIND": gate.kind.value,
        "REWORK_GATE_QUESTION": gate.question[:500],
        "REWORK_MISSION_ID": gate.mission_id,
    }

    async def run() -> None:
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                env=env,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            _, err = await asyncio.wait_for(proc.communicate(), timeout=30)
            if proc.returncode:
                log.warning("notify command exited %s: %s", proc.returncode, err.decode()[:200])
        except Exception as exc:  # never let a notifier hurt a mission
            log.warning("notify command failed: %s", exc)

    try:
        task = asyncio.get_running_loop().create_task(run())
    except RuntimeError:
        return None
    _notify_tasks.add(task)
    task.add_done_callback(_notify_tasks.discard)
    return task


class GateManager:
    def __init__(self, store: StateStore, bus: EventBus) -> None:
        self._store = store
        self._bus = bus

    async def request(
        self,
        mission_id: str,
        kind: GateKind,
        question: str,
        for_run: str | None = None,
        spec: dict[str, Any] | None = None,
    ) -> Gate:
        """Create an OPEN gate row and publish GATE_OPENED. The caller then
        interrupts. `for_run` ties an escalation gate to the run it unblocks,
        in the SAME row (not a follow-up event: see Gate.for_run)."""
        gate = Gate(
            id=new_id("gate"),
            mission_id=mission_id,
            kind=kind,
            question=question,
            for_run=for_run,
            spec=spec,
        )
        await self._store.create_gate(gate)
        await self._bus.publish(
            EventRecord(
                mission_id=mission_id,
                type=EventType.GATE_OPENED,
                payload={
                    "gate_id": gate.id,
                    "kind": kind.value,
                    "question": question,
                    **({"spec": spec} if spec else {}),
                },
            )
        )
        notify_owner(gate)
        return gate

    async def decide(
        self,
        gate_id: str,
        approved: bool,
        note: str | None = None,
        decision: dict[str, Any] | None = None,
    ) -> Gate | None:
        """Resolve an open gate; no-op (and no event) if already decided.

        `decision` is the typed answer the paused role receives (see
        Gate.decision); `note` stays human-facing."""
        before = await self._store.get_gate(gate_id)
        if before is None or before.status != GateStatus.OPEN:
            return before
        gate = await self._store.decide_gate(gate_id, approved, note, decision)
        if gate is not None and gate.status != GateStatus.OPEN:
            await self._bus.publish(
                EventRecord(
                    mission_id=gate.mission_id,
                    type=EventType.GATE_DECIDED,
                    payload={
                        "gate_id": gate.id,
                        "approved": approved,
                        "note": note,
                        "decision": decision,
                    },
                )
            )
        return gate

    async def open_gates(self, mission_id: str | None = None) -> list[Gate]:
        return await self._store.list_open_gates(mission_id)
