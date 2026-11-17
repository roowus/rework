"""What the Work tab lists, how switching chats repaints, and the Dashboard's
read-only Chat tab (the mission's `orch-<id>` transcript).

The rule: the Work list holds CONVERSATIONS. A conversation is a non-`orch-`
chat with at least one user or assistant message, ordered by its newest
MESSAGE. Missions are missions: a mission's own transcript is not a Work row,
it lives on the Dashboard's Chat tab.

Driven through the real app against a tmp sqlite store, with real clicks and
keys. Anything asserted at a lower level says so in its docstring."""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from seed import seed

from rework.kernel.state.records import (
    Chat,
    ChatMessage,
    Mission,
    MissionStatus,
    orchestrator_chat_id,
)
from rework.tui.app import ReworkApp
from rework.tui.widgets.base import ChipGroup, Row, RowList
from rework.tui.widgets.trajectory import Trajectory

SIZE = (140, 42)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "tui.sqlite3"
    monkeypatch.setenv("REWORK_DB", str(db))
    monkeypatch.setenv("REWORK_LIBRARY_DIR", str(tmp_path / "lib"))
    monkeypatch.setenv("REWORK_RESULTS_DIR", str(tmp_path / "results"))
    monkeypatch.setenv("REWORK_REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("REWORK_TUI_SETTINGS", str(tmp_path / "tui.json"))
    monkeypatch.setenv(
        "REWORK_MODELS",
        '[{"id":"flash","provider":"fake","model":"f","tier":1}]',
    )
    return db


def _app() -> ReworkApp:
    return ReworkApp(poll_seconds=0.3)


async def _store(db: Path) -> Any:
    from rework.kernel.state.sqlite import open_sqlite

    return await open_sqlite(str(db))


async def _chat(
    db: Path,
    title: str,
    messages: list[tuple[str, str, float]],
    *,
    chat_id: str | None = None,
    mission_id: str | None = None,
) -> str:
    """A chat with `(role, text, ts)` messages, written through the store API.
    role "note" is what agents append (a bare note row, no conversation)."""
    store = await _store(db)
    try:
        chat = Chat(title=title, mission_id=mission_id)
        if chat_id:
            chat.id = chat_id
        await store.create_chat(chat)
        for role, text, ts in messages:
            body = {} if role == "note" else {"role": role, "content": text}
            await store.append_chat_message(
                ChatMessage(chat_id=chat.id, role=role, body=body, display=text, ts=ts)
            )
    finally:
        await store.aclose()
    return chat.id


def _set_updated_at(db: Path, chat_id: str, updated_at: float) -> None:
    """Test construction only: pin chats.updated_at to build a divergence from
    the message timestamps (the store API always stamps it with 'now')."""
    con = sqlite3.connect(str(db))
    try:
        con.execute("UPDATE chats SET updated_at = ? WHERE id = ?", (updated_at, chat_id))
        con.commit()
    finally:
        con.close()


def _rows(app: ReworkApp) -> list[str]:
    return [r._markup for r in app.query_one("#chat-list", RowList).query(Row)]


def _row(app: ReworkApp, chat_id: str) -> Row:
    return next(r for r in app.query_one("#chat-list", RowList).query(Row) if r.key == chat_id)


def _log(app: ReworkApp) -> str:
    return " ".join(str(w.render()) for w in app.query("#chat-log .step"))


async def _until(pilot: Any, condition: Any, *, tries: int = 100) -> None:
    for _ in range(tries):
        if condition():
            return
        await pilot.pause(0.1)


async def _boot(pilot: Any) -> None:
    await pilot.pause(0.6)


NOW = time.time()


# -- the list rule -------------------------------------------------------------


async def test_a_missions_shell_chat_and_orchestrator_transcripts_never_appear(env: Path) -> None:
    """Missions are missions: neither the note-only shell an agent left behind
    for a mission nor the mission's `orch-<id>` transcript (however many
    messages it holds) is a row in the Work list; only the real conversation is."""
    store = await _store(env)
    try:
        await store.create_mission(
            Mission(id="m-shell000001", objective="o", status=MissionStatus.RUNNING)
        )
    finally:
        await store.aclose()
    await _chat(
        env,
        "SHELL for mission",
        [("note", "mission created", NOW - 50)],
        mission_id="m-shell000001",
    )
    await _chat(
        env,
        "orchestrator m-shell000001",
        [("assistant", "orchestrator machinery", NOW - 40), ("user", "owner nudge", NOW - 30)],
        chat_id=orchestrator_chat_id("m-shell000001"),
        mission_id="m-shell000001",
    )
    await _chat(env, "a real conversation", [("user", "hello there", NOW - 20)])
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _boot(pilot)
        rows = _rows(app)
        assert len(rows) == 1 and "a real conversation" in rows[0], rows
        assert not any("SHELL" in r or "orchestrator" in r for r in rows), rows
        assert not app.swallowed


async def test_a_user_chat_with_one_message_is_listed(env: Path) -> None:
    """The smallest real conversation, one user message, is a row."""
    await _chat(env, "just one message", [("user", "ping", NOW - 10)])
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _boot(pilot)
        assert any("just one message" in r for r in _rows(app)), _rows(app)


async def test_an_empty_chat_and_a_note_only_chat_are_not_listed(env: Path) -> None:
    """A chat with no messages opens blank, and one holding only notes is not a
    conversation either: neither is a row. A neighbour with a message proves the
    list itself rendered."""
    await _chat(env, "EMPTY chat", [])
    await _chat(env, "NOTES only", [("note", "launched m-x", NOW - 5)])
    await _chat(env, "has a message", [("assistant", "hi", NOW - 5)])
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _boot(pilot)
        rows = _rows(app)
        assert len(rows) == 1 and "has a message" in rows[0], rows


async def test_a_newer_message_sorts_above_an_older_one_whatever_updated_at_says(
    env: Path,
) -> None:
    """Order and age come from the newest MESSAGE. Here `updated_at` says the
    opposite (pinned directly, as a rename or attach would move it), and the
    row with the newest message must still be first and aged by that message.

    The clock is read here, not from the module-level `now`: the age text is
    asserted, and in a long full-suite run the import-time value is minutes old
    by the time this test runs ("18m ago" instead of "1m ago")."""
    now = time.time()
    stale = await _chat(env, "stale by message", [("user", "old", now - 86400 * 20)])
    fresh = await _chat(env, "fresh by message", [("user", "new", now - 60)])
    _set_updated_at(env, stale, now)  # looks newest by updated_at
    _set_updated_at(env, fresh, now - 86400 * 30)  # looks oldest by updated_at
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _boot(pilot)
        rows = _rows(app)
        assert "fresh by message" in rows[0] and "stale by message" in rows[1], rows
        assert "1m ago" in rows[0] or "now" in rows[0], rows[0]
        assert "20d ago" in rows[1], rows[1]


# -- switching chats always repaints -------------------------------------------


async def test_clicking_two_chats_in_a_row_shows_each_ones_own_content(env: Path) -> None:
    """Real clicks on two rows back to back: after each click the log holds
    that chat's lines and none of the other's, and the head names it."""
    a = await _chat(env, "alpha chat", [("user", "ALPHA-question", NOW - 90)])
    b = await _chat(env, "beta chat", [("user", "BETA-question", NOW - 30)])
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _boot(pilot)
        await pilot.click(_row(app, a))
        await _until(pilot, lambda: "ALPHA-question" in _log(app))
        assert "ALPHA-question" in _log(app) and "BETA-question" not in _log(app)
        await pilot.click(_row(app, b))
        await _until(pilot, lambda: "BETA-question" in _log(app))
        assert "BETA-question" in _log(app) and "ALPHA-question" not in _log(app), _log(app)
        work = app.view("work")
        assert work.convo is not None and work.convo.id == b
        await pilot.click(_row(app, a))  # and back again
        await _until(pilot, lambda: "ALPHA-question" in _log(app))
        assert "BETA-question" not in _log(app)
        assert not app.swallowed


async def test_opening_a_chat_with_no_messages_paints_an_explicit_empty_state(
    env: Path,
) -> None:
    """The list never offers an empty chat, so this goes through `open_chat` (the
    seam a restored session or a deep link uses, i.e. below the click). It must
    show the explicit empty state and never the previous chat's transcript."""
    a = await _chat(env, "alpha chat", [("user", "ALPHA-question", NOW - 90)])
    empty = await _chat(env, "nothing yet", [])
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _boot(pilot)
        await pilot.click(_row(app, a))
        await _until(pilot, lambda: "ALPHA-question" in _log(app))
        app.view("work").open_chat(empty)
        await _until(pilot, lambda: "No messages in this chat yet." in _log(app))
        assert "No messages in this chat yet." in _log(app), _log(app)
        assert "ALPHA-question" not in _log(app)


async def test_a_late_load_for_chat_a_never_paints_over_the_open_chat_b(env: Path) -> None:
    """`_load_seq` guard, asserted at the `_read_chat` level (the worker group
    cancels most stale loads before they get here; this is the backstop for one
    that was not cancelled in time). A load carrying an out-of-date request
    number must paint nothing; a control with the current number DOES paint, so
    the assertion is not vacuous."""
    a = await _chat(env, "alpha chat", [("user", "ALPHA-question", NOW - 90)])
    b = await _chat(env, "beta chat", [("user", "BETA-question", NOW - 30)])
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _boot(pilot)
        work = app.view("work")
        await pilot.click(_row(app, b))
        await _until(pilot, lambda: "BETA-question" in _log(app))
        await work._read_chat(a, "missing", work._load_seq - 1)  # a stale request
        assert work.convo is not None and work.convo.id == b
        assert "BETA-question" in _log(app) and "ALPHA-question" not in _log(app), _log(app)
        await work._read_chat(a, "missing", work._load_seq)  # control: current paints
        assert work.convo.id == a and "ALPHA-question" in _log(app)


# -- a failing conversation probe ------------------------------------------------


def _break_probe(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Make the store-side conversation probe raise (not a connection error, so
    it reaches the Hub). Returns a call counter."""
    from rework.tui.data import TuiData

    calls: list[int] = []

    async def boom(self: Any, chat_ids: list[str]) -> set[str]:
        calls.append(1)
        raise RuntimeError("probe down")

    monkeypatch.setattr(TuiData, "chat_ids_with_conversation", boom)
    return calls


async def test_a_failing_probe_keeps_the_real_chats_listed(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rule is preserved (shells, note-only and empty chats stay out); only
    the failure mode changed. A probe that starts failing AFTER the list was
    read must not blank the panel: the owner would think the conversations were
    deleted. The last vouched list stays, with the shell and empty chat still
    absent. Asserted through the real app: poll, break the probe, poll again."""
    await _chat(env, "real one", [("user", "hello", NOW - 20)])
    await _chat(env, "real two", [("assistant", "hi", NOW - 10)])
    await _chat(env, "EMPTY chat", [])
    await _chat(env, "NOTES only", [("note", "launched m-x", NOW - 5)])
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _boot(pilot)
        assert len(_rows(app)) == 2, _rows(app)
        calls = _break_probe(monkeypatch)
        await _until(pilot, lambda: bool(calls))
        await pilot.pause(0.8)  # let the failing poll land and repaint
        assert calls, "the broken probe must have been hit"
        rows = _rows(app)
        assert len(rows) == 2, rows
        assert any("real one" in r for r in rows) and any("real two" in r for r in rows), rows
        assert not any("EMPTY" in r or "NOTES" in r for r in rows), rows
        assert app.hub.snapshot.chats_failed is False


async def test_a_failing_probe_with_nothing_cached_says_so_instead_of_a_blank(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same rule, nothing to keep (process just started, store down): the list
    stays EMPTY (never fails open to list every chat, so the shell and the empty
    chat are not shown) but the panel says "could not read chats (retrying)".
    The note is the list's placeholder Static, not a Row, so there is nothing to
    click: no row exists, and the real chats are not listed either."""
    await _chat(env, "real one", [("user", "hello", NOW - 20)])
    await _chat(env, "EMPTY chat", [])
    _break_probe(monkeypatch)
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _boot(pilot)
        assert _rows(app) == [], _rows(app)
        lst = app.query_one("#chat-list", RowList)
        notes = [w for w in lst.query(".rowlist-empty") if w.display]
        assert len(notes) == 1, notes
        assert "could not read chats (retrying)" in str(notes[0].render())
        assert app.hub.snapshot.chats_failed is True
        assert not list(lst.query(Row))


async def test_a_saved_open_chat_survives_a_transient_empty_list(env: Path) -> None:
    """`_apply_restore` drops a saved chat only when the list was READ and the
    chat is not in it. An empty list that came from a failed read
    (`chats_failed`) says nothing about the chat, so it is still opened.
    Asserted at the `_apply_restore` level with a hand-built snapshot (the
    restore runs once, on the first snapshot, so a click cannot reach it); the
    control, a successfully read empty list, does NOT open it."""
    from rework.tui.hub import Snapshot
    from rework.tui.reload import ReloadState

    chat_id = await _chat(env, "the saved one", [("user", "remember me", NOW - 20)])
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _boot(pilot)
        work = app.view("work")
        opened: list[str] = []
        work.open_chat = lambda cid: opened.append(cid)  # type: ignore[method-assign]
        app._restore = ReloadState(chat_id=chat_id)
        app._apply_restore(Snapshot(chats=[], loaded=True, chats_failed=False))
        assert opened == [], "a successfully read list without the chat drops it"
        app._restore = ReloadState(chat_id=chat_id)
        app._apply_restore(Snapshot(chats=[], loaded=True, chats_failed=True))
        assert opened == [chat_id], opened


# -- the Dashboard's Chat tab ---------------------------------------------------


async def _open_chat_tab(app: ReworkApp, pilot: Any, mission_id: str) -> Any:
    app.open_mission(mission_id)
    await pilot.pause(0.8)
    chip = next(c for c in app.query_one("#m-tabs", ChipGroup).query(".chip") if c.value == "chat")
    await pilot.click(chip)
    return app.query_one("#m-chat", Trajectory)


async def test_the_dashboard_chat_tab_shows_the_orchestrator_transcript_and_scrolls(
    tmp_path: Path, env: Path
) -> None:
    """The mission's `orch-<id>` transcript is on the Dashboard, read-only, and
    the keys move it: after opening the tab the body follows the bottom, then
    PageUp, Up and Home each move the content (its scroll offset AND where the
    last line sits on screen), and End brings it back."""
    ids = await seed(str(env), history_days=20)
    mission = ids["running"]
    await _chat(
        env,
        f"orchestrator {mission}",
        [
            ("assistant" if i % 2 else "user", f"transcript line {i:03d}", NOW - 1000 + i)
            for i in range(80)
        ],
        chat_id=orchestrator_chat_id(mission),
        mission_id=mission,
    )
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.5)
        log = await _open_chat_tab(app, pilot, mission)
        await _until(pilot, lambda: len(log.query(".step")) >= 80)
        await pilot.pause(0.4)
        assert len(log.query(".step")) == 80
        text = " ".join(str(w.render()) for w in log.query(".step"))
        assert "transcript line 000" in text and "transcript line 079" in text
        assert app.focused is log, app.focused  # keys go to the transcript
        assert log.max_scroll_y > 0
        bottom = log.scroll_y
        assert bottom >= log.max_scroll_y - 2  # follows the newest line
        last = list(log.query(".step"))[-1]
        y0 = last.region.y
        for key in ("pageup", "up", "home"):
            before = log.scroll_y
            await pilot.press(key)
            await pilot.pause(0.3)
            assert log.scroll_y < before, (key, before, log.scroll_y)
        assert log.scroll_y == 0
        assert last.region.y > y0  # the content itself moved on screen
        await pilot.press("end")
        await pilot.pause(0.3)
        assert log.scroll_y >= log.max_scroll_y - 2
        # read-only: no composer is part of this tab
        assert not log.query("Input, TextArea")
        assert not app.swallowed


async def test_the_dashboard_chat_tab_says_so_when_the_orchestrator_never_ran(
    env: Path,
) -> None:
    """No `orch-<id>` row exists until the orchestrator first runs: the tab shows
    an explicit state for that, not a blank body or another mission's lines."""
    ids = await seed(str(env), history_days=20)
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.5)
        log = await _open_chat_tab(app, pilot, ids["running"])
        await _until(pilot, lambda: "No orchestrator transcript yet" in _tab_text(log))
        assert "No orchestrator transcript yet" in _tab_text(log), _tab_text(log)
        assert not app.swallowed


def _tab_text(log: Trajectory) -> str:
    return " ".join(str(w.render()) for w in log.query(".step"))
