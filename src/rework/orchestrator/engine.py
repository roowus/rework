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
from rework.kernel.llm.catalog import ModelCatalog, ModelProvider, UnknownModelError
from rework.kernel.state.records import (
    EventRecord,
    EventType,
    GateKind,
    GateStatus,
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
from rework.nodes.report_sections import mechanical_verdict
from rework.roles.base import EscalationKind, RoleBuildCtx
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


def _json_dump(value: Any) -> str:
    import json

    try:
        return json.dumps(value)
    except (TypeError, ValueError):
        return str(value)


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
        git_author: tuple[str, str] = ("rework", "rework@rework.local"),
        provider: ModelProvider | None = None,
        library: Any | None = None,
    ) -> None:
        self._mission = mission
        self._store = store
        self._bus = bus
        self._registry = registry
        self._catalog = catalog
        self._make_checkpointer = make_checkpointer
        self._workroot = workroot
        self._git_author = git_author
        self._provider = provider  # test/di override; None = build from catalog
        self._library = library  # LibraryHandle | None (M5); never breaks a run
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self._closers: list[Callable[[], Awaitable[None]]] = []
        self._ctxs: dict[str, RoleBuildCtx] = {}

    async def _build_graph(self, run: Run) -> Any:
        spec = self._registry.get(run.role)
        wanted = str(run.model_ref.get("id") or spec.default_model)
        try:
            entry = self._catalog.resolve(wanted)
        except UnknownModelError:
            # a deployment catalog without this role's default id (echo ships
            # 'fake'): fall back to the cheapest real model, visibly, rather
            # than failing the run before it starts
            entry = self._catalog.cheapest() or self._catalog.resolve(wanted)
            await self._bus.publish(
                EventRecord(
                    mission_id=self._mission.id,
                    run_id=run.id,
                    type=EventType.INFO,
                    payload={
                        "note": f"model {wanted!r} not in catalog; using {entry.id!r} "
                        "(cheapest available)"
                    },
                )
            )
        workdir = self._workroot / run.id.replace(":", "_")
        workdir.mkdir(parents=True, exist_ok=True)  # roles assume their workspace exists
        from rework.kernel.budget.ledger import Budget
        from rework.kernel.llm.adapters.retry import with_retries

        # the resolved catalog id belongs on the run row: escalation policies
        # (needs_model promotion) read it from there
        if not run.model_ref.get("id"):
            resolved = {**run.model_ref, "id": entry.id}
            await self._store.update_run(run.id, model_ref=resolved)
            run = run.model_copy(update={"model_ref": resolved})

        # plan-level mission budget overrides the spec default cap
        budget = spec.budget
        plan_cap = ((self._mission.plan or {}).get("budget") or {}).get("max_usd")
        if plan_cap is not None:
            budget = Budget(
                max_usd=float(plan_cap),
                max_input_tokens=spec.budget.max_input_tokens,
                max_output_tokens=spec.budget.max_output_tokens,
            )
        base_provider = self._provider or build_provider(entry)
        library_menu = ""
        if self._library is not None:
            try:
                library_menu = await self._library.menu()
            except Exception as exc:  # the Library must never break a run
                await self._bus.publish(
                    EventRecord(
                        mission_id=self._mission.id,
                        run_id=run.id,
                        type=EventType.INFO,
                        payload={"error": f"library menu unavailable: {exc}"},
                    )
                )
        ctx = RoleBuildCtx(
            run=run,
            mission=self._mission,
            catalog=self._catalog,
            provider=base_provider if self._provider else with_retries(base_provider),
            store=self._store,
            bus=self._bus,
            gates=GateManager(self._store, self._bus),
            budget=budget,
            workdir=workdir,
            git_author=self._git_author,
            library=self._library,
            library_menu=library_menu,
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
        git_author: tuple[str, str] = ("rework", "rework@rework.local"),
        provider: ModelProvider | None = None,
        library: Any | None = None,
    ) -> None:
        self._store = store
        self._bus = bus
        self._registry = registry
        self._catalog = catalog
        self._make_checkpointer = make_checkpointer
        self._workroot = workroot
        self._poll = poll_interval_s
        self._git_author = git_author
        self._provider = provider
        self._library = library
        self._plan: dict[str, Any] | None = None
        self._gates = GateManager(store, bus)
        self.mission_id = mission_id
        self._supervisor: RunSupervisor | None = None

    async def run(self) -> None:
        mission = await self._store.get_mission(self.mission_id)
        if mission is None:
            raise ValueError(f"mission {self.mission_id} not found")
        self._plan = mission.plan
        self._workroot.mkdir(parents=True, exist_ok=True)
        mission = await self._ensure_repo(mission)
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
            self._git_author,
            self._provider,
            self._library,
        )
        try:
            await self._run_steps(mission)
        finally:
            await self._supervisor.aclose()

    async def _ensure_repo(self, mission: Mission) -> Mission:
        """Git transport (ADR 0005): a repo_origin with no path means the
        daemon clones from origin itself, and the row then carries a path that
        is real on this machine. Idempotent across restarts: an existing clone
        is reused, and the row update persists so recovery skips the clone."""
        if not mission.repo_origin or mission.repo_path:
            return mission
        from rework.nodes.git_ops import clone_origin

        dest = self._workroot / "missions" / mission.id / "repo"

        def _clone_ready() -> bool:
            return (dest / ".git").is_dir()

        if not await asyncio.to_thread(_clone_ready):
            await clone_origin(mission.repo_origin, dest)
            await self._bus.publish(
                EventRecord(
                    mission_id=mission.id,
                    type=EventType.PROGRESS,
                    payload={"note": f"cloned {mission.repo_origin} (git transport)"},
                )
            )
        updated = await self._store.update_mission(mission.id, repo_path=str(dest))
        return updated or mission

    async def _run_steps(self, mission: Mission) -> None:
        steps = list(_plan_steps(mission))
        results: dict[str, Run] = {}  # step_name -> latest terminal run
        audit_rounds: dict[str, int] = {}
        last_audit_artifact: dict[str, Any] | None = None
        index = 0
        step_retries: dict[str, int] = {}
        while index < len(steps):
            step = steps[index]
            step_name = str(step.get("step") or step["role"])
            runs = await self._dispatch_step(mission, step)
            pump_result = await self._pump_until_terminal(runs)
            if pump_result == "cancelled":
                await self._synthesize(mission.id, ship_decision=None)
                return
            fresh = await self._store.get_run(runs[-1].id)
            if fresh is not None:
                results[step_name] = fresh

            # retry rule: a step that died on a transient failure (rate limit,
            # provider 5xx) gets ONE in-mission retry before the plan moves on.
            # Found live: a 429-storm killed the docs mission's investigator,
            # which never got the recovery pass's second chance because the
            # mission stayed up.
            if fresh is not None and fresh.status == RunStatus.FAILED:
                retries = step_retries.get(step_name, 0)
                if retries < 1:
                    step_retries[step_name] = retries + 1
                    await self._bus.publish(
                        EventRecord(
                            mission_id=mission.id,
                            type=EventType.INFO,
                            payload={
                                "note": f"step {step_name} failed; retrying once "
                                f"(attempt {retries + 2})",
                            },
                        )
                    )
                    index += 1
                    steps[index:index] = [{**step}]
                    continue
            if step.get("on_request_changes") and fresh is not None:
                last_audit_artifact = fresh.artifact

            # routing rule: an audit that requests changes sends the revise
            # target back for another attempt, then re-audits (max_rounds cap)
            rule = step.get("on_request_changes")
            if rule and fresh is not None and fresh.artifact:
                verdict = mechanical_verdict(fresh.artifact)
                rounds = audit_rounds.get(step_name, 1)
                if verdict == "request changes" and rounds < int(rule.get("max_rounds", 2)):
                    audit_rounds[step_name] = rounds + 1
                    revise_name = str(rule.get("revise") or "")
                    revise_spec = next(
                        (s for s in steps if str(s.get("step") or s["role"]) == revise_name),
                        None,
                    )
                    if revise_spec is not None:
                        steps[index + 1 : index + 1] = [
                            {
                                **revise_spec,
                                "context": {
                                    **(revise_spec.get("context") or {}),
                                    "investigation": _json_dump(fresh.artifact.get("findings")),
                                    "prior_audit": _json_dump(fresh.artifact),
                                    "_revision": rounds + 1,
                                },
                            },
                            {
                                **step,
                                "context": {
                                    **step.get("context", {}),
                                    "_revision": rounds + 1,
                                    "prior_dispositions": _json_dump(fresh.artifact),
                                },
                            },
                        ]
            index += 1

        # ship gate: the plan's final human decision before synthesis. An
        # existing row (open or decided) is REUSED: a restart after the owner
        # already decided must not open a duplicate gate (chaos-test finding)
        gates_spec = (mission.plan or {}).get("gates") or []
        ship_decision: bool | None = None
        if "ship" in gates_spec:
            gate = await self._latest_gate_of_kind(GateKind.SHIP)
            if gate is None:
                verdict = (
                    mechanical_verdict(last_audit_artifact) if last_audit_artifact else "approve"
                )
                gate = await self._gates.request(
                    mission.id,
                    kind=GateKind.SHIP,
                    question=f"Ship the mission result? audit verdict: {verdict}",
                )
                await self._bus.publish(
                    EventRecord(
                        mission_id=mission.id,
                        type=EventType.INFO,
                        payload={"note": f"ship gate {gate.id} open (verdict: {verdict})"},
                    )
                )
            if gate.status == GateStatus.OPEN:
                await self._store.update_mission(mission.id, status=MissionStatus.AWAITING_GATE)
            ship_decision = await self._gate_decision(mission.id, gate.id)

        # open_pr gate: human approves publishing the branch as a PR, then the
        # push + PR creation are mechanical (agents never merge; ADR 0003)
        pr_url: str | None = None
        if "open_pr" in gates_spec:
            gate = await self._latest_gate_of_kind(GateKind.OPEN_PR)
            if gate is None:
                verdict = (
                    mechanical_verdict(last_audit_artifact) if last_audit_artifact else "approve"
                )
                gate = await self._gates.request(
                    mission.id,
                    kind=GateKind.OPEN_PR,
                    question=(f"Open a pull request for this change? audit verdict: {verdict}"),
                )
                await self._bus.publish(
                    EventRecord(
                        mission_id=mission.id,
                        type=EventType.INFO,
                        payload={"note": f"open_pr gate {gate.id} open (verdict: {verdict})"},
                    )
                )
            if gate.status == GateStatus.OPEN:
                await self._store.update_mission(mission.id, status=MissionStatus.AWAITING_GATE)
            pr_approved = await self._gate_decision(mission.id, gate.id)
            if pr_approved:
                pr_url = await self._create_pr(mission, results)

        self._pr_url = pr_url
        await self._synthesize(mission.id, ship_decision)

    def _pr_body(
        self, mission: Mission, coder_artifact: dict[str, Any], audit_run: Run | None
    ) -> str:
        """The PR description, assembled mechanically from typed artifacts:
        the coder's summary, the audit's verdict + findings + warnings, and the
        run-stats table. Never LLM-written (same rule as the md report)."""
        lines: list[str] = [
            f"Produced by rework mission `{mission.id}`.",
            "",
            "## What changed",
            "",
            str(coder_artifact.get("summary") or "")[:4000],
            "",
        ]
        audit_artifact = (audit_run.artifact if audit_run else None) or {}
        verdict = mechanical_verdict(audit_artifact) if audit_artifact else "no audit"
        lines += ["## Audit", "", f"Verdict: **{verdict}** (mechanical mapping)."]
        findings = audit_artifact.get("findings", [])
        if findings:
            lines += ["", "| severity | verdict | finding |", "|---|---|---|"]
            for f in findings:
                lines.append(f"| {f.get('severity')} | {f.get('verdict')} | {f.get('title')} |")
            for f in findings:
                lines += ["", f"**{f.get('title')}**", "", str(f.get("detail", ""))]
                evidence = f.get("evidence") or []
                if evidence:
                    lines += ["", "Evidence: " + " · ".join(f"`{e}`" for e in evidence)]
        else:
            lines += ["", "No findings (clean audit)."]
        warnings = audit_artifact.get("warnings", [])
        if warnings:
            lines += ["", "### Audit warnings (coverage limits)", ""]
            for w in warnings:
                lines.append(f"- {w}")
        stats = coder_artifact.get("run_stats")
        if stats:
            elapsed = f"{stats.get('elapsed_seconds', 0.0):.1f}s"
            cost = f"${stats.get('cost_usd', 0.0):.4f} est"
            lines += [
                "",
                "## Run stats",
                "",
                f"{stats.get('model_calls', 0)} model calls ·"
                f" {stats.get('input_tokens', 0):,} in / {stats.get('output_tokens', 0):,} out"
                f" (cached {stats.get('cached_tokens', 0):,})"
                f" · {cost} · {elapsed}",
            ]
        lines += [
            "",
            f"Full report: `rework report {mission.id} --md`.",
            "",
            "_Agent-authored, human-merged: rework dogfooding rules._",
        ]
        return "\n".join(lines)

    async def _create_pr(self, mission: Mission, results: dict[str, Run]) -> str | None:
        """Push the coder's branch and open a PR. Mechanical: title/body from
        the brief + audit verdict; the human does the merging."""
        from rework.nodes.gh import pr_create
        from rework.nodes.git_ops import push_branch

        coder_run = results.get("code")
        if coder_run is None or coder_run.artifact is None:
            await self._bus.publish(
                EventRecord(
                    mission_id=mission.id,
                    type=EventType.INFO,
                    payload={"error": "open_pr approved but no coder artifact to publish"},
                )
            )
            return None
        branch = str(coder_run.artifact.get("branch") or "")
        worktree = Path(str(coder_run.artifact.get("worktree") or mission.repo_path or "."))
        title = f"rework: {mission.objective}"
        body = self._pr_body(mission, coder_run.artifact, results.get("audit"))
        await push_branch(worktree, branch)
        url = await pr_create(worktree, head=branch, title=title, body=body)
        await self._bus.publish(
            EventRecord(
                mission_id=mission.id,
                type=EventType.INFO,
                payload={"note": f"PR opened: {url}"},
            )
        )
        return url

    async def _synthesize(self, mission_id: str, ship_decision: bool | None) -> None:
        runs = await self._store.list_runs(mission_id)
        artifacts = {f"{r.role}:{r.step}": r.artifact for r in runs if r.artifact is not None}
        # mission outcome counts only the LATEST attempt per step: a failed
        # attempt that a later retry superseded must not fail the mission
        # (found live: mission 2's 503-dead investigator was retried green)
        latest: dict[tuple[str, str], Run] = {}
        for r in runs:  # list_runs is created_at-ordered
            latest[(r.step, r.role)] = r
        failed = [r for r in latest.values() if r.status == RunStatus.FAILED]
        result: dict[str, Any] = {"artifacts": artifacts}
        pr_url = getattr(self, "_pr_url", None)
        if pr_url:
            result["pr_url"] = pr_url
        current = await self._store.get_mission(mission_id)
        if current is not None and current.status == MissionStatus.CANCELLED:
            # the burn gate (or an owner cancel) already set the verdict while
            # steps were mid-flight; synthesis attaches artifacts but must not
            # resurrect a cancelled mission as DONE (chaos-test finding)
            status = MissionStatus.CANCELLED
            result = {**(current.result or {}), **result}
        elif ship_decision is False:
            status = MissionStatus.CANCELLED
            result["ship"] = "rejected at ship gate"
        elif failed:
            status = MissionStatus.FAILED
        else:
            status = MissionStatus.DONE
        await self._store.update_mission(mission_id, status=status, result=result)
        event = EventType.MISSION_DONE if status == MissionStatus.DONE else EventType.MISSION_FAILED
        await self._bus.publish(
            EventRecord(mission_id=mission_id, type=event, payload={"runs": len(runs)})
        )

    async def _gate_decision(self, mission_id: str, gate_id: str) -> bool:
        """Read the decision from the gate ROW (the source of truth). The
        row-update and its event commit separately, so the event log can lag
        the empty-open-gates check by a beat; polling the row closes that race
        (found as a rare CI-only 'ship rejected despite approval')."""
        while True:
            gate = await self._store.get_gate(gate_id)
            if gate is not None and gate.status != GateStatus.OPEN:
                return gate.status == GateStatus.APPROVED
            await asyncio.sleep(self._poll)

    async def _dispatch_step(self, mission: Mission, step: dict[str, Any]) -> list[Run]:
        role = str(step["role"])
        step_name = str(step.get("step") or role)
        attempts = await self._store.list_runs(mission.id)
        prior = [r for r in attempts if r.step == step_name and r.role == role]
        if prior and "_revision" not in (step.get("context") or {}):
            if prior[-1].status == RunStatus.COMPLETED:
                # recovery: this step already produced its artifact; keep it
                return [prior[-1]]
            if prior[-1].status in {
                RunStatus.INTENT,
                RunStatus.RUNNING,
                RunStatus.HALTED_ESCALATED,
            }:
                # recovery: this step's latest attempt is still in flight
                assert self._supervisor is not None
                for run in prior:
                    if run.status == RunStatus.HALTED_ESCALATED:
                        gate = await self._latest_gate_for_run(run)
                        if gate is not None and gate.status == GateStatus.APPROVED:
                            # decided while the engine was down: resume with
                            # the row's decision (a plain resume would re-fire
                            # the interrupt and open a duplicate gate)
                            await self._supervisor.resume_with_decision(run, True)
                            continue
                        if gate is not None and gate.status == GateStatus.REJECTED:
                            await self._store.update_run(run.id, status=RunStatus.ABORTED)
                            await self._bus.publish(
                                EventRecord(
                                    mission_id=self.mission_id,
                                    run_id=run.id,
                                    type=EventType.CANCELLED,
                                    payload={"reason": "gate rejected while engine was down"},
                                )
                            )
                            continue
                    await self._supervisor.ensure_started(run)
                return [prior[-1]]
        attempt = len(prior) + 1
        # Model/effort resolution: a step override wins, then the plan-level
        # default (the TUI writes plan["model"]/plan["effort"]), then the
        # role's default_model at graph build. model_ref is a free-form dict
        # on the run row, so both ride along with no schema change.
        plan = mission.plan or {}
        model_id = step.get("model") or plan.get("model")
        effort = step.get("effort") or plan.get("effort")
        model_ref: dict[str, Any] = {}
        if model_id:
            model_ref["id"] = str(model_id)
        if effort:
            model_ref["effort"] = str(effort)
        run = Run(
            id=make_run_id(mission.id, step_name, role, attempt),
            mission_id=mission.id,
            role=role,
            step=step_name,
            attempt=attempt,
            context=await self._resolve_context(mission, step),
            model_ref=model_ref,
        )
        created = await self._store.create_run(run)
        assert self._supervisor is not None
        await self._supervisor.ensure_started(created)
        return [created]

    async def _resolve_context(self, mission: Mission, step: dict[str, Any]) -> dict[str, Any]:
        """Static step context plus wiring: context_from pulls a prior step's
        artifact (or one of its fields) into this run's context. This is the
        orchestrator's inter-role policy in M2 form: mechanical, declarative,
        diff-only where the plan says so. Precedence: wiring fills first,
        static context overrides, so revision-injected context (audit findings
        handed to a revision attempt) survives the wiring."""
        context: dict[str, Any] = {}
        wiring = step.get("context_from") or {}
        for key, source in wiring.items():
            src_step = str(source.get("step") or "")
            field = source.get("field")
            candidates = [
                r
                for r in await self._store.list_runs(mission.id)
                if r.step == src_step and r.artifact is not None
            ]
            if not candidates:
                continue
            artifact = candidates[-1].artifact or {}
            context[key] = artifact.get(field) if field else artifact
        context.update(step.get("context") or {})
        return context

    async def _pump_until_terminal(self, runs: list[Run]) -> str:
        """Pump until every run is terminal. Returns "cancelled" when the burn
        gate cancelled the mission (the caller skips to synthesis). The burn
        gate is checked BEFORE handling halts: a spend-cap hold must not sit
        behind an unanswered escalation gate."""
        while True:
            fetched = [await self._store.get_run(r.id) for r in runs]
            fresh: list[Run] = [r for r in fetched if r is not None]
            if await self._burn_gate_hit() == "cancelled":
                return "cancelled"
            halted = [r for r in fresh if r.status == RunStatus.HALTED_ESCALATED]
            if halted:
                await self._handle_halted(halted)
            if all(_terminal(r) for r in fresh):
                return "ok"
            await asyncio.sleep(self._poll)

    async def _burn_gate_hit(self) -> str:
        """The owner's burn-gate: plan budget cap exceeded puts the mission
        ON_HOLD and opens a continue/abort gate (ADR 0003 decision 6).
        Returns "cancelled" (stop pumping) or "ok" (cap raised or not hit)."""
        cap = ((self._plan or {}).get("budget") or {}).get("max_usd")
        if cap is None:
            return "ok"
        spend = await self._store.mission_spend(self.mission_id)
        if spend.spent_usd <= float(cap):
            return "ok"
        mission = await self._store.get_mission(self.mission_id)
        if mission is None or mission.status != MissionStatus.RUNNING:
            return "ok"  # already held, or not ours to hold
        gate = await self._gates.request(
            self.mission_id,
            kind=GateKind.GENERIC,
            question=(
                f"Burn cap hit: ${spend.spent_usd:.2f} spent > ${float(cap):.2f} cap."
                f" Continue (cap raised to ${float(cap) * 2:.2f}) or cancel?"
            ),
        )
        await self._store.update_mission(self.mission_id, status=MissionStatus.ON_HOLD)
        await self._bus.publish(
            EventRecord(
                mission_id=self.mission_id,
                type=EventType.INFO,
                payload={
                    "note": f"burn cap hit (${spend.spent_usd:.2f} > ${float(cap):.2f}); "
                    f"mission ON_HOLD, gate {gate.id}"
                },
            )
        )
        while True:
            if not await self._store.list_open_gates(self.mission_id):
                break
            await asyncio.sleep(self._poll)
        decision = await self._gate_decision(self.mission_id, gate.id)
        if not decision:
            await self._store.update_mission(
                self.mission_id,
                status=MissionStatus.CANCELLED,
                result={"reason": "burn cap rejected"},
            )
            return "cancelled"
        budget = (self._plan or {}).get("budget") or {}
        budget["max_usd"] = float(cap) * 2
        if isinstance(self._plan, dict):
            self._plan["budget"] = budget
            await self._store.update_mission(
                self.mission_id,
                plan=self._plan,
            )
        await self._store.update_mission(self.mission_id, status=MissionStatus.RUNNING)
        await self._bus.publish(
            EventRecord(
                mission_id=self.mission_id,
                type=EventType.INFO,
                payload={"note": f"burn cap raised to ${float(cap) * 2:.2f}; continuing"},
            )
        )
        return "ok"

    async def _handle_halted(self, halted: list[Run]) -> None:
        """Apply escalation policies, then materialize gates for the rest.

        needs_model is handled mechanically first (promote within the provider,
        resume the role): the human only sees it when the catalog runs out.
        Everything else becomes a gate; the gate ID opened FOR a run is tracked
        so decisions are read from that row directly (question-matching broke
        for escalations whose payload carries detail, not question)."""
        assert self._supervisor is not None
        gated: list[tuple[Run, str | None]] = []  # (run, open_gate_id or None)
        for run in halted:
            # a resume already in flight (earlier iteration): skip; the row
            # flips RUNNING when the resume lands, and re-gating a resuming
            # run would loop forever (found live in the exhaustion test)
            live = self._supervisor.tasks.get(run.id)
            if live is not None and not live.done():
                continue
            payload = run.escalation or {}
            if str(payload.get("kind")) == EscalationKind.NEEDS_MODEL.value:
                if await self._promote_and_resume(run):
                    continue
            # falls through to a human gate; reuse the run's existing open
            # gate when a previous pass opened one (idempotent materialization)
            existing = await self._open_gate_for_run(run)
            if existing is None:
                # for_run rides ON the row: recovery reads the link from the
                # gate itself, so a kill between commit and event publish can
                # no longer orphan the gate (see Gate.for_run)
                gate = await self._gates.request(
                    self.mission_id,
                    kind=self._gate_kind(payload),
                    question=str(payload.get("question") or payload.get("detail") or "proceed?"),
                    for_run=run.id,
                )
                gated.append((run, gate.id))
            else:
                gated.append((run, existing.id))
        if gated:
            await self._store.update_mission(self.mission_id, status=MissionStatus.AWAITING_GATE)
        while True:
            open_gates = await self._store.list_open_gates(self.mission_id)
            if not open_gates:
                break
            await asyncio.sleep(self._poll)
        if gated:
            await self._store.update_mission(self.mission_id, status=MissionStatus.RUNNING)
        # apply decisions, straight from each run's gate row
        for run, gate_id in gated:
            if gate_id is None:
                continue
            gate_row = await self._store.get_gate(gate_id)
            if gate_row is None or gate_row.status == GateStatus.OPEN:
                continue  # decided while we were down is handled by recovery; not open here
            fresh = await self._store.get_run(run.id)
            if fresh is None or fresh.status != RunStatus.HALTED_ESCALATED:
                continue
            if gate_row.status == GateStatus.APPROVED:
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

    async def _gates_for_run(self, run: Run) -> list[Any]:
        """Every gate row pointing at this run, oldest first.

        The link is the gate row's `for_run` column. It used to be an INFO
        event published right AFTER the gate row was committed, so a kill in
        between left a gate nobody could attribute: recovery found no decision,
        the run stayed HALTED_ESCALATED, the pump looped forever and the
        mission hung until a 60s test timeout (found live on a slow 3.13 CI
        runner). The row is written atomically with the gate, so it cannot
        lose the race."""
        gates = await self._store.list_gates(self.mission_id)
        return [g for g in gates if g.for_run == run.id]

    async def _open_gate_for_run(self, run: Run) -> Any:
        """The run's open gate, if a previous pass materialized one."""
        for gate in reversed(await self._gates_for_run(run)):
            if gate.status == GateStatus.OPEN:
                return gate
        return None

    async def _latest_gate_for_run(self, run: Run) -> Any:
        """The run's most recent gate ROW, any status. Recovery uses this to
        apply a decision made while the engine was down: resuming the parked
        graph plain would re-fire the interrupt and open a duplicate gate."""
        gates = await self._gates_for_run(run)
        return gates[-1] if gates else None

    async def _latest_gate_of_kind(self, kind: GateKind) -> Any:
        """The mission's most recent gate of this kind, any status (ship and
        open_pr recovery: a restart must read the EXISTING gate, not open a
        duplicate for a decision the owner already made).

        Reads the ROWS, not the event stream: the row is committed before any
        event is published, and a 5000-event scan can also miss a gate on a
        long mission while the table cannot."""
        gates = [g for g in await self._store.list_gates(self.mission_id) if g.kind == kind]
        return gates[-1] if gates else None

    async def _promote_and_resume(self, run: Run) -> bool:
        """needs_model policy: promote within the provider (cross-provider is
        config-gated, ADR 0001) and resume the role with the new tier. Returns
        True when handled mechanically; False falls through to a human gate.
        Reads the run ROW: model_ref gets resolved onto it at graph-build time,
        and the in-memory object here may predate that."""
        payload = run.escalation or {}
        fresh = await self._store.get_run(run.id)
        current_id = str((fresh or run).model_ref.get("id") or "")
        promoted = self._catalog.promote(current_id) if current_id else None
        if promoted is None:
            await self._bus.publish(
                EventRecord(
                    mission_id=self.mission_id,
                    run_id=run.id,
                    type=EventType.INFO,
                    payload={
                        "note": f"needs_model: no higher tier for {current_id!r}; "
                        "escalating to human"
                    },
                )
            )
            return False
        new_ref = {**(fresh or run).model_ref, "id": promoted.id}
        await self._store.update_run(run.id, model_ref=new_ref)
        await self._bus.publish(
            EventRecord(
                mission_id=self.mission_id,
                run_id=run.id,
                type=EventType.MODEL_PROMOTED,
                payload={
                    "from": current_id,
                    "to": promoted.id,
                    "detail": str(payload.get("detail") or "")[:200],
                },
            )
        )
        resumed = await self._store.get_run(run.id)
        if resumed is not None and self._supervisor is not None:
            await self._supervisor.resume_with_decision(
                resumed, {"resolution": "model_promoted", "model": promoted.id}
            )
        return True

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
