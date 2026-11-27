"""services/snapshot.build over BOTH store backends: the daemon answers
/v1/snapshot from Postgres in production, so its queries are pinned there too."""

from __future__ import annotations

import time
from typing import Any

import pytest

from rework.kernel.events.bus import InProcessEventBus
from rework.kernel.gates import GateManager
from rework.kernel.state.records import (
    ORCHESTRATOR_CHAT_PREFIX,
    Charge,
    Chat,
    ChatMessage,
    EventRecord,
    EventType,
    GateKind,
    Mission,
    MissionStatus,
    Run,
    RunStatus,
)
from rework.services import snapshot


async def test_build_lists_what_the_heartbeat_shows(store: Any) -> None:
    await store.create_mission(Mission(id="m-a", objective="a", status=MissionStatus.RUNNING))
    await store.create_mission(Mission(id="m-b", objective="b", status=MissionStatus.DONE))
    await store.create_run(
        Run(id="m-a:s:echo:1", mission_id="m-a", role="echo", step="s", status=RunStatus.RUNNING)
    )
    await store.append_event(
        EventRecord(mission_id="m-a", run_id="m-a:s:echo:1", type=EventType.PROGRESS)
    )
    gate = await GateManager(store, InProcessEventBus(store)).request(
        "m-a", kind=GateKind.QUESTION, question="?"
    )
    old = await store.create_chat(Chat(title="older"))
    await store.append_chat_message(ChatMessage(chat_id=old.id, role="user", body={"text": "1"}))
    new = await store.create_chat(Chat(title="newer"))
    await store.append_chat_message(
        ChatMessage(chat_id=new.id, role="assistant", body={"text": "2"})
    )
    note_only = await store.create_chat(Chat(title="notes"))
    await store.append_chat_message(
        ChatMessage(chat_id=note_only.id, role="note", body={"text": "launched"})
    )
    orch = await store.create_chat(Chat(id=f"{ORCHESTRATOR_CHAT_PREFIX}m-a"))
    await store.append_chat_message(ChatMessage(chat_id=orch.id, role="user", body={"text": "x"}))
    await store.charge(Charge(mission_id="m-a", model="fake", cost_usd=0.5))

    snap = await snapshot.build(store, spend_since=time.time() - 3600)
    assert {m.id for m in snap["missions"]} == {"m-a", "m-b"}
    assert all(m.light for m in snap["missions"])
    assert [g.id for g in snap["gates"]] == [gate.id]
    assert [c.id for c in snap["chats"]] == [new.id, old.id], "newest message first"
    assert snap["spend_today"] == pytest.approx(0.5)
    assert {"m-a", "m-a:s:echo:1", "*"} <= set(snap["last_event"])
    assert snap["event_seq"] > 0 and snap["budget_raises"] == []

    # a JSON round trip (what the API sends) keeps every field the TUI reads
    back = snapshot.from_json(snapshot.to_json(snap))
    assert [m.id for m in back["missions"]] == [m.id for m in snap["missions"]]
    assert all(m.light for m in back["missions"])
    assert [c.last_message_at for c in back["chats"]] == [c.last_message_at for c in snap["chats"]]
    assert back["last_event"] == snap["last_event"]


async def test_build_on_an_empty_store(store: Any) -> None:
    snap = await snapshot.build(store)
    assert snap["missions"] == [] and snap["chats"] == [] and snap["gates"] == []
    assert snap["event_seq"] == 0 and snap["spend_today"] == 0.0 and snap["last_event"] == {}
