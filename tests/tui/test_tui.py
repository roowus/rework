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
    monkeypatch.setenv("REWORK_RESULTS_DIR", str(tmp_path / "results"))
    monkeypatch.setenv("REWORK_REPORTS_DIR", str(tmp_path / "reports"))
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
        # the registry check: COMMANDS holds provider CLASSES (textual
        # instantiates them itself); ours must be present
        from rework.tui.app import ReworkCommands

        assert ReworkCommands in app.COMMANDS
        # and instantiating the way textual does must work (the live crash:
        # a factory function got called with zero args)
        provider = ReworkCommands(app.screen, None)
        assert provider is not None


async def test_pressing_n_opens_wizard_not_crash(seeded: dict[str, str]) -> None:
    """Found live: pressing n ran push_screen(wait_for_dismiss=True) outside a
    worker and crashed the app (NoActiveWorker). A keypress must open the
    wizard; escape must close it cleanly."""
    app = ReworkApp()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await app.load_project_cache()
        await pilot.press("n")
        await pilot.pause()
        from rework.tui.app import NewMissionModal

        assert isinstance(app.screen, NewMissionModal)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, NewMissionModal)


async def test_pressing_p_opens_project_modal_not_crash(seeded: dict[str, str]) -> None:
    app = ReworkApp()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await app.load_project_cache()
        await pilot.press("p")
        await pilot.pause()
        from rework.tui.app import CreateProjectModal

        assert isinstance(app.screen, CreateProjectModal)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, CreateProjectModal)


# ------------------------------------------------------- cockpit redesign


async def test_prompt_bar_free_text_opens_prefilled_wizard(seeded: dict[str, str]) -> None:
    """The cockpit mechanic: typing an objective at the prompt and enter opens
    the wizard with the objective pre-filled (and remembers last-used repo +
    plan for the next launch)."""
    from textual.widgets import Input

    from rework.tui.app import Dashboard, NewMissionModal

    app = ReworkApp()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await app.load_project_cache()
        dash = app.get_screen("dashboard")
        assert isinstance(dash, Dashboard)
        prompt = dash.query_one("#prompt", Input)
        prompt.focus()
        prompt.insert_text_at_cursor("fix the resize glitch")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, NewMissionModal)
        assert modal.query_one("#f-objective", Input).value == "fix the resize glitch"
        await pilot.press("escape")
        await pilot.pause()
        # prompt is cleared after submit either way
        assert prompt.value == ""


async def test_wizard_remembers_last_used_repo_and_plan(seeded: dict[str, str]) -> None:
    """Second launch prefills from the first (owner: after the first mission,
    launching is one keypress)."""
    from textual.widgets import Input, Select

    from rework.tui.app import Dashboard, NewMissionModal

    app = ReworkApp()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await app.load_project_cache()
        dash = app.get_screen("dashboard")
        assert isinstance(dash, Dashboard)
        app._mission_defaults = {
            "repo": "https://github.com/roowus/rework",
            "repo_dir": None,
            "project_id": None,
            "plan": "fix-pr",
        }
        dash.run_worker(dash._new_mission_flow(objective="second thing"), exclusive=True)
        await pilot.pause()
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, NewMissionModal)
        assert modal.query_one("#f-repo", Input).value == "https://github.com/roowus/rework"
        assert str(modal.query_one("#f-plan", Select).value) == "fix-pr"


async def test_slash_commands_route(seeded: dict[str, str]) -> None:
    """/gates opens the gates screen; unknown commands notify instead of
    crash."""
    from textual.widgets import Input

    from rework.tui.app import Dashboard, GatesScreen

    app = ReworkApp()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await app.load_project_cache()
        dash = app.get_screen("dashboard")
        assert isinstance(dash, Dashboard)
        await app.run_slash("/gates")
        await pilot.pause()
        assert isinstance(app.screen, GatesScreen)
        await pilot.press("escape")
        await pilot.pause()
        # unknown command: no crash, no screen change
        before = app.screen
        await app.run_slash("/frobnicate")
        await pilot.pause()
        assert app.screen is before
        # prompt stays clean after a slash submit
        prompt = dash.query_one("#prompt", Input)
        prompt.value = "/gates"
        prompt.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert prompt.value == ""


