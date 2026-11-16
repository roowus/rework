"""The orchestrator CONVERSATION: how missions are created.

Owner principle (2026-11-05): 'rework works by u talking to an orchestrator and
the orchestrator decides' — mission creation is a chat, not a form with selects.
The chat reads your intent, works out the parts the machine already knows (which
project, which plan, which repo), asks only when something genuinely matters,
then PROPOSES a mission and waits for your yes.

Design notes:
- The model talks through TOOLS (propose_mission / ask), never free prose that
  we then have to parse. Tool arguments are already structured, so a proposal
  cannot drift from what we act on.
- A proposal is NOT a mission. It is presented back to you; only `launch()`
  after your consent creates rows. Nothing spends money by accident.
- Chat turns are billed to the mission ledger only once a mission exists; until
  then they use an in-memory session. The cost of conversation is visible in
  the chat itself (tokens noted per turn).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any

from rework.kernel.llm.catalog import ModelCatalog, ModelProvider
from rework.kernel.llm.types import ChatRequest, ToolSpec

logger = logging.getLogger("rework.chat")

# Reasoning headroom (the mission orchestrator's approach, ecbf960): thinking
# shares the output budget, so with an effort set it is never below the floor,
# and a reply cut off at max_tokens with no text and no tool call is retried
# once with a bigger budget, then once more at a lower effort.
EFFORT_FLOOR_TOKENS = 8_192
MAX_CUT_OFFS = 2
_LOWER_EFFORT = {"high": "medium", "medium": "low", "low": "low"}

SYSTEM_PROMPT = """You are rework's orchestrator. You turn the owner's intent into a mission.

Ground rules:
- Talk like a colleague: short, concrete, no filler, no bullet-point recaps.
- You almost always know enough to PROPOSE. Do not interrogate. Ask at most one
  question, and only when a wrong guess would waste real money (usually which
  repo/project when several could fit).
- The owner's words are the objective. Do not rewrite them into corporate
  speak; keep their phrasing.
- When you are ready, call propose_mission with the objective, the plan, and
  the project/repo you intend to use. Never claim a mission exists — only the
  owner's confirmation creates one.
- When the owner's request contains clearly separate pieces of work (different
  areas, different repos or different projects, things that could ship
  independently), call propose_mission once per piece and add one line saying
  why they are separate. Each one becomes its own mission, so they launch (and
  fail) independently.
- Keep ONE mission when the work is one coherent change, even if it touches
  several files. Never split just to split.
- If the owner changes their mind mid-conversation, update the proposal: a turn
  that proposes replaces the pending proposals with what you propose now.

Plans you can choose:
- echo    — no roles, just prove the plumbing works
- fix     — investigate, code, audit, then a ship gate
- fix-pr  — same, then the engine opens a pull request (no gate by default)
- draft   — use draft for document work: research write-ups, reports, letters,
            content — anything whose deliverable is writing, not code. The roles
            research with the web tools, write the report in the mission's Report
            tab, and audit every claim against its cited source. No branch push
            and no PR. Choose draft when the objective only reads and reports (an
            audit, a review, an analysis): put draft in propose_mission's plan
            yourself. A plan the owner names explicitly stays authoritative.

A project may also be repo-less: its transport is "upload", which means rework
keeps that project's files itself. For an upload project, fix and fix-pr do NOT
need a repo — the project's files area supplies the files, and results are
fetched, not pushed. Pass no repo for such a project, and never tell the owner
one is required.

Every mission starts from the repo's default branch: you cannot hand it an
earlier mission's branch, results or worktree, so never promise that. Describe
only what the plans above actually do. If earlier work matters, put what it
found into the objective.

Library guidance about creating missions (which project, plan or model to use,
how to label them) is for you: apply it through propose_mission's fields, do
not copy it into the objective. The roles cannot act on it (found live: "Also
label the mission with its project." in an objective sent the orchestrator off
asking the owner what it meant). Only lessons about doing the work itself
belong in the objective.

For a project backed by a git repo, prefer fix-pr: the owner reviews the change
as a pull request, which the engine opens after a clean audited finish. Use fix
when they only want the result held for inspection.

Available context is provided in the conversation as system notes. Use it.

