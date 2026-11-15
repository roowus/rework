"""A mission's status must match its gates: AWAITING_GATE means "the owner has
something to decide", so it cannot outlive the last open gate.

Found live (2026-11-14): two missions sat under "needs you" while the Gates tab
was empty, because their gates were approved and the status label was never
set back. These tests pin the fix at both layers: the engine clears the label
the moment the gate is decided, and the daemon repairs any row that slipped
through (a crash, a restart, an older build)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from rework.kernel.events.bus import InProcessEventBus
from rework.kernel.gates import GateManager
from rework.kernel.state.records import GateKind, GateStatus, Mission, MissionStatus
from rework.kernel.state.sqlite import open_sqlite
from rework.orchestrator.daemon import Daemon
from rework.orchestrator.engine import MissionRunner


async def _store(tmp_path: Path):  # type: ignore[no-untyped-def]
    return await open_sqlite(str(tmp_path / "s.sqlite3"))


def _runner(store, bus, mission_id: str) -> MissionRunner:  # type: ignore[no-untyped-def]
    return MissionRunner(
        mission_id,
        store,
        bus,
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        Path("."),
        poll_interval_s=0.01,
    )


async def test_deciding_a_gate_moves_the_mission_back_to_running(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    bus = InProcessEventBus(store)
    await store.create_mission(Mission(id="m1", objective="x"))
    gates = GateManager(store, bus)
    gate = await gates.request("m1", kind=GateKind.OPEN_PR, question="open the PR?")
    await store.update_mission("m1", status=MissionStatus.AWAITING_GATE)
    await gates.decide(gate.id, approved=True)

    runner = _runner(store, bus, "m1")
    assert await runner._gate_decision("m1", gate.id) is True
    mission = await store.get_mission("m1")
    assert mission is not None and mission.status == MissionStatus.RUNNING
    await store.aclose()


async def test_a_second_open_gate_keeps_the_mission_awaiting(tmp_path: Path) -> None:
    """Negative control: only the LAST open gate clears the label."""
    store = await _store(tmp_path)
    bus = InProcessEventBus(store)
    await store.create_mission(Mission(id="m1", objective="x"))
    gates = GateManager(store, bus)
    first = await gates.request("m1", kind=GateKind.OPEN_PR, question="a?")
    await gates.request("m1", kind=GateKind.GENERIC, question="b?")
    await store.update_mission("m1", status=MissionStatus.AWAITING_GATE)
    await gates.decide(first.id, approved=True)

    await _runner(store, bus, "m1")._gate_decision("m1", first.id)
    mission = await store.get_mission("m1")
    assert mission is not None and mission.status == MissionStatus.AWAITING_GATE
    await store.aclose()


async def test_a_paused_mission_is_not_flipped_to_running(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    bus = InProcessEventBus(store)
    await store.create_mission(Mission(id="m1", objective="x"))
    gates = GateManager(store, bus)
    gate = await gates.request("m1", kind=GateKind.SHIP, question="ship?")
    await gates.decide(gate.id, approved=True)
    await store.update_mission("m1", status=MissionStatus.ON_HOLD)  # owner paused it

    await _runner(store, bus, "m1")._clear_awaiting_gate("m1")
    mission = await store.get_mission("m1")
    assert mission is not None and mission.status == MissionStatus.ON_HOLD
    await store.aclose()


def _daemon(store, bus, tmp_path: Path) -> Daemon:  # type: ignore[no-untyped-def]
    cfg = SimpleNamespace(concurrency=1, max_roles=1, poll_interval_s=0.05, git_author=None)
    return Daemon(
        store,
        bus,
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        tmp_path,
        cfg,  # type: ignore[arg-type]
    )


async def test_the_daemon_repairs_awaiting_gate_with_no_open_gate(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    bus = InProcessEventBus(store)
    await store.create_mission(Mission(id="stale", objective="x"))
    await store.update_mission("stale", status=MissionStatus.AWAITING_GATE)
    await store.create_mission(Mission(id="real", objective="y"))
    gate = await GateManager(store, bus).request("real", kind=GateKind.SHIP, question="ship?")
    await store.update_mission("real", status=MissionStatus.AWAITING_GATE)

    fixed = await _daemon(store, bus, tmp_path)._repair_stale_gate_status()

    assert fixed == 1
    stale = await store.get_mission("stale")
    real = await store.get_mission("real")
    assert stale is not None and stale.status == MissionStatus.RUNNING
    assert real is not None and real.status == MissionStatus.AWAITING_GATE, "a real gate stays"
    assert (await store.get_gate(gate.id)).status == GateStatus.OPEN  # type: ignore[union-attr]
    events = await store.events_since(0, mission_id="stale", limit=20)
    assert any("awaiting_gate corrected" in str(e.payload) for e in events)
    await store.aclose()
