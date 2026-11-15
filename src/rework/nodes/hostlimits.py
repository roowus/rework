"""Host-aware defaults and per-command resource limits.

rework cannot overload its host (docs/ops.md#rewvis, 2026-11-13 incident): a
mission with a batch of parallel investigators once ran 6-7 roles whose pytest /
mypy / uv sync traffic swap-thrashed a 2-CPU box until it was OOM-killed and had
to be hard-rebooted. Two guards live here:

- sizing defaults derived from the host (CPU count, total RAM from
  /proc/meminfo) so a deployment needs no tuning to be safe (:func:`default_max_roles`,
  :func:`default_concurrency`, :func:`default_role_memory_max`);
- :func:`command_prefix`, which wraps a role's shell command in the first
  memory limit that works here — a systemd user scope, then a systemd system
  scope (what `rework.service` has: no user bus), then a `ulimit -v` in the
  wrapper shell — always deprioritized with `nice -n 10` / idle-class
  `ionice`. Pure functions over
  injectable probes so they are testable without touching the real host
  (tests/unit/test_hostlimits.py).
"""

from __future__ import annotations

import logging
import os
import shlex
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


# systemd probes run once per process (lru_cache) with a short timeout: a
# hanging bus must never stall the first role command of a mission.
SCOPE_PROBE_TIMEOUT = 3.0
# the system bus socket: present when `systemd-run --system` can work even
# though this process (rework.service) has no user bus
SYSTEM_BUS = Path("/run/systemd/private")


def _run_probe(argv: list[str]) -> bool:
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=SCOPE_PROBE_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


@lru_cache(maxsize=1)
def systemd_scope_available() -> bool:
    """Probe (once, cached) whether `systemd-run --user --scope` works here.

    The cheap case: an interactive/user-managed install has a user manager.
    A box without a user bus, without systemd, or with a transient-scope policy
    that refuses returns False. Found live on rewvis: `rework.service` is a
    SYSTEM service, so `--user` fails with "Failed to connect to bus: No medium
    found" (XDG_RUNTIME_DIR unset) even though lingering is enabled for ubuntu —
    which is why the system scope below exists."""
    exe = shutil.which("systemd-run")
    if not exe:
        return False
    return _run_probe([exe, "--user", "--scope", "-q", "true"])


def system_bus_present() -> bool:
    """True when there is a system bus for `systemd-run --system` to talk to."""
    try:
        return SYSTEM_BUS.exists()
    except OSError:  # pragma: no cover - exotic platforms
        return False


@lru_cache(maxsize=1)
def system_scope_available() -> bool:
    """Probe (once, cached) whether a SYSTEM transient scope works here.

    This is the path that works from a service like `rework.service`: it talks
    to the system bus, needs no user manager, and `--uid=<uid>` runs the command
    as the owner instead of root. Soft: no system bus (e.g. a container), no
    `systemd-run`, or a scope policy that refuses/prompts for auth -> False, and
    rework falls back to `ulimit -v`."""
    if not system_bus_present():
        return False  # no system bus to talk to
    exe = shutil.which("systemd-run")
    if not exe:
        return False
    return _run_probe([exe, "--system", "--scope", "-q", f"--uid={os.getuid()}", "true"])


_reported = False


def configured_memory_max() -> str:
    """REWORK_ROLE_MEMORY_MAX, or the ~60%-of-RAM default when unset."""
    raw = os.environ.get("REWORK_ROLE_MEMORY_MAX")
    return default_role_memory_max() if raw is None else raw


def ulimit_kb(memory_max: str) -> str:
    """REWORK_ROLE_MEMORY_MAX as the whole kB `ulimit -v` wants, or ''."""
    size = systemd_memory_max(memory_max)
    if not size:
        return ""
    kb = int(size) // 1024
    return str(kb) if kb > 0 else ""


def ulimit_wrapped(command: str, kb: str) -> str:
    """`command` behind a hard RLIMIT_AS of `kb` kB, as one /bin/sh program.

    `ulimit -v` is the memory limit that needs no systemd, no cgroup and no
    privileges, so it is the fallback that still holds where the 2026-11-13 OOM
    happened: a runaway pytest/mypy gets ENOMEM and dies instead of
    swap-thrashing the box. It only affects the wrapper's own children (the
    daemon never calls it), the command is shlex-quoted so quotes, `$`, `|`,
    `>` and spaces in it survive as ONE word (a pipeline cannot be `exec`ed
    directly, hence `exec /bin/sh -c`), and `exec` keeps the pid — so
    `start_new_session` + the process-group kill and the timeout still cover
    the whole tree. 2>/dev/null: a /bin/sh whose `ulimit` lacks `-v` must not
    print a warning on every command."""
    if not kb:
        return command
    return f"ulimit -v {kb} 2>/dev/null; exec /bin/sh -c {shlex.quote(command)}"


