"""_gate_decision reads the gate ROW, not the event log: the row-update and
its event commit separately, and the decision must survive that gap (the rare
CI 'ship rejected despite approval' race)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from rework.kernel.events.bus import InProcessEventBus
from rework.kernel.gates import GateManager
from rework.kernel.state.records import EventRecord, GateKind, Mission, new_id
from rework.kernel.state.sqlite import open_sqlite
from rework.orchestrator.engine import MissionRunner


class DropBus:
    """A bus that drops every event: the event log has NO GATE_DECIDED, only
    the row. The row is the truth and must decide."""

    async def publish(self, event: EventRecord) -> EventRecord:  # type: ignore[no-untyped-def]
        return event


async def test_gate_decision_reads_row_even_without_events(tmp_path: Path) -> None:
    store = await open_sqlite(str(tmp_path / "gd.sqlite3"))
    try:
        gates = GateManager(store, InProcessEventBus(store))
        mission = await store.create_mission(Mission(id=new_id("m"), objective="x"))
        gate = await gates.request(mission.id, kind=GateKind.SHIP, question="ship?")
        await gates.decide(gate.id, approved=True)

        runner = MissionRunner(
            mission.id,
            store,
            DropBus(),  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            tmp_path,  # type: ignore[arg-type]
        )
        decision: bool = await runner._gate_decision(mission.id, gate.id)
        assert decision is True
    finally:
        await store.aclose()


async def test_gate_decision_polls_until_decided(tmp_path: Path) -> None:
    store = await open_sqlite(str(tmp_path / "gd2.sqlite3"))
    try:
        gates = GateManager(store, InProcessEventBus(store))
        mission = await store.create_mission(Mission(id=new_id("m"), objective="x"))
        gate = await gates.request(mission.id, kind=GateKind.GENERIC, question="go?")
        runner = MissionRunner(
            mission.id,
            store,
            InProcessEventBus(store),
            None,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            tmp_path,  # type: ignore[arg-type]
        )

        # decide shortly after the poll starts: the poller must not return early
        async def decide_later() -> None:
            await asyncio.sleep(0.2)
            await gates.decide(gate.id, approved=False)

        decide_task = asyncio.create_task(decide_later())
        decision: Any = await asyncio.wait_for(
            runner._gate_decision(mission.id, gate.id), timeout=5
        )
        await decide_task
        assert decision is False
    finally:
        await store.aclose()
