"""~/.rework/rework.env holds provider keys on each machine (rewvis's systemd
already loads it); rework reads it itself, never overriding the environment."""

import os
from pathlib import Path

import pytest

from rework import targets as targets_mod
from rework.config import load_env_file, parse_env_file
from rework.credentials import (
    db_env_vars,
    fresh_db_url,
    is_auth_error,
    recover_db_credentials,
)

STALE = "postgresql://postgres:STALEPASS@db.internal:5433/rework_state"
FRESH = "postgresql://postgres:LIVEHOSTPASS@db.internal:5433/rework_state"


def test_the_env_file_fills_gaps_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    f = tmp_path / "rework.env"
    f.write_text(
        "# provider keys\n"
        "REWORK_T_A=alpha\n"
        "export REWORK_T_B='quoted value'\n"
        'REWORK_T_C="dq"\n'
        "REWORK_T_SET=from-file\n"
        "not a line\n\n"
    )
    monkeypatch.setenv("REWORK_T_SET", "from-env")
    for k in ("REWORK_T_A", "REWORK_T_B", "REWORK_T_C"):
        monkeypatch.delenv(k, raising=False)
    added = load_env_file(f)
    try:
        assert sorted(added) == ["REWORK_T_A", "REWORK_T_B", "REWORK_T_C"]
        assert os.environ["REWORK_T_A"] == "alpha"
        assert os.environ["REWORK_T_B"] == "quoted value"
        assert os.environ["REWORK_T_C"] == "dq"
        assert os.environ["REWORK_T_SET"] == "from-env"  # the environment wins
    finally:
        for k in added:
            os.environ.pop(k, None)


def test_a_missing_file_is_fine(tmp_path: Path) -> None:
    assert load_env_file(tmp_path / "nope.env") == []


# -- rotated credentials: parse-only reader + the fresh-URL helper -------------


def test_parse_env_file_does_not_touch_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REWORK_DB", "postgresql://u:stale@host/db")
    path = tmp_path / "rework.env"
    path.write_text('REWORK_DB="postgresql://u:fresh@host/db"\n# comment\nexport A_KEY=x\n')
    values = parse_env_file(path)
    assert values["REWORK_DB"] == "postgresql://u:fresh@host/db"
    assert values["A_KEY"] == "x"
    assert os.environ["REWORK_DB"] == "postgresql://u:stale@host/db", "parse only"


def test_load_env_file_override_replaces_but_default_still_setdefaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REWORK_DB", "postgresql://u:stale@host/db")
    path = tmp_path / "rework.env"
    path.write_text("REWORK_DB=postgresql://u:fresh@host/db\n")
    assert load_env_file(path) == [], "the documented setdefault contract is unchanged"
    assert os.environ["REWORK_DB"] == "postgresql://u:stale@host/db"
    assert load_env_file(path, override=True) == ["REWORK_DB"], "the rotation path overrides"
    assert os.environ["REWORK_DB"] == "postgresql://u:fresh@host/db"


def test_fresh_db_url_returns_the_value_from_the_file(tmp_path: Path) -> None:
    path = tmp_path / "rework.env"
    path.write_text("REWORK_DB=postgresql://u:fresh@host/db\n")
    assert fresh_db_url(path, "postgresql://u:stale@host/db") == "postgresql://u:fresh@host/db"


def test_fresh_db_url_none_when_the_value_is_unchanged(tmp_path: Path) -> None:
    """An identical URL must be None: otherwise every failed reconnect looks
    like a rotation and the app loops."""
    path = tmp_path / "rework.env"
    path.write_text("REWORK_DB=postgresql://u:same@host/db\n")
    assert fresh_db_url(path, "postgresql://u:same@host/db") is None


def test_fresh_db_url_none_when_the_file_is_missing(tmp_path: Path) -> None:
    assert fresh_db_url(tmp_path / "absent.env", "postgresql://u:stale@host/db") is None


def test_fresh_db_url_none_when_the_value_is_empty_or_malformed(tmp_path: Path) -> None:
    empty = tmp_path / "empty.env"
    empty.write_text("REWORK_DB=\nREWORK_OTHER=1\n")
    assert fresh_db_url(empty, "postgresql://u:stale@host/db") is None
    junk = tmp_path / "junk.env"
    junk.write_bytes(b"\xff\xfe not text \x00")
    assert fresh_db_url(junk, "postgresql://u:stale@host/db") is None
    no_value = tmp_path / "novalue.env"
    no_value.write_text("REWORK_OTHER=1\n")
    assert fresh_db_url(no_value, "postgresql://u:stale@host/db") is None


