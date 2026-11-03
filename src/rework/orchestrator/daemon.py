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
        await saver.setup()

        async def close() -> None:
            await cm.__aexit__(None, None, None)

        return saver, close

    return make


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
    ) -> None:
        self._store = store
        self._bus = bus
        self._registry = registry
        self._catalog = catalog
        self._make_checkpointer = make_checkpointer
        self._workroot = workroot
        self._config = config
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
        for status in (MissionStatus.RUNNING, MissionStatus.AWAITING_GATE):
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
        await self._bus.publish(
            EventRecord(
                mission_id=mission_id,
                type=EventType.MISSION_FAILED,
                payload={"error": f"{type(exc).__name__}: {exc}"},
            )
        )
