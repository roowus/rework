"""OpenAI-compatible adapter. One wire format covers OpenAI, DeepInfra, z.ai,
DeepSeek, and anything else that speaks /chat/completions; base_url selects the
endpoint (ADR 0001). OpenAIProvider is this adapter with the default base_url."""

from __future__ import annotations

import json
from typing import Any

from rework.kernel.llm.types import ChatRequest, ChatResult, ToolCall, Usage


def translate_request(request: ChatRequest) -> dict[str, Any]:
    """Generic messages to the /chat/completions wire format."""
    messages: list[dict[str, Any]] = []
    if request.system:
        messages.append({"role": "system", "content": request.system})
    for msg in request.messages:
        role = msg["role"]
        if role == "tool":
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": msg["tool_call_id"],
                    "content": msg["content"],
                }
            )
        elif role == "assistant" and msg.get("tool_calls"):
            messages.append(
                {
                    "role": "assistant",
                    "content": msg.get("content") or None,
                    "tool_calls": [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {
                                "name": tc["name"],
                                "arguments": json.dumps(tc["arguments"])
                                if isinstance(tc["arguments"], dict)
                                else tc["arguments"],
                            },
                        }
                        for tc in msg["tool_calls"]
                    ],
                }
            )
        elif msg.get("images"):
            # OpenAI content parts: text first, then image_url data URIs.
            from rework.kernel.llm.adapters.images import load_image

            parts: list[dict[str, Any]] = []
            if msg.get("content"):
                parts.append({"type": "text", "text": msg["content"]})
            for path in msg["images"]:
                data, mime = load_image(str(path))
                parts.append(
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}}
                )
            messages.append({"role": role, "content": parts})
        else:
            messages.append({"role": role, "content": msg["content"]})

    payload: dict[str, Any] = {
        "model": request.model,
        "messages": messages,
        "max_tokens": request.max_tokens,
    }
    if request.temperature is not None:
        payload["temperature"] = request.temperature
    if request.session_id:
        # OpenAI-standard field; Fireworks uses it for sticky replica routing,
        # which is what makes prompt caching hit (docs: prompt-caching)
        payload["user"] = request.session_id
    if request.tools:
        payload["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.input_schema or {"type": "object", "properties": {}},
                },
            }
            for t in request.tools
        ]
    if request.effort:
        # OpenAI-style reasoning effort. Servers that don't know the key ignore
        # it; reasoning endpoints reject max_tokens in favour of
        # max_completion_tokens, so send the modern name when effort is set.
        payload["reasoning_effort"] = request.effort
        payload["max_completion_tokens"] = payload.pop("max_tokens", None)
    return payload


def translate_response(data: Any) -> ChatResult:
    choice = data["choices"][0]
    message = choice.get("message", {})
    tool_calls = [
        ToolCall(
            id=tc.get("id", ""),
            name=tc["function"]["name"],
            arguments=json.loads(tc["function"].get("arguments") or "{}"),
        )
        for tc in message.get("tool_calls") or []
    ]
    usage = data.get("usage", {})
    details = usage.get("prompt_tokens_details") or {}
    cached = details.get("cached_tokens", 0) or 0
    return ChatResult(
        text=message.get("content") or "",
        tool_calls=tool_calls,
        usage=Usage(
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            cached_tokens=int(cached),
        ),
        # found live: every reply without tool calls was reported as end_turn,
        # so a report cut off at max_tokens counted as finished
        stop_reason=(
            "tool_calls"
            if tool_calls
            else "max_tokens"
            if choice.get("finish_reason") == "length"
            else "end_turn"
        ),
    )


def client_timeout_s(max_tokens: int | None) -> float:
    """Long enough for the answer asked for at a slow-but-healthy 35 tokens/s,
    never under 180s. Found live: kimi-k3 answers of 8192 tokens took 114-132s
    at 60-70 tok/s; slightly slower ones crossed a flat 180s, and re-sending
    the same request regenerated the same long answer into the same timeout
    (what looked like a provider 'stall' on 6% of calls)."""
    return max(180.0, 60.0 + float(max_tokens or 0) / 35.0)


IDLE_TIMEOUT_S = 90.0  # no bytes for this long = a dead request


async def assemble_stream(stream: Any) -> dict[str, Any]:
    """Chat-completion chunks back into the non-streamed response shape."""
    text: list[str] = []
    calls: dict[int, dict[str, str]] = {}
    finish: str | None = None
    usage: Any = None
    async for chunk in stream:
        if getattr(chunk, "usage", None):
            usage = chunk.usage
        for choice in chunk.choices or []:
            delta = choice.delta
            if delta.content:
                text.append(delta.content)
            for tc in delta.tool_calls or []:
                slot = calls.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
                if tc.id:
                    slot["id"] = tc.id
                fn = tc.function
                if fn is not None and fn.name and not slot["name"]:
                    slot["name"] = fn.name
                if fn is not None and fn.arguments:
                    slot["arguments"] += fn.arguments
            if choice.finish_reason:
                finish = choice.finish_reason
    tool_calls = [
        {"id": c["id"], "function": {"name": c["name"], "arguments": c["arguments"]}}
        for _, c in sorted(calls.items())
    ]
    return {
        "choices": [
            {
                "message": {"content": "".join(text), "tool_calls": tool_calls},
                "finish_reason": finish,
            }
        ],
        "usage": usage.model_dump() if usage is not None else {},
    }


class OpenAICompatProvider:
    """Works with any /chat/completions endpoint via base_url. Streams every
    call with an idle timeout (see complete): a silently-dropped upstream
    surfaces as APITimeoutError (RetryingProvider treats it as transient)
    within IDLE_TIMEOUT_S. SDK-level retries stay off; RetryingProvider owns
    retries."""

    name = "openai-compatible"

    def __init__(self, api_key: str, base_url: str | None = None, name: str | None = None) -> None:
        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=180.0, max_retries=0)
        if name:
            self.name = name

    async def complete(self, request: ChatRequest) -> ChatResult:
        """Streamed, with an IDLE timeout: a long answer keeps sending tokens
        (reasoning included; measured on Fireworks: chunk gaps under 1s), a
        dead request goes silent. A flat timeout could not tell them apart:
        180s cut real 8192-token answers, a longer one let a stalled call
        hang for minutes. The hard deadline (LoggedProvider) bounds the total."""
        from openai import Timeout

        payload = translate_request(request)
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
        stream = await self._client.chat.completions.create(
            **payload, timeout=Timeout(IDLE_TIMEOUT_S, connect=30.0)
        )
        return translate_response(await assemble_stream(stream))


class OpenAIProvider(OpenAICompatProvider):
    name = "openai"

    def __init__(self, api_key: str | None = None) -> None:
        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(api_key=api_key) if api_key else AsyncOpenAI()
