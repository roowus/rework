"""The daemon: one process per database, leader-locked, one runner per mission.

Boot order matters (ADR 0002 gotcha 7): recovery consults the store BEFORE any
new work: missions stuck in RUNNING/AWAITING_GATE get fresh MissionRunners (which
resume role graphs from checkpoints and honor gates decided while we were down),
then PENDING missions start normally.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from rework import update
from rework.config import Config, max_roles
from rework.kernel.events.bus import EventBus
from rework.kernel.llm.catalog import ModelCatalog
from rework.kernel.state.records import EventRecord, EventType, MissionStatus
from rework.kernel.state.store import StateStore
from rework.orchestrator.engine import CheckpointerFactory, MissionRunner
from rework.orchestrator.limits import MissionSlot, RoleLimiter
from rework.roles.registry import RoleRegistry
from rework.runtime import CODE_CHANGED, CodeChangeWatcher, find_repo_root

log = logging.getLogger("rework.daemon")


class DaemonLocked(Exception):
    pass


def preview_dispatch_allowed() -> bool:
    """A preview daemon (ADR 0008) does not dispatch role runs unless its owner
    opted in with --allow-missions (REWORK_PREVIEW_ALLOW_MISSIONS=1)."""
    if not os.environ.get("REWORK_PREVIEW"):
        return True
    return bool(os.environ.get("REWORK_PREVIEW_ALLOW_MISSIONS"))


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
        auto_update: bool = False,
    ) -> None:
        self._store = store
        self._bus = bus
        self._registry = registry
        self._catalog = catalog
        self._make_checkpointer = make_checkpointer
        self._workroot = workroot
        self._config = config
        self._library = library  # LibraryHandle | None (M5)
        # serve --auto-update / REWORK_AUTO_UPDATE: opt-in, default off
        self._auto_update = auto_update
        self._repo_root: Path | None = None
        self._last_update_check = 0.0
        self._sem = asyncio.Semaphore(config.concurrency)
        # daemon-wide cap on concurrently RUNNING roles, shared by every
        # mission's RunSupervisor (REWORK_MAX_ROLES, orchestrator/limits.py)
        self._limiter = RoleLimiter(max_roles(config), poll_interval_s=config.poll_interval_s)
        self._runners: dict[str, asyncio.Task[None]] = {}
        self._bg: set[asyncio.Task[None]] = set()
        self._owner = f"daemon-{uuid.uuid4().hex[:8]}"
        self._code: CodeChangeWatcher | None = None

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
            self._code = CodeChangeWatcher()
            # resolved once: the checkout this daemon runs from (may be None
            # for a pip install with no git tree, which disables auto-update)
            self._repo_root = find_repo_root()
            self._last_update_check = time.monotonic()
            while True:
                await self._store.heartbeat_lock(self._owner)
                await self._pickup_pending()
                self._reap()
                self._check_code()
                await self._maybe_auto_update()
                await asyncio.sleep(self._config.poll_interval_s)
        finally:
            await self._store.release_lock(self._owner)

    def _check_code(self) -> None:
        """The checkout this daemon was started from changed (a merge landed in
        the same tree): the modules in memory are the OLD ones. Say so once and
        keep running: the running missions must not be dropped, and restarting
        is the owner's call (docs/plans/m7-findings.md, finding 28)."""
        if self._code is not None and self._code.poll():
            log.warning("%s", CODE_CHANGED)

    async def _maybe_auto_update(self) -> None:
        """Opt-in auto-update (`serve --auto-update`, `REWORK_AUTO_UPDATE=1`).

        Every `update.AUTO_UPDATE_SECONDS`: if origin/main is ahead AND no
        mission is in flight, pull it in, `uv sync`, and exit so systemd's
        `Restart=always` brings the daemon back on the new code. A mission
        mid-run is never dropped for a deploy, offline is silence, and a
        refusal is a log line, never a crash (docs/plans/m7-findings.md 28)."""
        if not self._auto_update or self._repo_root is None:
            return
        now = time.monotonic()
        if now - self._last_update_check < update.AUTO_UPDATE_SECONDS:
            return
        self._last_update_check = now
        check = await asyncio.to_thread(update.check_remote, self._repo_root)
        if check is None or not check.available:
            return  # unknown (offline) or up to date: nothing to say
        busy = await update.mission_busy(self._store)
        if not update.should_auto_update(ahead=check.available, busy=busy):
            log.debug("auto-update deferred: a mission is in flight")
            return
        result = await asyncio.to_thread(update.run_update, self._repo_root)
        if not result.ok:
            log.warning("auto-update refused: %s", result.reason)
            return
        if not result.changed:
            return
        log.warning("rework updated: %s -> %s, exiting for restart", result.old, result.new)
        raise SystemExit(0)

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
        # pickup order (NOT the browse order of list_missions): high priority
        # first, oldest first within a level, so an old pending mission cannot
        # starve behind newer ones and a high-priority mission jumps the queue
        for mission in await self._store.next_pending_missions(limit=100):
            runner = self._runners.get(mission.id)
            if runner is not None and not runner.done():
                continue  # queued behind the concurrency limit; logged once
            log.info("picking up pending mission %s", mission.id)
            await self._spawn_runner(mission.id)

    async def _spawn_runner(self, mission_id: str) -> None:
        if not preview_dispatch_allowed():
            log.info(
                "preview mode: mission dispatch disabled "
                "(set REWORK_PREVIEW_ALLOW_MISSIONS=1 or use --allow-missions)"
            )
            return
        if mission_id in self._runners and not self._runners[mission_id].done():
            return

        async def guarded() -> None:
            # the mission slot is taken for WORK only. The runner hands it back
            # while it sits at a gate / waits for the owner / is on hold
            # (MissionSlot.while_waiting), so a waiting mission never blocks an
            # approved gate from proceeding (docs/ops.md#rewvis). Built here (not
            # in _spawn_runner) so the coroutine owns its own event loop's state.
            slot = MissionSlot(self._sem, mission_id)
            await slot.acquire()
            try:
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
                    slot=slot,
                    limiter=self._limiter,
                )
                await runner.run()
            finally:
                slot.release()

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
        # Delivery errors (push / gh) are kept WHOLE: git's useful line is the
        # LAST one, after the `remote:` banner and the forge's hints, so a
        # 2000-char prefix ends before the reason. Every other error stays
        # trimmed as before (a runaway traceback is not a mission report).
        from rework.nodes.gh import GHError
        from rework.nodes.git_ops import PushError

        keep_whole = isinstance(exc, (PushError, GHError))
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
                    result={
                        **(mission.result or {}),
                        "error": error if keep_whole else error[:2000],
                    },
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
