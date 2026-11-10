"""Every real model call, logged: model, latency, tokens, tool calls, errors.

Wraps whatever build_provider returns, so roles, the mission orchestrator and
the chat all leave the same trail in ~/.rework/logs (rework.logs). Mission/run
tags come from the caller's log context; prompts are never logged."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from rework.kernel.llm.types import ChatRequest, ChatResult

log = logging.getLogger("rework.llm")


def hard_timeout_s(max_tokens: int | None = None) -> float:
    """Upper bound for one model call, whatever the client thinks.
    REWORK_MODEL_TIMEOUT_S, when set, is the operator's override; otherwise
    300s or the client's own timeout plus a margin for long answers, so the
    deadline never cuts a call the client would allow."""
    from rework.kernel.llm.adapters.openai_compat import client_timeout_s

    override = os.environ.get("REWORK_MODEL_TIMEOUT_S")
    if override:
        return float(override)
    return max(300.0, client_timeout_s(max_tokens) + 30.0)


class LoggedProvider:
    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.name = getattr(inner, "name", "provider")

    def __getattr__(self, item: str) -> Any:
        return getattr(self._inner, item)

    async def complete(self, request: ChatRequest) -> ChatResult:
        start = time.perf_counter()
        try:
            # HARD deadline (found live: a Fireworks call stayed in flight 15+
            # minutes despite the client's 180s timeout, which does not bound a
            # request whose server keeps the connection alive)
            result: ChatResult = await asyncio.wait_for(
                self._inner.complete(request), timeout=hard_timeout_s(request.max_tokens)
            )
        except Exception as exc:
            log.warning(
                "model call failed: %s: %s",
                type(exc).__name__,
                str(exc)[:300],
                extra={"model": request.model, "ms": int((time.perf_counter() - start) * 1000)},
            )
            raise
        usage = result.usage
        log.info(
            "model call ok: %d tool calls, stop=%s",
            len(result.tool_calls),
            result.stop_reason,
            extra={
                "model": request.model,
                "ms": int((time.perf_counter() - start) * 1000),
                "tokens_in": usage.input_tokens,
                "tokens_out": usage.output_tokens,
            },
        )
        return result
