"""A transient database error inside a ROLE RUN must reach the daemon.

The hole (audit finding 5): `RunSupervisor._execute` swallowed every exception
out of a role graph into a FAILED run row, and `MissionRunner._synthesize` then
failed the MISSION from the failed run list. So a `TransientDatabaseError` (or a
raw driver deadlock) raised inside a role run never reached
`Daemon._log_runner_error`, no crash count was spent and nothing was requeued.

The fix: a transient error is re-raised out of `_execute` / `_execute_resume`,
`_pump_until_terminal` surfaces it, and the runner dies with it, which is what
the daemon's `_reap` -> `_log_runner_error` path consumes.

Red pre-fix: `runner.run()` returned normally (the mission ends FAILED on a
failed run row), so `pytest.raises(Deadlock)` failed and no crash was recorded.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from rework.kernel.events.bus import InProcessEventBus
from rework.kernel.llm.catalog import ModelCatalog, ModelEntry
from rework.kernel.state.records import EventType, Mission, MissionStatus, new_id
from rework.kernel.state.sqlite import SqliteStore, open_sqlite
from rework.orchestrator.daemon import Daemon, sqlite_checkpointer_factory
from rework.orchestrator.engine import MissionRunner
from rework.roles.registry import autodiscover


class Deadlock(Exception):
    """Duck-typed psycopg deadlock: the classifier only reads `sqlstate`."""

    def __init__(self) -> None:
        super().__init__("deadlock detected on UPDATE runs")
        self.sqlstate = "40P01"


async def test_a_transient_error_in_a_role_run_is_requeued_not_failed(tmp_path: Path) -> None:
    db_path = str(tmp_path / "state.sqlite3")
    ck_path = str(tmp_path / "checkpoints.sqlite3")
    store: SqliteStore = await open_sqlite(db_path)
    try:
        bus = InProcessEventBus(store)
        mission = await store.create_mission(Mission(id=new_id("m"), objective="survive"))
        runner = MissionRunner(
            mission.id,
            store,
            bus,
            autodiscover(),
            ModelCatalog([ModelEntry(id="fake", provider="fake", model="fake-1")]),
            sqlite_checkpointer_factory(ck_path),
            tmp_path / "work",
            poll_interval_s=0.05,
        )

        # The retry seam is bypassed on purpose: this is the raw driver error
        # escaping a role run, the exact shape that used to become a FAILED run.
        real_update_run = store.update_run
        raised: list[bool] = []

        async def flaky_update_run(run_id: str, **kwargs: Any) -> Any:
            status = kwargs.get("status")
            if not raised and getattr(status, "value", None) == "running":
                raised.append(True)
                raise Deadlock()
            return await real_update_run(run_id, **kwargs)

        store.update_run = flaky_update_run  # type: ignore[method-assign]

        with pytest.raises(Deadlock) as caught:
            await asyncio.wait_for(runner.run(), timeout=20)

        # the daemon's crash handler now decides, and it requeues
        daemon = Daemon(
            store,
            bus,
            None,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            tmp_path,
            SimpleNamespace(concurrency=1, max_roles=1, poll_interval_s=0.05, git_author=None),  # type: ignore[arg-type]
        )
        await daemon._log_runner_error(mission.id, caught.value)

        mission_after = await store.get_mission(mission.id)
        assert mission_after is not None
        assert mission_after.status == MissionStatus.PENDING, (
            "a transient database error must requeue the mission, never fail it"
        )
        result = mission_after.result or {}
        assert "40P01" in result["transient_db_error"]
        # the count is now a diagnostic, not the bound: the message names the
        # crash number and how far into the outage it happened
        assert "crash 1" in result["crash"]
        assert "into the outage" in result["crash"]
        assert isinstance(result["first_transient_crash_at"], float), (
            "the outage clock is stamped on the first crash"
        )
        events = await store.events_since(0, mission_id=mission.id, limit=200)
        assert EventType.MISSION_FAILED not in [e.type for e in events]
        runs = await store.list_runs(mission.id)
        assert [r.status.value for r in runs] != ["failed"], (
            "the transient error must not be recorded as a failed run"
        )
        assert await store.next_pending_missions(limit=10) != [], "the queue claims it again"
    finally:
        await store.aclose()


class _Deadlock(Exception):
    sqlstate = "40P01"


def _daemon(store: SqliteStore, bus: Any, tmp_path: Path) -> Any:
    from rework.orchestrator.daemon import Daemon

    return Daemon(
        store,
        bus,
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        tmp_path,
        SimpleNamespace(concurrency=1, max_roles=1, poll_interval_s=0.05, git_author=None),  # type: ignore[arg-type]
    )


async def test_the_outage_budget_is_time_not_a_crash_count(tmp_path: Path) -> None:
    """A long outage must not end a mission for running out of TRIES.

    The old rule was three crashes. One bad minute on the link can burn several
    crashes while the mission's work is safe on its checkpoint, and that mission
    was failed for arithmetic. The bound is now the elapsed outage: many crashes
    inside the budget keep requeueing.
    """
    import time as _time

    from rework.orchestrator import daemon as daemon_mod

    db_path = str(tmp_path / "state.sqlite3")
    store: SqliteStore = await open_sqlite(db_path)
    try:
        bus = InProcessEventBus(store)
        mission = await store.create_mission(Mission(id=new_id("m"), objective="survive"))
        d = _daemon(store, bus, tmp_path)

        # ten crashes inside the budget: the mission stays alive
        for _ in range(10):
            await d._log_runner_error(mission.id, _Deadlock("deadlock detected"))
            now = await store.get_mission(mission.id)
            assert now is not None and now.status == MissionStatus.PENDING, (
                "a mission inside the outage budget is requeued, never failed"
            )
        state = await store.get_mission(mission.id)
        assert state is not None
        assert state.result and "first_transient_crash_at" in state.result
        # the count still increments (it is the useful diagnostic), read it back
        # through the store API rather than the record, which does not carry it
        assert await store.increment_transient_crash_count(mission.id) >= 11

        # the SAME mission, now stamp it as an outage that began long ago: the
        # budget is spent, so the next crash ends it with the reason visible
        stamp = _time.time() - daemon_mod.TRANSIENT_OUTAGE_BUDGET_S - 60
        await store.update_mission(
            mission.id, result={**(state.result or {}), "first_transient_crash_at": stamp}
        )
        await d._log_runner_error(mission.id, _Deadlock("deadlock detected"))
        gone = await store.get_mission(mission.id)
        assert gone is not None and gone.status == MissionStatus.FAILED
        assert "budget" in str((gone.result or {}).get("error", "")), (
            "the failure names the elapsed outage, not a try count"
        )
    finally:
        await store.aclose()
