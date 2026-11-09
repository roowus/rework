"""TUI tests: the real app mounted against a tmp SQLite store, driven through
the same input path a human takes (clicks and keys via Textual's Pilot).

Anything that WRITES is asserted against the store (reopened separately), not
against widget state. The app never touches a deployment database: every test
sets REWORK_DB to a tmp file before the app is constructed."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from seed import seed, seed_library

from rework.kernel.llm.adapters.fake import FakeProvider
from rework.kernel.llm.types import ChatResult, ToolCall, Usage
from rework.kernel.state.records import Gate, GateKind
from rework.tui.app import ReworkApp
from rework.tui.widgets.base import Btn, RowList

SIZE = (140, 42)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "tui.sqlite3"
    monkeypatch.setenv("REWORK_DB", str(db))
    monkeypatch.setenv("REWORK_LIBRARY_DIR", str(tmp_path / "lib"))
    monkeypatch.setenv("REWORK_RESULTS_DIR", str(tmp_path / "results"))
    monkeypatch.setenv("REWORK_REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("REWORK_TUI_SETTINGS", str(tmp_path / "tui.json"))
    monkeypatch.setenv("REWORK_NOTIFY", "toast,bell")
    monkeypatch.setenv(
        "REWORK_MODELS",
        '[{"id":"flash","provider":"fake","model":"f","tier":1},'
        '{"id":"pro","provider":"fake","model":"p","tier":2}]',
    )
    return db


@pytest.fixture
async def ids(env: Path) -> dict[str, str]:
    return await seed(str(env), history_days=20)


async def _store(db: Path) -> Any:
    from rework.kernel.state.sqlite import open_sqlite

    return await open_sqlite(str(db))


def _app() -> ReworkApp:
    return ReworkApp(poll_seconds=0.3)


async def _settle(pilot: Any, t: float = 0.5) -> None:
    await pilot.pause(t)


def _btn(app: ReworkApp, root: Any, action: str) -> Btn:
    for btn in root.query(Btn):
        if btn.action == action and btn.display:
            return btn
    raise AssertionError(f"no visible button {action!r}")


def script_provider(monkeypatch: pytest.MonkeyPatch, results: list[ChatResult]) -> FakeProvider:
    """Every OrchestratorChat built by the app talks to this scripted provider."""
    provider = FakeProvider(results)
    import rework.kernel.llm.adapters as adapters

    monkeypatch.setattr(adapters, "build_provider", lambda entry, extra=None: provider)
    return provider


def propose(**args: Any) -> ChatResult:
    return ChatResult(
        text="Here's what I'd run.",
        tool_calls=[ToolCall(id="p1", name="propose_mission", arguments=args)],
        usage=Usage(input_tokens=900, output_tokens=60),
    )


# -- shell -------------------------------------------------------------------


async def test_opens_blank_on_work(ids: dict[str, str]) -> None:
    """Owner requirement: bare `rework` opens to an empty state; nothing auto-opens."""
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        assert app.active_tab == "work"
        work = app.view("work")
        assert work.convo is None
        assert app.query_one("#work-welcome").display
        assert app.view("dashboard").mission_id is None
        # the chat list is populated but nothing is selected
        assert app.query_one("#chat-list", RowList).selected is None
        assert not app.swallowed


async def test_tabs_by_key_and_by_click(ids: dict[str, str]) -> None:
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        for key, tab in [
            ("alt+2", "dashboard"),
            ("alt+3", "gates"),
            ("alt+4", "library"),
            ("alt+5", "cost"),
            ("alt+1", "work"),
        ]:
            await pilot.press(key)
            await pilot.pause()
            assert app.active_tab == tab, key
        # keys work even while the prompt has focus (modifier shortcuts)
        await pilot.press("h", "i", "alt+3")
        await pilot.pause()
        assert app.active_tab == "gates"
        tabs = list(app.query(".tab"))
        await pilot.click(tabs[1])
        await pilot.pause()
        assert app.active_tab == "dashboard"
        assert not app.swallowed


async def test_alt_keys_as_a_real_macos_terminal_sends_them(ids: dict[str, str], env: Path) -> None:
    """Found live: alt+N worked under Pilot but never in a real terminal,
    because Textual turns Option+digit into the macOS character (¡ ™ £ ¢ ∞)
    and Option+a / Option+x type å / ≈. Every shortcut answers to both."""
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        for key, tab in [
            ("trade_mark_sign", "dashboard"),
            ("pound_sign", "gates"),
            ("cent_sign", "library"),
            ("infinity", "cost"),
            ("inverted_exclamation_mark", "work"),
        ]:
            await pilot.press(key)
            await pilot.pause()
            assert app.active_tab == tab, key
        assert app.query_one("#prompt").text == "", "none of them typed a character"
        await pilot.press("pound_sign")
        await _settle(pilot)
        app.query_one("#gate-list", RowList).activate(ids["gate"], via_mouse=True)
        await _settle(pilot)
        app.query_one("#gate-text").focus()  # typing an answer must not eat the key
        await pilot.press("å")
        await _settle(pilot, 0.8)
    store = await _store(env)
    try:
        gate = await store.get_gate(ids["gate"])
        assert gate is not None and gate.status.value == "approved"
    finally:
        await store.aclose()


async def test_escape_twice_quits_once_does_not(ids: dict[str, str]) -> None:
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        await pilot.press("escape")
        await pilot.pause()
        assert app.is_running
        await pilot.press("escape")
        await pilot.pause()
    assert app.return_code in (0, None)


async def test_ctrl_c_copies_and_never_quits(ids: dict[str, str]) -> None:
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert app.is_running
        app.action_copy()
        assert app.is_running


async def test_menu_bar_dropdown_runs_an_action(ids: dict[str, str]) -> None:
    from rework.tui.widgets.chrome import MenuScreen

    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        view_title = next(t for t in app.query(".menu-title") if "View" in str(t.render()))
        await pilot.click(view_title)
        await pilot.pause()
        assert isinstance(app.screen, MenuScreen)
        entry = next(e for e in app.screen.query(".menu-item") if "Cost" in str(e.render()))
        await pilot.click(entry)
        await _settle(pilot, 0.3)
        assert not isinstance(app.screen, MenuScreen)
        assert app.active_tab == "cost"


async def test_status_bar_counts_open_gates(ids: dict[str, str]) -> None:
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        status = str(app.query_one("#menubar-status").render())
        assert "◆ 2" in status
        assert "local" in status, "the bar says which database this TUI talks to"


# -- resilience ----------------------------------------------------------------


def test_target_label_names_the_database(monkeypatch: pytest.MonkeyPatch) -> None:
    from rework.tui.app import target_label

    monkeypatch.delenv("REWORK_TARGET_NAME", raising=False)
    monkeypatch.setenv("REWORK_DB", "/tmp/x.sqlite3")
    assert target_label() == "local"
    monkeypatch.setenv("REWORK_DB", "postgresql://u:p@100.107.125.118:5433/rework_state")
    assert target_label() == "rewvis"
    monkeypatch.setenv("REWORK_DB", "postgresql://u:p@db.example.com/x")
    assert target_label() == "db.example.com"
    monkeypatch.setenv("REWORK_TARGET_NAME", "staging")
    assert target_label() == "staging"


async def test_a_hanging_database_never_freezes_input(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Found live: with rewvis unreachable the first refresh hung for minutes,
    and because it ran on the App's message queue NO key or click was handled.
    Here the database hangs FOREVER: input must still be handled at once."""
    import asyncio as _asyncio

    from rework.tui import data as data_mod

    monkeypatch.setattr(data_mod, "QUERY_TIMEOUT_S", 3600.0)

    async def hang(self, fn):  # type: ignore[no-untyped-def]
        await _asyncio.sleep(3600)

    monkeypatch.setattr(data_mod.TuiData, "_call", hang)
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.3)  # the first refresh is now hanging
        await _asyncio.wait_for(pilot.press("alt+2"), timeout=3)
        assert app.active_tab == "dashboard", "keys work while the database hangs"
        await _asyncio.wait_for(pilot.press("alt+1", "h", "i"), timeout=3)
        assert app.query_one("#prompt").text == "hi", "typing works too"
        assert app.is_running


