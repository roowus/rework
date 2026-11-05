"""LibraryStore conformance — both backends must pass this suite unchanged."""

from __future__ import annotations

from rework.library.model import (
    ItemStatus,
    ItemType,
    LibraryItem,
    Origin,
    Provenance,
)
from rework.library.store import GitLibrary


def _item(name: str = "toyrepo fix recipe", **kw) -> LibraryItem:
    return LibraryItem(
        type=kw.get("type", ItemType.SKILL),
        name=name,
        description=kw.get("description", "how to fix a planted bug in the toyrepo"),
        tags=kw.get("tags", []),
        provenance=kw.get(
            "provenance", Provenance(origin=Origin.INTERNAL, mission_id="m-abc", role="coder")
        ),
        status=kw.get("status", ItemStatus.ACTIVE),
    )


async def test_roundtrip_and_update(library_index) -> None:
    item = _item(tags=["toyrepo", "pytest"])
    stored = await library_index.upsert_item(item)
    assert stored.id == item.id

    got = await library_index.get_item(item.id)
    assert got is not None
    assert got.name == "toyrepo fix recipe"
    assert got.provenance.mission_id == "m-abc"
    assert got.tags == ["toyrepo", "pytest"]
    assert got.status == ItemStatus.ACTIVE
    assert await library_index.get_item("l-missing") is None

    # update: bump status + version, keep identity
    item.status = ItemStatus.RETIRED
    item.touch()
    await library_index.upsert_item(item)
    got2 = await library_index.get_item(item.id)
    assert got2 is not None
    assert got2.status == ItemStatus.RETIRED
    assert got2.version == 2


async def test_list_and_filters(library_index) -> None:
    await library_index.upsert_item(_item("skill one"))
    await library_index.upsert_item(
        _item("tool one", type=ItemType.TOOL, status=ItemStatus.PROPOSED)
    )
    await library_index.upsert_item(_item("skill two"))

    active = await library_index.list_items(status=ItemStatus.ACTIVE)
    assert {i.name for i in active} == {"skill one", "skill two"}
    tools = await library_index.list_items(type_=ItemType.TOOL)
    assert [i.name for i in tools] == ["tool one"]
    everything = await library_index.list_items()
    assert len(everything) == 3


async def test_search_ranking(library_index) -> None:
    await library_index.upsert_item(_item("toyrepo fix recipe", tags=["toyrepo"]))
    await library_index.upsert_item(
        _item("drive fetcher", description="pull files from google drive")
    )
    await library_index.upsert_item(
        _item(
            "pytest patterns",
            description="collection of pytest idioms for small repos",
            tags=["pytest", "testing"],
        )
    )

    hits = await library_index.search("toyrepo")
    assert hits[0].name == "toyrepo fix recipe"
    # matches come from name, tag, or description; non-matching items stay out
    assert {h.name for h in await library_index.search("pytest")} == {"pytest patterns"}
    assert {h.name for h in await library_index.search("drive")} == {"drive fetcher"}
    assert {h.name for h in await library_index.search("toyrepo")} == {"toyrepo fix recipe"}
    assert await library_index.search("zzz-nothing") == []

    # uses counter
    await library_index.record_use(hits[0].id)
    got = await library_index.get_item(hits[0].id)
    assert got is not None and got.uses == 1


def test_git_library_commits(tmp_path) -> None:
    import subprocess

    lib = GitLibrary(tmp_path / "lib")
    item = _item("toyrepo fix recipe")
    rel = lib.write_body(item, "# recipe\nrun pytest\n", "library: add skill")
    assert rel == "skills/toyrepo-fix-recipe.md"
    item.body_path = rel  # callers persist the returned path on the item
    body = lib.read_body(item)
    assert body is not None and "pytest" in body
    log = subprocess.run(["git", "log", "--oneline"], cwd=lib.root, capture_output=True, text=True)
    assert "library: add skill" in log.stdout
    # a second write bumps the file in place (same body path)
    item.touch()
    lib.write_body(item, "# recipe v2\n", "library: update skill")
    assert "v2" in (lib.read_body(item) or "")
    assert lib.last_commit(item) is not None
