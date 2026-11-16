"""Runtime capacity: the daemon's two caps follow the box (REWORK_CAPACITY).

The rules themselves (plan) are pure functions over numbers, the hysteresis is
pure and synchronous, and only the watchdog is asyncio, so every test here runs
without sleeping a tick interval or touching the real host."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest

from rework.config import Config
from rework.orchestrator.capacity import (
    CONCURRENCY_FLOOR,
    GB,
    HYSTERESIS_MEASUREMENTS,
    MIN_RAM_PER_ROLE_BYTES,
    MULTICORE_MIN_ROLES,
    Capacity,
    CapacityPolicy,
    CapacitySettings,
    DynamicCapacity,
    available_bytes,
    measure,
    plan,
)
from rework.orchestrator.daemon import Daemon
from rework.orchestrator.limits import PrioritySlots, RoleLimiter


@pytest.fixture(autouse=True)
def _no_ambient_capacity_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ambient env must not decide what these tests prove."""
    for name in (
        "REWORK_CAPACITY",
        "REWORK_CAPACITY_INTERVAL_S",
        "REWORK_CONCURRENCY",
        "REWORK_MAX_ROLES",
    ):
        monkeypatch.delenv(name, raising=False)


# -- plan: the rules ----------------------------------------------------------


def test_idle_box_gets_one_role_and_one_mission_per_cpu() -> None:
    cap = plan(8, 0.0, None)
    assert (cap.concurrency, cap.max_roles) == (8, 8)
    assert "8 CPUs" in cap.reason
    assert "8 missions / 8 roles" in cap.reason


def test_quiet_two_core_box_plans_two_and_two() -> None:
    cap = plan(2, 0.1, None)
    assert (cap.concurrency, cap.max_roles) == (2, 2)


def test_load_above_the_cpu_count_halves_the_roles() -> None:
    cap = plan(8, 16.0, None)  # 2x the CPU count
    assert cap.max_roles == 4
    assert cap.concurrency == 4, "the mission cap never exceeds the role cap"


def test_a_busy_multi_core_box_keeps_at_least_two_roles() -> None:
    cap = plan(2, 50.0, None)
    assert cap.max_roles == MULTICORE_MIN_ROLES == 2


def test_plenty_of_ram_does_not_cut_roles() -> None:
    cap = plan(4, 0.2, 8 * GB)
    assert (cap.concurrency, cap.max_roles) == (4, 4)
    assert "8.0 GB free" in cap.reason


def test_low_ram_cuts_the_roles_until_the_box_can_hold_them() -> None:
    cap = plan(16, 0.0, 2 * GB)
    assert cap.max_roles == 2 * GB // MIN_RAM_PER_ROLE_BYTES
    assert cap.max_roles == 1
    assert cap.concurrency == CONCURRENCY_FLOOR


def test_a_single_cpu_box_never_plans_zero() -> None:
    cap = plan(1, 12.0, 100)
    assert cap.concurrency >= 1
    assert cap.max_roles >= 1


def test_the_mission_cap_is_never_more_than_the_role_cap() -> None:
    for cpus, load in ((1, 0.0), (2, 0.0), (8, 9.0), (16, 40.0), (4, 0.5)):
        cap = plan(cpus, load, None)
        assert cap.concurrency <= cap.max_roles, (cpus, load, cap)
        assert cap.concurrency >= CONCURRENCY_FLOOR


def test_measure_never_raises_and_reports_no_ram_without_proc(tmp_path: Path) -> None:
    cpus, load1, avail = measure(tmp_path / "no-such-meminfo")
    assert cpus >= 1
    assert load1 >= 0.0
    assert avail is None


def test_memavailable_is_parsed_in_bytes(tmp_path: Path) -> None:
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:       1000 kB\nMemAvailable:  2048 kB\n")
    assert available_bytes(meminfo) == 2048 * 1024
    assert available_bytes(tmp_path) is None  # unreadable: None, never a raise


# -- hysteresis ---------------------------------------------------------------


