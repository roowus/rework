"""M3 chaos tests: the failures that happen in production, in test form.

- kill mid-role (the tool call): restart resumes from the last checkpoint
- gate decided while the daemon is fully DOWN: restart honors the row, no
  duplicate gate (role escalation gates AND mission-level ship gates)
- burn-gate rejection: mission stays CANCELLED, synthesis may not resurrect
- two daemons on one database: the loser is refused (leader lock)
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from conftest import role_catalog
from rework.config import Config
from rework.kernel.events.bus import InProcessEventBus
from rework.kernel.gates import GateManager
from rework.kernel.state.records import (
    Charge,
    Mission,
    MissionStatus,
    new_id,
)
from rework.kernel.state.sqlite import SqliteStore, open_sqlite
from rework.orchestrator.daemon import Daemon, DaemonLocked, sqlite_checkpointer_factory
from rework.orchestrator.engine import MissionRunner
from rework.roles.base import RoleBuildCtx, RoleSpec
from rework.roles.registry import RoleRegistry


class CrashySpec(RoleSpec):
    """work (fast, checkpoint lands after it) -> grind (the tool call: slow,
    killable mid-flight) -> gate (pause for the human)."""

    name = "crashy"
    description = "test role: killable mid-graph"
    goal = "grind then ask"
    default_model = "flash"

    def build_graph(self, ctx: RoleBuildCtx):  # type: ignore[no-untyped-def]
        from langgraph.graph import END, START, StateGraph
        from langgraph.types import interrupt

        async def work(state: dict) -> dict:  # type: ignore[no-untyped-def]
            return {"worked": True}

        async def grind(state: dict) -> dict:  # type: ignore[no-untyped-def]
            await asyncio.sleep(0.6)  # the "tool call" the kill lands inside
            return {"ground": True}

        async def gate(state: dict) -> dict:  # type: ignore[no-untyped-def]
            decision = interrupt({"kind": "question", "detail": "finish it?"})
            return {
                "artifact": {
                    "approved": bool(decision),
                    "ground": bool(state.get("ground")),
                }
            }

        graph = StateGraph(dict)  # type: ignore[arg-type]
        graph.add_node("work", work)
        graph.add_node("grind", grind)
        graph.add_node("gate", gate)
        graph.add_edge(START, "work")
        graph.add_edge("work", "grind")
        graph.add_edge("grind", "gate")
        graph.add_edge("gate", END)
        return graph.compile()


class QuickSpec(RoleSpec):
    """Completes without interrupting: the substrate for mission-level gate
    (ship) chaos tests."""

    name = "quick"
    description = "test role: finishes immediately"
    goal = "just finish"
    default_model = "flash"

    def build_graph(self, ctx: RoleBuildCtx):  # type: ignore[no-untyped-def]
        from langgraph.graph import END, START, StateGraph

        async def work(state: dict) -> dict:  # type: ignore[no-untyped-def]
            return {"artifact": {"kind": "quick"}}

        graph = StateGraph(dict)  # type: ignore[arg-type]
        graph.add_node("work", work)
        graph.add_edge(START, "work")
        graph.add_edge("work", END)
        return graph.compile()


@pytest.fixture
def registry() -> RoleRegistry:
    reg = RoleRegistry()
    reg.register(CrashySpec)
    reg.register(QuickSpec)
    return reg


@pytest.fixture
async def world(tmp_path: Path, registry: RoleRegistry) -> AsyncIterator[dict[str, Any]]:  # type: ignore[no-untyped-def]
    db_path = tmp_path / "chaos.sqlite3"
    ck_path = tmp_path / "checkpoints.sqlite3"
    store: SqliteStore = await open_sqlite(str(db_path))
    try:
        yield {
            "store": store,
            "bus": InProcessEventBus(store),
            "db_path": str(db_path),
            "ck_path": str(ck_path),
            "registry": registry,
            "catalog": role_catalog(),
            "workroot": tmp_path / "work",
            "tmp": tmp_path,
            "config": Config(db=str(db_path), checkpoints_db=str(ck_path), poll_interval_s=0.05),
        }
    finally:
        await store.aclose()


def _runner(world: dict[str, Any], mission_id: str) -> MissionRunner:  # type: ignore[no-untyped-def]
    return MissionRunner(
        mission_id,
        world["store"],
        world["bus"],
        world["registry"],
        world["catalog"],
        sqlite_checkpointer_factory(world["ck_path"]),
        world["workroot"],
        poll_interval_s=0.05,
    )


async def _wait_for(  # type: ignore[no-untyped-def]
    predicate, timeout_s: float = 10.0, what: str = "condition"
) -> None:
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.05)
    raise TimeoutError(f"timed out waiting for {what}")


async def test_kill_mid_role_resumes_from_checkpoint(world: dict[str, Any]) -> None:
    """Kill inside grind (the tool call), AFTER work's checkpoint landed. A
    fresh runner must resume from that checkpoint, replay grind, reach the
    gate, and complete on approval — exactly one run row (no duplicate)."""
    store: SqliteStore = world["store"]
    mission = await store.create_mission(
        Mission(
            id=new_id("m"),
            objective="survive a mid-flight kill",
            plan={"steps": [{"step": "do", "role": "crashy"}]},
        )
    )

    runner1 = _runner(world, mission.id)
    task1 = asyncio.create_task(runner1.run())
    await _wait_for(lambda: _run_status_is(store, mission.id, "running"), what="run started")
    await asyncio.sleep(0.2)  # work done (checkpointed), mid-grind now
    task1.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task1, timeout=5)
    if runner1._supervisor is not None:
        await runner1._supervisor.aclose()

    # the kill left the run RUNNING with work's checkpoint on the thread
    runs = await store.list_runs(mission.id)
    assert len(runs) == 1 and runs[0].status.value == "running"

    runner2 = _runner(world, mission.id)
    task2 = asyncio.create_task(runner2.run())
    await _wait_for(lambda: _open_gate(store, mission.id), what="gate after resume")
    second: SqliteStore = await open_sqlite(world["db_path"])
    try:
        gates = GateManager(second, InProcessEventBus(second))
        open_gates = await gates.open_gates(mission.id)
        await gates.decide(open_gates[0].id, approved=True)
    finally:
        await second.aclose()

    await asyncio.wait_for(task2, timeout=60)
    fresh = await store.get_run(runs[0].id)
    assert fresh is not None and fresh.status.value == "completed"
    assert (fresh.artifact or {}).get("approved") is True
    assert (fresh.artifact or {}).get("ground") is True  # grind ran to completion
    mission_row = await store.get_mission(mission.id)
    assert mission_row is not None and mission_row.status == MissionStatus.DONE


async def test_gate_decided_while_down_applied_on_restart(world: dict[str, Any]) -> None:
    """The owner answers while the daemon is DOWN. Restart must apply the
    decision from the row: approved resumes the role, no second gate opens."""
    store: SqliteStore = world["store"]
    mission = await store.create_mission(
        Mission(
            id=new_id("m"),
            objective="decide while I am gone",
            plan={"steps": [{"step": "do", "role": "crashy"}]},
        )
    )
    runner1 = _runner(world, mission.id)
    task1 = asyncio.create_task(runner1.run())
    await _wait_for(lambda: _open_gate(store, mission.id), what="gate to open")
    task1.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task1, timeout=5)
    if runner1._supervisor is not None:
        await runner1._supervisor.aclose()

    # the decision lands while nothing is running (the daemon is "down")
    second: SqliteStore = await open_sqlite(world["db_path"])
    try:
        gates = GateManager(second, InProcessEventBus(second))
        open_gates = await gates.open_gates(mission.id)
        await gates.decide(open_gates[0].id, approved=True, note="from my phone")
    finally:
        await second.aclose()

    runner2 = _runner(world, mission.id)
    task2 = asyncio.create_task(runner2.run())
    # crash/restart tests: these runners are ~2x slower than a laptop and
    # have blown 15s and 30s here; the assertion is RESUME, not speed
    await asyncio.wait_for(task2, timeout=60)

    # exactly ONE gate ever existed: no duplicate was opened for the answer
    events = await store.events_since(0, mission_id=mission.id)
    opened = [e for e in events if e.type.value == "gate_opened"]
    assert len(opened) == 1, f"restart opened duplicate gates: {len(opened)}"
    fresh = await store.get_run(f"{mission.id}:do:crashy:1")
    assert fresh is not None and fresh.status.value == "completed"
    assert (fresh.artifact or {}).get("approved") is True
    mission_row = await store.get_mission(mission.id)
    assert mission_row is not None and mission_row.status == MissionStatus.DONE


async def test_escalation_gate_links_to_its_run_on_the_row(world: dict[str, Any]) -> None:
    """The gate->run link is a COLUMN, not a follow-up event.

    Reproduces what timed out on a 3.13 CI runner: the engine used to publish
    an INFO event carrying {gate_id, for_run} AFTER committing the gate row. A
    kill between the two left a gate no one could attribute, so recovery never
    applied the owner's answer and the run sat HALTED_ESCALATED until the test
    window closed. Here the pointer event is deleted outright: recovery must
    still find the gate and finish."""
    store: SqliteStore = world["store"]
    mission = await store.create_mission(
        Mission(
            id=new_id("m"),
            objective="orphan-proof gate",
            plan={"steps": [{"step": "do", "role": "crashy"}]},
        )
    )
    runner1 = _runner(world, mission.id)
    task1 = asyncio.create_task(runner1.run())
    await _wait_for(lambda: _open_gate(store, mission.id), what="gate to open")
    task1.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task1, timeout=5)
    if runner1._supervisor is not None:
        await runner1._supervisor.aclose()

    run_id_value = f"{mission.id}:do:crashy:1"
    second: SqliteStore = await open_sqlite(world["db_path"])
    try:
        gates = GateManager(second, InProcessEventBus(second))
        open_gates = await gates.open_gates(mission.id)
        assert open_gates and open_gates[0].for_run == run_id_value, (
            "the gate row must name the run it unblocks"
        )
        # simulate the lost-write window: no event ever carried the pointer.
        # (the event table is the store's own substrate; a test reaching into
        # it is modelling a process that died before the publish, which no
        # public method can express)
        await second._db.execute("DELETE FROM events WHERE payload LIKE '%for_run%'")
        await second._db.commit()
        await gates.decide(open_gates[0].id, approved=True)
    finally:
        await second.aclose()

    runner2 = _runner(world, mission.id)
    await asyncio.wait_for(asyncio.create_task(runner2.run()), timeout=60)
    fresh = await store.get_run(run_id_value)
    assert fresh is not None and fresh.status.value == "completed"
    assert (fresh.artifact or {}).get("approved") is True


async def test_gate_rejected_while_down_aborts_on_restart(world: dict[str, Any]) -> None:
    """Same, but the answer is no: the run aborts from the row's decision and
    the mission still completes (the plan moves on)."""
    store: SqliteStore = world["store"]
    mission = await store.create_mission(
        Mission(
            id=new_id("m"),
            objective="reject while I am gone",
            plan={"steps": [{"step": "do", "role": "crashy"}]},
        )
    )
    runner1 = _runner(world, mission.id)
    task1 = asyncio.create_task(runner1.run())
    await _wait_for(lambda: _open_gate(store, mission.id), what="gate to open")
    task1.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task1, timeout=5)
    if runner1._supervisor is not None:
        await runner1._supervisor.aclose()

    second: SqliteStore = await open_sqlite(world["db_path"])
    try:
        gates = GateManager(second, InProcessEventBus(second))
        open_gates = await gates.open_gates(mission.id)
        await gates.decide(open_gates[0].id, approved=False)
    finally:
        await second.aclose()

    runner2 = _runner(world, mission.id)
    task2 = asyncio.create_task(runner2.run())
    await asyncio.wait_for(task2, timeout=60)
    fresh = await store.get_run(f"{mission.id}:do:crashy:1")
    assert fresh is not None and fresh.status.value == "aborted"
    mission_row = await store.get_mission(mission.id)
    assert mission_row is not None and mission_row.status == MissionStatus.DONE


async def test_ship_gate_decided_while_down_not_duplicated(world: dict[str, Any]) -> None:
    """Mission-level ship gate decided while down: restart reads the EXISTING
    row instead of opening a second gate for an answer already given."""
    store: SqliteStore = world["store"]
    mission = await store.create_mission(
        Mission(
            id=new_id("m"),
            objective="ship gate while down",
            plan={"steps": [{"step": "do", "role": "quick"}], "gates": ["ship"]},
        )
    )
    runner1 = _runner(world, mission.id)
    task1 = asyncio.create_task(runner1.run())
    await _wait_for(lambda: _open_gate(store, mission.id), what="ship gate")
    task1.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task1, timeout=5)
    if runner1._supervisor is not None:
        await runner1._supervisor.aclose()

    second: SqliteStore = await open_sqlite(world["db_path"])
    try:
        gates = GateManager(second, InProcessEventBus(second))
        open_gates = await gates.open_gates(mission.id)
        assert open_gates[0].kind.value == "ship"
        await gates.decide(open_gates[0].id, approved=True)
    finally:
        await second.aclose()

    runner2 = _runner(world, mission.id)
    task2 = asyncio.create_task(runner2.run())
    await asyncio.wait_for(task2, timeout=60)
    events = await store.events_since(0, mission_id=mission.id)
    opened = [e for e in events if e.type.value == "gate_opened"]
    assert len(opened) == 1, "restart must reuse the decided ship gate"
    mission_row = await store.get_mission(mission.id)
    assert mission_row is not None and mission_row.status == MissionStatus.DONE


async def test_burn_gate_rejection_cancels_and_stays_cancelled(
    world: dict[str, Any],
) -> None:
    """Rejecting the burn cap must CANCEL the mission — and synthesis (which
    runs right after, with no failed runs) must not resurrect it as DONE."""
    store: SqliteStore = world["store"]
    mission = await store.create_mission(
        Mission(
            id=new_id("m"),
            objective="too spendy, abort",
            plan={
                "steps": [{"step": "do", "role": "quick"}],
                "budget": {"max_usd": 0.01},
            },
        )
    )
    await store.charge(
        Charge(mission_id=mission.id, model="m", input_tokens=10, output_tokens=10, cost_usd=0.02)
    )
    runner = _runner(world, mission.id)
    gm = GateManager(store, world["bus"])

    async def reject_burn_gate() -> None:
        for _ in range(200):
            gates = await store.list_open_gates(mission.id)
            if gates and "Burn cap hit" in gates[0].question:
                await gm.decide(gates[0].id, approved=False)
                return
            await asyncio.sleep(0.05)

    rejecter = asyncio.create_task(reject_burn_gate())
    task = asyncio.create_task(runner.run())
    await asyncio.wait_for(task, timeout=60)
    await rejecter

    mission_row = await store.get_mission(mission.id)
    assert mission_row is not None, "mission must exist"
    assert mission_row.status == MissionStatus.CANCELLED, (
        f"burn-gate rejection must cancel, got {mission_row.status}"
    )
    assert (mission_row.result or {}).get("reason") == "burn cap rejected"
    gate_rows = [
        e
        for e in await store.events_since(0, mission_id=mission.id)
        if e.type.value == "gate_opened"
    ]
    assert len(gate_rows) == 1


async def test_second_daemon_refused_until_lock_free(world: dict[str, Any]) -> None:
    """Two daemons, one database: the second is refused while the first's
    heartbeat is fresh; after the first releases, the lock is takeable."""
    config: Config = world["config"]
    d1 = Daemon(
        store=world["store"],
        bus=world["bus"],
        registry=world["registry"],
        catalog=world["catalog"],
        make_checkpointer=sqlite_checkpointer_factory(world["ck_path"]),
        workroot=world["workroot"],
        config=config,
    )
    t1 = asyncio.create_task(d1.run_forever())
    await _wait_for(lambda: _lock_held(world["store"], d1._owner), what="lock acquired")

    d2 = Daemon(
        store=world["store"],
        bus=world["bus"],
        registry=world["registry"],
        catalog=world["catalog"],
        make_checkpointer=sqlite_checkpointer_factory(world["ck_path"]),
        workroot=world["workroot"],
        config=config,
    )
    with pytest.raises(DaemonLocked):
        await asyncio.wait_for(d2.run_forever(), timeout=5)

    t1.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(t1, timeout=5)
    await _wait_for(lambda: _lock_free(world["store"]), what="lock released on shutdown")

    d3 = Daemon(
        store=world["store"],
        bus=world["bus"],
        registry=world["registry"],
        catalog=world["catalog"],
        make_checkpointer=sqlite_checkpointer_factory(world["ck_path"]),
        workroot=world["workroot"],
        config=config,
    )
    t3 = asyncio.create_task(d3.run_forever())
    await _wait_for(lambda: _lock_held(world["store"], d3._owner), what="takeover")
    t3.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(t3, timeout=5)


# -- helpers ------------------------------------------------------------------


async def _open_gate(store: SqliteStore, mission_id: str) -> bool:
    return bool(await store.list_open_gates(mission_id))


async def _run_status_is(store: SqliteStore, mission_id: str, status: str) -> bool:
    runs = await store.list_runs(mission_id)
    return any(r.status.value == status for r in runs)


async def _lock_held(store: SqliteStore, owner: str) -> bool:
    row = await store._fetch_one("SELECT owner FROM daemon_lock WHERE id = 1", ())
    return row is not None and row["owner"] == owner


async def _lock_free(store: SqliteStore) -> bool:
    row = await store._fetch_one("SELECT owner FROM daemon_lock WHERE id = 1", ())
    return row is None


async def test_typed_gate_answer_reaches_the_role(world: dict[str, Any]) -> None:
    """A gate answered with FEEDBACK, not just yes/no.

    The reported gap was that decide_gate saves a note but the engine resumes the
    role with a boolean only. This drives the whole path: the role parks on
    interrupt(), the owner answers with a dict, and the role's graph receives
    that dict (including `approved`) instead of a bare True.
    """
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import interrupt

    from rework.roles.base import RoleSpec

    seen: list[Any] = []

    class EchoDecisionSpec(RoleSpec):
        name = "echo_decision"
        description = "records whatever its interrupt() returned"
        goal = "record the decision"
        default_model = "flash"

        def build_graph(self, ctx):  # type: ignore[no-untyped-def]
            async def ask(state: dict) -> dict:  # type: ignore[no-untyped-def]
                value = interrupt({"kind": "question", "detail": "how should I change it?"})
                seen.append(value)
                return {"artifact": {"decision": value}}

            graph = StateGraph(dict)  # type: ignore[arg-type]
            graph.add_node("ask", ask)
            graph.add_edge(START, "ask")
            graph.add_edge("ask", END)
            return graph.compile()

    world["registry"].register(EchoDecisionSpec)
    store: SqliteStore = world["store"]
    mission = await store.create_mission(
        Mission(
            id=new_id("m"),
            objective="answer me with feedback",
            plan={"steps": [{"step": "do", "role": "echo_decision"}]},
        )
    )
    task = asyncio.create_task(_runner(world, mission.id).run())
    await _wait_for(lambda: _open_gate(store, mission.id), what="gate to open")

    second: SqliteStore = await open_sqlite(world["db_path"])
    try:
        gates = GateManager(second, InProcessEventBus(second))
        open_gates = await gates.open_gates(mission.id)
        await gates.decide(
            open_gates[0].id,
            approved=False,
            note="needs work",
            decision={"feedback": "don't touch the parser", "changes": ["a", "b"]},
        )
    finally:
        await second.aclose()

    await asyncio.wait_for(task, timeout=60)
    assert seen == [
        {"feedback": "don't touch the parser", "changes": ["a", "b"], "approved": False}
    ], seen
    run = await store.get_run(f"{mission.id}:do:echo_decision:1")
    assert run is not None and run.artifact is not None
    assert run.artifact["decision"]["feedback"] == "don't touch the parser"


async def test_plain_gate_still_resumes_with_a_bool(world: dict[str, Any]) -> None:
    """Roles written before typed answers must keep receiving True/False."""
    store: SqliteStore = world["store"]
    mission = await store.create_mission(
        Mission(
            id=new_id("m"),
            objective="plain approval",
            plan={"steps": [{"step": "do", "role": "crashy"}]},
        )
    )
    task = asyncio.create_task(_runner(world, mission.id).run())
    await _wait_for(lambda: _open_gate(store, mission.id), what="gate to open")

    second: SqliteStore = await open_sqlite(world["db_path"])
    try:
        gates = GateManager(second, InProcessEventBus(second))
        open_gates = await gates.open_gates(mission.id)
        await gates.decide(open_gates[0].id, approved=True)  # no typed decision
    finally:
        await second.aclose()

    await asyncio.wait_for(task, timeout=60)
    run = await store.get_run(f"{mission.id}:do:crashy:1")
    assert run is not None and run.artifact is not None
    assert run.artifact["approved"] is True  # bool(decision) is True
