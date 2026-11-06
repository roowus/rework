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

import json
from dataclasses import dataclass, field
from typing import Any

from rework.kernel.llm.catalog import ModelCatalog, ModelProvider
from rework.kernel.llm.types import ChatRequest, ToolSpec

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
- If the owner changes their mind mid-conversation, update the proposal.

Plans you can choose:
- echo    — no roles, just prove the plumbing works
- fix     — investigate, code, audit, then a ship gate
- fix-pr  — same, but finishes by opening a pull request

Available context is provided in the conversation as system notes. Use it.

You can also LOOK before you leap: list_projects, list_missions, get_mission and
library_search read the owner's real state. Call them when the answer depends on
what already exists (which missions ran, what a project's repos are, whether a
skill already covers this). Do not narrate the lookups; act on them. You cannot
change anything from here — only a confirmed proposal creates a mission."""

PROPOSE_TOOL = ToolSpec(
    name="propose_mission",
    description=(
        "Propose the mission you would launch. The owner confirms or changes it. "
        "Call this whenever you have a concrete objective; do not narrate it in prose."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "objective": {
                "type": "string",
                "description": "What should happen, in the owner's words.",
            },
            "plan": {"type": "string", "enum": ["echo", "fix", "fix-pr"]},
            "project": {"type": "string", "description": "Project name, or empty for none."},
            "repo": {
                "type": "string",
                "description": ("Git URL, local folder path, or empty to use the project's repo."),
            },
            "model": {"type": "string", "description": "Optional catalog id to run the roles on."},
            "effort": {"type": "string", "enum": ["low", "medium", "high"]},
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
    plan: str = "fix"
    project: str = ""
    repo: str = ""
    model: str = ""
    effort: str = ""

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
        return " · ".join(bits)


@dataclass
class Turn:
    """One exchange, kept for rendering."""

    who: str  # "you" | "orchestrator" | "note"
    text: str


@dataclass
class ChatResult:
    reply: str = ""  # what to say back (may be empty when proposing)
    proposal: Proposal | None = None
    asked: str = ""  # a question when the model needs more
    turn_tokens: int = 0
    turn_cost_usd: float = 0.0
    transcript: list[Turn] = field(default_factory=list)


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
            return f"error: {type(exc).__name__}: {exc}"


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
        self.proposal: Proposal | None = None
        self.launched = False

    async def _record(self, role: str, body: dict[str, Any], display: str) -> None:
        """Persist one transcript row (no-op when persistence is off)."""
        if self._store is None or self.chat_id is None:
            return
        from rework.kernel.state.records import ChatMessage

        try:
            await self._store.append_chat_message(
                ChatMessage(chat_id=self.chat_id, role=role, body=body, display=display)
            )
        except Exception:  # a storage hiccup must not lose the conversation turn
            import logging

            logging.getLogger("rework.chat").exception("could not persist chat turn")

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

        # A bare yes after a proposal launches it — the owner's confirm gate.
        if self.proposal is not None and text.lower() in YES_WORDS:
            self.launched = True
            await self._note(text, "Launching.")
            return ChatResult(reply="Launching.", transcript=[Turn("you", text)])
        if self.proposal is not None and text.lower() in NO_WORDS:
            self.proposal = None
            reply = "Scrapped it. Tell me what to change and I'll propose again."
            await self._note(text, reply)
            return ChatResult(reply=reply, transcript=[Turn("you", text)])

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
        while True:
            request = ChatRequest(
                model=self._entry.model,
                system=self._system(),
                messages=self._history,
                tools=specs,
                max_tokens=min(self._entry.max_output, 2_048),
                effort=self._effort,
            )
            result = await self._provider.complete(request)
            total_in += result.usage.input_tokens
            total_out += result.usage.output_tokens

            look_calls = [tc for tc in result.tool_calls if tc.name in {t.name for t in LOOK_TOOLS}]
            if not look_calls or self._toolbox is None or rounds >= self._max_tool_rounds:
                break
            rounds += 1
            # the assistant's tool-call turn, then one tool result per call
            call_msg: dict[str, Any] = {
                "role": "assistant",
                "content": result.text or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments or {}),
                        },
                    }
                    for tc in result.tool_calls
                ],
            }
            self._history.append(call_msg)
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
                        {"role": "tool", "tool_call_id": tc.id, "name": tc.name, "content": out},
                        "",
                    )
            if self._on_turn is not None:
                self._on_turn("turn", {"turn": rounds, "tools": [c.name for c in look_calls]})

        cost = self._entry.estimate_cost(total_in, total_out)

        # Record the assistant turn (including any tool call) in history so
        # follow-ups keep context.
        assistant_msg: dict[str, Any] = {"role": "assistant", "content": result.text or ""}
        if result.tool_calls:
            assistant_msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments or {}),
                    },
                }
                for tc in result.tool_calls
            ]
        self._history.append(assistant_msg)

        reply = (result.text or "").strip()
        asked = ""
        proposal: Proposal | None = None
        for call in result.tool_calls:
            args = call.arguments or {}
            if call.name == "propose_mission":
                proposal = Proposal(
                    objective=str(args.get("objective", "")).strip(),
                    plan=str(args.get("plan", "fix")).strip() or "fix",
                    project=str(args.get("project", "")).strip(),
                    repo=str(args.get("repo", "")).strip(),
                    model=str(args.get("model", "")).strip(),
                    effort=str(args.get("effort", "")).strip(),
                )
                self.proposal = proposal
            elif call.name == "ask":
                asked = str(args.get("question", "")).strip()

        transcript = [Turn("you", text)]
        if proposal is not None:
            reply = reply or "Here's what I'd run."
            transcript.append(Turn("orchestrator", f"{reply}\n  {proposal.summary()}"))
        elif asked:
            reply = asked
            transcript.append(Turn("orchestrator", asked))
        else:
            reply = reply or "(no reply)"
            transcript.append(Turn("orchestrator", reply))

        shown = transcript[-1].text if transcript else reply
        self._display.append(("orchestrator", shown))
        await self._record("assistant", assistant_msg, shown)

        return ChatResult(
            reply=reply,
            proposal=proposal,
            asked=asked,
            turn_tokens=total_in + total_out,
            turn_cost_usd=cost,
            transcript=transcript,
        )


YES_WORDS = {"y", "yes", "yeah", "yep", "ok", "okay", "sure", "go", "launch", "do it", "run it"}
NO_WORDS = {"n", "no", "nope", "cancel", "stop", "scrap", "not yet"}
