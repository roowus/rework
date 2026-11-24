"""Admission control: REWORK_MAX_ROLES queues role runs instead of running six
pytest/mypy trees on a 2-CPU box (docs/ops.md#rewvis, 2026-11-13), and a mission
that is only WAITING must not hold a REWORK_CONCURRENCY slot."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from rework.config import Config
from rework.kernel.events.bus import InProcessEventBus
from rework.kernel.state.records import Mission, Run, new_id, run_id
from rework.kernel.state.sqlite import open_sqlite
from rework.orchestrator.agent import MissionOrchestrator
from rework.orchestrator.daemon import Daemon
from rework.orchestrator.engine import RunSupervisor
from rework.orchestrator.limits import MissionSlot, RoleLimiter


async def _work(limiter: RoleLimiter, rid: str, seen: list[int], *, sleep: float = 0.05) -> None:
    await limiter.acquire(rid)
    try:
        seen.append(limiter.running)
        await asyncio.sleep(sleep)
    finally:
        limiter.release(rid)


@pytest.mark.parametrize("cap", [1, 2])
async def test_never_more_than_cap_roles_run_at_once(cap: int) -> None:
    limiter = RoleLimiter(cap)
    seen: list[int] = []
    await asyncio.gather(*[_work(limiter, f"r{i}", seen) for i in range(cap * 3)])

    assert seen, "no run ever got a slot"
    assert max(seen) <= cap
    assert limiter.max_seen <= cap, f"high-water mark {limiter.max_seen} exceeded cap {cap}"
    assert limiter.running == 0
    assert limiter.queued == ()


async def test_excess_runs_report_queued_until_a_slot_is_free() -> None:
    limiter = RoleLimiter(1)
    assert await limiter.acquire("r-a") is False  # straight in, no wait
    assert limiter.queue_note("r-a") is None

    waiter = asyncio.create_task(limiter.acquire("r-b"))
    await asyncio.sleep(0)  # let it reach the queue
    assert limiter.is_queued("r-b") is True
    note = limiter.queue_note("r-b")
    assert note is not None and "queued" in note
    assert "cap 1" in note
    assert limiter.running == 1

    limiter.release("r-a")
    assert await asyncio.wait_for(waiter, timeout=1) is True  # it had to wait
    assert limiter.is_queued("r-b") is False
    assert limiter.running == 1


async def test_queue_is_fifo() -> None:
    limiter = RoleLimiter(1)
    await limiter.acquire("first")
    tasks = {rid: asyncio.create_task(limiter.acquire(rid)) for rid in ("second", "third")}
    await asyncio.sleep(0)
    assert limiter.queued == ("second", "third")

    limiter.release("first")
    assert await asyncio.wait_for(tasks["second"], timeout=1) is True
    assert limiter.queued == ("third",)
    limiter.release("second")
    assert await asyncio.wait_for(tasks["third"], timeout=1) is True
    limiter.release("third")
    assert limiter.running == 0


async def test_slot_is_released_when_a_run_completes() -> None:
    limiter = RoleLimiter(1)
    await _work(limiter, "done", [])
    assert limiter.running == 0
    assert await limiter.acquire("next") is False  # free immediately


async def test_slot_is_released_when_a_run_raises() -> None:
    limiter = RoleLimiter(1)

    async def boom() -> None:
        await limiter.acquire("bad")
        try:
            raise RuntimeError("role blew up")
        finally:
            limiter.release("bad")

    with pytest.raises(RuntimeError):
        await boom()
    assert limiter.running == 0
    assert await limiter.acquire("next") is False  # a dead run leaks nothing


async def test_cancelling_a_queued_run_holds_no_slot() -> None:
    limiter = RoleLimiter(1)
    await limiter.acquire("running")
    waiter = asyncio.create_task(limiter.acquire("queued"))
    await asyncio.sleep(0)
    assert limiter.is_queued("queued") is True

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert limiter.running == 1, "a cancelled waiter must not change the running count"
    assert limiter.is_queued("queued") is False

    limiter.release("running")
    assert limiter.running == 0
    assert await limiter.acquire("later") is False  # the free slot is really free


async def test_cancel_racing_admission_gives_the_slot_back() -> None:
    """_admit() can hand a waiter its slot in the same breath as the task is
    cancelled: the run never proceeds, so the slot must go back or the cap
    leaks for the life of the daemon."""
    limiter = RoleLimiter(1)
    await limiter.acquire("running")
    waiter = asyncio.create_task(limiter.acquire("racing"))
    await asyncio.sleep(0)
    assert limiter.is_queued("racing") is True

    limiter.release("running")  # admits "racing" ...
    waiter.cancel()  # ... and the cancellation lands before it resumes
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert limiter.running == 0, "the admitted-then-cancelled run leaked its slot"
    assert await limiter.acquire("later") is False


class CountingSem:
    """An asyncio.Semaphore that counts acquire/release, so a test can prove a
    nested wait released exactly once."""

    def __init__(self, value: int) -> None:
        self._sem = asyncio.Semaphore(value)
        self.acquires = 0
        self.releases = 0

    async def acquire(self) -> None:
        await self._sem.acquire()
        self.acquires += 1

    def release(self) -> None:
        self.releases += 1
        self._sem.release()


async def test_a_waiting_mission_does_not_hold_its_slot() -> None:
    """The owner requirement behind the 2026-11-13 incident: with
    REWORK_CONCURRENCY=1 an approved gate never ran, because its mission was
    parked at the gate while holding the one slot."""
    sem = CountingSem(1)
    mission_a = MissionSlot(sem, "m-a")  # type: ignore[arg-type]
    mission_b = MissionSlot(sem, "m-b")  # type: ignore[arg-type]
    await mission_a.acquire()
    assert mission_a.held is True

    b_task = asyncio.create_task(mission_b.acquire())
    await asyncio.sleep(0)
    assert mission_b.held is False, "b must be blocked while a is working"

    async with mission_a.while_waiting():
        assert mission_a.held is False, "a mission waiting at a gate holds no slot"
        assert await asyncio.wait_for(b_task, timeout=1) is None
        assert mission_b.held is True, "the waiting mission's slot went to another mission"
        mission_b.release()

    assert mission_a.held is True, "a takes its slot back before it works again"
    # a acquires, b acquires, a re-acquires; releases are a's waiting block and
    # b's own release (exactly one per acquire)
    assert sem.acquires == 3 and sem.releases == 2


async def test_nested_waits_release_only_once() -> None:
    sem = CountingSem(1)
    slot = MissionSlot(sem, "m-a")  # type: ignore[arg-type]
    await slot.acquire()

    async with slot.while_waiting():
        assert slot.held is False
        async with slot.while_waiting():  # an inner wait must not release twice
            assert slot.held is False
            other = MissionSlot(sem, "m-b")  # type: ignore[arg-type]
            await asyncio.wait_for(other.acquire(), timeout=1)
            other.release()
        assert slot.held is False, "the slot already went back; the inner wait did nothing"
    assert slot.held is True
    # one release from the outer wait, one from the other mission's slot:
    # the nested wait must not have added a second
    assert sem.acquires == 3 and sem.releases == 2


async def test_waiting_without_the_slot_never_touches_the_semaphore() -> None:
    sem = CountingSem(1)
    slot = MissionSlot(sem, "m-a")  # type: ignore[arg-type]
    async with slot.while_waiting():  # never acquired: nothing to release
        assert slot.held is False
    assert sem.acquires == 0 and sem.releases == 0
    assert slot.held is False


async def test_releasing_a_slot_the_run_no_longer_holds_is_a_no_op() -> None:
    """A parked run hands its slot back; the `finally` in `_execute` then runs
    `release` on a slot it no longer holds. It must not free somebody else's."""
    from rework.orchestrator.limits import RoleLimiter

    limiter = RoleLimiter(cap=2)
    await limiter.acquire("parked")
    await limiter.acquire("busy")
    assert limiter.running == 2

    async with limiter.while_waiting("parked"):
        assert limiter.running == 1, "the parked run gave its slot back"
        assert limiter._held == {"busy"}

    limiter.release("parked")  # the run finishes normally
    assert limiter.running == 1, "the second release must not free the busy run"
    limiter.release("busy")
    assert limiter.running == 0


