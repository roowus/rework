"""The orchestrator conversation as pure logic: proposals, confirmation, and
the guarantee that talking never creates a mission by itself."""

from __future__ import annotations

from typing import Any

import pytest

from conftest import ScriptedProvider, role_catalog
from rework.kernel.llm.types import ChatResult, ToolCall
from rework.orchestrator.chat import OrchestratorChat

PROPOSE = ToolCall(
    id="c1",
    name="propose_mission",
    arguments={"objective": "fix the bug", "plan": "fix", "project": "rework"},
)


def _chat(responses: list[ChatResult]) -> OrchestratorChat:
    provider = ScriptedProvider()
    provider.route("orchestrator", responses)
    return OrchestratorChat(provider, role_catalog(), "flash")


@pytest.mark.asyncio
async def test_proposal_is_not_a_launch() -> None:
    chat = _chat([ChatResult(tool_calls=[PROPOSE])])
    result = await chat.send("fix the bug")
    assert result.proposal is not None
    assert result.proposal.objective == "fix the bug"
    assert result.proposal.plan == "fix"
    assert chat.launched is False, "a proposal must never launch on its own"


@pytest.mark.asyncio
async def test_yes_launches_no_scraps() -> None:
    chat = _chat([ChatResult(tool_calls=[PROPOSE])])
    await chat.send("fix the bug")
    yes = await chat.send("yes")
    assert chat.launched is True and "Launching" in yes.reply

    chat2 = _chat([ChatResult(tool_calls=[PROPOSE])])
    await chat2.send("fix the bug")
    no = await chat2.send("no")
    assert chat2.launched is False
    assert chat2.proposal is None
    assert "Scrapped" in no.reply


@pytest.mark.asyncio
async def test_ask_tool_surfaces_the_question() -> None:
    chat = _chat(
        [
            ChatResult(
                tool_calls=[ToolCall(id="c2", name="ask", arguments={"question": "which repo?"})]
            )
        ]
    )
    result = await chat.send("fix it")
    assert result.asked == "which repo?"
    assert result.proposal is None


@pytest.mark.asyncio
async def test_cost_is_reported_per_turn() -> None:
    chat = _chat([ChatResult(text="hello")])
    result = await chat.send("hi")
    assert result.turn_tokens >= 0
    assert result.turn_cost_usd >= 0.0
    assert any(t.who == "orchestrator" for t in result.transcript)


@pytest.mark.asyncio
async def test_context_note_reaches_the_system_prompt() -> None:
    provider = ScriptedProvider()
    provider.route("orchestrator", [ChatResult(text="ok")])
    chat = OrchestratorChat(provider, role_catalog(), "flash", context="Projects: rework")
    await chat.send("hi")
    assert "Projects: rework" in (provider.requests[0].system or "")


@pytest.mark.asyncio
async def test_effort_rides_on_chat_requests() -> None:
    provider = ScriptedProvider()
    provider.route("orchestrator", [ChatResult(text="ok")])
    chat = OrchestratorChat(provider, role_catalog(), "flash", effort="high")
    await chat.send("hi")
    request: Any = provider.requests[0]
    assert request.effort == "high"


async def test_conversation_persists_and_reloads(store) -> None:
    """A chat can be closed and reopened: every turn lands in the store, and a
    fresh OrchestratorChat with the same chat_id replays it."""
    from rework.kernel.llm.catalog import ModelCatalog, ModelEntry
    from rework.kernel.llm.types import ChatResult
    from rework.kernel.state.records import Chat
    from rework.orchestrator.chat import OrchestratorChat

    class _P:
        def __init__(self) -> None:
            self.seen: list[list[dict]] = []

        async def complete(self, request):
            self.seen.append(request.messages)
            return ChatResult(text="Sure, what repo?")

    catalog = ModelCatalog([ModelEntry(id="flash", provider="fake", model="f")])
    chat_row = await store.create_chat(Chat(title=""))
    provider = _P()
    chat = OrchestratorChat(provider, catalog, "flash", store=store, chat_id=chat_row.id)
    await chat.send("audit the parser")
    await chat.send("it lives in github.com/x/y")

    stored = await store.chat_messages(chat_row.id)
    assert [m.role for m in stored] == ["user", "assistant", "user", "assistant"]
    assert stored[0].display == "audit the parser"

    # a NEW instance for the same chat sees the history and replays it to the model
    reopened = OrchestratorChat(provider, catalog, "flash", store=store, chat_id=chat_row.id)
    shown = await reopened.load()
    assert [who for who, _ in shown] == ["you", "orchestrator", "you", "orchestrator"]
    assert reopened.transcript[0] == ("you", "audit the parser")

    await reopened.send("and again")
    # the replayed history reached the provider: 4 rows before + the new user turn
    assert len(provider.seen[-1]) == 5
    assert provider.seen[-1][0]["content"] == "audit the parser"


