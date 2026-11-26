"""The leader lock is fenced (audit 2026-11-25, finding 2.3).

The daemon used to heartbeat from inside its dispatch loop and ignore the
result, so (a) a slow iteration let a second daemon take over and (b) the first
daemon kept dispatching anyway: two daemons driving the same missions. Now the
heartbeat has its own task, and a lost lock stops the daemon's runners and ends
`run_forever` with LeaderLost.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from rework.config import Config
from rework.kernel.events.bus import InProcessEventBus
from rework.kernel.state.sqlite import open_sqlite
from rework.orchestrator.daemon import Daemon, DaemonLocked, LeaderLost, sqlite_checkpointer_factory


async def _daemon(tmp_path: Any, store: Any, stale_s: float = 0.6) -> Daemon:
    db = tmp_path / "state.sqlite3"
    ck = tmp_path / "checkpoints.sqlite3"
    return Daemon(
        store=store,
        bus=InProcessEventBus(store),
        registry=None,  # type: ignore[arg-type]
        catalog=None,  # type: ignore[arg-type]
        make_checkpointer=sqlite_checkpointer_factory(str(ck)),
        workroot=tmp_path / "work",
        config=Config(
            db=str(db), checkpoints_db=str(ck), poll_interval_s=0.02, lock_stale_s=stale_s
        ),
    )


async def _owner(store: Any) -> str | None:
    cur = await store._db.execute("SELECT owner FROM daemon_lock WHERE id = 1")
    row = await cur.fetchone()
    return row["owner"] if row else None


async def _until(predicate: Any, within_s: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + within_s
    while asyncio.get_running_loop().time() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.02)
    raise TimeoutError


async def test_taken_over_daemon_stops_its_runners_and_exits(tmp_path: Any) -> None:
    store = await open_sqlite(str(tmp_path / "state.sqlite3"))
    try:
        daemon = await _daemon(tmp_path, store)
        # a stand-in runner: must be cancelled when the lock is lost
        cancelled = asyncio.Event()

        async def runner() -> None:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        task = asyncio.create_task(daemon.run_forever())
        await _until(lambda: _owner_is(store, daemon._owner))
        daemon._runners["m-x"] = asyncio.create_task(runner())

        # another daemon steals the row (what a takeover after a stale beat does)
        await store._db.execute("UPDATE daemon_lock SET owner = 'intruder' WHERE id = 1")
        await store._db.commit()

        with pytest.raises(LeaderLost):
            await asyncio.wait_for(task, timeout=5)
        assert cancelled.is_set(), "a daemon that lost the lock must stop its runners"
        # it must not release the NEW owner's lock on the way out
        assert await _owner(store) == "intruder"
        assert issubclass(LeaderLost, DaemonLocked)  # `serve` reports it the same way
    finally:
        await store.aclose()


async def _owner_is(store: Any, owner: str) -> bool:
    return await _owner(store) == owner


async def test_heartbeat_is_independent_of_a_slow_dispatch_loop(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dispatch iteration slower than the stale window used to let a second
    daemon in. The beat now runs on its own task, so it stays fresh."""
    store = await open_sqlite(str(tmp_path / "state.sqlite3"))
    try:
        daemon = await _daemon(tmp_path, store, stale_s=0.6)

        async def slow_pickup() -> None:
            await asyncio.sleep(2.0)  # > 3x the stale window

        monkeypatch.setattr(daemon, "_pickup_pending", slow_pickup)
        task = asyncio.create_task(daemon.run_forever())
        await _until(lambda: _owner_is(store, daemon._owner))
        await asyncio.sleep(1.5)
        # a contender is ANOTHER daemon: its own connection, like in production
        # (sharing the daemon's connection raced its in-flight heartbeat
        # transaction: "cannot start a transaction within a transaction" on CI)
        other = await open_sqlite(str(tmp_path / "state.sqlite3"))
        try:
            assert await other.acquire_lock("contender", stale_after_s=0.6) is False
        finally:
            await other.aclose()
        # stop it between store calls: let the slow pickup be the await it
        # is parked in, not a heartbeat write
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await store.aclose()


async def test_unreachable_store_counts_as_lost_after_the_window(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A heartbeat that keeps raising is not proof of loss, but past the stale
    window another daemon may legitimately own the lock: assume we lost it."""
    store = await open_sqlite(str(tmp_path / "state.sqlite3"))
    try:
        daemon = await _daemon(tmp_path, store, stale_s=0.6)
        task = asyncio.create_task(daemon.run_forever())
        await _until(lambda: _owner_is(store, daemon._owner))

        async def broken(owner: str) -> bool:
            raise ConnectionError("store unreachable")

        monkeypatch.setattr(store, "heartbeat_lock", broken)
        with pytest.raises(LeaderLost):
            await asyncio.wait_for(task, timeout=5)
    finally:
        await store.aclose()
