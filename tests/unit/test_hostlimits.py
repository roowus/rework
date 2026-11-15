"""Host-aware defaults and per-command resource limits (docs/ops.md#rewvis).
Pure functions over injected probes: no real cgroups, no real systemd, no real
/proc/meminfo."""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from rework import config as config_mod
from rework.nodes import hostlimits
from rework.nodes.hostlimits import (
    command_prefix,
    default_concurrency,
    default_max_roles,
    default_role_memory_max,
    parse_size,
    report_once,
    reset_report_flag,
    systemd_memory_max,
    total_memory_bytes,
    ulimit_kb,
)

ALL = ("nice", "ionice", "systemd-run")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REWORK_MAX_ROLES", raising=False)
    monkeypatch.delenv("REWORK_CONCURRENCY", raising=False)
    monkeypatch.delenv("REWORK_ROLE_MEMORY_MAX", raising=False)
    reset_report_flag()


# -- command_prefix: the chain, first working mechanism wins ---------------


def fake_which(present: tuple[str, ...]) -> Callable[[str], str | None]:
    return lambda name: f"/usr/bin/{name}" if name in present else None


def wrap(
    command: str = "uv run pytest -q",
    *,
    memory_max: str = "4G",
    present: tuple[str, ...] = ALL,
    user: bool = True,
    system: bool = True,
) -> list[str]:
    """command_prefix over injected probes only: no systemd, no cgroups."""
    return command_prefix(
        command,
        memory_max=memory_max,
        which=fake_which(present),
        scope_available=lambda: user,
        system_scope=lambda: system,
    )


def test_user_scope_is_the_first_mechanism_when_available() -> None:
    prefix = wrap()
    assert prefix[0] == "/usr/bin/systemd-run"
    assert "--user" in prefix and "--scope" in prefix
    assert "--system" not in prefix, "the user scope short-circuits the chain"
    assert prefix.count("/usr/bin/systemd-run") == 1
    assert "--property=MemoryMax=4294967296" in prefix
    assert "--property=MemoryAccounting=yes" in prefix
    assert "--collect" in prefix and "--quiet" in prefix
    assert "--nice=10" in prefix
    assert "--property=IOSchedulingClass=idle" in prefix
    assert prefix[-3:] == ["/bin/sh", "-c", "uv run pytest -q"]
    assert prefix[-1] == "uv run pytest -q", "the command is always the last element"


def test_user_scope_is_not_probed_further_when_it_works() -> None:
    """Short-circuit: the system probe must not run once the user scope works."""
    calls: list[str] = []

    def user() -> bool:
        calls.append("user")
        return True

    def system() -> bool:
        calls.append("system")
        return True

    command_prefix(
        "true",
        memory_max="1G",
        which=fake_which(ALL),  # type: ignore[arg-type]
        scope_available=user,
        system_scope=system,
    )
    assert calls == ["user"]


