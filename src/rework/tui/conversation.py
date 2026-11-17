"""The Work tab's brain, kept free of widgets so it is testable on its own.

A `Conversation` is one persisted chat plus (lazily) the OrchestratorChat that
talks to the model. It knows how to build the orchestrator's context from the
owner's world, how to replay a stored transcript into renderable items, and how
to turn a confirmed proposal into a mission (handoff §3.6)."""

from __future__ import annotations

import inspect
import json
from dataclasses import dataclass, field
from typing import Any

from rework.tui.hub import Snapshot


@dataclass
class Item:
    """One renderable transcript entry.

    kind: user | assistant | tool_call | tool_result | proposal | mission | note
    """

    kind: str
    text: str = ""
    name: str = ""
    args: str = ""
    error: str = ""
    proposal: Any = None
    mission_id: str = ""
    # for mission items: the plan the launch note named ("" for a legacy note)
    plan: str = ""
    # for proposals: still waiting for a decision (False once it launched or
    # was scrapped). Computed on replay by `items_from_messages`.
    live: bool = True
    attachments: list[str] = field(default_factory=list)


def args_text(arguments: Any) -> str:
    """Tool arguments as one readable line: k=v, k=v."""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except (ValueError, TypeError):
            return arguments
    if isinstance(arguments, dict):
        return ", ".join(f"{k}={v}" for k, v in arguments.items() if v not in ("", None))
    return str(arguments or "")