You can also LOOK before you leap: list_projects, list_missions, get_mission and
library_search read the owner's real state. Call them when the answer depends on
what already exists (which missions ran, what a project's repos are, whether a
skill already covers this). Do not narrate the lookups; act on them. You cannot
change anything from here — only a confirmed proposal creates a mission."""

PROPOSE_TOOL = ToolSpec(
    name="propose_mission",
    description=(
        "Propose the missions you would launch. The owner confirms or changes "
        "them. Call this whenever you have a concrete objective; call it once per "
        "separate piece of work. Do not narrate proposals in prose."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "objective": {
                "type": "string",
                "description": "What should happen, in the owner's words.",
            },
            "title": {
                "type": "string",
                "description": "A short name for lists, 3 to 6 words.",
            },
            "plan": {"type": "string", "enum": ["echo", "fix", "fix-pr", "draft"]},
            "project": {"type": "string", "description": "Project name, or empty for none."},
            "repo": {
                "type": "string",
                "description": ("Git URL, local folder path, or empty to use the project's repo."),
            },
            "model": {"type": "string", "description": "Optional catalog id to run the roles on."},
            "effort": {"type": "string", "enum": ["low", "medium", "high"]},
            "budget_usd": {
                "type": "number",
                "description": (
                    "Mission budget in USD, sized to the task: docs/small fix $1-2, "
                    "normal feature $3-5, large or risky $8+"
                ),
            },
            "budget_reason": {
                "type": "string",
                "description": "One line: why this budget, e.g. 'normal feature-sized change'",
            },
        },
        "required": ["objective", "plan"],
    },
)

ASK_TOOL = ToolSpec(
    name="ask",
    description=(
        "Ask the owner one short question when you genuinely cannot propose yet. "
        "Prefer proposing with your best guess and letting them correct it."
    ),
    input_schema={
        "type": "object",
        "properties": {"question": {"type": "string"}},
        "required": ["question"],
    },
)


# Read-only tools for the conversation. The orchestrator may LOOK at the
# owner's world before proposing; it may never change anything from here —
# mutations stay behind the proposal-then-consent path. Keeping the set
# read-only is what lets these run without a gate.
LOOK_TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="list_projects",
        description="List the owner's projects with their repo references.",
        input_schema={"type": "object", "properties": {}},
    ),
    ToolSpec(
        name="list_missions",
        description=(
            "Recent missions (newest first): id, status, objective, project. "
            "Use this to answer 'what have I been running' or to find a mission "
            "the owner is referring to."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 10},
                "status": {
                    "type": "string",
                    "description": (
                        "filter: pending|running|awaiting_gate|on_hold|done|failed|cancelled"
                    ),
                },
            },
        },
    ),
    ToolSpec(
        name="get_mission",
        description=(
            "Full detail for one mission: status, plan steps and roles, spend, "
            "and every run with its status and what it produced."
        ),
        input_schema={
            "type": "object",
            "properties": {"mission_id": {"type": "string"}},
            "required": ["mission_id"],
        },
    ),
    ToolSpec(
        name="library_search",
        description=(
            "Search the Library (cross-project skills, tools, reports, memories) "
            "for prior art before proposing a mission."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "type": {"type": "string", "description": "skill|tool|report|memory"},
            },
            "required": ["query"],
        },
    ),
]


@dataclass
class Proposal:
    objective: str
    title: str = ""
    plan: str = "fix"
    project: str = ""
    repo: str = ""
    model: str = ""
    effort: str = ""
    # always set: the model's own sizing when it gave a usable one, else the
    # estimator's (a proposal with no budget is a proposal nobody can price)
    budget_usd: float = 0.0
    budget_reason: str = ""

    def summary(self) -> str:
        bits = [f"objective: {self.objective}", f"plan: {self.plan}"]
        if self.project:
            bits.append(f"project: {self.project}")
        if self.repo:
            bits.append(f"repo: {self.repo}")
        if self.model:
            bits.append(f"model: {self.model}")
        if self.effort:
            bits.append(f"effort: {self.effort}")
        if self.budget_usd > 0:
            line = f"budget ${self.budget_usd:.2f}"
            if self.budget_reason:
                line = f"{line} ({self.budget_reason})"
            bits.append(line)
        return " · ".join(bits)


def proposal_plan(args: dict[str, Any], objective: str) -> str:
    """The plan a proposal carries: the one the model named, else a default.
    The classifier (`reads_only_plan`) only picks the DEFAULT, when the plan is
    absent; a plan the model explicitly named is never overridden."""
    named = str(args.get("plan", "")).strip()
    if named:
        return named
    from rework.missions import reads_only_plan

    return reads_only_plan(objective) or "fix"


def proposal_budget(
    args: dict[str, Any], text: str = "", *, cost_history: float | None = None
) -> tuple[float, str]:
    """(usd, reason) for a proposal: the model's own sizing when it gave a
    usable one, else `estimate_budget` on the task text, calibrated with what
    finished missions actually cost here (`cost_history`, only nudges inside
    the estimate's own band).

    Model output is never trusted to be well-formed, and it must never raise
    here: garbage falls back to the estimate, which always answers."""
    from rework.budget_policy import estimate_budget

    usd = _positive_float(args.get("budget_usd"))
    reason = str(args.get("budget_reason") or "").strip()
    if usd is not None:
        return usd, reason or f"model priced this at ${usd:.2f}"
    estimated, why = estimate_budget(text, cost_history=cost_history)
    return float(estimated), reason or why


def _positive_float(value: Any) -> float | None:
    """A finite, positive amount of money, or None: strings, bools, NaN, inf
    and <= 0 are all "the model did not really say" (never raise on them)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(str(value).strip().strip("$")) if isinstance(value, str) else float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


@dataclass
class Turn:
    """One exchange, kept for rendering."""

    who: str  # "you" | "orchestrator" | "note"
    text: str


@dataclass
class ChatResult:
    reply: str = ""  # what to say back (may be empty when proposing)
    # every proposal this turn produced, in call order (usually one, sometimes
    # several when the owner's request spans separate pieces of work)
    proposals: list[Proposal] = field(default_factory=list)
    # a bare yes with proposals pending means launch them ALL; a bare no means
    # scrap them all. The caller acts on these signals instead of re-deriving
    # which proposal the owner meant (there is no "which one" path).
    launched_all: bool = False
    scrapped_all: bool = False
    asked: str = ""  # a question when the model needs more
    turn_tokens: int = 0
    turn_cost_usd: float = 0.0
    transcript: list[Turn] = field(default_factory=list)

    @property
    def proposal(self) -> Proposal | None:
        """Compatibility view: the FIRST proposal of this turn, None when there
        is none. Callers that can only handle one proposal (a single card, a
        CLI print) keep working; `proposals` is the real shape."""
        return self.proposals[0] if self.proposals else None


class ChatTools:
    """Executes the read-only LOOK_TOOLS against the store.

    Every result is bounded and shaped for a model, not a human: ids first (the
    model will need to quote them back), then the few fields that decide a
    proposal. Errors are returned as text, never raised — a failed lookup is
    information the model should see and work around, not a dead conversation."""

    def __init__(self, store: Any, library: Any | None = None) -> None:
        self._store = store
        self._library = library

    async def execute(self, name: str, args: dict[str, Any]) -> str:
        try:
            if name == "list_projects":
                projects = await self._store.list_projects()
                if not projects:
                    return "(no projects yet)"
                lines = []
                for p in projects:
                    repos = (p.settings or {}).get("repos") or []
                    refs = ", ".join(f"{r.get('name')} ({r.get('kind') or 'local'})" for r in repos)
                    lines.append(f"{p.id}  {p.name}" + (f"  repos: {refs}" if refs else ""))
                return "\n".join(lines)
            if name == "list_missions":
                from rework.kernel.state.records import MissionStatus

                limit = int(args.get("limit") or 10)
                status = args.get("status")
                statuses = [MissionStatus(status)] if status else [None]
                rows = []
                for st in statuses:
                    rows.extend(await self._store.list_missions(status=st, limit=limit))
                if not rows:
                    return "(no missions)"
                return "\n".join(
                    f"{m.id}  [{m.status.value}]  {m.objective[:70]}"
                    + (f"  (project {m.project_id})" if m.project_id else "")
                    for m in rows[:limit]
                )
            if name == "get_mission":
                mission = await self._store.get_mission(str(args.get("mission_id") or ""))
                if mission is None:
                    return f"no mission {args.get('mission_id')!r}"
                spend = await self._store.mission_spend(mission.id)
                runs = await self._store.list_runs(mission.id)
                steps = [
                    f"{s.get('step') or s.get('role')}:{s.get('role')}"
                    for s in ((mission.plan or {}).get("steps") or [])
                ]
                out = [
                    f"mission {mission.id} [{mission.status.value}]",
                    f"objective: {mission.objective}",
                    f"plan steps: {', '.join(steps) or '(none)'}",
                    f"spend: ${spend.spent_usd:.4f}"
                    f" ({spend.input_tokens} in / {spend.output_tokens} out)",
                ]
                for r in runs:
                    artifact = r.artifact or {}
                    summary = str(artifact.get("summary") or artifact.get("kind") or "")[:120]
                    out.append(
                        f"  run {r.step}:{r.role}:{r.attempt} [{r.status.value}]"
                        + (f" — {summary}" if summary else "")
                    )
                    for finding in (artifact.get("findings") or [])[:3]:
                        if isinstance(finding, dict):
                            out.append(
                                f"      finding ({finding.get('severity', 'info')}):"
                                f" {finding.get('title') or finding.get('summary') or '?'}"
                            )
                return "\n".join(out)
            if name == "library_search":
                if self._library is None:
                    return "(the Library is not configured on this deployment)"
                hits = await self._library.search_items(
                    str(args.get("query") or ""), args.get("type")
                )
                if not hits:
                    return "(no library matches)"
                return "\n".join(
                    f"{i.id}  [{i.type.value}] {i.name}: {i.description}" for i in hits
                )
            return f"unknown tool {name!r}"
        except Exception as exc:  # a lookup failure is info, not a crash
            logger.exception("chat lookup tool %s failed", name)
            if name == "library_search":
                return f"error: Library search failed: {exc}; continuing without it"
            return f"error: {name} failed: {type(exc).__name__}: {exc}"


class OrchestratorChat:
    """A conversation that ends in a mission proposal, never in a silent launch."""

    def __init__(
        self,
        provider: ModelProvider,
        catalog: ModelCatalog,
        catalog_id: str,
        *,
        effort: str | None = None,
        context: str = "",
        store: Any | None = None,
        chat_id: str | None = None,
        on_turn: Any | None = None,
        library: Any | None = None,
        tools_enabled: bool = True,
        max_tool_rounds: int = 4,
    ) -> None:
        """`store` + `chat_id` turn persistence ON: every user and assistant
        message is appended to the chat row as it happens, and `load()` can
        replay the transcript into a new instance (the Work surface lists saved
        chats and reopens them). Without them the conversation is in-memory
        only, exactly as before — tests and CLI callers are unaffected.

        `on_turn(kind, payload)` is an optional sink for the tool loop, so a
        caller can render the trajectory live instead of waiting for the turn
        to end (mirrors the node trajectory events: turn / tool_call /
        tool_result)."""
        self._provider = provider
        self._catalog = catalog
        self._entry = catalog.resolve(catalog_id)
        self._effort = effort
        self._context = context
        self._store = store
        self.chat_id = chat_id
        self._on_turn = on_turn
        self._tools_enabled = tools_enabled and store is not None
        self._max_tool_rounds = max_tool_rounds
        self._toolbox = ChatTools(store, library) if self._tools_enabled else None
        self._history: list[dict[str, Any]] = []
        self._display: list[tuple[str, str]] = []  # (who, text) replay for the UI
        # PENDING proposals, in the order the model made them. One owner request
        # can span several separate pieces of work, so this is a list; the turn
        # that produces proposals REPLACES it (documented, tested).
        self.proposals: list[Proposal] = []
        self.launched = False

    @property
    def proposal(self) -> Proposal | None:
        """Compatibility view of `proposals`: the FIRST pending proposal, or
        None. Callers written for the old single-proposal engine keep working
        (they see the first one and ignore the rest)."""
        return self.proposals[0] if self.proposals else None

    @proposal.setter
    def proposal(self, value: Proposal | None) -> None:
        """Replaces the WHOLE pending list with `[value]` (or `[]` for None),
        which is what a single-proposal caller means when it assigns."""
        self.proposals = [value] if value is not None else []

    async def _record(self, role: str, body: dict[str, Any], display: str) -> bool:
        """Persist one transcript row (no-op when persistence is off). Returns
        False when the write failed — a storage hiccup must not lose the
        conversation turn, but it must not be silent either."""
        if self._store is None or self.chat_id is None:
            return True
        from rework.kernel.state.records import ChatMessage

        try:
            await self._store.append_chat_message(
                ChatMessage(chat_id=self.chat_id, role=role, body=body, display=display)
            )
        except Exception:
            logger.exception("could not persist chat turn (role=%s)", role)
            return False
        return True

    async def _note(self, user_text: str, reply: str) -> None:
        """Record a turn that never reached the model (a bare yes/no)."""
        self._display.append(("you", user_text))
        await self._record("user", {"role": "user", "content": user_text}, user_text)
        self._display.append(("orchestrator", reply))
        await self._record("assistant", {"role": "assistant", "content": reply}, reply)

    async def load(self) -> list[tuple[str, str]]:
        """Rehydrate the transcript from the store. Returns [(who, text)] for the
        UI, oldest first. Call once after constructing with a chat_id."""
        if self._store is None or self.chat_id is None:
            return []
        messages = await self._store.chat_messages(self.chat_id)
        self._history = [m.body for m in messages if m.body]
        # stored roles are provider-neutral ("user"/"assistant"); the display
        # vocabulary used throughout the UI is "you"/"orchestrator"
        who = {"user": "you", "assistant": "orchestrator"}
        self._display = [(who.get(m.role, m.role), m.display) for m in messages if m.display]
        return list(self._display)

    @property
    def transcript(self) -> list[tuple[str, str]]:
        """The conversation so far, as (who, text), for rendering."""
        return list(self._display)

    @property
    def model_label(self) -> str:
        return self._entry.id

    @property
    def supports_images(self) -> bool:
        """Whether the catalog says this model can read images. A model without
        the capability must be refused BEFORE sending, not silently stripped —
        a chat that quietly drops the screenshot answers confidently about
        something it never saw."""
        return "vision" in getattr(self._entry, "capabilities", set())

    def _output_budget(self) -> int:
        """Output tokens for one request: the model's max_output; with an
        effort set, thinking shares the budget, so never below the floor."""
        budget = int(self._entry.max_output or 4096)
        return max(budget, EFFORT_FLOOR_TOKENS) if self._effort else budget

    async def _complete(self, specs: list[ToolSpec], budget: int, effort: str | None) -> Any:
        """One model call; a provider failure is logged with its traceback."""
        request = ChatRequest(
            model=self._entry.model,
            system=self._system(),
            messages=self._history,
            tools=specs,
            max_tokens=budget,
            effort=effort,
            session_id=self.chat_id,
        )
        try:
            return await self._provider.complete(request)
        except Exception:
            logger.exception("orchestrator chat provider call failed")
            raise

    @staticmethod
    def _cut_off(result: Any) -> bool:
        """A reply that spent its whole budget thinking: no text, no tool call."""
        return (
            result.stop_reason == "max_tokens"
            and not (result.text or "").strip()
            and not result.tool_calls
        )

    def _system(self) -> str:
        parts = [SYSTEM_PROMPT]
        if self._context:
            parts.append("## Context\n" + self._context)
        return "\n\n".join(parts)

    async def send(self, text: str, images: list[str] | None = None) -> ChatResult:
        """One owner message in, one orchestrator turn out.

        `images` are file paths attached to this user turn (see
        kernel/llm/types.py: paths, not bytes)."""
        text = text.strip()
        images = list(images or [])
        if not text and not images:
            return ChatResult()
        if images and not self.supports_images:
            raise ValueError(
                f"{self._entry.id} has no vision capability; "
                "switch the orchestrator model or drop the attachment"
            )

        # A bare yes after proposals launches ALL of them, a bare no scraps all
        # of them. With more than one pending proposal there is no way to say
        # "which one" in words, so the answer is never ambiguous: yes is
        # all-in, no is all-out, and picking ONE is the per-card button in the
        # UI (which never goes through this path).
        if self.proposals and text.lower() in YES_WORDS:
            launching = list(self.proposals)
            self.proposals = []
            self.launched = True
            reply = "Launching all of them." if len(launching) > 1 else "Launching."
            await self._note(text, reply)
            return ChatResult(
                reply=reply,
                proposals=launching,
                launched_all=True,
                transcript=[Turn("you", text)],
            )
        if self.proposals and text.lower() in NO_WORDS:
            scrapped = len(self.proposals)
            self.proposals = []
            reply = (
                f"Scrapped all {scrapped}. Tell me what to change and I'll propose again."
                if scrapped > 1
                else "Scrapped it. Tell me what to change and I'll propose again."
            )
            await self._note(text, reply)
            return ChatResult(reply=reply, scrapped_all=True, transcript=[Turn("you", text)])

        user_msg: dict[str, Any] = {"role": "user", "content": text}
        if images:
            user_msg["images"] = images
        self._history.append(user_msg)
        chips = " ".join(f"📎 {p.split('/')[-1]}" for p in images)
        self._display.append(("you", f"{text}  {chips}".strip() if chips else text))
        await self._record("user", user_msg, self._display[-1][1])
        specs = [PROPOSE_TOOL, ASK_TOOL]
        if self._toolbox is not None:
            specs = [*specs, *LOOK_TOOLS]

        # BOUNDED TOOL LOOP: the orchestrator may look at the owner's world
        # (projects, missions, library) before it proposes. Every round is a
        # model call, so the loop is capped: a chat that keeps calling
        # list_missions instead of answering is a bug, not persistence.
        total_in = 0
        total_out = 0
        rounds = 0
        cut_offs = 0
        budget = self._output_budget()
        effort = self._effort
        try:
            while True:
                result = await self._complete(specs, budget, effort)
                total_in += result.usage.input_tokens
                total_out += result.usage.output_tokens

                if self._cut_off(result) and cut_offs < MAX_CUT_OFFS:
                    # cut off mid-thought: retry once with more room, then at a
                    # lower effort (the mission orchestrator's ladder, ecbf960)
                    cut_offs += 1
                    if cut_offs == 1:
                        budget = budget * 2
                        logger.warning(
                            "orchestrator reply cut off at the output limit; retrying with"
                            " a %d-token budget",
                            budget,
                        )
                    else:
                        effort = _LOWER_EFFORT.get(effort or "", "low")
                        logger.warning(
                            "orchestrator reply cut off at the output limit again; retrying"
                            " at effort %s",
                            effort,
                        )
                    continue

                look_calls = [
                    tc for tc in result.tool_calls if tc.name in {t.name for t in LOOK_TOOLS}
                ]
                if not look_calls or self._toolbox is None or rounds >= self._max_tool_rounds:
                    break
                rounds += 1
                # the assistant's tool-call turn, then one tool result per call
                call_msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": result.text or "",
                    "tool_calls": _neutral_calls(result.tool_calls),
                }
                self._history.append(call_msg)
                # persisted too: a reloaded history with tool rows but no preceding
                # assistant tool_calls is rejected by OpenAI-compatible providers
                await self._record("assistant", call_msg, "")
                for tc in result.tool_calls:
                    if tc.name in {t.name for t in LOOK_TOOLS}:
                        if self._on_turn is not None:
                            self._on_turn("tool_call", {"name": tc.name, "arguments": tc.arguments})
                        out = await self._toolbox.execute(tc.name, dict(tc.arguments))
                        if self._on_turn is not None:
                            self._on_turn(
                                "tool_result",
                                {
                                    "name": tc.name,
                                    "result": out[:300],
                                    "error": out[:200] if out.startswith("error") else "",
                                },
                            )
                    else:
                        out = "not executed here"
                    self._history.append(
                        {"role": "tool", "tool_call_id": tc.id, "name": tc.name, "content": out}
                    )
                    if self._record is not None:
                        await self._record(
                            "tool",
                            {
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "name": tc.name,
                                "content": out,
                            },
                            "",
                        )
                if self._on_turn is not None:
                    self._on_turn("turn", {"turn": rounds, "tools": [c.name for c in look_calls]})
        except Exception as exc:
            logger.exception("orchestrator chat turn failed")
            note = f"⚠ {type(exc).__name__}: {exc}"
            self._display.append(("orchestrator", note))
            await self._record("assistant", {}, note)
            return ChatResult(
                reply=note, transcript=[Turn("you", text), Turn("orchestrator", note)]
            )

        cost = self._entry.estimate_cost(total_in, total_out)

        # Record the assistant turn (including any tool call) in history so
        # follow-ups keep context — but never an empty-text turn with no tool
        # call: Anthropic rejects empty assistant messages (m-aea59e6edac5).
        reply = (result.text or "").strip()
        assistant_msg: dict[str, Any] = {"role": "assistant", "content": result.text or ""}
        if result.tool_calls:
            assistant_msg["tool_calls"] = _neutral_calls(result.tool_calls)
        if reply or result.tool_calls:
            self._history.append(assistant_msg)
        asked = ""
        # EVERY propose_mission call in this turn becomes its own pending
        # proposal, in call order. A later turn that proposes REPLACES the
        # pending list (the model has re-stated what it wants to run), so stale
        # proposals can never be launched by a bare yes.
        proposals: list[Proposal] = []
        history: float | None = None
        if any(c.name == "propose_mission" for c in result.tool_calls) and self._store:
            # the same calibration `rework new` uses (median finished cost), so
            # every proposal is sized against real spend and not just keywords
            from rework.budget_policy import typical_finished_cost

            history = await typical_finished_cost(self._store)
        for call in result.tool_calls:
            args = call.arguments or {}
            if call.name == "propose_mission":
                objective = str(args.get("objective", "")).strip()
                budget_usd, budget_reason = proposal_budget(args, objective, cost_history=history)
                proposals.append(
                    Proposal(
                        objective=objective,
                        title=str(args.get("title", "")).strip(),
                        plan=proposal_plan(args, objective),
                        project=str(args.get("project", "")).strip(),
                        repo=str(args.get("repo", "")).strip(),
                        model=str(args.get("model", "")).strip(),
                        effort=str(args.get("effort", "")).strip(),
                        budget_usd=budget_usd,
                        budget_reason=budget_reason,
                    )
                )
            elif call.name == "ask":
                asked = str(args.get("question", "")).strip()
        if proposals:
            self.proposals = proposals

        transcript = [Turn("you", text)]
        if proposals:
            reply = reply or "Here's what I'd run."
            lines = "\n  ".join(p.summary() for p in proposals)
            transcript.append(Turn("orchestrator", f"{reply}\n  {lines}"))
        elif asked:
            reply = asked
            transcript.append(Turn("orchestrator", asked))
        else:
            if not reply:
                if self._cut_off(result):
                    reply = (
                        "the model returned nothing (output token limit); "
                        "please retry or raise max_output"
                    )
                else:
                    reply = "the model returned nothing; please retry"
                logger.error(
                    "orchestrator chat turn ended with no reply (stop_reason=%s, effort=%s)",
                    result.stop_reason,
                    effort,
                )
            transcript.append(Turn("orchestrator", reply))

        shown = transcript[-1].text if transcript else reply
        self._display.append(("orchestrator", shown))
        # an empty-text reply has no replayable body: {} keeps the display row
        # without re-feeding an empty assistant message on reload
        body = assistant_msg if ((result.text or "").strip() or result.tool_calls) else {}
        saved = await self._record("assistant", body, shown)
        if not saved:
            # the reply still reaches the owner; only the note marks it unsaved
            reply = f"{reply}\n(not saved — could not persist the chat turn)"
            transcript[-1] = Turn("orchestrator", reply)
            self._display[-1] = ("orchestrator", reply)

        return ChatResult(
            reply=reply,
            proposals=proposals,
            asked=asked,
            turn_tokens=total_in + total_out,
            turn_cost_usd=cost,
            transcript=transcript,
        )


def _neutral_calls(calls: list[Any]) -> list[dict[str, Any]]:
    """Tool calls in rework's PROVIDER-NEUTRAL message shape ({id, name,
    arguments}), the one every adapter translates (nodes/toolloop.py writes the
    same). The OpenAI wire shape ({type, function: {...}}) crashed the adapters
    with KeyError 'name' on the round after any lookup (found live)."""
    return [{"id": tc.id, "name": tc.name, "arguments": dict(tc.arguments or {})} for tc in calls]


YES_WORDS = {"y", "yes", "yeah", "yep", "ok", "okay", "sure", "go", "launch", "do it", "run it"}
NO_WORDS = {"n", "no", "nope", "cancel", "stop", "scrap", "not yet"}
