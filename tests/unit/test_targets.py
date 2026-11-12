"""Named connection targets (~/.rework/targets.toml): parsing, selection
precedence and the setdefault-not-clobber apply (previews and the rewvis
systemd unit depend on explicit env winning over a target)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from rework import targets as targets_mod
from rework.targets import Target, TargetsFile, load_targets_file, resolve_target

FULL = """\
default = "rewvis"

[targets.rewvis]
db_env = "REWORK_TEST_REWVIS_DB"
daemon_url = "http://100.107.125.118:8941"
ssh = "ubuntu@100.107.125.118"
library_dir = "/home/ubuntu/.rework/library"

[targets.sandbox]
db = "sqlite:////tmp/rework-sandbox.db"
daemon_url = "http://127.0.0.1:8941"
"""


@pytest.fixture()
def targets_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "targets.toml"
    path.write_text(FULL)
    monkeypatch.setattr(targets_mod, "TARGETS_PATH", path)
    monkeypatch.setenv("REWORK_TEST_REWVIS_DB", "postgresql://db/rewvis")
    return path


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "REWORK_DB",
        "REWORK_DAEMON_URL",
        "REWORK_LIBRARY_DIR",
        "REWORK_TARGET_NAME",
        "REWORK_TARGET",
    ):
        monkeypatch.delenv(name, raising=False)


def test_parse_resolves_db_env_and_keeps_optionals(targets_file: Path) -> None:
    file = load_targets_file(targets_file)
    assert file.default == "rewvis"
    assert set(file.targets) == {"rewvis", "sandbox"}
    rewvis = file.targets["rewvis"]
    assert rewvis.db == "postgresql://db/rewvis"  # db_env looked up in the env
    assert rewvis.daemon_url == "http://100.107.125.118:8941"
    assert rewvis.ssh == "ubuntu@100.107.125.118"
    assert rewvis.library_dir == "/home/ubuntu/.rework/library"
    assert file.targets["sandbox"].db == "sqlite:////tmp/rework-sandbox.db"


def test_missing_file_means_no_targets(tmp_path: Path) -> None:
    file = load_targets_file(tmp_path / "nope.toml")
    assert file == TargetsFile()
    assert file.resolve_name() is None


def test_malformed_file_is_no_targets(tmp_path: Path) -> None:
    path = tmp_path / "targets.toml"
    path.write_text("this is not = [ toml")
    assert load_targets_file(path) == TargetsFile()


def test_incomplete_entries_are_skipped(tmp_path: Path) -> None:
    path = tmp_path / "targets.toml"
    path.write_text(
        '[targets.ok]\ndb = "sqlite:///x.db"\ndaemon_url = "http://h:1"\n'
        '[targets.no_daemon]\ndb = "sqlite:///y.db"\n'
    )
    file = load_targets_file(path)
    assert set(file.targets) == {"ok"}


def test_flag_beats_env_beats_default(targets_file: Path) -> None:
    file = load_targets_file(targets_file)
    assert file.resolve_name(flag="sandbox", env_var="rewvis") == "sandbox"
    assert file.resolve_name(env_var="rewvis") == "rewvis"
    assert file.resolve_name() == "rewvis"  # the file's default


def test_unknown_flag_raises_unknown_default_falls_back(targets_file: Path) -> None:
    file = load_targets_file(targets_file)
    with pytest.raises(KeyError):
        file.resolve_name(flag="nope")
    path = targets_file.with_name("bad-default.toml")
    path.write_text('default = "ghost"\n[targets.rewvis]\ndb = "sqlite:///x"\ndaemon_url = "http://h"\n')
    bad = load_targets_file(path)
    assert bad.resolve_name() is None  # a typo'd default must not take the CLI down
    assert bad.resolve_name(flag="rewvis") == "rewvis"


def test_apply_fills_the_environment(targets_file: Path) -> None:
    name = resolve_target(flag="rewvis")
    assert name == "rewvis"
    assert os.environ["REWORK_DB"] == "postgresql://db/rewvis"
    assert os.environ["REWORK_DAEMON_URL"] == "http://100.107.125.118:8941"
    assert os.environ["REWORK_LIBRARY_DIR"] == "/home/ubuntu/.rework/library"
    assert os.environ["REWORK_TARGET_NAME"] == "rewvis"


def test_apply_never_clobbers_explicit_env(targets_file: Path) -> None:
    # exactly the preview.py / rewvis-systemd contract: what is already set wins
    os.environ["REWORK_DB"] = "sqlite:////tmp/mine.db"
    os.environ["REWORK_DAEMON_URL"] = "http://127.0.0.1:9999"
    os.environ["REWORK_TARGET_NAME"] = "PREVIEW #3"
    assert resolve_target(flag="rewvis") == "rewvis"
    assert os.environ["REWORK_DB"] == "sqlite:////tmp/mine.db"
    assert os.environ["REWORK_DAEMON_URL"] == "http://127.0.0.1:9999"
    assert os.environ["REWORK_TARGET_NAME"] == "PREVIEW #3"


def test_no_target_touches_nothing(tmp_path: Path) -> None:
    assert resolve_target(path=tmp_path / "nope.toml") is None
    assert "REWORK_DB" not in os.environ
    assert "REWORK_TARGET_NAME" not in os.environ


def test_apply_false_only_answers(targets_file: Path) -> None:
    assert resolve_target(flag="sandbox", apply=False) == "sandbox"
    assert "REWORK_DB" not in os.environ
    assert "REWORK_TARGET_NAME" not in os.environ


def test_env_var_selection_via_rework_target(targets_file: Path) -> None:
    os.environ["REWORK_TARGET"] = "sandbox"
    assert resolve_target() == "sandbox"
    assert os.environ["REWORK_DB"] == "sqlite:////tmp/rework-sandbox.db"
    assert os.environ["REWORK_TARGET_NAME"] == "sandbox"


def test_target_is_a_carry_only_dataclass() -> None:
    t = Target(name="x")
    assert t.db is None and t.ssh is None
