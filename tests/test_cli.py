"""CLI tests: the M0 smoke (entrypoint imports, --help, version) plus the
mission commands (`new`, `ls`, `gates`, `report`) driven through typer's
CliRunner against a throwaway sqlite DB.

Every CLI command body calls `asyncio.run(...)`, so these tests are plain sync
functions: invoking CliRunner from inside an `async def` test dies with
"asyncio.run() cannot be called from a running event loop" (pytest-asyncio is
in auto mode). The `rework_db` fixture below is autouse precisely because a
command that runs without REWORK_DB set would read and write the developer's
real ~/.rework/state.sqlite3.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rework import __version__
from rework.cli import app
from rework.kernel.state.records import Gate, GateKind, GateStatus, Mission, MissionStatus
from rework.kernel.state.sqlite import open_sqlite

runner = CliRunner()


@pytest.fixture(autouse=True)
def rework_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point REWORK_DB at a tmp sqlite file for every test in this module.

    Nothing else sets it (tests/conftest.py has no autouse REWORK_DB fixture),
    so without this the commands would hit the real ~/.rework/state.sqlite3.
    """
    db = tmp_path / "state.sqlite3"
    monkeypatch.setenv("REWORK_DB", str(db))
    return db


def _seed_mission(db: Path, *, mission_id: str, objective: str) -> None:
    async def go() -> None:
        store = await open_sqlite(str(db))
        try:
            await store.create_mission(
                Mission(id=mission_id, objective=objective, status=MissionStatus.PENDING)
            )
        finally:
            await store.aclose()

    asyncio.run(go())


def _seed_gate(db: Path, gate: Gate) -> None:
    async def go() -> None:
        store = await open_sqlite(str(db))
        try:
            await store.create_gate(gate)
        finally:
            await store.aclose()

    asyncio.run(go())


def _get_mission(db: Path, mission_id: str) -> Mission | None:
    async def go() -> Mission | None:
        store = await open_sqlite(str(db))
        try:
            return await store.get_mission(mission_id)
        finally:
            await store.aclose()

    return asyncio.run(go())


def test_help_exits_zero() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "rework" in result.output


def test_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_new_accepts_git_url_without_local_path(rework_db: Path) -> None:
    """A --repo that is a git URL is an origin for the daemon to clone: `new`
    must never stat it as a local path (the URL points at no local directory)."""
    url = "https://github.com/example/repo.git"
    result = runner.invoke(app, ["new", "fix the planted bug", "--repo", url, "--plan", "fix"])

    assert result.exit_code == 0, result.output
    assert "repo not found" not in result.output
    mission_id = result.output.strip()
    assert mission_id.startswith("m-")

    mission = _get_mission(rework_db, mission_id)
    assert mission is not None
    assert mission.repo_origin == url  # the daemon clones this itself
    assert mission.repo_path is None  # nothing was resolved on the local filesystem


def test_new_accepts_git_url_without_plan(rework_db: Path) -> None:
    url = "git@github.com:example/repo.git"
    result = runner.invoke(app, ["new", "plain mission", "--repo", url])

    assert result.exit_code == 0, result.output
    assert "repo not found" not in result.output
    mission = _get_mission(rework_db, result.output.strip())
    assert mission is not None
    assert mission.repo_origin == url
    assert mission.repo_path is None


def test_ls_output_format(rework_db: Path) -> None:
    """`ls` prints `{id}  {status:<14}  {objective}` (two spaces, status
    left-justified to width 14)."""
    _seed_mission(rework_db, mission_id="m-abc123", objective="fix the planted bug")

    result = runner.invoke(app, ["ls"])

    assert result.exit_code == 0, result.output
    assert result.output == "m-abc123  pending         fix the planted bug\n"


def test_ls_on_empty_db_prints_nothing(rework_db: Path) -> None:
    result = runner.invoke(app, ["ls"])
    assert result.exit_code == 0, result.output
    assert result.output == ""


def test_gates_with_no_open_gates(rework_db: Path) -> None:
    """A decided gate is not open, so `gates` reports none."""
    _seed_mission(rework_db, mission_id="m-abc123", objective="fix the planted bug")
    _seed_gate(
        rework_db,
        Gate(
            id="gate-decided",
            mission_id="m-abc123",
            kind=GateKind.SHIP,
            question="Ship the mission result?",
            status=GateStatus.APPROVED,
        ),
    )

    result = runner.invoke(app, ["gates"])

    assert result.exit_code == 0, result.output
    assert result.output == "no open gates\n"


def test_gates_lists_open_gate_for_mission(rework_db: Path) -> None:
    """`--mission-id` is an option (not a positional argument)."""
    _seed_mission(rework_db, mission_id="m-abc123", objective="fix the planted bug")
    _seed_gate(
        rework_db,
        Gate(
            id="gate-xyz",
            mission_id="m-abc123",
            kind=GateKind.SHIP,
            question="Ship the mission result?",
        ),
    )

    result = runner.invoke(app, ["gates", "--mission-id", "m-abc123"])

    assert result.exit_code == 0, result.output
    assert result.output == "gate-xyz  [ship]  Ship the mission result?\n"


def test_report_missing_mission_exits_cleanly(rework_db: Path) -> None:
    result = runner.invoke(app, ["report", "m-nope"])

    assert result.exit_code == 1
    assert "no such mission: m-nope" in result.output
    assert "Traceback" not in result.output


def test_report_missing_mission_md_flag(rework_db: Path) -> None:
    result = runner.invoke(app, ["report", "m-nope", "--md"])

    assert result.exit_code == 1
    assert "no such mission: m-nope" in result.output
    assert "Traceback" not in result.output
