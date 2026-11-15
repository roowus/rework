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
        "REWORK_CHECKPOINTS_DB",
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
    path.write_text(
        'default = "ghost"\n[targets.rewvis]\ndb = "sqlite:///x"\ndaemon_url = "http://h"\n'
    )
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
    # the sandbox target's sqlite:// URL is NORMALIZED to the plain path
    # Config.open_store_from_config actually opens (the raw URL would be
    # mangled into a bogus relative directory named `sqlite:` in the CWD)
    assert os.environ["REWORK_DB"] == "/tmp/rework-sandbox.db"
    assert os.environ["REWORK_TARGET_NAME"] == "sandbox"


def test_target_is_a_carry_only_dataclass() -> None:
    t = Target(name="x")
    assert t.db is None and t.ssh is None


# --- main()'s argv scan and the sqlite reachability helpers ------------------


def test_parse_target_flag_forms() -> None:
    from rework.cli import parse_target_flag

    assert parse_target_flag(["ls"]) == (None, ["ls"])
    assert parse_target_flag(["--target", "sandbox", "ls"]) == ("sandbox", ["ls"])
    assert parse_target_flag(["--target=sandbox", "ls"]) == ("sandbox", ["ls"])
    assert parse_target_flag(["--target", "sandbox"]) == ("sandbox", [])
    assert parse_target_flag([]) == (None, [])
    # after the subcommand: never a global flag (main() must reject, not guess)
    assert parse_target_flag(["ls", "--target", "sandbox"]) == (
        None,
        ["ls", "--target", "sandbox"],
    )
    assert parse_target_flag(["ls", "--target=sandbox"]) == (
        None,
        ["ls", "--target=sandbox"],
    )
    assert parse_target_flag(["ls", "--", "--target", "sandbox"]) == (
        None,
        ["ls", "--", "--target", "sandbox"],
    )
    # a NAME that looks like a flag is not consumed (`--target --help`)
    assert parse_target_flag(["--target", "--help"]) == (None, ["--target", "--help"])
    assert parse_target_flag(["--target"]) == (None, ["--target"])
    assert parse_target_flag(["--target="]) == (None, [])
    # other leading flags are KEPT (never dropped: an empty rest means "open
    # the TUI", so dropping `--help` started the dashboard instead of help)
    assert parse_target_flag(["--help", "--target", "sandbox", "ls"]) == (
        "sandbox",
        ["--help", "ls"],
    )


def test_parse_target_flag_keeps_a_glued_short_option_in_rest() -> None:
    """A glued short option like `-n3` is kept in rest like any foreign flag.

    It is neither dropped nor skipped together with its glued value: typer is
    the one that decides what `-n3` means (`rework logs -n3`), so the parser
    hands the token over untouched. Dropping it turned `rework -n3 ls` into a
    bare `rework` and opened the TUI instead of running the command.
    """
    from rework.cli import parse_target_flag

    assert parse_target_flag(["-n3", "ls"]) == (None, ["-n3", "ls"])
    assert parse_target_flag(["-n3", "--target", "sandbox", "ls"]) == (
        "sandbox",
        ["-n3", "ls"],
    )


def test_subcommand_ignores_everything_after_the_end_of_options_marker() -> None:
    """The subcommand is the first non-flag token, but never one after `--`.

    `rework -- serve` hands `serve` to click as a POSITIONAL, not as a
    subcommand, so main() must not treat it as one: base read `--` at rest[0]
    and took the CLI logging path, and stopping at the marker in _subcommand()
    keeps that shape.
    """
    from rework.cli import _subcommand

    assert _subcommand([]) == ""
    assert _subcommand(["--help"]) == ""
    assert _subcommand(["-h"]) == ""
    assert _subcommand(["--", "serve"]) == ""
    assert _subcommand(["--help", "serve"]) == "serve"
    assert _subcommand(["update", "--target", "rewvis"]) == "update"


def test_parse_target_flag_keeps_help() -> None:
    """A lone leading flag must survive the scan, so main() does not see "bare rework".

    Regression: an empty rest means "open the TUI", so dropping `--help` / `-h`
    here made `rework --help` start the dashboard instead of printing help.
    """
    from rework.cli import parse_target_flag

    assert parse_target_flag(["--help"]) == (None, ["--help"])
    assert parse_target_flag(["-h"]) == (None, ["-h"])
    assert parse_target_flag(["--version"]) == (None, ["--version"])
    flag, rest = parse_target_flag(["--help"])
    assert flag is None
    assert rest  # non-empty: main() must reach the typer app, not run_tui()


def test_sqlite_db_path_forms() -> None:
    from rework.targets import sqlite_db_path

    assert sqlite_db_path("postgresql://h/db") is None
    assert sqlite_db_path("/abs/state.db") == Path("/abs/state.db")
    assert sqlite_db_path("rel/state.db") == Path("rel/state.db")
    # the docs/ops.md form: the one a bare Path(db) mangles into a relative path
    assert sqlite_db_path("sqlite:////Users/you/.rework/sandbox.db") == Path(
        "/Users/you/.rework/sandbox.db"
    )
    assert sqlite_db_path("sqlite:///rel/state.db") == Path("rel/state.db")
    assert sqlite_db_path("sqlite:x.db") == Path("x.db")
    assert sqlite_db_path("sqlite://host/x.db") is None  # a remote authority
    assert sqlite_db_path("sqlite:///") is None


