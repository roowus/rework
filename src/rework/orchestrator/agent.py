"""The mission orchestrator (ADR 0007): an LLM that runs a mission by
delegating every piece of work to roles.

It never does mission work itself. Its tools spawn roles, wait for them, read
their handoffs, look in on running ones, cancel, talk to the owner and finish.
The engine keeps the rails: budget (burn gate), a runaway turn cap, owner
pause/cancel, escalation policies, and the plan's fixed final gates.

Recovery (CLAUDE.md rule 3): the conversation is persisted turn by turn (a chat
row with id `orch-<mission>`), every turn's system prompt carries a digest
rebuilt from store rows, and spawns are idempotent (deterministic run ids plus
the tool-call id recorded on the run), so a restarted daemon reloads the
conversation and carries on.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING, Any

from rework.kernel.llm.types import ChatRequest, ToolSpec
from rework.kernel.state.records import (
    Charge,
    Chat,
    ChatMessage,
    EventRecord,
    EventType,
    GateStatus,
    Mission,
    MissionStatus,
    Run,
    RunStatus,
)
from rework.kernel.state.records import run_id as make_run_id
from rework.logs import bind

log = logging.getLogger("rework.orchestrator")

if TYPE_CHECKING:
    from rework.orchestrator.engine import MissionRunner

MARKER = "You are the MISSION ORCHESTRATOR"
MAX_TURNS = 80  # runaway protection only; the budget is the real rail
MAX_IDLE_NUDGES = 3

PLAYBOOKS: dict[str, str] = {
    "echo": (
        "Plumbing check: spawn the `echo` role once with the objective as its brief, "
        "wait for it, then finish."
    ),
    "fix": (
        "Usual shape: an investigator finds out what is going on (cheap model, quick "
        "depth unless the problem is unclear); a coder implements the change in its "
        "own worktree and runs the tests; an auditor reviews the coder's diff (strong "
        "model). If the auditor requests changes, spawn the coder again with the "
        "findings in its brief, then audit again; stop after two revision rounds and "
        "finish with what you have. Skip the investigator when the change is small "
        "and obvious. After you finish, the owner decides whether to ship."
    ),
    "fix-pr": (
        "Same shape as fix: investigate (optional for small changes), code, audit, "
        "revise on request-changes (at most two rounds). After you finish, the owner "
        "decides whether to open a pull request from the last coder's branch; "
        "agents never merge."
    ),
}

RULES = """\
How you work:
- You only orchestrate. Never do the mission's work yourself: every investigation,
  code change, test run and review is a role you spawn.
- Roles run asynchronously and report back when they finish. Call `wait` to block
  until something happens. Check in on a running role (`check_in`) only when the
  work is hard or failure-prone; for easy work just wait for the handoff.
- You may run roles in parallel (several spawn_role calls in one turn), but be
  conservative: parallelism costs money and most missions are sequential.
- Pick each role's model from the catalog: cheap tiers for exploration and
  straightforward coding, stronger tiers for audits and hard problems.
- Write every brief so the role can act without asking: what to do, why, what
  done looks like, and anything earlier roles found that it needs.
- Read handoffs (`read_artifact`) before deciding what comes next.
- Keep the owner informed with `message_owner` at meaningful moments only.
- Call `finish` when the mission's goal is met, or when it cannot be met (say why).
  Final gates (ship / open a PR) happen after you finish; you do not open them.
