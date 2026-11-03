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


class GateManager:
    def __init__(self, store: StateStore, bus: EventBus) -> None:
        self._store = store
        self._bus = bus

    async def request(self, mission_id: str, kind: GateKind, question: str) -> Gate:
        """Create an OPEN gate row and publish GATE_OPENED. The caller then interrupts."""
        gate = Gate(id=new_id("gate"), mission_id=mission_id, kind=kind, question=question)
        await self._store.create_gate(gate)
        await self._bus.publish(
            EventRecord(
                mission_id=mission_id,
                type=EventType.GATE_OPENED,
                payload={"gate_id": gate.id, "kind": kind.value, "question": question},
            )
        )
        return gate

    async def decide(self, gate_id: str, approved: bool, note: str | None = None) -> Gate | None:
        """Resolve an open gate; no-op (and no event) if already decided."""
        before = await self._store.get_gate(gate_id)
        if before is None or before.status != GateStatus.OPEN:
            return before
        gate = await self._store.decide_gate(gate_id, approved, note)
        if gate is not None and gate.status != GateStatus.OPEN:
            await self._bus.publish(
                EventRecord(
                    mission_id=gate.mission_id,
                    type=EventType.GATE_DECIDED,
                    payload={
                        "gate_id": gate.id,
                        "approved": approved,
                        "note": note,
                    },
                )
            )
        return gate

    async def open_gates(self, mission_id: str | None = None) -> list[Gate]:
        return await self._store.list_open_gates(mission_id)
