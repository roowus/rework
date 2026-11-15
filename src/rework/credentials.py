"""Database credentials that CHANGE while the process is running.

The TUI and the long-lived `rework library-mcp` process open their store once
and keep the URL they started with. When the Postgres password is rotated,
every reconnect fails with `FATAL: password authentication failed for user
"postgres"` (SQLSTATE 28P01) and the TUI stays offline until it is quit and
reopened. The recovery is: recognise an AUTHENTICATION failure, re-read the env
file (the process environment still holds the stale value, so `load_env_file`'s
setdefault contract cannot help here), and reconnect once with the new URL.

Everything here is defensive by design: this code runs on a path that is
already handling a connection failure, so it must never raise and never log or
embed a value (the URL carries a password).
"""

from __future__ import annotations

import os
from collections.abc import Collection, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from rework.config import parse_env_file

if TYPE_CHECKING:
    from rework.targets import TargetsFile

# The one line both recovery paths log when a reconnect with the new
# credentials works. It names no value: the URL carries the password.
CREDENTIALS_RELOADED = "database credentials changed on disk; reconnected"

# The variable Config.from_env actually reads for the database URL.
DEFAULT_DB_ENV = "REWORK_DB"

# Query parameters that carry credentials. Everything else in a query string
# (sslmode, connect_timeout, ...) describes the CONNECTION, so a rotation may
# change only these keys.
_CREDENTIAL_QUERY_KEYS = frozenset({"user", "password", "passfile"})

# Postgres SQLSTATEs that mean "these credentials are wrong", not "the network
# died": 28P01 invalid_password, 28000 invalid_authorization_specification.
AUTH_SQLSTATES = frozenset({"28P01", "28000"})

# psycopg does not always attach a sqlstate (a connect-time failure carries the
# server's message text instead), so the message is the other signal. This is
# the exact wording Postgres uses for a wrong password.
AUTH_MESSAGE = "password authentication failed"


def _psycopg_connection_types() -> tuple[type[BaseException], ...]:
    try:
        import psycopg
    except ImportError:  # pragma: no cover - psycopg is a hard dependency
        return ()
    return (psycopg.OperationalError, psycopg.InterfaceError)


def _sqlstate(exc: BaseException) -> str | None:
    """The SQLSTATE off an exception, read defensively: psycopg exposes it as
    `.sqlstate` and, on a real server error, on `.diag`. Fake exceptions in
    tests have neither, so every lookup is guarded."""
    candidates: list[Any] = [exc]
    try:
        candidates.append(getattr(exc, "diag", None))
    except Exception:  # a `diag` property that raises must not break recovery
        pass
    for owner in candidates:
        try:
            value = getattr(owner, "sqlstate", None)
        except Exception:  # pragma: no cover - a property that raises
            continue
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def is_auth_error(exc: BaseException) -> bool:
    """Whether this failure is an AUTHENTICATION failure (the credentials are
    stale) rather than a generic connection failure. Only those may re-read the
    credentials: every other error keeps today's behaviour untouched."""
    if _sqlstate(exc) in AUTH_SQLSTATES:
        return True
    if isinstance(exc, _psycopg_connection_types()):
        return AUTH_MESSAGE in str(exc).lower()
    return False


def active_target_name(targets: TargetsFile) -> str | None:
    """Which target THIS process is on. REWORK_TARGET_NAME is the record of a
    `--target NAME` launch (resolve_target sets it), so it comes first;
    REWORK_TARGET is the env-only selector and is the fallback. resolve_name
    alone cannot express that precedence: it never sees the flag, so it falls
    back to the file's `default` and a `--target other` process would read the
    DEFAULT target's variable (a wrong-database write, found in audit)."""
    recorded = os.environ.get("REWORK_TARGET_NAME")
    if recorded and recorded in targets.targets:
        return recorded
    return targets.resolve_name(env_var=os.environ.get("REWORK_TARGET"))


