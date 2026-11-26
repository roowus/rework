"""The daemon: one process per database, leader-locked, one runner per mission.

Boot order matters (ADR 0002 gotcha 7): recovery consults the store BEFORE any
new work: missions stuck in RUNNING/AWAITING_GATE get fresh MissionRunners (which
resume role graphs from checkpoints and honor gates decided while we were down),
then PENDING missions start normally.
"""

from __future__ import annotations

import asyncio
import contextlib
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
from rework.kernel.state.retry import is_transient_db_error, transient_error_summary
from rework.kernel.state.store import StateStore
from rework.orchestrator.capacity import DynamicCapacity
from rework.orchestrator.engine import CheckpointerFactory, MissionRunner
from rework.orchestrator.limits import MissionSlot, PrioritySlots, RoleLimiter
from rework.roles.registry import RoleRegistry
from rework.runtime import CODE_CHANGED, CodeChangeWatcher, find_repo_root

log = logging.getLogger("rework.daemon")

# A transient database error is not the mission's fault: requeue it from its
# checkpoint instead of failing it. The bound stops an unbounded requeue loop
# against a database that stays unreachable. 2026-11-14: an unclassified
# DeadlockDetected failed a nearly finished mission and threw its work away.
# How long a mission may keep being requeued for a database outage before it is
# given up on. A COUNT was the old rule (3 crashes) and it answers the wrong
# question: one bad minute on the link burns several crashes and ends a mission
# whose work is already safe on disk, while a mission that hits a crash every ten
# minutes for an hour is a real problem worth reporting. So the bound is time: as
# long as the store has been unreachable for less than this, keep requeueing (the
# mission resumes from its checkpoint, nothing is lost). The count is still
# recorded and shown, because it is the useful diagnostic.
#
# Rewvis measurement (2026-11-17): 0 missions have ever recorded a transient crash,
# so this has never fired. It is a rule that should not end a mission on its own.
TRANSIENT_OUTAGE_BUDGET_S = 15 * 60.0
# A floor on attempts, so a very fast failing loop still gets a few tries even if
# every crash lands inside the first second.
MIN_TRANSIENT_CRASHES = 3

# setup() on the postgres saver runs its migrations: DDL that takes ACCESS
# EXCLUSIVE on the checkpointer tables of the SAME database the store writes to,
# so running it per run connection races store writes (the 2026-11-14 lock
# cycle). It is idempotent, so once per process and database is enough.
_checkpointer_setup_lock = asyncio.Lock()
_checkpointer_setup_done: set[str] = set()


# a crashed runner's mission is requeued only from a status it was doing (or
# about to do) work in. PENDING is included: a runner can die before its first
# PENDING -> RUNNING write. A paused mission stays paused, a finished one stays
# finished.
_REQUEUEABLE = frozenset(
    {MissionStatus.PENDING, MissionStatus.RUNNING, MissionStatus.AWAITING_GATE}
)


class DaemonLocked(Exception):
    pass


