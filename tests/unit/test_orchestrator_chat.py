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
