"""Provider adapters + the factory that builds one from a ModelEntry + config."""

from __future__ import annotations

import os
from typing import Any

from rework.kernel.llm.adapters.anthropic import AnthropicProvider
from rework.kernel.llm.adapters.fake import FakeProvider
from rework.kernel.llm.adapters.openai_compat import OpenAICompatProvider, OpenAIProvider
from rework.kernel.llm.catalog import ModelEntry, ModelProvider

__all__ = [
    "AnthropicProvider",
    "FakeProvider",
    "OpenAICompatProvider",
    "OpenAIProvider",
    "build_provider",
]


def build_provider(entry: ModelEntry, extra: dict[str, Any] | None = None) -> ModelProvider:
    """Build the adapter an entry names, wrapped so every call is logged."""
    provider = _build(entry, extra)
    if entry.provider == "fake":
        return provider
    from rework.kernel.llm.adapters.logged import LoggedProvider

    return LoggedProvider(provider)


def _build(entry: ModelEntry, extra: dict[str, Any] | None = None) -> ModelProvider:
    """Build the adapter an entry names. Keys come from the entry's api_key_env or
    provider defaults (ANTHROPIC_API_KEY / OPENAI_API_KEY). extra carries env-independent
    credentials (e.g. per-endpoint keys for openai-compatible providers)."""
    if entry.provider == "fake":
        # REWORK_FAKE_SCRIPT drives a real daemon in e2e runs; unset = empty fake
        from rework.kernel.llm.adapters.fake import scripted_from_env

        return scripted_from_env()
    if entry.provider == "anthropic":
        key = os.environ.get(entry.api_key_env or "ANTHROPIC_API_KEY")
        return AnthropicProvider(api_key=key, base_url=entry.base_url)
    if entry.provider == "openai":
        key = os.environ.get(entry.api_key_env or "OPENAI_API_KEY")
        return OpenAIProvider(api_key=key)
    if entry.provider == "openai-compatible":
        key = os.environ.get(entry.api_key_env, "") if entry.api_key_env else ""
        if not key and extra:
            key = str(extra.get("api_key", ""))
        if not key:
            raise ValueError(f"no API key for openai-compatible entry {entry.id!r}")
        return OpenAICompatProvider(api_key=key, base_url=entry.base_url)
    raise ValueError(f"unknown provider {entry.provider!r} for entry {entry.id!r}")