class LeaderLost(DaemonLocked):
    """This daemon no longer holds the leader lock: another daemon took it over
    (our heartbeat went stale) or the row was removed. Continuing would be a
    split brain, two daemons dispatching the same missions, so the daemon stops
    its runners and exits; the service manager restarts it and it waits its
    turn like any second daemon."""


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
    migrations at most once per process and database: that setup is DDL and must
    not race store writes, so it is not repeated per run connection."""

    async def make(run_id: str) -> tuple[Any, Callable[[], Awaitable[None]]]:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        cm = AsyncPostgresSaver.from_conn_string(db_url)
        saver = await cm.__aenter__()
        if db_url not in _checkpointer_setup_done:
            async with _checkpointer_setup_lock:
                if db_url not in _checkpointer_setup_done:
                    await saver.setup()
                    _checkpointer_setup_done.add(db_url)

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
        # how a role builds its executor: the local jail, or a per-run
        # container when REWORK_EXECUTOR asks for one (ADR 0014). Decided once
        # per process, here, so no role reads the environment.
        from rework.nodes.docker import make_executor_factory

        self._executor_factory = make_executor_factory()
        self._sem = PrioritySlots(config.concurrency)
        # daemon-wide cap on concurrently RUNNING roles, shared by every
        # mission's RunSupervisor (REWORK_MAX_ROLES, orchestrator/limits.py)
        self._limiter = RoleLimiter(max_roles(config), poll_interval_s=config.poll_interval_s)
        # both caps follow the host at runtime (REWORK_CAPACITY=auto, the
        # default); the watchdog is started from inside run_forever, because a
        # task needs a running loop and this runs outside one. A hand-set
        # REWORK_CONCURRENCY / REWORK_MAX_ROLES reaches it through
        # CapacitySettings.from_env() as a FLOOR (REWORK_CAPACITY_PINS=floor, the
        # default), so both numbers being set no longer disables the watchdog
        self._capacity = DynamicCapacity(self._sem, self._limiter)
        self._runners: dict[str, asyncio.Task[None]] = {}
        self._bg: set[asyncio.Task[None]] = set()
        self._owner = f"daemon-{uuid.uuid4().hex[:8]}"
        self._code: CodeChangeWatcher | None = None
        self._source_task: asyncio.Task[None] | None = None
        self._beat_task: asyncio.Task[None] | None = None
        self._lost = asyncio.Event()

    async def run_forever(self) -> None:
        # staleness must be a small multiple of the heartbeat period, or a
        # SIGKILLed daemon (no cleanup, fresh heartbeat) blocks restarts
        stale_after = self._config.lock_stale_s
        if not await self._store.acquire_lock(self._owner, stale_after_s=stale_after):
            raise DaemonLocked(
                f"another daemon holds this database's lock "
                f"(heartbeat fresh within {stale_after:.0f}s); refusing to double-run"
            )
        try:
            # BEFORE the first dispatch: a fresh daemon starts at the caps the
            # box can take right now, not at the value resolved at import time
            self._capacity.start()
            self._log_dropped_env()
            await self._recover()
            self._code = CodeChangeWatcher()
            # resolved once: the checkout this daemon runs from (may be None
            # for a pip install with no git tree, which disables auto-update)
            self._repo_root = find_repo_root()
            self._last_update_check = time.monotonic()
            # the issue-tracker poller runs on ITS OWN cadence, not the hot
            # dispatch loop: a slow tracker must never delay a mission (ADR 0016)
            self._source_task = asyncio.create_task(self._poll_sources())
            # the heartbeat has its OWN task: driven by the dispatch loop, one
            # slow iteration (pickup, an auto-update check, a store retry) was
            # enough for a second daemon to see a stale lock and take over
            self._lost = asyncio.Event()
            self._beat_task = asyncio.create_task(self._heartbeat(stale_after))
            while True:
                await self._pickup_pending()
                await self._repair_stale_gate_status()
                self._reap()
                self._check_code()
                await self._maybe_auto_update()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._lost.wait(), self._config.poll_interval_s)
                if self._lost.is_set():
                    await self._stop_runners()
                    raise LeaderLost(
                        f"daemon {self._owner} lost the leader lock "
                        f"(heartbeat older than {stale_after:.0f}s or taken over); "
                        "stopped its runners to avoid a split brain"
                    )
        finally:
            if self._beat_task is not None:
                self._beat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._beat_task
            # no leaked task on any exit path: CancelledError, a normal return
            # and `raise SystemExit(0)` from auto-update all come through here
            if self._source_task is not None:
                self._source_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._source_task
            await self._capacity.stop()
            await self._store.release_lock(self._owner)

    async def _heartbeat(self, stale_after: float) -> None:
        """Keep the leader lock fresh; set `_lost` the moment it is not ours.

        A heartbeat that returns False means the row names another owner (we
        were taken over) or is gone: lost at once. A heartbeat that RAISES
        (the store is unreachable) is not proof of loss, but once our last
        good beat is older than the stale window minus one period, another
        daemon may legitimately have taken over, so we must assume we did."""
        period = max(stale_after / 6, 0.05)
        last_ok = time.monotonic()
        while True:
            await asyncio.sleep(period)
            try:
                held = await self._store.heartbeat_lock(self._owner)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                silent = time.monotonic() - last_ok
                log.warning("leader heartbeat failed (%.1fs since last): %s", silent, exc)
                if silent >= stale_after - period:
                    log.error("leader lock may be lost: no heartbeat for %.1fs", silent)
                    self._lost.set()
                    return
                continue
            if not held:
                log.error("leader lock lost: %s no longer owns it", self._owner)
                self._lost.set()
                return
            last_ok = time.monotonic()

    async def _stop_runners(self) -> None:
        """Cancel every mission runner (lock lost). Their rows are left as
        they are: the store is the truth and the new leader recovers them."""
        tasks = [t for t in self._runners.values() if not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._runners.clear()

    def _log_dropped_env(self) -> None:
        """Say ONCE, by name and never by value, which variables the role
        environment drops (ADR 0009). The daemon's own systemd unit can carry
        variables the owner's shell cannot see, so this is the only way to know
        whether a role lost something it needs: the fix is
        `rework role-env allow NAME`, and the log entry names it."""
        import os

        from rework.nodes.tools import allowed_names

        kept = allowed_names(dict(os.environ))
        dropped = sorted(set(os.environ) - kept)
        if dropped:
            log.info(
                "role commands drop %d variable(s) (ADR 0009; grant one with "
                "`rework role-env allow NAME`): %s",
                len(dropped),
                ", ".join(dropped),
            )

    async def _poll_sources(self) -> None:
        """Turn ready work items from the tracker into missions, forever.

        Disabled unless configured (REWORK_PLANE_TOKEN and friends, ADR 0016).
        Each tick is bounded, so a hung tracker costs one tick, and a failure is
        a log line rather than a dead daemon."""
        from rework.sources import poller

        config = poller.plane_config()
        if not poller.enabled(config):
            log.info("no issue source configured (REWORK_PLANE_*): poller idle")
            return
        client = poller.build_client(config)
        log.info("polling %s every %.0fs for work items", config["url"], config["interval_s"])
        try:
            while True:
                try:
                    created = await asyncio.wait_for(
                        poller.poll_once(
                            self._store,
                            client,
                            state=config["state"],
                            label=config["label"],
                        ),
                        timeout=min(config["interval_s"], 60),
                    )
                    for mission_id in created:
                        await self._source_mission(mission_id)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # a tracker outage is a log line
                    log.warning("source poll failed: %s", exc)
                await asyncio.sleep(config["interval_s"])
        finally:
            await client.close()

    async def _source_mission(self, mission_id: str) -> None:
        """A source-created mission carries no plan (the bridge does not know
        which playbook fits), so give it the same default a bare `rework new`
        would get: an orchestrated fix. The normal pickup path then dispatches
        it."""
        mission = await self._store.get_mission(mission_id)
        if mission is None or mission.plan is not None:
            return
        from rework.cli import default_orchestrator_model
        from rework.missions import orchestrated_plan

        await self._store.update_mission(
            mission_id,
            plan=orchestrated_plan("fix", default_orchestrator_model(self._catalog)),
        )
        log.info("source mission %s queued for dispatch", mission_id)

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

    async def _repair_stale_gate_status(self) -> int:
        """Safety net for the "needs you, but the Gates tab is empty" bug: a
        mission whose status says AWAITING_GATE while none of its gates is open
        is not waiting for the owner. Say what is true (RUNNING) and leave one
        INFO event behind. Cheap: one list per poll, and only the (normally
        zero) stale rows cost a query each."""
        fixed = 0
        for mission in await self._store.list_missions(
            status=MissionStatus.AWAITING_GATE, limit=100
        ):
            if await self._store.list_open_gates(mission.id):
                continue
            # compare-and-set: an owner pause/cancel in between stands
            if (
                await self._store.transition_mission(
                    mission.id, MissionStatus.RUNNING, from_={MissionStatus.AWAITING_GATE}
                )
                is None
            ):
                continue
            await self._bus.publish(
                EventRecord(
                    mission_id=mission.id,
                    type=EventType.INFO,
                    payload={"note": "no open gate: awaiting_gate corrected to running"},
                )
            )
            log.warning("mission %s: awaiting_gate with no open gate -> running", mission.id)
            fixed += 1
        return fixed

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
                    executor_factory=self._executor_factory,
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
                if is_transient_db_error(exc):
                    # Requeue, do not fail: the transaction was rolled back
                    # server side, and the mission resumes from its checkpoint.
                    summary = transient_error_summary(exc)
                    count = await self._store.increment_transient_crash_count(mission_id)
                    # when this mission's first crash in the current run of them was
                    # seen; a successful run resets the count (engine.py), which
                    # clears this too
                    first = (mission.result or {}).get("first_transient_crash_at")
                    now = time.time()
                    if not isinstance(first, (int, float)):
                        first = now
                    elapsed = now - float(first)
                    within_budget = count <= MIN_TRANSIENT_CRASHES or (
                        elapsed < TRANSIENT_OUTAGE_BUDGET_S
                    )
                    if within_budget:
                        log.warning(
                            "%s: transient database error (%s), crash %d,"
                            " %.0fs into the outage (budget %.0fs);"
                            " requeueing the mission to resume from its checkpoint",
                            mission_id,
                            summary,
                            count,
                            elapsed,
                            TRANSIENT_OUTAGE_BUDGET_S,
                        )
                        await self._store.transition_mission(
                            mission_id,
                            MissionStatus.PENDING,
                            from_=_REQUEUEABLE,
                            result={
                                **(mission.result or {}),
                                "transient_db_error": summary,
                                "first_transient_crash_at": first,
                                "crash": f"transient database error, crash {count},"
                                f" {elapsed:.0f}s into the outage, resuming from its"
                                " checkpoint",
                            },
                        )
                        await self._bus.publish(
                            EventRecord(
                                mission_id=mission_id,
                                type=EventType.INFO,
                                payload={
                                    "note": f"transient database error ({summary}), crash"
                                    f" {count}, {elapsed:.0f}s into the outage: the mission"
                                    " is requeued and will resume from its checkpoint"
                                },
                            )
                        )
                        return
                    # bound reached: fail, but keep the reason visible
                    error = (
                        f"transient database error: the store has been unreachable for"
                        f" {elapsed:.0f}s across {count} crashes, past the"
                        f" {TRANSIENT_OUTAGE_BUDGET_S:.0f}s budget, giving up: {summary}"
                    )
                await self._store.transition_mission(
                    mission_id,
                    MissionStatus.FAILED,
                    result={
                        **(mission.result or {}),
                        "error": error if keep_whole else error[:2000],
                    },
                )
                # terminal: a later fresh run of this mission gets a full budget
                await self._store.reset_transient_crash_count(mission_id)
        except Exception:
            log.exception("could not mark %s failed", mission_id)
        await self._bus.publish(
            EventRecord(
                mission_id=mission_id,
                type=EventType.MISSION_FAILED,
                payload={"error": f"{type(exc).__name__}: {exc}"},
            )
        )
