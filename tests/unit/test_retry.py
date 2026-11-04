"""Retry wrapper: transient errors get retried with backoff, non-transient
fail immediately, and exhaustion re-raises the last error."""

from __future__ import annotations

import pytest

from rework.kernel.llm.adapters.retry import RetryingProvider, _is_transient
from rework.kernel.llm.types import ChatRequest, ChatResult


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
