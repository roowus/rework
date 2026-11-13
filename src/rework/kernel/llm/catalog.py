"""ModelCatalog: the harness-style model list the orchestrator chooses from.

Entries carry tier (higher is more capable), costs, and caps. Promotion walks up
tiers within the same provider (cross-provider promotion is config-gated, ADR 0001).
"""

from __future__ import annotations

import re
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from rework.kernel.llm.types import ChatRequest, ChatResult

# hosts that mean "this machine": a catalog entry pointing at one of them is
# callable only where it was written, never on a machine it is synced to
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class UnknownModelError(KeyError):
    pass


# the env var a provider's adapter looks in when an entry names none
DEFAULT_KEY_ENVS = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}


def api_key_env_name(entry: ModelEntry) -> str:
    """The env var holding this entry's key ("" when the provider needs none).
    Mirrors build_provider's lookup, so `sync-config` can tell which keys an
    entry actually needs."""
    if entry.provider == "fake":
        return ""
    return entry.api_key_env or DEFAULT_KEY_ENVS.get(entry.provider, "")


def base_url_host(base_url: str | None) -> str:
    """The host of a base_url, lowercased; "" when there is none.

    Handles a full URL (scheme + port), a bare `host:port`, and a bare host.
    urlsplit cannot parse a bare `::1`, so that case is split by hand."""
    text = (base_url or "").strip()
    if not text:
        return ""
    try:
        host = (urlsplit(text if "//" in text else "//" + text).hostname or "").lower()
    except ValueError:  # unparseable (a bare IPv6 literal): fall through
        host = ""
    if host:
        return host
    head = text.split("/")[0]
    if head.startswith("["):  # [::1]:20128
        return head[1 : head.index("]")].lower() if "]" in head else head[1:].lower()
    if head.count(":") == 1:  # host:port, not IPv6
        head = head.split(":")[0]
    return head.lower()


def is_loopback_url(base_url: str | None) -> bool:
    """Whether base_url points at THIS machine (localhost / 127.0.0.1 / ::1).

    Used by `rework sync-config`: an entry on a loopback endpoint is reachable
    only on the machine whose catalog it is, so syncing it to another host
    would hand that host models it can never call."""
    return base_url_host(base_url) in LOOPBACK_HOSTS


_VERSION_RE = re.compile(r"\d+")


def version_key(catalog_id: str) -> tuple[int, ...]:
    """The numeric version segments of a catalog id, as a comparable tuple.

    `claude-sonnet-5-5` -> (5, 5), which sorts above `claude-sonnet-5` -> (5,);
    a dated id extends its undated sibling's tuple (`...-20250929` -> (5, 20250929)
    > (5,)), so a dated snapshot reads as newer. An id with no digits (`flash`,
    `pro`) gives () and never raises, so ranking is always total."""
    return tuple(int(part) for part in _VERSION_RE.findall(catalog_id))


def _negated_version(catalog_id: str) -> tuple[int, ...]:
    return tuple(-part for part in version_key(catalog_id))


def _price(entry: ModelEntry) -> tuple[float, float]:
    return (entry.cost_in_per_mtok, entry.cost_out_per_mtok)


def _rank_cheap(entry: ModelEntry) -> tuple[Any, ...]:
    """Cheapest first: low tier, low price, then the NEWEST version, then id
    (deterministic even when two entries tie on everything)."""
    return (entry.tier, *_price(entry), _negated_version(entry.id), entry.id)


def _rank_strong(entry: ModelEntry) -> tuple[Any, ...]:
    """Strongest first: high tier, then low price, then the NEWEST version,
    then id."""
    return (entry.tier, *(-c for c in _price(entry)), version_key(entry.id), entry.id)


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
        env = api_key_env_name(self)
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

    def usable(self) -> list[ModelEntry]:
        """Entries this process can actually call, real providers first: real
        + keyed, else anything keyed, else the whole catalog (a deployment of
        fakes only, as in tests). ONE implementation for every "which model may
        I use" site (the orchestrator's own model, role defaults, TUI knob)."""
        entries = self.all()
        real = [e for e in entries if e.provider != "fake" and e.has_credentials()]
        return real or [e for e in entries if e.has_credentials()] or entries

    def cheapest_usable(self) -> ModelEntry | None:
        """The cheapest entry this process can call. The fallback for a
        configured default model that is missing or has no key here."""
        entries = self.usable()
        return min(entries, key=_rank_cheap) if entries else None

    def strongest_usable(self) -> ModelEntry | None:
        """The strongest entry this process can call: highest tier, then lower
        price, then the newest version. The fallback for a configured
        escalation model that is missing or has no key here."""
        entries = self.usable()
        return max(entries, key=_rank_strong) if entries else None

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
