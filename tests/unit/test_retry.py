"""Retry wrappers: transient errors get retried with backoff, non-transient
fail immediately, and exhaustion re-raises the last error.

Two helpers are pinned here: the LLM adapter's provider retry (top) and
`rework.kernel.state.retry`, the store retry that turns a Postgres deadlock
(SQLSTATE 40P01) or serialization failure (40001) into another attempt instead
of a crashed mission runner.
"""

from __future__ import annotations

import logging
import types

import pytest

from rework.kernel.llm.adapters.retry import RetryingProvider, _is_transient
from rework.kernel.llm.types import ChatRequest, ChatResult
from rework.kernel.state import retry as state_retry
from rework.kernel.state.retry import (
    MAX_ATTEMPTS,
    TransientDatabaseError,
    is_transient_db_error,
    retry_transient,
)


class FlakyProvider:
    name = "flaky"

    def __init__(self, failures: list[Exception]) -> None:
        self._failures = list(failures)
        self.calls = 0

    async def complete(self, request: ChatRequest) -> ChatResult:
        self.calls += 1
        if self._failures:
            raise self._failures.pop(0)
        return ChatResult(text="ok")


def test_classification() -> None:
    assert _is_transient(RuntimeError("Error code: 503 - fetch connect timeout"))
    assert _is_transient(RuntimeError("server overloaded, try later"))
    assert not _is_transient(RuntimeError("401 invalid api key"))
    assert not _is_transient(ValueError("bad request shape"))


async def test_retries_transient_then_succeeds() -> None:
    flaky = FlakyProvider([RuntimeError("503 upstream"), RuntimeError("connect timeout")])
    provider = RetryingProvider(flaky, attempts=3, backoff_s=0.01)
    result = await provider.complete(ChatRequest(model="m", messages=[]))
    assert result.text == "ok"
    assert flaky.calls == 3


async def test_non_transient_fails_immediately() -> None:
    flaky = FlakyProvider([RuntimeError("401 invalid api key")])
    provider = RetryingProvider(flaky, attempts=3, backoff_s=0.01)
    with pytest.raises(RuntimeError, match="401"):
        await provider.complete(ChatRequest(model="m", messages=[]))
    assert flaky.calls == 1


async def test_exhaustion_raises_last() -> None:
    flaky = FlakyProvider([RuntimeError("503")] * 4)
    provider = RetryingProvider(flaky, attempts=3, backoff_s=0.01)
    with pytest.raises(RuntimeError, match="503"):
        await provider.complete(ChatRequest(model="m", messages=[]))
    assert flaky.calls == 3


async def test_rate_limits_get_a_deeper_pool() -> None:
    """429 storms outlast 5xx blips: rate limits get 3x the attempts and
    longer backoff (found live: the auditor burned all attempts in one)."""
    flaky = FlakyProvider([RuntimeError("Error code: 429 rate limit reached")] * 5)
    provider = RetryingProvider(flaky, attempts=3, backoff_s=0.01)
    result = await provider.complete(ChatRequest(model="m", messages=[]))
    assert result.text == "ok"
    assert flaky.calls == 6  # 5 failures + the success, within the 9-shot pool

    flaky2 = FlakyProvider([RuntimeError("Error code: 429 rate limit reached")] * 9)
    provider2 = RetryingProvider(flaky2, attempts=3, backoff_s=0.01)
    with pytest.raises(RuntimeError, match="429"):
        await provider2.complete(ChatRequest(model="m", messages=[]))
    assert flaky2.calls == 9


# --- rework.kernel.state.retry: the state-store retry helper ---------------------
#
# The adapter above has its own retry. Everything below pins the store helper that
# turns a Postgres deadlock (SQLSTATE 40P01) or serialization failure (40001) into
# a retried attempt instead of a crashed mission runner.


class DbError(RuntimeError):
    """Stands in for a psycopg error: a message plus its SQLSTATE.

    The helper is duck-typed on `sqlstate`, so no driver is needed here.
    """

    def __init__(self, message: str, sqlstate: str) -> None:
        super().__init__(message)
        self.sqlstate = sqlstate


