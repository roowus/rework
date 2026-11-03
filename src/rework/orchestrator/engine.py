"""The engine: MissionRunner + RunSupervisor + crash recovery.

Design (docs/design.md#orchestrator, ADR 0002):
- Roles are NOT subgraphs: each run is an independent asyncio task with its own
  checkpointer thread; dispatch writes INTENT rows, RunSupervisor turns them into
  tasks. Deterministic run ids make create_run idempotent.
- The store is the truth. Wake conditions re-derive from store rows every loop;
  in-process tasks are just the execution mechanism (CLAUDE.md rule 3).
- One pause primitive: a role node calls interrupt() first; the engine persists
  the payload onto the run, materializes a gate row from it, and later resumes
  with Command(resume=decision). Decisions made while the daemon was down are
  honored because resume always reads the gate row.

M1 scope: fixed single-step plans (mission.plan or the echo default). The LLM
plan/dispatch/check-in nodes land with the M2 vertical slice.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from langgraph.types import Command

from rework.kernel.events.bus import EventBus
from rework.kernel.gates import GateManager
from rework.kernel.llm.adapters import build_provider
from rework.kernel.llm.catalog import ModelCatalog
from rework.kernel.state.records import (
    EventRecord,
    EventType,
    GateKind,
    Mission,
    MissionStatus,
    Run,
    RunStatus,
    new_id,
)
from rework.kernel.state.records import (
    run_id as make_run_id,
)
from rework.kernel.state.store import StateStore
from rework.roles.base import RoleBuildCtx
from rework.roles.registry import RoleRegistry

DEFAULT_PLAN: dict[str, Any] = {"steps": [{"step": "echo", "role": "echo"}]}

# a callable returning (checkpointer, close_fn) for one run's checkpoint thread
CheckpointerFactory = Callable[[str], Awaitable[tuple[Any, Callable[[], Awaitable[None]]]]]


def _plan_steps(mission: Mission) -> list[dict[str, Any]]:
    plan = mission.plan or DEFAULT_PLAN
    steps = plan.get("steps") or []
    if not steps:
        raise ValueError(f"mission {mission.id} has an empty plan")
    return steps


def _terminal(run: Run) -> bool:
    return run.status in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.ABORTED}


class RunSupervisor:
    """Owns the live asyncio tasks for one mission's runs."""

    def __init__(
        self,
        mission: Mission,
        store: StateStore,
        bus: EventBus,
        registry: RoleRegistry,
        catalog: ModelCatalog,
        make_checkpointer: CheckpointerFactory,
        workroot: Path,
    ) -> None:
        self._mission = mission
        self._store = store
        self._bus = bus
        self._registry = registry
        self._catalog = catalog
        self._make_checkpointer = make_checkpointer
        self._workroot = workroot
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self._closers: list[Callable[[], Awaitable[None]]] = []
        self._ctxs: dict[str, RoleBuildCtx] = {}

    async def _build_graph(self, run: Run) -> Any:
        spec = self._registry.get(run.role)
        entry = self._catalog.resolve(str(run.model_ref.get("id") or spec.default_model))
        ctx = RoleBuildCtx(
            run=run,
            mission=self._mission,
            catalog=self._catalog,
            provider=build_provider(entry),
            store=self._store,
            bus=self._bus,
            gates=GateManager(self._store, self._bus),
            budget=spec.budget,
            workdir=self._workroot / run.id.replace(":", "_"),
        )
        graph = spec.build_graph(ctx)
        checkpointer, close = await self._make_checkpointer(run.id)
        graph.checkpointer = checkpointer
        self._closers.append(close)
        self._ctxs[run.id] = ctx
        return graph

    async def _execute(self, run: Run, resume_from_checkpoint: bool) -> None:
        """Run one role graph to its next stop: completion or interrupt."""
        try:
            graph = await self._build_graph(run)
            await self._store.update_run(run.id, status=RunStatus.RUNNING)
            await self._bus.publish(
                EventRecord(
                    mission_id=self._mission.id,
                    run_id=run.id,
                    type=EventType.ROLE_SPAWNED,
                    payload={"role": run.role},
                )
            )
            config = {"configurable": {"thread_id": run.id}}
            if resume_from_checkpoint:
                # input=None resumes from the last checkpoint; but a daemon that
                # died between the RUNNING flip and the first checkpoint write
                # left no state, so detect and fall back to a fresh start.
                saved = await graph.checkpointer.aget_tuple(config)
                if saved is not None:
                    result = await graph.ainvoke(None, config=config)
                else:
                    spec = self._registry.get(run.role)
                    result = await graph.ainvoke(
                        spec.initial_state(self._ctxs[run.id]), config=config
                    )
            else:
                spec = self._registry.get(run.role)
                result = await graph.ainvoke(spec.initial_state(self._ctxs[run.id]), config=config)
            await self._consume_result(run, result)
        except Exception as exc:
            await self._store.update_run(run.id, status=RunStatus.FAILED)
            await self._bus.publish(
                EventRecord(
                    mission_id=self._mission.id,
                    run_id=run.id,
                    type=EventType.INFO,
                    payload={"error": f"{type(exc).__name__}: {exc}"},
                )
            )

    async def _consume_result(self, run: Run, result: Any) -> None:
        if isinstance(result, dict) and "__interrupt__" in result:
            payload = result["__interrupt__"][0].value
            await self._store.update_run(
                run.id, status=RunStatus.HALTED_ESCALATED, escalation=payload
            )
            await self._bus.publish(
                EventRecord(
                    mission_id=self._mission.id,
                    run_id=run.id,
                    type=EventType.ROLE_HALTED,
                    payload=payload,
                )
            )
        else:
            artifact = result.get("artifact") if isinstance(result, dict) else None
            await self._store.update_run(
                run.id, status=RunStatus.COMPLETED, artifact=artifact or {}
            )
            await self._bus.publish(
                EventRecord(
                    mission_id=self._mission.id,
                    run_id=run.id,
                    type=EventType.ROLE_COMPLETED,
                    payload={"role": run.role},
                )
            )

    async def ensure_started(self, run: Run) -> None:
        """Start the run's task unless it is already live or finished."""
        if run.status in {
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.ABORTED,
            RunStatus.HALTED_ESCALATED,
        }:
            return  # terminal or parked at a gate; the pump loop handles those
        if run.id in self.tasks and not self.tasks[run.id].done():
            return
        resume = run.status == RunStatus.RUNNING  # crashed mid-flight
        self.tasks[run.id] = asyncio.create_task(self._execute(run, resume))

    async def resume_with_decision(self, run: Run, decision: Any) -> None:
        """Resume a halted run with a gate/escalation decision. Idempotent: the
        pump loop can re-enter _handle_halted while a resume task is still
        starting (its run row stays HALTED_ESCALATED until it flips RUNNING),
        so a live task for this run means a resume is already in flight."""
        existing = self.tasks.get(run.id)
        if existing is not None and not existing.done():
            return
        self.tasks[run.id] = asyncio.create_task(self._execute_resume(run, decision))

    async def _execute_resume(self, run: Run, decision: Any) -> None:
        try:
            graph = await self._build_graph(run)
            await self._store.update_run(run.id, status=RunStatus.RUNNING)
            config = {"configurable": {"thread_id": run.id}}
            result = await graph.ainvoke(Command(resume=decision), config=config)
            await self._consume_result(run, result)
        except Exception as exc:
            await self._store.update_run(run.id, status=RunStatus.FAILED)
            await self._bus.publish(
                EventRecord(
                    mission_id=self._mission.id,
                    run_id=run.id,
                    type=EventType.INFO,
                    payload={"error": f"resume failed: {type(exc).__name__}: {exc}"},
                )
            )

    async def wait_all(self) -> None:
        if self.tasks:
            await asyncio.gather(*self.tasks.values())

    async def aclose(self) -> None:
        for task in self.tasks.values():
            if not task.done():
                task.cancel()
        for close in self._closers:
            try:
                await close()
            except Exception:
                pass


