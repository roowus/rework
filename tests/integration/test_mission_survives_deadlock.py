"""The mission's stated success criterion: a database deadlock never ends it.

2026-11-14, live: `DeadlockDetected` (SQLSTATE 40P01) on a chat write killed a
nearly finished mission. Two independent layers now stand between that error and
a lost mission, so both are proven here against a real store and a real runner:

1. the store's retry seam absorbs a ONE-off deadlock, so the mission reaches
   DONE without anybody noticing;
2. a deadlock that outlives the seam (the store gave up after its attempts)
   still does not fail the mission: the daemon requeues it from its checkpoint.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from rework.config import Config
from rework.kernel.events.bus import InProcessEventBus
from rework.kernel.gates import GateManager
from rework.kernel.llm.catalog import ModelCatalog, ModelEntry
from rework.kernel.state.records import EventType, Mission, MissionStatus, new_id
from rework.kernel.state.retry import TransientDatabaseError
from rework.kernel.state.sqlite import SqliteStore, open_sqlite
from rework.orchestrator.daemon import Daemon, sqlite_checkpointer_factory
from rework.orchestrator.engine import MissionRunner
from rework.roles.registry import autodiscover


class InjectedDeadlock(Exception):
    """A deadlock as the driver reports it: the only attribute that matters is
    the SQLSTATE."""

    def __init__(self, sql: str) -> None:
        super().__init__(f"deadlock detected while running: {sql}")
        self.sqlstate = "40P01"


class _FailingExecute:
    """What aiosqlite's `execute()` returns, for a call that fails: both
    awaitable (`await conn.execute(...)`) and an async context manager
    (`async with conn.execute(...)`), so a proxy can stand in for it."""

    def __init__(self, sql: str) -> None:
        self._sql = sql

    def __await__(self) -> Any:
        return self._raise().__await__()

    async def _raise(self) -> Any:
        raise InjectedDeadlock(self._sql)

    async def __aenter__(self) -> Any:
        await self._raise()

    async def __aexit__(self, *exc: object) -> bool:
        return False


class FlakyConnection:
    """The store's own connection, with its mission writes breaking on cue.

    A proxy around `store._db` is the honest injection point: the error is
    raised from INSIDE a public store method, so the shared retry seam sees it
    exactly as it sees a driver error.
    """

    def __init__(
        self,
        real: Any,
        failures: int,
        sql_prefix: str = "UPDATE missions",
        *,
        at_commit: bool = False,
        write_prefix: str = "insert into events",
    ) -> None:
        self._real = real
        self._remaining = failures  # -1 keeps failing forever
        self._prefix = sql_prefix.lower()
        self.attempts = 0
        # at_commit injects the abort where sqlite really reports it: AFTER the
        # statement ran, so a seam that replays the method body without undoing
        # the attempt stores the write twice.
        self._at_commit = at_commit
        # which pending write the failing commit must swallow: an INSERT is the
        # non-idempotent case (a replayed UPDATE leaves the same rows behind)
        self._write_prefix = write_prefix.lower()
        self._writes: list[str] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    def execute(self, sql: str, parameters: Any = None) -> Any:
        if not self._at_commit and self._remaining != 0 and self._matches(sql):
            return self._fail(sql)
        if sql.strip().lower().startswith(("insert", "update", "delete")):
            # a commit here would make that statement durable
            self._writes.append(sql)
        return self._real.execute(sql, parameters)

    async def commit(self) -> None:
        if self._at_commit and self._remaining != 0 and self._pending_write():
            self.attempts += 1
            if self._remaining > 0:
                self._remaining -= 1
            self._writes.clear()
            raise InjectedDeadlock("COMMIT")
        self._writes.clear()
        await self._real.commit()

    def _pending_write(self) -> bool:
        """True when the pending write is the one a failed commit would leave
        behind: the commit raises after that statement ran, so a seam that
        replays the method body stores it twice."""
        return bool(self._writes) and self._writes[-1].strip().lower().startswith(
            self._write_prefix
        )

    def _matches(self, sql: str) -> bool:
        return sql.strip().lower().startswith(self._prefix)

    def _fail(self, sql: str) -> Any:
        self.attempts += 1
        if self._remaining > 0:
            self._remaining -= 1
        return _FailingExecute(sql)


async def _wait_for(
    predicate: Callable[[], Awaitable[bool]], timeout_s: float = 15.0, what: str = "condition"
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


def make_daemon(world: dict, tmp_path: Path) -> Daemon:
    cfg = SimpleNamespace(concurrency=1, max_roles=1, poll_interval_s=0.05, git_author=None)
    return Daemon(
        world["store"],
        world["bus"],
        world["registry"],
        world["catalog"],
        sqlite_checkpointer_factory(world["ck_path"]),
        world["workroot"],
        cfg,  # type: ignore[arg-type]
    )


async def _approve_every_gate(store: SqliteStore, mission_id: str, task: asyncio.Task) -> None:
    """Approve whatever the echo role asks, until the runner finishes."""
    gates = GateManager(store, InProcessEventBus(store))
    while not task.done():
        for gate in await store.list_open_gates(mission_id):
            await gates.decide(gate.id, approved=True)
        await asyncio.sleep(0.05)


async def test_a_single_deadlock_is_absorbed_and_the_mission_reaches_done(
    world: dict, tmp_path: Path
) -> None:
    """The store's retry seam retries the aborted write, so the runner never
    even sees the error and the mission completes normally."""
    store: SqliteStore = world["store"]
    mission = await store.create_mission(Mission(id=new_id("m"), objective="say hello"))
    flaky = FlakyConnection(store._db, failures=1)
    store._db = flaky  # type: ignore[assignment]

    runner = make_runner(world, mission.id)
    task = asyncio.create_task(runner.run())
    await _approve_every_gate(store, mission.id, task)
    await asyncio.wait_for(task, timeout=20)

    assert flaky.attempts >= 1, "the deadlock was never injected: this test proves nothing"
    done = await store.get_mission(mission.id)
    assert done is not None and done.status == MissionStatus.DONE
    runs = await store.list_runs(mission.id)
    assert len(runs) == 1 and runs[0].status.value == "completed"
    types = [e.type for e in await store.events_since(0, mission_id=mission.id)]
    assert EventType.MISSION_DONE in types
    assert EventType.MISSION_FAILED not in types


async def test_a_deadlock_at_commit_does_not_duplicate_the_mission_row(
    world: dict, tmp_path: Path
) -> None:
    """The incident's shape: sqlite reports the abort at COMMIT, after the DML
    of that call already ran. The seam must replay from clean state, so the
    retried attempt stores the mission's work exactly once.

    The proof is a baseline: the same mission runs once with no injection and
    once with the first commit aborted, and both must leave the SAME event log.
    A seam that replays the aborted attempt's writes adds an event (and, before
    the fix, did), which is what the comparison catches."""
    store: SqliteStore = world["store"]

    baseline_mission = await store.create_mission(Mission(id=new_id("m"), objective="say hello"))
    baseline_task = asyncio.create_task(make_runner(world, baseline_mission.id).run())
    await _approve_every_gate(store, baseline_mission.id, baseline_task)
    await asyncio.wait_for(baseline_task, timeout=20)
    # the event TYPE sequence: payloads carry freshly generated ids per mission,
    # while a replayed attempt shows up as an extra event of the same type
    baseline = [e.type for e in await store.events_since(0, mission_id=baseline_mission.id)]
    assert EventType.MISSION_DONE in baseline

    mission = await store.create_mission(Mission(id=new_id("m"), objective="say hello"))
    flaky = FlakyConnection(store._db, failures=1, at_commit=True)
    store._db = flaky  # type: ignore[assignment]

    runner = make_runner(world, mission.id)
    task = asyncio.create_task(runner.run())
    await _approve_every_gate(store, mission.id, task)
    await asyncio.wait_for(task, timeout=20)

    assert flaky.attempts >= 1, "the abort was never injected: this test proves nothing"
    done = await store.get_mission(mission.id)
    assert done is not None and done.status == MissionStatus.DONE
    runs = await store.list_runs(mission.id)
    assert len(runs) == 1, "the retried attempt stored the run twice"
    assert runs[0].status.value == "completed"
    events = await store.events_since(0, mission_id=mission.id)
    assert [e.type for e in events] == baseline, (
        "the aborted attempt's work survived the retry: the event log has duplicates"
    )
    assert EventType.MISSION_FAILED not in [e.type for e in events]


async def test_a_deadlock_past_the_seam_requeues_instead_of_failing(
    world: dict, tmp_path: Path
) -> None:
    """A database that deadlocks every attempt: the store exhausts its retry and
    raises TransientDatabaseError out of the runner. The daemon must requeue the
    mission, not throw its work away."""
    store: SqliteStore = world["store"]
    mission = await store.create_mission(
        Mission(id=new_id("m"), objective="survive", result={"summary": "in progress"})
    )
    flaky = FlakyConnection(store._db, failures=-1)
    store._db = flaky  # type: ignore[assignment]

    runner = make_runner(world, mission.id)
    with pytest.raises(TransientDatabaseError) as caught:
        await asyncio.wait_for(runner.run(), timeout=20)
    assert flaky.attempts == 3, "one attempt plus the store's two retries"
    assert caught.value.attempts == 3

    # the database recovers: the requeue itself must land
    flaky._remaining = 0
    await make_daemon(world, tmp_path)._log_runner_error(mission.id, caught.value)

    after = await store.get_mission(mission.id)
    assert after is not None
    assert after.status == MissionStatus.PENDING, "a deadlock is not the mission's fault"
    assert (after.result or {})["summary"] == "in progress"
    assert "40P01" in (after.result or {})["transient_db_error"]
    assert "crash 1 of 3" in (after.result or {})["crash"]
    events = await store.events_since(0, mission_id=mission.id, limit=200)
    assert EventType.MISSION_FAILED not in [e.type for e in events]
    assert any(
        e.type == EventType.INFO and "resume from its checkpoint" in str(e.payload) for e in events
    ), [e.payload for e in events]
    assert [m.id for m in await store.next_pending_missions()] == [mission.id]
