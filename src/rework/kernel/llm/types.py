"""Provider-agnostic chat types. Adapters translate these to and from wire formats.

Messages are plain dicts: {"role": "user"|"assistant"|"tool", "content": str, plus
"tool_calls" on assistant turns and "tool_call_id"/"name" on tool turns}. Keeping
them dumb makes checkpointed histories portable across adapters (within a provider
family; cross-provider promotion needs a normalization pass, ADR 0001).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ToolSpec(BaseModel):
    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0  # prompt tokens served from cache (0 when unknown)


class ChatRequest(BaseModel):
    model: str  # provider-side model id, not the catalog key
    system: str | None = None
    messages: list[dict[str, Any]] = Field(default_factory=list)
    tools: list[ToolSpec] = Field(default_factory=list)
    max_tokens: int = 4096
    temperature: float | None = None


class ChatResult(BaseModel):
    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    stop_reason: str = "end_turn"
