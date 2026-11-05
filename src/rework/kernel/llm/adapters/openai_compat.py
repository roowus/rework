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
        stop_reason="tool_calls" if tool_calls else "end_turn",
    )


class OpenAICompatProvider:
    """Works with any /chat/completions endpoint via base_url. Client timeout
    180s: a silently-dropped upstream must surface as APITimeoutError (which
    RetryingProvider treats as transient), not hang for the SDK default of
    10 minutes. SDK-level retries stay off; RetryingProvider owns retries."""

    name = "openai-compatible"

    def __init__(self, api_key: str, base_url: str | None = None, name: str | None = None) -> None:
        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=180.0, max_retries=0)
        if name:
            self.name = name

    async def complete(self, request: ChatRequest) -> ChatResult:
        payload = translate_request(request)
        response = await self._client.chat.completions.create(**payload)
        return translate_response(response.model_dump())


class OpenAIProvider(OpenAICompatProvider):
    name = "openai"

    def __init__(self, api_key: str | None = None) -> None:
        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(api_key=api_key) if api_key else AsyncOpenAI()
