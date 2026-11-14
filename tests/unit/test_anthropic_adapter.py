"""Anthropic adapter: endpoint pinned, adaptive thinking, thinking blocks kept
across a tool loop (all found live adding the Anthropic models)."""

import itertools

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


# --- empty text is never sent (cut-off retry persists a thinking-only turn) ---

THINK = {"type": "thinking", "thinking": "hmm", "signature": "sig1"}
REDACTED = {"type": "redacted_thinking", "data": "xyz"}


def _translate(messages: list[dict], session: str | None = "s1") -> dict:
    return A.translate_request(
        ChatRequest(model="m", messages=messages, max_tokens=100, session_id=session)
    )


def _all_blocks(payload: dict) -> list[dict]:
    out = []
    for m in payload["messages"]:
        if isinstance(m["content"], list):
            out += m["content"]
        else:
            out.append({"type": "text", "text": m["content"]})
    return out


def _check_valid(payload: dict) -> None:
    """The invariants the live API enforces: no empty text, no breakpoint on an
    empty or thinking block, at most four breakpoints, roles alternate."""
    marked = 0
    for b in _all_blocks(payload):
        if b["type"] == "text":
            assert b["text"].strip(), b
        if "cache_control" in b:
            marked += 1
            assert b["type"] not in ("thinking", "redacted_thinking"), b
            assert b["type"] != "text" or b["text"].strip(), b
    for b in payload.get("system") or []:
        marked += "cache_control" in b
    assert marked <= 4
    roles = [m["role"] for m in payload["messages"]]
    assert all(a != b for a, b in itertools.pairwise(roles)), roles


def test_empty_assistant_content_is_dropped_and_users_merge() -> None:
    msgs = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": ""},
        {"role": "user", "content": "continue"},
    ]
    payload = _translate(msgs)
    _check_valid(payload)
    assert [m["role"] for m in payload["messages"]] == ["user"]
    texts = [b["text"] for b in payload["messages"][0]["content"]]
    assert texts == ["go", "continue"]
    # nothing mutated in the caller's history
    assert msgs[1] == {"role": "assistant", "content": ""}


def test_thinking_only_cut_off_turn_sends_just_the_thinking() -> None:
    msgs = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "thinking_blocks": [THINK]},
        {"role": "user", "content": "continue"},
    ]
    payload = _translate(msgs)
    _check_valid(payload)
    assert [m["role"] for m in payload["messages"]] == ["user", "assistant", "user"]
    assert payload["messages"][1]["content"] == [THINK]  # no breakpoint on it


def test_thinking_only_turn_keeps_redacted_and_drops_unsigned_thinking() -> None:
    unsigned = {"type": "thinking", "thinking": "cut mid-thought"}
    msgs = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "  ", "thinking_blocks": [unsigned, REDACTED]},
        {"role": "user", "content": "continue"},
    ]
    payload = _translate(msgs)
    _check_valid(payload)
    assert payload["messages"][1]["content"] == [REDACTED]
    only_unsigned = [msgs[0], {**msgs[1], "thinking_blocks": [unsigned]}, msgs[2]]
    assert [m["role"] for m in _translate(only_unsigned)["messages"]] == ["user"]


def test_empty_text_with_tool_calls_sends_no_text_block() -> None:
    call = [{"id": "t1", "name": "read_file", "arguments": {"path": "a"}}]
    for empty in ("", "   \n", None):
        msgs = [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": empty, "tool_calls": call, "thinking_blocks": [THINK]},
            {"role": "tool", "tool_call_id": "t1", "name": "read_file", "content": "x"},
        ]
        payload = _translate(msgs)
        _check_valid(payload)
        assert [b["type"] for b in payload["messages"][1]["content"]] == ["thinking", "tool_use"]
        # the breakpoint on the tool_use turn sits on tool_use, not on thinking
        assert "cache_control" in payload["messages"][1]["content"][1]


def test_whitespace_only_content_is_dropped() -> None:
    msgs = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": " \n\t"},
        {"role": "user", "content": "again"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "  "},
    ]
    payload = _translate(msgs)
    _check_valid(payload)
    assert [m["role"] for m in payload["messages"]] == ["user", "assistant"]


def test_second_to_last_breakpoint_on_a_thinking_only_message_is_skipped() -> None:
    # the history _load() hands back after a cut-off retry: the empty turn is
    # second to last, where the cache breakpoint used to land on its empty block
    msgs = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "thinking_blocks": [THINK]},
        {"role": "user", "content": "continue"},
    ]
    payload = _translate(msgs)
    _check_valid(payload)
    assert "cache_control" in payload["messages"][-1]["content"][-1]
    assert "cache_control" not in payload["messages"][1]["content"][0]
    # same with the thinking turn really second to last and the tail after it
    msgs2 = [*msgs[:2], {"role": "user", "content": [{"type": "text", "text": "tail"}]}]
    _check_valid(_translate(msgs2))


def test_breakpoint_moves_to_the_last_text_block_before_trailing_thinking() -> None:
    msg = {"role": "assistant", "content": [{"type": "text", "text": "hi"}, THINK]}
    assert A._mark_message(msg) is True
    assert msg["content"][0]["cache_control"] == A.CACHE_BREAKPOINT
    assert "cache_control" not in msg["content"][1]
    assert A._mark_message({"role": "assistant", "content": [THINK]}) is False
    assert (
        A._mark_message({"role": "assistant", "content": [{"type": "text", "text": ""}]}) is False
    )
    assert A._mark_message({"role": "assistant", "content": ""}) is False


def test_list_content_loses_its_empty_text_parts() -> None:
    msgs = [
        {"role": "user", "content": [{"type": "text", "text": ""}, {"type": "text", "text": "q"}]},
        {"role": "assistant", "content": [{"type": "text", "text": " "}]},
        {"role": "user", "content": [{"type": "text", "text": "r"}]},
    ]
    payload = _translate(msgs)
    _check_valid(payload)
    assert [b["text"] for b in _all_blocks(payload)] == ["q", "r"]


def test_legacy_stored_history_is_repaired_on_retranslation() -> None:
    # PR #30 stored shape, several turns deep, with the poisoned turn inside
    msgs = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "thinking_blocks": [THINK]},
        {"role": "user", "content": "continue"},
        {"role": "assistant", "content": "done"},
        {"role": "user", "content": "next"},
    ]
    for session in ("s1", None):
        payload = _translate(msgs, session)
        _check_valid(payload)
        assert [m["role"] for m in payload["messages"]] == [
            "user",
            "assistant",
            "user",
            "assistant",
            "user",
        ]
    assert A._breakpoints(_translate(msgs)) <= 4
    # re-translating the same history does not accumulate breakpoints
    assert _translate(msgs) == _translate(msgs)
    assert all("cache_control" not in str(m) for m in msgs)
