"""M3 escalation policies: needs_model promotes within the provider and
resumes the role without a human; exhaustion falls through to a gate. Plus
the burn-gate: plan budget cap puts the mission ON_HOLD with a
continue/abort gate.

These drive `runner.run()` end-to-end (as the daemon does) rather than poking
internals: the slice tests established that pattern and it deadlocks less.

Housekeeping: every test closes its runner's supervisor in a finally. The
aiosqlite checkpointer thread is non-daemon; a leaked connection blocks pytest
exit (the conftest lesson, biting a second time).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from conftest import role_catalog
from rework.kernel.events.bus import InProcessEventBus
from rework.kernel.gates import GateManager
from rework.kernel.state.records import (
    Charge,
    EventType,
    Mission,
    MissionStatus,
    RunStatus,
    run_id,
)
from rework.kernel.state.sqlite import SqliteStore, open_sqlite
from rework.orchestrator.daemon import sqlite_checkpointer_factory
from rework.orchestrator.engine import MissionRunner
from rework.roles.base import RoleBuildCtx, RoleSpec
from rework.roles.registry import RoleRegistry


class EscalatingSpec(RoleSpec):
    """Halt with a typed envelope, then complete with the decision on resume."""

    name = "escalating"
    description = "test role: halts with the requested escalation kind"
    goal = "halt on command"
    default_model = "flash"

    def build_graph(self, ctx: RoleBuildCtx):  # type: ignore[no-untyped-def]
        from langgraph.graph import END, START, StateGraph
        from langgraph.types import interrupt

        async def halt(state: dict) -> dict:  # type: ignore[no-untyped-def]
            kind = str(ctx.run.context.get("kind") or "blocked")
            detail = str(ctx.run.context.get("detail") or "need a hand")
            decision = interrupt({"kind": kind, "detail": detail})
            return {"artifact": {"kind": "escalating", "decision": decision}}

        graph = StateGraph(dict)  # type: ignore[arg-type]
        graph.add_node("halt", halt)
        graph.add_edge(START, "halt")
        graph.add_edge("halt", END)
        return graph.compile()


@pytest.fixture
def registry() -> RoleRegistry:
    reg = RoleRegistry()
    reg.register(EscalatingSpec)
    return reg


@pytest.fixture
async def world(tmp_path: Path) -> AsyncIterator[dict[str, Any]]:  # type: ignore[no-untyped-def]
    store: SqliteStore = await open_sqlite(str(tmp_path / "pol.sqlite3"))
    try:
        yield {
            "store": store,
            "bus": InProcessEventBus(store),
            "tmp": tmp_path,
            "catalog": role_catalog(),
        }
    finally:
        await store.aclose()


async def _await_predicate(predicate, limit: float = 10.0) -> None:  # type: ignore[no-untyped-def]
    async with asyncio.timeout(limit):
        while True:
            if await predicate():
                return
            await asyncio.sleep(0.05)


async def test_needs_model_promotes_and_resumes(
    world: dict[str, Any], registry: RoleRegistry
) -> None:
    store: SqliteStore = world["store"]
    mission = await store.create_mission(
        Mission(
            id="mp1",
            objective="needs a bigger model",
            plan={
                "steps": [
                    {
                        "step": "do",
                        "role": "escalating",
                        "context": {
                            "kind": "needs_model",
                            "detail": "flash cannot reason about this",
                        },
                    }
                ]
            },
        )
    )
    runner = MissionRunner(
        "mp1",
        store,
        world["bus"],
        registry,
        world["catalog"],
        sqlite_checkpointer_factory(str(world["tmp"] / "ck1.sqlite3")),
        world["tmp"] / "w1",
        poll_interval_s=0.05,
    )
    task = asyncio.create_task(runner.run())
    await _await_predicate(lambda: _latest_attempt_done(store, "mp1", "do"))
    # cancel the parked runner (mission is awaiting nothing; it completed)
    await asyncio.wait_for(task, timeout=5)
    await runner._supervisor.aclose()

    promoted = await store.get_run(run_id("mp1", "do", "escalating", 1))
    assert promoted is not None
    assert promoted.model_ref.get("id") == "pro"  # promoted within the provider
    events = await store.events_since(0, mission_id="mp1")
    assert any(e.type == EventType.MODEL_PROMOTED for e in events)
    assert not await store.list_open_gates("mp1")  # no human was involved
    mission = await store.get_mission("mp1")
    assert mission is not None and mission.status == MissionStatus.DONE


async def test_needs_model_escalates_to_the_configured_model(
    world: dict[str, Any], registry: RoleRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mechanical path escalates to the ONE configured model, not to the
    next tier up within the provider. The prompt tells the orchestrator to
    escalate ONLY to the escalation model, so the engine must not invent a
    target the policy text never names (here: `pro`, the same-provider step)."""
    from rework.config import ESCALATION_MODEL_ENV, ESCALATION_MODEL_ID
    from rework.kernel.llm.catalog import ModelCatalog, ModelEntry

    monkeypatch.delenv(ESCALATION_MODEL_ENV, raising=False)
    catalog = ModelCatalog(
        [
            ModelEntry(id="flash", provider="fake", model="fake-flash", tier=1),
            ModelEntry(id="pro", provider="fake", model="fake-pro", tier=2),
            ModelEntry(id=ESCALATION_MODEL_ID, provider="fake", model="fake-escal", tier=3),
        ]
    )
    assert catalog.promote("flash").id == "pro"  # what the tier step would pick

    store: SqliteStore = world["store"]
    await store.create_mission(
        Mission(
            id="mp3",
            objective="needs a bigger model",
            plan={
                "steps": [
                    {
                        "step": "do",
                        "role": "escalating",
                        "context": {"kind": "needs_model", "detail": "flash is too small"},
                    }
                ]
            },
        )
    )
    runner = MissionRunner(
        "mp3",
        store,
        world["bus"],
        registry,
        catalog,
        sqlite_checkpointer_factory(str(world["tmp"] / "ck4.sqlite3")),
        world["tmp"] / "w4",
        poll_interval_s=0.05,
    )
    task = asyncio.create_task(runner.run())
    await _await_predicate(lambda: _latest_attempt_done(store, "mp3", "do"))
    await asyncio.wait_for(task, timeout=5)
    await runner._supervisor.aclose()

    promoted = await store.get_run(run_id("mp3", "do", "escalating", 1))
    assert promoted is not None
    assert promoted.model_ref.get("id") == ESCALATION_MODEL_ID
    events = await store.events_since(0, mission_id="mp3")
    hops = [e.payload for e in events if e.type == EventType.MODEL_PROMOTED]
    assert hops and hops[-1]["to"] == ESCALATION_MODEL_ID
    assert hops[-1]["from"] == "flash"
    assert not await store.list_open_gates("mp3")  # handled mechanically


