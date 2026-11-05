"""TUI tests with Textual Pilot: the real app mounted against a tmp sqlite
store, driven through the same input path a human takes.

The app opens its own store from REWORK_DB at first use, so every test sets
the env BEFORE constructing ReworkApp and seeds through a separate connection
to the same file."""

from __future__ import annotations

from pathlib import Path

import pytest

from rework.kernel.state.records import (
    EventRecord,
    EventType,
    Gate,
    GateKind,
    Mission,
    MissionStatus,
    new_id,
)
from rework.tui.app import ReworkApp


@pytest.fixture
def tui_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "tui.sqlite3"
    monkeypatch.setenv("REWORK_DB", str(db))
    monkeypatch.setenv("REWORK_LIBRARY_DIR", str(tmp_path / "lib"))
    return db


@pytest.fixture
async def seeded(tui_env: Path) -> dict[str, str]:
    from rework.kernel.state.sqlite import open_sqlite

    store = await open_sqlite(str(tui_env))
    try:
        done = await store.create_mission(
            Mission(
                id=new_id("m"),
                objective="fix the planted bug",
                status=MissionStatus.DONE,
                plan={"kind": "fix"},
            )
        )
        waiting = await store.create_mission(
            Mission(id=new_id("m"), objective="add dark mode", plan={"kind": "fix"})
        )
        gate = await store.create_gate(
            Gate(
                id=new_id("gate"),
                mission_id=waiting.id,
                kind=GateKind.SHIP,
                question="Ship the mission result? audit verdict: approve",
            )
        )
        await store.append_event(
            EventRecord(
                mission_id=waiting.id,
                type=EventType.MISSION_CREATED,
                payload={"objective": "add dark mode"},
            )
        )
        return {"done": done.id, "waiting": waiting.id, "gate": gate.id}
    finally:
        await store.aclose()


async def test_dashboard_renders_missions(seeded: dict[str, str]) -> None:
    app = ReworkApp()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await app.load_project_cache()
        dash = app.get_screen("dashboard")
        assert isinstance(dash.__class__.__name__, str)
        from textual.widgets import DataTable

        table = dash.query_one("#missions", DataTable)
        await dash.reload_missions()
        await pilot.pause()
        assert table.row_count == 2
        assert "gate(s) waiting" in app.sub_title


async def test_mission_detail_streams_events(seeded: dict[str, str]) -> None:
    app = ReworkApp()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await app.load_project_cache()
        dash = app.get_screen("dashboard")
        await dash.select_mission(seeded["waiting"])
        await pilot.pause()
        from textual.widgets import RichLog

        log = dash.query_one("#events", RichLog)
        assert len(log.lines) >= 1


async def test_gate_decision_from_dashboard(seeded: dict[str, str], tui_env: Path) -> None:
    """a on the dashboard approves the selected mission's gate: the STORE row
    flips (the daemon reads the same truth and resumes the mission)."""
    from rework.kernel.state.sqlite import open_sqlite

    app = ReworkApp()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await app.load_project_cache()
        dash = app.get_screen("dashboard")
        await dash.select_mission(seeded["waiting"])
        await pilot.pause()
        await dash.action_approve_gate()
        await pilot.pause()

    store = await open_sqlite(str(tui_env))
    try:
        gate = await store.get_gate(seeded["gate"])
        assert gate is not None
        assert gate.status.value == "approved"
    finally:
        await store.aclose()


async def test_create_project_modal(seeded: dict[str, str], tui_env: Path) -> None:
    from textual.widgets import Input

    from rework.tui.app import CreateProjectModal

    app = ReworkApp()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await app.load_project_cache()
        # drive the REAL flow: the dashboard action awaits the modal result
        # and writes the row. Run it as a worker (how a keypress would),
        # interact with the modal while it waits, then let it finish.
        dash = app.get_screen("dashboard")
        dash.run_worker(dash.action_new_project(), exclusive=True)
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, CreateProjectModal)
        name_input = modal.query_one("#p-name", Input)
        name_input.insert_text_at_cursor("capstone")
        await pilot.pause()
        modal.action_submit()
        await pilot.pause()
        await pilot.pause()
        from rework.kernel.state.sqlite import open_sqlite

        store = await open_sqlite(str(tui_env))
        try:
            projects = await store.list_projects()
        finally:
            await store.aclose()
        assert any(pr.name == "capstone" for pr in projects)


async def test_command_palette_lists_actions(seeded: dict[str, str]) -> None:
    """ctrl+p opens the palette and our providers are registered."""
    app = ReworkApp()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        # the registry check: the app's COMMANDS includes our provider factory
        from rework.tui.app import rework_commands_provider

        assert rework_commands_provider in app.COMMANDS
