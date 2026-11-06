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
        plan=str(args.get("plan", "fix")).strip() or "fix",
        project=str(args.get("project", "")).strip(),
        repo=str(args.get("repo", "")).strip(),
        model=str(args.get("model", "")).strip(),
        effort=str(args.get("effort", "")).strip(),
    )


LAUNCH_PREFIX = "launched "


def items_from_messages(messages: list[Any]) -> list[Item]:
    """Stored ChatMessages -> renderable items (tool calls included)."""
    items: list[Item] = []
    for msg in messages:
        body = msg.body or {}
        role = msg.role
        if role == "note":
            display = str(msg.display or "")
            if display.startswith(LAUNCH_PREFIX):
                items.append(Item("mission", mission_id=display[len(LAUNCH_PREFIX) :].strip()))
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
            proposal = None
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
                    proposal = proposal_from_args(args)
                elif name == "ask":
                    asked = str(args.get("question") or "")
            text = str(body.get("content") or "").strip()
            if proposal is not None:
                items.append(Item("assistant", text=text or "Here's what I'd run."))
                items.append(Item("proposal", proposal=proposal))
            elif asked:
                items.append(Item("assistant", text=asked))
            elif text or msg.display:
                items.append(Item("assistant", text=text or str(msg.display)))
    return items


def context_note(snap: Snapshot, project_id: str | None) -> str:
    """Facts the orchestrator should not have to ask about."""
    lines: list[str] = []
    if snap.projects:
        lines.append("Projects (name: repos):")
        for p in snap.projects:
            repos = (p.settings or {}).get("repos") or []
            refs = ", ".join(f"{r.get('name')}={r.get('url') or r.get('kind')}" for r in repos if r)
            lines.append(f"- {p.name} (id {p.id}): {refs or 'no repos registered'}")
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
    lines.append("Plans: echo (no repo), fix and fix-pr (need a repo).")
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
        # a reloaded chat whose last proposal never launched can still be
        # confirmed with a bare "yes"
        for item in reversed(await self.items()):
            if item.kind == "mission":
                break
            if item.kind == "proposal":
                self.engine.proposal = item.proposal
                break
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
        project_id = resolve_project(snap, proposal.project, self.chat.project_id)
        kwargs: dict[str, Any] = {
            "objective": proposal.objective,
            "plan": proposal.plan or "fix",
            "project_id": project_id,
            "repo": proposal.repo or None,
            "model": proposal.model or None,
            "effort": proposal.effort or None,
        }
        from rework.missions import create_mission

        # forward-compatible: once missions carry their own orchestrator model
        # (ADR 0007) and a chat link (M7 P0), pass them
        if _accepts(create_mission, "orchestrator_model"):
            kwargs["orchestrator_model"] = self.chat.model_id
            kwargs["orchestrator_effort"] = self.chat.effort
        if _accepts(create_mission, "chat_id"):
            kwargs["chat_id"] = self.id
        mission = await self.data.create_mission(**kwargs)
        await self.data.append_chat_note(self.id, f"{LAUNCH_PREFIX}{mission.id}")
        await self.data.update_chat(self.id, mission_id=mission.id)
        if self.engine is not None:
            self.engine.proposal = None
            self.engine.launched = False
        return mission


def default_model(catalog: Any) -> str:
    entries = catalog.all()
    usable = [e for e in entries if e.provider != "fake" and e.has_credentials()] or entries
    # the strongest callable tier is the orchestrator's natural default
    best = max(usable, key=lambda e: (e.tier, -e.cost_in_per_mtok)) if usable else None
    return str(best.id) if best else "fake"