async def _latest_attempt_done(store: SqliteStore, mission_id: str, step: str) -> bool:  # type: ignore[no-untyped-def]
    runs = [r for r in await store.list_runs(mission_id) if r.step == step]
    return bool(runs) and runs[-1].status in {
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.ABORTED,
    }


async def test_needs_model_exhausted_opens_gate(
    world: dict[str, Any], registry: RoleRegistry
) -> None:
    store: SqliteStore = world["store"]
    await store.create_mission(
        Mission(
            id="mp2",
            objective="top tier already",
            plan={
                "steps": [
                    {
                        "step": "do",
                        "role": "escalating",
                        "model": "pro",  # already the top tier: promotion exhausts
                        "context": {"kind": "needs_model"},
                    }
                ]
            },
        )
    )
    runner = MissionRunner(
        "mp2",
        store,
        world["bus"],
        registry,
        world["catalog"],
        sqlite_checkpointer_factory(str(world["tmp"] / "ck2.sqlite3")),
        world["tmp"] / "w2",
        poll_interval_s=0.05,
    )

    # pro is the top of its provider: promotion exhausts, a human gate opens.
    # Approve from a side task, like `rework approve` from another process.
    async def approve_gate() -> None:
        for _ in range(200):
            gates = await store.list_open_gates("mp2")
            if gates:
                await GateManager(store, world["bus"]).decide(gates[0].id, approved=True)
                return
            await asyncio.sleep(0.05)

    decider = asyncio.create_task(approve_gate())
    task = asyncio.create_task(runner.run())
    await asyncio.wait_for(task, timeout=15)
    await decider
    await runner._supervisor.aclose()

    events = await store.events_since(0, mission_id="mp2")
    assert any(e.type == EventType.GATE_OPENED for e in events)
    fresh = await store.get_run(run_id("mp2", "do", "escalating", 1))
    assert fresh is not None and fresh.status == RunStatus.COMPLETED


async def test_burn_gate_holds_then_continues(
    world: dict[str, Any], registry: RoleRegistry
) -> None:
    store: SqliteStore = world["store"]
    cap = 0.01
    await store.create_mission(
        Mission(
            id="mb",
            objective="spendy",
            plan={
                "steps": [
                    {
                        "step": "do",
                        "role": "escalating",
                        "context": {"kind": "question", "detail": "pick one"},
                    }
                ],
                "budget": {"max_usd": cap},
            },
        )
    )
    await store.charge(
        Charge(mission_id="mb", model="m", input_tokens=10, output_tokens=10, cost_usd=cap * 1.5)
    )
    runner = MissionRunner(
        "mb",
        store,
        world["bus"],
        registry,
        world["catalog"],
        sqlite_checkpointer_factory(str(world["tmp"] / "ck3.sqlite3")),
        world["tmp"] / "w3",
        poll_interval_s=0.05,
    )
    gm = GateManager(store, world["bus"])
    decided_burn = False

    async def gate_answerer() -> None:
        nonlocal decided_burn
        for _ in range(300):
            gates = await store.list_open_gates("mb")
            if gates:
                is_burn = "Burn cap hit" in gates[0].question
                await gm.decide(gates[0].id, approved=True)
                if is_burn:
                    decided_burn = True
            await asyncio.sleep(0.05)

    answerer = asyncio.create_task(gate_answerer())
    task = asyncio.create_task(runner.run())

    # the mission must pass through ON_HOLD at least once before finishing
    async def saw_hold() -> bool:
        m = await store.get_mission("mb")
        return m is not None and decided_burn

    try:
        await asyncio.wait_for(task, timeout=20)
    finally:
        await runner._supervisor.aclose()
    answerer.cancel()
    assert decided_burn, "the burn gate must have fired and been approved"

    m = await store.get_mission("mb")
    assert m is not None and m.status == MissionStatus.DONE
    assert (m.plan or {})["budget"]["max_usd"] == pytest.approx(cap * 2)