def human_size(size: str) -> str:
    """A byte count as the shortest exact rendering ('5G', '512M', '1234')."""
    if not size.isdigit():
        return size
    value = int(size)
    for unit, mult in (("T", 1024**4), ("G", 1024**3), ("M", 1024**2), ("K", 1024)):
        if value >= mult and value % mult == 0:
            return f"{value // mult}{unit}"
    return str(value)


def report_once(prefix: list[str], memory_max: str) -> None:
    """Say once per process, in ONE log record, what is actually in force for
    role commands — which memory limit and how they are deprioritized — so an
    operator can tell from the daemon log whether the cap really applies
    (`rework logs daemon -g 'role commands run'`)."""
    global _reported
    if _reported:
        return
    _reported = True
    shown = shlex.join(prefix)
    mem = systemd_memory_max(memory_max)
    exe = Path(prefix[0]).name if prefix else ""
    if exe == "systemd-run":
        kind = "user" if "--user" in prefix else "system"
        log.info(
            "role commands run in a systemd %s scope with MemoryMax=%s: %s",
            kind,
            human_size(mem),
            shown,
        )
    elif any("ulimit -v" in part for part in prefix):
        log.info(
            "role commands run with MemoryMax=%s (no systemd scope available: "
            "applying ulimit -v instead; sshd/tailscaled keep priority via "
            "nice/ionice): %s",
            human_size(mem),
            shown,
        )
    else:
        log.info(
            "role commands run deprioritized with NO memory limit "
            "(REWORK_ROLE_MEMORY_MAX unset/unparseable): %s",
            shown,
        )


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
    system_scope: Callable[[], bool] = system_scope_available,
) -> list[str]:
    """argv prefix that runs `command` (a shell string) under a resource chain:

    1. a systemd USER transient scope with `MemoryMax=` (interactive installs);
    2. a systemd SYSTEM transient scope with `MemoryMax=` (works from
       `rework.service`, which has no user bus);
    3. `nice -n 10` + `ionice -c3 -n7` in front of a `/bin/sh` that applies a
       hard `ulimit -v` before `exec`-ing the command — no systemd, no cgroup;
    4. nothing at all (bare `/bin/sh -c`, and report_once says so).

    A scope is only worth its extra process when there is a memory cap to
    enforce; without one the nice/ionice path is used. Every path uses the idle
    IO class. The returned prefix always ends with the shell invocation itself,
    so `["/bin/sh", "-c", command]` is its last two/three elements.
    """
    nice = which("nice")
    ionice = which("ionice")
    srun = which("systemd-run")
    mem = systemd_memory_max(memory_max) if memory_max else ""
    shell = ["/bin/sh", "-c", command]
    if mem and srun:
        props = [f"--property=MemoryMax={mem}", "--property=MemoryAccounting=yes", "--collect"]
        if nice:
            props += ["--nice=10"]
        if ionice:
            # idle IO class, same as the fallback below: a role's mypy must not
            # starve the box's own DB writes/journald. NOT a --property: a
            # transient SCOPE rejects IOSchedulingClass ("Unknown assignment",
            # exit 1 for every command; found on the first GitHub CI run), so
            # ionice runs INSIDE the scope instead.
            shell = [str(ionice), "-c3", "-n7", *shell]
        if scope_available():
            return [str(srun), "--user", "--scope", "--quiet", *props, *shell]
        if system_scope():
            return [
                str(srun),
                "--system",
                "--scope",
                "--quiet",
                f"--uid={os.getuid()}",
                *props,
                *shell,
            ]
    shell = ["/bin/sh", "-c", ulimit_wrapped(command, ulimit_kb(memory_max))]
    prefix: list[str] = []
    if nice:
        prefix = [str(nice), "-n", "10"]
        if ionice:
            prefix += [str(ionice), "-c3", "-n7"]
    elif ionice:
        # ionice alone still needs a shell to run the command
        prefix = [str(ionice), "-c3", "-n7"]
    return [*prefix, *shell]
