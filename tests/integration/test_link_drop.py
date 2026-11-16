"""The database link drops mid-mission: what resumes, what is retried, what the
owner sees. Built on the test_chaos harness (real langgraph graphs, no LLM), with a
FaultStore under a ResilientStore, the same stack the daemon runs.

- a store error mid run or at a step transition: one clean run, no doubled rows
- an outage during an owner gate wait: the decision is kept and applied once
- a long outage during the ship gate: the mission is not lost, no duplicate gate
- a keyed write repeated after it landed is a no-op; an unkeyed one is not retried
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import psycopg
import pytest

from conftest import role_catalog
from rework.kernel.events.bus import InProcessEventBus
from rework.kernel.gates import GateManager
from rework.kernel.state.records import (
    Charge,
    EventRecord,
    EventType,
    Mission,
    MissionStatus,
    Run,
    RunStatus,
    new_id,
)
from rework.kernel.state.records import (
    run_id as make_run_id,
)
from rework.kernel.state.resilient import ResilientStore, RetryPolicy
from rework.kernel.state.sqlite import SqliteStore, open_sqlite
from rework.orchestrator import engine as engine_module
from rework.orchestrator.daemon import sqlite_checkpointer_factory
from rework.orchestrator.engine import MissionRunner, RunSupervisor
from rework.roles.base import RoleBuildCtx, RoleSpec
from rework.roles.registry import RoleRegistry
from support.fault_store import FaultStore

# retries in milliseconds, so an outage of dozens of calls costs well under a second
FAST = RetryPolicy(max_elapsed_s=20.0, base_delay_s=0.01, max_delay_s=0.05, jitter=0.0)


class LedgerSpec(RoleSpec):
    """A coder-shaped run in miniature: one model call (charged under a deterministic
    key), a tool-call-sized pause, then done."""

    name = "ledger"
    description = "test role: charges once, then finishes"
    goal = "charge then finish"
    default_model = "flash"

    def build_graph(self, ctx: RoleBuildCtx):  # type: ignore[no-untyped-def]
        from langgraph.graph import END, START, StateGraph

        async def model_call(state: dict) -> dict:  # type: ignore[type-arg]
            await ctx.store.charge(
                Charge(
                    mission_id=ctx.mission.id,
                    run_id=ctx.run.id,
                    model="flash",
                    input_tokens=100,
                    output_tokens=20,
                    cost_usd=0.01,
                    dedupe_key=f"{ctx.run.id}:turn1:model",
                )
            )
            return {"charged": True}

        async def grind(state: dict) -> dict:  # type: ignore[type-arg]
            await asyncio.sleep(0.2)
            return {"ground": True}

        graph = StateGraph(dict)  # type: ignore[arg-type]
        graph.add_node("model_call", model_call)
        graph.add_node("grind", grind)
        graph.add_edge(START, "model_call")
        graph.add_edge("model_call", "grind")
        graph.add_edge("grind", END)
        return graph.compile()


class LedgerGateSpec(LedgerSpec):
    """LedgerSpec, then a pause for the owner (an escalation gate)."""

    name = "ledger_gate"
    description = "test role: charges, then asks the owner"

    def build_graph(self, ctx: RoleBuildCtx):  # type: ignore[no-untyped-def]
        from langgraph.graph import END, START, StateGraph
        from langgraph.types import interrupt

        async def model_call(state: dict) -> dict:  # type: ignore[type-arg]
            await ctx.store.charge(
                Charge(
                    mission_id=ctx.mission.id,
                    run_id=ctx.run.id,
                    model="flash",
                    input_tokens=100,
                    output_tokens=20,
                    cost_usd=0.01,
                    dedupe_key=f"{ctx.run.id}:turn1:model",
                )
            )
            return {"charged": True}

        async def gate(state: dict) -> dict:  # type: ignore[type-arg]
            decision = interrupt({"kind": "question", "detail": "finish it?"})
            return {"artifact": {"approved": bool(decision)}}

        graph = StateGraph(dict)  # type: ignore[arg-type]
        graph.add_node("model_call", model_call)
        graph.add_node("gate", gate)
        graph.add_edge(START, "model_call")
        graph.add_edge("model_call", "gate")
        graph.add_edge("gate", END)
        return graph.compile()


@pytest.fixture
def registry() -> RoleRegistry:
    reg = RoleRegistry()
    reg.register(LedgerSpec)
    reg.register(LedgerGateSpec)
    return reg


@pytest.fixture
async def world(tmp_path: Path, registry: RoleRegistry) -> AsyncIterator[dict[str, Any]]:
    real: SqliteStore = await open_sqlite(str(tmp_path / "linkdrop.sqlite3"))
    try:
        yield {
            "real": real,
            "db_path": str(tmp_path / "linkdrop.sqlite3"),
            "ck_path": str(tmp_path / "checkpoints.sqlite3"),
            "registry": registry,
            "catalog": role_catalog(),
            "workroot": tmp_path / "work",
        }
    finally:
        await real.aclose()


def _stack(world: dict[str, Any]) -> tuple[FaultStore, ResilientStore]:
    """The daemon's stack in miniature: resilient proxy over the (faulty) store."""
    fault = FaultStore(world["real"])
    return fault, ResilientStore(fault, policy=FAST)


