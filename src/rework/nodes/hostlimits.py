"""Host-aware defaults and per-command resource limits.

rework cannot overload its host (docs/ops.md#rewvis, 2026-11-13 incident): a
mission with a batch of parallel investigators once ran 6-7 roles whose pytest /
mypy / uv sync traffic swap-thrashed a 2-CPU box until it was OOM-killed and had
to be hard-rebooted. Two guards live here:

- sizing defaults derived from the host (CPU count, total RAM from
  /proc/meminfo) so a deployment needs no tuning to be safe (:func:`default_max_roles`,
  :func:`default_concurrency`, :func:`default_role_memory_max`);
- :func:`command_prefix`, which wraps a role's shell command in `nice` /
  `ionice` / a systemd transient scope with `MemoryMax=`. Pure functions over
  injectable probes so they are testable without touching the real host
  (tests/unit/test_hostlimits.py).
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path

log = logging.getLogger("rework.hostlimits")

# default share of host RAM a single role's command tree may use before the
# kernel OOM-kills it (alone) instead of the whole box going down
ROLE_MEMORY_SHARE = 0.6


def cpu_count() -> int:
    """Host CPU count, never below 1."""
    import os

    try:
        count = os.cpu_count()
    except (NotImplementedError, OSError):  # pragma: no cover - exotic platforms
        count = None
    return max(1, int(count or 1))


def default_max_roles() -> int:
    """REWORK_MAX_ROLES default: one role per CPU, at least one."""
    return cpu_count()


def default_concurrency() -> int:
    """REWORK_CONCURRENCY default: missions are mostly idle waiting on models,
    so allow a few, but never more than the box can actually run."""
    return max(1, min(cpu_count(), 4))


def total_memory_bytes(meminfo: Path | str = "/proc/meminfo") -> int | None:
    """Total RAM in bytes from /proc/meminfo, or None where it does not exist
    (macOS: the caller keeps the explicit default)."""
    try:
        text = Path(meminfo).read_text()
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("MemTotal:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1]) * 1024  # kB -> bytes
    return None


def parse_size(value: str) -> int | None:
    """'4G' / '512M' / '60%' / '1073741824' -> bytes, or None if unparseable.

    A percentage means that share of total host RAM; it is resolved here so the
    value handed to systemd is always an absolute byte count."""
    text = value.strip().upper()
    if not text:
        return None
    if text.endswith("%"):
        try:
            pct = float(text[:-1])
        except ValueError:
            return None
        total = total_memory_bytes()
        if total is None or pct <= 0:
            return None
        return int(total * pct / 100.0)
    units = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4, "B": 1}
    mult = 1
    if text[-1] in units:
        mult = units[text[-1]]
        text = text[:-1]
        if text.endswith("I"):  # "Gi" style
            text = text[:-1]
    try:
        amount = float(text)
    except ValueError:
        return None
    if amount <= 0:
        return None
    return int(amount * mult)


def systemd_memory_max(value: str) -> str:
    """Normalize a REWORK_ROLE_MEMORY_MAX value to a systemd MemoryMax string
    ('4123168604') or '' when it cannot be understood (then no cap is set)."""
    raw = value.strip()
    if not raw:
        return ""
    if raw.isdigit():
        return raw  # already bytes
    size = parse_size(raw)
    return str(size) if size else ""


def default_role_memory_max() -> str:
    """REWORK_ROLE_MEMORY_MAX default: ~60% of total RAM, blank if unknown."""
    total = total_memory_bytes()
    if not total:
        return ""
    return str(int(total * ROLE_MEMORY_SHARE))


@lru_cache(maxsize=1)
def systemd_scope_available() -> bool:
    """Probe (once, cached) whether `systemd-run --user --scope` works here.
    A box without a user manager, without systemd, or with a transient-scope
    policy that refuses returns False and rework falls back to nice/ionice."""
    exe = shutil.which("systemd-run")
    if not exe:
        return False
    try:
        proc = subprocess.run(
            [exe, "--user", "--scope", "-q", "true"],
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


_reported = False


def configured_memory_max() -> str:
    """REWORK_ROLE_MEMORY_MAX, or the ~60%-of-RAM default when unset."""
    import os

    raw = os.environ.get("REWORK_ROLE_MEMORY_MAX")
    return default_role_memory_max() if raw is None else raw


def report_once(prefix: list[str], memory_max: str) -> None:
    """Say once per process how role commands are wrapped (and which part of the
    wrapper could not be applied), so an operator can tell from the daemon log
    whether the memory cap is actually in force."""
    import shlex

    global _reported
    if _reported:
        return
    _reported = True
    shown = shlex.join(prefix)
    exe = Path(prefix[0]).name if prefix else ""
    if exe == "systemd-run":
        cap = next((p for p in prefix if p.startswith("--property=MemoryMax=")), "")
        log.info("role commands run in a systemd scope (%s): %s", cap or "no MemoryMax", shown)
    elif exe in {"nice", "ionice"}:
        wanted = systemd_memory_max(memory_max)
        log.info(
            "role commands run deprioritized; no usable systemd --user scope so "
            "MemoryMax%s was not applied: %s",
            f"={wanted}" if wanted else "",
            shown,
        )
    else:
        log.info("role commands run with no nice/ionice/systemd wrapper: %s", shown)


def reset_report_flag() -> None:
    """Test hook: forget that the wrapper was already reported."""
    global _reported
    _reported = False


def command_prefix(
    command: str,
    *,
    memory_max: str = "",
    which: Callable[[str], str | None] = shutil.which,
    scope_available: Callable[[], bool] = systemd_scope_available,
) -> list[str]:
    """argv prefix that runs `command` (a shell string, quoted with shlex) under
    nice/ionice and, where possible, inside a transient systemd scope with a
    memory cap.

    Falls back cleanly: each wrapper is added only when its binary exists (and
    the scope only when the probe succeeds AND there is a memory cap to enforce,
    since that is the reason the scope exists), so the returned prefix is always
    runnable. `["/bin/sh", "-c", command]` is the last two elements, i.e. the
    prefix ends with the shell invocation itself.
    """
    shell = ["/bin/sh", "-c", command]
    nice = which("nice")
    ionice = which("ionice")
    prefix: list[str] = []
    mem = systemd_memory_max(memory_max) if memory_max else ""
    if mem and scope_available():
        srun = which("systemd-run")
        if srun:
            prefix = [str(srun), "--user", "--scope", "--quiet", "--collect"]
            if mem:
                prefix += [f"--property=MemoryMax={mem}"]
            if nice:
                prefix += ["--nice=10"]
            if ionice:
                # best-effort IO class, lowest priority: a role's mypy must not
                # starve the box's own logging/DB writes
                prefix += ["--property=IOSchedulingClass=best-effort"]
                prefix += ["--property=IOSchedulingPriority=7"]
            return [*prefix, *shell]
    if nice:
        prefix = [str(nice), "-n", "10"]
        if ionice:
            prefix += [str(ionice), "-c3", "-n7"]
        return [*prefix, *shell]
    if ionice:
        # ionice alone still needs a shell to run the command
        return [str(ionice), "-c3", "-n7", *shell]
    return shell
