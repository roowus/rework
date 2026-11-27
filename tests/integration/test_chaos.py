"""M3 chaos tests: the failures that happen in production, in test form.

- kill mid-role (the tool call): restart resumes from the last checkpoint
- gate decided while the daemon is fully DOWN: restart honors the row, no
  duplicate gate (role escalation gates AND mission-level ship gates)
- burn-gate rejection: mission stays CANCELLED, synthesis may not resurrect
- two daemons on one database: the loser is refused (leader lock)
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from conftest import role_catalog, wait_for_gate_park
from rework.config import Config
from rework.kernel.events.bus import InProcessEventBus
from rework.kernel.gates import GateManager
from rework.kernel.state.records import (
    Charge,
    Mission,
    MissionStatus,
    RunStatus,
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

        async def work(state: dict) -> dict:  # type: ignore[no-untyped-def]
            return {"worked": True}

        async def grind(state: dict) -> dict:  # type: ignore[no-untyped-def]
            await asyncio.sleep(0.6)  # the "tool call" the kill lands inside
            return {"ground": True}

        async def gate(state: dict) -> dict:  # type: ignore[no-untyped-def]
            # the ONE pause primitive (ADR 0023): a run-scoped owner gate
            from rework.nodes.escalate import ask_owner_gate

            decision = await ask_owner_gate(ctx, "finish it?")
            return {
                "artifact": {
                    "approved": decision is True
                    or (isinstance(decision, dict) and bool(decision.get("approved"))),
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


class SlowSpec(RoleSpec):
    """Slow enough to be paused or cancelled mid-flight."""

    name = "slow"
    description = "test role: sleeps, then completes"
    goal = "sleep"
    default_model = "flash"

    def build_graph(self, ctx: RoleBuildCtx):  # type: ignore[no-untyped-def]
        from langgraph.graph import END, START, StateGraph

        async def work(state: dict) -> dict:  # type: ignore[no-untyped-def]
            await asyncio.sleep(0.5)
            return {"artifact": {"kind": "slow"}}

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
    reg.register(SlowSpec)
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
    # Wait for the `grind` NODE to start, not a fixed 0.2 s: by then `work` has
    # checkpointed and grind's 0.6 s sleep is in flight, which is exactly the
    # "mid-grind" the kill must land inside. A fixed sleep lost this race under
    # the full suite's load (work had not checkpointed, so resume had nothing).
    await _wait_for(
        lambda: _node_entered(store, mission.id, "grind"), what="the grind node to start"
    )
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
    decision from the row: approved resumes the role, no second gate opens.

    Flaked on CI (3.12 and 3.13) before the shared barrier existed. Reproduce
    under CPU load:

        for i in 1 2 3 4 5 6; do
            pytest tests/integration/test_chaos.py -q &
        done; wait

    Note the failure mode is NOT a mission-status assert (this test never makes
    one): the gate row and the mission's park are separate statements. The old
    gate-row-only wait could cancel the runner before the park landed, and the
    restart opened a SECOND gate, failing `len(opened) == 1` (the run-scoped
    gate is now found again by `for_run`, so a restart reuses it).
    The barrier now waits for the gate row AND
    the run's park (conftest.wait_for_gate_park)."""
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
    # BARRIER: the gate row is committed before the mission's park
    # (ask_owner_gate opens the run-scoped gate, then parks the mission).
    # A kill in that window restarts an UNPARKED run, which re-fires the
    # interrupt and opens a SECOND gate; the "one gate_opened" assert below
    # then fails. Wait for the park, not for the gate row.
    await wait_for_gate_park(
        store,
        mission.id,
        mission_status=MissionStatus.AWAITING_GATE,
        run_id=f"{mission.id}:do:crashy:1",
        what="run parked at its gate",
    )
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
    applied the owner's answer and the run sat parked until the test
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
    # BARRIER (same shape as the trio above): the gate row is committed before
    # the mission's park, so a kill on the gate row alone restarts
    # an UNPARKED role. What this test relies on next is the open gate AND the
    # gated run parked, so wait for both facts in one bounded poll.
    await wait_for_gate_park(
        store,
        mission.id,
        run_id=f"{mission.id}:do:crashy:1",
        what="the gate open and its run parked",
    )
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


