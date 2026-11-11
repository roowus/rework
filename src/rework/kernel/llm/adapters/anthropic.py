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


def _mark_cache(payload: dict[str, Any]) -> None:
    """Anthropic caches only up to explicit breakpoints: one after the system
    prompt (static per run) and one on the newest message, so each turn reads
    everything before it from cache. Requests with a session_id are the long
    agent loops where this pays (cache reads bill at a tenth of input)."""
    system = payload.get("system")
    if isinstance(system, str):
        payload["system"] = [
            {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
        ]
    messages = payload.get("messages") or []
    if not messages:
        return
    last = messages[-1]
    content = last.get("content")
    if isinstance(content, str):
        last["content"] = [
            {"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}
        ]
    elif isinstance(content, list) and content and isinstance(content[-1], dict):
        content[-1] = {**content[-1], "cache_control": {"type": "ephemeral"}}


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
            # thinking blocks first, exactly as received (the API checks them)
            blocks: list[dict[str, Any]] = list(msg.get("thinking_blocks") or [])
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
        elif msg.get("images"):
            # Anthropic content blocks: images first, then the words that
            # refer to them (the usual reading order for a screenshot + note).
            from rework.kernel.llm.adapters.images import load_image

            content: list[dict[str, Any]] = []
            for path in msg["images"]:
                data, mime = load_image(str(path))
                content.append(
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": mime, "data": data},
                    }
                )
            if msg.get("content"):
                content.append({"type": "text", "text": msg["content"]})
            messages.append({"role": role, "content": content})
        else:
            messages.append({"role": role, "content": msg["content"]})

    payload: dict[str, Any] = {
        "model": request.model,
        "max_tokens": request.max_tokens,
        "messages": messages,
    }
    if request.system:
        payload["system"] = request.system
    if request.session_id:
        _mark_cache(payload)
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
    if request.effort:
        if request.model in _BUDGET_THINKING:
            # older models: an extended-thinking budget. The API requires
            # max_tokens > budget_tokens and temperature unset while thinking.
            budget = {"low": 2_048, "medium": 8_192, "high": 24_576}.get(request.effort, 8_192)
            payload["thinking"] = {"type": "enabled", "budget_tokens": budget}
            payload["max_tokens"] = max(int(payload.get("max_tokens") or 0), budget + 1_024)
        else:
            # current models refuse a fixed budget (found live on
            # claude-sonnet-5): adaptive thinking, effort in output_config
            payload["thinking"] = {"type": "adaptive"}
            payload["output_config"] = {"effort": request.effort}
        payload.pop("temperature", None)
    return payload


# models that rejected adaptive thinking and take a budget instead (learned
# at runtime, see AnthropicProvider.complete)
_BUDGET_THINKING: set[str] = set()


def translate_response(data: Any) -> ChatResult:
    """Anthropic response JSON to ChatResult."""
    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    thinking: list[dict[str, Any]] = []
    for block in data.get("content", []):
        if block.get("type") in ("thinking", "redacted_thinking"):
            thinking.append({k: v for k, v in block.items() if v is not None})
        elif block.get("type") == "text":
            text_parts.append(block.get("text", ""))
        elif block.get("type") == "tool_use":
            tool_calls.append(
                ToolCall(id=block["id"], name=block["name"], arguments=block.get("input") or {})
            )
    usage = data.get("usage", {})
    cached = usage.get("cache_read_input_tokens", 0) or 0
    written = usage.get("cache_creation_input_tokens", 0) or 0
    return ChatResult(
        text="".join(text_parts),
        tool_calls=tool_calls,
        usage=Usage(
            # normalized: input INCLUDES cached tokens (OpenAI semantics);
            # Anthropic reports cache reads and writes separately
            input_tokens=(usage.get("input_tokens", 0) or 0) + cached + written,
            output_tokens=usage.get("output_tokens", 0),
            cached_tokens=int(cached),
        ),
        stop_reason=_STOP_MAP.get(data.get("stop_reason", "end_turn"), "end_turn"),
        thinking_blocks=thinking,
    )


ANTHROPIC_API = "https://api.anthropic.com"
IDLE_TIMEOUT_S = 90.0  # no bytes for this long = a dead request


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        from anthropic import AsyncAnthropic

        # the endpoint comes from rework's catalog, never ANTHROPIC_BASE_URL:
        # found live, the owner's shell points that at a local router for
        # Claude Code, and rework's Anthropic calls went there and failed 401
        url = base_url or ANTHROPIC_API
        self._client = (
            AsyncAnthropic(api_key=api_key, base_url=url, timeout=180.0, max_retries=0)
            if api_key
            else AsyncAnthropic(base_url=url, timeout=180.0, max_retries=0)
        )

    async def complete(self, request: ChatRequest) -> ChatResult:
        """Streamed, so long answers are never refused or cut by a flat
        timeout (as for openai_compat; the SDK idles out after 90s of
        silence). A model that rejects adaptive thinking is remembered and
        retried once with a thinking budget."""
        from anthropic import BadRequestError

        try:
            return await self._stream(translate_request(request))
        except BadRequestError as exc:
            text = str(exc)
            if not request.effort or request.model in _BUDGET_THINKING or "adaptive" not in text:
                raise
            _BUDGET_THINKING.add(request.model)
            return await self._stream(translate_request(request))

    async def _stream(self, payload: dict[str, Any]) -> ChatResult:
        from anthropic import Timeout

        async with self._client.messages.stream(
            **payload, timeout=Timeout(IDLE_TIMEOUT_S, connect=30.0)
        ) as stream:
            message = await stream.get_final_message()
        return translate_response(message.model_dump())
