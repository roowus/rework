"""The regression lock for the "gate row exists means the mission is parked"
flake class.

The engine commits a gate row in one autocommit statement and the fact a test
then relies on (mission AWAITING_GATE, or the gated run's HALTED_ESCALATED row)
in a later one. A poll on the gate row alone therefore wins that window under
load, and every test that cancels or restarts right after it reads the pre-park
state. `conftest.wait_for_gate_park` waits for BOTH facts; these tests hold the
helper to that contract against a store that reproduces the window on demand.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from conftest import wait_for_gate_park
from rework.kernel.events.bus import InProcessEventBus
from rework.kernel.gates import GateManager
from rework.kernel.state.records import (
    GateKind,
    Mission,
    MissionStatus,
    Run,
    RunStatus,
    new_id,
)
from rework.kernel.state.sqlite import SqliteStore, open_sqlite


class LyingStore:
    """A real store whose mission/run rows read STALE for the first N polls.

    That is exactly what a second connection sees inside the engine's two-write
    window: the gate row is already committed, the status row is not. Counts
    every read so a test can prove the helper kept polling instead of returning
    on the first poll where the gate row was visible."""

    def __init__(
        self,
        inner: SqliteStore,
        *,
        stale_mission_status: MissionStatus | None = None,
        stale_run_status: RunStatus | None = None,
        stale_polls: int = 0,
        stale_run_polls: int | None = None,
    ) -> None:
        self._inner = inner
        self._stale_mission_status = stale_mission_status
        self._stale_run_status = stale_run_status
        self._stale_polls = stale_polls
        self._stale_run_polls = stale_polls if stale_run_polls is None else stale_run_polls
        self.gate_reads = 0
        self.mission_reads = 0
        self.run_reads = 0

    async def list_open_gates(self, mission_id: str | None = None) -> Any:
        self.gate_reads += 1
        return await self._inner.list_open_gates(mission_id)

    async def get_mission(self, mission_id: str) -> Any:
        self.mission_reads += 1
        mission = await self._inner.get_mission(mission_id)
        if mission is not None and self.mission_reads <= self._stale_polls:
            assert self._stale_mission_status is not None
            mission = mission.model_copy(update={"status": self._stale_mission_status})
        return mission

    async def get_run(self, run_id: str) -> Any:
        self.run_reads += 1
        run = await self._inner.get_run(run_id)
        if run is not None and self.run_reads <= self._stale_run_polls:
            assert self._stale_run_status is not None
            run = run.model_copy(update={"status": self._stale_run_status})
        return run


@pytest.fixture
async def parked_world(tmp_path: Path) -> Any:
    """A mission that IS parked at an open gate in the database, so only the
    wrapper decides how long a reader keeps seeing the pre-park state."""
    store: SqliteStore = await open_sqlite(str(tmp_path / "barrier.sqlite3"))
    try:
        mission = await store.create_mission(
            Mission(
                id=new_id("m"),
                objective="park me",
                status=MissionStatus.AWAITING_GATE,
            )
        )
        run = Run(
            id=f"{mission.id}:do:echo:1",
            mission_id=mission.id,
            role="echo",
            step="do",
            status=RunStatus.HALTED_ESCALATED,
        )
        await store.create_run(run)
        await GateManager(store, InProcessEventBus(store)).request(
            mission.id, kind=GateKind.GENERIC, question="proceed?", for_run=run.id
        )
        yield {"store": store, "mission": mission, "run": run}
    finally:
        await store.aclose()


async def test_the_barrier_does_not_return_on_the_gate_row_alone(parked_world: Any) -> None:
    """A visible gate row is NOT the barrier. While the mission row still reads
    RUNNING, the helper must keep polling (and then give up on its own single
    timeout) instead of handing the test a half-parked mission."""
    store: SqliteStore = parked_world["store"]
    mission: Mission = parked_world["mission"]
    lying = LyingStore(
        store,
        stale_mission_status=MissionStatus.RUNNING,
        stale_polls=10**9,  # never catches up: the status write never lands
    )
    with pytest.raises(TimeoutError):
        await wait_for_gate_park(
            lying, mission.id, mission_status=MissionStatus.AWAITING_GATE, timeout_s=0.5
        )
    # it really did poll through the gate row every iteration, many times
    assert lying.gate_reads >= 5, lying.gate_reads
    assert lying.mission_reads >= 5, lying.mission_reads
    # ... and the underlying row was parked the whole time: the helper refused
    # to return on a state the test cannot act on
    assert await store.get_mission(mission.id) is not None


async def test_the_barrier_returns_once_the_mission_row_is_parked(parked_world: Any) -> None:
    """Same store, but the status write lands on the 6th read: the helper must
    wait exactly that long and then return (never earlier, never later)."""
    store: SqliteStore = parked_world["store"]
    mission: Mission = parked_world["mission"]
    lying = LyingStore(store, stale_mission_status=MissionStatus.RUNNING, stale_polls=5)
    await wait_for_gate_park(
        lying, mission.id, mission_status=MissionStatus.AWAITING_GATE, timeout_s=10
    )
    assert lying.mission_reads == 6, "the helper returned before the park or polled past it"
    assert lying.gate_reads == 6, lying.gate_reads


async def test_the_barrier_waits_for_the_gated_run_row_too(parked_world: Any) -> None:
    """The chaos flavour: mission status can already say AWAITING_GATE while the
    gated run is not parked yet. A restart in that window re-fires the interrupt
    and opens a duplicate gate, so the run row is part of the barrier."""
    store: SqliteStore = parked_world["store"]
    mission: Mission = parked_world["mission"]
    run: Run = parked_world["run"]
    # only the RUN row lags here: the mission row is parked for real
    running_run = LyingStore(
        store,
        stale_run_status=RunStatus.RUNNING,
        stale_run_polls=10**9,
    )
    with pytest.raises(TimeoutError):
        await wait_for_gate_park(
            running_run,
            mission.id,
            mission_status=MissionStatus.AWAITING_GATE,
            run_id=run.id,
            timeout_s=0.5,
        )
    assert running_run.run_reads >= 5, running_run.run_reads
    # once the run's park lands, the same call returns
    caught_up = LyingStore(
        store,
        stale_run_status=RunStatus.RUNNING,
        stale_run_polls=4,
    )
    await wait_for_gate_park(
        caught_up,
        mission.id,
        mission_status=MissionStatus.AWAITING_GATE,
        run_id=run.id,
        timeout_s=10,
    )
    assert caught_up.run_reads == 5, caught_up.run_reads
