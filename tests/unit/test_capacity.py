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


MEASUREMENTS: tuple[tuple[int, float, int | None], ...] = (
    (1, 0.0, None),
    (1, 12.0, 100),
    (2, 0.0, None),
    (4, 0.5, None),
    (8, 0.0, None),
    (8, 9.0, None),
    (8, 12.4, 5_100_000_000),
    (12, 0.0, None),
    (16, 40.0, None),
    (16, 0.0, 2 * GB),
)


def test_the_invariant_holds_on_every_pinned_path_too() -> None:
    """The unpinned sweep above is not the whole story: `target()` re-applies the
    explicit env caps with the pin winning for that cap, so the mission cap could
    come back above the role cap there. Sweep the pinned combinations as well.

    The invariant is scoped to the caps auto manages: with BOTH caps pinned there
    is nothing for auto to do, so those two cells are the documented exception
    (the owner gets exactly what they asked for, see
    test_both_pinned_caps_are_a_total_no_op)."""
    for fixed_concurrency, pinned_missions in ((True, 1), (True, 3), (True, 8)):
        for fixed_max_roles, pinned_roles in ((False, 0), (True, 2), (True, 5)):
            policy = CapacityPolicy(
                concurrency=pinned_missions,
                max_roles=pinned_roles or 1,
                fixed_concurrency=fixed_concurrency,
                fixed_max_roles=fixed_max_roles,
            )
            both_pinned = fixed_concurrency and fixed_max_roles
            for cpus, load, avail in MEASUREMENTS:
                cap = policy.target(cpus, load, avail)
                case = (fixed_concurrency, pinned_missions, fixed_max_roles, pinned_roles)
                assert f"{cap.concurrency} missions / {cap.max_roles} roles" in cap.reason
                assert cap.max_roles >= 1, (case, cpus, load, avail, cap)
                if fixed_max_roles:
                    assert cap.max_roles == pinned_roles, "the pin is honoured, not re-derived"
                if both_pinned:
                    # no managed cap: the pinned pair is returned untouched
                    assert cap.concurrency == pinned_missions, (case, cpus, cap)
                    continue
                assert cap.concurrency >= 1, (case, cpus, load, avail, cap)
                assert cap.concurrency <= cap.max_roles, (case, cpus, load, avail, cap)
                # the clamp only reduces the mission side, never raises it
                assert cap.concurrency == min(pinned_missions, cap.max_roles), (case, cpus, cap)


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
    assert new.max_roles == 5, "an explicit REWORK_MAX_ROLES is never re-derived or raised"
    # 8 idle CPUs would plan 8 missions, but the pinned role cap of 5 is the
    # resource that protects the box: the mission cap is clamped down to fit it.
    assert new.concurrency == 5, "never more mission slots than role slots, pinned or not"
    assert "5 missions / 5 roles" in new.reason