async def test_a_park_resumes_only_when_a_slot_is_free() -> None:
    """Leaving a park waits for a free slot, exactly like starting a run does.

    A run that took the freed slot keeps it until it finishes, the resumed run
    then takes it, and nothing leaks. This is the honest behaviour: a park hands
    capacity back, it does not reserve capacity."""
    from rework.orchestrator.limits import RoleLimiter

    limiter = RoleLimiter(cap=1)
    await limiter.acquire("parked")

    async def release_fresh_later() -> None:
        await asyncio.sleep(0.05)
        limiter.release("fresh")

    releaser = asyncio.create_task(release_fresh_later())
    async with limiter.while_waiting("parked"):
        fresh = asyncio.create_task(limiter.acquire("fresh"))
        await asyncio.sleep(0)
        assert await fresh is False, "the freed slot went to the fresh run at once"
        assert limiter._held == {"fresh"}
    # leaving the park waited for `fresh` to finish, then took the slot back
    assert limiter._held == {"parked"}
    await releaser
    limiter.release("parked")
    assert limiter.running == 0


async def test_release_is_idempotent() -> None:
    sem = CountingSem(1)
    slot = MissionSlot(sem, "m-a")  # type: ignore[arg-type]
    await slot.acquire()
    await slot.acquire()  # already held: no second acquire
    assert sem.acquires == 1
    slot.release()
    slot.release()  # not held: no second release, or the cap would silently grow
    assert sem.releases == 1


