"""REWORK_FAKE_SCRIPT drives a real daemon process in e2e runs (scripts/e2e)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rework.kernel.llm.adapters import build_provider
from rework.kernel.llm.adapters.fake import FakeProvider, _RoutedScript
from rework.kernel.llm.catalog import ModelEntry
from rework.kernel.llm.types import ChatRequest


def _req(system: str) -> ChatRequest:
    return ChatRequest(model="f", system=system, messages=[{"role": "user", "content": "x"}])


async def test_routes_by_system_prompt_and_shares_queues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "s.json"
    script.write_text(
        json.dumps(
            {
                "ORCH": [{"tool_calls": [{"name": "wait", "arguments": {"minutes": 1}}]}],
                "GOAL: role": [{"text": "one"}, {"text": "two"}],
            }
        )
    )
    monkeypatch.setenv("REWORK_FAKE_SCRIPT", str(script))
    _RoutedScript._shared.clear()
    entry = ModelEntry(id="f", provider="fake", model="f", tier=1)
    a, b = build_provider(entry), build_provider(entry)
    first = await a.complete(_req("I am the ORCH"))
    assert first.tool_calls[0].name == "wait" and first.tool_calls[0].arguments == {"minutes": 1}
    # two providers built in one process consume ONE queue per route
    assert (await a.complete(_req("GOAL: role x"))).text == "one"
    assert (await b.complete(_req("GOAL: role x"))).text == "two"
    with pytest.raises(AssertionError, match="exhausted"):
        await b.complete(_req("GOAL: role x"))


def test_unset_is_the_plain_fake(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REWORK_FAKE_SCRIPT", raising=False)
    provider = build_provider(ModelEntry(id="f", provider="fake", model="f", tier=1))
    assert type(provider) is FakeProvider
