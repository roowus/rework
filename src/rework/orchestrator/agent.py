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

from rework.kernel.llm.adapters.retry import _is_transient
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
from rework.nodes.report_sections import blocking_findings, open_blockers

log = logging.getLogger("rework.orchestrator")

if TYPE_CHECKING:
    from rework.orchestrator.engine import MissionRunner

MARKER = "You are the MISSION ORCHESTRATOR"
MAX_TURNS = 80  # runaway protection only; the budget is the real rail
TURN_EXTENSION = 40  # per owner approval at the cap
PREFERRED_FAILURE_LIMIT = 3  # failed turns in a row before leaving the chosen model
# waits when EVERY model fails transiently (an outage), about 7.5 minutes in all
OUTAGE_WAITS_S = (15.0, 30.0, 60.0, 120.0, 240.0)
MAX_IDLE_NUDGES = 3
PROVIDER_FAILURE_LIMIT = 2  # failed runs on one model before spawn_role refuses it

STRICTNESS_LEVELS = ("light", "standard", "strict")
DEFAULT_STRICTNESS = "standard"

PLAYBOOKS: dict[str, str] = {
    "echo": (
        "Plumbing check: spawn the `echo` role once with the objective as its brief, "
        "wait for it, then finish."
    ),
    "fix": (
        "Usual shape: an investigator finds out what is going on (cheap model, quick "
        "depth unless the problem is unclear); a coder implements the change in its "
        "own worktree and runs the tests; an auditor reviews the coder's diff. Every "
        "audit gets a `strictness` (light | standard | strict): pick it from RISK, not "
        "habit: change size, blast radius (auth, money, migrations, core control flow "
        "=> high), and what the last round found. Re-decide it EVERY round from the "
        "latest report: a first round that turned up real problems earns a stricter "
        "second round. Then LOOP: while the audit requests changes or reports a "
        "medium-or-higher finding that you are not willing to ship with, spawn the "
        "coder again with those findings in its brief (it continues on the previous "
        "coder's branch automatically) and audit again. Severity is the auditor's "
        "call, not a mechanical trigger: low notes alone do not need a revision, and "
        "shipping with a medium finding still open is YOUR judgment to make and to "
        "record in finish(accepted_findings=[...]) with a reason. If three rounds have "
        "not converged, ask_owner whether to continue. Skip the investigator when the "
        "change is small and obvious. After you finish, the owner decides whether to ship."
    ),
    "fix-pr": (
        "Same shape and same revise-until-clean loop as fix: investigate (optional for "
        "small changes), code, audit, revise while the audit requests changes or has "
        "medium-or-higher findings you are not willing to ship (ask_owner after three "
        "rounds). Pick each audit's `strictness` from risk (size, blast radius, prior "
        "findings) and re-decide it every round from the latest report, never once for "
        "the whole mission. After you finish, the ENGINE pushes the last coder's branch "
        "and opens the pull request when the owner approves the gate; agents never merge."
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
  straightforward coding, stronger tiers for audits and hard problems. If a role
  failed on provider errors (timeouts, 5xx, rate limits), the model's endpoint is
  struggling: respawn it on a different model rather than the same one.
- Write every brief so the role can act without asking: what to do, why, what
  done looks like, and anything earlier roles found that it needs.
- Scope briefs to the objective. Pass findings along as context, but do not turn
  speculative side concerns into extra work items unless they block the goal: a
  one-line change deserves a one-line-sized mission.
- Read handoffs (`read_artifact`) before deciding what comes next.
- Keep the owner informed with `message_owner` at meaningful moments only.
- Roles may ask you questions (`ask_orchestrator`); answer with `answer_escalation`
  when you can, or forward to the owner with `ask_owner(run_id=...)` when only they
  can decide. Never leave a role waiting.
- The owner may message you mid-mission; treat it as the latest word on intent.
- Steer a role that is drifting with `redirect` instead of cancelling it.
- Money: when the mission will need more than its budget, call request_budget
  (approval raises the cap); never ask about money with ask_owner.
- Cost: give every role a turn budget (`max_turns`) sized to the work, prefer quick
  investigations, and keep briefs tight. But money is the owner's call: never skip
  a needed revision or audit to save budget. The budget gate asks the owner before
  the cap is passed.
- Call `finish` when the mission's goal is met, or when it cannot be met (say why).
  Final gates (ship / open a PR) happen after you finish; you do not open them.
  The engine pushes the last coder's branch and opens the PR itself on approval:
  never spawn a role to push, commit, or open PRs (each coder run also creates a
  new branch, so a "push" coder only adds noise).
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
                "max_turns": {
                    "type": "integer",
                    "description": "Tool-round budget for this role (cost control). "
                    "Defaults: investigator 20 (deep 45), coder 40, auditor 25.",
                },
                "fresh": {
                    "type": "boolean",
                    "description": "Coders only: start from the original base instead of "
                    "continuing the previous coder's branch.",
                },
                "depth": {
                    "type": "string",
                    "enum": ["quick", "deep"],
                    "description": "Investigators only: quick (default) answers the brief and "
                    "stops; deep chases hypotheses to ground truth (costs more).",
                },
                "strictness": {
                    "type": "string",
                    "enum": list(STRICTNESS_LEVELS),
                    "description": "Auditors only: how hard the review looks. light = does it"
                    " do what the brief says, do tests and CI pass. standard (default) = light"
                    " + edge cases, scope, regressions. strict = standard + adversarial: hunt"
                    " failure modes, run the feature as the owner would, verify every claim in"
                    " the coder's summary. Pick it from risk and re-decide it EVERY round:"
                    " a fix that touched auth or core control flow after a round of findings"
                    " deserves strict even if round one was standard.",
                },
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
        name="answer_escalation",
        description="Answer a role that asked you something (see its question in wait/state).",
        input_schema={
            "type": "object",
            "properties": {"run_id": {"type": "string"}, "answer": {"type": "string"}},
            "required": ["run_id", "answer"],
        },
    ),
    ToolSpec(
        name="ask_owner",
        description=(
            "Ask the owner and wait for the answer. Use for decisions only they can make "
            "(intent, scope, risk, access). Pass run_id when a role's question is being "
            "forwarded: the role then receives the owner's answer directly."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "kind": {
                    "type": "string",
                    "enum": ["question", "choice", "permission", "task"],
                    "description": "question = free text; choice = pick one of options; "
                    "permission = allow/deny; task = the owner does something by hand.",
                },
                "options": {"type": "array", "items": {"type": "string"}},
                "run_id": {"type": "string"},
            },
            "required": ["question"],
        },
    ),
    ToolSpec(
        name="request_budget",
        description=(
            "Ask the owner to raise this mission's budget cap before it runs out. On "
            "approval the cap is raised (use this, never ask_owner, for money)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "max_usd": {"type": "number", "description": "The new cap you need."},
                "reason": {"type": "string", "description": "What is left and why."},
            },
            "required": ["max_usd", "reason"],
        },
    ),
    ToolSpec(
        name="redirect",
        description="Steer a running role: the instruction reaches it at its next turn.",
        input_schema={
            "type": "object",
            "properties": {"run_id": {"type": "string"}, "instruction": {"type": "string"}},
            "required": ["run_id", "instruction"],
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
        description=(
            "End the mission. outcome done = goal met; failed = it cannot be met. "
            "Refused when no auditor has run, or a coder ran after the last audit: "
            "ship only what a fresh audit has seen. When the latest audit still has "
            "open medium-or-higher findings or blockers, `done` needs accepted_findings "
            "with one reasoned entry per open finding."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "outcome": {"type": "string", "enum": ["done", "failed"]},
                "summary": {
                    "type": "string",
                    "description": "What happened, for the owner: what changed, why, and what "
                    "the audit found. This leads the PR body, so write it for a reviewer.",
                },
                "accepted_findings": {
                    "type": "array",
                    "description": "Judgment, not a bypass: for every open medium-or-higher "
                    "finding or blocker in the latest audit that you are shipping anyway, "
                    "say which one and why it is acceptable. Missing coverage is refused.",
                    "items": {
                        "type": "object",
                        "required": ["finding", "reason"],
                        "properties": {
                            "finding": {
                                "type": "string",
                                "description": "The finding's title or id, as the audit wrote it.",
                            },
                            "reason": {
                                "type": "string",
                                "description": "Why shipping with this open is the right call.",
                            },
                        },
                    },
                },
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
        self.questions_seen: set[str] = set()  # escalation ids already reported
        self._preferred_failures = 0  # consecutive turns the chosen model failed
        self.owner_seq = 0  # last owner message delivered
        # every event of the mission, read forward with a seq cursor (found
        # live: events_since(0, limit=N) returns the OLDEST N events, so on a
        # 9000-event mission owner messages and check-ins were invisible)
        self._events: list[Any] = []
        self._event_cursor = 0
        self.finished: tuple[str, str] | None = None
        # findings the orchestrator shipped with, and its reason for each: the
        # one mechanical rail that blocks finish(done) requires them (see
        # _enforce_findings_accepted), and they lead the mission report/PR body
        self.accepted_findings: list[dict[str, str]] = []
        self._providers: dict[str, Any] = {}

    def _usable(self) -> list[Any]:
        """Catalog entries this daemon can actually call (real ones first)."""
        entries = self.r._catalog.all()
        real = [e for e in entries if e.provider != "fake" and e.has_credentials()]
        return real or [e for e in entries if e.has_credentials()] or entries

    def _strongest(self) -> Any:
        entries = self._usable()
        return max(entries, key=lambda e: (e.tier, -e.cost_in_per_mtok))

    # -- provider ------------------------------------------------------------

    def provider(self, entry: Any = None) -> Any:
        """One provider per catalog entry. Timeouts are NOT retried on the same
        model: _think answers that turn with the next model instead."""
        entry = entry or self.entry
        if self.r._provider is not None:
            return self.r._provider  # tests inject a scripted one
        if entry.id not in self._providers:
            from rework.kernel.llm.adapters import build_provider
            from rework.kernel.llm.adapters.retry import with_retries

            self._providers[entry.id] = with_retries(build_provider(entry), retry_timeouts=False)
        return self._providers[entry.id]

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
        self.owner_seq = max(
            [int(m.get("owner_seq") or 0) for m in self.history if m.get("role") == "user"] + [0]
        )

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
            self.finished = self._recorded_finish()
            if self.finished is None:
                await self._finish_interrupted_turn()
        if self.finished is None:
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
        await self.r._synthesize(
            self.mid,
            ship,
            outcome=outcome,
            summary=summary,
            accepted_findings=self.accepted_findings,
        )

    async def _loop(self) -> None:
        idle = 0
        turns = sum(1 for m in self.history if m.get("role") == "assistant")
        while self.finished is None:
            signal = await self._owner_gate()
            if signal == "cancelled":
                await self.r._synthesize(self.mid, None)
                return
            if turns >= await self._turn_cap():
                # ask, don't fail: found live, the cap killed a large mission one
                # step before its PR, with every role's work committed
                if not await self._ask_more_turns(turns):
                    await self._note(f"turn cap reached at {turns}; owner declined more")
                    self.finished = ("failed", f"orchestrator hit its turn cap ({turns} turns)")
                    return
                continue
            turns += 1
            await self._deliver_owner_messages()
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
                        "content": "Respond with tool calls only (spawn_role, wait, "
                        "read_artifact, check_in, redirect, answer_escalation, ask_owner, "
                        "cancel_run, message_owner or finish).",
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
        # the system prompt is STATIC for the whole mission (prompt caching
        # works on exact prefixes); the live state rides in a trailing message
        # that is never persisted
        state = f"CURRENT STATE (rebuilt from the database for this turn):\n{await self._state()}"
        self._compact_history()
        messages = [*self.history, {"role": "user", "content": state}]
        system = await self._system_prompt()
        entry = self.entry
        tried: set[str] = set()
        outage = list(OUTAGE_WAITS_S)
        while True:
            request = ChatRequest(
                model=entry.model,
                system=system,
                messages=messages,
                tools=TOOLS,
                max_tokens=min(int(entry.max_output or 4096), 4096),
                effort=self.effort,
                session_id=self.run_id,
            )
            try:
                result = await self.provider(entry).complete(request)
                break
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
                # never fail the mission while another model can answer. Found
                # live: kimi-k3 stalls on ~6% of calls and a stalled request
                # stalls again when re-sent; the mission (work done, one step
                # from its PR) was failed while flash was healthy
                tried.add(entry.id)
                preferred = entry.id == self.entry.id
                if preferred:
                    self._preferred_failures += 1
                fallback = next(
                    (
                        e
                        for e in sorted(self._usable(), key=lambda e: (-e.tier, e.cost_in_per_mtok))
                        if e.id not in tried
                    ),
                    None,
                )
                if fallback is None and outage and _is_transient(exc):
                    # every model failed on a transient error: the network or
                    # the provider is down, not the mission. Found live: a
                    # connection drop failed a mission mid-way (all catalog
                    # models share one provider). Wait, then start over.
                    pause = outage.pop(0)
                    await self._note(
                        f"every model failed ({type(exc).__name__}); provider or network "
                        f"down? retrying in {pause:.0f}s"
                    )
                    await asyncio.sleep(pause)
                    tried.clear()
                    entry = self.entry
                    continue
                if fallback is None:
                    self.finished = ("failed", f"orchestrator model call failed: {exc}"[:300])
                    return None
                if preferred and self._preferred_failures >= PREFERRED_FAILURE_LIMIT:
                    await self._note(
                        f"orchestrator model {entry.id} failed {self._preferred_failures} "
                        f"turns in a row; switching to {fallback.id} for the rest"
                    )
                    self.entry = fallback
                    self._preferred_failures = 0
                else:
                    await self._note(
                        f"orchestrator model {entry.id} failed ({type(exc).__name__}); "
                        f"{fallback.id} answers this turn"
                    )
                entry = fallback
        if entry.id == self.entry.id:
            self._preferred_failures = 0
        usage = result.usage
        await self.store.charge(
            Charge(
                mission_id=self.mid,
                run_id=self.run_id,
                model=entry.model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cached_tokens=usage.cached_tokens,
                cost_usd=entry.estimate_cost(
                    usage.input_tokens, usage.output_tokens, usage.cached_tokens
                ),
            )
        )
        message: dict[str, Any] = {"role": "assistant", "content": result.text or ""}
        if result.thinking_blocks:
            message["thinking_blocks"] = result.thinking_blocks
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

    def _recorded_finish(self) -> tuple[str, str] | None:
        """A `finish` already executed before a restart (its tool result is in
        the history). Found live: a daemon restart while the mission waited at
        its final gate re-entered the loop and spent a model call re-deciding
        what it had already decided."""
        answered = {
            m.get("tool_call_id"): m.get("content") for m in self.history if m.get("role") == "tool"
        }
        for message in reversed(self.history):
            for call in message.get("tool_calls") or []:
                if call.get("name") == "finish" and answered.get(call.get("id")) == "finished":
                    args = call.get("arguments") or {}
                    self.accepted_findings = _accepted_findings(args.get("accepted_findings"))
                    return (str(args.get("outcome") or "done"), str(args.get("summary") or ""))
        return None

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
        if name == "answer_escalation":
            return await self._answer(str(args.get("run_id") or ""), str(args.get("answer") or ""))
        if name == "ask_owner":
            return await self._ask_owner(call_id, args)
        if name == "request_budget":
            return await self._request_budget(call_id, args)
        if name == "redirect":
            return await self._redirect(
                str(args.get("run_id") or ""), str(args.get("instruction") or "")
            )
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
            accepted: list[dict[str, str]] = []
            if outcome == "done":
                accepted = _accepted_findings(args.get("accepted_findings"))
                await self._enforce_audit_fresh()
                await self._enforce_findings_accepted(accepted)
            self.finished = (outcome, str(args.get("summary") or ""))
            self.accepted_findings = accepted
            await self._note(f"finished ({outcome}): {self.finished[1]}"[:400], to_owner=True)
            return "finished"
        raise ToolError(f"unknown tool {name!r}")

    async def _completed_runs(self, role: str) -> list[Run]:
        return [
            r
            for r in await self.store.list_runs(self.mid)
            if r.role == role and r.status == RunStatus.COMPLETED
        ]

    async def _enforce_audit_fresh(self) -> None:
        """The ONE mechanical rail on finish(done), not bypassable by anything
        the orchestrator can call: what ships must have been audited. Refuses
        when no auditor ever completed, or when a coder completed after the
        last completed audit (unaudited changes). The only way through is to
        spawn a fresh audit and wait for it."""
        audits = await self._completed_runs("auditor")
        if not audits:
            raise ToolError(
                "no auditor has completed in this mission: spawn an auditor on the coder's "
                "branch and wait for it before finishing with outcome='done'"
            )
        last_audit = audits[-1]
        coders = await self._completed_runs("coder")
        after = [r for r in coders if r.created_at > last_audit.created_at]
        if after:
            raise ToolError(
                f"coder {after[-1].id} ran after the last completed audit {last_audit.id}: "
                "those changes are unaudited. Spawn an auditor on the current branch and "
                "wait for it, then finish."
            )

    async def _enforce_findings_accepted(self, accepted: list[dict[str, str]]) -> None:
        """finish(done) with open medium-or-higher findings or blockers is the
        orchestrator's JUDGMENT, not a mechanical block: it is allowed only when
        every such open item is named in accepted_findings with a reason. This
        replaces the old fixed 'medium-or-higher always blocks' rule."""
        audits = await self._completed_runs("auditor")
        if not audits:
            return  # _enforce_audit_fresh already refused this
        artifact = audits[-1].artifact or {}
        open_items = [str(f.get("title") or "") for f in blocking_findings(artifact)]
        open_items += [str(b.get("question") or "") for b in open_blockers(artifact)]
        open_items = [item for item in open_items if item]
        if not open_items:
            return
        covered = {str(a.get("finding") or "").strip().lower() for a in accepted}
        missing = [
            item
            for item in open_items
            if not any(_covers(covered_key, item) for covered_key in covered)
        ]
        if missing:
            raise ToolError(
                "the latest audit still has open medium-or-higher findings/blockers that "
                "accepted_findings does not cover: "
                + "; ".join(missing)
                + ". Either spawn the coder again with these in its brief and re-audit, or "
                "call finish again with accepted_findings listing each one and why you are "
                "shipping with it open."
            )

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
            failures = await self._provider_failures(model, runs)
            if failures >= PROVIDER_FAILURE_LIMIT:
                # mechanical rail (found live: a prompt rule alone did not stop
                # the orchestrator re-picking a model whose endpoint kept failing)
                others = ", ".join(e.id for e in self._usable() if e.id != model)
                raise ToolError(
                    f"model {model!r} already failed {failures} runs in this mission on "
                    f"provider errors (timeouts/5xx/rate limits); choose another: {others}"
                )
        else:
            model = (self.mission.plan or {}).get("model") or None
        context = await self._wire(role, runs)
        for key, ref in (args.get("inputs") or {}).items():
            context[str(key)] = await self._resolve_ref(str(ref), runs)
        context["brief"] = brief
        if args.get("depth") in ("quick", "deep"):
            context["depth"] = args["depth"]
        context["max_turns"] = _turn_budget(role, args)
        if role == "auditor":
            # the strictness is the orchestrator's per-round judgment (light /
            # standard / strict); it reaches the auditor's prompt as a mode
            level = str(args.get("strictness") or DEFAULT_STRICTNESS).strip().lower()
            if level not in STRICTNESS_LEVELS:
                raise ToolError(
                    f"strictness must be one of {', '.join(STRICTNESS_LEVELS)}; got {level!r}"
                )
            context["strictness"] = level
        if role == "coder" and not args.get("fresh"):
            # a revision continues the last finished coder's branch; the diff
            # base stays the original, so the auditor still sees the whole change
            done = [
                r
                for r in runs
                if r.role == "coder"
                and r.status == RunStatus.COMPLETED
                and (r.artifact or {}).get("branch")
            ]
            if done:
                prev = done[-1].artifact or {}
                context["continue_branch"] = prev["branch"]
                if prev.get("base_sha"):
                    context["base_sha"] = prev["base_sha"]
                if prev.get("summary"):
                    # what the last attempt learned, so this one does not
                    # re-read the same files (dogfood #2)
                    context["previous_attempt"] = str(prev["summary"])[:6000]
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

    async def _all_events(self) -> list[Any]:
        """All mission events, oldest first; only new ones are fetched."""
        while True:
            batch = await self.store.events_since(
                self._event_cursor, mission_id=self.mid, limit=500
            )
            if not batch:
                return self._events
            self._events.extend(batch)
            self._event_cursor = max(int(e.seq or 0) for e in batch)
            if len(batch) < 500:
                return self._events

    async def _provider_failures(self, model: str, runs: list[Run]) -> int:
        """Failed runs on `model` whose error looks like the provider's fault."""
        failed = [
            r
            for r in runs
            if r.status == RunStatus.FAILED and (r.model_ref or {}).get("id") == model
        ]
        if not failed:
            return 0
        events = await self._all_events()
        errors: dict[str, str] = {}
        for ev in events:
            if ev.run_id and (ev.payload or {}).get("error"):
                errors[ev.run_id] = str(ev.payload["error"]).lower()
        markers = ("timeout", "timed out", "500", "502", "503", "504", "429", "connection")
        return sum(1 for r in failed if any(m in errors.get(r.id, "") for m in markers))

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
            if await self._pending_owner_messages():
                return "the owner sent you a message (it is in the conversation now)"
            asking = [r for r in runs if _open_question(r) and r.status not in TERMINAL]
            fresh_q = [
                r for r in asking if (r.escalation or {}).get("id") not in self.questions_seen
            ]
            if fresh_q:
                lines = []
                for r in fresh_q:
                    esc = r.escalation or {}
                    self.questions_seen.add(str(esc.get("id")))
                    lines.append(
                        f"{r.id} ({r.role}) asks [{esc.get('kind', 'question')}]: "
                        f"{esc.get('question')}\nIt is blocked until you answer_escalation or "
                        "ask_owner(run_id=...)."
                    )
                return "\n".join(lines)
            others = [r for r in runs if r.status not in TERMINAL and not _open_question(r)]
            if asking and not others:
                return "still waiting on you: " + ", ".join(
                    f"{r.id} asked {_short((r.escalation or {}).get('question') or '', 120)!r}"
                    for r in asking
                )
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
        events = await self._all_events()
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
        events = [e for e in await self._all_events() if e.run_id == run_id]
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

    async def _answer(self, run_id: str, answer: str) -> str:
        run = await self.store.get_run(run_id)
        if run is None or run.mission_id != self.mid:
            raise ToolError(f"no run {run_id!r} in this mission")
        if not _open_question(run):
            raise ToolError(f"{run_id} has no open question")
        if not answer.strip():
            raise ToolError("empty answer")
        await self.store.update_run(run_id, escalation={**(run.escalation or {}), "answer": answer})
        return f"answered {run_id}"

    async def _ask_owner(self, call_id: str, args: dict[str, Any]) -> str:
        from rework.kernel.state.records import GateKind
        from rework.nodes.escalate import answer_text

        question = str(args.get("question") or "").strip()
        if not question:
            raise ToolError("a question is required")
        kind_name = str(args.get("kind") or "question")
        try:
            kind = GateKind(kind_name)
        except ValueError as exc:
            raise ToolError(f"unknown kind {kind_name!r}") from exc
        options = [str(o) for o in (args.get("options") or []) if str(o).strip()]
        if kind == GateKind.CHOICE and len(options) < 2:
            raise ToolError("a choice needs at least two options")
        run_id = str(args.get("run_id") or "") or None
        if run_id is not None:
            run = await self.store.get_run(run_id)
            if run is None or run.mission_id != self.mid:
                raise ToolError(f"no run {run_id!r} in this mission")
        gate = next(
            (
                g
                for g in await self.store.list_gates(self.mid)
                if (g.spec or {}).get("_call") == call_id
            ),
            None,
        )  # re-executed after a restart: reuse, never duplicate
        if gate is None:
            spec: dict[str, Any] = {"_call": call_id, "asked_by": "orchestrator"}
            if options:
                spec["options"] = options
            gate = await self.r._gates.request(
                self.mid, kind=kind, question=question, for_run=run_id, spec=spec
            )
        await self.store.update_mission(self.mid, status=MissionStatus.AWAITING_GATE)
        while True:
            row = await self.store.get_gate(gate.id)
            if row is not None and row.status != GateStatus.OPEN:
                break
            mission = await self.store.get_mission(self.mid)
            if mission is not None and mission.status == MissionStatus.CANCELLED:
                return "the owner cancelled the mission"
            await asyncio.sleep(self.r._poll)
        await self.store.update_mission(self.mid, status=MissionStatus.RUNNING)
        text = answer_text(row)
        if run_id is not None:
            run = await self.store.get_run(run_id)
            if run is not None and _open_question(run):
                await self.store.update_run(
                    run_id, escalation={**(run.escalation or {}), "answer": f"owner: {text}"}
                )
                return f"owner: {text} (passed to {run_id})"
        return f"owner: {text}"

    def _compact_history(self) -> int:
        """Elide old tool results in the in-memory history, the roles' rule
        (toolloop._compact: past 160k chars, all but the last 12, in one step
        so the cached prefix stays stable). The stored chat keeps everything.
        Found live: an 80-turn orchestrator sent 335k chars per turn; with a
        cold prompt cache kimi-k3 timed out on every call."""
        from rework.nodes.toolloop import _compact

        elided = _compact(self.history)
        if elided:
            log.info("orchestrator history compacted: %d old tool results elided", elided)
        return elided

    async def _turn_cap(self) -> int:
        """MAX_TURNS plus TURN_EXTENSION per approved extension (gate rows, so
        the allowance survives a restart)."""
        granted = sum(
            1
            for g in await self.store.list_gates(self.mid)
            if (g.spec or {}).get("turn_cap") and g.status == GateStatus.APPROVED
        )
        return MAX_TURNS + TURN_EXTENSION * granted

    async def _ask_more_turns(self, turns: int) -> bool:
        from rework.kernel.state.records import GateKind

        gate = next(
            (
                g
                for g in await self.store.list_gates(self.mid)
                if (g.spec or {}).get("turn_cap") and g.status == GateStatus.OPEN
            ),
            None,
        )
        if gate is None:
            spend = await self.store.mission_spend(self.mid)
            gate = await self.r._gates.request(
                self.mid,
                kind=GateKind.PERMISSION,
                question=(
                    f"The orchestrator has taken {turns} turns (its runaway cap; "
                    f"${spend.spent_usd:.2f} spent). Allow {TURN_EXTENSION} more?"
                ),
                spec={"turn_cap": True, "asked_by": "engine"},
            )
        await self.store.update_mission(self.mid, status=MissionStatus.AWAITING_GATE)
        while True:
            row = await self.store.get_gate(gate.id)
            if row is not None and row.status != GateStatus.OPEN:
                break
            await asyncio.sleep(self.r._poll)
        await self.store.update_mission(self.mid, status=MissionStatus.RUNNING)
        return row.status == GateStatus.APPROVED

    async def _request_budget(self, call_id: str, args: dict[str, Any]) -> str:
        """One question about money, and the answer moves the real cap. Found
        live: a permission gate approved 'up to $5' while the engine's burn gate
        kept the old $2 cap and would have asked the owner again."""
        from rework.kernel.state.records import GateKind

        try:
            wanted = float(args.get("max_usd") or 0)
        except (TypeError, ValueError) as exc:
            raise ToolError("max_usd must be a number") from exc
        mission = await self.store.get_mission(self.mid) or self.mission
        plan = dict(mission.plan or {})
        budget = dict(plan.get("budget") or {})
        cap = float(budget.get("max_usd") or 0)
        if wanted <= cap:
            raise ToolError(f"the cap is already ${cap:.2f}")
        spend = await self.store.mission_spend(self.mid)
        question = (
            f"Raise this mission's budget from ${cap:.2f} to ${wanted:.2f}? "
            f"(${spend.spent_usd:.2f} spent) {str(args.get('reason') or '').strip()}"
        )
        gate = next(
            (
                g
                for g in await self.store.list_gates(self.mid)
                if (g.spec or {}).get("_call") == call_id
            ),
            None,
        )
        if gate is None:
            gate = await self.r._gates.request(
                self.mid,
                kind=GateKind.PERMISSION,
                question=question,
                spec={"_call": call_id, "asked_by": "orchestrator", "budget_to": wanted},
            )
        await self.store.update_mission(self.mid, status=MissionStatus.AWAITING_GATE)
        while True:
            row = await self.store.get_gate(gate.id)
            if row is not None and row.status != GateStatus.OPEN:
                break
            await asyncio.sleep(self.r._poll)
        await self.store.update_mission(self.mid, status=MissionStatus.RUNNING)
        if row.status != GateStatus.APPROVED:
            return (
                f"owner declined the budget raise (cap stays ${cap:.2f}): {row.decision_note or ''}"
            )
        budget["max_usd"] = wanted
        plan["budget"] = budget
        await self.store.update_mission(self.mid, plan=plan)
        if isinstance(self.r._plan, dict):  # the burn gate reads this copy
            self.r._plan["budget"] = budget
        await self._note(f"budget raised to ${wanted:.2f} by the owner")
        return f"budget raised to ${wanted:.2f}"

    async def _redirect(self, run_id: str, instruction: str) -> str:
        run = await self.store.get_run(run_id)
        if run is None or run.mission_id != self.mid:
            raise ToolError(f"no run {run_id!r} in this mission")
        if run.status in TERMINAL:
            raise ToolError(f"{run_id} already {run.status.value}")
        if not instruction.strip():
            raise ToolError("empty instruction")
        await self.store.update_run(run_id, redirect=instruction[:2000])
        return f"{run_id} will see it at its next turn"

    async def _owner_messages(self) -> list[Any]:
        return [
            e
            for e in await self._all_events()
            if e.type == EventType.OWNER_MESSAGE and int(e.seq or 0) > self.owner_seq
        ]

    async def _pending_owner_messages(self) -> bool:
        return bool(await self._owner_messages())

    async def _deliver_owner_messages(self) -> None:
        for ev in await self._owner_messages():
            text = str((ev.payload or {}).get("text") or "")
            seq = int(ev.seq or 0)
            self.owner_seq = max(self.owner_seq, seq)
            await self._append(
                {"role": "user", "content": f"Message from the owner: {text}", "owner_seq": seq},
                display=text,
            )
            log.info("owner message delivered", extra={"tool": "owner_message"})

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
        budget = plan.get("budget") or {}
        cap = budget.get("max_usd")
        repo = mission.repo_origin or mission.repo_path or "none (no code changes possible)"
        gates = plan.get("gates") or []
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
                + (
                    f"- budget: ${float(cap):.2f} (the owner is asked before it is exceeded)"
                    if cap
                    else "- budget: none"
                ),
                "The CURRENT STATE message at the end of the conversation is rebuilt from "
                "the database every turn; trust it over your memory.",
            ]
        )

    async def _state(self) -> str:
        spend = await self.store.mission_spend(self.mid)
        return f"- spend so far: ${spend.spent_usd:.3f}\n" + await self._digest()

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
                elif _open_question(r):
                    q = (r.escalation or {}).get("question") or ""
                    line += f" :: ASKS YOU: {_short(q, 200)}"
                lines.append(line)
        gates = await self.store.list_gates(self.mid)
        for g in gates:
            if g.status == GateStatus.OPEN:
                lines.append(f"- open gate {g.id} ({g.kind.value}): {_short(g.question, 160)}")
        mission = await self.store.get_mission(self.mid)
        if mission is not None and mission.status != MissionStatus.RUNNING:
            lines.append(f"- mission status: {mission.status.value}")
        return "\n".join(lines)


DEFAULT_TURNS = {"investigator": 20, "coder": 40, "auditor": 25}


def _accepted_findings(raw: Any) -> list[dict[str, str]]:
    """Sanitize the finish tool's accepted_findings: entries without both a
    finding reference and a reason are the model filling the field in to get
    past the rail, so they are dropped (and then refused as uncovered)."""
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        finding = str(item.get("finding") or item.get("title") or item.get("id") or "").strip()
        reason = str(item.get("reason") or "").strip()
        if finding and reason:
            out.append({"finding": finding, "reason": reason})
    return out


def _covers(accepted_key: str, open_item: str) -> bool:
    """Does an accepted_findings entry name this open finding/blocker? Exact
    match first, then a containment match either way so a full title or a
    distinctive fragment both count (the model quotes titles imperfectly)."""
    if not accepted_key:
        return False
    item = open_item.strip().lower()
    if accepted_key == item:
        return True
    return len(accepted_key) >= 8 and (accepted_key in item or item in accepted_key)


def _turn_budget(role: str, args: dict[str, Any]) -> int:
    asked = args.get("max_turns")
    if isinstance(asked, int | float) and asked > 0:
        return max(3, min(int(asked), 64))
    if role == "investigator" and args.get("depth") == "deep":
        return 45
    return DEFAULT_TURNS.get(role, 40)


def _open_question(run: Run) -> bool:
    """A role's ask_orchestrator call that has not been answered yet."""
    esc = run.escalation or {}
    return bool(esc.get("id")) and esc.get("answer") is None and bool(esc.get("question"))


def _args_line(args: dict[str, Any]) -> str:
    parts = []
    for key, value in args.items():
        text = value if isinstance(value, str) else json.dumps(value, default=str)
        parts.append(f"{key}={text}")
    return ", ".join(parts)