async def test_a_queued_run_reads_as_queued_through_check_in(tmp_path: Any) -> None:
    """The orchestrator must SEE the queue: a silent one looks like a hung role.
    (This is the seam check_in/live_label actually renders from.)"""
    store = await open_sqlite(str(tmp_path / "q.sqlite3"))
    try:
        mission = await store.create_mission(Mission(id=new_id("m"), objective="x"))
        rid = run_id(mission.id, "do", "coder", 1)
        run = await store.create_run(Run(id=rid, mission_id=mission.id, role="coder", step="do"))
        limiter = RoleLimiter(1)
        supervisor = RunSupervisor(
            mission,
            store,
            InProcessEventBus(store),
            None,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            tmp_path,
            limiter=limiter,
        )
        # stand-in for the orchestrator: check_in only needs these attributes
        orch = object.__new__(MissionOrchestrator)
        orch.r = SimpleNamespace(_supervisor=supervisor)  # type: ignore[assignment]
        orch.store = store
        orch.mid = mission.id
        orch._events = []
        orch._event_cursor = 0

        await limiter.acquire("someone-else")
        waiter = asyncio.create_task(limiter.acquire(rid))
        await asyncio.sleep(0)

        text = await orch._check_in(rid, 5)
        assert "queued" in text, text
        assert "cap 1" in text, text
        assert orch._live_label(run) == f"{rid} (queued)"

        limiter.release("someone-else")
        await asyncio.wait_for(waiter, timeout=1)
        text = await orch._check_in(rid, 5)
        assert "queued" not in text, "once admitted, the run is no longer queued"
        assert orch._live_label(run) == rid
        assert await store.get_run(rid) is not None
    finally:
        await store.aclose()


async def test_daemon_wires_both_caps_from_the_config(tmp_path: Any) -> None:
    """The knobs only matter if the daemon actually reads them: one shared
    role limiter (REWORK_MAX_ROLES) and one mission semaphore
    (REWORK_CONCURRENCY) for the whole process."""
    cfg = Config(
        db="postgresql://postgres@db/rework_state",
        checkpoints_db="postgresql://postgres@db/rework_state",
        poll_interval_s=0.05,
        concurrency=2,
        max_roles=3,
    )
    daemon = Daemon(
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        tmp_path,
        cfg,
    )
    assert daemon._limiter.cap == 3, "REWORK_MAX_ROLES caps RUNNING roles daemon-wide"
    assert daemon._sem._free == 2, "REWORK_CONCURRENCY caps WORKING missions"  # type: ignore[attr-defined]


async def test_a_gated_mission_resumes_ahead_of_never_started_missions() -> None:
    """The invisible-gate bug end to end: the mission steps aside at a gate, a
    never-started mission queues for its slot, then the gate is decided. The
    resumed mission must run its tail BEFORE the never-started one."""
    from rework.orchestrator.limits import PrioritySlots

    slots = PrioritySlots(1)
    order: list[str] = []
    gated = MissionSlot(slots, "m-gated")
    await gated.acquire()

    release_gate = asyncio.Event()

    async def gated_mission() -> None:
        async with gated.while_waiting():  # parked at the gate: slot given up
            await release_gate.wait()  # ... the owner approves
        order.append("gated-tail")  # the post-gate work (push, open the PR)
        gated.release()

    async def new_mission() -> None:
        slot = MissionSlot(slots, "m-new")
        await slot.acquire()
        order.append("new-mission")
        slot.release()

    waiter = asyncio.create_task(gated_mission())
    await asyncio.sleep(0)
    # the slot is free right now, so occupy it with a long-running mission and
    # queue the never-started one behind it
    busy = MissionSlot(slots, "m-busy")
    await busy.acquire()
    newcomer = asyncio.create_task(new_mission())
    await asyncio.sleep(0)
    release_gate.set()  # decided: gated now queues for its slot back
    await asyncio.sleep(0)
    busy.release()
    await asyncio.wait_for(asyncio.gather(waiter, newcomer), timeout=2)
    assert order == ["gated-tail", "new-mission"], order


async def test_resumed_acquire_jumps_the_fresh_queue() -> None:
    from rework.orchestrator.limits import PrioritySlots

    slots = PrioritySlots(1)
    await slots.acquire()  # the slot is busy
    order: list[str] = []

    async def take(name: str, resumed: bool) -> None:
        await slots.acquire(resumed=resumed)
        order.append(name)
        slots.release()

    fresh = [asyncio.create_task(take(f"fresh-{i}", False)) for i in range(3)]
    await asyncio.sleep(0)
    resumed = asyncio.create_task(take("resumed", True))
    await asyncio.sleep(0)
    slots.release()
    await asyncio.wait_for(asyncio.gather(resumed, *fresh), timeout=2)
    assert order == ["resumed", "fresh-0", "fresh-1", "fresh-2"]


async def test_priority_slots_never_exceed_the_cap_and_survive_cancellation() -> None:
    from rework.orchestrator.limits import PrioritySlots

    slots = PrioritySlots(1)
    await slots.acquire()
    queued = asyncio.create_task(slots.acquire())
    await asyncio.sleep(0)
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    slots.release()
    # the cancelled waiter must not have swallowed the slot
    await asyncio.wait_for(slots.acquire(), timeout=1)
    slots.release()
    assert slots._free == 1
