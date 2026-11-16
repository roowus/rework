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


@pytest.mark.parametrize(
    "exc",
    [
        psycopg.OperationalError("server closed the connection unexpectedly"),  # no SQLSTATE
        pgerr.AdminShutdown("terminating connection due to administrator command"),
        pgerr.CannotConnectNow("the database system is starting up"),
        pgerr.ConnectionFailure("connection lost"),
        psycopg.InterfaceError("the connection is closed"),
        sqlite3.OperationalError("database is locked"),
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
    ],
)
def test_permanent_answers_surface_at_once(exc: BaseException) -> None:
    assert is_transient(exc) is False