class MissionRunner:
    """Drives one mission through its plan steps, store-derived at every loop."""

    def __init__(
        self,
        mission_id: str,
        store: StateStore,
        bus: EventBus,
        registry: RoleRegistry,
        catalog: ModelCatalog,
        make_checkpointer: CheckpointerFactory,
        workroot: Path,
        poll_interval_s: float = 0.25,
    ) -> None:
        self._store = store
        self._bus = bus
        self._registry = registry
        self._catalog = catalog
        self._make_checkpointer = make_checkpointer
        self._workroot = workroot
        self._poll = poll_interval_s
        self._gates = GateManager(store, bus)
        self.mission_id = mission_id
        self._supervisor: RunSupervisor | None = None

    async def run(self) -> None:
        mission = await self._store.get_mission(self.mission_id)
        if mission is None:
            raise ValueError(f"mission {self.mission_id} not found")
        self._workroot.mkdir(parents=True, exist_ok=True)
        if mission.status == MissionStatus.PENDING:
            await self._store.update_mission(mission.id, status=MissionStatus.RUNNING)
        self._supervisor = RunSupervisor(
            mission,
            self._store,
            self._bus,
            self._registry,
            self._catalog,
            self._make_checkpointer,
            self._workroot,
        )
        try:
            await self._run_steps(mission)
        finally:
            await self._supervisor.aclose()

    async def _run_steps(self, mission: Mission) -> None:
        for step in _plan_steps(mission):
            runs = await self._dispatch_step(mission, step)
            await self._pump_until_terminal(runs)
        runs = await self._store.list_runs(mission.id)
        artifacts = {f"{r.role}:{r.step}": r.artifact for r in runs if r.artifact is not None}
        failed = [r for r in runs if r.status == RunStatus.FAILED]
        result = {"artifacts": artifacts}
        status = MissionStatus.FAILED if failed else MissionStatus.DONE
        await self._store.update_mission(mission.id, status=status, result=result)
        event = EventType.MISSION_DONE if status == MissionStatus.DONE else EventType.MISSION_FAILED
        await self._bus.publish(
            EventRecord(mission_id=mission.id, type=event, payload={"runs": len(runs)})
        )

    async def _dispatch_step(self, mission: Mission, step: dict[str, Any]) -> list[Run]:
        role = str(step["role"])
        step_name = str(step.get("step") or role)
        attempts = await self._store.list_runs(mission.id)
        prior = [r for r in attempts if r.step == step_name and r.role == role]
        if prior:
            # recovery: step already dispatched; restart crashed/never-started runs
            assert self._supervisor is not None
            for run in prior:
                await self._supervisor.ensure_started(run)
            return prior
        run = Run(
            id=make_run_id(mission.id, step_name, role, 1),
            mission_id=mission.id,
            role=role,
            step=step_name,
            context=dict(step.get("context") or {}),
        )
        created = await self._store.create_run(run)
        assert self._supervisor is not None
        await self._supervisor.ensure_started(created)
        return [created]

    async def _pump_until_terminal(self, runs: list[Run]) -> None:
        while True:
            fetched = [await self._store.get_run(r.id) for r in runs]
            fresh: list[Run] = [r for r in fetched if r is not None]
            halted = [r for r in fresh if r.status == RunStatus.HALTED_ESCALATED]
            if halted:
                await self._handle_halted(halted)
            if all(_terminal(r) for r in fresh):
                return
            await asyncio.sleep(self._poll)

    async def _handle_halted(self, halted: list[Run]) -> None:
        """Materialize gates for halted runs, then wait out decisions and apply."""
        assert self._supervisor is not None
        for run in halted:
            payload = run.escalation or {}
            if not await self._has_open_gate(run):
                gate = await self._gates.request(
                    self.mission_id,
                    kind=self._gate_kind(payload),
                    question=str(payload.get("question") or payload.get("detail") or "proceed?"),
                )
                await self._bus.publish(
                    EventRecord(
                        mission_id=self.mission_id,
                        run_id=run.id,
                        type=EventType.INFO,
                        payload={"gate_id": gate.id, "for_run": run.id},
                    )
                )
        await self._store.update_mission(self.mission_id, status=MissionStatus.AWAITING_GATE)
        while True:
            open_gates = await self._store.list_open_gates(self.mission_id)
            if not open_gates:
                break
            await asyncio.sleep(self._poll)
        await self._store.update_mission(self.mission_id, status=MissionStatus.RUNNING)
        # apply decisions to halted runs (match by question; M1 halts one at a time)
        for run in halted:
            fresh = await self._store.get_run(run.id)
            if fresh is None or fresh.status != RunStatus.HALTED_ESCALATED:
                continue
            decision = await self._decision_for(fresh)
            if decision is None:
                continue
            approved, _gate_id = decision
            if approved:
                await self._supervisor.resume_with_decision(fresh, True)
            else:
                await self._store.update_run(fresh.id, status=RunStatus.ABORTED)
                await self._bus.publish(
                    EventRecord(
                        mission_id=self.mission_id,
                        run_id=fresh.id,
                        type=EventType.CANCELLED,
                        payload={"reason": "gate rejected"},
                    )
                )

    async def _has_open_gate(self, run: Run) -> bool:
        payload = run.escalation or {}
        question = str(payload.get("question") or "")
        gates = await self._store.list_open_gates(self.mission_id)
        return any(g.question == question for g in gates)

    async def _decision_for(self, run: Run) -> tuple[bool, str] | None:
        """(approved, gate_id) for this run's halt question, from the event log.
        The event log is store truth: it survives daemon restarts, so a decision
        made while we were down is picked up here (ADR 0002)."""
        payload = run.escalation or {}
        question = str(payload.get("question") or "")
        events = await self._store.events_since(0, mission_id=self.mission_id, limit=2000)
        gate_id: str | None = None
        for e in events:  # last GATE_OPENED for this question wins
            if e.type == EventType.GATE_OPENED and e.payload.get("question") == question:
                gate_id = e.payload.get("gate_id")
        if gate_id is None:
            return None
        for e in reversed(events):  # latest decision for that gate
            if e.type == EventType.GATE_DECIDED and e.payload.get("gate_id") == gate_id:
                return bool(e.payload.get("approved")), gate_id
        return None

    @staticmethod
    def _gate_kind(payload: dict[str, Any]) -> GateKind:
        try:
            return GateKind(str(payload.get("kind") or "generic"))
        except ValueError:
            return GateKind.GENERIC


def new_mission_id() -> str:
    return new_id("m")
