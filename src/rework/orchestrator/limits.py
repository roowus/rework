"""Admission control for role runs: the daemon-wide cap on how many roles are
RUNNING at once (REWORK_MAX_ROLES, docs/ops.md#rewvis).

Before this, every `spawn_role` became an unguarded asyncio task, so a batch
mission with four parallel investigators produced 6-7 concurrent pytest / mypy /
uv sync trees on a 2-CPU box and swap-thrashed it into a hard reboot
(2026-11-13 incident). Runs beyond the cap are QUEUED, never refused: they hold
a RunStatus.INTENT row and the orchestrator sees "queued" in wait/check_in.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

log = logging.getLogger("rework.limits")


class RoleLimiter:
    """One per daemon process, shared by every mission's RunSupervisor.

    An explicit FIFO queue rather than a bare Semaphore, so "who is waiting"
    is answerable (queued runs are visible in wait/check_in) and a cancelled
    waiter can never consume a slot it was granted on the way out.
    """

    def __init__(self, cap: int, poll_interval_s: float = 0.5) -> None:
        self.cap = max(1, int(cap))
        self.running = 0
        self.max_seen = 0  # high-water mark: tests assert it never exceeds cap
        self._queue: list[str] = []
        self._waiters: dict[str, asyncio.Future[None]] = {}
        self._poll = poll_interval_s

    # -- introspection (check_in, wait, tests) -----------------------------
    @property
    def queued(self) -> tuple[str, ...]:
        """Run ids waiting for a slot, oldest first."""
        return tuple(self._queue)

    def is_queued(self, run_id: str) -> bool:
        return run_id in self._queue

    def queue_note(self, run_id: str) -> str | None:
        """How to describe a waiting run, or None when it is not queued."""
        if run_id not in self._queue:
            return None
        return f"queued (waiting for a free role slot, cap {self.cap})"

    # -- admission ---------------------------------------------------------
    async def acquire(self, run_id: str) -> bool:
        """Take a slot for `run_id`, waiting while the cap is reached.

        Returns True when the run had to wait. Cancelling a queued run leaks
        nothing: it never held a slot, and :meth:`_admit` skips waiters whose
        future is already done.
        """
        if self.running < self.cap and not self._queue:
            self.running += 1
            self.max_seen = max(self.max_seen, self.running)
            return False
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._queue.append(run_id)
        self._waiters[run_id] = fut
        log.info("run %s queued: %d roles running (cap %d)", run_id, self.running, self.cap)
        try:
            await fut
        except asyncio.CancelledError:
            self._forget(run_id)
            if fut.done() and not fut.cancelled():
                # _admit() handed this run its slot in the same tick the task
                # was cancelled, so the run never proceeds: give the slot back
                # instead of leaking it for the life of the daemon.
                self.running = max(0, self.running - 1)
                self._admit()
            log.info("run %s cancelled while queued; no slot was held", run_id)
            raise
        finally:
            self._waiters.pop(run_id, None)
        log.info("run %s admitted: %d/%d roles running", run_id, self.running, self.cap)
        return True

    def release(self, run_id: str) -> None:
        """Free the slot a run held and hand it to the next queued run."""
        self.running = max(0, self.running - 1)
        self._admit()

    def _forget(self, run_id: str) -> None:
        try:
            self._queue.remove(run_id)
        except ValueError:
            pass

    def _admit(self) -> None:
        while self._queue and self.running < self.cap:
            run_id = self._queue[0]
            fut = self._waiters.get(run_id)
            if fut is None or fut.done():
                self._queue.pop(0)
                continue  # cancelled while queued: it never gets a slot
            self._queue.pop(0)
            self.running += 1
            self.max_seen = max(self.max_seen, self.running)
            fut.set_result(None)


class MissionSlot:
    """One daemon mission slot (REWORK_CONCURRENCY, docs/ops.md#rewvis).

    A mission that is only WAITING — at a gate, for the owner, on hold — must
    not hold this slot, or an approved gate sits behind a mission that is doing
    nothing. :meth:`while_waiting` drops the slot for the duration of a wait and
    takes it back (fairly, at the back of the queue) before work resumes.
    Re-entrant: nested waits release once.
    """

    def __init__(self, sem: asyncio.Semaphore, mission_id: str) -> None:
        self._sem = sem
        self._mission_id = mission_id
        self.held = False

    async def acquire(self) -> None:
        if self.held:
            return
        await self._sem.acquire()
        self.held = True

    def release(self) -> None:
        if not self.held:
            return
        self.held = False
        self._sem.release()

    @asynccontextmanager
    async def while_waiting(self) -> AsyncIterator[None]:
        if not self.held:
            yield  # already released by an outer wait
            return
        self.release()
        try:
            yield
        finally:
            await self.acquire()
