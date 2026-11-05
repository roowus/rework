"""The agent-loop trajectory: events emitted per turn/tool call and how they
render. Owner: 'the trajectory of the agent loop should be shown just like how
cc or pi does it'."""

from __future__ import annotations

import pytest

from rework.kernel.budget.ledger import Budget
from rework.kernel.events.bus import InProcessEventBus
from rework.kernel.llm.catalog import ModelCatalog, ModelEntry
from rework.kernel.llm.types import ChatResult, ToolCall, ToolSpec
from rework.kernel.state.records import EventType, Mission
from rework.nodes.toolloop import make_toolloop_node


class _EchoExecutor:
    def __init__(self, outputs: list[str]) -> None:
        self._outputs = outputs

    async def execute(self, name: str, args: dict) -> str:
        return self._outputs.pop(0) if self._outputs else "ok"

    def specs(self, allowed=None) -> list[ToolSpec]:
        return [ToolSpec(name="read_file"), ToolSpec(name="run_command")]


class _Provider:
    def __init__(self, results: list[ChatResult]) -> None:
        self._results = results

    async def complete(self, request):  # type: ignore[no-untyped-def]
        return self._results.pop(0)


@pytest.mark.asyncio
async def test_trajectory_events_emitted_per_turn_and_tool(store) -> None:  # type: ignore[no-untyped-def]
    mission = await store.create_mission(
        Mission(id="m-traj", objective="traj", plan={"kind": "echo"})
    )
    bus = InProcessEventBus(store)
    provider = _Provider(
        [
            ChatResult(
                text="looking around",
                tool_calls=[ToolCall(id="t1", name="read_file", arguments={"path": "a.py"})],
            ),
            ChatResult(text="done"),
        ]
    )
    node = make_toolloop_node(
        provider=provider,  # type: ignore[arg-type]
        catalog=ModelCatalog([ModelEntry(id="flash", provider="fake", model="f")]),
        catalog_id="flash",
        executor=_EchoExecutor(["file contents"]),  # type: ignore[arg-type]
        system_prompt="sys",
        store=store,
        mission_id=mission.id,
        run_id="m-traj:do:echo:1",
        budget=Budget(),
        bus=bus,
    )
    out = await node({"messages": [{"role": "user", "content": "go"}]})
    assert out["loop_turns"] == 2

    events = await store.events_since(0, mission_id=mission.id)
    traj = [e.payload for e in events if (e.payload or {}).get("trajectory")]
    kinds = [p["trajectory"] for p in traj]
    assert kinds == ["turn", "tool_call", "tool_result", "turn"], kinds
    assert traj[0]["tools"] == ["read_file"]
    assert traj[1]["arguments"].startswith("path=a.py")
    assert traj[2]["result"] == "file contents"
    assert all(e.type is EventType.PROGRESS for e in events if (e.payload or {}).get("trajectory"))


@pytest.mark.asyncio
async def test_no_bus_means_no_trajectory_noise(store) -> None:  # type: ignore[no-untyped-def]
    """Tests and embedders that pass no bus must not gain virtual events."""
    mission = await store.create_mission(
        Mission(id="m-quiet", objective="q", plan={"kind": "echo"})
    )
    node = make_toolloop_node(
        provider=_Provider([ChatResult(text="done")]),  # type: ignore[arg-type]
        catalog=ModelCatalog([ModelEntry(id="flash", provider="fake", model="f")]),
        catalog_id="flash",
        executor=_EchoExecutor([]),  # type: ignore[arg-type]
        system_prompt="sys",
        store=store,
        mission_id=mission.id,
        run_id="r",
        budget=Budget(),
    )
    await node({"messages": [{"role": "user", "content": "go"}]})
    events = await store.events_since(0, mission_id=mission.id)
    assert not [e for e in events if (e.payload or {}).get("trajectory")]


def test_format_event_line_shapes() -> None:
    from rework.kernel.state.records import EventRecord
    from rework.tui.app import format_event_line

    turn = EventRecord(
        mission_id="m",
        run_id="r",
        type=EventType.PROGRESS,
        payload={
            "trajectory": "turn",
            "turn": 3,
            "text": "reading",
            "tools": ["grep"],
            "tokens": 1200,
        },
    )
    line = format_event_line(turn, "investigate")
    assert "turn 3" in line and "reading" in line and "grep" in line and "1,200 tok" in line

    err = EventRecord(
        mission_id="m",
        run_id="r",
        type=EventType.PROGRESS,
        payload={"trajectory": "tool_result", "name": "read", "error": "error: nope"},
    )
    assert "error: nope" in format_event_line(err, "code")