def _runner(world: dict[str, Any], store: Any, mission_id: str) -> MissionRunner:
    return MissionRunner(
        mission_id,
        store,
        InProcessEventBus(store),
        world["registry"],
        world["catalog"],
        sqlite_checkpointer_factory(world["ck_path"]),
        world["workroot"],
        poll_interval_s=0.05,
    )


async def _mission(
    world: dict[str, Any], steps: list[dict[str, str]], gates: list[str] | None = None
) -> str:
    plan: dict[str, Any] = {"steps": steps}
    if gates:
        plan["gates"] = gates
    mission = await world["real"].create_mission(
        Mission(id=new_id("m"), objective="link drop", plan=plan)
    )
    return mission.id


async def _ledger(real: SqliteStore, mission_id: str) -> dict[str, Any]:
    """The rows a clean run must produce: runs, charges, and events by type."""
    runs = await real.list_runs(mission_id)
    charges = 0
    for run in runs:
        charges += len(await real.run_charges(run.id))
    events = await real.events_since(0, mission_id=mission_id)
    return {
        "runs": sorted((r.step, r.role, r.status.value) for r in runs),
        "charges": charges,
        "events": Counter(e.type.value for e in events),
    }


async def _wait_for(  # type: ignore[no-untyped-def]
    predicate, timeout_s: float = 10.0, what: str = "condition"
) -> None:
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.05)
    raise TimeoutError(f"timed out waiting for {what}")


async def _open_gate(real: SqliteStore, mission_id: str) -> bool:
    return bool(await real.list_open_gates(mission_id))


async def _mission_status(real: SqliteStore, mission_id: str) -> MissionStatus | None:
    row = await real.get_mission(mission_id)
    return row.status if row else None


async def _clean_baseline(world: dict[str, Any], steps: list[dict[str, str]]) -> dict[str, Any]:
    mission_id = await _mission(world, steps)
    _, store = _stack(world)
    await asyncio.wait_for(_runner(world, store, mission_id).run(), timeout=30)
    assert await _mission_status(world["real"], mission_id) == MissionStatus.DONE
    return await _ledger(world["real"], mission_id)


# -- (a) and (b): a store error mid run, and at a step transition ---------------------

ONE_STEP = [{"step": "do", "role": "ledger"}]
TWO_STEPS = [{"step": "do", "role": "ledger"}, {"step": "then", "role": "ledger"}]


@pytest.mark.parametrize(
    ("steps", "method", "skip", "when"),
    [
        # mid run: the model charge lands, the ack is lost; the retry must not double it
        (ONE_STEP, "charge", 0, "after"),
        # mid run: a run-status write fails before it lands
        (ONE_STEP, "update_run", 1, "before"),
        # mid run: the run-status write lands, the ack is lost
        (ONE_STEP, "update_run", 0, "after"),
        # at the step transition: the second run's row is written, the ack is lost
        (TWO_STEPS, "create_run", 1, "after"),
        # at the step transition: the charge of the second step fails before it lands
        (TWO_STEPS, "charge", 1, "before"),
    ],
)
async def test_store_error_leaves_one_clean_run_and_no_doubles(
    world: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
    steps: list[dict[str, str]],
    method: str,
    skip: int,
    when: str,
) -> None:
    baseline = await _clean_baseline(world, steps)

    caplog.set_level(logging.INFO, logger="rework.store")
    fault, store = _stack(world)
    fault.fail_method(method, times=1, skip=skip, when=when)
    mission_id = await _mission(world, steps)
    await asyncio.wait_for(_runner(world, store, mission_id).run(), timeout=30)

    assert fault.injected, "the fault never fired: the test proves nothing"
    assert await _mission_status(world["real"], mission_id) == MissionStatus.DONE
    faulted = await _ledger(world["real"], mission_id)
    assert faulted == baseline, "a store error changed the rows: doubles or losses"

    # one warning when the outage starts, one info when it clears, no per-retry noise
    outage = [r for r in caplog.records if r.name == "rework.store"]
    assert sum(r.levelno == logging.WARNING for r in outage) == 1
    assert sum(r.levelno == logging.INFO for r in outage) == 1


# -- (c): an outage during an owner gate wait ------------------------------------------


