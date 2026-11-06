"""Retry wrapper for providers: transient upstream failures (5xx, connect
timeouts) get a few shots with backoff instead of failing the run. Found in
the first live mission: one router 503 killed the investigator's run.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from rework.kernel.llm.types import ChatRequest, ChatResult

log = logging.getLogger("rework.llm.retry")

_TRANSIENT_MARKERS = (
    "500",  # found live: a Fireworks 500 killed an auditor ($0.51 wasted) unretried
    "502",
    "503",
    "504",
    "429",
    "timeout",
    "connect",
    "overloaded",
    "internal server error",
)


def _is_transient(exc: Exception) -> bool:
    name = type(exc).__name__.lower()
    if (
        "connectionerror" in name
        or "apitimeout" in name
        or "overloaded" in name
        or "internalservererror" in name
    ):
        return True
    if "statuserror" in name:
        code = getattr(exc, "status_code", None)
        return code is None or code >= 500 or code == 429
    text = str(exc).lower()
    return any(marker in text for marker in _TRANSIENT_MARKERS)


class RetryingProvider:
    """Decorates any ModelProvider with bounded retry + backoff on transient
    errors. Non-transient errors (auth, bad request) fail immediately.
    429s get a deeper pool + longer backoff: rate-limit windows outlast a
    5xx blip (found live: the auditor burned 3+1 attempts in one 429 storm)."""

    name = "retrying"

    def __init__(self, inner: Any, attempts: int = 3, backoff_s: float = 2.0) -> None:
        self._inner = inner
        self._attempts = attempts
        self._rate_attempts = attempts * 3  # 429: 9 shots when base is 3
        self._backoff = backoff_s
        self.name = getattr(inner, "name", "retrying")

    @staticmethod
    def _is_rate_limit(exc: Exception) -> bool:
        if getattr(exc, "status_code", None) == 429:
            return True
        text = str(exc).lower()
        return "429" in text and ("rate limit" in text or "requests" in text)

    async def complete(self, request: ChatRequest) -> ChatResult:
        last: Exception | None = None
        attempt = 0
        limit = self._attempts
        while attempt < limit:
            attempt += 1
            try:
                return await self._inner.complete(request)
            except Exception as exc:
                if not _is_transient(exc):
                    raise
                last = exc
                if self._is_rate_limit(exc):
                    limit = self._rate_attempts  # extend the pool for 429s
                log.warning(
                    "transient provider error (attempt %d/%d): %s",
                    attempt,
                    limit,
                    str(exc)[:120],
                )
                if attempt < limit:
                    # rate limits need longer waits than 5xx blips
                    backoff = self._backoff * (attempt * 2 if self._is_rate_limit(exc) else attempt)
                    await asyncio.sleep(backoff)
        assert last is not None
        raise last


def with_retries(provider: Any, attempts: int = 3, backoff_s: float = 2.0) -> Any:
    return RetryingProvider(provider, attempts=attempts, backoff_s=backoff_s)