def record_sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record the backoff delays instead of sleeping them for real.

    state/retry.py uses nothing else from asyncio, so a shim module keeps the
    real asyncio.sleep (and the event loop) untouched.
    """
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(state_retry, "asyncio", types.SimpleNamespace(sleep=fake_sleep))
    monkeypatch.setattr(state_retry, "RETRY_SLEEP_RANGE_S", (0.001, 0.002))
    return delays


@pytest.mark.parametrize(("failures", "expected_calls"), [(1, 2), (2, 3)])
async def test_deadlock_is_retried_through_to_success(
    monkeypatch: pytest.MonkeyPatch, failures: int, expected_calls: int
) -> None:
    delays = record_sleeps(monkeypatch)
    calls = 0

    async def operation() -> str:
        nonlocal calls
        calls += 1
        if calls <= failures:
            raise DbError("deadlock detected", "40P01")
        return "sentinel"

    assert await retry_transient(operation) == "sentinel"
    assert calls == expected_calls
    assert len(delays) == failures, "each retry backed off, the success did not"
    assert all(0.001 <= delay <= 0.002 for delay in delays)


async def test_one_warning_per_retry_naming_the_attempt(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    record_sleeps(monkeypatch)
    calls = 0

    async def operation() -> str:
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise DbError("deadlock detected", "40P01")
        return "ok"

    with caplog.at_level(logging.WARNING, logger="rework.state.retry"):
        assert await retry_transient(operation) == "ok"

    records = [
        record
        for record in caplog.records
        if record.name == "rework.state.retry" and record.levelno >= logging.WARNING
    ]
    assert calls == 3
    assert len(records) == 2, "one warning per retry, not one per attempt"
    assert "attempt 1 of 3" in records[0].getMessage()
    assert "attempt 2 of 3" in records[1].getMessage()
    assert all("40P01" in record.getMessage() for record in records)


async def test_serialization_failure_is_retried_through_to_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delays = record_sleeps(monkeypatch)
    calls = 0

    async def operation() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise DbError("could not serialize access due to concurrent update", "40001")
        return "sentinel"

    assert await retry_transient(operation) == "sentinel"
    assert calls == 2
    assert len(delays) == 1


@pytest.mark.parametrize(
    "exc",
    [
        DbError("duplicate key value violates unique constraint", "23505"),
        ValueError("bad request shape"),
        DbError('syntax error at or near "SELEC"', "42601"),
    ],
    ids=["unique_violation", "value_error", "syntax_error"],
)
async def test_logical_error_is_never_retried(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, exc: Exception
) -> None:
    delays = record_sleeps(monkeypatch)
    calls = 0

    async def operation() -> str:
        nonlocal calls
        calls += 1
        raise exc

    with (
        caplog.at_level(logging.WARNING, logger="rework.state.retry"),
        pytest.raises(type(exc)) as caught,
    ):
        await retry_transient(operation)

    assert caught.value is exc, "the caller sees the original error, not a wrapper"
    assert calls == 1
    assert delays == []
    assert [r for r in caplog.records if r.name == "rework.state.retry"] == []


async def test_exhaustion_raises_transient_database_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delays = record_sleeps(monkeypatch)
    original = DbError("deadlock detected", "40P01")
    calls = 0

    async def operation() -> str:
        nonlocal calls
        calls += 1
        raise original

    with pytest.raises(TransientDatabaseError) as caught:
        await retry_transient(operation, label="update chats")

    raised = caught.value
    assert MAX_ATTEMPTS == 3
    assert calls == 3
    assert raised.attempts == 3
    assert raised.sqlstate == "40P01"
    assert "update chats" in str(raised)
    assert is_transient_db_error(raised), "the daemon requeues on exactly this classification"
    assert raised.__cause__ is original
    assert len(delays) == 2


def _runtime_error_wrapping(exc: BaseException) -> Exception:
    """`exc`, but raised `from` a plain RuntimeError (the store's wrapper shape)."""
    try:
        raise exc
    except BaseException as cause:
        try:
            raise RuntimeError("store call failed") from cause
        except RuntimeError as wrapper:
            return wrapper
    raise AssertionError("unreachable")  # pragma: no cover


def test_classification_walks_the_chain() -> None:
    assert is_transient_db_error(_runtime_error_wrapping(DbError("deadlock detected", "40P01")))

    class WithOrig(Exception):
        """psycopg's wrapper shape: the driver error sits on `.orig`."""

        def __init__(self) -> None:
            super().__init__("wrapped by the driver")
            self.orig = DbError("could not serialize access", "40001")

    assert is_transient_db_error(WithOrig())
    # A message that merely mentions the SQLSTATE is not an error we can retry.
    assert not is_transient_db_error(RuntimeError("deadlock detected (SQLSTATE 40P01)"))


async def test_cleanup_runs_between_attempts_not_after_the_last(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record_sleeps(monkeypatch)
    calls = 0
    cleanups: list[int] = []

    async def cleanup() -> None:
        cleanups.append(calls)

    async def operation() -> str:
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise DbError("deadlock detected", "40P01")
        return "ok"

    assert await retry_transient(operation, cleanup=cleanup) == "ok"
    assert calls == 3
    # Observed contract: cleanup runs after every FAILED attempt, so the aborted
    # transaction is rolled back before the next try (recorded while the failed
    # attempt is still the last call), and NOT after the successful return.
    assert cleanups == [1, 2]
