"""Both raise paths through the REAL engine: request_budget (the orchestrator
asking) and the burn gate (the engine noticing the cap).

Under the budget policy the first raises inside 2x the original cap are
automatic: the cap moves, no gate opens, the mission keeps running, and the
owner still hears about it (a BUDGET_RAISED event and REWORK_NOTIFY_CMD). Past
the ceiling — or once the automatic raises are used — the owner is asked, and
the question carries the earlier raises.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from conftest import ScriptedProvider, role_catalog
from rework.kernel.events.bus import InProcessEventBus
from rework.kernel.gates import GateManager
from rework.kernel.llm.types import ChatResult, ToolCall, Usage
from rework.kernel.state.records import EventType, GateStatus, Mission, MissionStatus
from rework.kernel.state.sqlite import SqliteStore, open_sqlite
from rework.missions import orchestrated_plan
from rework.orchestrator.agent import MARKER, orchestrator_chat_id
from rework.orchestrator.engine import MissionRunner
from rework.roles.base import RoleSpec
from rework.roles.registry import RoleRegistry, autodiscover

OBJECTIVE = "make add(2, 3) == 5 in toyrepo"


def call(name: str, cid: str, **args: Any) -> ToolCall:
    return ToolCall(id=cid, name=name, arguments=args)


def turn(*calls: ToolCall, text: str = "") -> ChatResult:
    return ChatResult(
        text=text, tool_calls=list(calls), usage=Usage(input_tokens=500, output_tokens=40)
    )


@pytest.fixture(autouse=True)
def _clean_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """A dev box's ~/.rework/rework.env must not decide these tests, and no
    ambient notifier may fire."""
    for name in ("REWORK_BUDGET_AUTO_RAISES", "REWORK_BUDGET_CEILING_FACTOR", "REWORK_NOTIFY_CMD"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def toy_repo(tmp_path: Path) -> Path:
    source = Path(__file__).parents[2] / "examples" / "toyrepo"
    dest = tmp_path / "mission" / "toyrepo"
    dest.mkdir(parents=True)
    for f in source.iterdir():
        shutil.copy(f, dest / f.name)
    for args in (
        ["git", "init", "-b", "main"],
        ["git", "add", "-A"],
        ["git", "-c", "user.email=r@r", "-c", "user.name=r", "commit", "-m", "init"],
    ):
        subprocess.run(args, cwd=dest, check=True, capture_output=True)
    return dest


@pytest.fixture
async def world(tmp_path: Path, toy_repo: Path) -> AsyncIterator[dict[str, Any]]:
    db = str(tmp_path / "raises.sqlite3")
    store: SqliteStore = await open_sqlite(db)
    try:
        plan = orchestrated_plan("fix", "pro", budget_usd=2.0)
        mission = await store.create_mission(
            Mission(id="mo", objective=OBJECTIVE, repo_path=str(toy_repo), plan=plan)
        )
        yield {
            "db": db,
            "store": store,
            "provider": ScriptedProvider(),
            "mission": mission,
            "ck": str(tmp_path / "ck.sqlite3"),
            "workroot": tmp_path / "work",
            "registry": autodiscover(),
            "catalog": role_catalog(),
        }
    finally:
        await store.aclose()


def runner(world: dict[str, Any]) -> MissionRunner:
    from rework.orchestrator.daemon import sqlite_checkpointer_factory

    return MissionRunner(
        world["mission"].id,
        world["store"],
        InProcessEventBus(world["store"]),
        world["registry"],
        world["catalog"],
        sqlite_checkpointer_factory(world["ck"]),
        world["workroot"],
        poll_interval_s=0.05,
        provider=world["provider"],
    )


async def _open_gates(store: SqliteStore) -> list[Any]:
    return await store.list_open_gates("mo")


async def _wait_open_gate(store: SqliteStore, within_s: float = 20) -> Any:
    for _ in range(int(within_s / 0.1)):
        gates = await _open_gates(store)
        if gates:
            return gates[0]
        await asyncio.sleep(0.1)
    raise AssertionError("no gate opened")


async def _decide(world: dict[str, Any], gate_id: str) -> None:
    second = await open_sqlite(world["db"])
    try:
        await GateManager(second, InProcessEventBus(second)).decide(gate_id, approved=True)
    finally:
        await second.aclose()


def _budget(mission: Mission | None) -> dict[str, Any]:
    return ((mission.plan if mission else None) or {}).get("budget") or {}


def _tool_text(msgs: list[Any], needle: str) -> bool:
    return any(needle in m.body.get("content", "") for m in msgs if m.role == "tool")


# -- request_budget ---------------------------------------------------------


async def test_the_first_two_request_budget_raises_are_automatic(world: dict[str, Any]) -> None:
    """$2 -> $3 -> $4 without opening a gate, and the cap really moves."""
    provider: ScriptedProvider = world["provider"]
    store: SqliteStore = world["store"]
    provider.route(
        MARKER,
        [
            turn(call("request_budget", "b1", max_usd=3, reason="one more coder")),
            turn(call("request_budget", "b2", max_usd=4, reason="audit loop")),
            turn(call("finish", "b3", outcome="failed", summary="checked budget")),
        ],
    )
    await asyncio.wait_for(asyncio.create_task(runner(world).run()), timeout=60)

    mission = await store.get_mission("mo")
    budget = _budget(mission)
    assert budget["max_usd"] == 4.0
    assert budget["original_max_usd"] == 2.0
    assert budget["auto_raises"] == 2
    assert [r["new"] for r in budget["raises"]] == [3.0, 4.0]
    assert await store.list_open_gates("mo") == [], "no gate for an automatic raise"

    events = await store.events_since(0, mission_id="mo")
    raised = [e for e in events if e.type == EventType.BUDGET_RAISED]
    assert [(e.payload["old"], e.payload["new"], e.payload["auto"]) for e in raised] == [
        (2.0, 3.0, True),
        (3.0, 4.0, True),
    ]
    assert not [e for e in events if e.type == EventType.GATE_OPENED]

    gates = await store.list_gates("mo")
    assert len(gates) == 2, "each auto raise leaves one decided history row"
    assert {g.status for g in gates} == {GateStatus.APPROVED}
    assert {g.decision_note for g in gates} == {"auto"}
    assert sorted(float((g.spec or {})["budget_to"]) for g in gates) == [3.0, 4.0]

    msgs = await store.chat_messages(orchestrator_chat_id("mo"))
    assert _tool_text(msgs, "auto-approved: cap now $3.00 (auto raise 1 of 2")
    assert _tool_text(msgs, "auto-approved: cap now $4.00 (auto raise 2 of 2")


async def test_the_third_raise_opens_a_gate_listing_the_earlier_ones(
    world: dict[str, Any],
) -> None:
    provider: ScriptedProvider = world["provider"]
    store: SqliteStore = world["store"]
    provider.route(
        MARKER,
        [
            turn(call("request_budget", "b1", max_usd=3, reason="one more coder")),
            turn(call("request_budget", "b2", max_usd=4, reason="audit loop")),
            turn(call("request_budget", "b3", max_usd=5, reason="fix the flake")),
            turn(call("finish", "b4", outcome="failed", summary="checked budget")),
        ],
    )
    task = asyncio.create_task(runner(world).run())
    gate = await _wait_open_gate(store)
    assert "$4.00 to $5.00" in gate.question
    assert "Earlier raises: $2.00 -> $3.00 (auto): one more coder; " in gate.question
    assert "$3.00 -> $4.00 (auto): audit loop." in gate.question
    assert "Original budget $2.00." in gate.question
    await _decide(world, gate.id)
    await asyncio.wait_for(task, timeout=60)

    budget = _budget(await store.get_mission("mo"))
    assert budget["max_usd"] == 5.0
    assert budget["auto_raises"] == 2, "the manual raise is not an automatic one"
    assert [r["auto"] for r in budget["raises"]] == [True, True, False]


async def test_a_first_raise_beyond_the_ceiling_opens_a_gate(world: dict[str, Any]) -> None:
    """2x the original $2 is $4: a $10 first ask is the owner's call."""
    provider: ScriptedProvider = world["provider"]
    store: SqliteStore = world["store"]
    provider.route(
        MARKER,
        [
            turn(call("request_budget", "b1", max_usd=10, reason="whole rewrite")),
            turn(call("finish", "b2", outcome="failed", summary="checked budget")),
        ],
    )
    task = asyncio.create_task(runner(world).run())
    gate = await _wait_open_gate(store)
    assert "$2.00 to $10.00" in gate.question
    assert "Original budget $2.00." in gate.question
    assert "Earlier raises:" not in gate.question, "nothing was raised yet"
    await _decide(world, gate.id)
    await asyncio.wait_for(task, timeout=60)
    budget = _budget(await store.get_mission("mo"))
    assert budget["max_usd"] == 10.0
    assert budget["auto_raises"] == 0


