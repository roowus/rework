"""Which store errors are retried through an outage (ResilientStore.is_transient).

A link drop must be retried; a permanent answer from a live server (a statement
timeout, a lock that is not available, too many connections) must surface at once,
or each such call burns the whole five-minute retry budget."""

from __future__ import annotations

import sqlite3

import psycopg
import pytest
from psycopg import errors as pgerr

from rework.kernel.state.resilient import is_transient

# sqlite3 raises OperationalError for every condition, so the class alone decides
# nothing: these are the messages that must be told apart.
SQLITE_TRANSIENT = [
    "database is locked",
    "database table is locked: items",
    "database is busy",
    "disk I/O error",
]
SQLITE_PERMANENT = [
    "attempt to write a readonly database",
    "no such table: nope",
    "no such column: nope",
    "no such function: nope",
    "database disk image is malformed",
]
SQLITE_AMBIGUOUS = [
    "unable to open database file",
    "database or disk is full",
    "out of memory",
    "interrupted",
]


@pytest.mark.parametrize(
    "exc",
    [
        psycopg.OperationalError("server closed the connection unexpectedly"),  # no SQLSTATE
        pgerr.AdminShutdown("terminating connection due to administrator command"),
        pgerr.CannotConnectNow("the database system is starting up"),
        pgerr.ConnectionFailure("connection lost"),
        psycopg.InterfaceError("the connection is closed"),
        *[sqlite3.OperationalError(m) for m in SQLITE_TRANSIENT],
    ],
)
def test_link_losses_are_retried(exc: BaseException) -> None:
    assert is_transient(exc) is True


@pytest.mark.parametrize(
    "exc",
    [
        pgerr.QueryCanceled("canceling statement due to statement timeout"),
        pgerr.LockNotAvailable("could not obtain lock"),
        pgerr.TooManyConnections("sorry, too many clients already"),
        pgerr.InsufficientResources("out of memory"),
        ValueError("not a store error"),
        *[sqlite3.OperationalError(m) for m in SQLITE_PERMANENT],
        # ambiguous conditions fail fast too (see _is_sqlite_transient)
        *[sqlite3.OperationalError(m) for m in SQLITE_AMBIGUOUS],
        sqlite3.DatabaseError("file is not a database"),  # the sqlite3 class for NOTADB
    ],
)
def test_permanent_answers_surface_at_once(exc: BaseException) -> None:
    assert is_transient(exc) is False


@pytest.mark.parametrize("message", ["Database Is Locked", "DATABASE IS BUSY"])
def test_sqlite_transient_match_ignores_case(message: str) -> None:
    assert is_transient(sqlite3.OperationalError(message)) is True
