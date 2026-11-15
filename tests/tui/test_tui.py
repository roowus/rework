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
from textual.widgets import Input

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


async def test_stats_say_not_loaded_instead_of_zero(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Owner screenshot: the Dashboard showed 0 / $0 everywhere while its first
    refresh had not landed, which reads as real data."""
    import asyncio as _asyncio

    from rework.tui import data as data_mod
    from rework.tui.widgets.base import Stat

    monkeypatch.setattr(data_mod, "QUERY_TIMEOUT_S", 3600.0)

    async def hang(self, fn):  # type: ignore[no-untyped-def]
        await _asyncio.sleep(3600)

    monkeypatch.setattr(data_mod.TuiData, "_call", hang)
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.3)
        await _asyncio.wait_for(pilot.press("alt+2"), timeout=3)
        await pilot.pause(0.3)
        app.view("dashboard").on_snapshot(app.hub.snapshot)
        running = app.query_one("#st-running", Stat)
        assert running._value == "—" and running._sub == "connecting…"


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


async def test_a_finished_card_shows_the_newest_events_of_a_long_mission(
    ids: dict[str, str], env: Path
) -> None:
    """Finding #37: the finished-mission backfill used to read FORWARD from seq
    0, so a mission with more events than one page left the card showing the
    OLDEST window (the stale lines the fix is about)."""
    from rework.kernel.state.records import EventRecord, EventType, MissionStatus
    from rework.tui.widgets.cards import MissionCard

    store = await _store(env)
    try:
        await store.update_mission(ids["running"], status=MissionStatus.DONE)
        for i in range(5200):  # more than the backfill's 5000-event window
            await store.append_event(
                EventRecord(
                    mission_id=ids["running"],
                    run_id=f"{ids['running']}:code:coder:1",
                    type=EventType.PROGRESS,
                    payload={"trajectory": "tool_call", "name": f"tool_{i:04d}", "arguments": ""},
                )
            )
    finally:
        await store.aclose()

    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        app.query_one("#chat-list", RowList).activate(ids["chat"], via_mouse=True)
        await _settle(pilot, 2.0)
        card = app.query_one(MissionCard)
        tail = "\n".join(card.tail)
        assert card.tail_filled
    assert "tool_5199" in tail, tail
    assert "tool_0000" not in tail, tail


async def test_a_failed_tail_backfill_does_not_freeze_the_card_empty(
    ids: dict[str, str], env: Path
) -> None:
    """Finding #37 robustness: one transient query failure must not mark the
    card filled — that would freeze an empty tail forever."""
    from rework.kernel.state.records import MissionStatus
    from rework.tui.widgets.cards import MissionCard

    store = await _store(env)
    try:
        await store.update_mission(ids["running"], status=MissionStatus.DONE)
    finally:
        await store.aclose()

    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        real = app.hub.data.mission_events_newest

        async def boom(*_a: Any, **_k: Any) -> Any:
            raise OSError("consuming input failed: Operation timed out")

        app.hub.data.mission_events_newest = boom  # type: ignore[method-assign]
        app.query_one("#chat-list", RowList).activate(ids["chat"], via_mouse=True)
        await _settle(pilot, 1.0)
        card = app.query_one(MissionCard)
        assert not card.tail_filled, "a failed fetch must not mark the card filled"

        app.hub.data.mission_events_newest = real  # type: ignore[method-assign]
        await _settle(pilot, 1.5)
        assert card.tail_filled, "the next successful poll must fill the card"
        assert card.tail, "the backfill must not leave an empty tail"


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


# -- model picker: models this process has no key for -------------------------------


def _picker_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """A catalog with one callable model and one whose key is not in this env."""
    monkeypatch.delenv("REWORK_TEST_NO_KEY", raising=False)
    monkeypatch.setenv("REWORK_TEST_KEY", "sk-test")
    monkeypatch.setenv(
        "REWORK_MODELS",
        '[{"id":"keyed","provider":"openai-compatible","model":"k","tier":2,'
        '"api_key_env":"REWORK_TEST_KEY","base_url":"http://127.0.0.1:1/v1"},'
        '{"id":"nokey","provider":"openai-compatible","model":"n","tier":1,'
        '"api_key_env":"REWORK_TEST_NO_KEY","base_url":"http://127.0.0.1:1/v1"}]',
    )


async def test_model_picker_marks_a_model_whose_key_is_missing(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Found live: the picker offered models whose API key is not in this
    process, and the failure only appeared at the first LLM call. They are
    marked `· no key` and dimmed — never hidden, never disabled: the owner can
    pick one and go add the key, and the orchestrator rejects a key-less model
    with a clear message if it really has none."""
    from rework.tui.theme import MUTED
    from rework.tui.widgets.base import Row
    from rework.tui.widgets.modals import Picker

    _picker_app(monkeypatch)
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        app.view("work").pick("model")
        await pilot.pause()
        picker = app.screen
        assert isinstance(picker, Picker), "the model knob opens the picker"
        rows = {row.key: row for row in picker.query(Row)}
        assert set(rows) == {"keyed", "nokey"}

        nokey = rows["nokey"]
        assert "no key" in str(nokey.render())
        assert nokey.has_class("-dim")
        assert nokey.styles.color.hex.lower() == MUTED.lower(), (
            "the -dim class must actually dim the row, not just sit on it"
        )
        assert nokey.display, "a key-less model is still shown"

        keyed = rows["keyed"]
        assert "no key" not in str(keyed.render())
        assert not keyed.has_class("-dim")

        # still selectable (RowList has no disabled concept) and sorted after
        # the callable ones
        lst = picker.query_one("#picker-list", RowList)
        assert lst.selectable == ["keyed", "nokey"]
        await pilot.press("enter")
        await _settle(pilot)
        assert app.view("work").model_id == "keyed"


async def test_picker_rows_carry_the_option_classes() -> None:
    """Option.classes reaches RowData/Row (the dim hook the model picker uses)."""
    from textual.app import App

    from rework.tui.widgets.base import Row
    from rework.tui.widgets.modals import Option, Picker

    picker = Picker(
        "Choose model",
        [Option("a", "a", "meta a"), Option("b", "b", "meta b", classes="-dim")],
    )

    class PickerApp(App[None]):
        def on_mount(self) -> None:
            self.push_screen(picker)

    async with PickerApp().run_test() as pilot:
        await pilot.pause()
        rows = {row.key: row for row in picker.query(Row)}
        assert not rows["a"].has_class("-dim")
        assert rows["b"].has_class("-dim")
        # the class survives a re-fill (filtering rebuilds the rows)
        picker._fill("b")
        await pilot.pause()
        rows = {row.key: row for row in picker.query(Row)}
        assert rows["b"].has_class("-dim")
        assert "meta b" in str(rows["b"].render())


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


async def test_dashboard_search_filters_and_clears(ids: dict[str, str]) -> None:
    """The search bar above the mission list: "/" focuses it, typing filters
    live (debounced), esc clears it and hands focus back to the list."""
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        await pilot.press("alt+2")
        await _settle(pilot)
        lst = app.query_one("#mission-list", RowList)
        before = len(lst.selectable)
        assert before > 1

        await pilot.press("slash")
        bar = app.query_one("#mission-search", Input)
        assert bar.has_focus, "/ focuses the search bar"
        assert bar.placeholder == "Search missions…  (/ to focus)"
        for ch in "flaky":
            await pilot.press(ch)
        await _settle(pilot, 0.8)  # the filter is debounced
        dash = app.view("dashboard")
        after = lst.selectable
        assert after == [ids["running"]], after
        assert dash.query_text == "flaky"
        # the other filters still combine with the text
        assert len(after) < before

        await pilot.press("escape")
        await _settle(pilot, 0.8)
        assert bar.value == "" and not bar.has_focus
        assert len(lst.selectable) == before, "esc clears the search"
        assert lst.has_focus
        assert app.is_running, "esc in the search bar must not arm quit"
        assert not app.swallowed


async def test_dashboard_ai_search_ranks_and_opens(
    ids: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """✦ ranks missions through a provider and the ranked rows open missions."""
    provider = script_provider(
        monkeypatch,
        [
            ChatResult(
                text=(
                    "Sure — best first:\n```json\n"
                    f'[{{"id": "{ids["running"]}", "reason": "the float tolerance fix"}}]\n'
                    "```"
                ),
                usage=Usage(input_tokens=200, output_tokens=40),
            )
        ],
    )
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        await pilot.press("alt+2")
        await _settle(pilot)
        dash = app.view("dashboard")
        await pilot.press("slash")
        for ch in "flaky":
            await pilot.press(ch)
        await _settle(pilot, 0.5)
        await pilot.click(_btn(app, dash, "ai-search"))
        await _settle(pilot, 1.5)
        assert provider.requests, "the ✦ search asked the model"
        assert "flaky" in provider.requests[0].messages[0]["content"]
        assert dash.ai_results == [{"id": ids["running"], "reason": "the float tolerance fix"}]
        lst = app.query_one("#mission-list", RowList)
        assert lst.selectable == [ids["running"]]
        assert "1 ranked" in str(app.query_one("#mission-note").render())
        row = next(r for r in app.query("#mission-list .row") if r.key == ids["running"])
        assert "float tolerance" in str(row.render()), "the reason rides the row"

        lst.activate(ids["running"], via_mouse=True)
        await _settle(pilot, 0.8)
        assert dash.mission_id == ids["running"], "a ranked row opens its mission"

        await pilot.press("escape")
        await _settle(pilot, 0.5)
        assert dash.ai_results is None
        await pilot.press("escape")
        await _settle(pilot, 0.5)
        assert dash.mission_id is None
        assert app.is_running
        assert not app.swallowed


async def test_dashboard_ai_search_without_credentials_says_so(
    ids: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No key for any catalog model: a clean notice, never a crash."""
    monkeypatch.setenv(
        "REWORK_MODELS",
        '[{"id":"haiku","provider":"anthropic","model":"claude-haiku-4-5","tier":1}]',
    )
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        await pilot.press("alt+2")
        await _settle(pilot)
        dash = app.view("dashboard")
        await pilot.press("slash")
        await pilot.press("x")
        await _settle(pilot, 0.5)
        await pilot.click(_btn(app, dash, "ai-search"))
        await _settle(pilot, 1.0)
        assert dash.ai_results is None
        assert "AI search unavailable: no model with credentials" in dash.ai_note
        assert "no model with credentials" in str(app.query_one("#mission-note").render())
        assert app.is_running
        assert not app.swallowed


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


async def test_cost_tab_active_time_sums_run_stats(ids: dict[str, str], env: Path) -> None:
    """The "active time" headline sums run_stats over the range's missions.

    Fallback semantics: a run whose artifact has no `active_seconds` contributes
    its `elapsed_seconds`; `active_seconds == 0.0` is the normal value for a
    single-call run, so it must stay 0 instead of falling back to wall time."""
    import time as _time

    from rework.kernel.state.records import Mission, MissionStatus, Run, RunStatus
    from rework.tui.widgets.base import ChipGroup, Stat

    now = _time.time()
    store = await _store(env)
    try:
        await store.create_mission(
            Mission(
                id="m-active00000a",
                objective="sum the active time of this mission's runs",
                status=MissionStatus.DONE,
                created_at=now - 600,
            )
        )
        artifacts: dict[str, Any] = {
            # 152s active; wall time (9_999s) must lose
            "a": {"run_stats": {"active_seconds": 152.0, "elapsed_seconds": 9999.0}},
            # no active_seconds key at all -> elapsed stands in
            "b": {"run_stats": {"elapsed_seconds": 100.0}},
            # one model call: active_seconds 0.0 is real, elapsed must not leak in
            "c": {"run_stats": {"active_seconds": 0.0, "elapsed_seconds": 5000.0}},
            "d": {"run_stats": {}},
            "e": None,
        }
        for key, artifact in artifacts.items():
            await store.create_run(
                Run(
                    id=f"m-active00000a:code:coder:{key}",
                    mission_id="m-active00000a",
                    role="coder",
                    step="code",
                    status=RunStatus.COMPLETED,
                    artifact=artifact,
                    created_at=now - 600,
                )
            )
        # outside the 30d range: only the "All" chip may count its 60s
        await store.create_mission(
            Mission(
                id="m-activeold00b",
                objective="an old mission with a minute of active time",
                status=MissionStatus.DONE,
                created_at=now - 40 * 86400,
                updated_at=now - 40 * 86400,
            )
        )
        await store.create_run(
            Run(
                id="m-activeold00b:code:coder:1",
                mission_id="m-activeold00b",
                role="coder",
                step="code",
                status=RunStatus.COMPLETED,
                artifact={"run_stats": {"active_seconds": 60.0}},
                created_at=now - 40 * 86400,
            )
        )
    finally:
        await store.aclose()

    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        await pilot.press("alt+5")
        await _settle(pilot, 1.2)
        active = app.query_one("#c-active", Stat)
        assert active._label == "active time"
        assert active._value == "4m 12s", active._value
        rendered = str(active.render())
        assert "4m 12s" in rendered and "active time" in rendered
        # the stat follows the range selector: "All" adds the 40-day-old run
        chips = app.query_one("#cost-range", ChipGroup)
        await pilot.click(next(c for c in chips.query(".chip") if c.value == "0"))
        await _settle(pilot, 1.2)
        assert app.query_one("#c-active", Stat)._value == "5m 12s"
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


async def test_dashboard_orchestrator_output_shows_owner_notes_and_summary(
    ids: dict[str, str], env: Path
) -> None:
    """The orchestrator session's Output tab shows its notes to the owner
    (INFO events with to_owner), oldest first, then the mission summary."""
    from textual.widgets import Static

    from rework.kernel.state.records import EventRecord, EventType
    from rework.tui.widgets.base import ChipGroup

    mid = ids["running"]
    orch = f"{mid}:orchestrate:orchestrator:1"
    note1 = "FIRST NOTE TO OWNER"
    note2 = "SECOND NOTE TO OWNER"
    hidden = "INTERNAL NOTE NOT FOR OWNER"
    summary = "FINAL SUMMARY TEXT"

    store = await _store(env)
    try:
        await store.update_mission(
            mid,
            plan={
                "kind": "fix",
                "steps": [
                    {"step": "investigate", "role": "investigator"},
                    {"step": "code", "role": "coder"},
                    {"step": "audit", "role": "auditor"},
                ],
                "gates": ["ship"],
                "orchestrator": {"model": "pro"},
            },
            result={"summary": summary},
        )
        base = 1_000_000.0
        for i, payload in enumerate(
            [
                {"note": note1, "to_owner": True},
                {"note": hidden},
                {"note": note2, "to_owner": True},
            ]
        ):
            await store.append_event(
                EventRecord(
                    mission_id=mid,
                    run_id=orch,
                    type=EventType.INFO,
                    payload=payload,
                    ts=base + i,
                )
            )
    finally:
        await store.aclose()

    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        app.open_mission(mid)
        await _settle(pilot, 1.0)
        dash = app.view("dashboard")
        runs = app.query_one("#run-list", RowList)
        assert orch in runs.keys, f"orchestrator session missing from {runs.keys}"
        runs.activate(orch, via_mouse=True)
        await _settle(pilot, 0.8)
        assert dash.run_id == orch
        tabs = app.query_one("#m-tabs", ChipGroup)
        chip = next(c for c in tabs.query(".chip") if c.value == "output")
        await pilot.click(chip)
        await _settle(pilot, 0.8)
        assert dash.mtab == "output"
        body = str(app.query_one("#m-output-body", Static).render())

    assert note1 in body and note2 in body, body
    assert hidden not in body, body
    assert body.index(note1) < body.index(note2), body
    assert summary in body, body
    assert body.index(note2) < body.index(summary), body
    assert not app.swallowed


async def test_a_finished_card_shows_its_cost(ids: dict[str, str], env: Path) -> None:
    """Found live after #9: the finished-card backfill skipped the spend fetch,
    so a done mission's card lost its cost ($7.07 on the PR-preview mission)."""
    from rework.kernel.state.records import MissionStatus
    from rework.tui.widgets.cards import MissionCard

    store = await _store(env)
    try:
        await store.update_mission(ids["running"], status=MissionStatus.DONE)
    finally:
        await store.aclose()

    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        app.query_one("#chat-list", RowList).activate(ids["chat"], via_mouse=True)
        await _settle(pilot, 2.0)
        card = app.query_one(MissionCard)
        assert card.tail_filled
        assert card.spend > 0, "a finished card must still show what it cost"


# -- preview a PR (Mission menu) ---------------------------------------------


def _git_dir(tmp_path: Path) -> Path:
    """A minimal real git repo (only rev-parse --show-toplevel has to work)."""
    import subprocess

    repo = tmp_path / "preview-repo"
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
    return repo


async def _open_preview_modal(pilot: Any, app: ReworkApp) -> Any:
    from rework.tui.widgets.modals import Form

    title = next(t for t in app.query(".menu-title") if "Mission" in str(t.render()))
    await pilot.click(title)
    await pilot.pause()
    entry = next(e for e in app.screen.query(".menu-item") if "Preview a PR" in str(e.render()))
    await pilot.click(entry)
    await _settle(pilot)
    form = app.screen
    assert isinstance(form, Form)
    return form


def _fake_subprocess(monkeypatch: pytest.MonkeyPatch, calls: list[list[str]]) -> None:
    """Nothing real is launched: both the git check and the preview are stubbed."""
    from types import SimpleNamespace

    from rework.tui import actions as actions_mod

    def fake_run(cmd: Any, *a: Any, **kw: Any) -> Any:
        calls.append([str(c) for c in cmd])
        return SimpleNamespace(returncode=0, stdout="/repo\n", stderr="")

    monkeypatch.setattr(actions_mod, "subprocess", SimpleNamespace(run=fake_run))


async def test_menu_preview_a_pr_opens_modal_with_no_mission_focused(
    ids: dict[str, str], env: Path, tmp_path: Path
) -> None:
    """The whole point: no mission focused (action_mission would refuse here)."""
    repo = _git_dir(tmp_path)
    store = await _store(env)
    try:
        await store.update_mission(ids["running"], repo_path=str(repo))
    finally:
        await store.aclose()

    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        assert app.view("work").convo is None  # nothing opened, no target mission
        form = await _open_preview_modal(pilot, app)
        assert form.query_one("#f-repo").value == str(repo)
        assert form.query_one("#f-pr").value == ""


async def _add_mission(
    store: Any,
    mid: str,
    *,
    created_at: float,
    project_id: str | None = None,
    repo_path: str | None = None,
) -> None:
    """One extra mission, seeded through the real store (newest = biggest ts)."""
    from rework.kernel.state.records import Mission, MissionStatus

    await store.create_mission(
        Mission(
            id=mid,
            objective=f"seeded mission {mid}",
            project_id=project_id,
            repo_path=repo_path,
            status=MissionStatus.DONE,
            created_at=created_at,
            updated_at=created_at,
        )
    )


async def _prefill_with_work_project(ids: dict[str, str], pilot: Any, app: ReworkApp) -> str:
    """Open the modal with the Work tab's project selected (the seed's chat)."""
    app.query_one("#chat-list", RowList).activate(ids["chat"], via_mouse=True)
    await _settle(pilot, 1.0)
    assert app.view("work").project_id == ids["project"]
    form = await _open_preview_modal(pilot, app)
    return str(form.query_one("#f-repo").value)


async def test_preview_prefill_takes_the_newest_mission_in_the_project(
    ids: dict[str, str], env: Path, tmp_path: Path
) -> None:
    """Newest wins, not oldest: two missions of the selected project, both with
    a repo path, and the prefill must be the newer one's."""
    import time as _time

    older = tmp_path / "older-repo"
    newer = tmp_path / "newer-repo"
    now = _time.time()
    store = await _store(env)
    try:
        await _add_mission(
            store,
            "m-preview000a",
            created_at=now - 7200,
            project_id=ids["project"],
            repo_path=str(older),
        )
        await _add_mission(
            store,
            "m-preview000b",
            created_at=now - 60,
            project_id=ids["project"],
            repo_path=str(newer),
        )
    finally:
        await store.aclose()

    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        assert await _prefill_with_work_project(ids, pilot, app) == str(newer)


async def test_preview_prefill_prefers_the_selected_project_over_a_newer_mission(
    ids: dict[str, str], env: Path, tmp_path: Path
) -> None:
    """Project-scoped: a NEWER mission outside the selected project must not
    override an older one inside it."""
    import time as _time

    inside = tmp_path / "inside-repo"
    outside = tmp_path / "outside-repo"
    now = _time.time()
    store = await _store(env)
    try:
        await _add_mission(
            store,
            "m-preview000c",
            created_at=now - 7200,
            project_id=ids["project"],
            repo_path=str(inside),
        )
        await _add_mission(store, "m-preview000d", created_at=now - 60, repo_path=str(outside))
    finally:
        await store.aclose()

    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        assert await _prefill_with_work_project(ids, pilot, app) == str(inside)


async def test_preview_prefill_falls_back_to_the_newest_mission_overall(
    ids: dict[str, str], env: Path, tmp_path: Path
) -> None:
    """The selected project has NO repo_path-bearing mission: the newest mission
    overall supplies the path (the seed's project missions have none)."""
    import time as _time

    older = tmp_path / "global-older-repo"
    newer = tmp_path / "global-newer-repo"
    now = _time.time()
    store = await _store(env)
    try:
        await _add_mission(store, "m-preview000e", created_at=now - 7200, repo_path=str(older))
        await _add_mission(store, "m-preview000f", created_at=now - 60, repo_path=str(newer))
    finally:
        await store.aclose()

    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        assert await _prefill_with_work_project(ids, pilot, app) == str(newer)


async def test_preview_prefill_ignores_a_newer_mission_without_a_repo_path(
    ids: dict[str, str], env: Path, tmp_path: Path
) -> None:
    """A newer mission with repo_path=None must not shadow an older one that has
    a path -- neither inside the project nor in the overall fallback."""
    import time as _time

    has_path = tmp_path / "has-path-repo"
    now = _time.time()
    store = await _store(env)
    try:
        await _add_mission(
            store,
            "m-preview000g",
            created_at=now - 7200,
            project_id=ids["project"],
            repo_path=str(has_path),
        )
        # newer, same project, but no path: must be skipped, not returned
        await _add_mission(store, "m-preview000h", created_at=now - 60, project_id=ids["project"])
        # newest mission of all, project-less and pathless: also skipped
        await _add_mission(store, "m-preview000i", created_at=now - 30)
    finally:
        await store.aclose()

    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        assert await _prefill_with_work_project(ids, pilot, app) == str(has_path)


async def test_preview_pr_refuses_non_digit_number(
    ids: dict[str, str], env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _git_dir(tmp_path)
    calls: list[list[str]] = []
    _fake_subprocess(monkeypatch, calls)
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        form = await _open_preview_modal(pilot, app)
        form.query_one("#f-pr").value = "abc"
        form.query_one("#f-repo").value = str(repo)
        await pilot.click(_btn(app, form, "submit"))
        await _settle(pilot)
    assert any("digits" in text for _, text in app.notifications_sent)
    assert calls == [], calls


async def test_preview_pr_refuses_empty_repo_path(
    ids: dict[str, str], env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    _fake_subprocess(monkeypatch, calls)
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        form = await _open_preview_modal(pilot, app)
        form.query_one("#f-pr").value = "128"
        form.query_one("#f-repo").value = "   "
        await pilot.click(_btn(app, form, "submit"))
        await _settle(pilot, 1.0)
    assert any("local git repo" in text for _, text in app.notifications_sent)
    assert calls == [], calls


async def test_preview_pr_refuses_a_path_that_is_not_a_git_repo(
    ids: dict[str, str], env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from rework.tui import actions as actions_mod

    calls: list[list[str]] = []
    monkeypatch.setattr(
        actions_mod,
        "subprocess",
        SimpleNamespace(
            run=lambda cmd, *a, **kw: (
                calls.append([str(c) for c in cmd])
                or SimpleNamespace(returncode=128, stdout="", stderr="not a repo")
            )
        ),
    )
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        form = await _open_preview_modal(pilot, app)
        form.query_one("#f-pr").value = "128"
        form.query_one("#f-repo").value = str(tmp_path / "not-a-repo")
        await pilot.click(_btn(app, form, "submit"))
        await _settle(pilot, 1.0)
    assert any("Not a git repo" in text for _, text in app.notifications_sent)
    assert all(cmd[:2] != ["rework", "preview"] for cmd in calls), calls


async def test_preview_a_pr_end_to_end_through_the_menu(
    ids: dict[str, str], env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole flow, unbroken: Mission menu -> modal -> validation -> the REAL
    subprocess.run, against a REAL git repo, with a fake `rework` on PATH.

    Nothing about the wiring is stubbed: a wrong argument order, a missing PATH
    entry or a changed callback contract fails here. `app.suspend()` runs for
    real too -- only the headless driver's "can I suspend?" flag is flipped,
    because HeadlessDriver reports can_suspend == False and Textual would then
    raise SuspendNotSupported (what the terminal handoff itself does is a no-op
    off-screen; that part stays unverified, see the handoff note)."""
    import os
    import stat

    from textual.drivers.headless_driver import HeadlessDriver

    repo = _git_dir(tmp_path)
    marker = tmp_path / "marker.txt"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "rework"
    fake.write_text('#!/bin/sh\nprintf "%s" "$*" > "$MARKER"\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("MARKER", str(marker))
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(HeadlessDriver, "can_suspend", property(lambda self: True))

    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        assert app.view("work").convo is None, "no mission focused: action_mission would refuse"
        form = await _open_preview_modal(pilot, app)
        form.query_one("#f-pr").value = "128"
        form.query_one("#f-repo").value = str(repo)
        await pilot.click(_btn(app, form, "submit"))
        await _settle(pilot)
        await app.workers.wait_for_complete()
        assert not app.swallowed, app.swallowed

    assert marker.exists(), "the real `rework` on PATH was never run"
    assert marker.read_text() == f"preview 128 --repo-dir {repo}", marker.read_text()
    assert any("Starting preview of PR 128" in t for _, t in app.notifications_sent)


async def test_the_form_hint_names_its_submit_action(ids: dict[str, str]) -> None:
    """'ctrl+s save' read oddly on the Preview a PR form; the hint follows the
    submit label."""
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        form = await _open_preview_modal(pilot, app)
        hint = str(form.query_one(".dialog-hint").render())
        assert "ctrl+s preview" in hint.lower() and "save" not in hint.lower()


# -- live sync: another instance's writes appear without a reload -----------------------
#
# Every rework instance points at the SAME store; each TUI re-reads it on its poll
# tick. These tests prove the surfaces that used to fetch once now converge: a write
# made through instance A's persistence path shows up in instance B's open view, and
# the tick after that must not duplicate it.


async def test_open_transcript_picks_up_another_instances_turn(
    ids: dict[str, str], env: Path
) -> None:
    """The open chat transcript used to be read once by _load_chat: a turn
    persisted by another instance never appeared until the chat was reopened."""
    from rework.kernel.state.records import ChatMessage

    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        work = app.view("work")
        app.query_one("#chat-list", RowList).activate(ids["chat"], via_mouse=True)
        await _settle(pilot, 0.8)
        assert work.convo is not None and work.convo.id == ids["chat"]
        before = len(app.query("#chat-log Step"))
        assert before > 0, "the seeded transcript should have rendered"

        # instance A: a turn lands in the shared store, not in this process
        store = await _store(env)
        try:
            await store.append_chat_message(
                ChatMessage(
                    chat_id=ids["chat"],
                    role="assistant",
                    body={"role": "assistant", "content": "from the other laptop"},
                    display="from the other laptop",
                )
            )
        finally:
            await store.aclose()

        await _settle(pilot, 1.2)  # one poll tick
        after = app.query("#chat-log Step")
        assert len(after) == before + 1, "the new turn was appended exactly once"
        assert "from the other laptop" in str(after[-1].render())

        await _settle(pilot, 1.2)  # a second tick: nothing new, no duplicate
        assert len(app.query("#chat-log Step")) == before + 1
        assert not app.swallowed

        # switching chats resets the cursor: reopening replays the full log once
        app.query_one("#chat-list", RowList).activate(ids["chat"], via_mouse=True)
        await _settle(pilot, 0.8)
        assert len(app.query("#chat-log Step")) == before + 1

        # a chat with no messages at all renders nothing (and stays empty)
        chat = await work.hub.data.new_chat(title="empty one")
        await _settle(pilot, 0.8)
        work.new_chat()
        await _settle(pilot, 0.8)
        assert work._chat_cursor == 0
        await work.hub.data.delete_chat(chat.id)


async def test_a_new_message_does_not_yank_a_scrolled_up_reader(
    ids: dict[str, str], env: Path
) -> None:
    """Trajectory.add only follows the bottom when the reader is at it."""
    from rework.kernel.state.records import ChatMessage

    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        app.query_one("#chat-list", RowList).activate(ids["chat"], via_mouse=True)
        await _settle(pilot, 0.8)
        store = await _store(env)
        try:
            # enough turns that the log is longer than the pane
            for n in range(40):
                await store.append_chat_message(
                    ChatMessage(
                        chat_id=ids["chat"],
                        role="user",
                        body={"role": "user", "content": f"filler {n}"},
                        display=f"filler {n}",
                    )
                )
        finally:
            await store.aclose()
        await _settle(pilot, 1.2)
        log = app.query_one("#chat-log")
        assert log.max_scroll_y > 0, "the transcript is longer than the pane"
        log.scroll_home(animate=False)
        await pilot.pause()
        at = log.scroll_y
        assert not log._at_bottom(), "the reader is scrolled up"

        store = await _store(env)
        try:
            await store.append_chat_message(
                ChatMessage(
                    chat_id=ids["chat"],
                    role="user",
                    body={"role": "user", "content": "while you were reading"},
                    display="while you were reading",
                )
            )
        finally:
            await store.aclose()
        await _settle(pilot, 1.2)
        assert "while you were reading" in str(app.query("#chat-log Step")[-1].render())
        assert log.scroll_y == at, "a new line must not scroll the reader"


async def test_own_turn_is_rendered_once_and_not_replayed_by_the_tick(
    ids: dict[str, str], env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_send` renders the turn live and persists it; the poll must not render
    it a second time (and must catch its cursor up)."""
    script_provider(
        monkeypatch,
        [
            ChatResult(
                text="looking into it", tool_calls=[], usage=Usage(input_tokens=10, output_tokens=5)
            )
        ],
    )
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        work = app.view("work")
        app.query_one("#chat-list", RowList).activate(ids["chat"], via_mouse=True)
        await _settle(pilot, 0.8)
        before = len(app.query("#chat-log Step"))

        prompt = app.query_one("#prompt")
        prompt.text = "how is it going?"
        await pilot.press("enter")
        for _ in range(60):  # wait for the scripted turn to finish, not for a fixed time
            await _settle(pilot, 0.1)
            if len(app.query("#chat-log Step")) > before and not work._sending:
                break
        await _settle(pilot, 0.3)
        after_send = len(app.query("#chat-log Step"))
        assert after_send > before
        await _settle(pilot, 1.2)  # poll ticks with no new rows
        assert len(app.query("#chat-log Step")) == after_send
        assert work._chat_cursor > 0
        assert not app.swallowed


async def test_foreign_row_written_during_own_turn_is_rendered_once(
    ids: dict[str, str], env: Path
) -> None:
    """Instance A writes while B is mid-turn: B must still render A's row once,
    and must not re-render its own (already live-rendered) rows."""
    from rework.kernel.state.records import ChatMessage

    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        work = app.view("work")
        app.query_one("#chat-list", RowList).activate(ids["chat"], via_mouse=True)
        await _settle(pilot, 0.8)

        def shown() -> str:
            return "\n".join(str(s.render()) for s in app.query("#chat-log Step"))

        work._sending = True  # B is streaming its own turn
        try:
            await app.hub.data.append_chat_note(ids["chat"], "own row from B")  # rendered live
            store = await _store(env)  # a genuinely separate handle: instance A
            try:
                await store.append_chat_message(
                    ChatMessage(chat_id=ids["chat"], role="note", body={}, display="foreign from A")
                )
            finally:
                await store.aclose()
            for _ in range(50):  # bounded wait for a tick to pick it up
                await _settle(pilot, 0.1)
                if "foreign from A" in shown():
                    break
        finally:
            work._sending = False
        await _settle(pilot, 2.5)  # more ticks after the turn: still exactly once
        text = shown()
        assert text.count("foreign from A") == 1
        assert "own row from B" not in text, "own rows are never replayed by the poll"
        assert not app.swallowed


async def test_gates_history_list_and_detail_follow_other_instances(
    ids: dict[str, str], env: Path
) -> None:
    """Decided gates were fetched once per chip change and the detail pane's
    steps paged FORWARD from seq 0 (the oldest window). Both now follow the tick."""
    from rework.kernel.state.records import EventRecord, EventType

    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        await pilot.press("alt+3")
        await _settle(pilot, 0.8)
        gates = app.view("gates")
        app.query_one("#gate-list", RowList).activate(ids["gate"], via_mouse=True)
        await _settle(pilot, 0.8)
        steps = str(app.query_one("#gate-steps").render())
        assert steps.strip(), "the selected gate's steps rendered"

        store = await _store(env)
        try:
            # instance A: the selected gate's own mission emits a new event
            await store.append_event(
                EventRecord(
                    mission_id=ids["waiting"],
                    run_id=None,
                    type=EventType.PROGRESS,
                    payload={"trajectory": "turn", "text": "a fresh step from elsewhere"},
                )
            )
        finally:
            await store.aclose()

        await _settle(pilot, 1.2)
        steps = str(app.query_one("#gate-steps").render())
        assert "a fresh step from elsewhere" in steps, "the new event was appended"
        first = steps
        await _settle(pilot, 1.2)  # a second tick must not duplicate the line
        assert str(app.query_one("#gate-steps").render()) == first

        # instance A decides a gate: the history list must show it without a
        # chip toggle (it used to be fetched once per chip change)
        from rework.tui.widgets.base import ChipGroup

        chips = gates.query_one("#gate-mode", ChipGroup)
        await pilot.click(next(c for c in chips.query(".chip") if c.value == "decided"))
        await _settle(pilot, 0.8)
        assert gates.mode == "decided"
        store = await _store(env)
        try:
            await store.decide_gate(ids["gate"], True, note="from the other laptop")
        finally:
            await store.aclose()
        await _settle(pilot, 1.2)
        assert ids["gate"] in [g.id for g in gates.history], (
            "the decided gate is in the history list"
        )
        assert not app.swallowed


async def test_library_results_and_body_follow_the_index(ids: dict[str, str], env: Path) -> None:
    """The Library tab had no on_snapshot at all: results and the reader body
    were fetched once. An item added or rewritten elsewhere now appears."""
    import os

    await seed_library(str(env), os.environ["REWORK_LIBRARY_DIR"])
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        await pilot.press("alt+4")
        await _settle(pilot, 1.0)
        lib = app.view("library")
        lst = app.query_one("#lib-list", RowList)
        target = next(k for k in lst.selectable)
        lst.activate(target, via_mouse=True)
        await _settle(pilot, 0.8)
        body_before = str(app.query_one("#lib-md")._markdown)
        assert body_before.strip(), "the selected item's body rendered"

        from rework.library.store import open_library_store

        index = await open_library_store(str(env))
        try:
            item = await index.get_item(target)
            assert item is not None
            item.description = "rewritten by the other instance"
            await index.upsert_item(item)
        finally:
            await index.aclose()

        await _settle(pilot, 1.2)
        assert any(i.description.startswith("rewritten") for i in lib.items)
        assert lib.selected is not None and lib.selected.description.startswith("rewritten")
        assert not app.swallowed


async def test_dashboard_report_body_follows_a_run_that_changes_without_a_new_run(
    ids: dict[str, str], env: Path
) -> None:
    """The report body reload key was `mission_id:len(runs)`, so a run flipping
    RUNNING -> COMPLETED with an artifact — which rewrites the body but adds no
    run — left the report stale forever. The key is now the mission's event seq."""
    import asyncio as _asyncio

    from textual.widgets import Markdown

    from rework.kernel.state.records import EventRecord, EventType, RunStatus
    from rework.tui.widgets.base import ChipGroup

    app = _app()

    async def _until(cond: Any, budget_s: float = 8.0) -> bool:
        loop = _asyncio.get_running_loop()
        end = loop.time() + budget_s
        while loop.time() < end:
            if cond():
                return True
            await pilot.pause(0.15)
        return cond()

    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        app.open_mission(ids["running"])
        await _settle(pilot, 0.8)
        dash = app.view("dashboard")
        tabs = app.query_one("#m-tabs", ChipGroup)
        await pilot.click(next(c for c in tabs.query(".chip") if c.value == "report"))
        await _settle(pilot, 0.8)
        body = app.query_one("#m-report-body", Markdown)
        assert await _until(lambda: dash.report_loaded_for is not None), "the report loaded"
        before = str(body._markdown)
        assert before.strip(), "the report body rendered"
        stamped = dash.report_loaded_for
        runs_before = len(dash.runs)

        # a tick with nothing new must NOT re-render (no per-tick flicker)
        await _settle(pilot, 1.0)
        assert dash.report_loaded_for == stamped, "an unchanged tick must not reload"

        # instance A: the coder run completes with an artifact, adding no run.
        # The engine does update_run(...COMPLETED, artifact=...) and publishes
        # the matching event; the run count stays put.
        store = await _store(env)
        try:
            coder = next(r for r in await store.list_runs(ids["running"]) if r.role == "coder")
            await store.update_run(
                coder.id,
                status=RunStatus.COMPLETED,
                artifact={
                    "kind": "coder_handoff",
                    "branch": "rework/from-the-other-laptop",
                    "commit": "0" * 40,
                    "summary": "handoff written by the other instance",
                    "diff": "--- a/x\n+++ b/x\n",
                    "run_stats": {"model_calls": 9, "cost_usd": 1.5, "elapsed_seconds": 12.0},
                },
            )
            await store.append_event(
                EventRecord(
                    mission_id=ids["running"],
                    run_id=coder.id,
                    type=EventType.ROLE_COMPLETED,
                    payload={"role": "coder", "status": "completed"},
                )
            )
        finally:
            await store.aclose()

        assert len(dash.runs) == runs_before, "the change added no run"
        assert await _until(
            lambda: "handoff written by the other instance" in str(body._markdown)
        ), "the report body must reload on the run's transition"
        after = str(body._markdown)
        assert after != before
        assert dash.report_loaded_for != stamped, "the key moved"

        # and the settled key must not reload again on the next quiet tick
        settled = dash.report_loaded_for
        await _settle(pilot, 1.0)
        assert dash.report_loaded_for == settled, "a quiet tick must not reload"
        assert str(body._markdown) == after
        assert not app.swallowed


async def test_dashboard_graph_orchestrated_missions(env: Path) -> None:
    """Orchestrated missions show one node per run, not collapsed by step."""
    # env fixture returns tmp_path, the DB is at tmp_path / "tui.sqlite3"
    # but open_sqlite needs the DB path directly from REWORK_DB env
    # Let's just use the env var that was set
    import os

    from rework.kernel.state.records import Mission, MissionStatus, Run, RunStatus
    from rework.kernel.state.sqlite import open_sqlite

    db_path = os.environ["REWORK_DB"]

    # Create an orchestrated mission with multiple runs
    store = await open_sqlite(db_path)
    try:
        mission = await store.create_mission(
            Mission(
                id="m-orch00000001",
                objective="refactor the auth module",
                status=MissionStatus.RUNNING,
                plan={
                    "kind": "fix",
                    "orchestrator": {"model": "pro"},
                    "playbook": "standard",
                    "gates": ["ship"],
                },
            )
        )

        # Create runs: 1 investigator, 3 coders, 3 auditors
        await store.create_run(
            Run(
                id=f"{mission.id}:investigate:investigator:1",
                mission_id=mission.id,
                role="investigator",
                step="investigate",
                attempt=1,
                status=RunStatus.COMPLETED,
                artifact={"summary": "Found issues in auth.py"},
            )
        )

        # Round 1: coder + auditor (changes requested)
        await store.create_run(
            Run(
                id=f"{mission.id}:code:coder:1",
                mission_id=mission.id,
                role="coder",
                step="code",
                attempt=1,
                status=RunStatus.COMPLETED,
                artifact={"branch": "rework/m-orch00000001"},
            )
        )
        await store.create_run(
            Run(
                id=f"{mission.id}:audit:auditor:1",
                mission_id=mission.id,
                role="auditor",
                step="audit",
                attempt=1,
                status=RunStatus.COMPLETED,
                context={"strictness": "standard"},
                artifact={
                    "findings": [
                        {
                            "title": "Missing test",
                            "severity": "high",
                            "detail": "...",
                            "evidence": ["test.py:1"],
                            "verdict": "CONFIRMED",
                            "confidence": 0.9,
                        },
                        {
                            "title": "Poor naming",
                            "severity": "medium",
                            "detail": "...",
                            "evidence": ["auth.py:10"],
                            "verdict": "CONFIRMED",
                            "confidence": 0.8,
                        },
                        {
                            "title": "Typo in comment",
                            "severity": "medium",
                            "detail": "...",
                            "evidence": ["auth.py:20"],
                            "verdict": "PLAUSIBLE",
                            "confidence": 0.6,
                        },
                    ]
                },
            )
        )

        # Round 2: coder revision + auditor (more changes)
        await store.create_run(
            Run(
                id=f"{mission.id}:code:coder:2",
                mission_id=mission.id,
                role="coder",
                step="code",
                attempt=2,
                status=RunStatus.COMPLETED,
                artifact={"branch": "rework/m-orch00000001"},
            )
        )
        await store.create_run(
            Run(
                id=f"{mission.id}:audit:auditor:2",
                mission_id=mission.id,
                role="auditor",
                step="audit",
                attempt=2,
                status=RunStatus.COMPLETED,
                context={"strictness": "strict"},
                artifact={
                    "findings": [
                        {
                            "title": "Still missing edge case",
                            "severity": "critical",
                            "detail": "...",
                            "evidence": ["test.py:5"],
                            "verdict": "CONFIRMED",
                            "confidence": 0.95,
                        },
                    ]
                },
            )
        )

        # Round 3: coder revision + auditor (approved)
        await store.create_run(
            Run(
                id=f"{mission.id}:code:coder:3",
                mission_id=mission.id,
                role="coder",
                step="code",
                attempt=3,
                status=RunStatus.COMPLETED,
                artifact={"branch": "rework/m-orch00000001"},
            )
        )
        await store.create_run(
            Run(
                id=f"{mission.id}:audit:auditor:3",
                mission_id=mission.id,
                role="auditor",
                step="audit",
                attempt=3,
                status=RunStatus.COMPLETED,
                context={"strictness": "standard"},
                artifact={"findings": []},
            )
        )

    finally:
        await store.aclose()

    # Now test the graph view
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        app.open_mission("m-orch00000001")
        await _settle(pilot, 1.0)

        # Click the graph tab to make nodes visible
        from rework.tui.widgets.base import ChipGroup

        tabs = app.query_one("#m-tabs", ChipGroup)
        graph_chip = next(c for c in tabs.query(".chip") if c.value == "graph")
        await pilot.click(graph_chip)
        await _settle(pilot, 0.6)

        # Check nodes are per-run
        nodes = [n.node for n in app.query(".gnode")]
        node_keys = [n.key for n in nodes]

        # Should have: orchestrator + 7 runs + ship gate + end = 10 nodes
        assert len(nodes) >= 9, f"Expected at least 9 nodes, got {len(nodes)}: {node_keys}"

        # Check that run IDs are used as keys (not step names)
        run_nodes = [n for n in nodes if n.run_id and n.run_id.startswith("m-orch")]
        assert len(run_nodes) >= 7, f"Expected 7 run nodes, got {len(run_nodes)}"

        # Check audit nodes have findings
        audit_nodes = [n for n in nodes if "auditor" in (n.run_id or "")]
        assert len(audit_nodes) == 3, f"Expected 3 audit nodes, got {len(audit_nodes)}"

        # First audit should show findings summary
        audit1_id = "m-orch00000001:audit:auditor:1"
        if audit1_id in node_keys:
            idx = node_keys.index(audit1_id)
            audit1_node = nodes[idx]
            # Should have "1 high · 2 medium" in findings
            assert audit1_node.findings, f"Audit node should have findings summary: {audit1_node}"
            assert "high" in audit1_node.findings.lower(), (
                f"Expected 'high' in findings: {audit1_node.findings}"
            )

        # Check coder revisions are marked
        coder_nodes = [n for n in nodes if "coder" in (n.run_id or "")]
        assert len(coder_nodes) >= 3, f"Expected 3 coder nodes, got {len(coder_nodes)}"

        # Second and third coders should be marked as revisions
        revision_titles = [n.title for n in coder_nodes[1:]]
        # At least one should be marked as revision
        assert any("revision" in t.lower() for t in revision_titles), (
            f"Expected revision markers in {revision_titles}"
        )

        assert not app.swallowed


async def test_dashboard_graph_parallel_auditors(env: Path) -> None:
    """Test parallel runs (two auditors spawned together after one coder)."""
    import os

    from rework.kernel.state.records import (
        EventRecord,
        EventType,
        Mission,
        MissionStatus,
        Run,
        RunStatus,
    )
    from rework.kernel.state.sqlite import open_sqlite

    store = await open_sqlite(os.environ["REWORK_DB"])
    try:
        # Create orchestrated mission
        mission = await store.create_mission(
            Mission(
                id="m-parallel00001",
                objective="Test parallel auditors",
                status=MissionStatus.RUNNING,
                plan={
                    "orchestrator": {"model": "pro"},
                    "playbook": "standard",
                    "gates": ["ship"],
                },
            )
        )

        # Round 1: one coder
        await store.create_run(
            Run(
                id=f"{mission.id}:code:coder:1",
                mission_id=mission.id,
                role="coder",
                step="code",
                attempt=1,
                status=RunStatus.COMPLETED,
                artifact={"branch": "rework/m-parallel00001"},
            )
        )

        # Create role_spawned events for the two auditors (spawned in same turn)
        seq_base = 100
        await store.append_event(
            EventRecord(
                seq=seq_base + 1,
                mission_id=mission.id,
                run_id=f"{mission.id}:audit:auditor:1",
                type=EventType.ROLE_SPAWNED,
                payload={"role": "auditor"},
            )
        )
        await store.append_event(
            EventRecord(
                seq=seq_base + 2,
                mission_id=mission.id,
                run_id=f"{mission.id}:audit:auditor:2",
                type=EventType.ROLE_SPAWNED,
                payload={"role": "auditor"},
            )
        )

        # Two auditors spawned together (same created_at timestamp)
        import time

        spawn_time = time.time()
        await store.create_run(
            Run(
                id=f"{mission.id}:audit:auditor:1",
                mission_id=mission.id,
                role="auditor",
                step="audit",
                attempt=1,
                status=RunStatus.COMPLETED,
                created_at=spawn_time,
                context={"strictness": "standard"},
                artifact={"findings": []},
            )
        )
        await store.create_run(
            Run(
                id=f"{mission.id}:audit:auditor:2",
                mission_id=mission.id,
                role="auditor",
                step="audit",
                attempt=2,
                status=RunStatus.COMPLETED,
                created_at=spawn_time,  # Same time = parallel
                context={"strictness": "strict"},
                artifact={"findings": []},
            )
        )

    finally:
        await store.aclose()

    # Test the graph view
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        app.open_mission("m-parallel00001")
        await _settle(pilot, 1.0)

        # Click the graph tab
        from rework.tui.widgets.base import ChipGroup

        tabs = app.query_one("#m-tabs", ChipGroup)
        graph_chip = next(c for c in tabs.query(".chip") if c.value == "graph")
        await pilot.click(graph_chip)
        await _settle(pilot, 0.6)

        # Check for parallel layout
        # We should have a Horizontal container for the parallel auditors
        parallel_layers = app.query(".glayer")

        # Should have at least one parallel layer (the two auditors)
        assert len(parallel_layers) >= 1, f"Expected parallel layer, got {len(parallel_layers)}"

        # Check nodes
        nodes = [n.node for n in app.query(".gnode")]
        node_keys = [n.key for n in nodes]

        # Should have: orchestrator + coder + 2 auditors + gate + end = 6 nodes
        assert len(nodes) >= 5, f"Expected at least 5 nodes, got {len(nodes)}: {node_keys}"

        # Check for both auditor run IDs
        auditor_nodes = [n for n in nodes if "auditor" in (n.run_id or "")]
        assert len(auditor_nodes) == 2, f"Expected 2 auditor nodes, got {len(auditor_nodes)}"

        assert not app.swallowed


async def test_dashboard_graph_revision_edges(env: Path) -> None:
    """Test revision loop edges with labels."""
    import os

    from rework.kernel.state.records import Mission, MissionStatus, Run, RunStatus
    from rework.kernel.state.sqlite import open_sqlite

    store = await open_sqlite(os.environ["REWORK_DB"])
    try:
        # Create orchestrated mission with revision loops
        mission = await store.create_mission(
            Mission(
                id="m-revisions001",
                objective="Test revision loops",
                status=MissionStatus.RUNNING,
                plan={
                    "orchestrator": {"model": "pro"},
                    "playbook": "standard",
                    "gates": ["ship"],
                },
            )
        )

        # Round 1: coder + audit with findings
        await store.create_run(
            Run(
                id=f"{mission.id}:code:coder:1",
                mission_id=mission.id,
                role="coder",
                step="code",
                attempt=1,
                status=RunStatus.COMPLETED,
                artifact={"branch": "rework/m-revisions001"},
            )
        )
        await store.create_run(
            Run(
                id=f"{mission.id}:audit:auditor:1",
                mission_id=mission.id,
                role="auditor",
                step="audit",
                attempt=1,
                status=RunStatus.COMPLETED,
                context={"strictness": "standard"},
                artifact={
                    "findings": [
                        {
                            "title": "Bug 1",
                            "severity": "high",
                            "detail": "...",
                            "evidence": [],
                            "verdict": "CONFIRMED",
                            "confidence": 0.9,
                        },
                        {
                            "title": "Bug 2",
                            "severity": "medium",
                            "detail": "...",
                            "evidence": [],
                            "verdict": "CONFIRMED",
                            "confidence": 0.8,
                        },
                    ]
                },
            )
        )

        # Round 2: coder revision + audit with one finding
        await store.create_run(
            Run(
                id=f"{mission.id}:code:coder:2",
                mission_id=mission.id,
                role="coder",
                step="code",
                attempt=2,
                status=RunStatus.COMPLETED,
                artifact={"branch": "rework/m-revisions001"},
            )
        )
        await store.create_run(
            Run(
                id=f"{mission.id}:audit:auditor:2",
                mission_id=mission.id,
                role="auditor",
                step="audit",
                attempt=2,
                status=RunStatus.COMPLETED,
                context={"strictness": "strict"},
                artifact={
                    "findings": [
                        {
                            "title": "Still an issue",
                            "severity": "critical",
                            "detail": "...",
                            "evidence": [],
                            "verdict": "CONFIRMED",
                            "confidence": 0.95,
                        },
                    ]
                },
            )
        )

        # Round 3: coder revision + audit approved
        await store.create_run(
            Run(
                id=f"{mission.id}:code:coder:3",
                mission_id=mission.id,
                role="coder",
                step="code",
                attempt=3,
                status=RunStatus.COMPLETED,
                artifact={"branch": "rework/m-revisions001"},
            )
        )
        await store.create_run(
            Run(
                id=f"{mission.id}:audit:auditor:3",
                mission_id=mission.id,
                role="auditor",
                step="audit",
                attempt=3,
                status=RunStatus.COMPLETED,
                context={"strictness": "standard"},
                artifact={"findings": []},
            )
        )

    finally:
        await store.aclose()

    # Test the graph view
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        app.open_mission("m-revisions001")
        await _settle(pilot, 1.0)

        # Click the graph tab
        from rework.tui.widgets.base import ChipGroup

        tabs = app.query_one("#m-tabs", ChipGroup)
        graph_chip = next(c for c in tabs.query(".chip") if c.value == "graph")
        await pilot.click(graph_chip)
        await _settle(pilot, 0.6)

        # Check nodes
        nodes = [n.node for n in app.query(".gnode")]
        node_keys = [n.key for n in nodes]

        # Should have all runs visible
        assert len(nodes) >= 8, f"Expected at least 8 nodes, got {len(nodes)}: {node_keys}"

        # Check for revision markers
        coder_nodes = [n for n in nodes if "coder" in (n.run_id or "")]
        assert len(coder_nodes) == 3, f"Expected 3 coder nodes, got {len(coder_nodes)}"

        # Second and third coders should be marked as revisions
        revision_nodes = [n for n in coder_nodes if "revision" in n.title.lower()]
        assert len(revision_nodes) >= 2, (
            f"Expected at least 2 revision nodes, got {len(revision_nodes)}"
        )

        # Check for audit findings
        audit_nodes = [n for n in nodes if "auditor" in (n.run_id or "")]
        assert len(audit_nodes) == 3, f"Expected 3 audit nodes, got {len(audit_nodes)}"

        # First audit should have findings summary
        audit1_nodes = [n for n in audit_nodes if n.run_id == "m-revisions001:audit:auditor:1"]
        assert len(audit1_nodes) == 1, "Should find first audit node"
        assert "high" in audit1_nodes[0].findings.lower(), (
            f"Expected 'high' in findings: {audit1_nodes[0].findings}"
        )

        # For now, just verify edges exist (labels are rendered but checking their text is complex)
        edges = app.query(".gedge")
        assert len(edges) >= 3, f"Expected at least 3 edges between nodes, got {len(edges)}"

        assert not app.swallowed


@pytest.mark.asyncio
async def test_dashboard_graph_two_parallel_layers(env: Path) -> None:
    """Test two separate parallel layers - round 1 has 2 auditors, round 2 has 2 auditors.

    Each .glayer Horizontal should hold exactly its own 2 nodes, not duplicated.
    """
    import os
    import time

    from rework.kernel.state.records import Mission, MissionStatus, Run, RunStatus
    from rework.kernel.state.sqlite import open_sqlite

    store = await open_sqlite(os.environ["REWORK_DB"])
    try:
        mission = await store.create_mission(
            Mission(
                id="m-twoparallel1",
                objective="Test two parallel layers",
                status=MissionStatus.RUNNING,
                plan={
                    "orchestrator": {"model": "pro"},
                    "playbook": "standard",
                    "gates": ["ship"],
                },
            )
        )

        base_time = time.time()

        # Round 1: coder + 2 parallel auditors
        await store.create_run(
            Run(
                id=f"{mission.id}:code:coder:1",
                mission_id=mission.id,
                role="coder",
                step="code",
                attempt=1,
                status=RunStatus.COMPLETED,
                created_at=base_time,
                artifact={"branch": "rework/m-twoparallel1"},
            )
        )
        # Two auditors spawned together (within 2s window)
        await store.create_run(
            Run(
                id=f"{mission.id}:audit:auditor:1",
                mission_id=mission.id,
                role="auditor",
                step="audit",
                attempt=1,
                status=RunStatus.COMPLETED,
                created_at=base_time + 10,
                context={"strictness": "standard"},
                artifact={
                    "findings": [
                        {
                            "title": "Issue A",
                            "severity": "high",
                            "detail": "...",
                            "evidence": [],
                            "verdict": "CONFIRMED",
                            "confidence": 0.9,
                        }
                    ]
                },
            )
        )
        await store.create_run(
            Run(
                id=f"{mission.id}:audit:auditor:2",
                mission_id=mission.id,
                role="auditor",
                step="audit",
                attempt=2,
                status=RunStatus.COMPLETED,
                created_at=base_time + 10.5,  # Within 2s window = parallel
                context={"strictness": "strict"},
                artifact={"findings": []},
            )
        )

        # Round 2: coder revision + 2 parallel auditors
        await store.create_run(
            Run(
                id=f"{mission.id}:code:coder:2",
                mission_id=mission.id,
                role="coder",
                step="code",
                attempt=2,
                status=RunStatus.COMPLETED,
                created_at=base_time + 120,  # Well after round 1
                artifact={"branch": "rework/m-twoparallel1"},
            )
        )
        await store.create_run(
            Run(
                id=f"{mission.id}:audit:auditor:3",
                mission_id=mission.id,
                role="auditor",
                step="audit",
                attempt=3,
                status=RunStatus.COMPLETED,
                created_at=base_time + 180,
                context={"strictness": "standard"},
                artifact={"findings": []},
            )
        )
        await store.create_run(
            Run(
                id=f"{mission.id}:audit:auditor:4",
                mission_id=mission.id,
                role="auditor",
                step="audit",
                attempt=4,
                status=RunStatus.COMPLETED,
                created_at=base_time + 180.5,  # Within 2s window = parallel
                context={"strictness": "strict"},
                artifact={"findings": []},
            )
        )

    finally:
        await store.aclose()

    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        app.open_mission("m-twoparallel1")
        await _settle(pilot, 1.0)

        from rework.tui.widgets.base import ChipGroup

        tabs = app.query_one("#m-tabs", ChipGroup)
        graph_chip = next(c for c in tabs.query(".chip") if c.value == "graph")
        await pilot.click(graph_chip)
        await _settle(pilot, 0.6)

        # Should have 2 parallel layers (one for each round's 2 auditors)
        parallel_layers = list(app.query(".glayer"))
        assert len(parallel_layers) == 2, f"Expected 2 parallel layers, got {len(parallel_layers)}"

        # Each layer should have exactly 2 nodes
        for i, layer in enumerate(parallel_layers):
            nodes_in_layer = list(layer.query(".gnode"))
            assert len(nodes_in_layer) == 2, (
                f"Layer {i} should have 2 nodes, got {len(nodes_in_layer)}"
            )
            # They should have different run IDs
            run_ids = [n.node.run_id for n in nodes_in_layer]
            assert len(set(run_ids)) == 2, f"Layer {i} nodes should be distinct: {run_ids}"

        # Verify total: orch + coder1 + 2 auditors + coder2 + 2 auditors + gate + end = 9
        all_nodes = list(app.query(".gnode"))
        assert len(all_nodes) == 9, f"Expected 9 nodes total, got {len(all_nodes)}"

        assert not app.swallowed


@pytest.mark.asyncio
async def test_dashboard_graph_mixed_artifacts(env: Path) -> None:
    """Test that layers are not dropped when some runs have artifacts and some don't.

    Regression test for the bug where mission_layers silently dropped layers when
    some audit runs had artifact=None or empty findings.
    """
    import os
    import time

    from rework.kernel.state.records import Mission, MissionStatus, Run, RunStatus
    from rework.kernel.state.sqlite import open_sqlite

    store = await open_sqlite(os.environ["REWORK_DB"])
    try:
        mission = await store.create_mission(
            Mission(
                id="m-mixedartif1",
                objective="Test mixed artifact presence",
                status=MissionStatus.RUNNING,
                plan={
                    "orchestrator": {"model": "pro"},
                    "playbook": "standard",
                    "gates": ["ship"],
                },
            )
        )

        base_time = time.time()

        # Round 1: coder + auditor WITH findings
        await store.create_run(
            Run(
                id=f"{mission.id}:code:coder:1",
                mission_id=mission.id,
                role="coder",
                step="code",
                attempt=1,
                status=RunStatus.COMPLETED,
                created_at=base_time,
                artifact={"branch": "rework/m-mixedartif1"},
            )
        )
        await store.create_run(
            Run(
                id=f"{mission.id}:audit:auditor:1",
                mission_id=mission.id,
                role="auditor",
                step="audit",
                attempt=1,
                status=RunStatus.COMPLETED,
                created_at=base_time + 60,
                context={"strictness": "standard"},
                artifact={
                    "findings": [
                        {
                            "title": "Bug",
                            "severity": "high",
                            "detail": "...",
                            "evidence": [],
                            "verdict": "CONFIRMED",
                            "confidence": 0.9,
                        }
                    ]
                },
            )
        )

        # Round 2: coder + auditor WITHOUT artifact (still running or no findings yet)
        await store.create_run(
            Run(
                id=f"{mission.id}:code:coder:2",
                mission_id=mission.id,
                role="coder",
                step="code",
                attempt=2,
                status=RunStatus.COMPLETED,
                created_at=base_time + 120,
                artifact={"branch": "rework/m-mixedartif1"},
            )
        )
        await store.create_run(
            Run(
                id=f"{mission.id}:audit:auditor:2",
                mission_id=mission.id,
                role="auditor",
                step="audit",
                attempt=2,
                status=RunStatus.RUNNING,  # Still running - no artifact yet
                created_at=base_time + 180,
                context={"strictness": "standard"},
                artifact=None,  # No artifact
            )
        )

        # Round 3: coder + auditor with EMPTY artifact
        await store.create_run(
            Run(
                id=f"{mission.id}:code:coder:3",
                mission_id=mission.id,
                role="coder",
                step="code",
                attempt=3,
                status=RunStatus.COMPLETED,
                created_at=base_time + 240,
                artifact={"branch": "rework/m-mixedartif1"},
            )
        )
        await store.create_run(
            Run(
                id=f"{mission.id}:audit:auditor:3",
                mission_id=mission.id,
                role="auditor",
                step="audit",
                attempt=3,
                status=RunStatus.COMPLETED,
                created_at=base_time + 300,
                context={"strictness": "standard"},
                artifact={},  # Empty artifact
            )
        )

    finally:
        await store.aclose()

    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        app.open_mission("m-mixedartif1")
        await _settle(pilot, 1.0)

        from rework.tui.widgets.base import ChipGroup

        tabs = app.query_one("#m-tabs", ChipGroup)
        graph_chip = next(c for c in tabs.query(".chip") if c.value == "graph")
        await pilot.click(graph_chip)
        await _settle(pilot, 0.6)

        # Check that all 6 runs are rendered as nodes (plus orchestrator, gate, end)
        all_nodes = list(app.query(".gnode"))
        node_keys = [n.node.key for n in all_nodes]

        # Expected: orchestrator + 3 coders + 3 auditors + gate + end = 9 nodes
        assert len(all_nodes) == 9, (
            f"Expected 9 nodes (all runs visible), got {len(all_nodes)}: {node_keys}"
        )

        # Verify each run is present
        expected_runs = [
            f"{mission.id}:code:coder:1",
            f"{mission.id}:audit:auditor:1",
            f"{mission.id}:code:coder:2",
            f"{mission.id}:audit:auditor:2",
            f"{mission.id}:code:coder:3",
            f"{mission.id}:audit:auditor:3",
        ]
        for run_id in expected_runs:
            assert run_id in node_keys, f"Missing node for run {run_id}"

        assert not app.swallowed


@pytest.mark.asyncio
async def test_dashboard_graph_sequential_not_merged(env: Path) -> None:
    """Test that sequential revision-loop spawns are NOT merged into parallel groups.

    Regression test for the brittle parallel-detection heuristic: sequential runs
    (coder -> auditor -> coder) should each be their own layer, even if their
    spawn events have adjacent sequence numbers.
    """
    import os
    import time

    from rework.kernel.state.records import Mission, MissionStatus, Run, RunStatus
    from rework.kernel.state.sqlite import open_sqlite

    store = await open_sqlite(os.environ["REWORK_DB"])
    try:
        mission = await store.create_mission(
            Mission(
                id="m-sequential1",
                objective="Test sequential runs stay separate",
                status=MissionStatus.DONE,
                plan={
                    "orchestrator": {"model": "pro"},
                    "playbook": "standard",
                    "gates": [],
                },
            )
        )

        base_time = time.time()

        # 3 sequential rounds, each spawned well apart in time
        # Even if their event seq numbers were 1,2,3,4,5,6, they should NOT merge
        for i in range(3):
            # Each round is 100 seconds apart (well beyond the 2s parallel window)
            round_time = base_time + (i * 100)
            await store.create_run(
                Run(
                    id=f"{mission.id}:code:coder:{i + 1}",
                    mission_id=mission.id,
                    role="coder",
                    step="code",
                    attempt=i + 1,
                    status=RunStatus.COMPLETED,
                    created_at=round_time,
                    artifact={"branch": f"rework/m-sequential1-{i + 1}"},
                )
            )
            await store.create_run(
                Run(
                    id=f"{mission.id}:audit:auditor:{i + 1}",
                    mission_id=mission.id,
                    role="auditor",
                    step="audit",
                    attempt=i + 1,
                    status=RunStatus.COMPLETED,
                    created_at=round_time + 50,  # 50 seconds later
                    context={"strictness": "standard"},
                    artifact={
                        "findings": []
                        if i == 2
                        else [
                            {
                                "title": "Issue",
                                "severity": "high",
                                "detail": "...",
                                "evidence": [],
                                "verdict": "CONFIRMED",
                                "confidence": 0.9,
                            }
                        ]
                    },
                )
            )

    finally:
        await store.aclose()

    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        app.open_mission("m-sequential1")
        await _settle(pilot, 1.0)

        from rework.tui.widgets.base import ChipGroup

        tabs = app.query_one("#m-tabs", ChipGroup)
        graph_chip = next(c for c in tabs.query(".chip") if c.value == "graph")
        await pilot.click(graph_chip)
        await _settle(pilot, 0.6)

        # Should NOT have any parallel layers - all runs should be sequential
        parallel_layers = list(app.query(".glayer"))
        assert len(parallel_layers) == 0, (
            f"Expected 0 parallel layers (all sequential), got {len(parallel_layers)}"
        )

        # Each run should be in its own layer - 6 runs = 6 run layers
        # Plus orchestrator + end = 8 total layers
        # Count by edges (n layers = n-1 edges for a linear graph)
        edges = list(app.query(".gedge"))
        # orchestrator -> coder1 -> audit1 -> coder2 -> audit2 -> coder3 -> audit3 -> end = 7 edges
        assert len(edges) == 7, f"Expected 7 edges (8 layers), got {len(edges)}"

        assert not app.swallowed


@pytest.mark.asyncio
async def test_dashboard_graph_collapse_many_rounds(env: Path) -> None:
    """Test that graphs with many rounds collapse old rounds correctly.

    Tests: title says "rounds 1-N" with correct round count, status reflects
    actual run statuses, and the right number of rounds are visible.
    """
    import os
    import time

    from rework.kernel.state.records import Mission, MissionStatus, Run, RunStatus
    from rework.kernel.state.sqlite import open_sqlite

    store = await open_sqlite(os.environ["REWORK_DB"])
    try:
        mission = await store.create_mission(
            Mission(
                id="m-manyround1",
                objective="Test collapse of many rounds",
                status=MissionStatus.DONE,
                plan={
                    "orchestrator": {"model": "pro"},
                    "playbook": "standard",
                    "gates": [],
                },
            )
        )

        base_time = time.time()

        # Create 7 rounds (more than COLLAPSE_ROUND_THRESHOLD=5)
        # This should trigger collapsing to keep ~4 visible
        for i in range(7):
            round_time = base_time + (i * 100)
            status = RunStatus.COMPLETED
            # Make one early round failed to test status derivation
            if i == 1:
                status = RunStatus.FAILED
            await store.create_run(
                Run(
                    id=f"{mission.id}:code:coder:{i + 1}",
                    mission_id=mission.id,
                    role="coder",
                    step="code",
                    attempt=i + 1,
                    status=status,
                    created_at=round_time,
                    artifact={"branch": f"rework/m-manyround1-{i + 1}"},
                )
            )
            await store.create_run(
                Run(
                    id=f"{mission.id}:audit:auditor:{i + 1}",
                    mission_id=mission.id,
                    role="auditor",
                    step="audit",
                    attempt=i + 1,
                    status=RunStatus.COMPLETED,
                    created_at=round_time + 50,
                    context={"strictness": "standard"},
                    artifact={"findings": []},
                )
            )

    finally:
        await store.aclose()

    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        app.open_mission("m-manyround1")
        await _settle(pilot, 1.0)

        from rework.tui.widgets.base import ChipGroup

        tabs = app.query_one("#m-tabs", ChipGroup)
        graph_chip = next(c for c in tabs.query(".chip") if c.value == "graph")
        await pilot.click(graph_chip)
        await _settle(pilot, 0.6)

        # Find the collapsed summary node
        all_nodes = list(app.query(".gnode"))
        collapsed_nodes = [n for n in all_nodes if "collapsed" in n.node.key]

        assert len(collapsed_nodes) == 1, f"Expected 1 collapsed node, got {len(collapsed_nodes)}"

        collapsed = collapsed_nodes[0].node
        # Title should say "rounds 1-X (collapsed)" where X is the round count, not group count
        assert "rounds 1-" in collapsed.title.lower(), (
            f"Title should start with 'rounds 1-': {collapsed.title}"
        )
        assert "(collapsed)" in collapsed.title.lower(), (
            f"Title should contain '(collapsed)': {collapsed.title}"
        )

        # Status should reflect the failed run (round 2 had a failed coder)
        # The collapsed status should be "failed" since there's a failure in the collapsed rounds
        assert collapsed.status == "failed", (
            f"Collapsed status should be 'failed' (round 2 failed), got: {collapsed.status}"
        )

        assert not app.swallowed


@pytest.mark.asyncio
async def test_dashboard_graph_owner_sequence_rounds(env: Path) -> None:
    """The owner's real mission (m-e825d222884d): investigator, then three review
    rounds of two coders each - each followed by the audit that reviewed it.

    The seed reproduces the real flag pattern: EVERY coder 2-6 carries
    context["continue_branch"], including coder 3, the revision spawned after
    auditor 1. A round is the coders between two audits plus that audit, so the
    flag joins coders inside a round but never bridges across an audit.

    Every run appears exactly once, in order; the round's verdict labels the edge
    into its audit; identities come from the run id ('coder #5', never 'coder 4');
    the header flow line follows the same run-derived rounds.
    """
    import os
    import time

    from rework.kernel.state.records import Mission, MissionStatus, Run, RunStatus
    from rework.kernel.state.sqlite import open_sqlite

    store = await open_sqlite(os.environ["REWORK_DB"])
    base = time.time()
    try:
        mission = await store.create_mission(
            Mission(
                id="m-ownerflow01",
                objective="Owner sequence: three review rounds",
                status=MissionStatus.DONE,
                plan={
                    "orchestrator": {"model": "pro"},
                    "playbook": "standard",
                    "gates": ["open_pr"],
                },
            )
        )

        def coder(n: int, *, continues: bool = False) -> Run:
            return Run(
                id=f"{mission.id}:code:coder:{n}",
                mission_id=mission.id,
                role="coder",
                step="code",
                attempt=n,
                status=RunStatus.COMPLETED,
                created_at=base + n + 1 + (n - 1) // 2,
                context={"continue_branch": True} if continues else {},
                artifact={"branch": "rework/m-ownerflow01"},
            )

        def auditor(n: int, findings: list[dict[str, Any]]) -> Run:
            return Run(
                id=f"{mission.id}:audit:auditor:{n}",
                mission_id=mission.id,
                role="auditor",
                step="audit",
                attempt=n,
                status=RunStatus.COMPLETED,
                created_at=base + 3 * n + 1,
                context={"strictness": "standard"},
                artifact={"findings": findings},
            )

        def finding(severity: str) -> dict[str, Any]:
            return {
                "title": f"{severity} issue",
                "severity": severity,
                "detail": "...",
                "evidence": [],
                "verdict": "CONFIRMED",
                "confidence": 0.9,
            }

        await store.create_run(
            Run(
                id=f"{mission.id}:investigate:investigator:1",
                mission_id=mission.id,
                role="investigator",
                step="investigate",
                attempt=1,
                status=RunStatus.COMPLETED,
                created_at=base,
                artifact={"summary": "scope"},
            )
        )
        # round 1: coders 1-2 (2 continues 1), audited by auditor 1 -> approve with fixes
        await store.create_run(coder(1))
        await store.create_run(coder(2, continues=True))
        await store.create_run(auditor(1, [finding("high"), finding("high")]))
        # round 2: coders 3-4, audited by auditor 2 -> request changes.
        # coder 3 is the REAL post-audit revision: it carries continue_branch
        # too, and must still open a new round instead of bridging the audit.
        await store.create_run(coder(3, continues=True))
        await store.create_run(coder(4, continues=True))
        await store.create_run(auditor(2, [finding("critical")]))
        # round 3: coders 5-6, audited by auditor 3 -> approve
        await store.create_run(coder(5, continues=True))
        await store.create_run(coder(6, continues=True))
        await store.create_run(auditor(3, []))

        stored_runs = await store.list_runs(mission.id)
        stored_gates = await store.list_gates(mission.id)
    finally:
        await store.aclose()

    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        app.open_mission("m-ownerflow01")
        await _settle(pilot, 1.0)

        from textual.widgets import Static

        # the header flow line is derived from the same runs as the graph
        flow = str(app.query_one("#m-flow", Static).render())

        from rework.tui.widgets.base import ChipGroup
        from rework.tui.widgets.graph import mission_flow_summary

        tokens = mission_flow_summary(mission, stored_runs, stored_gates)
        assert tokens == [
            "investigator",
            "3 rounds (6 coders, 3 audits)",
            "open_pr gate",
            "end",
        ], tokens
        cursor = -1
        for token in tokens:
            cursor = flow.find(token, cursor + 1)
            assert cursor >= 0, f"header flow line missing {token!r}: {flow!r}"

        tabs = app.query_one("#m-tabs", ChipGroup)
        graph_chip = next(c for c in tabs.query(".chip") if c.value == "graph")
        await pilot.click(graph_chip)
        await _settle(pilot, 0.6)

        nodes = [n.node for n in app.query(".gnode")]
        keys = [n.key for n in nodes]

        # every one of the 10 runs, exactly once, in order - nothing folded away
        run_keys = [k for k in keys if k.startswith("m-ownerflow01:")]
        assert run_keys == [r.id for r in stored_runs], run_keys
        assert len(run_keys) == len(set(run_keys)) == 10, run_keys

        # the round structure: investigator, coder layer, audit layer, x3, gate, end
        assert keys[0] == "orchestrator", keys
        assert [k for k in keys if not k.startswith("m-ownerflow01:")] == [
            "orchestrator",
            "gate:open_pr",
            "end",
        ], keys
        coder_layers = [
            [n.node.key for n in layer.query(".gnode")] for layer in app.query(".glayer")
        ]
        assert coder_layers == [
            [f"m-ownerflow01:code:coder:{i}" for i in (1, 2)],
            [f"m-ownerflow01:code:coder:{i}" for i in (3, 4)],
            [f"m-ownerflow01:code:coder:{i}" for i in (5, 6)],
        ], coder_layers

        # identities come from the run id, so the last round is coders 5-6
        titles = {n.key: n.title for n in nodes}
        assert titles["m-ownerflow01:code:coder:1"] == "coder", titles
        assert titles["m-ownerflow01:code:coder:2"] == "coder #2", titles
        assert titles["m-ownerflow01:audit:auditor:1"] == "auditor", titles
        assert titles["m-ownerflow01:code:coder:3"] == "coder #3 (revision)", titles
        assert titles["m-ownerflow01:code:coder:4"] == "coder #4", titles
        assert titles["m-ownerflow01:audit:auditor:2"] == "auditor #2", titles
        assert titles["m-ownerflow01:code:coder:5"] == "coder #5 (revision)", titles
        assert titles["m-ownerflow01:code:coder:6"] == "coder #6", titles
        assert titles["m-ownerflow01:audit:auditor:3"] == "auditor #3", titles
        assert "coder 4" not in titles.values(), titles

        # the continuation is called out on the continuing coder
        coder6 = next(n for n in nodes if n.key == "m-ownerflow01:code:coder:6")
        assert "coders 5-6" in coder6.sub and "continues coder #5" in coder6.sub, coder6.sub

        # the review verdict labels BOTH sides of each audit at mission level:
        # into the audit the findings ('approve with fixes: 2 high'), out of it
        # the direction toward the next coder revision or the mission's gates
        # ('request changes' -> 'approve'). An unlabelled edge is just '│\n▼';
        # a labelled one carries the label on its last line.
        edge_labels = [str(e.render()).splitlines()[-1].strip() for e in app.query(".gedge")]
        assert len(edge_labels) == 9, edge_labels
        assert [label for label in edge_labels if label != "▼"] == [
            "approve with fixes: 2 high",  # into auditor 1
            "approve with fixes",  # auditor 1 -> coder #3 (revision)
            "request changes: 1 critical",  # into auditor 2
            "request changes",  # auditor 2 -> coder #5 (revision)
            "approve",  # into auditor 3
            "approve",  # auditor 3 -> the mission proceeds to its gate
        ], edge_labels

        # a labelled edge is tall enough to be seen: '│', '▼', label (3 rows)
        for edge in app.query(".gedge"):
            rows = str(edge.render()).splitlines()
            assert edge.size.height >= len(rows), (edge.size, rows)

        # 3 review rounds is under the collapse threshold: the loop stays visible
        assert not [n for n in nodes if n.expandable], keys

        assert not app.swallowed


@pytest.mark.asyncio
async def test_dashboard_graph_collapsed_rounds_toggle(env: Path) -> None:
    """Clicking the collapsed-round summary expands the folded rounds, clicking
    it again folds them back (the summary node is expandable, run_id-less)."""
    import os
    import time

    from rework.kernel.state.records import Mission, MissionStatus, Run, RunStatus
    from rework.kernel.state.sqlite import open_sqlite

    store = await open_sqlite(os.environ["REWORK_DB"])
    try:
        mission = await store.create_mission(
            Mission(
                id="m-toggle000001",
                objective="Toggle collapsed rounds",
                status=MissionStatus.DONE,
                plan={"orchestrator": {"model": "pro"}, "playbook": "standard", "gates": []},
            )
        )
        base = time.time()
        for i in range(1, 8):  # 7 rounds > COLLAPSE_ROUND_THRESHOLD
            await store.create_run(
                Run(
                    id=f"{mission.id}:code:coder:{i}",
                    mission_id=mission.id,
                    role="coder",
                    step="code",
                    attempt=i,
                    status=RunStatus.COMPLETED,
                    created_at=base + i * 100,
                    artifact={"branch": f"rework/m-toggle000001-{i}"},
                )
            )
            await store.create_run(
                Run(
                    id=f"{mission.id}:audit:auditor:{i}",
                    mission_id=mission.id,
                    role="auditor",
                    step="audit",
                    attempt=i,
                    status=RunStatus.COMPLETED,
                    created_at=base + i * 100 + 50,
                    context={"strictness": "standard"},
                    artifact={"findings": []},
                )
            )
    finally:
        await store.aclose()

    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        app.open_mission("m-toggle000001")
        await _settle(pilot, 1.0)

        from rework.tui.widgets.base import ChipGroup

        tabs = app.query_one("#m-tabs", ChipGroup)
        graph_chip = next(c for c in tabs.query(".chip") if c.value == "graph")
        await pilot.click(graph_chip)
        await _settle(pilot, 0.6)

        def keys() -> list[str]:
            return [n.node.key for n in app.query(".gnode")]

        folded = keys()
        # 7 rounds, keep the latest 2: rounds 1-5 fold, rounds 6-7 stay visible
        assert folded == [
            "orchestrator",
            "rounds-collapsed",
            "m-toggle000001:code:coder:6",
            "m-toggle000001:audit:auditor:6",
            "m-toggle000001:code:coder:7",
            "m-toggle000001:audit:auditor:7",
            "end",
        ], folded

        summary = next(n for n in app.query(".gnode") if n.node.key == "rounds-collapsed")
        assert summary.node.expandable and summary.node.run_id is None
        assert summary.node.title == "rounds 1-5 (collapsed)", summary.node.title
        assert "10 runs" in summary.node.sub, summary.node.sub

        # click the summary: the folded rounds appear
        await pilot.click(summary)
        await _settle(pilot, 0.6)
        expanded = keys()
        assert "rounds-collapsed" in expanded, expanded
        assert all(f"m-toggle000001:code:coder:{i}" in expanded for i in range(1, 6)), expanded
        assert all(f"m-toggle000001:audit:auditor:{i}" in expanded for i in range(1, 6)), expanded
        # orchestrator + the summary (still shown, now "(expanded)") + 14 runs + end
        assert len(expanded) == 17, expanded
        summary = next(n for n in app.query(".gnode") if n.node.key == "rounds-collapsed")
        assert summary.node.title == "rounds 1-5 (expanded)", summary.node.title

        # click again: back to the folded default
        await pilot.click(summary)
        await _settle(pilot, 0.6)
        assert keys() == folded, keys()

        assert not app.swallowed


async def test_dashboard_ai_search_deadline_covers_the_run_fetch(
    ids: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hung run fetch must not outlive the search budget: the ranking still
    happens (summaries without run output) and the bar never sticks on ✦."""
    import asyncio

    from rework.tui import search

    class _Provider:
        name = "fake"

        def __init__(self) -> None:
            self.requests: list[Any] = []

        async def complete(self, request: Any) -> ChatResult:
            self.requests.append(request)
            return ChatResult(
                text=f'[{{"id": "{ids["running"]}", "reason": "still the closest"}}]',
                usage=Usage(input_tokens=200, output_tokens=20),
            )

    provider = _Provider()
    import rework.kernel.llm.adapters as adapters

    monkeypatch.setattr(adapters, "build_provider", lambda entry, extra=None: provider)
    monkeypatch.setattr(search, "TIMEOUT_S", 0.6)
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        await pilot.press("alt+2")
        await _settle(pilot)
        dash = app.view("dashboard")

        async def hung(mission_id: str) -> list[Any]:
            await asyncio.sleep(30)
            return []

        monkeypatch.setattr(dash.hub.data, "mission_runs", hung, raising=False)
        await pilot.press("slash")
        for ch in "flaky":
            await pilot.press(ch)
        await _settle(pilot, 0.5)
        started = asyncio.get_running_loop().time()
        await pilot.click(_btn(app, dash, "ai-search"))
        await _settle(pilot, 2.0)
        assert asyncio.get_running_loop().time() - started < 4.0, "the budget held"
        assert provider.requests, "a hung fetch still reaches the model"
        assert dash.ai_results == [{"id": ids["running"], "reason": "still the closest"}]
        assert dash._ai_busy is False
        assert "searching" not in str(app.query_one("#mission-note").render())
        assert app.is_running


async def test_dashboard_escape_dismisses_an_in_flight_ai_search(
    ids: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """esc while ✦ is still running dismisses it — it never arms app quit."""
    import asyncio

    class _Slow:
        name = "fake"

        def __init__(self) -> None:
            self.requests: list[Any] = []

        async def complete(self, request: Any) -> ChatResult:
            self.requests.append(request)
            await asyncio.sleep(3)
            return ChatResult(text=f'[{{"id": "{ids["running"]}", "reason": "late"}}]')

    provider = _Slow()
    import rework.kernel.llm.adapters as adapters

    monkeypatch.setattr(adapters, "build_provider", lambda entry, extra=None: provider)
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        await pilot.press("alt+2")
        await _settle(pilot)
        dash = app.view("dashboard")
        await pilot.press("slash")
        for ch in "flaky":
            await pilot.press(ch)
        await _settle(pilot, 0.5)
        await pilot.click(_btn(app, dash, "ai-search"))
        await _settle(pilot, 0.4)
        assert dash._ai_busy is True, "the search is in flight"
        await pilot.press("escape")
        await _settle(pilot, 0.6)
        assert dash._ai_busy is False
        assert dash.ai_results is None
        assert dash.ai_note == ""
        assert app.is_running, "esc did not arm quit"
        assert not app.swallowed


# -- repo-less (upload) projects -------------------------------------------------


async def test_create_project_derives_transport_from_its_repos(env: Path) -> None:
    """The TUI must never write a settings dict that contradicts itself: no
    repo means upload/fetch, a repo means git/push_branch."""
    from rework.tui.data import TuiData

    data = TuiData()
    try:
        plain = await data.create_project("p")
        assert plain.settings["transport"] == "upload"
        assert plain.settings["results"] == "fetch"
        assert plain.settings["repos"] == []
        with_repo = await data.create_project(
            "p2", [{"name": "r", "kind": "github", "url": "https://github.com/a/b"}]
        )
        assert with_repo.settings["transport"] == "git"
        assert with_repo.settings["results"] == "push_branch"
    finally:
        await data.aclose()


async def _launch_fix_proposal(
    app: ReworkApp, pilot: Any, ids: dict[str, str], env: Path, **proposal: Any
) -> list[Any]:
    """Type one message, click the proposal's Launch, return the NEW missions."""
    from rework.tui.widgets.cards import ProposalCard

    store = await _store(env)
    try:
        before = {m.id for m in await store.list_missions(limit=100)}
    finally:
        await store.aclose()
    text = proposal.pop("say")
    for ch in text:
        await pilot.press(ch if ch != " " else "space")
    await pilot.press("enter")
    await _settle(pilot, 1.0)
    cards = list(app.query(ProposalCard))
    assert cards, "the proposal must render as a card"
    await pilot.click(_btn(app, cards[-1], "launch"))
    await _settle(pilot, 1.0)
    store = await _store(env)
    try:
        return [m for m in await store.list_missions(limit=100) if m.id not in before]
    finally:
        await store.aclose()


async def test_fix_on_a_repo_less_project_launches_without_a_repo(
    ids: dict[str, str], env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fix mission on an upload project supplies its files from the project's
    files area, so no repo is required and none is invented."""
    script_provider(
        monkeypatch,
        [propose(objective="tidy the notes", plan="fix", project="rework")],
    )
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        new = await _launch_fix_proposal(app, pilot, ids, env, say="tidy the notes")
        assert not app.swallowed
    assert len(new) == 1, new
    mission = new[0]
    assert mission.project_id == ids["upload_project"]
    assert not mission.repo_origin and not mission.repo_path, "an upload project names no repo"


async def test_a_repo_named_on_an_upload_project_is_dropped(
    ids: dict[str, str], env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The project's transport decides: a model that names a repo out of habit
    must not turn an upload-project mission into a clone of something else."""
    script_provider(
        monkeypatch,
        [
            propose(
                objective="tidy the notes",
                plan="fix",
                project="rework",
                repo="https://github.com/x/elsewhere",
            )
        ],
    )
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        new = await _launch_fix_proposal(app, pilot, ids, env, say="tidy the notes")
        assert not app.swallowed
    assert len(new) == 1, new
    assert new[0].project_id == ids["upload_project"]
    assert not new[0].repo_origin and not new[0].repo_path, "the files area supplies the files"


async def test_fix_on_a_git_project_still_passes_its_repo_through(
    ids: dict[str, str], env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script_provider(
        monkeypatch,
        [
            propose(
                objective="fix the flaky test",
                plan="fix",
                project="polystats",
                repo="https://github.com/x/polystats",
            )
        ],
    )
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        new = await _launch_fix_proposal(app, pilot, ids, env, say="fix the flaky test")
        assert not app.swallowed
    assert len(new) == 1, new
    assert new[0].project_id == ids["project"]
    assert new[0].repo_origin == "https://github.com/x/polystats"


async def test_add_files_pushes_parsed_paths_into_an_upload_project(
    ids: dict[str, str], env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rework.tui.data import TuiData
    from rework.tui.widgets.modals import Form

    seen: list[tuple[str, list[str]]] = []

    async def fake(self: Any, project_id: str, paths: list[str]) -> list[str]:
        seen.append((project_id, paths))
        return ["notes.md (new)"]

    monkeypatch.setattr(TuiData, "add_project_files", fake)
    monkeypatch.setenv("HOME", str(tmp_path))
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        app.action_tab("dashboard")
        await _settle(pilot)
        dash = app.view("dashboard")
        dash.project = ids["upload_project"]
        await pilot.click(_btn(app, dash, "add-files"))
        await _settle(pilot)
        form = app.screen
        assert isinstance(form, Form)
        form.query_one("#f-paths").text = "~/notes.md\na.md"
        await pilot.click(_btn(app, form, "submit"))
        await _settle(pilot, 1.0)
        assert not app.swallowed
    assert seen == [(ids["upload_project"], [str(tmp_path / "notes.md"), "a.md"])], seen
    assert any("1 path(s) added" in text for _, text in app.notifications_sent)


async def test_add_files_accepts_a_path_with_a_space(
    ids: dict[str, str], env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The form split its whole input on spaces, so `My Papers` could not be
    entered at all: one path per line must survive the spaces."""
    from rework.tui.data import TuiData
    from rework.tui.widgets.modals import Form

    seen: list[tuple[str, list[str]]] = []
    spaced = tmp_path / "My Papers"
    spaced.mkdir()
    (spaced / "notes.md").write_text("# n\n")

    async def fake(self: Any, project_id: str, paths: list[str]) -> list[str]:
        seen.append((project_id, paths))
        return ["notes.md (new)"]

    monkeypatch.setattr(TuiData, "add_project_files", fake)
    monkeypatch.setenv("HOME", str(tmp_path))
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        app.action_tab("dashboard")
        await _settle(pilot)
        dash = app.view("dashboard")
        dash.project = ids["upload_project"]
        await pilot.click(_btn(app, dash, "add-files"))
        await _settle(pilot)
        form = app.screen
        assert isinstance(form, Form)
        label = str(form.query(".form-label").first().render())
        assert "one path per line" in label, label
        form.query_one("#f-paths").text = f"{spaced}\nnotes.md"
        await pilot.click(_btn(app, form, "submit"))
        await _settle(pilot, 1.0)
        assert not app.swallowed
    assert seen == [(ids["upload_project"], [str(spaced), "notes.md"])], seen


async def test_add_files_refuses_a_git_project(
    ids: dict[str, str], env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rework.tui.data import TuiData
    from rework.tui.widgets.modals import Form

    async def fake(self: Any, project_id: str, paths: list[str]) -> list[str]:  # pragma: no cover
        raise AssertionError("a git project must never reach the files area")

    monkeypatch.setattr(TuiData, "add_project_files", fake)
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        app.view("dashboard").project = ids["project"]  # polystats: git transport
        # the File menu is the other way in; it lands on the same handler
        title = next(t for t in app.query(".menu-title") if "File" in str(t.render()))
        await pilot.click(title)
        await pilot.pause()
        entry = next(
            e for e in app.screen.query(".menu-item") if "Add project files" in str(e.render())
        )
        await pilot.click(entry)
        await _settle(pilot, 0.6)
        assert not isinstance(app.screen, Form), "no form for a git project"
        assert not app.swallowed
    assert any("push a branch instead" in text for _, text in app.notifications_sent)