def test_a_clamped_pin_comes_back_on_the_next_measurement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M1: `REWORK_CONCURRENCY=8` pinned with the role cap managed. A starved first
    measurement clamps the pair to 1 / 1; if that clamped pair were written back
    over the pin, the box that pinned a mission cap would stop following the box
    until a daemon restart, and the 8 would never come back."""
    monkeypatch.setenv("REWORK_CONCURRENCY", "8")
    settings = CapacitySettings.from_env()
    assert settings.fixed_concurrency is True
    assert settings.fixed_max_roles is False

    policy = CapacityPolicy(
        concurrency=8,
        max_roles=8,
        fixed_concurrency=settings.fixed_concurrency,
        fixed_max_roles=settings.fixed_max_roles,
    )
    starved = policy.apply_initial(16, 0.0, 2 * GB)  # 16 idle CPUs, 2.0 GB free
    assert starved is not None
    assert (starved.concurrency, starved.max_roles) == (1, 1), "the clamp cut it to fit 1 role"
    assert (policy.concurrency, policy.max_roles) == (1, 1), "the LIVE caps are 1 / 1 now"

    # the box is idle and healthy again: the pin is still 8, not the clamped 1
    assert policy.observe(16, 0.0, None) is None, "first sighting of the new plan"
    back = policy.observe(16, 0.0, None)
    assert back is not None, "the second sighting must apply"
    assert (back.concurrency, back.max_roles) == (8, 16), "the pin comes back, 1 was not a pin"
    assert "8 missions / 16 roles" in back.reason
    assert (policy.concurrency, policy.max_roles) == (8, 16)

    # on an idle 8-core box the same pin is exactly 8 missions / 8 roles
    idle8 = policy.target(8, 0.0, None)
    assert (idle8.concurrency, idle8.max_roles) == (8, 8), "the pin is clamped to the role cap"


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


def test_a_box_already_at_its_caps_logs_nothing_on_apply(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """apply_now() on a box already at the constructed caps: _apply runs and the
    caps do not move, so the change-log guard must stay silent (observe_now()
    never reaches that guard, it returns before it)."""
    slots, limiter = PrioritySlots(8), RoleLimiter(8)
    dyn = DynamicCapacity(slots, limiter, settings=_settings(), measure_fn=lambda: (8, 0.0, None))
    with caplog.at_level(logging.INFO, logger="rework.capacity"):
        assert dyn.apply_now() is not None, "the first look is applied unconditionally"
    assert (slots.capacity, limiter.cap) == (8, 8)
    assert [r for r in caplog.records if r.name == "rework.capacity"] == [], "no change, no line"
    assert [r for r in caplog.records if r.name == "rework.capacity"] == [], "still nothing"


def test_an_intervening_different_plan_resets_the_hysteresis_streak() -> None:
    """A, B, A, A: B must clear the A streak, so the change lands on the FOURTH
    measurement. Without that reset B would be confirmed on its second sighting
    and applied on the second measurement of the run."""
    policy = CapacityPolicy(concurrency=2, max_roles=2)
    four = (4, 0.0, None)  # A: 4 idle CPUs plan 4 missions / 4 roles
    six = (6, 0.0, None)  # B: a third pair, neither the current caps nor A
    assert (plan(*four).concurrency, plan(*four).max_roles) == (4, 4)
    assert (plan(*six).concurrency, plan(*six).max_roles) == (6, 6)
    assert policy.observe(*four) is None, "first sighting of A: not confirmed yet"
    assert policy.observe(*six) is None, "B is a different target: the A streak dies"
    assert (policy.concurrency, policy.max_roles) == (2, 2), "B was not applied either"
    assert policy.observe(*four) is None, "A starts a NEW streak, one long again"
    assert policy.observe(*four) is not None, "two A in a row now: apply"
    assert (policy.concurrency, policy.max_roles) == (4, 4)


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
    assert dyn.policy.has_managed_cap is False
    assert dyn.enabled is False
    assert dyn.start() is None


def test_both_pinned_caps_are_a_total_no_op(caplog: pytest.LogCaptureFixture) -> None:
    """REWORK_CONCURRENCY=8 plus REWORK_MAX_ROLES=2 is honoured exactly as given:
    every entry point returns None, no clamp runs on that pair and the live caps
    stay 8 / 2 (the documented behaviour, see docs/ops.md)."""
    slots, limiter = PrioritySlots(8), RoleLimiter(2)
    calls = [(16, 0.0, 2 * GB), (16, 0.0, None), (16, 0.0, None)]
    dyn = DynamicCapacity(
        slots,
        limiter,
        settings=_settings(fixed_concurrency=True, fixed_max_roles=True),
        measure_fn=lambda: calls.pop(0),
    )
    assert dyn.policy.has_managed_cap is False
    assert dyn.enabled is False
    with caplog.at_level(logging.INFO, logger="rework.capacity"):
        assert dyn.policy.target(16, 0.0, 2 * GB) == Capacity(
            8, 2, "16 CPUs, load 0.0, 2.0 GB free: 8 missions / 2 roles"
        ), "the pinned pair is reported as given"
        assert dyn.apply_now() is None
        assert dyn.observe_now() is None
        assert dyn.start() is None
        assert dyn.task is None, "no watchdog on a fully pinned pair"
    assert (slots.capacity, limiter.cap) == (8, 2), "the owner gets exactly what they asked for"
    assert [r for r in caplog.records if r.name == "rework.capacity"] == []


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


async def test_one_raise_does_not_kill_the_watchdog(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unexpected raise inside a tick is logged once, at WARNING, with the
    traceback, and the loop keeps its interval: the next measurement is applied."""

    def flaky() -> tuple[int, float, int | None]:
        calls.append(len(calls) + 1)
        if len(calls) == 2:
            raise RuntimeError("boom: a probe that cannot answer")
        return (2, 0.0, None) if len(calls) >= 3 else (4, 0.0, None)

    calls: list[int] = []
    slots, limiter = PrioritySlots(1), RoleLimiter(1)
    dyn = DynamicCapacity(slots, limiter, settings=_settings(interval_s=0.01), measure_fn=flaky)
    with caplog.at_level(logging.WARNING, logger="rework.capacity"):
        task = dyn.start()  # call 1: applied at once, unconditionally
        assert task is not None
        assert (slots.capacity, limiter.cap) == (4, 4)
        try:
            for _ in range(200):
                if len(calls) >= 4:
                    break
                await asyncio.sleep(0.01)
            assert len(calls) >= 3, "the loop kept ticking after the raise"
            for _ in range(200):
                if (slots.capacity, limiter.cap) == (2, 2):
                    break
                await asyncio.sleep(0.01)
            assert not task.done(), "one raise must not end the watchdog"
            assert (slots.capacity, limiter.cap) == (2, 2), "the next measurement applied"
        finally:
            await dyn.stop()
    warnings = [
        r for r in caplog.records if r.name == "rework.capacity" and r.levelno == logging.WARNING
    ]
    assert len(warnings) == 1, f"one warning, not one per tick: {[r.message for r in warnings]}"
    assert "capacity update failed" in warnings[0].message
    assert "2 missions / 2 roles" in warnings[0].message or "4 missions / 4 roles" in (
        warnings[0].message
    )
    assert warnings[0].exc_info is not None, "the traceback goes with it"


