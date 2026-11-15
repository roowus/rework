"""Cost controls found necessary by the first dogfood mission ($3 for a
one-line CSS change): prompt-cache routing, cached-token pricing, transcript
compaction, loop resume, turn budgets, retrying provider 500s."""

from __future__ import annotations

from pathlib import Path

from rework.kernel.llm.catalog import ModelEntry
from rework.kernel.llm.types import ChatRequest
from rework.nodes import toolloop


def _breakpoints(payload: dict) -> list[dict]:  # type: ignore[type-arg]
    """Every cache_control value in a translated Anthropic request, in order:
    system blocks first, then messages."""
    found = [
        b["cache_control"]
        for b in payload.get("system", [])
        if isinstance(b, dict) and "cache_control" in b
    ]
    for msg in payload["messages"]:
        content = msg["content"]
        if isinstance(content, list):
            found += [
                b["cache_control"] for b in content if isinstance(b, dict) and "cache_control" in b
            ]
    return found


def test_session_id_reaches_both_adapters() -> None:
    from rework.kernel.llm.adapters.anthropic import translate_request as to_anthropic
    from rework.kernel.llm.adapters.openai_compat import translate_request as to_openai

    req = ChatRequest(
        model="m",
        system="static rules",
        messages=[
            {"role": "user", "content": "go"},
            {"role": "user", "content": "CURRENT STATE (rebuilt for this turn)"},
        ],
        session_id="m-1:code:coder:1",
    )
    assert to_openai(req)["user"] == "m-1:code:coder:1", "Fireworks routes by it (cache hits)"
    payload = to_anthropic(req)
    assert payload["system"][0]["cache_control"] == {"type": "ephemeral"}
    # the newest message is the volatile tail (the orchestrator's per-turn
    # state message, never persisted): the breakpoint must ALSO sit on the
    # message before it, or the cached prefix never recurs and the whole
    # transcript behind the tail re-bills as fresh input every turn
    assert payload["messages"][-2]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert payload["messages"][-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    plain = to_openai(ChatRequest(model="m", messages=[{"role": "user", "content": "x"}]))
    assert "user" not in plain


def test_a_volatile_tail_is_cached_behind() -> None:
    """The orchestrator appends a per-turn state message on top of a persisted
    transcript: system + second-to-last + last are marked (3 of the API's 4),
    and the same handling covers both content shapes."""
    from rework.kernel.llm.adapters.anthropic import translate_request as to_anthropic

    history = [
        {"role": "user", "content": "Mission objective: fix it. Begin."},
        {"role": "assistant", "content": "ok"},
        {"role": "tool", "tool_call_id": "t1", "name": "read_file", "content": "x"},
    ]
    req = ChatRequest(
        model="m",
        system="static rules",
        messages=[*history, {"role": "user", "content": "CURRENT STATE (this turn)"}],
        session_id="m-1",
    )
    payload = to_anthropic(req)
    assert _breakpoints(payload) == [
        {"type": "ephemeral"},
        {"type": "ephemeral"},
        {"type": "ephemeral"},
    ], "system + messages[-2] + messages[-1], and no more"
    # the tool result is a list content: the breakpoint lands on its last block
    tool_result = payload["messages"][2]["content"]
    assert tool_result[-1]["type"] == "tool_result"
    assert tool_result[-1]["cache_control"] == {"type": "ephemeral"}
    # and never on the caller's persisted history
    assert "cache_control" not in history[2]
    again = to_anthropic(req)
    assert len(_breakpoints(again)) == 3, "no accumulation across turns"


def test_short_histories_do_not_break_or_over_mark() -> None:
    from rework.kernel.llm.adapters.anthropic import translate_request as to_anthropic

    empty = ChatRequest(model="m", system="s", messages=[], session_id="chat-1")
    assert _breakpoints(to_anthropic(empty)) == [{"type": "ephemeral"}]  # the system block
    one = ChatRequest(
        model="m", system="s", messages=[{"role": "user", "content": "hi"}], session_id="chat-1"
    )
    payload = to_anthropic(one)
    assert len(payload["messages"]) == 1 and len(_breakpoints(payload)) == 2
    assert payload["messages"][-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}


def test_breakpoints_never_exceed_the_api_limit() -> None:
    from rework.kernel.llm.adapters.anthropic import translate_request as to_anthropic

    # content that already carries a breakpoint (a resumed payload, or a caller
    # that marked its own block) must not be marked twice or pushed over 4
    marked = {"type": "text", "text": "x", "cache_control": {"type": "ephemeral"}}
    req = ChatRequest(
        model="m",
        system="s",
        messages=[
            {"role": "user", "content": [marked]},
            {"role": "user", "content": [{"type": "text", "text": "y"}]},
        ],
        session_id="m-1",
    )
    payload = to_anthropic(req)
    assert _breakpoints(payload) == [
        {"type": "ephemeral"},
        {"type": "ephemeral"},
        {"type": "ephemeral"},
    ]
    assert (
        req.messages[0]["content"][0] is marked
        and "cache_control" not in req.messages[1]["content"][0]
    )


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


async def test_a_hung_model_call_hits_the_hard_deadline(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Found live: a provider call stayed in flight 15+ minutes despite the
    client's own timeout. Every call has a hard deadline, and the timeout is a
    transient error the retry wrapper retries."""
    import asyncio

    import pytest

    from rework.kernel.llm.adapters.logged import LoggedProvider
    from rework.kernel.llm.adapters.retry import _is_transient
    from rework.kernel.llm.types import ChatRequest

    monkeypatch.setenv("REWORK_MODEL_TIMEOUT_S", "0.2")

    class Hung:
        async def complete(self, request):  # type: ignore[no-untyped-def]
            await asyncio.sleep(3600)

    with pytest.raises(TimeoutError) as caught:
        await LoggedProvider(Hung()).complete(ChatRequest(model="m"))
    assert _is_transient(caught.value)


async def test_a_max_tokens_reply_is_not_idle_and_does_not_fail_the_mission() -> None:
    """Cut-off replies are retried with overrides and never count toward
    MAX_IDLE_NUDGES: two cut-offs plus two genuinely idle replies stay under the
    limit, so the mission reaches its tool call instead of failing."""
    from types import SimpleNamespace

    from rework.kernel.llm.types import ChatResult, ToolCall
    from rework.orchestrator.agent import MissionOrchestrator

    def reply(stop: str, *calls: ToolCall) -> ChatResult:
        return ChatResult(text="", tool_calls=list(calls), stop_reason=stop)

    finish = ToolCall(id="f", name="finish", arguments={})
    script = [
        reply("max_tokens"),
        reply("max_tokens"),
        reply("end_turn"),
        reply("end_turn"),
        reply("tool_use", finish),
    ]
    thinks: list[dict[str, object]] = []
    notes: list[str] = []
    agent = object.__new__(MissionOrchestrator)
    agent.finished = None
    agent.effort = "high"
    agent.entry = SimpleNamespace(max_output=8192)
    agent.history = []
    agent.mid = "m"

    async def gate() -> str:
        return "ok"

    async def turn_cap() -> int:
        return 1000

    async def nothing(*_a: object, **_k: object) -> None:
        return None

    async def think(max_tokens: int | None = None, effort: str | None = None) -> ChatResult:
        thinks.append({"max_tokens": max_tokens, "effort": effort})
        return script.pop(0)

    async def execute(calls: list[ToolCall]) -> None:
        agent.finished = ("done", "ok")

    async def note(text: str, **_k: object) -> None:
        notes.append(text)

    async def append(message: dict[str, object], **_k: object) -> None:
        agent.history.append(message)

    agent._owner_gate = gate  # type: ignore[method-assign]
    agent._turn_cap = turn_cap  # type: ignore[method-assign]
    agent._deliver_owner_messages = nothing  # type: ignore[method-assign]
    agent._think = think  # type: ignore[method-assign]
    agent._execute_calls = execute  # type: ignore[method-assign]
    agent._note = note  # type: ignore[method-assign]
    agent._append = append  # type: ignore[method-assign]

    await agent._loop()

    assert agent.finished == ("done", "ok"), "a cut-off reply must not fail the mission"
    assert not script
    # the bigger budget applies to the first retry, a lower effort to the second;
    # later (idle) turns of the same series keep them, and nothing was left in _think
    assert thinks[0] == {"max_tokens": None, "effort": None}
    assert thinks[1] == {"max_tokens": 32_768, "effort": "high"}
    assert thinks[2] == {"max_tokens": 32_768, "effort": "medium"}
    assert len(notes) == 2 and "high -> medium" in notes[1] and "32768" in notes[1]