async def test_gate_decision_during_outage_is_kept_and_applied_once(
    world: dict[str, Any],
) -> None:
    real: SqliteStore = world["real"]
    mission_id = await _mission(world, [{"step": "do", "role": "ledger_gate"}])
    fault, store = _stack(world)
    task = asyncio.create_task(_runner(world, store, mission_id).run())
    await _wait_for(lambda: _open_gate(real, mission_id), what="gate to open")

    # the store goes down while the owner answers; the owner's own store is unaffected
    fault.set_down(True)
    second: SqliteStore = await open_sqlite(world["db_path"])
    try:
        gates = GateManager(second, InProcessEventBus(second))
        open_gates = await gates.open_gates(mission_id)
        await gates.decide(open_gates[0].id, approved=True, note="from my phone")
    finally:
        await second.aclose()
    await asyncio.sleep(0.3)  # the engine's gate poll is failing through this window
    fault.set_down(False)

    await asyncio.wait_for(task, timeout=30)
    assert fault.injected, "the outage never reached the engine"
    assert await _mission_status(real, mission_id) == MissionStatus.DONE
    rows = await _ledger(real, mission_id)
    assert rows["charges"] == 1
    assert rows["events"]["gate_opened"] == 1, "the gate was re-opened after the outage"
    run = (await real.list_runs(mission_id))[0]
    assert (run.artifact or {}).get("approved") is True, "the owner's decision was lost"


# -- (e): a long outage during the ship gate ------------------------------------------


async def test_long_outage_at_ship_gate_loses_no_mission_and_no_gate(
    world: dict[str, Any],
) -> None:
    real: SqliteStore = world["real"]
    mission_id = await _mission(world, ONE_STEP, gates=["ship"])
    fault, store = _stack(world)
    task = asyncio.create_task(_runner(world, store, mission_id).run())
    await _wait_for(lambda: _open_gate(real, mission_id), what="ship gate")

    # far longer than one backoff step: dozens of failed polls and reconnects
    fault.set_down(True)
    await asyncio.sleep(1.0)
    polls_while_down = fault.calls
    fault.set_down(False)
    second: SqliteStore = await open_sqlite(world["db_path"])
    try:
        gates = GateManager(second, InProcessEventBus(second))
        open_gates = await gates.open_gates(mission_id)
        assert open_gates[0].kind.value == "ship"
        await gates.decide(open_gates[0].id, approved=True)
    finally:
        await second.aclose()

    await asyncio.wait_for(task, timeout=30)
    assert polls_while_down > 20, "the outage was too short to prove anything"
    assert await _mission_status(real, mission_id) == MissionStatus.DONE
    rows = await _ledger(real, mission_id)
    assert rows["events"]["gate_opened"] == 1, "a duplicate gate opened after the outage"
    assert rows["charges"] == 1


# -- the store seam itself ---------------------------------------------------------------


async def test_keyed_writes_are_noops_on_repeat(world: dict[str, Any]) -> None:
    real: SqliteStore = world["real"]
    mission_id = await _mission(world, ONE_STEP)
    charge = Charge(mission_id=mission_id, run_id="r1", model="m", cost_usd=0.5, dedupe_key="r1:t1")
    await real.charge(charge)
    await real.charge(charge)
    assert len(await real.run_charges("r1")) == 1

    first = await real.append_event(
        EventRecord(mission_id=mission_id, type=EventType.INFO, dedupe_key="k1")
    )
    again = await real.append_event(
        EventRecord(mission_id=mission_id, type=EventType.INFO, dedupe_key="k1")
    )
    assert again.seq == first.seq
    assert len(await real.events_since(0, mission_id=mission_id)) == 1


async def test_unkeyed_append_is_attempted_once_and_the_error_surfaces(
    world: dict[str, Any],
) -> None:
    fault, store = _stack(world)
    fault.fail_method("append_event", times=1, when="after")
    with pytest.raises(psycopg.OperationalError):
        await store.append_event(EventRecord(type=EventType.INFO))
    assert fault.calls == 1, "an un-keyed event must not be repeated blindly"


async def test_non_repeatable_write_surfaces_and_the_link_is_still_usable(
    world: dict[str, Any],
) -> None:
    fault, store = _stack(world)
    fault.fail_method("create_mission", times=1)
    with pytest.raises(psycopg.OperationalError):
        await store.create_mission(Mission(id=new_id("m"), objective="x", plan={"steps": []}))
    assert fault.calls == 1
    # the next call is served normally: the failure did not poison the proxy
    assert await store.list_missions() == []


# -- (f) an outage that outlasts the retry budget: the run is re-driven, not lost --------

SHORT_BUDGET = RetryPolicy(max_elapsed_s=0.05, base_delay_s=0.01, max_delay_s=0.02, jitter=0.0)


