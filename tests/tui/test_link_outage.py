"""The TUI rides out a store outage while it polls (TuiData._resilient).

During the outage the shell shows offline (the Hub snapshot's `online` flag, which
the shell's status bar paints). When the store returns, the poll recovers on its own,
with the same TuiData and Hub (no restart), and the budget-raise cursor is not lost:
the next poll fetches the event that was written while the TUI was cut off.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from rework.kernel.state.records import EventRecord, EventType, Mission, new_id
from rework.kernel.state.sqlite import open_sqlite
from rework.tui.data import TuiData
from rework.tui.hub import Hub
from support.fault_store import FaultStore


async def test_the_poll_rides_out_an_outage_and_recovers_with_no_restart(
    tmp_path: Path, caplog: Any
) -> None:
    db = str(tmp_path / "tui.sqlite3")
    real = await open_sqlite(db)
    fault = FaultStore(real)
    opened: list[FaultStore] = []

    async def open_store() -> FaultStore:
        opened.append(fault)  # every (re)open of the TUI's link
        return fault

    data = TuiData(store_factory=open_store)
    hub = Hub(data)
    painted: list[Any] = []  # what the shell was handed on each repaint
    hub.subscribe(painted.append)
    try:
        mission = await real.create_mission(Mission(id=new_id("m"), objective="outage"))
        await hub.refresh()  # first poll: loads everything, primes the budget cursor
        assert hub.snapshot.loaded and hub.snapshot.online
        assert painted[-1].online is True

        fault.set_down(True)  # the link drops: every call on this store now fails
        writer = await open_sqlite(db)  # the orchestrator keeps writing meanwhile
        try:
            await writer.append_event(
                EventRecord(
                    mission_id=mission.id,
                    type=EventType.BUDGET_RAISED,
                    payload={"from": 1.0, "to": 2.0},
                )
            )
        finally:
            await writer.aclose()

        await hub.refresh()  # must return normally: no exception reaches the shell
        assert hub.snapshot.online is False, "the shell must show offline"
        assert data._reconnecting_hint, "the TUI names the outage while it lasts"
        assert painted[-1].online is False, "the offline state was painted"
        assert len(opened) >= 2, "the TUI tried to reconnect during the outage"
        assert not any(r.levelno >= logging.ERROR for r in caplog.records)

        fault.set_down(False)  # the store is back
        await hub.refresh()  # the next poll, on its own
        assert hub.snapshot.online is True
        assert data._reconnecting_hint == ""
        assert painted[-1].online is True
        assert hub.data is data, "recovered on the same TuiData: no new instance"
        # the cursor was not lost: the event written during the outage arrives now
        assert [e.type for e in hub.snapshot.budget_raises] == [EventType.BUDGET_RAISED]

        await hub.refresh()  # and it is not announced twice
        assert hub.snapshot.budget_raises == []
        assert not any(r.levelno >= logging.ERROR for r in caplog.records)
    finally:
        await hub.aclose()
        await real.aclose()
