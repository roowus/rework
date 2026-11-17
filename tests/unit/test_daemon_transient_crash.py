"""A transient database error must requeue a mission, not fail it.

2026-11-14: `DeadlockDetected` (SQLSTATE 40P01) on a chat write killed the
runner, and the daemon turned any runner exception into FAILED. A deadlock is
the server telling us "run that transaction again", so the crash handler now
requeues the mission with a bounded per-mission budget and the mission resumes
from its checkpoint.

These tests pin the handler's decision table: requeue (and how many times),
terminal at the bound, an honest failure for anything that is NOT transient, no
resurrection of a finished mission, and a fresh budget after a terminal write.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from rework.kernel.events.bus import InProcessEventBus
from rework.kernel.state.records import EventType, Mission, MissionStatus
from rework.kernel.state.retry import TransientDatabaseError
from rework.kernel.state.sqlite import SqliteStore, open_sqlite
from rework.orchestrator.daemon import (
    MIN_TRANSIENT_CRASHES,
    TRANSIENT_OUTAGE_BUDGET_S,
    Daemon,
)


class Deadlock(Exception):
    """Duck-typed psycopg deadlock: the only thing the classifier reads is
    `sqlstate`, so no driver is needed here. It IS an exception subclass
    because the crash handler is handed whatever a runner raised."""

    def __init__(self, sqlstate: str = "40P01") -> None:
        super().__init__("deadlock detected on UPDATE chats")
        self.sqlstate = sqlstate


def _daemon(store: SqliteStore, bus: InProcessEventBus, tmp_path: Path) -> Daemon:
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


async def _running_mission(store: SqliteStore) -> str:
    """A mission in flight, with work already recorded on its result row."""
    await store.create_mission(
        Mission(
            id="m1",
            objective="survive a deadlock",
            repo_path="/work/m1/repo",
            result={"summary": "half the work is done"},
        )
    )
    await store.update_mission("m1", status=MissionStatus.RUNNING)
    return "m1"


async def _crash_count(store: SqliteStore, mission_id: str) -> int:
    row = await store._fetch_one(  # type: ignore[attr-defined]
        "SELECT transient_crash_count AS c FROM missions WHERE id = ?", (mission_id,)
    )
    return int(row["c"]) if row is not None else -1


async def _events(store: SqliteStore, mission_id: str) -> list[EventType]:
    return [e.type for e in await store.events_since(0, mission_id=mission_id, limit=200)]


async def test_a_deadlock_requeues_the_mission_instead_of_failing_it(tmp_path: Path) -> None:
    store = await open_sqlite(str(tmp_path / "s.sqlite3"))
    try:
        bus = InProcessEventBus(store)
        mission_id = await _running_mission(store)

        await _daemon(store, bus, tmp_path)._log_runner_error(mission_id, Deadlock())

        mission = await store.get_mission(mission_id)
        assert mission is not None
        assert mission.status == MissionStatus.PENDING, "a deadlock is not the mission's fault"
        result = mission.result or {}
        assert result["summary"] == "half the work is done", "the old result keys must survive"
        assert "40P01" in result["transient_db_error"]
        assert "crash 1" in result["crash"] and "resuming" in result["crash"]
        assert "into the outage" in result["crash"]

        events = await store.events_since(0, mission_id=mission_id, limit=200)
        assert EventType.MISSION_FAILED not in [e.type for e in events]
        infos = [e for e in events if e.type == EventType.INFO]
        assert any("resume from its checkpoint" in str(e.payload) for e in infos), infos

        # the requeue is what the existing pickup machinery claims
        pending = [m.id for m in await store.next_pending_missions()]
        assert pending == [mission_id]
    finally:
        await store.aclose()


async def test_the_real_transient_database_error_requeues_too(tmp_path: Path) -> None:
    """What the store's own retry raises once it has exhausted its attempts."""
    store = await open_sqlite(str(tmp_path / "s.sqlite3"))
    try:
        bus = InProcessEventBus(store)
        mission_id = await _running_mission(store)
        exhausted = TransientDatabaseError(
            "SqliteStore.update_mission failed after 3 attempts",
            sqlstate="40P01",
            attempts=3,
        )

        await _daemon(store, bus, tmp_path)._log_runner_error(mission_id, exhausted)

        mission = await store.get_mission(mission_id)
        assert mission is not None and mission.status == MissionStatus.PENDING
        result = mission.result or {}
        assert "TransientDatabaseError" in result["transient_db_error"]
        assert "40P01" in result["transient_db_error"]
        assert "40P01" in result["crash"] or "resuming" in result["crash"]
        assert EventType.MISSION_FAILED not in await _events(store, mission_id)
    finally:
        await store.aclose()


async def test_the_crash_past_the_time_budget_fails_the_mission(tmp_path: Path) -> None:
    """Past the OUTAGE BUDGET the mission is failed: an unbounded requeue loop
    against a database that stays broken would hide the outage forever. The bound
    is elapsed time, not a count (a bad minute must not burn a mission's work)."""
    import time as _time

    store = await open_sqlite(str(tmp_path / "s.sqlite3"))
    try:
        bus = InProcessEventBus(store)
        mission_id = await _running_mission(store)
        daemon = _daemon(store, bus, tmp_path)

        # inside the budget: many crashes, still alive
        for crash in range(1, MIN_TRANSIENT_CRASHES + 4):
            await daemon._log_runner_error(mission_id, Deadlock())
            mission = await store.get_mission(mission_id)
            assert mission is not None and mission.status == MissionStatus.PENDING
            assert f"crash {crash}" in (mission.result or {})["crash"]
            assert "into the outage" in (mission.result or {})["crash"]
            await store.update_mission(mission_id, status=MissionStatus.RUNNING)

        # spend the budget on the row, then crash again
        mission = await store.get_mission(mission_id)
        assert mission is not None
        old = {**(mission.result or {})}
        old["first_transient_crash_at"] = _time.time() - TRANSIENT_OUTAGE_BUDGET_S - 1
        await store.update_mission(mission_id, result=old, status=MissionStatus.RUNNING)

        await daemon._log_runner_error(mission_id, Deadlock())

        mission = await store.get_mission(mission_id)
        assert mission is not None and mission.status == MissionStatus.FAILED
        error = (mission.result or {})["error"]
        assert "transient database error" in error
        assert "budget" in error and "giving up" in error
        assert "40P01" in error
        assert EventType.MISSION_FAILED in await _events(store, mission_id)
    finally:
        await store.aclose()


