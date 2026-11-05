"""TUI data layer: one live store connection the app shares (the TUI is a
CLIENT, same as the CLI commands: it talks to whatever REWORK_DB points at,
local sqlite or the deployment Postgres over the tailnet)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from rework.config import (
    DEFAULT_STATE_DIR,
    Config,
    default_results_dir,
    open_store_from_config,
)
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

    async def spend_by_model(self, mission_id: str | None = None) -> list[dict[str, Any]]:
        store = await self.store()
        return await store.spend_by_model(mission_id)

    async def spend_total(self) -> dict[str, float]:
        """Every model's row summed: the number the owner actually wants."""
        rows = await self.spend_by_model()
        return {
            "usd": sum(float(r.get("cost_usd") or 0) for r in rows),
            "in": sum(int(r.get("input_tokens") or 0) for r in rows),
            "out": sum(int(r.get("output_tokens") or 0) for r in rows),
            "calls": sum(int(r.get("calls") or 0) for r in rows),
        }

    async def get_mission(self, mission_id: str) -> Any:
        store = await self.store()
        return await store.get_mission(mission_id)

    async def library_set_status(self, item_id: str, retire: bool) -> bool:
        """a / d on the library split: activate or retire the selected item."""
        index, _git = await self.library()
        item = await index.get_item(item_id)
        if item is None:
            return False
        from rework.library.model import ItemStatus

        item.status = ItemStatus.RETIRED if retire else ItemStatus.ACTIVE
        item.touch()
        await index.upsert_item(item)
        return True

    async def mission_result_path(self, mission_id: str) -> str | None:
        """Where the daemon says this mission's finished worktree lives (None =
        nothing to fetch). The repo_path column is daemon-local until fetch
        pulls it; the CLI fetch result lands in ~/rework/results/<id>/."""
        store = await self.store()
        mission = await store.get_mission(mission_id)
        if mission is None or not mission.repo_path:
            return None
        return str(default_results_dir() / mission_id)

    async def fetch_result(self, mission_id: str) -> tuple[str, int]:
        """Pull the final worktree over the workspace API (same call the CLI
        makes). Returns (destination, file count). Raises on failure."""
        import io
        import os
        import tarfile

        import httpx

        from rework.orchestrator.workspace import workspace_url_from_env

        token = os.environ.get("REWORK_TOKEN")
        url = f"{workspace_url_from_env().rstrip('/')}/workspaces/results/{mission_id}"
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        resp = await httpx.AsyncClient(timeout=300.0).get(url, headers=headers)
        if resp.status_code == 404:
            raise FileNotFoundError(f"nothing to fetch for {mission_id}")
        resp.raise_for_status()
        dest = default_results_dir() / mission_id
        dest.mkdir(parents=True, exist_ok=True)
        count = 0
        with tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz") as tar:
            for member in tar.getmembers():
                tar.extract(member, dest, filter="data")
                count += 1
        return str(dest), count

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

    async def library_hybrid_search(self, query: str) -> list[Any]:
        """Hybrid (embeddings + keyword) when REWORK_EMBEDDINGS_* is set;
        silently keyword-only otherwise. The TUI stays alive if the embedder
        errors: HybridSearch already degrades internally."""
        index, _git = await self.library()
        from rework.library.embeddings import embeddings_from_env
        from rework.library.search import HybridSearch

        embedder = embeddings_from_env()
        hybrid = HybridSearch(index, embedder)
        if embedder is not None:
            await hybrid.ensure_indexed()
        return await hybrid.search(query, limit=50)

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