def proposal_from_args(args: dict[str, Any], *, cost_history: float | None = None) -> Any:
    """A stored propose_mission call rebuilt as a Proposal: a replayed card must
    show the same budget the live one did, including the estimate fallback for
    arguments saved before the model could price a mission.

    `cost_history` is what the live path passes (`typical_finished_cost`), and
    it only matters for an UNPRICED proposal: the estimate's band is nudged by
    it, so a replay without it would show — and then stamp at launch — a
    different cap than the live card. A caller that has no store (a pure
    replay test) may omit it and gets the keyword estimate."""
    from rework.orchestrator.chat import Proposal, proposal_budget, proposal_plan

    objective = str(args.get("objective", "")).strip()
    budget_usd, budget_reason = proposal_budget(args, objective, cost_history=cost_history)
    return Proposal(
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


LAUNCH_PREFIX = "launched "
# A launch note reads `launched <mission id> · <plan> · <objective>`. Both extra
# fields are prose, not schema: together they are what lets a replayed chat tell
# WHICH of several proposals was launched (position alone cannot: the note is
# appended at the end of the transcript). The plan is in there because a chat
# may hold two proposals with the SAME objective and a different plan (the
# system prompt steers the model to re-propose when the owner changes their
# mind), and an objective alone cannot tell those twins apart.
NOTE_SEP = " · "


@dataclass
class LaunchNote:
    """What a launch note names: the mission plus the proposal it came from."""

    mission_id: str = ""
    plan: str = ""
    objective: str = ""


def launch_note(mission_id: str, plan: str = "", objective: str = "") -> str:
    """The transcript line for a launch: `launched <id> · <plan> · <objective>`.

    With a plan the note always carries BOTH separators, even when the
    objective is empty: the field count is what makes it unambiguous, and a
    two-field note means the legacy objective-only shape. Only a note whose
    plan is empty falls back to that legacy shape (or to the bare
    `launched <id>` when there is no objective either)."""
    # a note is ONE transcript line, so newlines collapse; the edges are left
    # alone so `parse_launch_note` round-trips the objective verbatim
    plan = (plan or "").replace("\n", " ")
    objective = (objective or "").replace("\n", " ")
    if not plan:
        # nothing to anchor a plan field: keep the legacy two-field shape,
        # which `parse_launch_note` reads as an objective (never as a plan)
        return f"{LAUNCH_PREFIX}{mission_id}" + (f"{NOTE_SEP}{objective}" if objective else "")
    return f"{LAUNCH_PREFIX}{mission_id}{NOTE_SEP}{plan}{NOTE_SEP}{objective}"


def parse_launch_note(display: str) -> LaunchNote:
    """Inverse of `launch_note`, tolerant of every shape ever written.

    The field count decides what the fields mean, never a guess about their
    content (an objective can look exactly like a plan):
    - `launched <id>` (no separator) -> plan "", objective "": a legacy bare
      note, which settles the oldest pending proposal.
    - `launched <id> · <a>` (two fields) -> the legacy shape, so `<a>` is the
      OBJECTIVE and the plan stays "". Old notes never carried a plan, so
      reading `<a>` as one would settle the wrong card.
    - `launched <id> · <a> · <b>` (three or more) -> plan `<a>`, objective
      `<b>`.

    Objectives may themselves contain the separator, and the current format
    always writes the plan, so the split is bounded rather than greedy: the
    first two occurrences of NOTE_SEP are taken as the field boundaries and
    everything after them is the objective, verbatim."""
    rest = display[len(LAUNCH_PREFIX) :] if display.startswith(LAUNCH_PREFIX) else display
    # NOT stripped as a whole: an empty objective leaves a trailing separator
    # and dropping its space would turn a three-field note into a two-field one
    parts = rest.split(NOTE_SEP, 2)
    mission_id = parts[0].strip()
    if len(parts) == 1:
        return LaunchNote(mission_id=mission_id)
    if len(parts) == 2:
        return LaunchNote(mission_id=mission_id, objective=parts[1])
    return LaunchNote(mission_id=mission_id, plan=parts[1].strip(), objective=parts[2])


def items_from_messages(messages: list[Any], *, cost_history: float | None = None) -> list[Item]:
    """Stored ChatMessages -> renderable items (tool calls included).

    Every `propose_mission` call in an assistant message becomes its own
    proposal item, in call order: one request can span several separate pieces
    of work and each is its own mission. A proposal stays `live` (still
    decidable) unless a launch note settles it; see `_settle_launched`.

    `cost_history` is forwarded to `proposal_from_args` so a replayed proposal
    card is priced exactly like the live one (see that function)."""
    items: list[Item] = []
    for msg in messages:
        body = msg.body or {}
        role = msg.role
        if role == "note":
            display = str(msg.display or "")
            if display.startswith(LAUNCH_PREFIX):
                note = parse_launch_note(display)
                items.append(
                    Item("mission", mission_id=note.mission_id, plan=note.plan, text=note.objective)
                )
            elif display:
                items.append(Item("note", text=display))
            continue
        if role == "user":
            text = str(body.get("content") or msg.display or "")
            items.append(Item("user", text=text, attachments=list(body.get("images") or [])))
            continue
        if role == "tool":
            content = str(body.get("content") or "")
            name = str(body.get("name") or "tool")
            items.append(Item("tool_call", name=name))
            items.append(
                Item(
                    "tool_result",
                    name=name,
                    text=content,
                    error=content if content.startswith("error") else "",
                )
            )
            continue
        if role == "assistant":
            calls = body.get("tool_calls") or []
            proposals: list[Any] = []
            asked = ""
            for call in calls:
                fn = call.get("function") or {}
                name = fn.get("name") or call.get("name")
                raw = fn.get("arguments") or call.get("arguments") or {}
                try:
                    args = json.loads(raw) if isinstance(raw, str) else dict(raw)
                except (ValueError, TypeError):
                    args = {}
                if name == "propose_mission":
                    proposals.append(proposal_from_args(args, cost_history=cost_history))
                elif name == "ask":
                    asked = str(args.get("question") or "")
            text = str(body.get("content") or "").strip()
            if proposals:
                items.append(Item("assistant", text=text or "Here's what I'd run."))
                items.extend(Item("proposal", proposal=p) for p in proposals)
            elif asked:
                items.append(Item("assistant", text=asked))
            elif text or msg.display:
                items.append(Item("assistant", text=text or str(msg.display)))
    _settle_launched(items)
    return items


def _settle_launched(items: list[Item]) -> None:
    """Mark replayed proposals that already became a mission as not live.

    Rule (documented in docs/design.md): a launch note settles the pending
    proposal it NAMES, matching the plan and objective it carries; a legacy
    note that carries only an objective matches on the objective alone; a bare
    `launched <id>` note settles the OLDEST still-pending proposal (position is
    all it has). When nothing matches, the note still consumed a mission, so
    the oldest pending proposal is settled.

    Two genuinely identical proposals (same plan, same objective) settle the
    oldest of them, which is correct: they are interchangeable, so a mission
    exists for the work either way. Anything left pending can still be
    launched, here or from the engine's own list."""
    pending: list[Item] = []
    for item in items:
        if item.kind == "proposal":
            pending.append(item)
            continue
        if item.kind != "mission" or not pending:
            continue
        plan = (item.plan or "").strip()
        wanted = (item.text or "").strip()
        if plan:
            # the note names the plan too: same objective + same plan, else it
            # is a DIFFERENT proposal that happens to share the objective
            chosen = next(
                (
                    p
                    for p in pending
                    if str(p.proposal.objective).strip() == wanted
                    and str(getattr(p.proposal, "plan", "") or "").strip() == plan
                ),
                None,
            )
        elif wanted:
            chosen = next((p for p in pending if str(p.proposal.objective).strip() == wanted), None)
        else:
            chosen = None
        if chosen is None:
            chosen = pending[0]
        chosen.live = False
        pending.remove(chosen)


def context_note(snap: Snapshot, project_id: str | None) -> str:
    """Facts the orchestrator should not have to ask about."""
    lines: list[str] = []
    if snap.projects:
        from rework.missions import derive_transport

        lines.append("Projects (name: transport; repos):")
        for p in snap.projects:
            settings = p.settings or {}
            repos = settings.get("repos") or []
            refs = ", ".join(f"{r.get('name')}={r.get('url') or r.get('kind')}" for r in repos if r)
            transport, results = derive_transport(settings)
            lines.append(
                f"- {p.name} (id {p.id}): transport {transport} (results {results}); "
                f"{refs or 'no repos registered'}"
            )
    else:
        lines.append("There are no projects yet; missions can be project-less.")
    if project_id:
        lines.append(
            f"The owner is working in project {snap.project_name(project_id)!r}: "
            "default to it unless they say otherwise."
        )
    active = [m for m in snap.missions if m.status.value in {"running", "awaiting_gate", "on_hold"}]
    if active:
        lines.append("Missions in flight:")
        for mission in active[:10]:
            lines.append(f"- {mission.id} [{mission.status.value}] {mission.objective[:80]}")
    lines.append(
        "Plans: echo (no repo). fix and fix-pr need a repo for a git-transport "
        "project; for an upload-transport project they do NOT need one — the "
        "project's files area supplies the files, and results are fetched, not pushed. "
        "draft (read-and-report work: the deliverable is the mission's Report "
        "tab, no files are written into the repository, and there is no PR) still "
        "resolves a repo to run against; audit (read-only survey of a codebase, no "
        "code change, no PR) also needs a repo."
    )
    return "\n".join(lines)


def resolve_project(snap: Snapshot, name: str, fallback: str | None) -> str | None:
    """The chat speaks project NAMES; missions store ids."""
    name = (name or "").strip()
    if not name:
        return fallback
    for p in snap.projects:
        if name.lower() in {str(p.name).lower(), str(p.id).lower()}:
            return str(p.id)
    return fallback


def _accepts(fn: Any, param: str) -> bool:
    try:
        return param in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


class Conversation:
    """One chat: the stored row + a lazily-built OrchestratorChat."""

    def __init__(self, data: Any, chat: Any) -> None:
        self.data = data
        self.chat = chat
        self.engine: Any = None  # OrchestratorChat
        self._engine_key: tuple[str, str] | None = None

    @property
    def id(self) -> str:
        return str(self.chat.id)

    async def items(self) -> list[Item]:
        return items_from_messages(
            await self.data.chat_messages(self.id), cost_history=await self.cost_history()
        )

    async def cost_history(self) -> float | None:
        """What finished missions really cost here, for pricing a replayed
        proposal: the same number the live path uses, so both cards agree."""
        try:
            from rework.budget_policy import typical_finished_cost

            return await typical_finished_cost(await self.data.store())
        except Exception:  # a replay must never die for lack of a ledger
            return None

    async def ensure_engine(self, snap: Snapshot, on_turn: Any) -> Any:
        """(Re)build the OrchestratorChat for the chat's current model/effort.
        A model switch mid-chat rebuilds it and replays the stored history."""
        from rework.config import load_catalog
        from rework.kernel.llm.adapters import build_provider
        from rework.orchestrator.chat import OrchestratorChat

        catalog = load_catalog()
        model_id = self.chat.model_id or default_model(catalog)
        key = (model_id, self.chat.effort or "")
        if self.engine is not None and self._engine_key == key:
            self.engine._context = context_note(snap, self.chat.project_id)
            return self.engine
        entry = catalog.resolve(model_id)
        kwargs: dict[str, Any] = {
            "effort": self.chat.effort or None,
            "context": context_note(snap, self.chat.project_id),
            "store": await self.data.store(),
            "chat_id": self.id,
        }
        if _accepts(OrchestratorChat.__init__, "on_turn"):
            kwargs["on_turn"] = on_turn
        if _accepts(OrchestratorChat.__init__, "library"):
            kwargs["library"] = await self._library_handle()
        self.engine = OrchestratorChat(build_provider(entry), catalog, model_id, **kwargs)
        await self.engine.load()
        # EVERY proposal that never launched is restored, in order, so a
        # reloaded chat shows all of them and a bare "yes" launches all of them
        # (not just the newest one, which is what the old single slot did)
        self.engine.proposals = [
            item.proposal for item in await self.items() if item.kind == "proposal" and item.live
        ]
        self._engine_key = key
        return self.engine

    async def _library_handle(self) -> Any:
        try:
            from rework.library.tools import LibraryHandle

            index, git = await self.data.library()
            return LibraryHandle(index=index, git=git)
        except Exception:
            return None

    async def launch(self, snap: Snapshot, proposal: Any) -> Any:
        """Create the mission a confirmed proposal describes. Raises ValueError
        when the plan needs a repo nobody named (the caller asks for one)."""
        from rework.missions import create_mission, project_is_upload

        # A proposal is launchable only while it is still PENDING in the engine:
        # a later proposing turn replaces the pending list, and a click that
        # lost the race to a sibling launch arrives too late. Creating the
        # mission before checking is what let one proposal run twice.
        if self.engine is not None and not any(
            same_proposal(p, proposal) for p in self.engine.proposals
        ):
            return None

        project_id = resolve_project(snap, proposal.project, self.chat.project_id)
        # the project's OWN settings decide whether a repo is meaningful: for an
        # upload project the files area supplies the files (results fetched).
        settings = next(
            (dict(p.settings or {}) for p in snap.projects if str(p.id) == str(project_id)),
            None,
        )
        upload = settings is not None and project_is_upload(settings)
        kwargs: dict[str, Any] = {
            "objective": proposal.objective,
            "plan": proposal.plan or "fix",
            "project_id": project_id,
            "model": proposal.model or None,
            "effort": proposal.effort or None,
        }
        # never hand create_mission a None repo: the key is simply absent when
        # there is nothing to clone (upload project, or no repo named at all).
        repo = (getattr(proposal, "repo", "") or "").strip()
        if repo and not upload:
            kwargs["repo"] = repo

        # the owner may have edited the estimate on the card: whatever the
        # budget field holds at launch is the cap the mission is created with
        budget_usd = float(getattr(proposal, "budget_usd", 0.0) or 0.0)
        if budget_usd > 0 and _accepts(create_mission, "budget_usd"):
            kwargs["budget_usd"] = budget_usd

        # forward-compatible: once missions carry their own orchestrator model
        # (ADR 0007) and a chat link (M7 P0), pass them
        if _accepts(create_mission, "orchestrator_model"):
            kwargs["orchestrator_model"] = self.chat.model_id
            kwargs["orchestrator_effort"] = self.chat.effort
        if _accepts(create_mission, "chat_id"):
            kwargs["chat_id"] = self.id
        if getattr(proposal, "title", "") and _accepts(create_mission, "title"):
            kwargs["title"] = proposal.title
        mission = await self.data.create_mission(**kwargs)
        # the note carries the proposal's plan AND objective as well as the
        # mission id so a replayed chat can tell WHICH of several proposals was
        # launched: the note lands at the end of the transcript, so position
        # alone cannot, and an objective alone cannot tell two proposals with
        # the same objective but a different plan apart. See
        # items_from_messages / _settle_launched.
        await self.data.append_chat_note(
            self.id, launch_note(mission.id, proposal.plan, proposal.objective)
        )
        # Chat.mission_id stays a scalar, and it keeps the FIRST mission this
        # chat launched: it is only a denormalised pointer for the chat list.
        # The real one-chat-to-many-missions link is Mission.chat_id (which is
        # what WorkView.active_missions() queries), and overwriting it would
        # hide every earlier mission behind the newest one.
        if not self.chat.mission_id:
            await self.data.update_chat(self.id, mission_id=mission.id)
            self.chat.mission_id = mission.id
        if self.engine is not None:
            # only the proposal that launched is settled: the others stay
            # pending and can still be launched on their own
            self.engine.proposals = [
                p for p in self.engine.proposals if not same_proposal(p, proposal)
            ]
            self.engine.launched = False
        return mission


def same_proposal(a: Any, b: Any) -> bool:
    """Identity first (the engine hands out the very objects the cards hold),
    then the full field tuple so a replayed Proposal still matches."""
    if a is b:
        return True
    fields = ("objective", "title", "plan", "project", "repo", "model", "effort")
    return all(getattr(a, f, "") == getattr(b, f, "") for f in fields)


def default_model(catalog: Any) -> str:
    # the configured default (REWORK_DEFAULT_MODEL), resolved against the
    # catalog: the TUI knob starts on the same model everything else does
    from rework.config import default_model_id

    return default_model_id(catalog)
