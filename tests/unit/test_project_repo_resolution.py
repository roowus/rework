"""A mission inherits its project's repo (the chat usually names only the
project), and project refs that are URLs become origins the daemon clones."""

from __future__ import annotations

from pathlib import Path

import pytest

from rework.kernel.state.records import Project
from rework.missions import create_mission

URL = "https://github.com/roowus/rework"


@pytest.fixture
async def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    path = str(tmp_path / "s.sqlite3")
    monkeypatch.setenv("REWORK_DB", path)
    return path


async def _project(db: str, repos: list[dict[str, str]]) -> str:
    from rework.kernel.state.sqlite import open_sqlite

    store = await open_sqlite(db)
    try:
        p = await store.create_project(Project(name="rework", settings={"repos": repos}))
        return p.id
    finally:
        await store.aclose()


async def test_project_only_mission_uses_the_projects_single_repo(db: str) -> None:
    pid = await _project(db, [{"name": "rework", "kind": "git", "url": URL}])
    mission = await create_mission("fix the dashboard", plan="fix-pr", project_id=pid)
    assert mission.repo_origin == URL, "a URL ref is an origin the daemon clones"
    assert mission.repo_path is None


async def test_named_ref_with_git_kind_is_an_origin_not_a_path(db: str) -> None:
    pid = await _project(db, [{"name": "rework", "kind": "git", "url": URL}])
    mission = await create_mission("x", plan="fix", project_id=pid, repo="rework")
    assert mission.repo_origin == URL and mission.repo_path is None


async def test_several_repos_and_no_name_is_ambiguous(db: str) -> None:
    pid = await _project(
        db,
        [
            {"name": "api", "kind": "git", "url": URL},
            {"name": "web", "kind": "git", "url": URL + "-web"},
        ],
    )
    with pytest.raises(ValueError, match="several repos"):
        await create_mission("x", plan="fix", project_id=pid)


async def test_project_without_repos_still_needs_one_for_fix(db: str) -> None:
    pid = await _project(db, [])
    with pytest.raises(ValueError, match="requires a repo"):
        await create_mission("x", plan="fix", project_id=pid)
    echo = await create_mission("x", plan="echo", project_id=pid)
    assert echo.repo_origin is None and echo.repo_path is None


async def test_resolve_project_accepts_a_name(db: str) -> None:
    from rework.missions import resolve_project

    pid = await _project(db, [])
    assert await resolve_project("rework") == pid
    assert await resolve_project(pid) == pid
    with pytest.raises(ValueError, match="project not found"):
        await resolve_project("nope")


async def test_a_mission_on_a_project_repo_is_labeled_automatically(db: str) -> None:
    """Owner: use project labels correctly. A mission created with only the
    repo URL (old CLI habit) still lands in the project owning that repo."""
    pid = await _project(db, [{"name": "rework", "kind": "git", "url": URL}])
    for spelling in (URL, URL + ".git", URL + "/", "git@github.com:roowus/rework.git"):
        mission = await create_mission("x", plan="fix", repo=spelling)
        assert mission.project_id == pid, spelling
    other = await create_mission("y", plan="fix", repo="https://github.com/someone/else")
    assert other.project_id is None


def test_project_assign_relabels_missions(db: str) -> None:
    import asyncio

    from typer.testing import CliRunner

    from rework.cli import app
    from rework.kernel.state.records import Mission
    from rework.kernel.state.sqlite import open_sqlite

    async def setup() -> str:
        pid = await _project(db, [])
        store = await open_sqlite(db)
        try:
            await store.create_mission(Mission(id="m-old1", objective="old"))
        finally:
            await store.aclose()
        return pid

    pid = asyncio.run(setup())
    result = CliRunner().invoke(app, ["project", "assign", "rework", "m-old1"])
    assert result.exit_code == 0, result.output

    async def check() -> str | None:
        store = await open_sqlite(db)
        try:
            m = await store.get_mission("m-old1")
            return m.project_id if m else None
        finally:
            await store.aclose()

    assert asyncio.run(check()) == pid