def test_system_scope_is_used_when_the_user_scope_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rewvis case: rework.service has no user bus (`systemd-run --user`
    fails with "No medium found"), so the system scope carries the cap."""
    monkeypatch.setattr(hostlimits.os, "getuid", lambda: 1000)
    prefix = wrap(user=False, system=True)
    assert prefix[0] == "/usr/bin/systemd-run"
    assert "--system" in prefix and "--user" not in prefix
    assert "--uid=1000" in prefix, "the command still runs as the owner, not root"
    assert "--property=MemoryMax=4294967296" in prefix
    assert "--property=MemoryAccounting=yes" in prefix
    assert "--collect" in prefix and "--quiet" in prefix
    assert "--nice=10" in prefix
    assert "--property=IOSchedulingClass=idle" in prefix
    assert prefix[-3:] == ["/bin/sh", "-c", "uv run pytest -q"]


def test_scope_needs_a_memory_cap_to_be_worth_it() -> None:
    """No MemoryMax, no scope: nice/ionice are enough and cost no extra process."""
    prefix = wrap("mypy src", memory_max="")
    assert "systemd-run" not in prefix[0]
    assert prefix == [
        "/usr/bin/nice",
        "-n",
        "10",
        "/usr/bin/ionice",
        "-c3",
        "-n7",
        "/bin/sh",
        "-c",
        "mypy src",
    ]


def test_every_systemd_probe_failing_lands_on_the_ulimit_wrapper() -> None:
    prefix = wrap(user=False, system=False, memory_max="1G")
    assert "systemd-run" not in " ".join(prefix)
    assert prefix[:6] == ["/usr/bin/nice", "-n", "10", "/usr/bin/ionice", "-c3", "-n7"]
    assert prefix[-3:-1] == ["/bin/sh", "-c"]
    assert prefix[-1].startswith("ulimit -v 1048576 2>/dev/null; exec /bin/sh -c ")


def test_ulimit_wrapper_argv_is_one_quoted_program() -> None:
    """A command with quotes, `$` and spaces must survive as ONE argument."""
    command = """python3 -c 'print("a  b", "$HOME", `id -un`)' | grep "it's" """
    prefix = wrap(command, memory_max="2G", user=False, system=False)
    assert prefix[-2] == "-c"
    program = prefix[-1]
    assert program == f"ulimit -v 2097152 2>/dev/null; exec /bin/sh -c {shlex.quote(command)}"
    assert "exec /bin/sh -c " in program, "the command is exec'ed as one word"
    assert len(prefix) == 9, f"nothing may be split off the command: {prefix}"


def test_ulimit_wrapper_really_runs_the_command_unchanged(tmp_path: Path) -> None:
    """End-to-end through a real /bin/sh (no systemd involved): the ulimit
    prefix must not change what the command sees."""
    out = tmp_path / "out.txt"
    command = f"""printf '%s|' "a  b" '$HOME' "it's" > {out}"""
    prefix = wrap(command, memory_max="4G", present=("nice",), user=False, system=False)
    proc = subprocess.run(prefix, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert out.read_text() == "a  b|$HOME|it's|"


def test_ulimit_wrapper_actually_bounds_the_child(tmp_path: Path) -> None:
    """The cap is not decorative: a child that asks for more than the RLIMIT_AS
    dies instead of eating the box."""
    prefix = wrap(
        "python3 -c 'import sys; sys.exit(0 if (lambda a: a)(bytearray(4 * 1024**3)) else 1)'",
        memory_max="256M",
        user=False,
        system=False,
    )
    proc = subprocess.run(prefix, capture_output=True, text=True, timeout=120)
    assert proc.returncode != 0, "a 4 GiB allocation under a 256M ulimit must fail"


def test_fallback_branch_ionice_alone() -> None:
    prefix = wrap("git status", memory_max="1G", present=("ionice",), user=False, system=False)
    assert prefix == [
        "/usr/bin/ionice",
        "-c3",
        "-n7",
        "/bin/sh",
        "-c",
        "ulimit -v 1048576 2>/dev/null; exec /bin/sh -c 'git status'",
    ]


def test_bare_shell_when_nothing_is_available() -> None:
    prefix = wrap("echo hi", memory_max="", present=(), user=False, system=False)
    assert prefix == ["/bin/sh", "-c", "echo hi"]


def test_unparseable_memory_max_falls_back_instead_of_crashing() -> None:
    prefix = wrap("echo hi", memory_max="lots")
    assert not any("MemoryMax" in part for part in prefix)
    assert not any("ulimit" in part for part in prefix), "an unreadable cap sets no cap"
    assert prefix[0] == "/usr/bin/nice", "an unreadable cap degrades to the fallback wrapper"
    assert prefix[-3:] == ["/bin/sh", "-c", "echo hi"]


def test_every_path_uses_the_idle_io_class() -> None:
    """Consistency (audit finding): a scope used best-effort/7 while the
    fallback used idle/3. Both are idle now."""
    for prefix in (
        wrap(user=True),
        wrap(user=False, system=True),
        wrap(user=False, system=False),
    ):
        assert "--property=IOSchedulingPriority=7" not in prefix
        assert "--property=IOSchedulingClass=best-effort" not in prefix
    assert "--property=IOSchedulingClass=idle" in wrap(user=True)
    assert "--property=IOSchedulingClass=idle" in wrap(user=False, system=True)
    fallback = wrap(user=False, system=False)
    assert fallback[3:6] == ["/usr/bin/ionice", "-c3", "-n7"]


# -- the scope probes -------------------------------------------------------


def test_scope_probes_are_cached_once_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], float | None]] = []

    def fake_run(argv: list[str], **kwargs: float) -> object:
        calls.append((list(argv), kwargs.get("timeout")))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(hostlimits.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(hostlimits, "system_bus_present", lambda: True)
    monkeypatch.setattr(hostlimits.subprocess, "run", fake_run)
    hostlimits.systemd_scope_available.cache_clear()
    hostlimits.system_scope_available.cache_clear()
    try:
        assert hostlimits.systemd_scope_available() is True
        assert hostlimits.systemd_scope_available() is True
        assert hostlimits.system_scope_available() is True
        assert hostlimits.system_scope_available() is True
        assert len(calls) == 2, "each probe runs once, then is cached"
        assert calls[0][0][1:3] == ["--user", "--scope"]
        assert "--system" in calls[1][0] and f"--uid={os.getuid()}" in calls[1][0]
        assert all(timeout is not None and timeout <= 3.0 for _, timeout in calls)
    finally:
        hostlimits.systemd_scope_available.cache_clear()
        hostlimits.system_scope_available.cache_clear()


def test_system_scope_is_not_attempted_without_a_system_bus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(hostlimits.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(hostlimits, "system_bus_present", lambda: False)
    monkeypatch.setattr(
        hostlimits.subprocess,
        "run",
        lambda *a, **k: pytest.fail("must not shell out without a system bus"),
    )
    hostlimits.system_scope_available.cache_clear()
    try:
        assert hostlimits.system_scope_available() is False
    finally:
        hostlimits.system_scope_available.cache_clear()


def test_probe_failure_and_timeout_fail_soft(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> object:
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(hostlimits.shutil, "which", lambda name: "/usr/bin/systemd-run")
    monkeypatch.setattr(hostlimits, "system_bus_present", lambda: True)
    monkeypatch.setattr(hostlimits.subprocess, "run", refuse)
    hostlimits.systemd_scope_available.cache_clear()
    hostlimits.system_scope_available.cache_clear()
    try:
        assert hostlimits.systemd_scope_available() is False
        assert hostlimits.system_scope_available() is False

        def boom(*args: object, **kwargs: object) -> object:
            raise subprocess.TimeoutExpired("systemd-run", 3.0)

        monkeypatch.setattr(hostlimits.subprocess, "run", boom)
        hostlimits.systemd_scope_available.cache_clear()
        assert hostlimits.systemd_scope_available() is False
    finally:
        hostlimits.systemd_scope_available.cache_clear()
        hostlimits.system_scope_available.cache_clear()


# -- report_once: exactly one record, saying what is actually in force ------


def _messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "rework.hostlimits"]


def test_user_scope_is_reported_as_in_force(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="rework.hostlimits")
    report_once(wrap(memory_max="5G"), "5G")
    (message,) = _messages(caplog)
    assert message.startswith("role commands run in a systemd user scope with MemoryMax=5G")


def test_system_scope_is_reported_as_in_force(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="rework.hostlimits")
    report_once(wrap("echo hi", memory_max="5G", user=False, system=True), "5G")
    (message,) = _messages(caplog)
    assert message.startswith("role commands run in a systemd system scope with MemoryMax=5G")


def test_ulimit_fallback_says_the_cap_is_in_force_without_systemd(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="rework.hostlimits")
    report_once(wrap("echo hi", memory_max="5G", user=False, system=False), "5G")
    (message,) = _messages(caplog)
    assert message.startswith("role commands run with MemoryMax=5G (no systemd scope available")
    assert "applying ulimit -v instead" in message
    assert "nice/ionice" in message
    assert "ulimit -v 5242880" in message, "the operator can see the exact argv"


@pytest.mark.parametrize("memory_max", ["", "unparseable"])
def test_no_cap_is_reported_as_no_cap(caplog: pytest.LogCaptureFixture, memory_max: str) -> None:
    caplog.set_level(logging.INFO, logger="rework.hostlimits")
    report_once(wrap("echo hi", memory_max=memory_max, user=False, system=False), memory_max)
    (message,) = _messages(caplog)
    assert "NO memory limit" in message
    assert "REWORK_ROLE_MEMORY_MAX unset/unparseable" in message


def test_report_once_logs_exactly_one_record(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="rework.hostlimits")
    prefix = wrap("echo hi", memory_max="4G", present=("nice",), user=False, system=False)
    report_once(prefix, "4G")
    report_once(prefix, "4G")
    reset_report_flag()
    assert len(_messages(caplog)) == 1, _messages(caplog)
    report_once(prefix, "4G")
    assert len(_messages(caplog)) == 2, "reset_report_flag is the only way to log twice"


def test_reset_report_flag_lets_it_report_again(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="rework.hostlimits")
    bare = ["/bin/sh", "-c", "true"]
    report_once(bare, "")
    reset_report_flag()
    report_once(bare, "")
    assert len(_messages(caplog)) == 2


# -- sizes ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("4G", 4 * 1024**3),
        ("512M", 512 * 1024**2),
        ("2g", 2 * 1024**3),
        ("64K", 64 * 1024),
        ("12345", 12345),
        ("1.5G", int(1.5 * 1024**3)),
        ("0", None),
        ("", None),
        ("garbage", None),
        ("4X", None),
    ],
)
def test_parse_size(text: str, expected: int | None) -> None:
    assert parse_size(text) == expected


def test_parse_size_percentage_is_a_share_of_host_ram(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:       8388608 kB\nMemFree:  1 kB\n")
    monkeypatch.setattr(hostlimits, "total_memory_bytes", lambda: total_memory_bytes(meminfo))
    assert parse_size("60%") == int(8 * 1024**3 * 0.6)
    assert parse_size("100%") == 8 * 1024**3
    assert parse_size("0%") is None


def test_total_memory_bytes_reads_memtotal_in_bytes(tmp_path: Path) -> None:
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(
        "MemTotal:       12257524 kB\nMemFree:         1234567 kB\nSwapTotal:       4194300 kB\n"
    )
    assert total_memory_bytes(meminfo) == 12257524 * 1024


def test_total_memory_bytes_is_none_without_proc(tmp_path: Path) -> None:
    assert total_memory_bytes(tmp_path / "no-such-meminfo") is None


def test_default_role_memory_max_is_about_sixty_percent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(hostlimits, "total_memory_bytes", lambda: 10 * 1024**3)
    assert default_role_memory_max() == str(int(10 * 1024**3 * 0.6))
    assert hostlimits.ROLE_MEMORY_SHARE == pytest.approx(0.6)
    monkeypatch.setattr(hostlimits, "total_memory_bytes", lambda: None)
    assert default_role_memory_max() == "", "unknown RAM means no cap, never a guess"


@pytest.mark.parametrize(
    ("value", "expected"), [("4G", "4194304"), ("512M", "524288"), ("", ""), ("junk", "")]
)
def test_ulimit_kb_is_whole_kilobytes(value: str, expected: str) -> None:
    assert ulimit_kb(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [("4G", str(4 * 1024**3)), ("1073741824", "1073741824"), ("", ""), ("junk", "")],
)
def test_systemd_memory_max_normalizes_to_bytes(value: str, expected: str) -> None:
    assert systemd_memory_max(value) == expected


# -- host-aware config defaults --------------------------------------------


def test_default_max_roles_is_one_per_cpu(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hostlimits, "cpu_count", lambda: 2)
    assert default_max_roles() == 2
    monkeypatch.setattr(hostlimits, "cpu_count", lambda: 16)
    assert default_max_roles() == 16


def test_default_concurrency_caps_at_four(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hostlimits, "cpu_count", lambda: 1)
    assert default_concurrency() == 1
    monkeypatch.setattr(hostlimits, "cpu_count", lambda: 2)
    assert default_concurrency() == 2
    monkeypatch.setattr(hostlimits, "cpu_count", lambda: 32)
    assert default_concurrency() == 4


def test_cpu_count_is_never_below_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "cpu_count", lambda: None)
    assert hostlimits.cpu_count() == 1


def test_env_wins_for_max_roles_and_concurrency(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hostlimits, "cpu_count", lambda: 16)
    monkeypatch.setenv("REWORK_MAX_ROLES", "3")
    monkeypatch.setenv("REWORK_CONCURRENCY", "1")
    assert config_mod._max_roles() == 3
    assert config_mod._concurrency() == 1


def test_host_aware_defaults_when_env_is_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hostlimits, "cpu_count", lambda: 12)
    assert config_mod._max_roles() == 12
    assert config_mod._concurrency() == 4


def test_bad_env_values_fall_back_to_the_host_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hostlimits, "cpu_count", lambda: 2)
    monkeypatch.setenv("REWORK_MAX_ROLES", "many")
    monkeypatch.setenv("REWORK_CONCURRENCY", "lots")
    assert config_mod._max_roles() == 2
    assert config_mod._concurrency() == 2


def test_sqlite_deployments_keep_concurrency_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hostlimits, "cpu_count", lambda: 8)
    assert config_mod._concurrency(sqlite_default=1) == 1
    monkeypatch.setenv("REWORK_CONCURRENCY", "3")
    assert config_mod._concurrency(sqlite_default=1) == 3, "the env still wins"


def test_max_roles_on_the_config_object(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hostlimits, "cpu_count", lambda: 2)
    cfg = config_mod.Config(db="/tmp/x.sqlite3", checkpoints_db="/tmp/c.sqlite3", max_roles=6)
    assert config_mod.max_roles(cfg) == 6
    unset = config_mod.Config(db="/tmp/x.sqlite3", checkpoints_db="/tmp/c.sqlite3")
    assert config_mod.max_roles(unset) == 2, "0 means 'derive from the host'"


# -- wiring: run_command really executes the wrapper -----------------------


async def test_run_command_executes_the_ulimit_wrapper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The chain is not just a function nobody calls: LocalExecutor's
    run_command spawns the prefix, and the child really carries the RLIMIT_AS.
    Both systemd probes are injected as unavailable, so this is the rewvis path
    without touching systemd."""
    from functools import partial

    from rework.nodes.tools import LocalExecutor

    monkeypatch.setenv("REWORK_ROLE_MEMORY_MAX", "256M")
    executor = LocalExecutor(tmp_path)
    probe = {"command": "grep -i 'address space' /proc/self/limits"}

    monkeypatch.setattr(
        hostlimits,
        "command_prefix",
        partial(
            hostlimits.command_prefix,
            scope_available=lambda: False,
            system_scope=lambda: False,
        ),
    )
    capped = await executor.execute("run_command", probe)
    assert capped.startswith("(exit 0)"), capped
    assert "268435456" in capped, f"the child must run under ulimit -v 262144 kB: {capped}"

    # negative control: with the wrapper replaced by a plain shell, the very
    # same probe reads "unlimited" — so the assertion above is not vacuous
    monkeypatch.setattr(
        hostlimits, "command_prefix", lambda command, **_: ["/bin/sh", "-c", command]
    )
    assert "unlimited" in await executor.execute("run_command", probe)
