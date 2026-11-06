"""ModelCatalog: the harness-style model list the orchestrator chooses from.

Entries carry tier (higher is more capable), costs, and caps. Promotion walks up
tiers within the same provider (cross-provider promotion is config-gated, ADR 0001).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from rework.kernel.llm.types import ChatRequest, ChatResult


class UnknownModelError(KeyError):
    pass


class ModelEntry(BaseModel):
    id: str  # catalog key, e.g. "haiku"; referenced by RoleSpec/plan
    provider: str  # adapter name: "anthropic" | "openai" | "openai-compatible"
    model: str  # provider-side model id, e.g. "claude-haiku-4-5"
    tier: int = 0  # higher = more capable; promotion moves up within a provider
    cost_in_per_mtok: float = 0.0
    cost_out_per_mtok: float = 0.0
    # cached input price; None = half the input price (Fireworks' default)
    cost_cached_per_mtok: float | None = None
    capabilities: set[str] = Field(default_factory=set)  # e.g. {"vision", "thinking"}
    context_window: int = 200_000
    max_output: int = 8_192
    base_url: str | None = None  # openai-compatible endpoints
    api_key_env: str = ""  # env var holding the key; empty = provider default

    def estimate_cost(self, input_tokens: int, output_tokens: int, cached_tokens: int = 0) -> float:
        """`input_tokens` INCLUDES cached ones (adapters normalize to that);
        the cached part bills at the cached rate."""
        cached = max(0, min(cached_tokens, input_tokens))
        cached_rate = (
            self.cost_cached_per_mtok
            if self.cost_cached_per_mtok is not None
            else self.cost_in_per_mtok * 0.5
        )
        return (
            (input_tokens - cached) * self.cost_in_per_mtok
            + cached * cached_rate
            + output_tokens * self.cost_out_per_mtok
        ) / 1e6

    def has_credentials(self) -> bool:
        """Whether THIS process can call the model (its key is in the env).
        Mirrors build_provider's lookup. Found live: an orchestrator picked a
        catalog model whose key existed only on another machine, and the
        role died before its first call."""
        import os

        if self.provider == "fake":
            return True
        default = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}
        env = self.api_key_env or default.get(self.provider, "")
        return bool(env and os.environ.get(env))


@runtime_checkable
class ModelProvider(Protocol):
    """One adapter per wire format. name matches ModelEntry.provider.
    runtime_checkable so pydantic can validate RoleBuildCtx.provider by instance."""

    name: str

    async def complete(self, request: ChatRequest) -> ChatResult: ...


class ModelCatalog:
    def __init__(self, entries: list[ModelEntry]) -> None:
        self._entries: dict[str, ModelEntry] = {}
        for entry in entries:
            if entry.id in self._entries:
                raise ValueError(f"duplicate catalog id: {entry.id}")
            self._entries[entry.id] = entry

    def resolve(self, catalog_id: str) -> ModelEntry:
        try:
            return self._entries[catalog_id]
        except KeyError:
            raise UnknownModelError(
                f"catalog id {catalog_id!r} not in {[e.id for e in self._entries.values()]}"
            ) from None

    def all(self) -> list[ModelEntry]:
        return list(self._entries.values())

    def cheapest(self) -> ModelEntry | None:
        """Lowest-tier entry (ties: first registered). The stub/echo fallback:
        a role whose default model id is absent from a deployment catalog gets
        the cheapest real model rather than a hard failure."""
        if not self._entries:
            return None
        return min(self._entries.values(), key=lambda e: e.tier)

    def promote(self, catalog_id: str, cross_provider: bool = False) -> ModelEntry | None:
        """Next tier up. Same provider unless cross_provider is enabled and a
        higher tier exists elsewhere; returns None when no promotion exists
        (orchestrator policy turns that into an ASK_HUMAN escalation)."""
        current = self.resolve(catalog_id)
        candidates = [
            e
            for e in self._entries.values()
            if e.tier > current.tier and (cross_provider or e.provider == current.provider)
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda e: e.tier)

    @classmethod
    def from_config(cls, data: list[dict[str, Any]]) -> ModelCatalog:
        return cls([ModelEntry(**item) for item in data])