def db_env_vars() -> tuple[str, ...]:
    """The env var names a database URL may live under, in the order a rotated
    value is picked up. REWORK_DB comes first because that is what
    Config.from_env reads, so it is the name that has to end up fresh. The
    active target's `db_env` is added second: targets.toml keeps the URL in its
    own variable (REWORK_REWVIS_DB on the Mac) and only resolve_target's
    setdefault ever copies it into REWORK_DB, so hardcoding REWORK_DB alone
    would miss a target that uses a different name."""
    names: list[str] = [DEFAULT_DB_ENV]
    try:
        from rework.targets import load_targets_file

        targets = load_targets_file()
        active = active_target_name(targets)
        target = targets.targets.get(active) if active is not None else None
        if target is not None and target.db_env and target.db_env not in names:
            names.append(target.db_env)
    except Exception:
        # A broken or absent targets file means "no extra name", never a raise:
        # this runs while recovering from a failed connection.
        pass
    return tuple(names)


def target_key(url: str) -> tuple[str, str, str, str] | None:
    """The parts of a URL that say WHICH database it points at: host, port,
    database name and the non-credential query string (sslmode and friends).
    A credential rotation changes the user/password only, so two URLs with the
    same key address the same database. None when the value cannot be read as
    a postgres URL: this runs on a recovery path, so it never raises."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        port = str(parts.port or "")
        database = parts.path.strip("/")
        query = "&".join(
            sorted(
                pair
                for pair in parts.query.split("&")
                if pair and pair.split("=", 1)[0].strip().lower() not in _CREDENTIAL_QUERY_KEYS
            )
        )
    except ValueError:  # a bad port, a broken IPv6 literal, ...
        return None
    if not host or not database:
        return None
    return (host, port, database, query)


def fresh_db_url(
    env_file: Path | None,
    current_url: str | None,
    var_names: Sequence[str] = (DEFAULT_DB_ENV,),
    avoid: Collection[str] = (),
) -> str | None:
    """The database URL the env FILE holds now, or None when there is nothing
    to recover.

    Pure: reads the file only (never os.environ, which still holds the stale
    value), touches no global state, logs nothing. None covers a missing,
    unreadable or malformed file, an empty value, and a value identical to
    `current_url`, so an unchanged URL can never drive a reconnect loop.

    A fresh value is a ROTATION only when it addresses the SAME database:
    host, port, database name and any non-credential connection parameter must
    match `current_url`, with only the user/password changing. A value that
    moves the target is rejected (None), so a mis-resolved or other-target
    variable can never redirect this process's writes.

    `avoid` lists the URLs this process has already left behind. A file another
    writer has not updated yet can still hold one of them, and re-adopting it
    is the flap that kept the recovery from converging (found with two
    writers): such a value is skipped, so the sequence is monotone."""
    values = parse_env_file(env_file)
    current = (current_url or "").strip()
    # No known current target means "cannot prove it is the same database",
    # so nothing may be switched: the safe answer is always None.
    current_key = target_key(current) if current else None
    if current_key is None:
        return None
    for name in var_names:
        value = values.get(name, "").strip()
        if not value or value == current or value in avoid:
            continue
        if target_key(value) == current_key:
            return value
    return None


def recover_db_credentials(
    *,
    current_url: str | None = None,
    env_file: Path | None = None,
    var_names: Sequence[str] | None = None,
    avoid: Collection[str] = (),
) -> str | None:
    """Re-read the env file and, when it holds a DIFFERENT database URL, put it
    where the store factory will see it (REWORK_DB). Returns the fresh URL, or
    None when nothing changed (then callers must fail exactly as they do
    today: one attempt, no second retry, no loop). Never raises and never logs
    the value."""
    try:
        fresh = fresh_db_url(
            env_file,
            os.environ.get(DEFAULT_DB_ENV) if current_url is None else current_url,
            db_env_vars() if var_names is None else var_names,
            avoid,
        )
        if fresh is None:
            return None
        os.environ[DEFAULT_DB_ENV] = fresh
        return fresh
    except Exception:  # pragma: no cover - recovery must never mask the real error
        return None