@pytest.fixture
def fast_redrive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine_module, "REDRIVE_START_S", 0.01)
    monkeypatch.setattr(engine_module, "REDRIVE_MAX_S", 0.05)


async def test_outage_outlasting_the_budget_is_redriven_to_one_clean_run(
    world: dict[str, Any], fast_redrive: None
) -> None:
    """The store is down for much longer than the store's own retries. The role and
    the mission give up their pass, re-drive from the store with backoff, and once the
    link is back the mission is DONE with exactly the rows of one clean run."""
    baseline = await _clean_baseline(world, ONE_STEP)
    mission_id = await _mission(world, ONE_STEP)
    fault = FaultStore(world["real"])
    store = ResilientStore(fault, policy=SHORT_BUDGET)
    fault.set_down(True)
    task = asyncio.create_task(_runner(world, store, mission_id).run())

    async def outage_has_lasted() -> bool:
        return fault.calls > 200  # many retry budgets, many re-drive passes

    await _wait_for(outage_has_lasted, timeout_s=20, what="the outage to outlast the budget")
    assert not task.done()  # nothing gave up while the link was down
    fault.set_down(False)
    await asyncio.wait_for(task, timeout=30)

    assert await _mission_status(world["real"], mission_id) == MissionStatus.DONE
    assert await _ledger(world["real"], mission_id) == baseline


async def test_owner_cancel_stops_a_run_that_is_being_redriven(
    world: dict[str, Any], fast_redrive: None
) -> None:
    """A cancelled task must end, even while it waits to re-drive through an outage."""
    mission_id = await _mission(world, ONE_STEP)
    fault = FaultStore(world["real"])
    store = ResilientStore(fault, policy=SHORT_BUDGET)
    fault.set_down(True)
    task = asyncio.create_task(_runner(world, store, mission_id).run())

    async def redriving() -> bool:
        return fault.calls > 50

    await _wait_for(redriving, timeout_s=20, what="the re-drive loop to start")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=10)
    fault.set_down(False)


# -- (g) the ROLE path alone re-drives its run through an outage -----------------------


def _role_run(mission_id: str) -> Run:
    """The row _dispatch_step writes for the one-step plan (no mission pass involved)."""
    return Run(
        id=make_run_id(mission_id, "do", "ledger", 1),
        mission_id=mission_id,
        role="ledger",
        step="do",
        attempt=1,
    )


async def _role_alone(
    world: dict[str, Any], store: Any, mission_id: str
) -> tuple[Run, RunSupervisor]:
    """One role run and the supervisor that drives it, built directly: no MissionRunner,
    so no mission-level pass can re-drive the run. Only the role's own loop can."""
    mission = await world["real"].get_mission(mission_id)
    assert mission is not None
    run = await world["real"].create_run(_role_run(mission_id))
    supervisor = RunSupervisor(
        mission,
        store,
        InProcessEventBus(store),
        world["registry"],
        world["catalog"],
        sqlite_checkpointer_factory(world["ck_path"]),
        world["workroot"],
    )
    return run, supervisor


async def test_the_role_path_redrives_its_own_run_through_an_outage(
    world: dict[str, Any], fast_redrive: None
) -> None:
    """_execute alone, with a store that is down for far longer than the store's own
    retries and then recovers. The run must end COMPLETED with exactly one clean run's
    rows. Making _drive give up instead of re-driving leaves the run RUNNING, and this
    test goes red on the `not task.done()` check."""
    clean_id = await _mission(world, ONE_STEP)
    clean_run, clean_sup = await _role_alone(world, world["real"], clean_id)
    await asyncio.wait_for(clean_sup._execute(clean_run, False), timeout=30)
    baseline = await _ledger(world["real"], clean_id)
    assert baseline["runs"] == [("do", "ledger", RunStatus.COMPLETED.value)]

    mission_id = await _mission(world, ONE_STEP)
    fault = FaultStore(world["real"])
    store = ResilientStore(fault, policy=SHORT_BUDGET)
    run, supervisor = await _role_alone(world, store, mission_id)
    fault.set_down(True)
    task = asyncio.create_task(supervisor._execute(run, False))

    async def role_is_redriving() -> bool:
        # the store gave up many times while the role kept re-driving; or the task already ended
        return task.done() or fault.calls > 200

    await _wait_for(role_is_redriving, timeout_s=20, what="the role to re-drive")
    assert not task.done(), "the role gave up instead of re-driving through the outage"
    fault.set_down(False)
    await asyncio.wait_for(task, timeout=30)

    row = await world["real"].get_run(run.id)
    assert row is not None and row.status == RunStatus.COMPLETED
    assert await _ledger(world["real"], mission_id) == baseline, "one clean run's rows only"
