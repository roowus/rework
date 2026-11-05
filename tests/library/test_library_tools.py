"""Curation tiers + role integration (M5.2)."""

from __future__ import annotations

from rework.library.curation import accept_status, contribute
from rework.library.model import (
    ItemStatus,
    ItemType,
    Origin,
)
from rework.library.store import GitLibrary
from rework.library.tools import (
    LibraryAwareExecutor,
    LibraryHandle,
    library_executor,
    with_library_menu,
)


def _handle(tmp_path, library_index) -> LibraryHandle:
    return LibraryHandle(index=library_index, git=GitLibrary(tmp_path / "lib"))


async def test_accept_status_table() -> None:
    assert accept_status(ItemType.SKILL) is ItemStatus.ACTIVE
    assert accept_status(ItemType.REPORT) is ItemStatus.ACTIVE
    assert accept_status(ItemType.MEMORY) is ItemStatus.ACTIVE
    assert accept_status(ItemType.TOOL) is ItemStatus.PROPOSED  # supply chain
    assert accept_status(ItemType.SKILL, Origin.EXTERNAL) is ItemStatus.PROPOSED


async def test_contribute_gates_tools(tmp_path, library_index) -> None:
    handle = _handle(tmp_path, library_index)
    skill, note1 = await contribute(
        handle.index,
        handle.git,
        type_=ItemType.SKILL,
        name="toy recipe",
        description="d",
        body="# x",
        tags=["t"],
        mission_id="m-1",
        role="coder",
    )
    assert skill.status is ItemStatus.ACTIVE
    assert "proposed" not in note1

    tool, note2 = await contribute(
        handle.index,
        handle.git,
        type_=ItemType.TOOL,
        name="deploy script",
        description="d",
        body="#!/bin/sh",
        tags=[],
        mission_id="m-1",
        role="coder",
    )
    assert tool.status is ItemStatus.PROPOSED
    assert "owner must run" in note2
    # git shows the contribution
    assert handle.git.last_commit(tool) is not None


class _StubInner:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def execute(self, name: str, args: dict) -> str:
        self.calls.append(name)
        return f"inner:{name}"

    def specs(self, allowed=None):
        from rework.nodes.tools import BUILTIN_TOOLS

        return list(BUILTIN_TOOLS)


async def test_library_aware_executor(tmp_path, library_index) -> None:
    handle = _handle(tmp_path, library_index)
    await contribute(
        handle.index,
        handle.git,
        type_=ItemType.SKILL,
        name="planted bug recipe",
        description="how to fix the toyrepo",
        body="run pytest, flip the sign",
        tags=["toyrepo"],
    )
    inner = _StubInner()
    ex = LibraryAwareExecutor(inner, handle, "m-1", "m-1:code:coder:1", "coder")

    # specs gain the library tools
    names = {s.name for s in ex.specs()}
    assert {"library_search", "library_read", "library_contribute"} <= names

    # search finds and records use
    out = await ex.execute("library_search", {"query": "toyrepo"})
    assert "planted bug recipe" in out

    # read serves the body
    items = await handle.index.search("toyrepo")
    body = await ex.execute("library_read", {"item_id": items[0].id})
    assert "flip the sign" in body

    # contribute lands in the index (skill auto-accepts)
    out = await ex.execute(
        "library_contribute",
        {"type": "memory", "name": "lesson", "description": "x", "body": "remember"},
    )
    assert "contributed" in out

    # a contributed tool stays proposed and is NOT readable via library_read
    out = await ex.execute(
        "library_contribute",
        {"type": "tool", "name": "risky", "description": "x", "body": "rm -rf /"},
    )
    assert "proposed" in out
    tool_id = (await handle.index.list_items(type_=ItemType.TOOL))[0].id
    assert "proposed" in await ex.execute("library_read", {"item_id": tool_id})

    # everything else delegates untouched
    assert await ex.execute("read_file", {"path": "x"}) == "inner:read_file"
    assert inner.calls == ["read_file"]


async def test_menu_lists_active_skills_only(tmp_path, library_index) -> None:
    handle = _handle(tmp_path, library_index)
    await contribute(
        handle.index,
        handle.git,
        type_=ItemType.SKILL,
        name="active skill",
        description="useful",
        body="b",
        tags=[],
    )
    await contribute(
        handle.index,
        handle.git,
        type_=ItemType.TOOL,
        name="gated tool",
        description="nope",
        body="b",
        tags=[],
    )
    menu = await handle.menu()
    assert "active skill" in menu
    assert "gated tool" not in menu
    assert "Active library skills" in menu


def test_wrappers_identity_when_library_off() -> None:
    class _Ctx:
        library = None
        library_menu = ""

    inner = _StubInner()
    assert library_executor(_Ctx(), inner) is inner
    assert with_library_menu(_Ctx(), "prompt") == "prompt"


def test_wrappers_active_when_library_on(tmp_path, library_index) -> None:
    class _Run:
        role = "coder"
        id = "m-1:code:coder:1"

    class _Mission:
        id = "m-1"

    class _Ctx:
        library = _handle(tmp_path, library_index)
        library_menu = "Active library skills:\n- x"
        run = _Run()
        mission = _Mission()

    wrapped = library_executor(_Ctx(), _StubInner())
    assert isinstance(wrapped, LibraryAwareExecutor)
    assert "Active library skills" in with_library_menu(_Ctx(), "prompt")


async def test_search_serves_active_only(tmp_path, library_index) -> None:
    """Retired items vanish from search (a serving path), not from ls."""
    handle = _handle(tmp_path, library_index)
    item, _ = await contribute(
        handle.index,
        handle.git,
        type_=ItemType.SKILL,
        name="old trick",
        description="obsolete",
        body="b",
        tags=["old"],
    )
    assert "old trick" in await handle.search_text("old")
    item.status = ItemStatus.RETIRED
    item.touch()
    await handle.index.upsert_item(item)
    assert "no library matches" in await handle.search_text("old")
    assert (await handle.index.list_items())[0].name == "old trick"  # ls still shows it


async def test_multiword_natural_search(tmp_path, library_index) -> None:
    """Agents search like they talk: 'toyrepo planted bug' must hit an item
    whose name carries those words (found live: the coder's multi-word
    library_search returned nothing under whole-query substring rules)."""
    handle = _handle(tmp_path, library_index)
    await contribute(
        handle.index,
        handle.git,
        type_=ItemType.SKILL,
        name="toyrepo planted bug recipe",
        description="one-line add() fix",
        body="b",
        tags=[],
    )
    out = await handle.search_text("toyrepo planted bug")
    assert "toyrepo planted bug recipe" in out
    out = await handle.search_text("how do I fix the toyrepo?")
    assert "toyrepo planted bug recipe" in out
