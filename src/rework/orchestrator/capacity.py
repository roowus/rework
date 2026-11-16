"""Runtime capacity: how many missions and roles this box can take RIGHT NOW.

The two caps the daemon enforces (missions: REWORK_CONCURRENCY, roles:
REWORK_MAX_ROLES) used to be resolved exactly once, at startup, so changing
them meant editing ~/.rework/rework.env and restarting `rework serve`. This
module re-measures the host every REWORK_CAPACITY_INTERVAL_S and moves both
caps without a restart and without dropping a running mission.

The rules (:func:`plan`), all derived from constants below:

- cpus below 1 never happen, load below 0 never happens;
- roles = cpus when the box is idle, halved (integer division, min 1) when the
  1 minute load average is above the CPU count; the LOAD rule alone never takes
  a multi-core box under MULTICORE_MIN_ROLES roles (one role per CPU is already
  the point);
- missions = max(CONCURRENCY_FLOOR, roles), then clamped so it is never MORE
  than roles: a mission slot with nothing to run is a wasted queue slot;
- available RAM, when /proc/meminfo says so, must be at least
  MIN_RAM_PER_ROLE_BYTES per planned role, and roles are cut one at a time
  until it is, down to 1. The RAM rule runs AFTER the load rule and is not
  bounded by MULTICORE_MIN_ROLES: plan(16, 0.0, 2 GB) is 1 mission / 1 role,
  because protecting the box beats using it. This is the rule that keeps the
  box out of swap, the 2026-11-13 failure mode
  (docs/ops.md#the-2026-11-13-incident).

1.5 GB is MIN_RAM_PER_ROLE_BYTES = 1_500_000_000 bytes: the DECIMAL gigabyte,
the same unit the log line prints ("9.1 GB free").

A change is applied only when the measured plan differs from the current caps
by at least one AND the same target was seen HYSTERESIS_MEASUREMENTS
measurements in a row, so one noisy `uv sync` on a 2-CPU box cannot flap the
caps. :class:`CapacityPolicy` holds that logic as pure, sync, sleep free code;
:class:`DynamicCapacity` is the thin asyncio half that samples and applies it.

PRECEDENCE (REWORK_CAPACITY, default auto):

- REWORK_CAPACITY=fixed: today's behaviour, exactly. No measurement, no
  watchdog, no log line, caps as constructed.
- REWORK_CAPACITY=auto (or unset, empty, or anything unrecognised): measure and
  manage, except for a cap the owner set EXPLICITLY: a non-empty
  REWORK_CONCURRENCY fixes the mission cap, a non-empty REWORK_MAX_ROLES fixes
  the role cap, and auto manages only the other one. Nothing else records
  whether an env var was set by the owner or derived from the host, so the
  check is a plain `os.environ.get(...)` on a non-empty value (see
  :meth:`CapacitySettings.from_env`).
- With BOTH caps explicit there is nothing for auto to manage
  (:attr:`CapacityPolicy.has_managed_cap` is False): rework honours the two
  pinned numbers exactly as given, measures nothing, runs no watchdog and
  therefore runs no clamp on that pair either. Mission slots are inert once the
  role cap binds, so the arithmetic difference costs the box nothing.

A pin is immutable once the policy is built: it lives in its own field
(`_pinned_concurrency` / `_pinned_max_roles`) and
:meth:`CapacityPolicy._remember` never writes it, so a clamp that cut the
mission cap for one measurement cannot come back as the pin on the next one.

On the caps that auto actually manages, the manager keeps the value the daemon
was CONSTRUCTED with only when that cap is pinned: `REWORK_CONCURRENCY=3` means
3, never a re-derivation. The one exception is the invariant above: a managed
mission cap is reduced to fit a pinned ROLE cap when the role cap is the
smaller of the two (roles are the resource that protects the box), and a pinned
mission cap is likewise cut back to a managed role cap. A pinned cap is never
raised to satisfy it.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime import cycle
    from rework.orchestrator.limits import PrioritySlots, RoleLimiter

log = logging.getLogger("rework.capacity")

# decimal gigabyte: one unit everywhere, so the log line and the RAM rule agree
GB = 1_000_000_000
# available RAM a single role's pytest/mypy/uv tree needs before the box swaps
MIN_RAM_PER_ROLE_BYTES = 1_500_000_000  # 1.5 GB
# floor of the mission cap (a daemon that runs no mission is useless)
CONCURRENCY_FLOOR = 1
# load1 above this multiple of the CPU count means the box is already busy
LOAD_HALVING_NOT_ABOVE_CPUS = 1.0
# a multi-core box never plans fewer roles than this, even under heavy load
MULTICORE_MIN_ROLES = 2
# a new plan must be seen this many measurements in a row before it is applied
HYSTERESIS_MEASUREMENTS = 2
# seconds between measurements (REWORK_CAPACITY_INTERVAL_S)
CAPACITY_INTERVAL_DEFAULT = 60.0
MEMINFO = "/proc/meminfo"

CAPACITY_AUTO = "auto"
CAPACITY_FIXED = "fixed"


@dataclass(frozen=True)
class Capacity:
    """One planned pair of caps, with the sentence that explains it."""

    concurrency: int
    max_roles: int
    reason: str


def _cpu_count() -> int:
    try:
        count = os.cpu_count()
    except (NotImplementedError, OSError):  # pragma: no cover - exotic platforms
        count = None
    return max(1, int(count or 1))


def available_bytes(meminfo: Path | str = MEMINFO) -> int | None:
    """MemAvailable in bytes from /proc/meminfo, or None where there is no such
    file (macOS) or it cannot be parsed. Never raises."""
    try:
        text = Path(meminfo).read_text()
    except (OSError, UnicodeDecodeError):
        return None
    for line in text.splitlines():
        if line.startswith("MemAvailable:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1]) * 1024  # kB -> bytes
            return None
    return None


def measure(meminfo: Path | str = MEMINFO) -> tuple[int, float, int | None]:
    """(cpu_count, load1, available_bytes) for this moment. Never raises: a box
    that cannot answer getloadavg (or has no /proc) reads as idle/unknown."""
    try:
        load1 = float(os.getloadavg()[0])
    except (OSError, AttributeError):
        load1 = 0.0
    return _cpu_count(), max(0.0, load1), available_bytes(meminfo)


def _reason(
    cpu_count: int, load1: float, avail_bytes: int | None, concurrency: int, max_roles: int
) -> str:
    parts = [f"{cpu_count} CPU" if cpu_count == 1 else f"{cpu_count} CPUs", f"load {load1:.1f}"]
    if avail_bytes is not None:
        parts.append(f"{avail_bytes / GB:.1f} GB free")
    return f"{', '.join(parts)}: {concurrency} missions / {max_roles} roles"


def plan(cpu_count: int, load1: float, avail_bytes: int | None) -> Capacity:
    """The caps this box can take, from pure numbers (no clock, no host probe)."""
    cpus = max(1, int(cpu_count))
    load = max(0.0, float(load1))
    roles = cpus
    if load > cpus * LOAD_HALVING_NOT_ABOVE_CPUS:
        roles = max(1, cpus // 2)  # busy: half the cores, never zero
    if cpus >= 2:
        roles = max(MULTICORE_MIN_ROLES, roles)
    if avail_bytes is not None:
        while roles > 1 and avail_bytes < MIN_RAM_PER_ROLE_BYTES * roles:
            roles -= 1  # not enough free RAM for that many roles: cut them
    concurrency = max(CONCURRENCY_FLOOR, roles)
    concurrency = min(concurrency, roles)  # never more mission slots than roles
    concurrency = max(1, concurrency)
    return Capacity(
        concurrency=concurrency,
        max_roles=roles,
        reason=_reason(cpus, load, avail_bytes, concurrency, roles),
    )


@dataclass(frozen=True)
class CapacitySettings:
    """The two capacity settings, read from the env exactly once at startup."""

    mode: str = CAPACITY_AUTO
    interval_s: float = CAPACITY_INTERVAL_DEFAULT
    fixed_concurrency: bool = False
    fixed_max_roles: bool = False

    @property
    def auto(self) -> bool:
        return self.mode == CAPACITY_AUTO

    @classmethod
    def from_env(cls) -> CapacitySettings:
        raw_mode = os.environ.get("REWORK_CAPACITY", "").strip().lower()
        # unknown/empty -> auto: a typo must never turn the guard off
        mode = CAPACITY_FIXED if raw_mode == CAPACITY_FIXED else CAPACITY_AUTO
        interval = _interval_from_env()
        return cls(
            mode=mode,
            interval_s=interval,
            # a non-empty env var is the only signal that the owner chose a cap
            fixed_concurrency=bool(os.environ.get("REWORK_CONCURRENCY")),
            fixed_max_roles=bool(os.environ.get("REWORK_MAX_ROLES")),
        )


def _interval_from_env() -> float:
    """REWORK_CAPACITY_INTERVAL_S, else 60; unparseable/non-positive -> 60."""
    raw = os.environ.get("REWORK_CAPACITY_INTERVAL_S")
    if not raw:
        return CAPACITY_INTERVAL_DEFAULT
    try:
        value = float(raw)
    except ValueError:
        return CAPACITY_INTERVAL_DEFAULT
    return value if value > 0 else CAPACITY_INTERVAL_DEFAULT


class CapacityPolicy:
    """When is a measured plan stable enough to apply? Pure and synchronous.

    The caller feeds each measurement in (:meth:`apply_initial`,
    :meth:`observe`) and gets back a :class:`Capacity` only when the caps should
    move, so hysteresis is unit-testable without sleeping a minute per tick.
    """

    def __init__(
        self,
        *,
        concurrency: int,
        max_roles: int,
        fixed_concurrency: bool = False,
        fixed_max_roles: bool = False,
        hysteresis: int = HYSTERESIS_MEASUREMENTS,
    ) -> None:
        # The pins are IMMUTABLE and live in their own fields: _remember only ever
        # tracks the live caps, so a clamped value can never be mistaken for the
        # pin on the next measurement. `concurrency` / `max_roles` are the caps in
        # force right now (they start at the constructed value).
        self._pinned_concurrency = max(1, int(concurrency)) if fixed_concurrency else None
        self._pinned_max_roles = max(1, int(max_roles)) if fixed_max_roles else None
        self.concurrency = max(1, int(concurrency))
        self.max_roles = max(1, int(max_roles))
        self.fixed_concurrency = fixed_concurrency
        self.fixed_max_roles = fixed_max_roles
        self.hysteresis = max(1, int(hysteresis))
        self._candidate: tuple[int, int] | None = None
        self._streak = 0

    @property
    def has_managed_cap(self) -> bool:
        """True when at least one cap is left for auto (and its clamp) to move.

        False means both caps are pinned: every entry point is a no-op and the
        pinned pair is honoured exactly as given, watchdog included."""
        return self._pinned_concurrency is None or self._pinned_max_roles is None

    def target(self, cpu_count: int, load1: float, avail_bytes: int | None) -> Capacity:
        """What the caps should be now, with the pinned cap(s) applied.

        The pin is read from the immutable pin fields, never from the live caps,
        so the result depends only on the constructor and the measurement."""
        measured = plan(cpu_count, load1, avail_bytes)
        concurrency = (
            self._pinned_concurrency
            if self._pinned_concurrency is not None
            else measured.concurrency
        )
        roles = self._pinned_max_roles if self._pinned_max_roles is not None else measured.max_roles
        # The clamp applies to the OUTPUT of the caps auto manages, and only there:
        # it reduces the mission cap to fit the role cap (roles are what protects
        # the box) and never raises a pinned number. It does not touch
        # _pinned_concurrency, so the pin survives: REWORK_MAX_ROLES=2 on 12 CPUs
        # is 2 missions / 2 roles, and a later idle measurement with the role cap
        # managed plans 8 again, not 1. With BOTH caps pinned there is nothing for
        # auto to manage, so the pair is returned exactly as given (the entry
        # points never even reach this method then).
        if self.has_managed_cap:
            concurrency = max(1, min(concurrency, roles))
        if (concurrency, roles) == (measured.concurrency, measured.max_roles):
            return measured
        cpus = max(1, int(cpu_count))
        return Capacity(concurrency, roles, _reason(cpus, load1, avail_bytes, concurrency, roles))

    def apply_initial(
        self, cpu_count: int, load1: float, avail_bytes: int | None
    ) -> Capacity | None:
        """The FIRST measurement is applied unconditionally, so a fresh daemon
        starts at the caps the box can take instead of the startup snapshot."""
        if not self.has_managed_cap:
            return None
        new = self.target(cpu_count, load1, avail_bytes)
        self._remember(new)
        return new

    def observe(self, cpu_count: int, load1: float, avail_bytes: int | None) -> Capacity | None:
        """A later measurement: apply it only when it has been stable for
        :attr:`hysteresis` measurements in a row. One spike changes nothing."""
        if not self.has_managed_cap:
            return None
        new = self.target(cpu_count, load1, avail_bytes)
        target = (new.concurrency, new.max_roles)
        current = (self.concurrency, self.max_roles)
        if target == current:
            self._candidate = None  # same caps: nothing pending any more
            self._streak = 0
            return None
        if target == self._candidate:
            self._streak += 1
        else:
            self._candidate = target
            self._streak = 1
        if self._streak < self.hysteresis:
            return None  # first sighting of a new plan: wait for confirmation
        self._remember(new)
        return new

    def _remember(self, new: Capacity) -> None:
        # Live caps only. The pins in _pinned_concurrency / _pinned_max_roles are
        # never written here: a clamp applied to this measurement must not become
        # the pin that feeds the next one.
        self.concurrency = new.concurrency
        self.max_roles = new.max_roles
        self._candidate = None
        self._streak = 0


class DynamicCapacity:
    """Keeps the daemon's two caps following the box, with no restart.

    Call :meth:`start` from inside the running loop (it applies the first plan
    at once and then ticks every `settings.interval_s`); the daemon cancels it
    through :meth:`stop` in its own `finally`, so neither the auto-update exit
    nor a CancelledError leaves a task behind. Shrinking never cancels work:
    :meth:`PrioritySlots.set_capacity` / :meth:`RoleLimiter.set_cap` only stop
    admitting new work while the running missions drain.
    """

    def __init__(
        self,
        slots: PrioritySlots,
        limiter: RoleLimiter,
        *,
        settings: CapacitySettings | None = None,
        measure_fn: Callable[[], tuple[int, float, int | None]] = measure,
    ) -> None:
        self.settings = settings if settings is not None else CapacitySettings.from_env()
        self._slots = slots
        self._limiter = limiter
        self._measure = measure_fn
        self.policy = CapacityPolicy(
            concurrency=slots.capacity,
            max_roles=limiter.cap,
            fixed_concurrency=self.settings.fixed_concurrency,
            fixed_max_roles=self.settings.fixed_max_roles,
        )
        self._task: asyncio.Task[None] | None = None
        self.last: Capacity | None = None
        self._warned = False

    @property
    def enabled(self) -> bool:
        """Auto, and at least one cap left for it to manage."""
        return self.settings.auto and self.policy.has_managed_cap

    @property
    def task(self) -> asyncio.Task[None] | None:
        return self._task

    def apply_now(self) -> Capacity | None:
        """Measure once and apply the first plan (unconditional)."""
        if not self.enabled:
            return None
        return self._apply(self.policy.apply_initial(*self._measure()))

    def observe_now(self) -> Capacity | None:
        """Measure once and apply the plan only if it is now confirmed."""
        if not self.enabled:
            return None
        return self._apply(self.policy.observe(*self._measure()))

    def _guarded(self, action: Callable[[], Capacity | None]) -> Capacity | None:
        """One measurement, made fatal proof: a raise inside the measurement OR
        inside applying it is logged at WARNING with its traceback, the caps
        stay where they were and the watchdog keeps its interval. A persistent
        failure warns ONCE, not once per tick, so it cannot bury the log."""
        try:
            result = action()
        except Exception:
            if not self._warned:
                self._warned = True
                log.warning(
                    "capacity update failed, keeping %d missions / %d roles and "
                    "retrying every %.0f s",
                    self._slots.capacity,
                    self._limiter.cap,
                    self.settings.interval_s,
                    exc_info=True,
                )
            return None
        self._warned = False
        return result

    def start(self) -> asyncio.Task[None] | None:
        """Apply the first plan and start the watchdog (needs a running loop)."""
        if not self.enabled:
            return None
        self._guarded(self.apply_now)  # a failed first look must not abort startup
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._watch(), name="rework-capacity")
        return self._task

    async def stop(self) -> None:
        """Cancel the watchdog and wait for it. Safe to call when never
        started, and safe to call from a `finally` while being cancelled."""
        task, self._task = self._task, None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass  # our own cancellation is already unwinding the caller

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(self.settings.interval_s)
            self._guarded(self.observe_now)

    def _apply(self, new: Capacity | None) -> Capacity | None:
        if new is None:
            return None
        before = (self._slots.capacity, self._limiter.cap)
        self._slots.set_capacity(new.concurrency)
        self._limiter.set_cap(new.max_roles)
        self.last = new
        if (new.concurrency, new.max_roles) != before:
            # ONE line per change, never per tick: the reason says why
            log.info(
                "capacity changed: %d missions / %d roles -> %d missions / %d roles (%s)",
                before[0],
                before[1],
                new.concurrency,
                new.max_roles,
                new.reason,
            )
        return new
