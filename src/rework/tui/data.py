"""TUI data layer: one live store connection the app shares (the TUI is a
CLIENT, same as the CLI commands: it talks to whatever REWORK_DB points at,
local sqlite or the deployment Postgres over the tailnet)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from rework.config import DEFAULT_STATE_DIR, Config, open_store_from_config
from rework.kernel.state.records import Mission, Project, Run
from rework.kernel.state.store import StateStore
from rework.library.store import GitLibrary, LibraryStore, open_library_store


def library_root() -> Path:
    import os

    return Path(
        os.environ.get("REWORK_LIBRARY_DIR", str(DEFAULT_STATE_DIR / "library"))
    ).expanduser()


class TuiData:
    """Lazy single connections + thin query methods. All async; the Textual
    loop is the only caller (single-connection stores are safe here)."""

    def __init__(self) -> None:
        self._store: StateStore | None = None
        self._index: LibraryStore | None = None
        self._git: GitLibrary | None = None

    async def store(self) -> StateStore:
        if self._store is None:
            self._store = await open_store_from_config(Config.from_env())
        return self._store

    async def library(self) -> tuple[LibraryStore, GitLibrary]:
        if self._index is None or self._git is None:
            config = Config.from_env()
            self._index = await open_library_store(config.db)
            self._git = GitLibrary(library_root(), author=config.git_author)
        return self._index, self._git

    async def aclose(self) -> None:
        if self._store is not None:
            await self._store.aclose()
            self._store = None
        if self._index is not None:
            await self._index.aclose()
            self._index = None

    # -- queries ----------------------------------------------------------

    async def projects(self) -> list[Project]:
        store = await self.store()
        return await store.list_projects()

    async def missions(self, limit: int = 60) -> list[Mission]:
        store = await self.store()
        return await store.list_missions(limit=limit)

    async def open_gates(self, mission_id: str | None = None) -> list[Any]:
        store = await self.store()
        return await store.list_open_gates(mission_id)

    async def events_tail(self, mission_id: str, since: int, limit: int = 500) -> list[Any]:
        store = await self.store()
        return await store.events_since(since, mission_id=mission_id, limit=limit)

    async def mission_runs(self, mission_id: str) -> list[Run]:
        store = await self.store()
        return await store.list_runs(mission_id)

    async def mission_spend(self, mission_id: str) -> float:
        store = await self.store()
        spend = await store.mission_spend(mission_id)
        return spend.spent_usd

    async def decide_gate(self, gate_id: str, approved: bool) -> bool:
        store = await self.store()
        gate = await store.decide_gate(gate_id, approved)
        return gate is not None and gate.status.value == ("approved" if approved else "rejected")

    # -- library ----------------------------------------------------------

    async def library_search(self, query: str) -> list[Any]:
        index, _ = await self.library()
        if not query.strip():
            return await index.list_items(status=None, limit=100)
        return await index.search(query, limit=50)

    async def library_body(self, item_id: str) -> tuple[Any, str | None]:
        index, git = await self.library()
        item = await index.get_item(item_id)
        if item is None:
            return None, None
        return item, git.read_body(item)

    # -- writes ------------------------------------------------------------

    async def create_project(self, name: str, transport: str = "git") -> Project:
        from rework.kernel.state.records import Project as ProjectRecord

        store = await self.store()
        project = ProjectRecord(
            name=name, settings={"transport": transport, "results": "push_branch", "repos": []}
        )
        return await store.create_project(project)

    async def create_mission(self, **kwargs: Any) -> Mission:
        from rework.missions import create_mission as _create

        return await _create(store=await self.store(), **kwargs)


def human_age(epoch: float) -> str:
    delta = max(0.0, time.time() - epoch)
    if delta < 60:
        return f"{delta:.0f}s"
    if delta < 3600:
        return f"{delta / 60:.0f}m"
    if delta < 86400:
        return f"{delta / 3600:.0f}h"
    return f"{delta / 86400:.0f}d"


def status_rich(status: str) -> str:
    """Status text with a color tag for DataTable cells (rich markup)."""
    color = {
        "running": "cyan",
        "awaiting_gate": "bold yellow",
        "on_hold": "yellow",
        "done": "green",
        "failed": "red",
        "cancelled": "dim",
        "pending": "white",
    }.get(status, "white")
    label = status.replace("_", " ")
    return f"[{color}]{label}[/{color}]"
