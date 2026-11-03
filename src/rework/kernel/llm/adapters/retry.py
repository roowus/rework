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

_TRANSIENT_MARKERS = ("502", "503", "504", "429", "timeout", "connect", "overloaded")


def _is_transient(exc: Exception) -> bool:
    name = type(exc).__name__.lower()
    if "connectionerror" in name or "apitimeout" in name or "overloaded" in name:
        return True
    if "statuserror" in name:
        code = getattr(exc, "status_code", None)
        return code is None or code >= 500 or code == 429
    text = str(exc).lower()
    return any(marker in text for marker in _TRANSIENT_MARKERS)


class RetryingProvider:
    """Decorates any ModelProvider with bounded retry + backoff on transient
    errors. Non-transient errors (auth, bad request) fail immediately."""

    name = "retrying"

    def __init__(self, inner: Any, attempts: int = 3, backoff_s: float = 2.0) -> None:
        self._inner = inner
        self._attempts = attempts
        self._backoff = backoff_s
        self.name = getattr(inner, "name", "retrying")

    async def complete(self, request: ChatRequest) -> ChatResult:
        last: Exception | None = None
        for attempt in range(1, self._attempts + 1):
            try:
                return await self._inner.complete(request)
            except Exception as exc:
                if not _is_transient(exc):
                    raise
                last = exc
                log.warning(
                    "transient provider error (attempt %d/%d): %s",
                    attempt,
                    self._attempts,
                    str(exc)[:120],
                )
                if attempt < self._attempts:
                    await asyncio.sleep(self._backoff * attempt)
        assert last is not None
        raise last


def with_retries(provider: Any, attempts: int = 3, backoff_s: float = 2.0) -> Any:
    return RetryingProvider(provider, attempts=attempts, backoff_s=backoff_s)
