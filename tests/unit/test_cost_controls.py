"""Cost controls found necessary by the first dogfood mission ($3 for a
one-line CSS change): prompt-cache routing, cached-token pricing, transcript
compaction, loop resume, turn budgets, retrying provider 500s."""

from __future__ import annotations

from pathlib import Path

from rework.kernel.llm.catalog import ModelEntry
from rework.kernel.llm.types import ChatRequest
from rework.nodes import toolloop


def test_session_id_reaches_both_adapters() -> None:
    from rework.kernel.llm.adapters.anthropic import translate_request as to_anthropic
    from rework.kernel.llm.adapters.openai_compat import translate_request as to_openai

    req = ChatRequest(
        model="m",
        system="static rules",
        messages=[{"role": "user", "content": "go"}],
        session_id="m-1:code:coder:1",
    )
    assert to_openai(req)["user"] == "m-1:code:coder:1", "Fireworks routes by it (cache hits)"
    payload = to_anthropic(req)
    assert payload["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert payload["messages"][-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    plain = to_openai(ChatRequest(model="m", messages=[{"role": "user", "content": "x"}]))
    assert "user" not in plain


def test_cached_tokens_bill_at_the_discount() -> None:
    e = ModelEntry(id="f", provider="fake", model="f", cost_in_per_mtok=0.3, cost_out_per_mtok=1.2)
    full = e.estimate_cost(1_000_000, 0)
    half_cached = e.estimate_cost(1_000_000, 0, cached_tokens=500_000)
    assert round(full, 6) == 0.3 and round(half_cached, 6) == 0.225
    custom = ModelEntry(
        id="c", provider="fake", model="c", cost_in_per_mtok=1.0, cost_cached_per_mtok=0.1
    )
    assert round(custom.estimate_cost(1_000_000, 0, 1_000_000), 6) == 0.1


def test_anthropic_usage_counts_cache_reads_as_input() -> None:
    from rework.kernel.llm.adapters.anthropic import translate_response

    result = translate_response(
        {
            "content": [{"type": "text", "text": "hi"}],
            "usage": {
                "input_tokens": 100,
                "cache_read_input_tokens": 900,
                "cache_creation_input_tokens": 0,
                "output_tokens": 5,
            },
        }
    )
    assert result.usage.input_tokens == 1000 and result.usage.cached_tokens == 900


def test_compaction_elides_old_tool_output_but_keeps_recent() -> None:
    msgs = [{"role": "user", "content": "brief"}]
    n = toolloop.KEEP_RECENT + 8
    size = toolloop.COMPACT_AT // toolloop.KEEP_RECENT
    for i in range(n):
        msgs.append({"role": "assistant", "content": "", "tool_calls": [{"id": str(i)}]})
        msgs.append({"role": "tool", "name": "read_file", "content": f"file {i}\n" + "x" * size})
    elided = toolloop._compact(msgs)
    tools = [m for m in msgs if m["role"] == "tool"]
    assert elided == n - toolloop.KEEP_RECENT
    assert tools[0]["content"].startswith("[elided") and "file 0" in tools[0]["content"]
    assert all(not t["content"].startswith("[elided") for t in tools[-toolloop.KEEP_RECENT :])
    assert toolloop._compact(msgs) == 0, "stable after one compaction (cache-friendly)"


def test_loop_transcript_resumes_and_drops_a_dangling_call(tmp_path: Path) -> None:
    path = tmp_path / "loop.json"
    opening = [{"role": "user", "content": "brief"}]
    saved = [
        *opening,
        {"role": "assistant", "content": "", "tool_calls": [{"id": "1", "name": "x"}]},
        {"role": "tool", "tool_call_id": "1", "name": "x", "content": "ok"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "2", "name": "y"}]},
    ]
    toolloop._save_loop(path, saved, 7)
    messages, turns = toolloop._load_loop(path, opening)  # type: ignore[misc]
    assert turns == 7 and messages[-1]["role"] == "tool", "unanswered call dropped"
    other = [{"role": "user", "content": "a different run"}]
    assert toolloop._load_loop(path, other) is None
    toolloop._clear_loop(path)
    assert not path.exists()


def test_provider_500s_are_retried() -> None:
    from rework.kernel.llm.adapters.retry import _is_transient

    class InternalServerError(Exception):
        pass

    assert _is_transient(InternalServerError("Error code: 500 - Internal server error"))
    assert not _is_transient(ValueError("bad request: unknown field"))


def test_turn_budget_defaults_and_bounds() -> None:
    from rework.orchestrator.agent import _turn_budget

    assert _turn_budget("investigator", {}) == 20
    assert _turn_budget("investigator", {"depth": "deep"}) == 45
    assert _turn_budget("coder", {}) == 40
    assert _turn_budget("auditor", {"max_turns": 500}) == 64
    assert _turn_budget("auditor", {"max_turns": 1}) == 3