- Respond with tool calls. Text without a tool call wastes a turn."""

TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="spawn_role",
        description=(
            "Start a role on a piece of work. Returns its run id. The run starts at once; "
            "call wait to hear back. Several calls in one turn run in parallel."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "role": {"type": "string", "description": "A role name from ROLES."},
                "step": {
                    "type": "string",
                    "description": "Short step name, e.g. investigate / code / audit. "
                    "Reusing a step name after it finished starts a new attempt.",
                },
                "brief": {"type": "string", "description": "What the role must do."},
                "inputs": {
                    "type": "object",
                    "description": "Optional context keys from the role's INPUTS, each set "
                    "to a run id or step name, optionally with .field (e.g. "
                    '{"diff": "code.diff"}). Usual wiring is automatic.',
                },
                "model": {"type": "string", "description": "Catalog id (default: role's)."},
                "effort": {"type": "string", "enum": ["low", "medium", "high"]},
            },
            "required": ["role", "brief"],
        },
    ),
    ToolSpec(
        name="wait",
        description=(
            "Block until a role finishes, fails or escalates, or `minutes` pass. "
            "Returns what happened."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "minutes": {
                    "type": "number",
                    "description": "Wake up after this long even if nothing finished "
                    "(for check-ins). Omit to wait for the next report.",
                }
            },
        },
    ),
    ToolSpec(
        name="read_artifact",
        description="A finished role's handoff: summary, findings, verdict, diff excerpt.",
        input_schema={
            "type": "object",
            "properties": {"run_id": {"type": "string"}},
            "required": ["run_id"],
        },
    ),
    ToolSpec(
        name="check_in",
        description="A running role's recent steps (turns, tool calls, results).",
        input_schema={
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "last": {"type": "integer", "description": "How many steps (default 12)."},
            },
            "required": ["run_id"],
        },
    ),
    ToolSpec(
        name="cancel_run",
        description="Stop a role that is going nowhere. Say why.",
        input_schema={
            "type": "object",
            "properties": {"run_id": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["run_id", "reason"],
        },
    ),
    ToolSpec(
        name="message_owner",
        description="A short progress note shown to the owner in their chat.",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    ),
    ToolSpec(
        name="finish",
        description="End the mission. outcome done = goal met; failed = it cannot be met.",
        input_schema={
            "type": "object",
            "properties": {
                "outcome": {"type": "string", "enum": ["done", "failed"]},
                "summary": {"type": "string", "description": "What happened, for the owner."},
            },
            "required": ["outcome", "summary"],
        },
    ),
]

# usual wiring: context a role gets automatically from the latest finished run
# of the producing role (explicit `inputs` override it)
AUTO_WIRING: dict[str, dict[str, tuple[str, str | None]]] = {
    "coder": {"investigation": ("investigator", None)},
    "auditor": {
        "diff": ("coder", "diff"),
        "worktree_path": ("coder", "worktree"),
        "investigation": ("investigator", None),
    },
}

TERMINAL = {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.ABORTED}


class ToolError(Exception):
    """A tool call the orchestrator got wrong: returned to it as text."""


def orchestrator_chat_id(mission_id: str) -> str:
    return f"orch-{mission_id}"


def orchestrator_run_id(mission_id: str) -> str:
    """Pseudo run id for the orchestrator's own events and charges (no Run row:
    the supervisor must never try to build it as a role)."""
    return make_run_id(mission_id, "orchestrate", "orchestrator", 1)


def _short(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def artifact_summary(artifact: dict[str, Any] | None) -> str:
    """One line a model can scan: summary, verdict, counts."""
    if not artifact:
        return "no artifact"
    bits = []
    if artifact.get("verdict"):
        bits.append(f"verdict={artifact['verdict']}")
    if artifact.get("summary"):
        bits.append(_short(str(artifact["summary"]), 220))
    if artifact.get("findings"):
        bits.append(f"{len(artifact['findings'])} findings")
    if artifact.get("diff"):
        lines = str(artifact["diff"]).splitlines()
        adds = sum(1 for x in lines if x.startswith("+") and not x.startswith("+++"))
        dels = sum(1 for x in lines if x.startswith("-") and not x.startswith("---"))
        bits.append(f"diff +{adds}/-{dels}")
    if artifact.get("blockers"):
        bits.append(f"{len(artifact['blockers'])} blockers")
    return "; ".join(bits) or _short(artifact, 200)


class MissionOrchestrator:
    def __init__(self, runner: MissionRunner, mission: Mission) -> None:
        self.r = runner
        self.mission = mission
        self.store = runner._store
        self.bus = runner._bus
        self.mid = mission.id
        self.chat_id = orchestrator_chat_id(mission.id)
        self.run_id = orchestrator_run_id(mission.id)
        plan = mission.plan or {}
        orch = plan.get("orchestrator") or {}
        self.playbook = str(plan.get("playbook") or "fix")
        catalog = runner._catalog
        wanted = str(orch.get("model") or "")
        try:
            self.entry = catalog.resolve(wanted) if wanted else self._strongest()
        except Exception:
            self.entry = self._strongest()
        self.effort = orch.get("effort") or None
        self.history: list[dict[str, Any]] = []
        self.seen: dict[str, str] = {}  # run id -> status already reported
        self.finished: tuple[str, str] | None = None
        self._provider: Any = None

    def _usable(self) -> list[Any]:
        """Catalog entries this daemon can actually call (real ones first)."""
        entries = self.r._catalog.all()
        real = [e for e in entries if e.provider != "fake" and e.has_credentials()]
        return real or [e for e in entries if e.has_credentials()] or entries

    def _strongest(self) -> Any:
        entries = self._usable()
        return max(entries, key=lambda e: (e.tier, -e.cost_in_per_mtok))

    # -- provider ------------------------------------------------------------

    def provider(self) -> Any:
        if self._provider is None:
            if self.r._provider is not None:
                self._provider = self.r._provider  # tests inject a scripted one
            else:
                from rework.kernel.llm.adapters import build_provider
                from rework.kernel.llm.adapters.retry import with_retries

                self._provider = with_retries(build_provider(self.entry))
        return self._provider

    # -- persistence -------------------------------------------------------

    async def _load(self) -> None:
        chat = await self.store.get_chat(self.chat_id)
        if chat is None:
            await self.store.create_chat(
                Chat(
                    id=self.chat_id,
                    title=f"orchestrator {self.mid}",
                    project_id=self.mission.project_id,
                    model_id=self.entry.id,
                    effort=self.effort,
                    mission_id=self.mid,
                )
            )
            return
        self.history = [m.body for m in await self.store.chat_messages(self.chat_id) if m.body]

    async def _append(self, message: dict[str, Any], display: str = "") -> None:
        self.history.append(message)
        await self.store.append_chat_message(
            ChatMessage(chat_id=self.chat_id, role=message["role"], body=message, display=display)
        )

    async def _emit(self, kind: str, **payload: Any) -> None:
        await self.bus.publish(
            EventRecord(
                mission_id=self.mid,
                run_id=self.run_id,
                type=EventType.PROGRESS,
                payload={"trajectory": kind, "actor": "orchestrator", **payload},
            )
        )

    async def _note(self, text: str, **extra: Any) -> None:
        await self.bus.publish(
            EventRecord(
                mission_id=self.mid,
                run_id=self.run_id,
                type=EventType.INFO,
                payload={"note": text, "actor": "orchestrator", **extra},
            )
        )

    # -- main loop ---------------------------------------------------------

    async def run(self) -> None:
        bind(mission=self.mid, run=self.run_id, role="orchestrator")
        log.info("orchestrator start: model=%s playbook=%s", self.entry.id, self.playbook)
        await self._load()
        await self._recover_runs()
        if not self.history:
            await self._append(
                {
                    "role": "user",
                    "content": f"Mission objective: {self.mission.objective}\n\nBegin.",
                },
                display=self.mission.objective,
            )
        else:
            await self._finish_interrupted_turn()
        await self._loop()
        if self.finished is None:
            return  # cancelled by the owner or the burn gate
        outcome, summary = self.finished
        results = await self._results_by_role()
        audit = results.get("audit")
        ship, pr_url = (None, None)
        if outcome == "done":
            ship, pr_url = await self.r._final_gates(
                self.mission, results, audit.artifact if audit else None
            )
        self.r._pr_url = pr_url
        await self.r._synthesize(self.mid, ship, outcome=outcome, summary=summary)

    async def _loop(self) -> None:
        idle = 0
        turns = sum(1 for m in self.history if m.get("role") == "assistant")
        while self.finished is None:
            signal = await self._owner_gate()
            if signal == "cancelled":
                await self.r._synthesize(self.mid, None)
                return
            if turns >= MAX_TURNS:
                await self._note(f"turn cap ({MAX_TURNS}) reached; finishing as failed")
                self.finished = ("failed", f"orchestrator hit its {MAX_TURNS}-turn cap")
                return
            turns += 1
            result = await self._think()
            if result is None:
                return
            if not result.tool_calls:
                idle += 1
                if idle >= MAX_IDLE_NUDGES:
                    self.finished = ("failed", "orchestrator stopped calling tools")
                    return
                await self._append(
                    {
                        "role": "user",
                        "content": "Respond with tool calls only: spawn_role, wait, "
                        "read_artifact, check_in, cancel_run, message_owner or finish.",
                    }
                )
                continue
            idle = 0
            await self._execute_calls(result.tool_calls)

    async def _owner_gate(self) -> str:
        """Owner pause/cancel and the burn gate, before every turn."""
        signal = await self.r._owner_signal()
        if signal == "cancelled":
            return "cancelled"
        if signal == "paused":
            if await self.r._pause_until_resumed() == "cancelled":
                return "cancelled"
            await self._restart_live_runs()
        if await self.r._burn_gate_hit() == "cancelled":
            return "cancelled"
        return "ok"

    async def _think(self) -> Any:
        request = ChatRequest(
            model=self.entry.model,
            system=await self._system_prompt(),
            messages=self.history,
            tools=TOOLS,
            max_tokens=min(int(self.entry.max_output or 4096), 4096),
            effort=self.effort,
        )
        try:
            result = await self.provider().complete(request)
        except Exception as exc:
            log.exception("orchestrator model call failed")
            await self.bus.publish(
                EventRecord(
                    mission_id=self.mid,
                    run_id=self.run_id,
                    type=EventType.INFO,
                    payload={"error": f"orchestrator model call failed: {exc}"[:400]},
                )
            )
            self.finished = ("failed", f"orchestrator model call failed: {exc}"[:300])
            return None
        usage = result.usage
        await self.store.charge(
            Charge(
                mission_id=self.mid,
                run_id=self.run_id,
                model=self.entry.model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cached_tokens=getattr(usage, "cached_tokens", 0) or 0,
                cost_usd=self.entry.estimate_cost(usage.input_tokens, usage.output_tokens),
            )
        )
        message: dict[str, Any] = {"role": "assistant", "content": result.text or ""}
        if result.tool_calls:
            message["tool_calls"] = [
                {"id": tc.id, "name": tc.name, "arguments": dict(tc.arguments or {})}
                for tc in result.tool_calls
            ]
        await self._append(message, display=result.text or "")
        turn = sum(1 for m in self.history if m.get("role") == "assistant")
        await self._emit(
            "turn",
            turn=turn,
            text=(result.text or "")[:400],
            tools=[tc.name for tc in result.tool_calls],
            tokens=usage.input_tokens + usage.output_tokens,
        )
        return result

    async def _execute_calls(self, calls: list[Any]) -> None:
        # non-blocking tools first, so a spawn and a wait in one turn work
        ordered = sorted(calls, key=lambda c: c.name in ("wait", "finish"))
        for call in ordered:
            args = dict(call.arguments or {})
            log.info(
                "orchestrator %s %s",
                call.name,
                _short(_args_line(args), 200),
                extra={"tool": call.name},
            )
            await self._emit("tool_call", name=call.name, arguments=_short(_args_line(args), 300))
            try:
                output = await self._dispatch(call.id, call.name, args)
                error = ""
            except ToolError as exc:
                output, error = f"error: {exc}", str(exc)
                log.info("orchestrator tool error: %s", exc)
            except Exception as exc:  # a tool bug must not kill the mission
                log.exception("orchestrator tool %s crashed", call.name)
                output = f"error: {type(exc).__name__}: {exc}"
                error = output
            await self._emit(
                "tool_result", name=call.name, result=_short(output, 400), error=error[:200]
            )
            await self._append(
                {"role": "tool", "tool_call_id": call.id, "name": call.name, "content": output}
            )
            if self.finished is not None:
                return

    async def _finish_interrupted_turn(self) -> None:
        """A crash between recording an assistant turn and its tool results
        leaves calls unanswered; providers reject that history. Re-execute them
        (spawn is idempotent by call id)."""
        last = next((m for m in reversed(self.history) if m.get("role") == "assistant"), None)
        if last is None or not last.get("tool_calls"):
            return
        idx = max(i for i, m in enumerate(self.history) if m is last)
        answered = {m.get("tool_call_id") for m in self.history[idx + 1 :]}
        from rework.kernel.llm.types import ToolCall

        pending = [
            ToolCall(id=c["id"], name=c["name"], arguments=c.get("arguments") or {})
            for c in last["tool_calls"]
            if c["id"] not in answered
        ]
        if pending:
            await self._note(f"resuming after restart: re-running {len(pending)} tool call(s)")
            await self._execute_calls(pending)

    # -- tools ---------------------------------------------------------------

    async def _dispatch(self, call_id: str, name: str, args: dict[str, Any]) -> str:
        if name == "spawn_role":
            return await self._spawn(call_id, args)
        if name == "wait":
            return await self._wait(args.get("minutes"))
        if name == "read_artifact":
            return await self._read_artifact(str(args.get("run_id") or ""))
        if name == "check_in":
            return await self._check_in(str(args.get("run_id") or ""), int(args.get("last") or 12))
        if name == "cancel_run":
            return await self._cancel(str(args.get("run_id") or ""), str(args.get("reason") or ""))
        if name == "message_owner":
            return await self._message_owner(str(args.get("text") or ""))
        if name == "finish":
            outcome = str(args.get("outcome") or "done")
            if outcome not in ("done", "failed"):
                raise ToolError("outcome must be done or failed")
            live = [r for r in await self.store.list_runs(self.mid) if r.status not in TERMINAL]
            if live and outcome == "done":
                raise ToolError(
                    "roles are still running: "
                    + ", ".join(r.id for r in live)
                    + ". Wait for them or cancel them first."
                )
            self.finished = (outcome, str(args.get("summary") or ""))
            await self._note(f"finished ({outcome}): {self.finished[1]}"[:400], to_owner=True)
            return "finished"
        raise ToolError(f"unknown tool {name!r}")

    async def _spawn(self, call_id: str, args: dict[str, Any]) -> str:
        role = str(args.get("role") or "").strip()
        registry = self.r._registry
        if role not in registry.names():
            raise ToolError(f"no role {role!r}; roles: {', '.join(registry.names())}")
        brief = str(args.get("brief") or "").strip()
        if not brief:
            raise ToolError("a brief is required")
        step = str(args.get("step") or role).strip() or role
        runs = await self.store.list_runs(self.mid)
        for run in runs:  # re-executed call after a restart
            if (run.context or {}).get("_call") == call_id:
                return f"spawned {run.id} (already running)"
        prior = [r for r in runs if r.step == step and r.role == role]
        live = [r for r in prior if r.status not in TERMINAL]
        if live:
            raise ToolError(f"step {step!r} already has a live run {live[-1].id}")
        if role == "coder" and not self.mission.repo_path:
            raise ToolError("this mission has no repo; a coder cannot run")
        model = str(args.get("model") or "").strip() or None
        if model:
            try:
                entry = self.r._catalog.resolve(model)
            except Exception as exc:
                raise ToolError(f"unknown model {model!r}") from exc
            if not entry.has_credentials():
                usable = ", ".join(e.id for e in self._usable())
                raise ToolError(f"model {model!r} has no API key here; usable: {usable}")
        else:
            model = (self.mission.plan or {}).get("model") or None
        context = await self._wire(role, runs)
        for key, ref in (args.get("inputs") or {}).items():
            context[str(key)] = await self._resolve_ref(str(ref), runs)
        context["brief"] = brief
        context["_call"] = call_id
        model_ref: dict[str, Any] = {}
        if model:
            model_ref["id"] = model
        effort = args.get("effort") or (self.mission.plan or {}).get("effort")
        if effort:
            model_ref["effort"] = str(effort)
        run = Run(
            id=make_run_id(self.mid, step, role, len(prior) + 1),
            mission_id=self.mid,
            role=role,
            step=step,
            attempt=len(prior) + 1,
            context=context,
            model_ref=model_ref,
        )
        created = await self.store.create_run(run)
        assert self.r._supervisor is not None
        await self.r._supervisor.ensure_started(created)
        return f"spawned {created.id} ({role}, model {model or 'role default'})"

    async def _wire(self, role: str, runs: list[Run]) -> dict[str, Any]:
        context: dict[str, Any] = {}
        for key, (producer, field) in AUTO_WIRING.get(role, {}).items():
            done = [r for r in runs if r.role == producer and r.status == RunStatus.COMPLETED]
            if not done or not done[-1].artifact:
                continue
            artifact = done[-1].artifact or {}
            value = artifact.get(field) if field else artifact
            if value:
                context[key] = value
        return context

    async def _resolve_ref(self, ref: str, runs: list[Run]) -> Any:
        """'<run id>[.field]' or '<step>[.field]' -> that run's artifact (field)."""
        target, field = ref, None
        by_id = {r.id: r for r in runs}
        if ref not in by_id and "." in ref:
            target, field = ref.rsplit(".", 1)
        run = by_id.get(target)
        if run is None:
            done = [r for r in runs if r.step == target and r.artifact]
            run = done[-1] if done else None
        if run is None or not run.artifact:
            raise ToolError(f"input {ref!r}: no finished run with an artifact")
        if field is None:
            return run.artifact
        if field not in run.artifact:
            raise ToolError(f"input {ref!r}: artifact has no field {field!r}")
        return run.artifact[field]

    async def _wait(self, minutes: Any) -> str:
        deadline = time.monotonic() + float(minutes) * 60 if minutes else None
        while True:
            if await self._owner_gate() == "cancelled":
                self.finished = None
                return "the owner cancelled the mission"
            runs = await self.store.list_runs(self.mid)
            halted = [r for r in runs if r.status == RunStatus.HALTED_ESCALATED]
            if halted:
                reported = [r for r in halted if self.seen.get(r.id) != "halted"]
                for r in reported:
                    self.seen[r.id] = "halted"
                    payload = r.escalation or {}
                    await self._note(
                        f"{r.id} escalated ({payload.get('kind', '?')}); "
                        "handling by policy or owner gate"
                    )
                # escalation policies + owner gates (blocks until decided)
                await self.r._handle_halted(halted)
                continue
            changed = [r for r in runs if r.status in TERMINAL and self.seen.get(r.id) != r.status]
            if changed:
                lines = []
                for r in changed:
                    self.seen[r.id] = r.status.value
                    lines.append(await self._report(r))
                live = [r.id for r in runs if r.status not in TERMINAL]
                tail = f"\nstill running: {', '.join(live)}" if live else ""
                return "\n".join(lines) + tail
            live_runs = [r for r in runs if r.status not in TERMINAL]
            if not live_runs:
                return "no roles are running. spawn a role or finish."
            if deadline is not None and time.monotonic() >= deadline:
                return "timeout; still running: " + ", ".join(r.id for r in live_runs)
            await asyncio.sleep(self.r._poll)

    async def _report(self, run: Run) -> str:
        if run.status == RunStatus.COMPLETED:
            return f"{run.id} ({run.role}) completed: {artifact_summary(run.artifact)}"
        reason = ""
        events = await self.store.events_since(0, mission_id=self.mid, limit=2000)
        for ev in reversed(events):
            if ev.run_id == run.id and (ev.payload or {}).get("error"):
                reason = str(ev.payload["error"])[:300]
                break
        return f"{run.id} ({run.role}) {run.status.value}" + (f": {reason}" if reason else "")

    async def _read_artifact(self, run_id: str) -> str:
        run = await self.store.get_run(run_id)
        if run is None or run.mission_id != self.mid:
            raise ToolError(f"no run {run_id!r} in this mission")
        if not run.artifact:
            return f"{run_id} has no artifact yet (status {run.status.value})"
        artifact = dict(run.artifact)
        diff = artifact.pop("diff", None)
        artifact.pop("run_stats", None)
        out = json.dumps(artifact, default=str, indent=1)
        if len(out) > 7000:
            out = out[:7000] + "\n… (truncated)"
        if diff:
            lines = str(diff).splitlines()
            excerpt = "\n".join(lines[:150])
            more = f"\n… ({len(lines) - 150} more lines)" if len(lines) > 150 else ""
            out += f"\n\ndiff:\n{excerpt}{more}"
        return out

    async def _check_in(self, run_id: str, last: int) -> str:
        run = await self.store.get_run(run_id)
        if run is None or run.mission_id != self.mid:
            raise ToolError(f"no run {run_id!r} in this mission")
        events = [
            e
            for e in await self.store.events_since(0, mission_id=self.mid, limit=5000)
            if e.run_id == run_id
        ]
        lines = []
        for ev in events[-max(1, min(last, 40)) :]:
            p = ev.payload or {}
            kind = p.get("trajectory")
            if kind == "turn":
                lines.append(f"turn {p.get('turn')}: {_short(p.get('text') or '', 200)}")
            elif kind == "tool_call":
                lines.append(f"  call {p.get('name')} {_short(p.get('arguments') or '', 150)}")
            elif kind == "tool_result":
                body = p.get("error") or p.get("result") or ""
                lines.append(f"  -> {_short(body, 200)}")
            elif p.get("note") or p.get("error"):
                lines.append(f"  ({_short(p.get('note') or p.get('error'), 200)})")
        status = f"{run_id} is {run.status.value}"
        return status + ("\n" + "\n".join(lines) if lines else "; no steps yet")

    async def _cancel(self, run_id: str, reason: str) -> str:
        run = await self.store.get_run(run_id)
        if run is None or run.mission_id != self.mid:
            raise ToolError(f"no run {run_id!r} in this mission")
        if run.status in TERMINAL:
            return f"{run_id} already {run.status.value}"
        assert self.r._supervisor is not None
        task = self.r._supervisor.tasks.get(run_id)
        if task is not None and not task.done():
            task.cancel()
        await self.store.update_run(run_id, status=RunStatus.ABORTED)
        self.seen[run_id] = RunStatus.ABORTED.value
        await self.bus.publish(
            EventRecord(
                mission_id=self.mid,
                run_id=run_id,
                type=EventType.CANCELLED,
                payload={"reason": f"orchestrator: {reason}"[:300]},
            )
        )
        return f"cancelled {run_id}"

    async def _message_owner(self, text: str) -> str:
        if not text.strip():
            raise ToolError("empty message")
        await self._note(text[:1000], to_owner=True)
        if self.mission.chat_id:
            await self.store.append_chat_message(
                ChatMessage(
                    chat_id=self.mission.chat_id,
                    role="note",
                    body={},
                    display=f"{self.mid}: {text[:1000]}",
                )
            )
        return "sent"

    # -- recovery & state ------------------------------------------------------

    async def _recover_runs(self) -> None:
        for run in await self.store.list_runs(self.mid):
            if run.status in TERMINAL:
                self.seen[run.id] = run.status.value
            else:
                await self.r._recover_run(run)

    async def _restart_live_runs(self) -> None:
        assert self.r._supervisor is not None
        for run in await self.store.list_runs(self.mid):
            if run.status not in TERMINAL and run.status != RunStatus.HALTED_ESCALATED:
                await self.r._supervisor.ensure_started(run)

    async def _results_by_role(self) -> dict[str, Run]:
        """Final gates and the PR read the latest completed coder / auditor."""
        out: dict[str, Run] = {}
        for run in await self.store.list_runs(self.mid):
            if run.status != RunStatus.COMPLETED:
                continue
            if run.role == "coder":
                out["code"] = run
            elif run.role == "auditor":
                out["audit"] = run
        return out

    async def _system_prompt(self) -> str:
        mission = await self.store.get_mission(self.mid) or self.mission
        plan = mission.plan or {}
        registry = self.r._registry
        roles = []
        for name in registry.sorted_by_name():
            spec = registry.get(name)
            if name == "echo" and self.playbook != "echo":
                continue
            inputs = sorted(getattr(spec, "context_manifest", set()) or [])
            roles.append(
                f"- {name}: {spec.description} Goal: {spec.goal or '-'} "
                f"Default model: {spec.default_model}. INPUTS: {', '.join(inputs) or 'brief'}"
            )
        catalog = [
            f"- {e.id}: tier {e.tier}, ${e.cost_in_per_mtok}/${e.cost_out_per_mtok} per Mtok"
            + (f", {', '.join(sorted(e.capabilities))}" if e.capabilities else "")
            for e in self._usable()
        ]
        spend = await self.store.mission_spend(self.mid)
        budget = plan.get("budget") or {}
        cap = budget.get("max_usd")
        repo = mission.repo_origin or mission.repo_path or "none (no code changes possible)"
        gates = plan.get("gates") or []
        state = await self._digest()
        return "\n\n".join(
            [
                f"{MARKER} for rework, a multi-agent workflow engine. You run one "
                "mission by delegating work to roles.",
                RULES,
                f"PLAYBOOK ({self.playbook}): {PLAYBOOKS.get(self.playbook, PLAYBOOKS['fix'])}",
                "ROLES:\n" + "\n".join(roles),
                "Automatic wiring: a coder receives the latest investigator handoff; an "
                "auditor receives the latest coder's diff and worktree plus the latest "
                "investigation. Put revision feedback in the brief.",
                "CATALOG:\n" + "\n".join(catalog),
                "MISSION:\n"
                f"- id: {mission.id}\n- objective: {mission.objective}\n- repo: {repo}\n"
                f"- final gates after you finish: {', '.join(gates) or 'none'}\n"
                f"- spend so far: ${spend.spent_usd:.3f}"
                + (f" of a ${float(cap):.2f} budget" if cap else ""),
                "CURRENT STATE (rebuilt from the database every turn):\n" + state,
            ]
        )

    async def _digest(self) -> str:
        runs = await self.store.list_runs(self.mid)
        if not runs:
            lines = ["no roles spawned yet"]
        else:
            lines = []
            for r in runs:
                model = (r.model_ref or {}).get("id") or "default"
                line = f"- {r.id} [{r.status.value}] role={r.role} model={model}"
                if r.status == RunStatus.COMPLETED:
                    line += f" :: {artifact_summary(r.artifact)}"
                elif r.status == RunStatus.HALTED_ESCALATED:
                    line += f" :: escalated {_short(r.escalation or {}, 160)}"
                lines.append(line)
        gates = await self.store.list_gates(self.mid)
        for g in gates:
            if g.status == GateStatus.OPEN:
                lines.append(f"- open gate {g.id} ({g.kind.value}): {_short(g.question, 160)}")
        mission = await self.store.get_mission(self.mid)
        if mission is not None and mission.status != MissionStatus.RUNNING:
            lines.append(f"- mission status: {mission.status.value}")
        return "\n".join(lines)


def _args_line(args: dict[str, Any]) -> str:
    parts = []
    for key, value in args.items():
        text = value if isinstance(value, str) else json.dumps(value, default=str)
        parts.append(f"{key}={text}")
    return ", ".join(parts)
