"""The Library MCP server (M5.3): any MCP-capable harness (Claude Code, pi,
anything) mounts the SAME library rework roles use.

stdio transport; run with `rework library-mcp` and mount with e.g.
  claude mcp add rework-library -- uv run --project ~/projects/rework rework library-mcp

Tools: search / get (read body) / contribute (subject to the same curation
rules as roles: tools arrive proposed and wait for `rework library activate`).
The index sits on the state DB substrate; bodies in the library git repo
(REWORK_LIBRARY_DIR, default ~/.rework/library).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rework.library.curation import contribute
from rework.library.model import ItemStatus, ItemType
from rework.library.store import GitLibrary, LibraryStore


def build_mcp_server(index: LibraryStore, git: GitLibrary, hybrid: Any | None = None) -> Any:
    """Build the FastMCP server over an open index. Separate function so
    tests can connect an in-memory client without spawning a process."""
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("rework-library")

    @mcp.tool()
    async def search(query: str, item_type: str | None = None) -> str:
        """Search the Library: cross-project skills, tools, reports, and
        memories that rework missions (and you) built up over time. Returns
        matching items as 'id  [type] name: description'."""
        type_ = ItemType(item_type) if item_type else None
        items = (
            await hybrid.search(query, type_=type_, limit=10)
            if hybrid is not None
            else await index.search(query, type_=type_, limit=10)
        )
        if not items:
            return "(no library matches)"
        return "\n".join(f"{i.id}  [{i.type.value}] {i.name}: {i.description}" for i in items)

    @mcp.tool()
    async def get(item_id: str) -> str:
        """Read a Library item's full body by id (from search)."""
        item = await index.get_item(item_id)
        if item is None:
            return f"error: no library item {item_id}"
        if item.status is not ItemStatus.ACTIVE:
            return f"error: {item.name} is {item.status.value}; only active items are served"
        body = git.read_body(item)
        await index.record_use(item_id)
        return body or f"(item {item_id} has no body file)"

    @mcp.tool(name="contribute")
    async def contribute_tool(
        item_type: str,
        name: str,
        body: str,
        description: str = "",
        tags: list[str] | None = None,
        project: str | None = None,
        reusable_because: str | None = None,
        force: bool = False,
    ) -> str:
        """Contribute to the Library. Pick the type carefully:
        - skill: a GENERAL, REUSABLE technique you would apply again on a
          DIFFERENT task (e.g. "how to make a TUI look good", "how to write
          CliRunner tests with a tmp sqlite db"). Name it by the technique,
          not the task, and pass reusable_because: a real sentence on WHY it
          generalizes (not just asserting that it does). A skill with a
          missing, too-thin, or merely-asserted justification, or that reads
          as a recap of this task (e.g. "toyrepo add() planted-bug: one-line
          fix flips both tests green"), is saved as a report instead -- not
          dropped.
        - report: a finished artifact, or a one-off task summary/outcome.
        - memory: a single fact worth remembering.
        Tools are supply chain: they arrive proposed and wait for a human
        `rework library activate`.
        `force=True` is the human escape hatch (same as the CLI's --force):
        a human driving this MCP tool directly is never blocked by the
        generality gate -- use it only when a person, not an autonomous
        agent, is making the call."""
        try:
            type_ = ItemType(item_type)
        except ValueError:
            return "error: type must be skill|tool|report|memory"
        item, note = await contribute(
            index,
            git,
            type_=type_,
            name=name,
            description=description,
            body=body,
            tags=tags or [],
            project=project,
            reusable_because=reusable_because,
            force=force,
        )
        return f"{note}\n{item.id}"

    return mcp


def run_stdio() -> None:
    """Open the configured library, then serve MCP over stdio (the CLI entry).
    mcp.run() owns its own event loop, so the store opens under a separate
    asyncio.run and the server takes over from there."""
    import asyncio
    import os

    from rework.config import DEFAULT_STATE_DIR, Config
    from rework.library.store import open_library_store

    config = Config.from_env()
    root = Path(os.environ.get("REWORK_LIBRARY_DIR", str(DEFAULT_STATE_DIR / "library")))

    async def _open() -> tuple[LibraryStore, GitLibrary, Any]:
        from rework.library.embeddings import embeddings_from_env
        from rework.library.search import HybridSearch

        index = await open_library_store(config.db)
        embedder = embeddings_from_env()
        hybrid = HybridSearch(index, embedder) if embedder else None
        return index, GitLibrary(root, author=config.git_author), hybrid

    index, git, hybrid = asyncio.run(_open())
    mcp = build_mcp_server(index, git, hybrid=hybrid)
    mcp.run()  # stdio transport