async def test_a_persistent_failure_warns_once_not_once_per_tick(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Every tick fails: the loop stays alive and the log gets ONE warning, so a
    broken probe cannot bury the file at one record a minute."""

    calls: list[int] = []

    def always_boom() -> tuple[int, float, int | None]:
        calls.append(len(calls) + 1)
        raise RuntimeError("boom")

    slots, limiter = PrioritySlots(2), RoleLimiter(2)
    dyn = DynamicCapacity(
        slots, limiter, settings=_settings(interval_s=0.01), measure_fn=always_boom
    )
    with caplog.at_level(logging.WARNING, logger="rework.capacity"):
        task = dyn.start()
        assert task is not None
        try:
            for _ in range(200):
                if len(calls) >= 5:
                    break
                await asyncio.sleep(0.01)
            assert len(calls) >= 5, "the watchdog kept ticking through the failures"
            assert not task.done(), "a persistent raise must not end the watchdog"
        finally:
            await dyn.stop()
    assert (slots.capacity, limiter.cap) == (2, 2)
    warnings = [
        r for r in caplog.records if r.name == "rework.capacity" and r.levelno == logging.WARNING
    ]
    assert len(warnings) == 1, f"warned {len(warnings)} times for {len(calls)} failures"


async def test_a_failed_first_measurement_does_not_abort_startup(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """start() applies the first plan; if that measurement raises, the daemon
    still starts, the watchdog still runs and the caps stay as constructed."""

    def boom() -> tuple[int, float, int | None]:
        raise RuntimeError("boom")

    slots, limiter = PrioritySlots(3), RoleLimiter(3)
    dyn = DynamicCapacity(slots, limiter, settings=_settings(), measure_fn=boom)
    with caplog.at_level(logging.WARNING, logger="rework.capacity"):
        task = dyn.start()
    assert task is not None, "startup must not be aborted by the failed measurement"
    assert dyn.task is task
    assert not task.done()
    assert (slots.capacity, limiter.cap) == (3, 3), "caps stay as constructed"
    await dyn.stop()
    assert task.done()
    warnings = [r for r in caplog.records if r.name == "rework.capacity"]
    assert len(warnings) == 1, "one warning for the one failure"
    assert warnings[0].levelno == logging.WARNING
    assert warnings[0].exc_info is not None


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
