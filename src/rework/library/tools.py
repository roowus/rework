"""Role integration (M5.2): the Library at spawn and mid-run.

LibraryHandle rides RoleBuildCtx (the daemon builds it; tests and
library-less deployments pass None and roles run exactly as before).
LibraryAwareExecutor wraps a role's executor (same pattern as
ComplaintAwareExecutor) and answers three tools: search, read, contribute.
The spawn menu lists ACTIVE skills by name + description only; bodies are
pulled on demand via library_read, never preloaded.
"""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel, ConfigDict

from rework.kernel.llm.types import ToolSpec
from rework.library.curation import contribute
from rework.library.model import ItemStatus, ItemType
from rework.library.store import GitLibrary, LibraryStore


class LibraryHandle(BaseModel):
    """Everything a role needs to touch the library; None = library off."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    index: LibraryStore
    git: GitLibrary
    hybrid: Any | None = None  # HybridSearch when the semantic layer is on

    async def search_items(self, query: str, type_name: str | None = None):
        """Hybrid when configured, keyword-only otherwise."""
        if self.hybrid is not None:
            return await self.hybrid.search(
                query, type_=ItemType(type_name) if type_name else None, limit=10
            )
        return await self.index.search(
            query, type_=ItemType(type_name) if type_name else None, limit=10
        )

    async def menu(self) -> str:
        """The spawn menu: ACTIVE skills, id + name + description.

        The id is REQUIRED here, not decoration: found live (prod mission
        m-c8b72e7734b7), a coder with a name-only menu invented
        `fix-<slug>-<procedure>` and ate an `error: no library item` turn. The
        model can only pass back what it was shown."""
        skills = await self.index.list_items(
            type_=ItemType.SKILL, status=ItemStatus.ACTIVE, limit=50
        )
        if not skills:
            return ""
        lines = ["Active library skills (library_read takes the id shown here):"]
        for s in skills:
            line = f"- {s.id}  {s.name}"
            lines.append(f"{line}: {s.description}" if s.description else line)
        return "\n".join(lines)

    async def search_text(self, query: str, type_name: str | None = None) -> str:
        items = await self.search_items(query, type_name)
        if not items:
            return "(no library matches)"
        await asyncio.gather(*(self.index.record_use(i.id) for i in items))
        return "\n".join(f"{i.id}  [{i.type.value}] {i.name}: {i.description}" for i in items)

    async def resolve(self, ref: str) -> Any:
        """An id, or failing that a name. Models pass back whatever they were
        shown; a slugified name used to become an opaque miss."""
        item = await self.index.get_item(ref)
        if item is not None:
            return item
        wanted = ref.strip().lower()
        for candidate in await self.index.list_items(limit=500):
            if candidate.id == ref or candidate.name.strip().lower() == wanted:
                return candidate
        return None

    async def read_text(self, item_id: str) -> str:
        item = await self.resolve(item_id)
        if item is None:
            known = await self.index.list_items(limit=5)
            available = ", ".join(f"{i.id} ({i.name})" for i in known) or "none"
            return (
                f"error: no library item {item_id!r}. Use an id from the library"
                f" menu or library_search; available now: {available}"
            )
        if item.status is not ItemStatus.ACTIVE:
            return f"error: {item.name} is {item.status.value}; only active items are served"
        body = await asyncio.to_thread(self.git.read_body, item)
        await self.index.record_use(item.id)
        return body or f"(item {item.id} has no body file)"

    async def contribute_text(self, **args: Any) -> str:
        try:
            type_ = ItemType(str(args.get("type") or ""))
        except ValueError:
            return "error: type must be skill|tool|report|memory"
        name = str(args.get("name") or "").strip()
        body = str(args.get("body") or "")
        if not name or not body.strip():
            return "error: name and body are required"
        _item, note = await contribute(
            self.index,
            self.git,
            type_=type_,
            name=name,
            description=str(args.get("description") or ""),
            body=body,
            tags=[str(t) for t in (args.get("tags") or [])][:10],
            mission_id=str(args.get("mission_id") or "") or None,
            role=str(args.get("role") or "") or None,
            requested_status=ItemStatus.ACTIVE,
        )
        _ = _item  # the note carries the id/status text the agent sees
        return note


LIBRARY_SPECS: list[ToolSpec] = [
    ToolSpec(
        name="library_search",
        description=(
            "Search the Library (cross-project skills/reports/memories)."
            " Args: {query, type?} - type: skill|tool|report|memory"
        ),
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}, "type": {"type": "string"}},
            "required": ["query"],
        },
    ),
    ToolSpec(
        name="library_read",
        description="Read a Library item's full body. Args: {item_id}",
        input_schema={
            "type": "object",
            "properties": {"item_id": {"type": "string"}},
            "required": ["item_id"],
        },
    ),
    ToolSpec(
        name="library_contribute",
        description=(
            "Contribute what you learned to the Library so future missions are"
            " cheaper: a reusable procedure (skill), a finished artifact worth"
            " keeping (report), a fact worth remembering (memory). Tools are"
            " owner-gated. Args: {type, name, description, body, tags?, role?}"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "type": {"type": "string"},
                "name": {"type": "string"},
                "description": {"type": "string"},
                "body": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "role": {"type": "string"},
            },
            "required": ["type", "name", "body"],
        },
    ),
]


def library_executor(ctx: Any, executor: Any) -> Any:
    """Wrap a role's executor with library tools when the ctx carries a
    LibraryHandle; identity when off (tests, library-less deployments)."""
    if ctx.library is None:
        return executor
    return LibraryAwareExecutor(
        executor, ctx.library, ctx.mission.id, ctx.run.id, str(ctx.run.role)
    )


def with_library_menu(ctx: Any, system_prompt: str) -> str:
    """Append the ACTIVE-skills spawn menu to a role prompt when present."""
    if ctx.library_menu:
        return f"{system_prompt}\n\n{ctx.library_menu}"
    return system_prompt


class LibraryAwareExecutor:
    """Wraps any executor; answers the three library tools, delegates the rest
    (the inner jail/sandbox keeps isolating). Order with other wrappers does
    not matter: disjoint tool names."""

    def __init__(
        self,
        inner: Any,
        handle: LibraryHandle,
        mission_id: str,
        run_id: str,
        role: str,
    ) -> None:
        self._inner = inner
        self._handle = handle
        self._mission_id = mission_id
        self._run_id = run_id
        self._role = role

    async def execute(self, name: str, args: dict[str, Any]) -> str:
        if name == "library_search":
            try:
                return await self._handle.search_text(
                    str(args.get("query") or ""), args.get("type")
                )
            except ValueError:
                return "error: type must be skill|tool|report|memory"
        if name == "library_read":
            return await self._handle.read_text(str(args.get("item_id") or ""))
        if name == "library_contribute":
            return await self._handle.contribute_text(
                **{**args, "mission_id": self._mission_id, "role": self._role}
            )
        return await self._inner.execute(name, args)

    def specs(self, allowed: set[str] | None = None) -> list[ToolSpec]:
        base = self._inner.specs(allowed)
        if allowed is not None and not any(
            s.name.startswith("library_") for s in LIBRARY_SPECS if s.name in allowed
        ):
            return base
        return [*base, *LIBRARY_SPECS]
