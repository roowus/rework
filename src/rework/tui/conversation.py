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


def proposal_from_args(args: dict[str, Any]) -> Any:
    from rework.orchestrator.chat import Proposal

    return Proposal(
        objective=str(args.get("objective", "")).strip(),
        title=str(args.get("title", "")).strip(),
        plan=str(args.get("plan", "fix")).strip() or "fix",
        project=str(args.get("project", "")).strip(),
        repo=str(args.get("repo", "")).strip(),
        model=str(args.get("model", "")).strip(),
        effort=str(args.get("effort", "")).strip(),
    )


LAUNCH_PREFIX = "launched "
# A launch note reads `launched <mission id> · <objective>`. The objective is
# prose, not schema: it is what lets a replayed chat tell WHICH of several
# proposals from one turn was launched (position alone cannot: the note is
# appended at the end of the transcript).
NOTE_SEP = " · "


def launch_note(mission_id: str, objective: str = "") -> str:
    objective = (objective or "").strip().replace("\n", " ")
    return f"{LAUNCH_PREFIX}{mission_id}" + (f"{NOTE_SEP}{objective}" if objective else "")


def parse_launch_note(display: str) -> tuple[str, str]:
    """`launched m-x · fix it` -> ("m-x", "fix it"); a legacy bare note -> ("m-x", "")."""
    rest = display[len(LAUNCH_PREFIX) :]
    mission_id, _, objective = rest.partition(NOTE_SEP)
    return mission_id.strip(), objective.strip()


def items_from_messages(messages: list[Any]) -> list[Item]:
    """Stored ChatMessages -> renderable items (tool calls included).

    Every `propose_mission` call in an assistant message becomes its own
    proposal item, in call order: one request can span several separate pieces
    of work and each is its own mission. A proposal stays `live` (still
    decidable) unless a launch note settles it; see `_settle_launched`."""
    items: list[Item] = []
    for msg in messages:
        body = msg.body or {}
        role = msg.role
        if role == "note":
            display = str(msg.display or "")
            if display.startswith(LAUNCH_PREFIX):
                mission_id, objective = parse_launch_note(display)
                items.append(Item("mission", mission_id=mission_id, text=objective))
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
                    proposals.append(proposal_from_args(args))
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
    proposal whose objective it names, else the OLDEST still-pending proposal
    (which is what a legacy `launched <id>` note, or a proposal relaunched by
    hand, means). Anything left pending can still be launched, here or from the
    engine's own list."""
    pending: list[Item] = []
    for item in items:
        if item.kind == "proposal":
            pending.append(item)
            continue
        if item.kind != "mission" or not pending:
            continue
        wanted = (item.text or "").strip()
        chosen = next((p for p in pending if str(p.proposal.objective).strip() == wanted), None)
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
        "draft (document work: research with the web tools, write files in the "
        "project, no PR) follows the same repo rule as fix."
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
        return items_from_messages(await self.data.chat_messages(self.id))

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
        # the note carries the proposal's objective as well as the mission id so
        # a replayed chat can tell WHICH of several proposals from one turn was
        # launched (the note lands at the end of the transcript, so position
        # alone cannot). See items_from_messages / _settle_launched.
        await self.data.append_chat_note(
            self.id, launch_note(mission.id, proposal.objective)
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