async def test_a_project_can_turn_auto_raises_off(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The kill switch is per project too, not just per deployment."""
    from rework.kernel.state.records import Project

    provider: ScriptedProvider = world["provider"]
    store: SqliteStore = world["store"]
    await store.create_project(Project(id="p1", name="careful", settings={"budget_auto_raises": 0}))
    await store.update_mission("mo", project_id="p1")
    world["mission"] = await store.get_mission("mo") or world["mission"]
    provider.route(
        MARKER,
        [
            turn(call("request_budget", "b1", max_usd=3, reason="one more coder")),
            turn(call("finish", "b2", outcome="failed", summary="checked budget")),
        ],
    )
    task = asyncio.create_task(runner(world).run())
    gate = await _wait_open_gate(store)
    assert "$2.00 to $3.00" in gate.question
    await _decide(world, gate.id)
    await asyncio.wait_for(task, timeout=60)
    budget = _budget(await store.get_mission("mo"))
    assert budget["max_usd"] == 3.0
    assert budget["auto_raises"] == 0


# -- the burn gate ----------------------------------------------------------


class _QuickSpec(RoleSpec):
    """Completes without interrupting: enough substrate for a burn gate."""

    name = "quick"
    description = "test role: finishes immediately"
    goal = "just finish"
    default_model = "flash"

    def build_graph(self, ctx: Any) -> Any:
        from langgraph.graph import END, START, StateGraph

        async def work(state: dict) -> dict:  # type: ignore[type-arg]
            return {"artifact": {"kind": "quick"}}

        graph = StateGraph(dict)  # type: ignore[arg-type]
        graph.add_node("work", work)
        graph.add_edge(START, "work")
        graph.add_edge("work", END)
        return graph.compile()


@pytest.fixture
def burn_registry() -> RoleRegistry:
    registry = RoleRegistry()
    registry.register(_QuickSpec)
    return registry


async def _burn_world(
    tmp_path: Path,
    burn_registry: RoleRegistry,
    *,
    cap: float = 2.0,
    step: float = 2.0,
    spent: float = 3.0,
    budget_extra: dict[str, Any] | None = None,
) -> tuple[MissionRunner, SqliteStore, InProcessEventBus]:
    from rework.kernel.state.records import Charge
    from rework.orchestrator.daemon import sqlite_checkpointer_factory

    store = await open_sqlite(str(tmp_path / "burn.sqlite3"))
    bus = InProcessEventBus(store)
    await store.create_mission(
        Mission(
            id="mb",
            objective="spendy",
            plan={
                "steps": [{"step": "do", "role": "quick"}],
                "budget": {
                    "max_usd": cap,
                    "ask_every_usd": step,
                    "original_max_usd": 2.0,
                    "auto_raises": 0,
                    "raises": [],
                    **(budget_extra or {}),
                },
            },
        )
    )
    await store.charge(
        Charge(mission_id="mb", model="m", input_tokens=10, output_tokens=10, cost_usd=spent)
    )
    runner = MissionRunner(
        "mb",
        store,
        bus,
        burn_registry,
        role_catalog(),
        sqlite_checkpointer_factory(str(tmp_path / "ckb.sqlite3")),
        tmp_path / "wb",
        poll_interval_s=0.05,
    )
    return runner, store, bus


async def _run_and_watch(
    runner: MissionRunner, store: SqliteStore, answer: bool | None
) -> list[str]:
    """Run to completion; with `answer` given, approve/reject every gate that
    opens. Returns the questions of the gates that were answered."""
    asked: list[str] = []
    gm = GateManager(store, InProcessEventBus(store))

    async def answerer() -> None:
        for _ in range(400):
            gates = await store.list_open_gates("mb")
            if gates and answer is not None:
                asked.append(gates[0].question)
                await gm.decide(gates[0].id, approved=answer)
            await asyncio.sleep(0.05)

    task = asyncio.create_task(answerer())
    try:
        await asyncio.wait_for(asyncio.create_task(runner.run()), timeout=60)
    finally:
        await runner._supervisor.aclose()
        task.cancel()
    return asked


async def test_the_first_burn_gate_raise_is_automatic_and_does_not_hold(
    tmp_path: Path, burn_registry: RoleRegistry
) -> None:
    """The cap is hit mid-mission: the policy raises it and the mission keeps
    working — no ON_HOLD, no cancelled roles, no gate."""
    runner, store, _bus = await _burn_world(tmp_path, burn_registry)
    seen_hold = False

    async def watch() -> None:
        nonlocal seen_hold
        for _ in range(400):
            mission = await store.get_mission("mb")
            if mission is not None and mission.status == MissionStatus.ON_HOLD:
                seen_hold = True
            await asyncio.sleep(0.02)

    watcher = asyncio.create_task(watch())
    try:
        asked = await _run_and_watch(runner, store, answer=None)
    finally:
        watcher.cancel()
    assert asked == [], "an automatic raise must not open a gate"
    assert not seen_hold, "an automatic raise must not put the mission on hold"
    budget = _budget(await store.get_mission("mb"))
    assert budget["max_usd"] == 4.0
    assert budget["auto_raises"] == 1
    events = await store.events_since(0, mission_id="mb")
    assert [e.payload["new"] for e in events if e.type == EventType.BUDGET_RAISED] == [4.0]
    await store.aclose()


async def test_a_burn_gate_beyond_the_ceiling_asks_with_the_earlier_raises(
    tmp_path: Path, burn_registry: RoleRegistry
) -> None:
    """$4 cap, $2 step -> $6 is past 2x the original $2: the owner decides, and
    the question carries what was already raised."""
    runner, store, _bus = await _burn_world(
        tmp_path,
        burn_registry,
        cap=4.0,
        step=2.0,
        spent=5.0,
        budget_extra={
            "auto_raises": 1,
            "raises": [{"at": 1.0, "old": 2.0, "new": 4.0, "reason": "first", "auto": True}],
        },
    )
    asked = await _run_and_watch(runner, store, answer=True)
    burn = [q for q in asked if "Burn cap hit" in q]
    assert burn, f"the burn gate must fire, saw {asked}"
    assert "Continue up to $6.00" in burn[0]
    assert "Earlier raises: $2.00 -> $4.00 (auto): first." in burn[0]
    assert "Original budget $2.00." in burn[0]
    budget = _budget(await store.get_mission("mb"))
    assert budget["max_usd"] == 6.0
    assert [r["auto"] for r in budget["raises"]] == [True, False]
    await store.aclose()
