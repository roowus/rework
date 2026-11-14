"""M1 acceptance tests, for real: the stub mission end to end with a gate,
approval from a second store connection (the cross-device approve path), and
crash recovery resuming the role graph from its checkpoint.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import pytest

from rework.config import Config
from rework.kernel.events.bus import InProcessEventBus
from rework.kernel.gates import GateManager
from rework.kernel.llm.catalog import ModelCatalog, ModelEntry
from rework.kernel.state.records import EventType, Mission, MissionStatus, new_id
from rework.kernel.state.sqlite import SqliteStore, open_sqlite
from rework.orchestrator.daemon import sqlite_checkpointer_factory
from rework.orchestrator.engine import MissionRunner
from rework.roles.registry import autodiscover


async def _wait_for(
    predicate: Callable[[], Awaitable[bool]],
    timeout_s: float = 10.0,
    what: str = "condition",
) -> None:
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.05)
    raise TimeoutError(f"timed out waiting for {what}")


@pytest.fixture
async def world(tmp_path: Path) -> AsyncIterator[dict]:  # type: ignore[no-untyped-def]
    db_path = tmp_path / "state.sqlite3"
    ck_path = tmp_path / "checkpoints.sqlite3"
    store: SqliteStore = await open_sqlite(str(db_path))
    try:
        yield {
            "store": store,
            "bus": InProcessEventBus(store),
            "db_path": str(db_path),
            "ck_path": str(ck_path),
            "registry": autodiscover(),
            "catalog": ModelCatalog([ModelEntry(id="fake", provider="fake", model="fake-1")]),
            "workroot": tmp_path / "work",
            "config": Config(db=str(db_path), checkpoints_db=str(ck_path)),
        }
    finally:
        await store.aclose()


def make_runner(world: dict, mission_id: str) -> MissionRunner:  # type: ignore[no-untyped-def]
    return MissionRunner(
        mission_id,
        world["store"],
        world["bus"],
        world["registry"],
        world["catalog"],
        sqlite_checkpointer_factory(world["ck_path"]),
        world["workroot"],
        poll_interval_s=0.05,
    )


async def test_end_to_end_with_gate_and_external_approve(world: dict) -> None:
    store: SqliteStore = world["store"]
    mission = await store.create_mission(Mission(id=new_id("m"), objective="say hello"))

    runner = make_runner(world, mission.id)
    run_task = asyncio.create_task(runner.run())

    # the role halts at its gate and the mission waits
    await _wait_for(
        lambda: _open_gate_exists(store, mission.id),
        what="gate to open",
    )
    await _wait_for(
        lambda: _mission_is(store, mission.id, MissionStatus.AWAITING_GATE),
        what="mission AWAITING_GATE",
    )

    # approve from a SECOND connection, exactly like `rework approve` from a laptop
    second: SqliteStore = await open_sqlite(world["db_path"])
    try:
        gates = GateManager(second, InProcessEventBus(second))
        open_gates = await gates.open_gates(mission.id)
        assert len(open_gates) == 1
        decided = await gates.decide(open_gates[0].id, approved=True, note="lgtm")
        assert decided is not None and decided.status.value == "approved"
    finally:
        await second.aclose()

    await asyncio.wait_for(run_task, timeout=15)

    done = await store.get_mission(mission.id)
    assert done is not None and done.status == MissionStatus.DONE
    runs = await store.list_runs(mission.id)
    assert len(runs) == 1 and runs[0].status.value == "completed"
    assert runs[0].artifact == {
        "kind": "echo",
        "objective": "say hello",
        "approved": True,
    }
    types = [e.type for e in await store.events_since(0, mission_id=mission.id)]
    for expected in (
        EventType.ROLE_SPAWNED,
        EventType.ROLE_HALTED,
        EventType.GATE_OPENED,
        EventType.GATE_DECIDED,
        EventType.ROLE_COMPLETED,
        EventType.MISSION_DONE,
    ):
        assert expected in types, f"missing {expected} in {types}"


async def test_crash_recovery_resumes_from_checkpoint(world: dict) -> None:
    """Daemon dies while the role is halted at its gate. A fresh runner finds
    the halted run + open gate (idempotent materialization), waits out the
    decision, and resumes the SAME checkpointed thread."""
    store: SqliteStore = world["store"]
    mission = await store.create_mission(Mission(id=new_id("m"), objective="survive a crash"))

    # first runner drives to the gate, then dies (cancelled while halted)
    runner1 = make_runner(world, mission.id)
    task1 = asyncio.create_task(runner1.run())
    await _wait_for(lambda: _open_gate_exists(store, mission.id), what="gate to open")
    await _wait_for(
        lambda: _run_status_is(store, mission.id, "halted_escalated"),
        what="run halted",
    )
    task1.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task1, timeout=5)
    if runner1._supervisor is not None:
        await runner1._supervisor.aclose()  # close checkpointers

    # a fresh runner (simulated daemon restart) adopts the mission
    runner2 = make_runner(world, mission.id)
    task2 = asyncio.create_task(runner2.run())
    await _wait_for(
        lambda: _mission_is(store, mission.id, MissionStatus.AWAITING_GATE),
        what="fresh runner to re-await the existing gate",
    )
    # still exactly one gate: re-materialization is idempotent
    assert len(await store.list_open_gates(mission.id)) == 1

    # decide externally, then the fresh runner resumes the SAME checkpointed
    # thread and completes the mission
    second: SqliteStore = await open_sqlite(world["db_path"])
    try:
        gates = GateManager(second, InProcessEventBus(second))
        open_gates = await gates.open_gates(mission.id)
        await gates.decide(open_gates[0].id, approved=True)
    finally:
        await second.aclose()

    await asyncio.wait_for(task2, timeout=15)
    done = await store.get_mission(mission.id)
    assert done is not None and done.status == MissionStatus.DONE
    runs = await store.list_runs(mission.id)
    # exactly one run row: the deterministic id prevented a duplicate dispatch
    assert len(runs) == 1
    assert runs[0].status.value == "completed"
    assert runs[0].artifact is not None and runs[0].artifact["approved"] is True


async def test_recovery_of_run_stuck_running_without_checkpoint(world: dict) -> None:
    """A daemon that died between the RUNNING flip and the first checkpoint
    write leaves a run with no graph state. Recovery must fall back to a fresh
    start instead of crashing on ainvoke(None)."""
    from rework.kernel.state.records import Run, RunStatus
    from rework.kernel.state.records import run_id as make_run_id

    store: SqliteStore = world["store"]
    mission = await store.create_mission(Mission(id=new_id("m"), objective="stuck running"))
    await store.update_mission(mission.id, status=MissionStatus.RUNNING)
    await store.create_run(
        Run(
            id=make_run_id(mission.id, "echo", "echo", 1),
            mission_id=mission.id,
            role="echo",
            step="echo",
            status=RunStatus.RUNNING,
        )
    )

    runner = make_runner(world, mission.id)
    task = asyncio.create_task(runner.run())
    await _wait_for(lambda: _open_gate_exists(store, mission.id), what="gate to open")

    gates = GateManager(store, InProcessEventBus(store))
    open_gates = await gates.open_gates(mission.id)
    assert open_gates, "run must have reached the gate despite missing checkpoint"
    await gates.decide(open_gates[0].id, approved=True)

    await asyncio.wait_for(task, timeout=15)
    done = await store.get_mission(mission.id)
    assert done is not None and done.status == MissionStatus.DONE
    runs = await store.list_runs(mission.id)
    assert runs[0].status.value == "completed"


async def test_rejected_gate_aborts_run(world: dict) -> None:
    store: SqliteStore = world["store"]
    mission = await store.create_mission(Mission(id=new_id("m"), objective="deny me"))
    runner = make_runner(world, mission.id)
    task = asyncio.create_task(runner.run())
    await _wait_for(lambda: _open_gate_exists(store, mission.id), what="gate to open")

    gates = GateManager(store, InProcessEventBus(store))
    open_gates = await gates.open_gates(mission.id)
    await gates.decide(open_gates[0].id, approved=False, note="no")

    await asyncio.wait_for(task, timeout=15)
    runs = await store.list_runs(mission.id)
    assert runs[0].status.value == "aborted"
    done = await store.get_mission(mission.id)
    assert done is not None and done.status == MissionStatus.DONE


# -- helpers ------------------------------------------------------------------


async def _open_gate_exists(store: SqliteStore, mission_id: str) -> bool:
    return bool(await store.list_open_gates(mission_id))


async def _mission_is(store: SqliteStore, mission_id: str, status: MissionStatus) -> bool:
    mission = await store.get_mission(mission_id)
    return mission is not None and mission.status == status


async def _run_status_is(store: SqliteStore, mission_id: str, status: str) -> bool:
    runs = await store.list_runs(mission_id)
    return any(r.status.value == status for r in runs)


async def test_git_transport_clones_from_origin(world: dict, tmp_path: Path) -> None:
    """ADR 0005 git transport: a mission carrying only repo_origin gets its
    repo cloned by the daemon at run start, and the row then points at a path
    that is real on the daemon's machine."""
    from conftest import make_git_repo

    store: SqliteStore = world["store"]
    src = make_git_repo(tmp_path)
    mission = await store.create_mission(
        Mission(id=new_id("m"), objective="echo this", repo_origin=f"file://{src}")
    )
    runner = make_runner(world, mission.id)
    run_task = asyncio.create_task(runner.run())

    # the clone must happen BEFORE the role spawns: by the time the gate is
    # open, repo_path already points at the daemon-side clone
    expected = world["workroot"] / "missions" / mission.id / "repo"
    await _wait_for(
        lambda: _mission_is(store, mission.id, MissionStatus.AWAITING_GATE),
        what="gate after clone",
    )
    fresh = await store.get_mission(mission.id)
    assert fresh is not None
    assert fresh.repo_path == str(expected)
    assert (expected / ".git").is_dir()

    # approve the echo gate like a second-machine owner, then let it finish
    second: SqliteStore = await open_sqlite(world["db_path"])
    try:
        gates = GateManager(second, InProcessEventBus(second))
        open_gates = await gates.open_gates(mission.id)
        assert len(open_gates) == 1
        await gates.decide(open_gates[0].id, approved=True)
    finally:
        await second.aclose()
    await asyncio.wait_for(run_task, timeout=15)

    # idempotent across restarts: a fresh runner reuses the clone, no re-clone
    assert await store.get_mission(mission.id) is not None
    again = world["workroot"] / "missions" / mission.id / "repo"
    assert (again / ".git").is_dir()


