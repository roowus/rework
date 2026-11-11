"""Provider-agnostic chat types. Adapters translate these to and from wire formats.

Messages are plain dicts: {"role": "user"|"assistant"|"tool", "content": str, plus
"tool_calls" on assistant turns and "tool_call_id"/"name" on tool turns}. Keeping
them dumb makes checkpointed histories portable across adapters (within a provider
family; cross-provider promotion needs a normalization pass, ADR 0001).

Images ride as a `images` list of FILE PATHS on the user message. Paths, not
bytes: message dicts land in the LangGraph checkpoint and get replayed, and
base64 blobs there would bloat every checkpoint write. Adapters read the file
at request time and encode on the wire.
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
    # Reasoning effort: "low" | "medium" | "high". How it travels is the
    # adapter's business (Anthropic thinking budget, OpenAI reasoning_effort);
    # adapters that cannot express it ignore it. None = provider default.
    effort: str | None = None
    # Sticky routing key for prompt caching (one per role run / orchestrator).
    # Providers cache by exact prefix but only within one replica; without a
    # session key consecutive turns land on random replicas and miss (found
    # live: 6k of 8.9M input tokens cached). Adapters that cannot use it ignore it.
    session_id: str | None = None


class ChatResult(BaseModel):
    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    stop_reason: str = "end_turn"
    # provider reasoning blocks that must go back with this turn (Anthropic:
    # thinking/redacted_thinking blocks are required on the next request of a
    # tool loop). Callers store them on the assistant message as
    # "thinking_blocks"; adapters that do not use them ignore the key.
    thinking_blocks: list[dict[str, Any]] = Field(default_factory=list)