async def test_a_slow_database_ends_up_offline(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A read that does not answer within QUERY_TIMEOUT_S counts as a dead
    connection: the status bar says offline instead of waiting forever."""
    import asyncio as _asyncio

    from rework.tui import data as data_mod

    monkeypatch.setattr(data_mod, "QUERY_TIMEOUT_S", 0.3)

    async def hang(self, fn):  # type: ignore[no-untyped-def]
        await _asyncio.sleep(3600)

    monkeypatch.setattr(data_mod.TuiData, "_call", hang)
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(1.5)
        assert "offline" in str(app.query_one("#menubar-status").render())


async def test_a_dead_database_never_exits_the_app(ids: dict[str, str]) -> None:
    """Handoff trap #1: a dropped connection must mark offline, not crash."""
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)

        async def boom(*_a: Any, **_k: Any) -> Any:
            raise OSError("consuming input failed: Operation timed out")

        app.hub.data.missions = boom  # type: ignore[method-assign]
        app.hub.data.events_tail = boom  # type: ignore[method-assign]
        await pilot.press("alt+2")
        await _settle(pilot, 1.0)
        assert app.is_running
        assert "offline" in str(app.query_one("#menubar-status").render())
        # the stale snapshot keeps the dashboard populated
        assert app.query_one("#mission-list", RowList).selectable


