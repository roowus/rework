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
    chat = OrchestratorChat(
        provider, catalog, "flash", store=store, chat_id=chat_row.id
    )
    await chat.send("audit the parser")
    await chat.send("it lives in github.com/x/y")

    stored = await store.chat_messages(chat_row.id)
    assert [m.role for m in stored] == ["user", "assistant", "user", "assistant"]
    assert stored[0].display == "audit the parser"

    # a NEW instance for the same chat sees the history and replays it to the model
    reopened = OrchestratorChat(
        provider, catalog, "flash", store=store, chat_id=chat_row.id
    )
    shown = await reopened.load()
    assert [who for who, _ in shown] == ["you", "orchestrator", "you", "orchestrator"]
    assert reopened.transcript[0] == ("you", "audit the parser")

    await reopened.send("and again")
    # the replayed history reached the provider: 4 rows before + the new user turn
    assert len(provider.seen[-1]) == 5
    assert provider.seen[-1][0]["content"] == "audit the parser"