def test_fresh_db_url_checks_each_variable_name_in_order(tmp_path: Path) -> None:
    """A target keeps the URL under its own name (targets.toml db_env); the
    helper must take the names as a parameter instead of assuming REWORK_DB."""
    path = tmp_path / "rework.env"
    path.write_text(f"REWORK_REWVIS_DB={FRESH}\n")
    assert fresh_db_url(path, STALE, ("REWORK_DB",)) is None
    assert fresh_db_url(path, STALE, ("REWORK_DB", "REWORK_REWVIS_DB")) == FRESH


def test_fresh_db_url_returns_none_when_a_target_from_the_environment_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The targets file lives outside the caller's control; an unreadable or
    absent one must mean "no extra name", not an exception on the recovery
    path."""
    monkeypatch.setenv("REWORK_TARGET", "nosuch")
    path = tmp_path / "rework.env"
    path.write_text(f"REWORK_DB={FRESH}\n")
    assert fresh_db_url(path, STALE) == FRESH


# -- the auth classifier -------------------------------------------------------


def test_is_auth_error_recognises_a_wrong_password() -> None:
    import psycopg

    assert is_auth_error(
        psycopg.OperationalError('FATAL:  password authentication failed for user "postgres"')
    )
    assert is_auth_error(psycopg.OperationalError("password authentication failed"))


def test_is_auth_error_recognises_the_sqlstates() -> None:
    import psycopg

    for state in ("28P01", "28000"):
        exc = psycopg.OperationalError("something")
        exc.sqlstate = state  # type: ignore[attr-defined]
        assert is_auth_error(exc), state


def test_is_auth_error_reads_the_sqlstate_off_diag_defensively() -> None:
    class _Diag:
        sqlstate = "28P01"

    class _Weird(Exception):
        diag = _Diag()

    assert is_auth_error(_Weird("boom"))

    class _RaisingDiag(Exception):
        @property
        def diag(self):  # type: ignore[no-untyped-def]
            raise RuntimeError("no diag")

    assert is_auth_error(_RaisingDiag("boom")) is False, "never raises on a fake"


def test_is_auth_error_no_for_other_failures() -> None:
    import psycopg

    assert not is_auth_error(psycopg.OperationalError("consuming input failed"))
    assert not is_auth_error(OSError("connection refused"))
    assert not is_auth_error(RuntimeError("password authentication failed")), "not a db error"
    plain = psycopg.OperationalError("x")
    plain.sqlstate = "57014"  # type: ignore[attr-defined]
    assert not is_auth_error(plain), "a different SQLSTATE is not an auth failure"


# -- only the CREDENTIALS may change: never the database the process writes to -


def test_a_fresh_value_for_a_different_host_port_or_database_is_rejected(
    tmp_path: Path,
) -> None:
    """Audit blocker: with two targets configured, a mis-resolved variable (or
    the owner's own edit) pointed at ANOTHER target's database, and switching
    to it would have written to production. A rotation changes the user and
    password only, so anything that moves the target is refused."""
    path = tmp_path / "rework.env"
    for elsewhere in (
        "postgresql://postgres:LIVEHOSTPASS@OTHERHOST:5433/rework_state",  # host
        "postgresql://postgres:LIVEHOSTPASS@db.internal:5555/rework_state",  # port
        "postgresql://postgres:LIVEHOSTPASS@db.internal:5433/rewvis",  # database
        "postgresql://postgres:LIVEHOSTPASS@db.internal:5433/rework_state?sslmode=disable",
    ):
        path.write_text(f"REWORK_DB={elsewhere}\n")
        assert fresh_db_url(path, STALE) is None, elsewhere


def test_a_changed_password_on_the_same_host_port_and_database_is_accepted(
    tmp_path: Path,
) -> None:
    path = tmp_path / "rework.env"
    path.write_text(f"REWORK_DB={FRESH}\n")
    assert fresh_db_url(path, STALE) == FRESH
    # the user may change too, and sslmode describes the CONNECTION, not the
    # target: same host/port/db plus the same sslmode is still a rotation
    path.write_text(
        "REWORK_DB=postgresql://other:NEW@db.internal:5433/rework_state?sslmode=require\n"
    )
    assert (
        fresh_db_url(
            path, "postgresql://postgres:OLD@db.internal:5433/rework_state?sslmode=require"
        )
        is not None
    )


def test_a_candidate_with_a_wrong_scheme_is_never_a_rotation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Audit blocker: target_key looked at host/port/db only, so a value
    differing from the in-use DSN by a SCHEME TYPO keyed identically and was
    accepted. Config.from_env opens anything that is not `postgresql://`/
    `postgres://` as a sqlite PATH, so the wrong scheme left the TUI silently
    "live" on an empty local file whose NAME held the DSN, password included."""
    path = tmp_path / "rework.env"
    monkeypatch.setenv("REWORK_DB", STALE)
    for wrong in (
        "postgresql+asyncpg://postgres:LIVEHOSTPASS@db.internal:5433/rework_state",
        "junk://postgres:LIVEHOSTPASS@db.internal:5433/rework_state",
        "http://postgres:LIVEHOSTPASS@db.internal:5433/rework_state",
        "mysql://postgres:LIVEHOSTPASS@db.internal:5433/rework_state",
        # urlsplit lowercases the scheme, but the loader matches the prefix
        # case-sensitively, so this is still a sqlite path to Config.from_env.
        "POSTGRESQL://postgres:LIVEHOSTPASS@db.internal:5433/rework_state",
    ):
        path.write_text(f"REWORK_DB={wrong}\n")
        assert fresh_db_url(path, STALE, ("REWORK_DB",)) is None, wrong
        # and the guard holds where it matters: the environment is untouched
        assert (
            recover_db_credentials(current_url=STALE, env_file=path, var_names=("REWORK_DB",))
            is None
        ), wrong
        assert os.environ["REWORK_DB"] == STALE, wrong


def test_the_two_accepted_postgres_schemes_are_interchangeable(tmp_path: Path) -> None:
    """`postgres://` and `postgresql://` name the same database and both open a
    real Postgres connection, so a rotation that only changes the spelling is
    still a rotation (the guard rejects scheme TYPOS, not this)."""
    path = tmp_path / "rework.env"
    path.write_text("REWORK_DB=postgres://postgres:LIVEHOSTPASS@db.internal:5433/rework_state\n")
    assert fresh_db_url(path, STALE) == (
        "postgres://postgres:LIVEHOSTPASS@db.internal:5433/rework_state"
    )
    long_current = "postgresql://postgres:STALEPASS@db.internal:5433/rework_state"
    path.write_text(f"REWORK_DB={FRESH}\n")
    assert fresh_db_url(path, long_current) == FRESH


def test_fresh_db_url_refuses_to_rotate_from_a_non_postgres_current_url(tmp_path: Path) -> None:
    """Defensive: a current URL the loader would not open as Postgres is not a
    database this process can be said to be rotating credentials ON."""
    path = tmp_path / "rework.env"
    path.write_text(f"REWORK_DB={FRESH}\n")
    for current in (
        "junk://postgres:STALEPASS@db.internal:5433/rework_state",
        "postgresql+asyncpg://postgres:STALEPASS@db.internal:5433/rework_state",
        "http://postgres:STALEPASS@db.internal:5433/rework_state",
        "POSTGRESQL://postgres:STALEPASS@db.internal:5433/rework_state",
    ):
        assert fresh_db_url(path, current) is None, current


def test_fresh_db_url_never_switches_from_an_unparseable_current_url(tmp_path: Path) -> None:
    """Nothing to compare against means nothing can be proven to be the same
    database, so the safe answer is None (this runs while already failing)."""
    path = tmp_path / "rework.env"
    path.write_text(f"REWORK_DB={FRESH}\n")
    assert fresh_db_url(path, None) is None
    assert fresh_db_url(path, "not a url") is None
    assert fresh_db_url(path, "sqlite:////tmp/state.db") is None


def test_fresh_db_url_none_when_the_candidate_cannot_be_parsed(tmp_path: Path) -> None:
    path = tmp_path / "rework.env"
    path.write_text("REWORK_DB=://broken\n")
    assert fresh_db_url(path, STALE) is None


# -- the active target: REWORK_TARGET_NAME (a --target launch) beats REWORK_TARGET


TWO_TARGETS = """\
default = "rewvis"

[targets.rewvis]
db_env = "REWORK_TEST_REWVIS_DB"
daemon_url = "http://100.107.125.118:8941"

[targets.other]
db_env = "REWORK_TEST_OTHER_DB"
daemon_url = "http://127.0.0.1:8941"
"""

# the same database (host, port, name) under two different variables
PROD_URL = "postgresql://postgres:PRODPASS@db.internal:5433/rework_state"
SANDBOX_URL = "postgresql://postgres:SANDBOXPASS@db.internal:5433/rework_state"


@pytest.fixture()
def two_targets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "targets.toml"
    path.write_text(TWO_TARGETS)
    monkeypatch.setattr(targets_mod, "TARGETS_PATH", path)
    for name in ("REWORK_TARGET", "REWORK_TARGET_NAME"):
        monkeypatch.delenv(name, raising=False)
    # both variables are set, as resolve_target leaves them on a real launch
    # (`db_env` with no value in the environment drops the whole entry)
    monkeypatch.setenv("REWORK_TEST_REWVIS_DB", PROD_URL)
    monkeypatch.setenv("REWORK_TEST_OTHER_DB", SANDBOX_URL)
    return path


def test_db_env_vars_uses_the_target_a_target_flag_launched(
    two_targets: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The audit's wrong-database write: `--target other` sets REWORK_TARGET_NAME
    and leaves REWORK_TARGET unset, so resolving from REWORK_TARGET alone fell
    back to the file's `default` (rewvis) and a rotation would have rewritten
    REWORK_DB to rewvis's URL. The target this process is ON wins."""
    monkeypatch.setenv("REWORK_TARGET_NAME", "other")
    assert db_env_vars() == ("REWORK_DB", "REWORK_TEST_OTHER_DB")


def test_db_env_vars_uses_the_default_when_no_target_was_asked_for(
    two_targets: Path,
) -> None:
    assert db_env_vars() == ("REWORK_DB", "REWORK_TEST_REWVIS_DB")


def test_db_env_vars_falls_back_to_rework_target(
    two_targets: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REWORK_TARGET", "other")
    assert db_env_vars() == ("REWORK_DB", "REWORK_TEST_OTHER_DB")


def test_a_target_launch_recovers_its_own_variable_end_to_end(
    two_targets: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End to end against a real targets.toml: with TWO targets listed, a
    `--target other` process must pick the rotated value out of ITS OWN
    variable and never touch the default target's."""
    monkeypatch.setenv("REWORK_TARGET_NAME", "other")
    env_file = tmp_path / "rework.env"
    env_file.write_text(f"REWORK_TEST_REWVIS_DB={PROD_URL}\nREWORK_TEST_OTHER_DB={SANDBOX_URL}\n")
    monkeypatch.setenv("REWORK_DB", STALE)

    fresh = recover_db_credentials(current_url=STALE, env_file=env_file)
    assert fresh == SANDBOX_URL, "the --target target's own variable is the source"
    assert fresh != PROD_URL, "the default target's variable never redirects us"
    assert os.environ["REWORK_DB"] == SANDBOX_URL


def test_recover_db_credentials_with_no_current_url(
    two_targets: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The TUI calls it with no URL to compare against: it must read the value
    this process already holds, and never raise."""
    monkeypatch.setenv("REWORK_TARGET_NAME", "other")
    env_file = tmp_path / "rework.env"
    env_file.write_text(f"REWORK_TEST_OTHER_DB={SANDBOX_URL}\n")
    assert recover_db_credentials(current_url=None, env_file=env_file) is None, "nothing to compare"
    monkeypatch.setenv("REWORK_DB", STALE)
    assert recover_db_credentials(current_url=None, env_file=env_file) == SANDBOX_URL
    assert os.environ["REWORK_DB"] == SANDBOX_URL
    # a missing file stays quiet too
    assert recover_db_credentials(current_url=STALE, env_file=tmp_path / "absent.env") is None


# -- the default loader stays byte-for-byte compatible with the old one -------


def test_a_repeated_key_keeps_its_first_value(tmp_path: Path) -> None:
    """Defect found in audit: routing the default loader through a dict made a
    repeated key take the LAST occurrence, where the old line-by-line loader
    took the FIRST (`if name not in os.environ`). The owner hand-edits this
    file, so a duplicate must behave exactly as it did."""
    path = tmp_path / "rework.env"
    path.write_text("REWORK_T_DUP=first\nREWORK_T_DUP=second\n")
    assert parse_env_file(path)["REWORK_T_DUP"] == "first"
    try:
        assert load_env_file(path) == ["REWORK_T_DUP"]
        assert os.environ["REWORK_T_DUP"] == "first"
    finally:
        os.environ.pop("REWORK_T_DUP", None)


def test_the_parser_matches_the_old_loader_line_for_line(tmp_path: Path) -> None:
    """The awkward lines, all of which the old loader handled its own way: a
    quoted value with a comment inside stays whole (no inline-comment
    stripping), a line without "=" is skipped, a blank name is skipped, an
    empty value is kept as empty, and `export ` is optional."""
    path = tmp_path / "rework.env"
    path.write_text(
        "REWORK_T_Q1='single quoted'\n"
        'REWORK_T_Q2="double quoted"\n'
        "REWORK_T_INLINE=alpha # not a comment\n"
        "REWORK_T_EMPTY=\n"
        "export REWORK_T_EXPORT=exported\n"
        "=no name\n"
        "no equals sign\n"
        "   \n"
        "# comment\n"
    )
    values = parse_env_file(path)
    assert values == {
        "REWORK_T_Q1": "single quoted",
        "REWORK_T_Q2": "double quoted",
        "REWORK_T_INLINE": "alpha # not a comment",
        "REWORK_T_EMPTY": "",
        "REWORK_T_EXPORT": "exported",
    }
    for name in values:
        assert name not in os.environ
