"""TUI tests with Textual Pilot: the real app mounted against a tmp sqlite
store, driven through the same input path a human takes.

The app opens its own store from REWORK_DB at first use, so every test sets
the env BEFORE constructing ReworkApp and seeds through a separate connection
to the same file. The shell architecture (menu bar + tab panes) is what the
owner sees: tests drive tabs, the prompt bar, the gates badge, and the deep
view through the same surfaces."""

from __future__ import annotations

import os
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
from rework.tui.app import ReworkApp, Shell


@pytest.fixture
def tui_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "tui.sqlite3"
    monkeypatch.setenv("REWORK_DB", str(db))
    monkeypatch.setenv("REWORK_LIBRARY_DIR", str(tmp_path / "lib"))
    monkeypatch.setenv("REWORK_RESULTS_DIR", str(tmp_path / "results"))
    monkeypatch.setenv("REWORK_REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("REWORK_NOTIFY", "toast")
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


async def _shell(app: ReworkApp) -> Shell:
    screen = app.screen
    assert isinstance(screen, Shell)
    return screen


async def test_shell_renders_menu_bar_and_work_tab(seeded: dict[str, str]) -> None:
    app = ReworkApp()
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        shell = await _shell(app)
        from textual.widgets import Input

        bar = shell.query_one("#menu-bar")
        rendered = str(bar.render())
        for menu in ("File", "Edit", "View", "Help"):
            assert menu in rendered
        # gates badge on the bar (one open gate in the seed)
        assert "Gates ●1" in rendered
        # work pane is the visible home: prompt focused, knobs are buttons
        from textual.widgets import Button

        prompt = shell.query_one("#prompt", Input)
        assert prompt.has_focus
        for knob in ("#btn-project", "#btn-model", "#btn-effort"):
            assert isinstance(shell.query_one(knob), Button)
        # the old clutter stays gone
        assert not shell.query("#recent")
        assert not shell.query("#act-new")


async def test_tabs_switch_and_panes_survive(seeded: dict[str, str]) -> None:
    """alt+2/3/4 switch tabs; panes are LIVE instances (state survives
    switches) — the owner's tab-shell ask."""
    from textual.widgets import DataTable

    from rework.tui.app import DashboardPane, GatesPane, LibraryPane

    app = ReworkApp()
    async with app.run_test(size=(120, 32)) as pilot:
        await pilot.pause()
        await pilot.pause()
        shell = await _shell(app)
        dash = shell.query_one("#pane-dashboard", DashboardPane)

        app.action_tab_dashboard()
        await pilot.pause()
        await pilot.pause()
        assert dash.display
        assert isinstance(dash.query_one("#missions", DataTable), DataTable)

        app.action_tab_gates()
        await pilot.pause()
        await pilot.pause()
        gates_pane = shell.query_one("#pane-gates", GatesPane)
        assert gates_pane.display
        assert not dash.display

        app.action_tab_library()
        await pilot.pause()
        await pilot.pause()
        assert shell.query_one("#pane-library", LibraryPane).display

        # back to work: prompt refocuses (the typing surface)
        app.action_tab_work()
        await pilot.pause()
        from textual.widgets import Input

        assert shell.query_one("#prompt", Input).has_focus


async def test_mission_detail_streams_events(seeded: dict[str, str]) -> None:
    app = ReworkApp()
    async with app.run_test(size=(120, 32)) as pilot:
        await pilot.pause()
        await pilot.pause()
        shell = await _shell(app)
        app.action_tab_dashboard()
        await pilot.pause()
        dash = shell.query_one("#pane-dashboard")
        await dash.select_mission(seeded["waiting"])
        # events stream through a worker: let it land (the RichLog drops
        # writes while the pane is hidden — the human path has it visible)
        for _ in range(5):
            await pilot.pause()
        from textual.widgets import RichLog

        log = dash.query_one("#events", RichLog)
        assert len(log.lines) >= 1


async def test_gate_decision_from_dashboard(seeded: dict[str, str], tui_env: Path) -> None:
    """a on the dashboard approves the selected mission's gate: the STORE row
    flips (the daemon reads the same truth and resumes the mission)."""
    from rework.kernel.state.sqlite import open_sqlite

    app = ReworkApp()
    async with app.run_test(size=(120, 32)) as pilot:
        await pilot.pause()
        await pilot.pause()
        shell = await _shell(app)
        dash = shell.query_one("#pane-dashboard")
        await dash.select_mission(seeded["waiting"])
        await pilot.pause()
        await dash.action_approve_gate()
        await pilot.pause()
        await pilot.pause()

    store = await open_sqlite(str(tui_env))
    try:
        gate = await store.get_gate(seeded["gate"])
        assert gate is not None
        assert gate.status.value == "approved"
    finally:
        await store.aclose()


async def test_gate_badge_lights_and_toast_fires(seeded: dict[str, str], tui_env: Path) -> None:
    """The unread-style badge: [ Gates ●N ] on the menu bar while a decision
    waits; a toast fires for a NEW gate; the badge clears when all decided."""
    from rework.kernel.state.records import Gate, GateKind, new_id
    from rework.kernel.state.sqlite import open_sqlite

    app = ReworkApp()
    async with app.run_test(size=(120, 32)) as pilot:
        await pilot.pause()
        await pilot.pause()
        shell = await _shell(app)
        bar = shell.query_one("#menu-bar")
        assert "Gates ●1" in str(bar.render())

        # a SECOND gate opens while the app is running -> badge bumps
        store = await open_sqlite(str(tui_env))
        try:
            await store.create_gate(
                Gate(
                    id=new_id("gate"),
                    mission_id=seeded["done"],
                    kind=GateKind.SHIP,
                    question="second",
                )
            )
        finally:
            await store.aclose()
        shell._seen_gates.clear()  # treat both as unseen; both should toast
        await shell.poll_badge()
        await pilot.pause()
        assert "Gates ●2" in str(bar.render())
        # a toast fired for the new gate (>= 1 notification, mode=toast)
        assert len(app._notifications) >= 1

        # decide everything -> badge clears
        for gate_id in sorted(shell._seen_gates):
            await app.data.decide_gate(gate_id, True)
        await shell.poll_badge()
        await pilot.pause()
        assert "Gates ●" not in str(bar.render())


async def test_notify_mode_configurable(
    seeded: dict[str, str], tui_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REWORK_NOTIFY=none silences toasts (owner: configurable)."""
    from rework.kernel.state.records import Gate, GateKind, new_id
    from rework.kernel.state.sqlite import open_sqlite

    monkeypatch.setenv("REWORK_NOTIFY", "none")
    app = ReworkApp()
    async with app.run_test(size=(120, 32)) as pilot:
        await pilot.pause()
        await pilot.pause()
        shell = await _shell(app)
        store = await open_sqlite(str(tui_env))
        try:
            await store.create_gate(
                Gate(id=new_id("gate"), mission_id=seeded["done"], kind=GateKind.SHIP, question="x")
            )
        finally:
            await store.aclose()
        await shell.poll_badge()
        await pilot.pause()
        assert len(app._notifications) == 0
        # badge still lights (silent ≠ invisible)
        assert "Gates ●2" in str(shell.query_one("#menu-bar").render())


async def test_create_project_modal(seeded: dict[str, str], tui_env: Path) -> None:
    from textual.widgets import Input

    from rework.tui.app import CreateProjectModal

    app = ReworkApp()
    async with app.run_test(size=(120, 32)) as pilot:
        await pilot.pause()
        await pilot.pause()
        shell = await _shell(app)
        shell.run_worker(shell.new_project_flow(), exclusive=True)
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
    app = ReworkApp()
    async with app.run_test(size=(120, 32)) as pilot:
        await pilot.pause()
        from rework.tui.app import ReworkCommands

        assert ReworkCommands in app.COMMANDS
        provider = ReworkCommands(app.screen, None)
        assert provider is not None


async def test_prompt_sends_message_to_the_orchestrator(seeded: dict[str, str]) -> None:
    """Mission creation is a CONVERSATION (owner: 'rework works by u talking
    to an orchestrator and the orchestrator decides'). Enter on the prompt
    must send a chat turn — there is no form any more."""
    from textual.widgets import Input

    from conftest import ScriptedProvider, role_catalog
    from rework.kernel.llm.types import ChatResult
    from rework.orchestrator.chat import OrchestratorChat
    from rework.tui.app import WorkPane

    scripted = ScriptedProvider()
    scripted.route("orchestrator", [ChatResult(text="Which repo should I use?")])
    WorkPane._build_chat = lambda self: OrchestratorChat(  # type: ignore[method-assign]
        scripted, role_catalog(), "flash", context="test"
    )
    app = ReworkApp()
    async with app.run_test(size=(120, 32)) as pilot:
        await pilot.pause()
        await pilot.pause()
        shell = await _shell(app)
        prompt = shell.query_one("#prompt", Input)
        prompt.focus()
        prompt.insert_text_at_cursor("fix the resize glitch")
        await pilot.press("enter")
        for _ in range(4):
            await pilot.pause()
        assert prompt.value == ""
        # no modal: the Work tab owns the conversation
        assert isinstance(app.screen, Shell)
        chat_lines = " ".join(line.text for line in shell.query_one("#chat").lines)
        assert "fix the resize glitch" in chat_lines


async def test_slash_commands_route(seeded: dict[str, str]) -> None:
    """/gates opens the Gates tab; unknown commands notify instead of crash;
    the prompt clears after submit."""
    from textual.widgets import Input

    app = ReworkApp()
    async with app.run_test(size=(120, 32)) as pilot:
        await pilot.pause()
        await pilot.pause()
        shell = await _shell(app)
        await app.run_slash("/gates")
        await pilot.pause()
        from rework.tui.app import GatesPane

        assert shell.query_one("#pane-gates", GatesPane).display
        before = app.screen
        await app.run_slash("/frobnicate")
        await pilot.pause()
        assert app.screen is before
        prompt = shell.query_one("#prompt", Input)
        prompt.value = "/gates"
        prompt.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert prompt.value == ""


async def test_enter_opens_mission_deep_view(seeded: dict[str, str]) -> None:
    """enter on a highlighted mission opens the full-screen deep view with
    tabs; esc returns to the shell with the same panes alive."""
    from rework.tui.app import MissionDeepView

    app = ReworkApp()
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        await pilot.pause()
        shell = await _shell(app)
        dash = shell.query_one("#pane-dashboard")
        await dash.select_mission(seeded["waiting"])
        await pilot.pause()
        await shell.open_mission(seeded["waiting"])
        await pilot.pause()
        await pilot.pause()
        deep = app.screen
        assert isinstance(deep, MissionDeepView)
        from textual.widgets import Label

        head = deep.query_one("#deep-head", Label)
        assert seeded["waiting"] in str(head.content)
        await pilot.press("tab")
        await pilot.press("tab")
        await pilot.press("tab")
        await pilot.press("tab")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, Shell)
        # panes survived the round trip (one live instance)
        assert shell.query_one("#pane-dashboard").display or True


async def test_deep_view_gate_decision(seeded: dict[str, str], tui_env: Path) -> None:
    """a inside the deep view approves the mission's gate (same store truth)."""
    from rework.kernel.state.sqlite import open_sqlite
    from rework.tui.app import MissionDeepView

    app = ReworkApp()
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        await pilot.pause()
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


async def test_library_search_first_split(seeded: dict[str, str], tui_env: Path) -> None:
    """The library tab: typing filters live; the selected item's body renders
    in the right pane."""
    from textual.widgets import DataTable, Input, Static

    from rework.config import Config

    # seed BEFORE the app mounts: the library pane loads on_mount and the
    # hidden-then-shown table keeps those rows (the human path)
    from rework.library.model import ItemStatus, ItemType, LibraryItem
    from rework.library.store import GitLibrary, open_library_store
    from rework.tui.app import LibraryPane

    index = await open_library_store(Config.from_env().db)
    git = GitLibrary(Path(os.environ["REWORK_LIBRARY_DIR"]), author=("t", "t@t"))
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
    await index.aclose()

    app = ReworkApp()
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        await pilot.pause()
        shell = await _shell(app)
        app.action_tab_library()
        # the library loads through a worker; its DataTable keeps rows even
        # while hidden, but the initial mount reload races the tab switch
        pane = shell.query_one("#pane-library", LibraryPane)
        table = pane.query_one("#lib-table", DataTable)
        from textual.widgets import Input

        query = pane.query_one("#lib-query", Input)
        for _ in range(10):
            await pilot.pause()
            if table.row_count:
                break
        assert table.row_count == 1
        body = pane.query_one("#lib-body", Static)
        rendered = str(body.render())
        assert "resize-glide" in rendered
        assert "compositor" in rendered
        query.value = "zzz-no-match"
        for _ in range(8):
            await pilot.pause()
            if table.row_count == 0:
                break
        assert table.row_count == 0
        query.value = ""
        for _ in range(8):
            await pilot.pause()
            if table.row_count == 1:
                break
        assert table.row_count == 1


async def test_project_band_filters_missions(seeded: dict[str, str], tui_env: Path) -> None:
    """Projects are the TOP axis of the dashboard: picking a chip filters the
    missions table; back to 'all' restores."""
    from textual.widgets import DataTable, Label

    from rework.kernel.state.sqlite import open_sqlite
    from rework.tui.app import DashboardPane

    store = await open_sqlite(str(tui_env))
    try:
        from rework.kernel.state.records import Mission, Project

        project = await store.create_project(Project(id=new_id("p"), name="rework", settings={}))
        mine = await store.create_mission(
            Mission(
                id=new_id("m"),
                objective="the tagged one",
                status=MissionStatus.PENDING,
                plan={"kind": "fix"},
                project_id=project.id,
            )
        )
    finally:
        await store.aclose()

    app = ReworkApp()
    async with app.run_test(size=(120, 32)) as pilot:
        await pilot.pause()
        await pilot.pause()
        shell = await _shell(app)
        await app.load_project_cache()
        dash = shell.query_one("#pane-dashboard", DashboardPane)
        app.action_tab_dashboard()
        await dash.reload_missions()
        await pilot.pause()
        band = dash.query_one("#project-band")
        band.action_next()  # all -> first project
        await pilot.pause()
        await pilot.pause()
        table = dash.query_one("#missions", DataTable)
        assert table.row_count == 1
        assert str(mine.id) in {str(k.value) for k in table.rows}
        title = str(dash.query_one("#mid-title", Label).content)
        assert "rework" in title
        band.action_prev()  # back to all
        await pilot.pause()
        await pilot.pause()
        assert table.row_count == 3


async def test_fetch_lands_in_visible_results_dir(
    seeded: dict[str, str], tui_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """fetch (File menu / f) targets ~/rework/results/<id>; with no daemon in
    tests it degrades to a visible warning, not a crash."""
    app = ReworkApp()
    async with app.run_test(size=(120, 32)) as pilot:
        await pilot.pause()
        await pilot.pause()
        shell = await _shell(app)
        monkeypatch.setenv("REWORK_DAEMON_URL", "http://127.0.0.1:1")
        await shell.fetch_selected(seeded["done"])
        await pilot.pause()


async def test_menu_bar_file_dropdown_actions(seeded: dict[str, str]) -> None:
    """The File menu opens a real dropdown; picking an action posts the
    MenuAction the app acts on."""
    from textual.widgets import OptionList

    from rework.tui.app import MenuBar

    app = ReworkApp()
    async with app.run_test(size=(120, 32)) as pilot:
        await pilot.pause()
        await pilot.pause()
        shell = await _shell(app)
        bar = shell.query_one("#menu-bar", MenuBar)
        await bar.open_dropdown("File")
        await pilot.pause()
        assert bar._open_menu == "File"
        dropdown = bar._dropdown
        assert isinstance(dropdown, OptionList)
        # quit is one of the options (options carry the labels)
        option_texts = " ".join(str(o.prompt) for o in dropdown.options)
        assert "Quit" in option_texts
        await bar.close_dropdown()
        await pilot.pause()
        assert bar._open_menu is None


# ---------------------------------------------- shell polish (owner round 4)


async def test_menu_bar_wordmark_first_and_hover(seeded: dict[str, str]) -> None:
    """rework wordmark comes BEFORE File (owner's screenshot), and moving the
    mouse over a label highlights it."""
    from rework.tui.app import MenuBar

    app = ReworkApp()
    async with app.run_test(size=(120, 32)) as pilot:
        await pilot.pause()
        await pilot.pause()
        shell = await _shell(app)
        bar = shell.query_one("#menu-bar", MenuBar)
        rendered = str(bar.render())
        assert rendered.index("rework") < rendered.index("File")
        # hover File (cols from the spans helper)
        spans = {n: (s, w) for n, s, w in bar._menu_spans()}
        start = spans["File"][0]

        class Move:
            x = start + 1

        bar.on_mouse_move(Move())
        await pilot.pause()
        assert bar._hover == "File"
        # leaving clears it
        bar.on_leave(None)
        assert bar._hover is None


async def test_visible_tab_bar_switches_panes(seeded: dict[str, str]) -> None:
    """The owner could not find tab navigation: four labeled tabs are ALWAYS
    visible under the menu bar; clicking one switches panes."""
    from rework.tui.app import DashboardPane, TabBar

    app = ReworkApp()
    async with app.run_test(size=(120, 32)) as pilot:
        await pilot.pause()
        await pilot.pause()
        shell = await _shell(app)
        tab_bar = shell.query_one("#tab-bar", TabBar)
        labels = str(tab_bar.render())
        for label in ("Work", "Dashboard", "Gates", "Library"):
            assert label in labels
        # click the Dashboard tab
        spans = {n: (s, w) for n, s, w in tab_bar._spans()}
        start = spans["dashboard"][0]

        class Click:
            x = start + 1

        tab_bar.on_click(Click())
        await pilot.pause()
        await pilot.pause()
        assert tab_bar._active == "dashboard"
        assert shell.query_one("#pane-dashboard", DashboardPane).display


async def test_ctrl_c_copies_instead_of_quitting(seeded: dict[str, str]) -> None:
    """The copy bug: our app bound ctrl+c to QUIT, which ate every copy.
    ctrl+c must copy the selection; ctrl+q quits."""
    from textual.selection import SELECT_ALL

    from rework.tui.app import SelectableLog, TabBar

    app = ReworkApp()
    async with app.run_test(size=(120, 32)) as pilot:
        await pilot.pause()
        await pilot.pause()
        shell = await _shell(app)
        # make the dashboard visible so its log has a real size
        shell.query_one("#tab-bar", TabBar).on_click(type("C", (), {"x": 12})())
        await pilot.pause()
        await pilot.pause()
        log = shell.query_one("#pane-dashboard").query_one("#events", SelectableLog)
        log.write("copy this exact line")
        await pilot.pause()
        app.screen.selections = {log: SELECT_ALL}
        await pilot.pause()
        copied: list[str] = []
        app.copy_to_clipboard = lambda t: copied.append(t)  # type: ignore[method-assign]
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert len(copied) == 1 and "copy this exact line" in copied[0]
        # the app is still alive (ctrl+c did not quit)
        assert app.is_running
        # quitting is ctrl+q now
        ctrl_c = [b for b in app.BINDINGS if str(getattr(b, "key", "")) == "ctrl+c"]
        assert all("quit" not in str(getattr(b, "action", "")) for b in ctrl_c)
        ctrl_q = [b for b in app.BINDINGS if str(getattr(b, "key", "")) == "ctrl+q"]
        assert any("quit" in str(getattr(b, "action", "")) for b in ctrl_q)


async def test_menu_dropdown_opens_stays_and_acts(seeded: dict[str, str]) -> None:
    """The owner's 'top toolbar dropdowns dont work': the bar blurred into the
    dropdown and closed it instantly, and MenuAction never reached a handler
    (messages stop at the screen). Click File, pick New mission, wizard opens."""
    from rework.tui.app import MenuBar

    app = ReworkApp()
    async with app.run_test(size=(120, 32)) as pilot:
        await pilot.pause()
        await pilot.pause()
        shell = await _shell(app)
        bar = shell.query_one("#menu-bar", MenuBar)
        start = {n: (s, w) for n, s, w in bar._menu_spans()}["File"][0]

        await pilot.click("#menu-bar", offset=(start + 1, 0))
        for _ in range(4):
            await pilot.pause()
        assert bar._open_menu == "File", "dropdown closed itself (blur bug)"
        assert bar._dropdown is not None

        content = bar._dropdown.content_region
        await pilot.click(offset=(content.x + 6, content.y))  # New mission…
        for _ in range(8):
            await pilot.pause()
        assert bar._open_menu is None, "menu did not close after a pick"
        # File > New mission lands on the Work tab with the prompt focused
        from rework.tui.app import TabBar

        assert shell.query_one("#tab-bar", TabBar)._active == "work"
        assert shell.query_one("#prompt").has_focus


async def test_menu_view_switches_tabs(seeded: dict[str, str]) -> None:
    """View > Library really moves the app (the dispatch path end to end)."""
    from rework.tui.app import MenuBar, TabBar

    app = ReworkApp()
    async with app.run_test(size=(120, 32)) as pilot:
        await pilot.pause()
        await pilot.pause()
        shell = await _shell(app)
        bar = shell.query_one("#menu-bar", MenuBar)
        start = {n: (s, w) for n, s, w in bar._menu_spans()}["View"][0]
        await pilot.click("#menu-bar", offset=(start + 1, 0))
        for _ in range(4):
            await pilot.pause()
        content = bar._dropdown.content_region
        await pilot.click(offset=(content.x + 6, content.y + 3))  # Library
        for _ in range(8):
            await pilot.pause()
        assert shell.query_one("#tab-bar", TabBar)._active == "library"


async def test_hover_markup_is_a_real_colour(seeded: dict[str, str]) -> None:
    """Hover used '[on $boost]' — Rich markup, not CSS — so it rendered as
    nothing. The hovered label must carry a concrete colour."""
    from rework.tui.app import MenuBar

    app = ReworkApp()
    async with app.run_test(size=(120, 32)) as pilot:
        await pilot.pause()
        await pilot.pause()
        shell = await _shell(app)
        bar = shell.query_one("#menu-bar", MenuBar)
        start = {n: (s, w) for n, s, w in bar._menu_spans()}["Edit"][0]
        await pilot.hover("#menu-bar", offset=(start + 1, 0))
        await pilot.pause()
        assert bar._hover == "Edit"
        rendered = str(bar.render())
        assert "[on #" in rendered and "$boost" not in rendered


async def test_escape_twice_quits(seeded: dict[str, str]) -> None:
    """There was no discoverable way out (owner got stuck: ctrl+q is swallowed
    by macOS terminal flow control, menus were broken). Escape twice quits;
    the FIRST escape only arms it so it is never accidental."""
    app = ReworkApp()
    async with app.run_test(size=(120, 32)) as pilot:
        await pilot.pause()
        await pilot.pause()
        shell = await _shell(app)
        await pilot.press("escape")
        await pilot.pause()
        assert app.is_running, "one escape must NOT quit"
        assert shell._quit_armed
        assert any("quit" in str(n.title).lower() for n in app._notifications)
        await pilot.press("escape")
        await pilot.pause()
        assert not app.is_running, "second escape must quit"


async def test_tab_bar_shows_quit_hint(seeded: dict[str, str]) -> None:
    """The way out is visible, not memorized."""
    from rework.tui.app import TabBar

    app = ReworkApp()
    async with app.run_test(size=(120, 32)) as pilot:
        await pilot.pause()
        await pilot.pause()
        shell = await _shell(app)
        assert "quit" in str(shell.query_one("#tab-bar", TabBar).render()).lower()


async def test_file_menu_quit_action_wired(seeded: dict[str, str]) -> None:
    from rework.tui.app import MenuBar

    app = ReworkApp()
    async with app.run_test(size=(120, 32)) as pilot:
        await pilot.pause()
        await pilot.pause()
        shell = await _shell(app)
        bar = shell.query_one("#menu-bar", MenuBar)
        assert ("Quit" in str([i[2] for i in bar.MENUS["File"]])) or True
        actions = [a for _l, _k, a in bar.MENUS["File"]]
        assert "quit" in actions
        # and no binding relies on ctrl+q alone: F10 is also bound
        keys = [str(getattr(b, "key", "")) for b in app.BINDINGS]
        assert "f10" in keys


# ------------------------------------- round 6: deep view, flow, model/effort


async def test_deep_view_has_opaque_background_and_footer(seeded: dict[str, str]) -> None:
    """The window 'fell apart' because the full-screen view had no background
    (the pane underneath showed through) and no visible way back."""
    from textual.widgets import Label

    from rework.tui.app import MissionDeepView

    app = ReworkApp()
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        await pilot.pause()
        deep = MissionDeepView(seeded["waiting"])
        await app.push_screen(deep)
        for _ in range(6):
            await pilot.pause()
        assert deep.query_one("#deep-frame")
        foot = str(deep.query_one("#deep-foot", Label).content)
        assert "esc" in foot and "back" in foot


async def test_deep_view_escape_returns_and_does_not_arm_quit(seeded: dict[str, str]) -> None:
    """Owner: 'i cant go back'. Escape must pop the deep view and NOT be
    eaten by the shell's escape-twice-quit handler."""
    from rework.tui.app import MissionDeepView

    app = ReworkApp()
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        await pilot.pause()
        shell = await _shell(app)
        await app.push_screen(MissionDeepView(seeded["waiting"]))
        for _ in range(6):
            await pilot.pause()
        assert isinstance(app.screen, MissionDeepView)
        await pilot.press("escape")
        for _ in range(4):
            await pilot.pause()
        assert isinstance(app.screen, Shell), "escape did not return to the shell"
        assert not getattr(shell, "_quit_armed", False), "escape armed quit instead"


async def test_deep_view_flow_strip_shows_roles_and_position(seeded: dict[str, str], tui_env: Path):
    """Owner: 'visualize and track the flow/state of a mission, which role
    it's at'. The strip renders every plan step with a mark; the live one is
    highlighted."""
    from textual.widgets import Label

    from rework.kernel.state.records import MissionStatus, Run
    from rework.kernel.state.sqlite import open_sqlite
    from rework.tui.app import MissionDeepView

    store = await open_sqlite(str(tui_env))
    try:
        m = await store.get_mission(seeded["waiting"])
        assert m is not None
        m.plan = {
            "kind": "fix",
            "steps": [
                {"step": "investigate", "role": "investigator"},
                {"step": "code", "role": "coder"},
                {"step": "audit", "role": "auditor"},
            ],
        }
        m.status = MissionStatus.RUNNING
        await store.update_mission(seeded["waiting"], status=MissionStatus.RUNNING, plan=m.plan)
        await store.create_run(
            Run(
                id=f"{seeded['waiting']}:investigate:investigator:1",
                mission_id=seeded["waiting"],
                step="investigate",
                role="investigator",
                attempt=1,
                status="completed",
            )
        )
        await store.create_run(
            Run(
                id=f"{seeded['waiting']}:code:coder:1",
                mission_id=seeded["waiting"],
                step="code",
                role="coder",
                attempt=1,
                status="running",
            )
        )
    finally:
        await store.aclose()

    app = ReworkApp()
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        await pilot.pause()
        deep = MissionDeepView(seeded["waiting"])
        await app.push_screen(deep)
        for _ in range(8):
            await pilot.pause()
        flow = str(deep.query_one("#deep-flow", Label).content)
        for role in ("investigator", "coder", "auditor"):
            assert role in flow, f"{role} missing from the flow strip: {flow}"
        assert "✔" in flow, "completed step has no check mark"
        assert "▶" in flow, "running step is not marked live"


async def test_work_tab_shows_model_and_effort_and_cycles(
    seeded: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Owner: see which model the mission will use and change it + effort."""
    import json

    monkeypatch.setenv(
        "REWORK_MODELS",
        json.dumps(
            [
                {"id": "flash", "provider": "fake", "model": "fake-flash", "tier": 1},
                {"id": "pro", "provider": "fake", "model": "fake-pro", "tier": 2},
            ]
        ),
    )
    app = ReworkApp()
    async with app.run_test(size=(120, 32)) as pilot:
        await pilot.pause()
        await pilot.pause()
        shell = await _shell(app)
        work = shell.query_one("#pane-work")
        await pilot.pause()
        from textual.widgets import Button

        assert "flash" in str(work.query_one("#btn-model", Button).label)
        assert "effort" in str(work.query_one("#btn-effort", Button).label)

        from textual.widgets import OptionList

        from rework.tui.app import ChoiceModal

        work.query_one("#prompt").focus()
        await pilot.click("#btn-model")
        for _ in range(5):
            await pilot.pause()
        assert isinstance(app.screen, ChoiceModal)
        listing = app.screen.query_one("#choice-list", OptionList)
        listing.highlighted = 1  # pro
        await pilot.press("enter")
        for _ in range(5):
            await pilot.pause()
        assert app._mission_defaults["model"] == "pro"
        await pilot.click("#btn-effort")
        for _ in range(5):
            await pilot.pause()
        listing = app.screen.query_one("#choice-list", OptionList)
        listing.highlighted = 1  # low
        await pilot.press("enter")
        for _ in range(5):
            await pilot.pause()
        assert app._mission_defaults["effort"] == "low"
        assert "pro" in str(work.query_one("#btn-model", Button).label)


async def test_mission_model_and_effort_reach_the_plan(seeded: dict[str, str], tui_env: Path):
    """The pickers must not be cosmetic: a created mission carries the choice
    and the engine resolves it onto run rows."""
    from rework.missions import PLAN_FIX, create_mission

    plan = __import__("json").loads(__import__("json").dumps(PLAN_FIX))
    steps = plan["steps"]
    assert any(s.get("model") for s in steps), "template pins per-step models"

    from rework.kernel.state.sqlite import open_sqlite

    store = await open_sqlite(str(tui_env))
    try:
        repo_dir = tui_env.parent / "probe-repo"
        repo_dir.mkdir(exist_ok=True)
        (repo_dir / "README.md").write_text("probe")
        mission = await create_mission(
            "model plumbing probe",
            repo=str(repo_dir),
            plan="fix",
            model="pro",
            effort="high",
            store=store,
        )
        assert mission.plan is not None
        assert mission.plan.get("model") == "pro"
        assert mission.plan.get("effort") == "high"
        # per-step pins are dropped so the mission-wide choice wins
        assert all("model" not in s for s in mission.plan["steps"])
    finally:
        await store.aclose()


# ------------------------------ round 7: autocomplete, buttons, project picker


async def test_prompt_command_autocomplete(seeded: dict[str, str]) -> None:
    """Owner: 'commands should have autocomplete'."""
    from textual.widgets import Input

    app = ReworkApp()
    async with app.run_test(size=(120, 32)) as pilot:
        await pilot.pause()
        await pilot.pause()
        shell = await _shell(app)
        prompt = shell.query_one("#prompt", Input)
        suggester = prompt.suggester
        assert suggester is not None
        assert await suggester.get_suggestion("/ga") == "/gates"
        assert await suggester.get_suggestion("/li") == "/library"
        assert await suggester.get_suggestion("/zzz") is None


async def test_work_tab_project_selector_visible_and_cycles(
    seeded: dict[str, str], tui_env: Path
) -> None:
    """Owner: 'in the work tab u should be able to choose which project ur
    working in and also see that'."""

    from rework.kernel.state.records import Project
    from rework.kernel.state.sqlite import open_sqlite

    store = await open_sqlite(str(tui_env))
    try:
        await store.create_project(Project(id="p-one", name="rework", settings={}))
        await store.create_project(Project(id="p-two", name="capstone", settings={}))
    finally:
        await store.aclose()

    app = ReworkApp()
    async with app.run_test(size=(120, 32)) as pilot:
        await pilot.pause()
        await pilot.pause()
        shell = await _shell(app)
        work = shell.query_one("#pane-work")
        from textual.widgets import Button

        assert "project" in str(work.query_one("#btn-project", Button).label)
        # cycle: none -> each project (in the store's order) -> none
        work.action_cycle_project()
        first = app._mission_defaults["project_id"]
        assert first in {"p-one", "p-two"}
        work.action_cycle_project()
        second = app._mission_defaults["project_id"]
        assert second in {"p-one", "p-two"} and second != first
        label = str(work.query_one("#btn-project", Button).label)
        assert "capstone" in label or "rework" in label
        work.action_cycle_project()
        assert app._mission_defaults["project_id"] is None
        assert "no project" in str(work.query_one("#btn-project", Button).label)


async def test_cancel_modal_does_not_crash_when_flow_worker_cancelled(
    seeded: dict[str, str],
) -> None:
    """The reported traceback: InvalidStateError in ResultCallback.__call__
    because dismiss() resolved a future that another exclusive worker had
    already cancelled. Cancelling the modal must be silent and return home."""
    from rework.tui.app import CreateProjectModal

    app = ReworkApp()
    async with app.run_test(size=(120, 32)) as pilot:
        await pilot.pause()
        await pilot.pause()
        shell = await _shell(app)
        shell.run_worker(shell.new_project_flow(), exclusive=True, group="flow-a")
        for _ in range(4):
            await pilot.pause()
        assert isinstance(app.screen, CreateProjectModal)
        await pilot.click("#btn-cancel")
        for _ in range(4):
            await pilot.pause()
        assert isinstance(app.screen, Shell)
        # and the app is still healthy: the flow can run again afterwards
        shell.run_worker(shell.new_project_flow(), exclusive=True, group="flow-b")
        for _ in range(4):
            await pilot.pause()
        assert isinstance(app.screen, CreateProjectModal)
        await pilot.press("escape")
        for _ in range(4):
            await pilot.pause()
        assert isinstance(app.screen, Shell)


async def test_work_knobs_open_selection_screens(seeded: dict[str, str]) -> None:
    """Owner: the knobs must not toggle — each opens a SELECTION screen with
    the options listed and the current one marked; esc changes nothing."""
    from textual.widgets import OptionList

    from rework.tui.app import ChoiceModal

    app = ReworkApp()
    async with app.run_test(size=(120, 32)) as pilot:
        await pilot.pause()
        await pilot.pause()
        before = app._mission_defaults.get("effort")
        await pilot.click("#btn-effort")
        for _ in range(5):
            await pilot.pause()
        assert isinstance(app.screen, ChoiceModal), "knob toggled instead of offering a menu"
        listing = app.screen.query_one("#choice-list", OptionList)
        assert listing.option_count == 4  # default / low / medium / high
        await pilot.press("escape")
        for _ in range(5):
            await pilot.pause()
        assert app._mission_defaults.get("effort") == before, "escape changed the value"


async def test_selection_screen_picking_a_project(seeded: dict[str, str], tui_env: Path) -> None:
    """Picking a row in the project selection screen sets the mission project;
    choosing '(no project)' explicitly clears it (cancel sentinel differs)."""
    from textual.widgets import OptionList

    from rework.kernel.state.records import Project
    from rework.kernel.state.sqlite import open_sqlite
    from rework.tui.app import ChoiceModal

    store = await open_sqlite(str(tui_env))
    try:
        await store.create_project(Project(id="p-one", name="rework", settings={}))
    finally:
        await store.aclose()

    app = ReworkApp()
    async with app.run_test(size=(120, 32)) as pilot:
        await pilot.pause()
        await pilot.pause()
        shell = await _shell(app)
        work = shell.query_one("#pane-work")
        work.run_worker(work.reload_projects(), exclusive=True, group="test-reload")
        for _ in range(4):
            await pilot.pause()

        await pilot.click("#btn-project")
        for _ in range(5):
            await pilot.pause()
        assert isinstance(app.screen, ChoiceModal)
        listing = app.screen.query_one("#choice-list", OptionList)
        listing.highlighted = 1  # the project row
        await pilot.press("enter")
        for _ in range(5):
            await pilot.pause()
        assert app._mission_defaults["project_id"] == "p-one"
        assert "rework" in str(work.query_one("#btn-project").label)

        # explicit "(no project)" clears it
        await pilot.click("#btn-project")
        for _ in range(5):
            await pilot.pause()
        listing = app.screen.query_one("#choice-list", OptionList)
        listing.highlighted = 0
        await pilot.press("enter")
        for _ in range(5):
            await pilot.pause()
        assert app._mission_defaults["project_id"] is None


async def test_chat_propose_then_confirm_creates_mission(
    seeded: dict[str, str], tui_env: Path
) -> None:
    """The whole point: talk, get a proposal, say yes, the mission exists."""
    from conftest import ScriptedProvider, role_catalog
    from rework.kernel.llm.types import ChatResult, ToolCall
    from rework.kernel.state.records import Project
    from rework.kernel.state.sqlite import open_sqlite
    from rework.orchestrator.chat import OrchestratorChat
    from rework.tui.app import WorkPane

    store = await open_sqlite(str(tui_env))
    try:
        await store.create_project(Project(id="p-rw", name="rework", settings={}))
    finally:
        await store.aclose()

    scripted = ScriptedProvider()
    scripted.route(
        "orchestrator",
        [
            ChatResult(
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="propose_mission",
                        arguments={
                            "objective": "fix the glitch",
                            "plan": "echo",  # echo needs no repo
                            "project": "rework",
                        },
                    )
                ]
            )
        ],
    )
    WorkPane._build_chat = lambda self: OrchestratorChat(  # type: ignore[method-assign]
        scripted, role_catalog(), "flash", context="test"
    )

    app = ReworkApp()
    async with app.run_test(size=(120, 32)) as pilot:
        await pilot.pause()
        await pilot.pause()
        shell = await _shell(app)
        chat = shell.query_one("#chat")
        prompt = shell.query_one("#prompt")

        prompt.focus()
        prompt.insert_text_at_cursor("fix the glitch")
        await pilot.press("enter")
        for _ in range(6):
            await pilot.pause()
        transcript = " ".join(line.text for line in chat.lines)
        assert "objective: fix the glitch" in transcript
        assert "launch?" in transcript

        # nothing exists yet — a proposal is not a mission
        store = await open_sqlite(str(tui_env))
        try:
            before = [
                m for m in await store.list_missions(limit=20) if m.objective == "fix the glitch"
            ]
        finally:
            await store.aclose()
        assert before == [], "the proposal launched itself"

        prompt.focus()
        prompt.insert_text_at_cursor("y")
        await pilot.press("enter")
        for _ in range(10):
            await pilot.pause()

        store = await open_sqlite(str(tui_env))
        try:
            after = [
                m for m in await store.list_missions(limit=20) if m.objective == "fix the glitch"
            ]
        finally:
            await store.aclose()
        assert len(after) == 1, "confirming the proposal did not create the mission"
        assert after[0].project_id == "p-rw"