async def test_gate_rejected_while_down_reaches_the_role_on_restart(world: dict[str, Any]) -> None:
    """Same, but the answer is no: on restart the role RECEIVES the rejection
    from the row (one pause primitive, ADR 0023: a "no" is an answer, not an
    abort), records approved=False and the mission still completes."""
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
    # BARRIER: same two-write window as test_gate_decided_while_down_applied_
    # on_restart, but here an early cancel shows up as the run never reaching
    # `aborted` (the restart re-runs an unparked role). Wait for the park.
    await wait_for_gate_park(
        store,
        mission.id,
        mission_status=MissionStatus.AWAITING_GATE,
        run_id=f"{mission.id}:do:crashy:1",
        what="run parked at its gate",
    )
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
    assert fresh is not None and fresh.status.value == "completed"
    assert (fresh.artifact or {}).get("approved") is False
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
    # BARRIER: the ship gate row commits before _final_gates writes
    # AWAITING_GATE. Cancelling between the two lets the restart re-run the
    # plan's final gates and open a SECOND ship gate, which the
    # "exactly one gate_opened" assert forbids. Wait for both facts.
    await wait_for_gate_park(
        store, mission.id, mission_status=MissionStatus.AWAITING_GATE, what="ship gate park"
    )
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
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rejecting the burn cap must CANCEL the mission — and synthesis (which
    runs right after, with no failed runs) must not resurrect it as DONE."""
    # rejecting a gate only happens when a gate opens: pin auto-raises off, or
    # the first raise is taken by the budget policy without asking
    monkeypatch.setenv("REWORK_BUDGET_AUTO_RAISES", "0")
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


async def _node_entered(store: SqliteStore, mission_id: str, node: str) -> bool:
    """Has `node` started in this mission? `_NodeEvents` publishes a PROGRESS
    event with `{node, phase: enter}` per node start, so this is a real signal
    rather than a guess at a sleep duration."""
    for event in await store.events_since(0, mission_id=mission_id):
        payload = event.payload or {}
        if payload.get("node") == node and payload.get("phase") == "enter":
            return True
    return False


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

    from rework.nodes.escalate import ask_owner_gate
    from rework.roles.base import RoleSpec

    seen: list[Any] = []

    class EchoDecisionSpec(RoleSpec):
        name = "echo_decision"
        description = "records whatever its owner gate returned"
        goal = "record the decision"
        default_model = "flash"

        def build_graph(self, ctx):  # type: ignore[no-untyped-def]
            async def ask(state: dict) -> dict:  # type: ignore[no-untyped-def]
                value = await ask_owner_gate(ctx, "how should I change it?")
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


async def test_owner_cancel_stops_an_in_flight_mission(world: dict[str, Any]) -> None:
    """Cancel written straight to the store (what `rework cancel` and the UI do)
    must stop a running mission and leave it CANCELLED."""
    store: SqliteStore = world["store"]
    mission = await store.create_mission(
        Mission(
            id=new_id("m"),
            objective="cancel me",
            plan={"steps": [{"step": "do", "role": "slow"}]},
        )
    )
    task = asyncio.create_task(_runner(world, mission.id).run())
    await _wait_for(lambda: _run_status_is(store, mission.id, "running"), what="run started")

    await store.update_mission(mission.id, status=MissionStatus.CANCELLED)
    await asyncio.wait_for(task, timeout=60)  # returns, does not hang

    fresh = await store.get_mission(mission.id)
    assert fresh is not None and fresh.status == MissionStatus.CANCELLED


async def test_owner_pause_holds_and_resume_finishes(world: dict[str, Any]) -> None:
    """Pause stops the roles without losing the mission; flipping the status back
    to running lets it finish. This is the pause/resume loop a UI needs."""
    store: SqliteStore = world["store"]
    mission = await store.create_mission(
        Mission(
            id=new_id("m"),
            objective="pause then resume",
            plan={"steps": [{"step": "do", "role": "slow"}]},
        )
    )
    task = asyncio.create_task(_runner(world, mission.id).run())
    await _wait_for(lambda: _run_status_is(store, mission.id, "running"), what="run started")

    await store.update_mission(mission.id, status=MissionStatus.ON_HOLD)
    # while held, nothing may advance to completion
    await asyncio.sleep(0.8)
    held = await store.get_mission(mission.id)
    assert held is not None and held.status == MissionStatus.ON_HOLD
    run = await store.get_run(f"{mission.id}:do:slow:1")
    assert run is not None and run.status != RunStatus.COMPLETED

    await store.update_mission(mission.id, status=MissionStatus.RUNNING)
    await asyncio.wait_for(task, timeout=60)
    fresh = await store.get_mission(mission.id)
    assert fresh is not None and fresh.status == MissionStatus.DONE


async def test_crashed_runner_marks_the_mission_failed(world: dict[str, Any]) -> None:
    """A runner that dies with an unhandled exception must not leave its
    mission on 'running' forever (found live: a client created a plan an older
    daemon could not run; every UI showed it running)."""
    from rework.kernel.state.records import Mission, MissionStatus

    store = world["store"]
    await store.create_mission(
        Mission(id="m-broken", objective="x", status=MissionStatus.PENDING, plan={"steps": []})
    )
    daemon = Daemon(
        store=store,
        bus=world["bus"],
        registry=world["registry"],
        catalog=world["catalog"],
        make_checkpointer=sqlite_checkpointer_factory(world["ck_path"]),
        workroot=world["workroot"],
        config=world["config"],
    )
    task = asyncio.create_task(daemon.run_forever())
    try:

        async def failed() -> bool:
            m = await store.get_mission("m-broken")
            return m is not None and m.status == MissionStatus.FAILED

        for _ in range(100):
            if await failed():
                break
            await asyncio.sleep(0.1)
        mission = await store.get_mission("m-broken")
        assert mission is not None and mission.status == MissionStatus.FAILED
        # ADR 0012: an empty plan has no driver and is refused at the driver
        # guard, before the plan-steps check. The failure is what matters.
        error = str((mission.result or {}).get("error"))
        assert "empty plan" in error or "has no driver" in error, error
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_a_queued_pending_mission_is_logged_once(
    world: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    """Found live: with concurrency 1 a second mission waits, and the daemon
    logged 'picking up pending mission' on every poll (275 lines in 4 min)."""
    import logging

    from rework.kernel.state.records import Mission

    store: SqliteStore = world["store"]
    await store.create_mission(Mission(id="mq", objective="queued", repo_path=str(world["tmp"])))
    d = Daemon(
        store=store,
        bus=world["bus"],
        registry=world["registry"],
        catalog=world["catalog"],
        make_checkpointer=sqlite_checkpointer_factory(world["ck_path"]),
        workroot=world["workroot"],
        config=world["config"],
    )
    spawned: list[str] = []

    async def fake_spawn(mid: str) -> None:
        spawned.append(mid)
        d._runners[mid] = asyncio.create_task(asyncio.sleep(60))  # queued on the semaphore

    d._spawn_runner = fake_spawn  # type: ignore[method-assign]
    with caplog.at_level(logging.INFO, logger="rework.daemon"):
        for _ in range(5):
            await d._pickup_pending()
    try:
        assert spawned == ["mq"]
        assert sum("picking up pending mission mq" in r.message for r in caplog.records) == 1
    finally:
        d._runners["mq"].cancel()


def _pickup_daemon(world: dict[str, Any], spawned: list[str]) -> Daemon:
    """A Daemon that records what pickup chose; _spawn_runner is the only fake."""
    d = Daemon(
        store=world["store"],
        bus=world["bus"],
        registry=world["registry"],
        catalog=world["catalog"],
        make_checkpointer=sqlite_checkpointer_factory(world["ck_path"]),
        workroot=world["workroot"],
        config=world["config"],
    )

    async def fake_spawn(mid: str) -> None:
        spawned.append(mid)
        d._runners[mid] = asyncio.create_task(asyncio.sleep(60))  # queued on the semaphore

    d._spawn_runner = fake_spawn  # type: ignore[method-assign]
    return d


def _cancel_runners(d: Daemon) -> None:
    for task in d._runners.values():
        task.cancel()


async def test_pickup_takes_the_oldest_pending_mission_first(world: dict[str, Any]) -> None:
    """Newest-first listing let fresh missions jump the queue: pickup is
    oldest-first so an old pending mission runs before newer ones."""
    from rework.kernel.state.records import Mission

    store: SqliteStore = world["store"]
    await store.create_mission(Mission(id="m-old", objective="old", created_at=1_000.0))
    for i in range(3):
        await store.create_mission(
            Mission(id=f"m-new-{i}", objective="new", created_at=5_000.0 + i)
        )

    spawned: list[str] = []
    d = _pickup_daemon(world, spawned)
    try:
        await d._pickup_pending()
        assert spawned[0] == "m-old"
        # every pending mission eventually runs, oldest first
        assert spawned == ["m-old", "m-new-0", "m-new-1", "m-new-2"]
    finally:
        _cancel_runners(d)


async def test_pickup_oldest_still_picked_with_more_than_ten_pending(
    world: dict[str, Any],
) -> None:
    """Starvation regression: with 13 pending missions the newest-first window
    of 10 never contained the oldest, so it never ran."""
    from rework.kernel.state.records import Mission

    store: SqliteStore = world["store"]
    await store.create_mission(Mission(id="m-old", objective="starved", created_at=1_000.0))
    for i in range(12):
        await store.create_mission(
            Mission(id=f"m-newer-{i:02d}", objective="newer", created_at=5_000.0 + i)
        )
    assert len(await store.list_missions(status=MissionStatus.PENDING, limit=100)) == 13

    spawned: list[str] = []
    d = _pickup_daemon(world, spawned)
    try:
        await d._pickup_pending()
        # the oldest is inside the window and is chosen first
        assert spawned[0] == "m-old"
    finally:
        _cancel_runners(d)
