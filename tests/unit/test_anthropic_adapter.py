"""Anthropic adapter: endpoint pinned, adaptive thinking, thinking blocks kept
across a tool loop (all found live adding the Anthropic models)."""

from rework.kernel.llm.adapters import anthropic as A
from rework.kernel.llm.types import ChatRequest


def test_the_endpoint_ignores_anthropic_base_url(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://localhost:20128")
    p = A.AnthropicProvider(api_key="k")
    assert str(p._client.base_url).startswith("https://api.anthropic.com")
    q = A.AnthropicProvider(api_key="k", base_url="https://proxy.example/v1")
    assert str(q._client.base_url).startswith("https://proxy.example")


def test_effort_is_adaptive_unless_the_model_needs_a_budget() -> None:
    req = ChatRequest(model="claude-sonnet-5", messages=[], max_tokens=4096, effort="high")
    payload = A.translate_request(req)
    assert payload["thinking"] == {"type": "adaptive"}
    assert payload["output_config"] == {"effort": "high"}
    A._BUDGET_THINKING.add("old-model")
    try:
        old = A.translate_request(req.model_copy(update={"model": "old-model"}))
        assert old["thinking"]["type"] == "enabled" and old["max_tokens"] > 24_576
    finally:
        A._BUDGET_THINKING.discard("old-model")


def test_thinking_blocks_round_trip_through_a_tool_turn() -> None:
    data = {
        "content": [
            {"type": "thinking", "thinking": "look at the file", "signature": "sig1"},
            {"type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "a"}},
        ],
        "usage": {"input_tokens": 5, "output_tokens": 3},
        "stop_reason": "tool_use",
    }
    result = A.translate_response(data)
    assert result.thinking_blocks == [
        {"type": "thinking", "thinking": "look at the file", "signature": "sig1"}
    ]
    msgs = [
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "t1", "name": "read_file", "arguments": {"path": "a"}}],
            "thinking_blocks": result.thinking_blocks,
        },
        {"role": "tool", "tool_call_id": "t1", "name": "read_file", "content": "x"},
    ]
    payload = A.translate_request(ChatRequest(model="m", messages=msgs, max_tokens=100))
    blocks = payload["messages"][1]["content"]
    assert [b["type"] for b in blocks] == ["thinking", "tool_use"]