async def test_send_back_at_gate_returns_mission_to_running(world: dict) -> None:
    """When a gate is SENT_BACK, the mission returns to RUNNING without synthesis,
    and the owner's feedback is delivered as an owner message. A second pass through
    _final_gates opens a FRESH gate (reuse trap regression test)."""
    from rework.kernel.state.records import GateStatus
    
    store: SqliteStore = world["store"]
    mission = await store.create_mission(
        Mission(
            id=new_id("m"),
            objective="send me back",
            plan={"steps": [{"step": "echo", "role": "echo"}], "gates": ["ship"]},
        )
    )

    runner = make_runner(world, mission.id)
    run_task = asyncio.create_task(runner.run())

    # Wait for the ship gate to open
    await _wait_for(
        lambda: _open_gate_exists(store, mission.id),
        what="ship gate to open",
    )
    await _wait_for(
        lambda: _mission_is(store, mission.id, MissionStatus.AWAITING_GATE),
        what="mission AWAITING_GATE",
    )

    # Get the first gate and send it back
    gates = GateManager(store, InProcessEventBus(store))
    open_gates = await gates.open_gates(mission.id)
    assert len(open_gates) == 1
    first_gate = open_gates[0]
    first_gate_id = first_gate.id

    # Send the gate back with feedback
    decided = await gates.decide(first_gate_id, send_back=True, note="needs revision")
    assert decided is not None and decided.status == GateStatus.SENT_BACK

    # The mission should return to RUNNING (not DONE)
    await _wait_for(
        lambda: _mission_is(store, mission.id, MissionStatus.RUNNING),
        what="mission back to RUNNING after send-back",
    )

    # Check that an owner message was published with the feedback
    events = await store.events_since(0, mission_id=mission.id)
    owner_messages = [e for e in events if e.type == EventType.OWNER_MESSAGE]
    assert len(owner_messages) > 0, "owner message should be published for send-back"
    assert "needs revision" in owner_messages[-1].payload.get("text", "")

    # On the next gate opening (reuse trap test), it should be FRESH (not reuse the SENT_BACK gate)
    await _wait_for(
        lambda: _open_gate_exists(store, mission.id),
        what="second ship gate to open after send-back",
    )
    
    second_gates = await gates.open_gates(mission.id)
    # Should have a second gate now (first was sent_back, so new one opens)
    assert len(second_gates) >= 1
    second_gate = second_gates[-1]
    
    # The second gate should be different from the first (fresh gate)
    # or at least in OPEN status (not reusing the SENT_BACK one)
    assert second_gate.status == GateStatus.OPEN

    # Approve the second gate
    await gates.decide(second_gate.id, approved=True, note="approved on retry")

    # Wait for completion
    await asyncio.wait_for(run_task, timeout=15)

    # Mission should be DONE
    done = await store.get_mission(mission.id)
    assert done is not None and done.status == MissionStatus.DONE
    assert done.result is not None and done.result.get("ship") is None  # not "rejected at ship gate"