# -- gates --------------------------------------------------------------------


async def test_approve_a_gate_by_clicking(ids: dict[str, str], env: Path) -> None:
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        await pilot.press("alt+3")
        await _settle(pilot)
        gates = app.view("gates")
        lst = app.query_one("#gate-list", RowList)
        assert ids["gate"] in lst.keys
        lst.activate(ids["gate"], via_mouse=True)
        await _settle(pilot)
        await pilot.click(_btn(app, gates, "approve"))
        await _settle(pilot, 0.8)
    store = await _store(env)
    try:
        gate = await store.get_gate(ids["gate"])
        assert gate is not None and gate.status.value == "approved"
    finally:
        await store.aclose()


async def test_reject_with_feedback_reaches_the_decision(ids: dict[str, str], env: Path) -> None:
    from textual.widgets import TextArea

    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        await pilot.press("alt+3")
        await _settle(pilot)
        app.query_one("#gate-list", RowList).activate(ids["question_gate"], via_mouse=True)
        await _settle(pilot)
        app.query_one("#gate-text", TextArea).text = "only the flaky one"
        await pilot.press("alt+x")
        await _settle(pilot, 0.8)
    store = await _store(env)
    try:
        gate = await store.get_gate(ids["question_gate"])
        assert gate is not None and gate.status.value == "rejected"
        assert gate.decision_note == "only the flaky one"
        assert (gate.decision or {}).get("feedback") == "only the flaky one"
    finally:
        await store.aclose()


async def test_new_gate_notifies(ids: dict[str, str], env: Path) -> None:
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot, 0.8)
        assert app.notifications_sent == []  # existing gates are not news
        store = await _store(env)
        try:
            await store.create_gate(
                Gate(
                    id="gate-fresh0000",
                    mission_id=ids["running"],
                    kind=GateKind.GENERIC,
                    question="May I delete the old fixtures?",
                )
            )
        finally:
            await store.aclose()
        await _settle(pilot, 1.0)
        channels = [c for c, text in app.notifications_sent if "fixtures" in text]
        assert "toast" in channels and "bell" in channels
        assert "◆ 3" in str(app.query_one("#menubar-status").render())


