"""The Postgres seam: `_serialized` retries a deadlock, not a logical error.

Every public `PostgresStore` method is wrapped in `_serialized`, so that wrapper
is the single place the store retries a transient abort: a deadlock (SQLSTATE
40P01) or a serialization failure (40001) must re-run the method from a clean
connection, while a logical error must propagate on the first call.

No database is needed: `postgres.py` only imports psycopg and defines classes at
import time (connections are opened by `open_postgres`), so importing the
private wrapper here is safe in a sqlite-only environment. The fake below carries
the only two attributes the wrapper touches.
"""

from __future__ import annotations

import asyncio

import pytest

from rework.kernel.state import retry as retry_module
from rework.kernel.state.postgres import _serialized, _TaskLock
from rework.kernel.state.retry import MAX_ATTEMPTS, TransientDatabaseError


class DbError(RuntimeError):
    """A driver-shaped error: a message plus its SQLSTATE."""

    def __init__(self, message: str, sqlstate: str) -> None:
        super().__init__(message)
        self.sqlstate = sqlstate


class FakePostgresStore:
    """A stand-in for PostgresStore with just what `_serialized` uses.

    `_serialized` takes the reentrant per-task op lock (the store's real lock,
    so reentrancy is exercised too) and calls `_end_idle_transaction`, which a
    real store turns into commit/rollback on the shared connection. The wrapper
    is applied to the method here exactly as the real store applies it.
    """

    def __init__(self, failures: list[Exception]) -> None:
        self._op_lock = _TaskLock()
        self._failures = list(failures)
        self.calls = 0
        self.cleanups: list[int] = []

    async def _end_idle_transaction(self) -> None:
        # Recorded with the call count at cleanup time, so the test can prove the
        # cleanup ran BETWEEN attempts and not only at the end.
        self.cleanups.append(self.calls)

    @_serialized
    async def write(self) -> str:
        self.calls += 1
        if self._failures:
            raise self._failures.pop(0)
        return "ok"


async def test_serialized_retries_a_deadlock_and_cleans_up_between_attempts() -> None:
    store = FakePostgresStore([DbError("deadlock detected", "40P01")])

    assert await store.write() == "ok"
    assert store.calls == 2, "the deadlocked attempt must be retried"
    # Observed contract: cleanup runs after EVERY attempt, failed or not, so the
    # aborted transaction is rolled back before the retry and the successful one
    # is committed before the wrapper returns. Recorded with the call count at
    # cleanup time, so [1, 2] proves the first cleanup sat between the attempts.
    assert store.cleanups == [1, 2]


async def test_serialized_propagates_a_logical_error_at_once() -> None:
    error = DbError("duplicate key value violates unique constraint", "23505")
    store = FakePostgresStore([error])

    with pytest.raises(DbError) as caught:
        await store.write()

    assert caught.value is error
    assert store.calls == 1, "a unique violation is not retryable"
    assert store.cleanups == [1], "cleanup still runs once, after the single attempt"


async def test_serialized_exhaustion_still_classifies_as_transient() -> None:
    store = FakePostgresStore([DbError("deadlock detected", "40P01") for _ in range(5)])

    with pytest.raises(TransientDatabaseError) as caught:
        await store.write()

    assert store.calls == MAX_ATTEMPTS
    assert caught.value.attempts == MAX_ATTEMPTS
    assert caught.value.sqlstate == "40P01"


class TwoMethodStore:
    """Two decorated methods sharing ONE op lock: the lock is what is under test."""

    def __init__(self, failures: list[Exception]) -> None:
        self._op_lock = _TaskLock()
        self._failures = list(failures)
        self.others = 0

    async def _end_idle_transaction(self) -> None:
        return None

    @_serialized
    async def deadlocking(self) -> str:
        if self._failures:
            raise self._failures.pop(0)
        return "first"

    @_serialized
    async def other(self) -> str:
        self.others += 1
        return "second"


async def test_the_backoff_sleep_does_not_hold_the_op_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The op lock covers ONE ATTEMPT, so the backoff sleep must not hold it.

    While the deadlocked write is in its sleep, a second task must be able to
    take the lock and finish its own call. Pre-fix the sleep ran INSIDE
    `async with self._op_lock`, so `other()` blocked on the lock until the retry
    was over and the wait_for below timed out. The sleep is patched, so no real
    delay is involved and the interleaving is deterministic."""
    store = TwoMethodStore([DbError("deadlock detected", "40P01")])
    sleeping = asyncio.Event()
    release = asyncio.Event()

    async def fake_sleep(_delay: float) -> None:
        sleeping.set()
        await release.wait()

    monkeypatch.setattr(retry_module.asyncio, "sleep", fake_sleep)

    first = asyncio.create_task(store.deadlocking())
    await asyncio.wait_for(sleeping.wait(), timeout=5)
    assert not first.done(), "the first call must be sleeping between its attempts"

    # the decisive assertion: another task gets the connection during the backoff
    assert await asyncio.wait_for(store.other(), timeout=5) == "second"
    assert store.others == 1
    assert not first.done(), "the second call must finish BEFORE the first returns"

    release.set()
    assert await asyncio.wait_for(first, timeout=5) == "first"
