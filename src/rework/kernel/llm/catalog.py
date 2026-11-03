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
    capabilities: set[str] = Field(default_factory=set)  # e.g. {"vision", "thinking"}
    context_window: int = 200_000
    max_output: int = 8_192
    base_url: str | None = None  # openai-compatible endpoints
    api_key_env: str = ""  # env var holding the key; empty = provider default

    def estimate_cost(self, input_tokens: int, output_tokens: int) -> float:
        return (input_tokens * self.cost_in_per_mtok + output_tokens * self.cost_out_per_mtok) / 1e6


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
