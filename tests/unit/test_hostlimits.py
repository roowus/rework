"""Host-aware defaults and per-command resource limits (docs/ops.md#rewvis).
Pure functions over injected probes: no real cgroups, no real systemd, no real
/proc/meminfo."""

from __future__ import annotations

import logging
import os
from pathlib import Path

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
)

ALL = ("nice", "ionice", "systemd-run")


def fake_which(present: tuple[str, ...]) -> object:
    return lambda name: f"/usr/bin/{name}" if name in present else None


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REWORK_MAX_ROLES", raising=False)
    monkeypatch.delenv("REWORK_CONCURRENCY", raising=False)
    monkeypatch.delenv("REWORK_ROLE_MEMORY_MAX", raising=False)
    reset_report_flag()


# -- command_prefix: the four branches --------------------------------------


def test_scope_branch_caps_memory_and_keeps_the_command_last() -> None:
    prefix = command_prefix(
        "uv run pytest -q",
        memory_max="4G",
        which=fake_which(ALL),  # type: ignore[arg-type]
        scope_available=lambda: True,
    )
    assert prefix[0] == "/usr/bin/systemd-run"
    assert "--user" in prefix and "--scope" in prefix
    assert "--property=MemoryMax=4294967296" in prefix
    assert "--nice=10" in prefix
    assert "--property=IOSchedulingPriority=7" in prefix
    assert prefix[-3:] == ["/bin/sh", "-c", "uv run pytest -q"]
    assert prefix[-1] == "uv run pytest -q", "the command is always the last element"


def test_scope_branch_needs_a_memory_cap_to_be_worth_it() -> None:
    """No MemoryMax, no scope: nice/ionice are enough and cost no extra process."""
    prefix = command_prefix(
        "mypy src",
        memory_max="",
        which=fake_which(ALL),  # type: ignore[arg-type]
        scope_available=lambda: True,
    )
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


def test_fallback_branch_nice_and_ionice_when_no_scope() -> None:
    prefix = command_prefix(
        "ruff check .",
        memory_max="4G",
        which=fake_which(("nice", "ionice")),  # type: ignore[arg-type]
        scope_available=lambda: False,
    )
    assert prefix == [
        "/usr/bin/nice",
        "-n",
        "10",
        "/usr/bin/ionice",
        "-c3",
        "-n7",
        "/bin/sh",
        "-c",
        "ruff check .",
    ]


def test_fallback_branch_ionice_alone() -> None:
    prefix = command_prefix(
        "git status",
        memory_max="1G",
        which=fake_which(("ionice",)),  # type: ignore[arg-type]
        scope_available=lambda: False,
    )
    assert prefix == ["/usr/bin/ionice", "-c3", "-n7", "/bin/sh", "-c", "git status"]


def test_bare_shell_when_nothing_is_available() -> None:
    prefix = command_prefix(
        "echo hi",
        memory_max="",
        which=fake_which(()),  # type: ignore[arg-type]
        scope_available=lambda: False,
    )
    assert prefix == ["/bin/sh", "-c", "echo hi"]


def test_unparseable_memory_max_falls_back_instead_of_crashing() -> None:
    prefix = command_prefix(
        "echo hi",
        memory_max="lots",
        which=fake_which(ALL),  # type: ignore[arg-type]
        scope_available=lambda: True,
    )
    assert not any("MemoryMax" in part for part in prefix)
    assert prefix[0] == "/usr/bin/nice", "an unreadable cap degrades to the fallback wrapper"
    assert prefix[-3:] == ["/bin/sh", "-c", "echo hi"]


# -- report_once ------------------------------------------------------------


def test_report_once_logs_exactly_one_record(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="rework.hostlimits")
    prefix = command_prefix(
        "echo hi",
        which=fake_which(("nice",)),
        scope_available=lambda: False,  # type: ignore[arg-type]
    )
    report_once(prefix, "4G")
    report_once(prefix, "4G")
    records = [r for r in caplog.records if r.name == "rework.hostlimits"]
    assert len(records) == 1, [r.getMessage() for r in records]
    message = records[0].getMessage()
    assert "nice -n 10" in message
    assert "MemoryMax" in message, "the operator must learn the cap is NOT in force"


def test_reset_report_flag_lets_it_report_again(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="rework.hostlimits")
    bare = ["/bin/sh", "-c", "true"]
    report_once(bare, "")
    reset_report_flag()
    report_once(bare, "")
    assert len([r for r in caplog.records if r.name == "rework.hostlimits"]) == 2


def test_report_once_names_the_scope_when_it_is_in_force(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="rework.hostlimits")
    prefix = command_prefix(
        "echo hi",
        memory_max="1G",
        which=fake_which(ALL),
        scope_available=lambda: True,  # type: ignore[arg-type]
    )
    report_once(prefix, "1G")
    message = caplog.records[0].getMessage()
    assert "systemd scope" in message and "1073741824" in message


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