def test_a_one_measurement_spike_changes_nothing() -> None:
    policy = CapacityPolicy(concurrency=2, max_roles=2)
    assert policy.apply_initial(2, 0.1, None) == Capacity(2, 2, policy.target(2, 0.1, None).reason)
    assert policy.observe(8, 0.0, None) is None, "one spike must not move the caps"
    assert (policy.concurrency, policy.max_roles) == (2, 2)
    assert policy.observe(2, 0.1, None) is None, "back to normal: still nothing pending"
    assert (policy.concurrency, policy.max_roles) == (2, 2)


def test_two_consecutive_measurements_do_change_the_caps() -> None:
    policy = CapacityPolicy(concurrency=2, max_roles=2)
    policy.apply_initial(2, 0.1, None)
    assert policy.observe(8, 0.0, None) is None
    new = policy.observe(8, 0.0, None)
    assert new is not None
    assert (new.concurrency, new.max_roles) == (8, 8)
    assert (policy.concurrency, policy.max_roles) == (8, 8)
    assert HYSTERESIS_MEASUREMENTS == 2


def test_a_shrink_also_needs_confirmation() -> None:
    policy = CapacityPolicy(concurrency=8, max_roles=8)
    policy.apply_initial(8, 0.0, None)
    assert policy.observe(8, 40.0, None) is None
    new = policy.observe(8, 40.0, None)
    assert new is not None and new.max_roles == 4


def test_the_first_measurement_is_applied_unconditionally() -> None:
    policy = CapacityPolicy(concurrency=1, max_roles=1)
    first = policy.apply_initial(8, 0.0, None)
    assert first is not None and (first.concurrency, first.max_roles) == (8, 8)


# -- settings / precedence ----------------------------------------------------


def test_defaults_are_auto_every_sixty_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = CapacitySettings.from_env()
    assert settings.auto is True
    assert settings.interval_s == 60.0
    assert settings.fixed_concurrency is False
    assert settings.fixed_max_roles is False


