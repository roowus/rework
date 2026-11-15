"""~/.rework/rework.env holds provider keys on each machine (rewvis's systemd
already loads it); rework reads it itself, never overriding the environment."""

import os
from pathlib import Path

import pytest

from rework.config import load_env_file, parse_env_file
from rework.credentials import fresh_db_url, is_auth_error


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
    path.write_text("REWORK_REWVIS_DB=postgresql://u:fresh@host/db\n")
    assert fresh_db_url(path, "x", ("REWORK_DB",)) is None
    assert (
        fresh_db_url(path, "x", ("REWORK_DB", "REWORK_REWVIS_DB")) == "postgresql://u:fresh@host/db"
    )


def test_fresh_db_url_returns_none_when_a_target_from_the_environment_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The targets file lives outside the caller's control; an unreadable or
    absent one must mean "no extra name", not an exception on the recovery
    path."""
    monkeypatch.setenv("REWORK_TARGET", "nosuch")
    path = tmp_path / "rework.env"
    path.write_text("REWORK_DB=postgresql://u:fresh@host/db\n")
    assert fresh_db_url(path, "old") == "postgresql://u:fresh@host/db"


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