async def test_notify_none_is_silent(
    ids: dict[str, str], env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REWORK_NOTIFY", "none")
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot, 0.8)
        store = await _store(env)
        try:
            await store.create_gate(
                Gate(
                    id="gate-quiet0000",
                    mission_id=ids["running"],
                    kind=GateKind.GENERIC,
                    question="quiet?",
                )
            )
        finally:
            await store.aclose()
        await _settle(pilot, 1.0)
        assert app.notifications_sent == []


# -- work: the conversation ----------------------------------------------------------


async def test_chat_proposes_then_launch_creates_mission(
    ids: dict[str, str], env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = script_provider(
        monkeypatch,
        [propose(objective="say hello", plan="echo", project="polystats", title="Greeting")],
    )
    from rework.tui.widgets.cards import MissionCard, ProposalCard

    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        for ch in "say hello":
            await pilot.press(ch if ch != " " else "space")
        await pilot.press("enter")
        await _settle(pilot, 1.0)
        cards = list(app.query(ProposalCard))
        assert cards, "the orchestrator's proposal must render as a card"
        assert provider.requests, "the prompt must reach the model"
        # nothing exists until Launch
        store = await _store(env)
        try:
            before = {m.id for m in await store.list_missions(limit=100)}
        finally:
            await store.aclose()
        await pilot.click(_btn(app, cards[-1], "launch"))
        await _settle(pilot, 1.0)
        assert list(app.query(MissionCard))
        chat_id = app.view("work").convo.id
    store = await _store(env)
    try:
        missions = await store.list_missions(limit=100)
        new = [m for m in missions if m.id not in before]
        assert len(new) == 1
        assert new[0].objective == "say hello"
        assert new[0].project_id == ids["project"], "project NAME resolves to the id"
        assert new[0].title == "Greeting", "the proposal's title names the mission"
        chat = await store.get_chat(chat_id)
        assert chat is not None and chat.mission_id == new[0].id
        msgs = await store.chat_messages(chat_id)
        assert any(m.role == "note" and new[0].id in m.display for m in msgs)
    finally:
        await store.aclose()


async def test_typing_yes_launches(
    ids: dict[str, str], env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script_provider(monkeypatch, [propose(objective="ping", plan="echo")])
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        for keys in (["p", "i", "n", "g", "enter"], ["y", "e", "s", "enter"]):
            await pilot.press(*keys)
            await _settle(pilot, 1.0)
    store = await _store(env)
    try:
        assert any(m.objective == "ping" for m in await store.list_missions(limit=100))
    finally:
        await store.aclose()


async def test_fix_plan_without_repo_asks_instead_of_crashing(
    ids: dict[str, str], env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script_provider(monkeypatch, [propose(objective="fix it", plan="fix")])
    from rework.tui.widgets.cards import ProposalCard

    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        await pilot.press("f", "i", "x", "enter")
        await _settle(pilot, 1.0)
        await pilot.click(_btn(app, list(app.query(ProposalCard))[-1], "launch"))
        await _settle(pilot, 0.8)
        text = " ".join(str(s.render()) for s in app.query(".step-gate"))
        assert "repo" in text
        assert not app.swallowed
    store = await _store(env)
    try:
        assert not any(m.objective == "fix it" for m in await store.list_missions(limit=100))
    finally:
        await store.aclose()


async def test_failed_send_keeps_the_prompt_text(
    ids: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    script_provider(monkeypatch, [])  # FakeProvider raises when it runs dry
    from rework.tui.widgets.prompt import Prompt

    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        await pilot.press("h", "e", "y", "enter")
        await _settle(pilot, 1.0)
        assert app.query_one("#prompt", Prompt).text == "hey"
        assert app.is_running


async def test_reopening_a_chat_replays_its_trajectory(ids: dict[str, str]) -> None:
    from rework.tui.widgets.cards import MissionCard, ProposalCard

    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        app.query_one("#chat-list", RowList).activate(ids["chat"], via_mouse=True)
        await _settle(pilot, 1.0)
        assert app.view("work").convo.id == ids["chat"]
        calls = [str(s.render()) for s in app.query(".step-call")]
        assert any("list_missions" in c for c in calls)
        proposals = list(app.query(ProposalCard))
        assert proposals and not proposals[-1].live, "a launched proposal is settled"
        cards = list(app.query(MissionCard))
        assert cards and cards[0].mission_id == ids["running"]
        await _settle(pilot, 0.8)
        assert cards[0].tail, "the mission card streams the live trajectory"


async def test_typing_after_clicking_the_transcript_reaches_the_prompt(ids: dict[str, str]) -> None:
    """Found driving the real TUI: a click in the chat log moved focus to the
    transcript, and the next keystrokes vanished."""
    from rework.tui.widgets.prompt import Prompt

    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        app.query_one("#chat-list", RowList).activate(ids["chat"], via_mouse=True)
        await _settle(pilot, 0.8)
        app.query_one("#chat-log").focus()
        await pilot.press("o", "k")
        assert app.query_one("#prompt", Prompt).text == "ok"
        assert app.focused is app.query_one("#prompt", Prompt)


async def test_prompt_newline_keys_and_image_paste(ids: dict[str, str], tmp_path: Path) -> None:
    from textual import events

    from rework.tui.widgets.prompt import Prompt

    image = tmp_path / "shot.png"
    image.write_bytes(b"\x89PNG\r\n")
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        prompt = app.query_one("#prompt", Prompt)
        await pilot.press("a", "alt+enter", "b")
        assert prompt.text == "a\nb"
        await prompt._on_paste(events.Paste(str(image)))
        await pilot.pause()
        assert prompt.attachments == [str(image.resolve())]
        big = "x" * 50_000
        prompt.text = ""
        prompt.insert(big)
        assert len(prompt.text) == 50_000, "long pastes are kept whole"


# -- dashboard --------------------------------------------------------------------


async def test_dashboard_drill_down_and_back(ids: dict[str, str]) -> None:
    from rework.tui.widgets.trajectory import Step

    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        await pilot.press("alt+2")
        await _settle(pilot)
        lst = app.query_one("#mission-list", RowList)
        assert ids["running"] in lst.keys and ids["waiting"] in lst.keys
        lst.activate(ids["running"], via_mouse=True)
        await _settle(pilot, 1.0)
        dash = app.view("dashboard")
        assert dash.mission_id == ids["running"]
        steps = [str(s.render()) for s in app.query("#m-traj Step")]
        assert any("read_file" in s for s in steps)
        runs = app.query_one("#run-list", RowList)
        coder = next(k for k in runs.keys if ":coder:" in k)
        runs.activate(coder, via_mouse=True)
        await _settle(pilot, 0.5)
        filtered = [str(s.render()) for s in app.query("#m-traj Step")]
        assert filtered and not any("grep" in s for s in filtered), "run filter applies"
        assert all(isinstance(s, Step) for s in app.query("#m-traj Step"))
        await pilot.press("escape")
        await pilot.pause()
        assert dash.run_id is None and dash.mission_id == ids["running"]
        await pilot.press("escape")
        await pilot.pause()
        assert dash.mission_id is None
        assert app.is_running, "esc inside a drill-down navigates, never arms quit"


async def test_dashboard_graph_and_tabs_render(ids: dict[str, str]) -> None:
    from rework.tui.widgets.base import ChipGroup

    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        app.open_mission(ids["running"])
        await _settle(pilot, 1.0)
        dash = app.view("dashboard")
        tabs = app.query_one("#m-tabs", ChipGroup)
        for value in ("graph", "output", "diff", "report", "gates"):
            chip = next(c for c in tabs.query(".chip") if c.value == value)
            await pilot.click(chip)
            await _settle(pilot, 0.6)
            assert dash.mtab == value
        nodes = [n.node.key for n in app.query(".gnode")]
        assert nodes[:3] == ["investigate", "code", "audit"]
        assert not app.swallowed


async def test_pause_and_cancel_from_dashboard(ids: dict[str, str], env: Path) -> None:
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        app.open_mission(ids["running"])
        await _settle(pilot, 0.8)
        head = app.query_one("#dash-controls")
        await pilot.click(_btn(app, head, "pause"))
        await _settle(pilot, 0.8)
        store = await _store(env)
        try:
            m = await store.get_mission(ids["running"])
            assert m is not None and m.status.value == "on_hold"
        finally:
            await store.aclose()
        cancel = _btn(app, head, "cancel")
        await pilot.click(cancel)  # arms
        await pilot.pause()
        store = await _store(env)
        try:
            m = await store.get_mission(ids["running"])
            assert m is not None and m.status.value == "on_hold", "one click must not cancel"
        finally:
            await store.aclose()
        await pilot.click(cancel)  # confirms
        await _settle(pilot, 0.8)
    store = await _store(env)
    try:
        m = await store.get_mission(ids["running"])
        assert m is not None and m.status.value == "cancelled"
    finally:
        await store.aclose()


# -- library & cost ---------------------------------------------------------------------


async def test_library_search_read_and_retire(ids: dict[str, str], env: Path) -> None:
    import os

    await seed_library(str(env), os.environ["REWORK_LIBRARY_DIR"])
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        await pilot.press("alt+4")
        await _settle(pilot, 0.8)
        for ch in "flaky":
            await pilot.press(ch)
        await _settle(pilot, 0.8)
        lst = app.query_one("#lib-list", RowList)
        assert len(lst.selectable) == 1
        lst.activate(lst.selectable[0], via_mouse=True)
        await _settle(pilot, 0.8)
        lib = app.view("library")
        assert lib.selected is not None and lib.selected.name == "pytest-flaky-triage"
        await pilot.click(_btn(app, lib, "retire"))
        await _settle(pilot, 0.8)
        item_id = lib.selected.id
    from rework.library.store import open_library_store

    index = await open_library_store(str(env))
    try:
        item = await index.get_item(item_id)
        assert item is not None and item.status.value == "retired"
    finally:
        await index.aclose()


async def test_library_add_item(ids: dict[str, str], env: Path) -> None:
    from rework.tui.widgets.modals import Form

    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        await pilot.press("alt+4")
        await _settle(pilot)
        await pilot.click(_btn(app, app.view("library"), "new"))
        await pilot.pause()
        form = app.screen
        assert isinstance(form, Form)
        form.query_one("#f-name").value = "uv-migration"
        form.query_one("#f-description").value = "moving CI to uv"
        form.query_one("#f-body").text = "# uv\nuse `uv sync`"
        await pilot.click(_btn(app, form, "submit"))
        await _settle(pilot, 1.2)
    from rework.library.store import open_library_store

    index = await open_library_store(str(env))
    try:
        names = [i.name for i in await index.list_items(status=None, limit=50)]
        assert "uv-migration" in names
    finally:
        await index.aclose()


async def test_cost_tab_renders_history(ids: dict[str, str]) -> None:
    from rework.tui.widgets.base import Stat

    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        await pilot.press("alt+5")
        await _settle(pilot, 1.2)
        total = app.query_one("#c-total", Stat)
        assert total._value.startswith("$") and total._value != "$0"
        heat = str(app.query_one("#c-heat").render())
        assert "Mon" in heat and "active days" in heat
        model_table = str(app.query_one("#c-model").render())
        assert "deepseek-v4p1-flash" in model_table
        assert not app.swallowed


async def test_resize_survives(ids: dict[str, str]) -> None:
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        for tab in ("alt+2", "alt+3", "alt+5", "alt+1"):
            await pilot.press(tab)
            await pilot.resize_terminal(80, 24)
            await pilot.pause()
            await pilot.resize_terminal(200, 60)
            await pilot.pause()
        assert app.is_running and not app.swallowed


# -- steering (M7 P2) ------------------------------------------------------------


async def test_choice_gate_is_answered_by_clicking_an_option(
    ids: dict[str, str], env: Path
) -> None:
    store = await _store(env)
    try:
        await store.create_gate(
            Gate(
                id="gate-choice0001",
                mission_id=ids["running"],
                kind=GateKind.CHOICE,
                question="Old parser or new parser?",
                spec={"options": ["old", "new"], "asked_by": "orchestrator"},
            )
        )
    finally:
        await store.aclose()
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot, 0.8)
        await pilot.press("alt+3")
        await _settle(pilot)
        app.query_one("#gate-list", RowList).activate("gate-choice0001", via_mouse=True)
        await _settle(pilot)
        choices = [b for b in app.query("#gate-choices Btn").results(Btn)]
        assert [b.payload for b in choices] == ["old", "new"]
        await pilot.click(choices[1])
        await _settle(pilot, 0.8)
    store = await _store(env)
    try:
        gate = await store.get_gate("gate-choice0001")
        assert gate is not None and gate.status.value == "approved"
        assert (gate.decision or {}).get("choice") == "new"
    finally:
        await store.aclose()


async def test_question_gate_requires_an_answer(ids: dict[str, str], env: Path) -> None:
    from textual.widgets import TextArea

    store = await _store(env)
    try:
        await store.create_gate(
            Gate(
                id="gate-quest0002",
                mission_id=ids["running"],
                kind=GateKind.QUESTION,
                question="Which test file?",
            )
        )
    finally:
        await store.aclose()
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot, 0.8)
        await pilot.press("alt+3")
        await _settle(pilot)
        app.query_one("#gate-list", RowList).activate("gate-quest0002", via_mouse=True)
        await _settle(pilot)
        await pilot.press("alt+a")  # no text yet: refused
        await _settle(pilot, 0.5)
        store = await _store(env)
        try:
            assert (await store.get_gate("gate-quest0002")).status.value == "open"  # type: ignore[union-attr]
        finally:
            await store.aclose()
        app.query_one("#gate-text", TextArea).text = "tests/test_stats.py"
        await pilot.press("alt+a")
        await _settle(pilot, 0.8)
    store = await _store(env)
    try:
        gate = await store.get_gate("gate-quest0002")
        assert gate is not None and gate.status.value == "approved"
        assert (gate.decision or {}).get("answer") == "tests/test_stats.py"
    finally:
        await store.aclose()


async def test_chat_messages_go_to_the_running_mission(ids: dict[str, str], env: Path) -> None:
    """A chat whose mission is running sends the owner's words to that
    mission's orchestrator (an OWNER_MESSAGE event), not to the chat model."""
    store = await _store(env)
    try:
        await store.update_chat(ids["chat"], mission_id=ids["running"])
    finally:
        await store.aclose()
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        app.query_one("#chat-list", RowList).activate(ids["chat"], via_mouse=True)
        await _settle(pilot, 1.0)
        work = app.view("work")
        assert work.target == ids["running"], "defaults to the running mission"
        knob = next(k for k in app.query(".knob") if getattr(k, "knob", "") == "to")
        assert knob.display and "mission" in str(knob.render())
        for ch in "add a test":
            await pilot.press(ch if ch != " " else "space")
        await pilot.press("enter")
        await _settle(pilot, 0.8)
    store = await _store(env)
    try:
        events = await store.events_since(0, mission_id=ids["running"], limit=500)
        owner = [e for e in events if e.type.value == "owner_message"]
        assert owner and owner[-1].payload["text"] == "add a test"
    finally:
        await store.aclose()