@pytest.mark.parametrize("raw", ["fixed", " FIXED "])
def test_fixed_mode_is_recognised(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("REWORK_CAPACITY", raw)
    assert CapacitySettings.from_env().auto is False


@pytest.mark.parametrize("raw", ["", "auto", "AUTO", "nonsense"])
def test_unknown_or_empty_mode_is_auto(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("REWORK_CAPACITY", raw)
    assert CapacitySettings.from_env().auto is True


@pytest.mark.parametrize("raw", ["", "soon", "0", "-5"])
def test_bad_interval_falls_back_to_sixty(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("REWORK_CAPACITY_INTERVAL_S", raw)
    assert CapacitySettings.from_env().interval_s == 60.0


def test_a_good_interval_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REWORK_CAPACITY_INTERVAL_S", "5")
    assert CapacitySettings.from_env().interval_s == 5.0


def test_explicit_concurrency_env_fixes_only_that_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REWORK_CONCURRENCY", "3")
    settings = CapacitySettings.from_env()
    assert settings.fixed_concurrency is True
    assert settings.fixed_max_roles is False

    policy = CapacityPolicy(
        concurrency=3,
        max_roles=2,
        fixed_concurrency=settings.fixed_concurrency,
        fixed_max_roles=settings.fixed_max_roles,
    )
    new = policy.apply_initial(8, 0.0, None)
    assert new is not None
    assert new.concurrency == 3, "an explicit REWORK_CONCURRENCY is never re-derived"
    assert new.max_roles == 8, "auto still manages the role cap"


def test_explicit_max_roles_env_fixes_only_that_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REWORK_MAX_ROLES", "5")
    settings = CapacitySettings.from_env()
    assert settings.fixed_max_roles is True
    assert settings.fixed_concurrency is False

    policy = CapacityPolicy(
        concurrency=1,
        max_roles=5,
        fixed_concurrency=settings.fixed_concurrency,
        fixed_max_roles=settings.fixed_max_roles,
    )
    new = policy.apply_initial(8, 0.0, None)
    assert new is not None
    assert new.max_roles == 5, "an explicit REWORK_MAX_ROLES is never re-derived"
    assert new.concurrency == 8


# -- PrioritySlots.set_capacity ----------------------------------------------


async def test_growing_the_mission_cap_admits_a_parked_waiter() -> None:
    slots = PrioritySlots(1)
    await slots.acquire()
    waiter = asyncio.create_task(slots.acquire())
    await asyncio.sleep(0)
    assert not waiter.done(), "it must be parked on the single slot"
    assert slots.capacity == 1

    slots.set_capacity(2)
    assert slots.capacity == 2
    await asyncio.wait_for(waiter, timeout=1)  # admitted at once, no release first
    assert slots.free == 0


async def test_shrinking_does_not_kill_a_running_mission_and_the_free_count_goes_negative() -> None:
    slots = PrioritySlots(2)
    await slots.acquire()
    await slots.acquire()
    assert slots.free == 0

    slots.set_capacity(1)  # the box got busy: one mission slot from now on
    assert slots.capacity == 1
    assert slots.free == -1, "two missions still hold a slot; nothing was cancelled"

    parked = asyncio.create_task(slots.acquire())
    await asyncio.sleep(0)
    assert not parked.done(), "a shrunk cap admits nobody new"

    slots.release()  # a running mission finishes on its own
    assert slots.free == 0, "one release drains the overdraft, nothing more"
    await asyncio.sleep(0)
    assert not parked.done(), "still at the cap: one mission is running"

    slots.release()  # the second one finishes too
    await asyncio.wait_for(parked, timeout=1)  # the freed slot really came back
    assert slots.free == 0
    assert slots.capacity == 1


def test_set_capacity_floors_at_one() -> None:
    slots = PrioritySlots(3)
    slots.set_capacity(0)
    assert slots.capacity == 1
    assert slots.free == 1


# -- RoleLimiter.set_cap -----------------------------------------------------


async def test_growing_the_role_cap_admits_a_queued_run_at_once() -> None:
    limiter = RoleLimiter(1)
    assert await limiter.acquire("running") is False
    waiter = asyncio.create_task(limiter.acquire("queued"))
    await asyncio.sleep(0)
    assert limiter.is_queued("queued") is True

    limiter.set_cap(2)
    assert await asyncio.wait_for(waiter, timeout=1) is True
    assert limiter.running == 2
    assert limiter.queued == ()


async def test_shrinking_stops_admission_but_running_roles_keep_their_slots() -> None:
    limiter = RoleLimiter(3)
    for rid in ("a", "b", "c"):
        assert await limiter.acquire(rid) is False
    assert limiter.running == 3

    limiter.set_cap(1)
    assert limiter.cap == 1
    assert limiter.running == 3, "a shrink must never cancel running work"

    waiter = asyncio.create_task(limiter.acquire("late"))
    await asyncio.sleep(0)
    assert limiter.is_queued("late") is True, "nothing is admitted while over the new cap"

    limiter.release("a")
    limiter.release("b")
    await asyncio.sleep(0)
    assert limiter.running == 1
    assert limiter.is_queued("late") is True, "still at the cap after two drains"

    limiter.release("c")
    assert await asyncio.wait_for(waiter, timeout=1) is True
    assert limiter.running == 1


def test_set_cap_floors_at_one() -> None:
    limiter = RoleLimiter(4)
    limiter.set_cap(0)
    assert limiter.cap == 1


# -- DynamicCapacity: the watchdog -------------------------------------------


def _settings(**kwargs: Any) -> CapacitySettings:
    base: dict[str, Any] = {"mode": "auto", "interval_s": 60.0}
    base.update(kwargs)
    return CapacitySettings(**base)


def test_the_watchdog_applies_the_first_plan_and_logs_one_change(
    caplog: pytest.LogCaptureFixture,
) -> None:
    slots, limiter = PrioritySlots(1), RoleLimiter(1)
    dyn = DynamicCapacity(slots, limiter, settings=_settings(), measure_fn=lambda: (8, 0.0, None))
    with caplog.at_level(logging.INFO, logger="rework.capacity"):
        dyn.apply_now()
    assert (slots.capacity, limiter.cap) == (8, 8)
    lines = [r.message for r in caplog.records if r.name == "rework.capacity"]
    assert len(lines) == 1
    assert "1 missions / 1 roles -> 8 missions / 8 roles" in lines[0]
    assert "8 CPUs" in lines[0], "the reason sentence says why"


def test_no_log_line_when_the_plan_did_not_change(caplog: pytest.LogCaptureFixture) -> None:
    slots, limiter = PrioritySlots(8), RoleLimiter(8)
    dyn = DynamicCapacity(slots, limiter, settings=_settings(), measure_fn=lambda: (8, 0.0, None))
    with caplog.at_level(logging.INFO, logger="rework.capacity"):
        assert dyn.observe_now() is None
    assert [r for r in caplog.records if r.name == "rework.capacity"] == []


def test_one_bad_measurement_does_not_move_the_caps(caplog: pytest.LogCaptureFixture) -> None:
    slots, limiter = PrioritySlots(2), RoleLimiter(2)
    readings = [(2, 0.0, None), (2, 30.0, None)]
    dyn = DynamicCapacity(slots, limiter, settings=_settings(), measure_fn=lambda: readings.pop(0))
    dyn.apply_now()
    assert (slots.capacity, limiter.cap) == (2, 2)
    with caplog.at_level(logging.INFO, logger="rework.capacity"):
        assert dyn.observe_now() is None
    assert (slots.capacity, limiter.cap) == (2, 2)
    assert [r for r in caplog.records if r.name == "rework.capacity"] == []


def test_two_bad_measurements_halve_the_caps(caplog: pytest.LogCaptureFixture) -> None:
    slots, limiter = PrioritySlots(8), RoleLimiter(8)
    readings = [(8, 0.0, None), (8, 30.0, None), (8, 30.0, None)]
    dyn = DynamicCapacity(slots, limiter, settings=_settings(), measure_fn=lambda: readings.pop(0))
    dyn.apply_now()
    with caplog.at_level(logging.INFO, logger="rework.capacity"):
        assert dyn.observe_now() is None
        assert dyn.observe_now() is not None
    assert (slots.capacity, limiter.cap) == (4, 4)
    assert len([r for r in caplog.records if r.name == "rework.capacity"]) == 1


def test_fixed_mode_does_nothing_at_all(caplog: pytest.LogCaptureFixture) -> None:
    slots, limiter = PrioritySlots(2), RoleLimiter(3)
    dyn = DynamicCapacity(
        slots,
        limiter,
        settings=_settings(mode="fixed"),
        measure_fn=lambda: (8, 0.0, None),
    )
    assert dyn.enabled is False
    with caplog.at_level(logging.INFO, logger="rework.capacity"):
        assert dyn.apply_now() is None
        assert dyn.observe_now() is None
        assert dyn.start() is None
        assert dyn.task is None, "fixed mode starts no watchdog"
    assert (slots.capacity, limiter.cap) == (2, 3), "caps stay exactly as constructed"
    assert [r for r in caplog.records if r.name == "rework.capacity"] == []


def test_both_explicit_caps_leave_nothing_to_manage() -> None:
    slots, limiter = PrioritySlots(2), RoleLimiter(3)
    dyn = DynamicCapacity(
        slots,
        limiter,
        settings=_settings(fixed_concurrency=True, fixed_max_roles=True),
        measure_fn=lambda: (8, 0.0, None),
    )
    assert dyn.enabled is False
    assert dyn.start() is None


async def test_the_watchdog_starts_and_stops_without_leaking_a_task() -> None:
    slots, limiter = PrioritySlots(1), RoleLimiter(1)
    dyn = DynamicCapacity(
        slots, limiter, settings=_settings(interval_s=0.5), measure_fn=lambda: (2, 0.0, None)
    )
    task = dyn.start()
    assert task is not None
    assert (slots.capacity, limiter.cap) == (2, 2)

    await dyn.stop()
    assert dyn.task is None
    assert task.done(), "the watchdog must not outlive the daemon"
    await dyn.stop()  # idempotent: a second finally must not raise


async def test_the_watchdog_ticks_and_applies_a_confirmed_change() -> None:
    slots, limiter = PrioritySlots(1), RoleLimiter(1)
    readings = [(4, 0.0, None), (2, 0.0, None), (2, 0.0, None)]
    dyn = DynamicCapacity(
        slots,
        limiter,
        settings=_settings(interval_s=0.01),
        measure_fn=lambda: readings.pop(0) if readings else (2, 0.0, None),
    )
    task = dyn.start()
    assert task is not None
    try:
        for _ in range(100):
            if (slots.capacity, limiter.cap) == (2, 2):
                break
            await asyncio.sleep(0.01)
        assert (slots.capacity, limiter.cap) == (2, 2), dyn.last
    finally:
        await dyn.stop()


# -- daemon wiring ------------------------------------------------------------


def test_the_daemon_builds_its_watchdog_from_the_two_caps(tmp_path: Path) -> None:
    """The daemon's own construction path: the policy starts from the caps the
    two limiters hold, so an explicit env value stays fixed."""
    cfg = Config(
        db="postgresql://postgres@db/rework_state",
        checkpoints_db="postgresql://postgres@db/rework_state",
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
    assert daemon._capacity.policy.concurrency == 2
    assert daemon._capacity.policy.max_roles == 3
    assert daemon._capacity.task is None, "no task outside a running loop"


async def test_the_daemon_starts_the_watchdog_inside_the_loop_and_stops_it(tmp_path: Path) -> None:
    cfg = Config(
        db="postgresql://postgres@db/rework_state",
        checkpoints_db="postgresql://postgres@db/rework_state",
        concurrency=1,
        max_roles=1,
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
    task = daemon._capacity.start()
    try:
        assert task is not None
        assert daemon._capacity.task is task
        # a quiet box: one role and one mission per CPU, unconditionally
        cpus = daemon._capacity.policy.max_roles
        assert cpus >= 1
        assert daemon._sem.capacity == cpus
        assert daemon._limiter.cap == cpus
    finally:
        await daemon._capacity.stop()
    assert daemon._capacity.task is None
    assert task.done()


async def test_run_forever_cancelled_leaves_no_capacity_task_behind(tmp_path: Path) -> None:
    """The real loop: the watchdog runs, the caps were applied, and cancelling
    the daemon (what `rework serve` does on SIGTERM) tears the task down."""
    from rework.kernel.events.bus import InProcessEventBus
    from rework.kernel.state.sqlite import open_sqlite

    db, ck = tmp_path / "state.sqlite3", tmp_path / "checkpoints.sqlite3"
    store = await open_sqlite(str(db))
    daemon = Daemon(
        store=store,
        bus=InProcessEventBus(store),
        registry=None,  # type: ignore[arg-type]
        catalog=None,  # type: ignore[arg-type]
        make_checkpointer=None,  # type: ignore[arg-type]
        workroot=tmp_path / "work",
        config=Config(db=str(db), checkpoints_db=str(ck), poll_interval_s=0.01),
    )
    task = asyncio.create_task(daemon.run_forever())
    try:
        for _ in range(100):
            if daemon._capacity.task is not None:
                break
            await asyncio.sleep(0.01)
        watchdog = daemon._capacity.task
        assert watchdog is not None, "the loop must start the watchdog"
        assert not watchdog.done()
        # the caps now follow the host, not the startup snapshot
        assert daemon._sem.capacity == daemon._limiter.cap
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await store.aclose()
    assert daemon._capacity.task is None
    assert watchdog.done(), "no leaked task after the daemon is cancelled"
