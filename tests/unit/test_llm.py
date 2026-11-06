"""Model catalog + adapter translation tests. No network; contract tests with real
keys live in tests/contract/ and skip without credentials."""

from __future__ import annotations

import pytest

from rework.kernel.llm.adapters.anthropic import translate_request as an_translate
from rework.kernel.llm.adapters.fake import FakeProvider
from rework.kernel.llm.adapters.openai_compat import translate_request as oa_translate
from rework.kernel.llm.catalog import ModelCatalog, ModelEntry, UnknownModelError
from rework.kernel.llm.types import ChatRequest, ChatResult, ToolCall, ToolSpec, Usage


def make_catalog() -> ModelCatalog:
    return ModelCatalog(
        [
            ModelEntry(
                id="flash",
                provider="openai-compatible",
                model="glm-4.5-flash",
                tier=1,
                base_url="http://x/v1",
                api_key_env="X_KEY",
                cost_in_per_mtok=0.2,
                cost_out_per_mtok=1.1,
            ),
            ModelEntry(
                id="pro",
                provider="openai-compatible",
                model="glm-4.6",
                tier=2,
                base_url="http://x/v1",
                api_key_env="X_KEY",
                cost_in_per_mtok=0.6,
                cost_out_per_mtok=2.2,
            ),
            ModelEntry(
                id="haiku",
                provider="anthropic",
                model="claude-haiku-4-5",
                tier=1,
                cost_in_per_mtok=1.0,
                cost_out_per_mtok=5.0,
            ),
            ModelEntry(
                id="opus",
                provider="anthropic",
                model="claude-opus-5",
                tier=3,
                cost_in_per_mtok=5.0,
                cost_out_per_mtok=25.0,
            ),
        ]
    )


# -- catalog ------------------------------------------------------------------


def test_resolve_and_unknown() -> None:
    cat = make_catalog()
    assert cat.resolve("haiku").model == "claude-haiku-4-5"
    with pytest.raises(UnknownModelError):
        cat.resolve("nope")


def test_promote_walks_tiers_within_provider() -> None:
    cat = make_catalog()
    assert cat.promote("flash").id == "pro"  # skips the anthropic tiers
    assert cat.promote("pro") is None  # top of its provider
    assert cat.promote("haiku").id == "opus"


def test_promote_cross_provider_gated() -> None:
    cat = make_catalog()
    assert cat.promote("pro", cross_provider=True).id == "opus"


def test_duplicate_ids_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        ModelCatalog(
            [
                ModelEntry(id="a", provider="anthropic", model="x"),
                ModelEntry(id="a", provider="anthropic", model="y"),
            ]
        )


def test_cost_estimate() -> None:
    entry = make_catalog().resolve("haiku")
    assert entry.estimate_cost(1_000_000, 1_000_000) == pytest.approx(6.0)


def test_from_config() -> None:
    cat = ModelCatalog.from_config([{"id": "a", "provider": "anthropic", "model": "m", "tier": 1}])
    assert cat.resolve("a").tier == 1


# -- anthropic translation ------------------------------------------------------


def test_anthropic_request_translation() -> None:
    req = ChatRequest(
        model="claude-haiku-4-5",
        system="be brief",
        messages=[
            {"role": "user", "content": "run the tool"},
            {
                "role": "assistant",
                "content": "ok",
                "tool_calls": [{"id": "t1", "name": "ls", "arguments": {"path": "/tmp"}}],
            },
            {"role": "tool", "tool_call_id": "t1", "content": "file.txt"},
        ],
        tools=[ToolSpec(name="ls", description="list", input_schema={"type": "object"})],
        temperature=0.2,
    )
    payload = an_translate(req)
    assert payload["model"] == "claude-haiku-4-5"
    assert payload["system"] == "be brief"
    assert payload["temperature"] == 0.2
    assert payload["tools"][0]["input_schema"] == {"type": "object"}
    assert payload["messages"][1]["content"][0] == {"type": "text", "text": "ok"}
    assert payload["messages"][1]["content"][1] == {
        "type": "tool_use",
        "id": "t1",
        "name": "ls",
        "input": {"path": "/tmp"},
    }
    assert payload["messages"][2]["content"][0]["type"] == "tool_result"
    assert payload["messages"][2]["content"][0]["tool_use_id"] == "t1"


# -- openai-compatible translation ----------------------------------------------


def test_openai_request_translation() -> None:
    req = ChatRequest(
        model="glm-4.5-flash",
        system="be brief",
        messages=[
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "t1", "name": "ls", "arguments": {"path": "/tmp"}}],
            },
            {"role": "tool", "tool_call_id": "t1", "content": "file.txt"},
        ],
        tools=[ToolSpec(name="ls", description="list", input_schema={"type": "object"})],
    )
    payload = oa_translate(req)
    assert payload["messages"][0] == {"role": "system", "content": "be brief"}
    assert payload["messages"][2]["tool_calls"][0]["function"]["name"] == "ls"
    assert payload["messages"][3] == {"role": "tool", "tool_call_id": "t1", "content": "file.txt"}
    assert payload["tools"][0]["function"]["parameters"] == {"type": "object"}


# -- fake provider ----------------------------------------------------------------


async def test_fake_provider_scripts_and_records() -> None:
    fake = FakeProvider(
        [
            ChatResult(
                text="calling tool",
                tool_calls=[ToolCall(id="t1", name="ls", arguments={"path": "/"})],
                usage=Usage(input_tokens=10, output_tokens=5),
                stop_reason="tool_calls",
            ),
            ChatResult(text="done"),
        ]
    )
    req = ChatRequest(model="fake-1", messages=[{"role": "user", "content": "go"}])
    first = await fake.complete(req)
    assert first.stop_reason == "tool_calls"
    assert first.tool_calls[0].name == "ls"
    second = await fake.complete(req)
    assert second.text == "done"
    assert len(fake.requests) == 2


def test_has_credentials_mirrors_build_provider(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from rework.kernel.llm.catalog import ModelEntry

    monkeypatch.delenv("X_TEST_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    compat = ModelEntry(id="c", provider="openai-compatible", model="m", api_key_env="X_TEST_KEY")
    claude = ModelEntry(id="h", provider="anthropic", model="claude")
    assert ModelEntry(id="f", provider="fake", model="f").has_credentials()
    assert not compat.has_credentials() and not claude.has_credentials()
    monkeypatch.setenv("X_TEST_KEY", "k")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    assert compat.has_credentials() and claude.has_credentials()
