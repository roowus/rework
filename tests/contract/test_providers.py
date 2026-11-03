"""Provider contract tests: one cheap live call per adapter with real credentials.
Skipped without keys; never in the default CI path (no keys there). These catch
SDK drift that scripted tests cannot."""

from __future__ import annotations

import os

import pytest

from rework.kernel.llm.adapters.anthropic import AnthropicProvider
from rework.kernel.llm.adapters.openai_compat import OpenAICompatProvider, OpenAIProvider
from rework.kernel.llm.types import ChatRequest

pytestmark = pytest.mark.contract


@pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="no ANTHROPIC_API_KEY")
async def test_anthropic_live() -> None:
    provider = AnthropicProvider()
    result = await provider.complete(
        ChatRequest(
            model="claude-haiku-4-5",
            messages=[{"role": "user", "content": "Reply with the single word: ok"}],
            max_tokens=16,
        )
    )
    assert result.usage.output_tokens > 0


@pytest.mark.skipif(not os.environ.get("OPENAI_API_KEY"), reason="no OPENAI_API_KEY")
async def test_openai_live() -> None:
    provider = OpenAIProvider()
    result = await provider.complete(
        ChatRequest(
            model="gpt-4.1-mini",
            messages=[{"role": "user", "content": "Reply with the single word: ok"}],
            max_tokens=16,
        )
    )
    assert result.usage.output_tokens > 0


@pytest.mark.skipif(
    not (os.environ.get("REWORK_COMPAT_BASE_URL") and os.environ.get("REWORK_COMPAT_KEY")),
    reason="no REWORK_COMPAT_BASE_URL/REWORK_COMPAT_KEY",
)
async def test_openai_compat_live() -> None:
    provider = OpenAICompatProvider(
        api_key=os.environ["REWORK_COMPAT_KEY"],
        base_url=os.environ["REWORK_COMPAT_BASE_URL"],
    )
    result = await provider.complete(
        ChatRequest(
            model=os.environ.get("REWORK_COMPAT_MODEL", "default"),
            messages=[{"role": "user", "content": "Reply with the single word: ok"}],
            max_tokens=16,
        )
    )
    assert result.usage.output_tokens > 0