def test_db_reachable_on_a_sqlite_url(tmp_path: Path) -> None:
    from rework.targets import db_reachable

    good = tmp_path / "sandbox.db"
    good.write_bytes(b"")  # an empty file is a valid sqlite database
    assert db_reachable(str(good))  # a plain path
    assert db_reachable(f"sqlite:///{good}")  # the docs/ops.md URL form
    assert not db_reachable(f"sqlite:///{tmp_path}/nope.db")  # missing file
    assert not db_reachable("postgresql://nobody:nobody@127.0.0.1:1/nope")


def test_checkpoints_db_applied_and_never_clobbered(tmp_path: Path) -> None:
    os.environ.pop("REWORK_CHECKPOINTS_DB", None)
    path = tmp_path / "targets.toml"
    path.write_text(
        '[targets.sandbox]\ndb = "/tmp/rework/state.sqlite3"'
        '\ndaemon_url = "http://127.0.0.1:8941"\n'
    )
    resolve_target(flag="sandbox", path=path)
    # a sqlite target is self-contained: checkpoints land next to the state db
    assert os.environ["REWORK_CHECKPOINTS_DB"] == "/tmp/rework/checkpoints.sqlite3"
    # an explicit env always wins (setdefault-only, like every other key)
    os.environ["REWORK_CHECKPOINTS_DB"] = "/tmp/mine/ck.db"
    resolve_target(flag="sandbox", path=path)
    assert os.environ["REWORK_CHECKPOINTS_DB"] == "/tmp/mine/ck.db"
    del os.environ["REWORK_CHECKPOINTS_DB"]


def test_checkpoints_db_field_and_env_beat_the_default(tmp_path: Path) -> None:
    os.environ.pop("REWORK_CHECKPOINTS_DB", None)
    path = tmp_path / "targets.toml"
    path.write_text(
        '[targets.sandbox]\ndb = "/tmp/rework/state.sqlite3"'
        '\ndaemon_url = "http://127.0.0.1:8941"'
        '\ncheckpoints_db = "/tmp/other/ck.db"\n'
    )
    resolve_target(flag="sandbox", path=path)
    assert os.environ["REWORK_CHECKPOINTS_DB"] == "/tmp/other/ck.db"
    os.environ.pop("REWORK_CHECKPOINTS_DB")
    path.write_text(
        '[targets.sandbox]\ndb = "/tmp/rework/state.sqlite3"'
        '\ndaemon_url = "http://127.0.0.1:8941"'
        '\ncheckpoints_db_env = "REWORK_TEST_CK_DB"\n'
    )
    os.environ["REWORK_TEST_CK_DB"] = "/tmp/from-env/ck.db"
    resolve_target(flag="sandbox", path=path)
    assert os.environ["REWORK_CHECKPOINTS_DB"] == "/tmp/from-env/ck.db"
    os.environ.pop("REWORK_CHECKPOINTS_DB")
    os.environ.pop("REWORK_TEST_CK_DB")


def test_postgres_target_touches_no_checkpoints_var(targets_file: Path) -> None:
    os.environ.pop("REWORK_CHECKPOINTS_DB", None)
    resolve_target(flag="rewvis")  # postgres shares the main db
    assert "REWORK_CHECKPOINTS_DB" not in os.environ


# --- the sqlite:// URL normalization (HIGH audit fix) ------------------------


def test_sqlite_url_target_is_normalized_to_a_plain_path(tmp_path: Path) -> None:
    """`db = "sqlite:////abs/x.db"` in targets.toml must reach REWORK_DB as the
    bare filesystem path: Config.open_store_from_config does Path(db) on it for
    sqlite, and the raw URL mangles into a bogus relative directory literally
    named `sqlite:` in the CWD (the silent wrong-target run this feature
    exists to prevent). docs/ops.md used to document exactly that URL form."""
    db = tmp_path / "sandbox.db"
    db.write_bytes(b"")  # a valid empty sqlite database
    path = tmp_path / "targets.toml"
    path.write_text(
        f'[targets.sandbox]\ndb = "sqlite:///{db}"\ndaemon_url = "http://127.0.0.1:8941"\n'
    )
    resolve_target(flag="sandbox", path=path)
    assert os.environ["REWORK_DB"] == str(db)  # the bare path, no scheme
    assert os.environ["REWORK_TARGET_NAME"] == "sandbox"
    # and the self-contained checkpoints default now derives from the REAL
    # directory (before the fix a URL target derived a mangled `sqlite:...` path)
    assert os.environ["REWORK_CHECKPOINTS_DB"] == str(tmp_path / "checkpoints.sqlite3")
    os.environ.pop("REWORK_CHECKPOINTS_DB")


def test_postgres_url_target_is_never_normalized(targets_file: Path) -> None:
    resolve_target(flag="rewvis")
    assert os.environ["REWORK_DB"] == "postgresql://db/rewvis"  # passed through untouched


def test_plain_sqlite_path_helper() -> None:
    from rework.targets import plain_sqlite_path

    assert plain_sqlite_path("/abs/state.db") == "/abs/state.db"  # already plain: identity
    assert plain_sqlite_path("sqlite:////Users/you/.rework/sandbox.db") == (
        "/Users/you/.rework/sandbox.db"
    )
    assert plain_sqlite_path("sqlite:///rel/state.db") == "rel/state.db"
    assert plain_sqlite_path("postgresql://db/rewvis") is None  # postgres: untouched
    assert plain_sqlite_path("sqlite://host/x.db") is None  # remote authority: not a local file
    assert plain_sqlite_path(None) is None
    assert plain_sqlite_path("") is None
