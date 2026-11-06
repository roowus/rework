"""The daemon: one process per database, leader-locked, one runner per mission.

Boot order matters (ADR 0002 gotcha 7): recovery consults the store BEFORE any
new work: missions stuck in RUNNING/AWAITING_GATE get fresh MissionRunners (which
resume role graphs from checkpoints and honor gates decided while we were down),
then PENDING missions start normally.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from rework.config import Config
from rework.kernel.events.bus import EventBus
from rework.kernel.llm.catalog import ModelCatalog
from rework.kernel.state.records import EventRecord, EventType, MissionStatus
from rework.kernel.state.store import StateStore
from rework.orchestrator.engine import CheckpointerFactory, MissionRunner
from rework.roles.registry import RoleRegistry

log = logging.getLogger("rework.daemon")


class DaemonLocked(Exception):
    pass


def sqlite_checkpointer_factory(checkpoints_db: str) -> CheckpointerFactory:
    """One checkpointer connection per run, on the shared checkpoints file."""

    async def make(run_id: str) -> tuple[Any, Callable[[], Awaitable[None]]]:
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        cm = AsyncSqliteSaver.from_conn_string(checkpoints_db)
        saver = await cm.__aenter__()
        # the checkpointer holds its own connection to the (shared) file;
        # teach it the same wait-your-turn rule the store uses BEFORE setup()
        # (setup() itself writes DDL and can hit a lock on a busy file)
        conn = getattr(saver, "conn", None)
        if conn is not None:
            try:
                await conn.execute("PRAGMA busy_timeout = 10000")
            except Exception:  # pragma: no cover - shape varies by version
                pass
        await saver.setup()

        async def close() -> None:
            await cm.__aexit__(None, None, None)

        return saver, close

    return make


def postgres_checkpointer_factory(db_url: str) -> CheckpointerFactory:
    """One checkpointer connection per run, against Postgres (the M4
    deployment-grade path: checkpoints share the state database, so a daemon
    restart resumes threads with no local files). setup() runs the saver's
    migrations once per connection (idempotent DDL)."""

    async def make(run_id: str) -> tuple[Any, Callable[[], Awaitable[None]]]:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        cm = AsyncPostgresSaver.from_conn_string(db_url)
        saver = await cm.__aenter__()
        await saver.setup()

        async def close() -> None:
            await cm.__aexit__(None, None, None)

        return saver, close

    return make


def checkpointer_factory_for(config: Config) -> CheckpointerFactory:
    """Pick the checkpointer backend from the state config: Postgres state
    means Postgres checkpoints (one substrate), else the sqlite file."""
    if config.is_postgres:
        return postgres_checkpointer_factory(config.checkpoints_db)
    return sqlite_checkpointer_factory(config.checkpoints_db)


class Daemon:
    def __init__(
        self,
        store: StateStore,
        bus: EventBus,
        registry: RoleRegistry,
        catalog: ModelCatalog,
        make_checkpointer: CheckpointerFactory,
        workroot: Path,
        config: Config,
        library: Any | None = None,
    ) -> None:
        self._store = store
        self._bus = bus
        self._registry = registry
        self._catalog = catalog
        self._make_checkpointer = make_checkpointer
        self._workroot = workroot
        self._config = config
        self._library = library  # LibraryHandle | None (M5)
        self._sem = asyncio.Semaphore(config.concurrency)
        self._runners: dict[str, asyncio.Task[None]] = {}
        self._bg: set[asyncio.Task[None]] = set()
        self._owner = f"daemon-{uuid.uuid4().hex[:8]}"

    async def run_forever(self) -> None:
        # staleness must be a small multiple of the heartbeat period, or a
        # SIGKILLed daemon (no cleanup, fresh heartbeat) blocks restarts
        stale_after = max(self._config.poll_interval_s * 10, 5.0)
        if not await self._store.acquire_lock(self._owner, stale_after_s=stale_after):
            raise DaemonLocked(
                f"another daemon holds this database's lock "
                f"(heartbeat fresh within {stale_after:.0f}s); refusing to double-run"
            )
        try:
            await self._recover()
            while True:
                await self._store.heartbeat_lock(self._owner)
                await self._pickup_pending()
                self._reap()
                await asyncio.sleep(self._config.poll_interval_s)
        finally:
            await self._store.release_lock(self._owner)

    async def _recover(self) -> None:
        for status in (
            MissionStatus.RUNNING,
            MissionStatus.AWAITING_GATE,
            # a paused mission is not abandoned: its runner must come back and
            # sit in the hold loop, or a resume from another machine would be
            # ignored until someone restarted the daemon twice
            MissionStatus.ON_HOLD,
        ):
            for mission in await self._store.list_missions(status=status, limit=100):
                log.info("recovering mission %s (%s)", mission.id, status.value)
                await self._spawn_runner(mission.id)

    async def _pickup_pending(self) -> None:
        for mission in await self._store.list_missions(status=MissionStatus.PENDING, limit=10):
            log.info("picking up pending mission %s", mission.id)
            await self._spawn_runner(mission.id)

    async def _spawn_runner(self, mission_id: str) -> None:
        if mission_id in self._runners and not self._runners[mission_id].done():
            return

        async def guarded() -> None:
            async with self._sem:
                runner = MissionRunner(
                    mission_id,
                    self._store,
                    self._bus,
                    self._registry,
                    self._catalog,
                    self._make_checkpointer,
                    self._workroot,
                    poll_interval_s=self._config.poll_interval_s,
                    git_author=self._config.git_author,
                    library=self._library,
                )
                await runner.run()

        self._runners[mission_id] = asyncio.create_task(guarded())

    def _reap(self) -> None:
        for mid in [m for m, t in self._runners.items() if t.done()]:
            task = self._runners.pop(mid)
            if task.cancelled():
                continue
            exc = task.exception()
            if exc is not None:
                # log into the event stream; the daemon itself keeps running
                bg = asyncio.create_task(self._log_runner_error(mid, exc))
                self._bg.add(bg)
                bg.add_done_callback(self._bg.discard)

    async def _log_runner_error(self, mission_id: str, exc: BaseException) -> None:
        """A runner that died with an unhandled exception leaves nobody driving
        its mission. Say so on the ROW, not only in the event log: found live,
        a mission whose runner crashed sat on 'running' forever in every UI."""
        log.error("runner for %s crashed: %s: %s", mission_id, type(exc).__name__, exc)
        error = f"{type(exc).__name__}: {exc}"
        try:
            mission = await self._store.get_mission(mission_id)
            if mission is not None and mission.status not in (
                MissionStatus.DONE,
                MissionStatus.FAILED,
                MissionStatus.CANCELLED,
            ):
                await self._store.update_mission(
                    mission_id,
                    status=MissionStatus.FAILED,
                    result={**(mission.result or {}), "error": error[:2000]},
                )
        except Exception:
            log.exception("could not mark %s failed", mission_id)
        await self._bus.publish(
            EventRecord(
                mission_id=mission_id,
                type=EventType.MISSION_FAILED,
                payload={"error": f"{type(exc).__name__}: {exc}"},
            )
        )