async def test_a_plain_exception_still_fails_the_mission_at_once(tmp_path: Path) -> None:
    """Negative control: only a TRANSIENT error is requeued. A bug stays a bug."""
    store = await open_sqlite(str(tmp_path / "s.sqlite3"))
    try:
        bus = InProcessEventBus(store)
        mission_id = await _running_mission(store)

        await _daemon(store, bus, tmp_path)._log_runner_error(mission_id, ValueError("boom"))

        mission = await store.get_mission(mission_id)
        assert mission is not None and mission.status == MissionStatus.FAILED
        assert (mission.result or {})["error"] == "ValueError: boom"
        assert EventType.MISSION_FAILED in await _events(store, mission_id)
        assert await _crash_count(store, mission_id) == 0, "a bug spends no crash budget"
    finally:
        await store.aclose()


async def test_a_logical_sqlstate_still_fails_the_mission_at_once(tmp_path: Path) -> None:
    """23505 (unique violation) would fail the same way on a retry: it must not
    be mistaken for the deadlock class."""
    store = await open_sqlite(str(tmp_path / "s.sqlite3"))
    try:
        bus = InProcessEventBus(store)
        mission_id = await _running_mission(store)

        await _daemon(store, bus, tmp_path)._log_runner_error(
            mission_id, Deadlock(sqlstate="23505")
        )

        mission = await store.get_mission(mission_id)
        assert mission is not None and mission.status == MissionStatus.FAILED
        result = mission.result or {}
        assert "crash" not in result, "a logical error is not counted as a crash"
        assert result["error"] == "Deadlock: deadlock detected on UPDATE chats"
        assert EventType.MISSION_FAILED in await _events(store, mission_id)
        assert await _crash_count(store, mission_id) == 0
    finally:
        await store.aclose()


async def test_a_finished_mission_is_never_resurrected(tmp_path: Path) -> None:
    """A late crash from a runner whose mission already reached DONE must not
    put it back on the queue or rewrite its result.

    The row is what these assertions pin: the handler's terminal-status guard
    skips the whole requeue branch, so no PENDING flip, no INFO note, no crash
    counter. (The MISSION_FAILED publish sits OUTSIDE that guard in the
    daemon, so a terminal mission still gets that one event; that is
    pre-existing behaviour for every error type and not asserted here either
    way, so tightening it later does not break this test.)"""
    store = await open_sqlite(str(tmp_path / "s.sqlite3"))
    try:
        bus = InProcessEventBus(store)
        await store.create_mission(
            Mission(id="m1", objective="already done", result={"summary": "shipped"})
        )
        await store.update_mission("m1", status=MissionStatus.DONE)

        await _daemon(store, bus, tmp_path)._log_runner_error("m1", Deadlock())

        mission = await store.get_mission("m1")
        assert mission is not None
        assert mission.status == MissionStatus.DONE, "a finished mission is not resurrected"
        assert mission.result == {"summary": "shipped"}, "and its result is not rewritten"
        assert await _crash_count(store, "m1") == 0, "nor does it spend a crash budget"
        assert await store.next_pending_missions() == []
        events = await store.events_since(0, mission_id="m1", limit=200)
        assert not [e for e in events if e.type == EventType.INFO], "no requeue note"
    finally:
        await store.aclose()


async def test_a_terminal_failure_gives_a_later_run_a_full_budget(tmp_path: Path) -> None:
    store = await open_sqlite(str(tmp_path / "s.sqlite3"))
    try:
        bus = InProcessEventBus(store)
        mission_id = await _running_mission(store)
        daemon = _daemon(store, bus, tmp_path)
        import time as _time

        from rework.orchestrator.daemon import TRANSIENT_OUTAGE_BUDGET_S

        for _ in range(MIN_TRANSIENT_CRASHES + 1):
            await daemon._log_runner_error(mission_id, Deadlock())
            await store.update_mission(mission_id, status=MissionStatus.RUNNING)

        # spend the outage budget, then crash: that terminal failure clears the clock
        mission = await store.get_mission(mission_id)
        assert mission is not None
        spent = {**(mission.result or {})}
        spent["first_transient_crash_at"] = _time.time() - TRANSIENT_OUTAGE_BUDGET_S - 1
        await store.update_mission(mission_id, result=spent, status=MissionStatus.RUNNING)
        await daemon._log_runner_error(mission_id, Deadlock())
        failed = await store.get_mission(mission_id)
        assert failed is not None and failed.status == MissionStatus.FAILED

        assert await _crash_count(store, mission_id) == 0
        # and the next run's first crash is crash 1 again, with a fresh clock
        await store.update_mission(mission_id, status=MissionStatus.RUNNING)
        await daemon._log_runner_error(mission_id, Deadlock())
        mission = await store.get_mission(mission_id)
        assert mission is not None and mission.status == MissionStatus.PENDING
        assert "crash 1" in (mission.result or {})["crash"]
    finally:
        await store.aclose()
