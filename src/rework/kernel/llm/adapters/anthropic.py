"""Anthropic adapter. Translation is pure and unit-tested; the client call is thin."""

from __future__ import annotations

import json
from typing import Any

from rework.kernel.llm.types import ChatRequest, ChatResult, ToolCall, Usage

_STOP_MAP = {
    "end_turn": "end_turn",
    "stop_sequence": "stop_sequence",
    "max_tokens": "max_tokens",
    "tool_use": "tool_calls",
    "refusal": "refusal",
}


def translate_request(request: ChatRequest) -> dict[str, Any]:
    """Generic messages to the Anthropic Messages wire format."""
    messages: list[dict[str, Any]] = []
    for msg in request.messages:
        role = msg["role"]
        if role == "tool":
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": msg["tool_call_id"],
                            "content": msg["content"],
                        }
                    ],
                }
            )
        elif role == "assistant" and msg.get("tool_calls"):
            blocks: list[dict[str, Any]] = []
            if msg.get("content"):
                blocks.append({"type": "text", "text": msg["content"]})
            for tc in msg["tool_calls"]:
                args = tc["arguments"]
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": tc["id"],
                        "name": tc["name"],
                        "input": args if isinstance(args, dict) else json.loads(args),
                    }
                )
            messages.append({"role": "assistant", "content": blocks})
        else:
            messages.append({"role": role, "content": msg["content"]})

    payload: dict[str, Any] = {
        "model": request.model,
        "max_tokens": request.max_tokens,
        "messages": messages,
    }
    if request.system:
        payload["system"] = request.system
    if request.temperature is not None:
        payload["temperature"] = request.temperature
    if request.tools:
        payload["tools"] = [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema or {"type": "object", "properties": {}},
            }
            for t in request.tools
        ]
    return payload


def translate_response(data: Any) -> ChatResult:
    """Anthropic response JSON to ChatResult."""
    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    for block in data.get("content", []):
        if block.get("type") == "text":
            text_parts.append(block.get("text", ""))
        elif block.get("type") == "tool_use":
            tool_calls.append(
                ToolCall(id=block["id"], name=block["name"], arguments=block.get("input") or {})
            )
    usage = data.get("usage", {})
    return ChatResult(
        text="".join(text_parts),
        tool_calls=tool_calls,
        usage=Usage(
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
        ),
        stop_reason=_STOP_MAP.get(data.get("stop_reason", "end_turn"), "end_turn"),
    )


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str | None = None) -> None:
        from anthropic import AsyncAnthropic

        self._client = AsyncAnthropic(api_key=api_key) if api_key else AsyncAnthropic()

    async def complete(self, request: ChatRequest) -> ChatResult:
        payload = translate_request(request)
        response = await self._client.messages.create(**payload)
        return translate_response(response.model_dump())