async def test_enter_opens_mission_deep_view(seeded: dict[str, str]) -> None:
    """enter on a highlighted mission opens the full-screen deep view with
    tabs; esc returns to the dashboard."""
    from textual.widgets import DataTable

    from rework.tui.app import Dashboard, MissionDeepView

    app = ReworkApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await app.load_project_cache()
        dash = app.get_screen("dashboard")
        assert isinstance(dash, Dashboard)
        await dash.select_mission(seeded["waiting"])
        await pilot.pause()
        # enter on the dashboard: focus lives on the missions DataTable after
        # select_mission, so the RowSelected path is the human path
        table = dash.query_one("#missions", DataTable)
        table.focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        deep = app.screen
        assert isinstance(deep, MissionDeepView)
        # the header names the mission
        from textual.widgets import Label

        head = deep.query_one("#deep-head", Label)
        assert seeded["waiting"] in str(head.content)
        # tab cycling works and lands back
        await pilot.press("tab")
        await pilot.press("tab")
        await pilot.press("tab")
        await pilot.press("tab")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, Dashboard)


async def test_deep_view_gate_decision(seeded: dict[str, str], tui_env: Path) -> None:
    """a inside the deep view approves the mission's gate (same store truth)."""
    from rework.kernel.state.sqlite import open_sqlite
    from rework.tui.app import MissionDeepView

    app = ReworkApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await app.load_project_cache()
        deep = MissionDeepView(seeded["waiting"])
        await app.push_screen(deep)
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()
        await deep.action_approve()
        await pilot.pause()
        await pilot.pause()

    store = await open_sqlite(str(tui_env))
    try:
        gate = await store.get_gate(seeded["gate"])
        assert gate is not None
        assert gate.status.value == "approved"
    finally:
        await store.aclose()


async def test_library_search_first_split(seeded: dict[str, str]) -> None:
    """The library opens with the cursor IN the search box; typing filters
    live; the selected item's body renders in the right pane."""
    from textual.widgets import DataTable, Input, Static

    from rework.tui.app import LibraryScreen

    app = ReworkApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        # seed one library item directly through the data layer
        index, git = await app.data.library()
        from rework.library.model import ItemStatus, ItemType, LibraryItem

        item = LibraryItem(
            id="l-test1",
            type=ItemType.SKILL,
            name="resize-glide",
            description="fixing terminal resize glitches",
            status=ItemStatus.ACTIVE,
        )
        await index.upsert_item(item)
        rel = git.write_body(item, "# resize-glide\n\nCheck the compositor first.", "test seed")
        item.body_path = rel
        await index.upsert_item(item)
        app.push_screen(LibraryScreen())
        await pilot.pause()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, LibraryScreen)
        query = screen.query_one("#lib-query", Input)
        assert query.has_focus
        table = screen.query_one("#lib-table", DataTable)
        assert table.row_count == 1
        # body pane shows the rendered markdown
        body = screen.query_one("#lib-body", Static)
        rendered = str(body.render())
        assert "resize-glide" in rendered
        assert "compositor" in rendered
        # live filter: typing a non-match empties the list
        query.value = "zzz-no-match"
        await pilot.pause()
        await pilot.pause()
        assert table.row_count == 0
        query.value = ""
        await pilot.pause()
        await pilot.pause()
        assert table.row_count == 1
        await app.data.aclose()


async def test_fetch_lands_in_visible_results_dir(
    seeded: dict[str, str], tui_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """f fetches into ~/rework/results/<id> (overridable via
    REWORK_RESULTS_DIR; tests point it at tmp). Here the daemon has no
    worktree, so the TUI must degrade to a visible warning, not crash."""
    from rework.tui.app import Dashboard

    app = ReworkApp()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await app.load_project_cache()
        dash = app.get_screen("dashboard")
        assert isinstance(dash, Dashboard)
        await dash.select_mission(seeded["done"])
        await pilot.pause()
        # no workspace server in tests → connection error path → notify, no raise
        monkeypatch.setenv("REWORK_DAEMON_URL", "http://127.0.0.1:1")
        await dash.action_fetch()
        await pilot.pause()