async def test_orchestrator_looks_before_it_proposes(store) -> None:
    """The chat can call read-only tools, and the result feeds the next round.

    This is what makes a Work-tab trajectory worth rendering: the orchestrator
    checks the owner's actual missions instead of guessing, then proposes."""
    from rework.kernel.llm.catalog import ModelCatalog, ModelEntry
    from rework.kernel.llm.types import ChatResult, ToolCall
    from rework.kernel.state.records import Mission, MissionStatus
    from rework.orchestrator.chat import OrchestratorChat

    await store.create_mission(
        Mission(id="m-look", objective="fix the parser", status=MissionStatus.DONE)
    )

    class _P:
        def __init__(self) -> None:
            self.requests: list[Any] = []

        async def complete(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                return ChatResult(
                    tool_calls=[ToolCall(id="t1", name="list_missions", arguments={"limit": 5})]
                )
            return ChatResult(
                tool_calls=[
                    ToolCall(
                        id="t2",
                        name="propose_mission",
                        arguments={"objective": "fix the parser", "plan": "fix"},
                    )
                ]
            )

    provider = _P()
    turns: list[tuple[str, dict]] = []
    chat = OrchestratorChat(
        provider,
        ModelCatalog([ModelEntry(id="flash", provider="fake", model="f")]),
        "flash",
        store=store,
        on_turn=lambda kind, payload: turns.append((kind, payload)),
    )
    result = await chat.send("what should I run next?")

    # the tool actually ran against the store, and its output reached the model
    second = provider.requests[1].messages
    tool_rows = [m for m in second if m.get("role") == "tool"]
    assert tool_rows and "m-look" in tool_rows[0]["content"]
    assert "fix the parser" in tool_rows[0]["content"]

    # the trajectory sink saw the call and its result, for the UI to render
    assert [k for k, _ in turns] == ["tool_call", "tool_result", "turn"]
    assert turns[1][1]["result"].startswith("m-look")

    # and the proposal still came through
    assert result.proposal is not None
    assert result.proposal.objective == "fix the parser"


async def test_tool_loop_is_bounded(store) -> None:
    """A model that keeps calling tools forever must be cut off, not loop."""
    from rework.kernel.llm.catalog import ModelCatalog, ModelEntry
    from rework.kernel.llm.types import ChatResult, ToolCall
    from rework.orchestrator.chat import OrchestratorChat

    class _Loop:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, request):
            self.calls += 1
            return ChatResult(tool_calls=[ToolCall(id="t", name="list_projects", arguments={})])

    provider = _Loop()
    chat = OrchestratorChat(
        provider,
        ModelCatalog([ModelEntry(id="flash", provider="fake", model="f")]),
        "flash",
        store=store,
        max_tool_rounds=2,
    )
    await chat.send("go")
    # 1 initial call + 2 tool rounds, then it stops regardless of the model
    assert provider.calls == 3


async def test_tools_are_off_without_a_store() -> None:
    """A chat with no store (CLI/test) keeps the old single-call behaviour."""
    from rework.kernel.llm.catalog import ModelCatalog, ModelEntry
    from rework.kernel.llm.types import ChatResult
    from rework.orchestrator.chat import OrchestratorChat

    class _P:
        def __init__(self) -> None:
            self.requests: list[Any] = []

        async def complete(self, request):
            self.requests.append(request)
            return ChatResult(text="hi")

    provider = _P()
    chat = OrchestratorChat(
        provider, ModelCatalog([ModelEntry(id="flash", provider="fake", model="f")]), "flash"
    )
    await chat.send("hello")
    names = {t.name for t in provider.requests[0].tools}
    assert "list_missions" not in names  # no toolbox, no look tools
    assert {"propose_mission", "ask"} <= names


