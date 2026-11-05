"""Library MCP server (M5.3): a REAL stdio round trip, exactly the mount path
an MCP client takes (initialize -> list_tools -> call_tool)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from rework.library.curation import contribute
from rework.library.model import ItemType
from rework.library.store import GitLibrary, open_library_store


@pytest.fixture
def mcp_env(tmp_path: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["REWORK_DB"] = str(tmp_path / "state.sqlite3")
    env["REWORK_LIBRARY_DIR"] = str(tmp_path / "lib")
    return env


async def _seed(env: dict[str, str]) -> None:
    index = await open_library_store(env["REWORK_DB"])
    git = GitLibrary(Path(env["REWORK_LIBRARY_DIR"]))
    try:
        await contribute(
            index,
            git,
            type_=ItemType.SKILL,
            name="toy recipe",
            description="how to fix the toyrepo",
            body="run pytest, flip the sign",
            tags=["toyrepo"],
        )
    finally:
        await index.aclose()


async def test_mcp_stdio_round_trip(tmp_path: Path, mcp_env: dict[str, str]) -> None:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    await _seed(mcp_env)
    rework_bin = Path(sys.executable).parent / "rework"
    if not rework_bin.exists():  # CI may invoke via a different venv layout
        pytest.skip("rework entrypoint not found next to the interpreter")
    params = StdioServerParameters(command=str(rework_bin), args=["library-mcp"], env=mcp_env)

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert {"search", "get", "contribute"} <= {t.name for t in tools.tools}

            # search over the SEEDED library (same DB the server opened)
            res = await session.call_tool("search", {"query": "toyrepo"})
            text = res.content[0].text
            assert "toy recipe" in text
            item_id = text.split()[0]

            # get serves the body
            res = await session.call_tool("get", {"item_id": item_id})
            assert "flip the sign" in res.content[0].text

            # contribute applies curation: tools arrive proposed, not active
            res = await session.call_tool(
                "contribute",
                {"item_type": "tool", "name": "risky script", "body": "echo hi"},
            )
            assert "proposed" in res.content[0].text

            # a retired/unknown id reads as an error, not silence
            res = await session.call_tool("get", {"item_id": "l-does-not-exist"})
            assert "error" in res.content[0].text