async def test_chat_history_survives_real_adapters(store) -> None:
    """The history a chat builds (and reloads) must translate through BOTH real
    adapters. Found live: tool calls were stored in OpenAI wire shape, every
    adapter expects the provider-neutral one, and the round after any lookup
    crashed with KeyError 'name'. Fake providers never translate, so only a
    test that runs the adapters' own translate_request can catch this."""
    from rework.kernel.llm.adapters.anthropic import translate_request as to_anthropic
    from rework.kernel.llm.adapters.openai_compat import translate_request as to_openai
    from rework.kernel.llm.catalog import ModelCatalog, ModelEntry
    from rework.kernel.llm.types import ChatRequest, ChatResult, ToolCall
    from rework.kernel.state.records import Chat
    from rework.orchestrator.chat import OrchestratorChat

    class _P:
        def __init__(self) -> None:
            self.requests: list[Any] = []

        async def complete(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                return ChatResult(
                    tool_calls=[ToolCall(id="t1", name="list_projects", arguments={})]
                )
            return ChatResult(
                text="here",
                tool_calls=[
                    ToolCall(
                        id="t2",
                        name="propose_mission",
                        arguments={"objective": "x", "plan": "echo"},
                    )
                ],
            )

    catalog = ModelCatalog([ModelEntry(id="flash", provider="fake", model="f")])
    provider = _P()
    row = await store.create_chat(Chat(title=""))
    chat = OrchestratorChat(provider, catalog, "flash", store=store, chat_id=row.id)
    await chat.send("hi")
    for request in provider.requests:
        to_openai(request)
        to_anthropic(request)

    # reopened: the persisted history must be valid for the adapters too
    reopened = OrchestratorChat(provider, catalog, "flash", store=store, chat_id=row.id)
    await reopened.load()
    history = reopened._history
    roles = [m["role"] for m in history]
    assert roles.index("assistant") < roles.index("tool"), "tool rows need their call"
    replay = ChatRequest(model="f", messages=[*history, {"role": "user", "content": "more"}])
    to_openai(replay)
    to_anthropic(replay)


# -- a chat turn always ends visibly (provider/tool/persistence/cut-off) ------


class RaisingProvider:
    """Fails complete() once, then behaves like a scripted reply."""

    def __init__(self, exc: Exception, then: ChatResult | None = None) -> None:
        self.exc = exc
        self.then = then
        self.calls = 0

    async def complete(self, request: Any) -> ChatResult:
        self.calls += 1
        if self.exc is not None:
            exc, self.exc = self.exc, None
            raise exc
        assert self.then is not None
        return self.then


def _catalog() -> Any:
    from rework.kernel.llm.catalog import ModelCatalog, ModelEntry

    return ModelCatalog([ModelEntry(id="flash", provider="fake", model="fake-flash", tier=1)])


@pytest.mark.asyncio
async def test_provider_error_is_visible_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    """A provider crash must not take the turn down silently: the owner sees a
    line and the traceback is logged."""
    import logging

    chat = OrchestratorChat(
        RaisingProvider(RuntimeError("boom")), _catalog(), "flash", tools_enabled=False
    )
    with caplog.at_level(logging.ERROR, logger="rework.chat"):
        result = await chat.send("hello")
    assert "RuntimeError" in result.reply and "boom" in result.reply
    assert "(no reply)" not in result.reply
    assert result.transcript[-1].text == result.reply
    assert chat.transcript[-1] == ("orchestrator", result.reply)
    errors = [r for r in caplog.records if r.name == "rework.chat" and r.levelname == "ERROR"]
    assert errors and any(r.exc_info for r in errors), "the traceback must be logged"


@pytest.mark.asyncio
async def test_tool_failure_reaches_the_model_plainly_and_the_turn_continues(
    store: Any,
) -> None:
    """A library search blow-up is information, not a dead turn: the model is
    told plainly and goes on to answer."""
    from rework.kernel.state.records import Chat

    class _BrokenLibrary:
        async def search_items(self, query: str, type_: Any = None) -> list:
            raise RuntimeError("database connection lost")

    class _P:
        def __init__(self) -> None:
            self.requests: list[Any] = []

        async def complete(self, request: Any) -> ChatResult:
            self.requests.append(request)
            if len(self.requests) == 1:
                return ChatResult(
                    tool_calls=[ToolCall(id="t1", name="library_search", arguments={"query": "x"})]
                )
            return ChatResult(text="carrying on without the library")

    row = await store.create_chat(Chat(title=""))
    provider = _P()
    chat = OrchestratorChat(
        provider,
        _catalog(),
        "flash",
        store=store,
        chat_id=row.id,
        library=_BrokenLibrary(),
    )
    result = await chat.send("what do we know about x?")
    tool_rows = [m for m in provider.requests[1].messages if m.get("role") == "tool"]
    assert tool_rows, "the tool result must reach the model"
    assert "Library search failed" in tool_rows[0]["content"]
    assert "continuing without it" in tool_rows[0]["content"]
    assert "RuntimeError" not in tool_rows[0]["content"]  # no raw exception repr
    assert result.reply == "carrying on without the library"


@pytest.mark.asyncio
async def test_persistence_failure_still_surfaces_the_reply(
    store: Any, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A store that refuses writes must not eat the reply: it is shown with a
    note, and the traceback is logged."""
    import logging

    from rework.kernel.state.records import Chat

    row = await store.create_chat(Chat(title=""))

    async def _boom(message: Any) -> Any:
        raise RuntimeError("disk full")

    monkeypatch.setattr(store, "append_chat_message", _boom)
    chat = OrchestratorChat(ScriptedProvider(), _catalog(), "flash", store=store, chat_id=row.id)
    chat._provider.route("orchestrator", [ChatResult(text="the answer")])
    with caplog.at_level(logging.ERROR, logger="rework.chat"):
        result = await chat.send("hi")
    assert "the answer" in result.reply
    assert "not saved" in result.reply
    assert chat.transcript[-1][1] == result.reply
    errors = [r for r in caplog.records if r.name == "rework.chat" and r.exc_info]
    assert errors, "the persistence traceback must be logged"


def _cut_off() -> ChatResult:
    return ChatResult(text="", stop_reason="max_tokens")


@pytest.mark.asyncio
async def test_max_tokens_cut_off_retries_with_a_bigger_budget() -> None:
    """Thinking ate the whole budget: retry once with 2x the tokens, then answer."""
    provider = ScriptedProvider()
    provider.route("orchestrator", [_cut_off(), ChatResult(text="here you go")])
    chat = OrchestratorChat(provider, _catalog(), "flash", effort="high")
    result = await chat.send("think hard")
    assert result.reply == "here you go"
    first, second = provider.requests
    assert first.max_tokens >= 8_192, "effort raises the budget floor"
    assert second.max_tokens == first.max_tokens * 2
    assert second.effort == "high"  # first retry keeps the effort


@pytest.mark.asyncio
async def test_repeated_cut_offs_end_in_a_visible_diagnostic(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If the retries also return nothing, the owner gets a real line, never
    '(no reply)', and nothing empty enters the history."""
    import logging

    provider = ScriptedProvider()
    provider.route("orchestrator", [_cut_off(), _cut_off(), _cut_off(), _cut_off()])
    chat = OrchestratorChat(provider, _catalog(), "flash", effort="high")
    with caplog.at_level(logging.WARNING, logger="rework.chat"):
        result = await chat.send("think forever")
    assert "output token limit" in result.reply
    assert "(no reply)" not in result.reply
    assert chat.transcript[-1][1] == result.reply
    requests = provider.requests
    assert len(requests) == 3  # initial + bigger budget + lower effort
    assert requests[1].max_tokens == requests[0].max_tokens * 2
    assert requests[2].effort == "medium"  # the ladder then lowers the effort
    assert [m["role"] for m in chat._history] == ["user"], "no empty assistant turns"
    assert any(r.levelname == "ERROR" for r in caplog.records if r.name == "rework.chat")


@pytest.mark.asyncio
async def test_empty_assistant_turns_are_never_persisted(store: Any) -> None:
    """Anthropic rejects empty assistant messages: an empty model reply leaves
    no empty row in the store or the history, and the display still says why."""
    from rework.kernel.state.records import Chat

    row = await store.create_chat(Chat(title=""))
    provider = ScriptedProvider()
    provider.route("orchestrator", [ChatResult(text="", stop_reason="end_turn")])
    chat = OrchestratorChat(provider, _catalog(), "flash", store=store, chat_id=row.id)
    result = await chat.send("hi")
    assert result.reply and "(no reply)" not in result.reply
    stored = await store.chat_messages(row.id)
    for m in stored:
        if m.role == "assistant":
            assert m.body == {} or m.body.get("content"), "no empty assistant body"
            assert m.display, "no empty assistant display"
    assert [m["role"] for m in chat._history] == ["user"]


# -- the budget a proposal carries --------------------------------------------------


def _proposal_chat(arguments: dict[str, Any]) -> tuple[Any, Any]:
    provider = ScriptedProvider()
    provider.route(
        "orchestrator",
        [ChatResult(tool_calls=[ToolCall(id="c1", name="propose_mission", arguments=arguments)])],
    )
    return OrchestratorChat(provider, role_catalog(), "flash"), provider


async def test_propose_mission_keeps_the_budget_the_model_sized() -> None:
    """The model knows the task, so its sizing wins when it gives a usable one."""
    chat, _ = _proposal_chat(
        {
            "objective": "add a dark mode toggle to the settings page",
            "plan": "fix",
            "budget_usd": 4.5,
            "budget_reason": "normal feature-sized change",
        }
    )
    result = await chat.send("add a dark mode toggle")
    assert result.proposal is not None
    assert result.proposal.budget_usd == 4.5
    assert result.proposal.budget_reason == "normal feature-sized change"


async def test_propose_mission_falls_back_to_the_estimate_without_a_budget() -> None:
    """No model budget (an old prompt, a model that skipped the optional field):
    the estimate sizes it, so a proposal is never unpriced."""
    from rework.budget_policy import estimate_budget

    objective = "fix the typo in the readme"
    chat, _ = _proposal_chat({"objective": objective, "plan": "fix"})
    result = await chat.send(objective)
    expected, expected_reason = estimate_budget(objective)
    assert result.proposal is not None
    assert result.proposal.budget_usd == expected > 0
    assert result.proposal.budget_reason == expected_reason
    # the docs-sized band, not the blanket default
    assert result.proposal.budget_usd <= 2.0


async def test_propose_mission_never_raises_on_garbage_budget_output() -> None:
    """A model that answers `"a lot"`, `-3`, `true` or NaN to `budget_usd` must
    not kill the turn: the estimate answers instead."""
    from rework.budget_policy import estimate_budget

    objective = "migrate the missions table to the new schema"
    expected, _ = estimate_budget(objective)
    for garbage in ("a lot", -3, 0, True, None, "NaN", "inf", []):
        chat, _ = _proposal_chat(
            {"objective": objective, "plan": "fix", "budget_usd": garbage, "budget_reason": "  "}
        )
        result = await chat.send(objective)
        assert result.proposal is not None, garbage
        # a large, risky brief: the estimator's $8+ band, and a reason line
        assert result.proposal.budget_usd >= 8.0, garbage
        assert result.proposal.budget_usd == expected, garbage
        assert result.proposal.budget_reason, garbage


async def test_proposal_summary_shows_the_budget_and_its_reason() -> None:
    """The transcript line is what the owner reads in the chat: it must carry
    the money, with the reason in parentheses."""
    from rework.orchestrator.chat import Proposal

    summary = Proposal(
        objective="fix the parser", budget_usd=3.0, budget_reason="normal feature-sized change"
    ).summary()
    assert "budget $3.00 (normal feature-sized change)" in summary
    assert Proposal(objective="x").summary().find("budget") == -1


async def test_a_replayed_proposal_card_keeps_its_budget() -> None:
    """proposal_from_args rebuilds the card from stored tool args: a reloaded
    chat must show the same budget, including the estimate for args saved
    before the model could price a mission."""
    from rework.budget_policy import estimate_budget
    from rework.tui.conversation import proposal_from_args

    priced = proposal_from_args(
        {
            "objective": "add dark mode",
            "plan": "fix",
            "budget_usd": 4.5,
            "budget_reason": "normal feature-sized change",
        }
    )
    assert priced.budget_usd == 4.5
    assert priced.budget_reason == "normal feature-sized change"

    legacy = proposal_from_args({"objective": "fix the typo in the readme", "plan": "fix"})
    expected, expected_reason = estimate_budget("fix the typo in the readme")
    assert legacy.budget_usd == expected > 0
    assert legacy.budget_reason == expected_reason


async def test_a_replayed_card_is_priced_like_the_live_one(store: Any) -> None:
    """Parity with the live path: `chat.py` sizes a proposal with
    `typical_finished_cost(store)`, so a replay that SKIPPED it showed (and then
    stamped at launch) a different cap for the same stored arguments.

    A dear shop: the $3-5 feature band is nudged to $4.50 live, and the replay
    from the same stored args must land on exactly that number."""
    from rework.budget_policy import typical_finished_cost
    from rework.kernel.state.records import Charge, MissionStatus
    from rework.kernel.state.records import Mission as M
    from rework.tui.conversation import proposal_from_args

    for mid, cost in (("m-done1", 2.0), ("m-done2", 4.0)):
        await store.create_mission(M(id=mid, objective="earlier", status=MissionStatus.DONE))
        await store.charge(
            Charge(mission_id=mid, model="m", input_tokens=10, output_tokens=10, cost_usd=cost)
        )
    live_history = await typical_finished_cost(store)
    assert live_history == 3.0
    args = {"objective": "add a feature to the parser, " * 20, "plan": "fix"}
    live = proposal_from_args(args, cost_history=live_history)
    assert live.budget_usd == 4.5

    blind = proposal_from_args(args)  # a caller with no store: keyword estimate only
    assert blind.budget_usd == 5.0, "without the history the band's ceiling is used"
    assert live.budget_usd != blind.budget_usd, "the history is what this test is about"
