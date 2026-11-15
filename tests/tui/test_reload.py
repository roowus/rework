"""Self-reload (findings 15 + 28): the shell notices new code, waits for the
owner to be idle, saves what they were looking at, exits, and `run_tui` re-execs
the process so the NEW code loads — same argv (`--target` still applies).

Nothing here may replace the test process: the actual `os.execv` sits behind
`rework.tui.reload._exec`, which every test patches, and every test points the
state file at tmp_path."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from seed import seed

from rework import runtime
from rework.runtime import CODE_CHANGED, CODE_CHANGED_IDLE
from rework.tui import reload as reload_mod
from rework.tui.app import ReworkApp
from rework.tui.reload import ReloadState, is_idle
from rework.tui.widgets.base import RowList
from rework.tui.widgets.prompt import Prompt

SIZE = (140, 42)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "tui.sqlite3"
    monkeypatch.setenv("REWORK_DB", str(db))
    monkeypatch.setenv("REWORK_LIBRARY_DIR", str(tmp_path / "lib"))
    monkeypatch.setenv("REWORK_RESULTS_DIR", str(tmp_path / "results"))
    monkeypatch.setenv("REWORK_REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("REWORK_TUI_SETTINGS", str(tmp_path / "state" / "tui.json"))
    monkeypatch.setenv("REWORK_TUI_RELOAD_STATE", str(tmp_path / "state" / "reload.json"))
    monkeypatch.setenv("REWORK_NOTIFY", "toast")
    monkeypatch.setenv("REWORK_MODELS", '[{"id":"flash","provider":"fake","model":"f","tier":1}]')
    return db


@pytest.fixture
async def ids(env: Path) -> dict[str, str]:
    return await seed(str(env), history_days=5)


@pytest.fixture
def execs(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, list[str]]]:
    """Every exec REQUEST is recorded; the test process is never replaced."""
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(reload_mod, "_exec", lambda path, argv: calls.append((path, argv)))
    return calls


def _app() -> ReworkApp:
    return ReworkApp(poll_seconds=0.3)


def _note(app: ReworkApp) -> str:
    from textual.widgets import Static

    return str(app.query_one("#tabbar-note", Static).render())


async def _settle(pilot: Any, t: float = 0.5) -> None:
    await pilot.pause(t)


# -- the state file ----------------------------------------------------------


def test_state_round_trips_through_the_file(env: Path) -> None:
    """Every field worth restoring survives write → read, including the draft,
    the tab, the open chat/mission/gate and the pending toast."""
    state = ReloadState(
        tab="gates",
        chat_id="c-1",
        mission_id="m-1",
        gate_id="g-1",
        draft="half a sentence",
        scroll={"#chat-log": 42.5},
        pending_toast="updated to abc1234",
        commit="abc1234",
        at=1234.5,
    )
    assert reload_mod.save_state(state)
    raw = json.loads(reload_mod.state_path().read_text())
    assert raw["draft"] == "half a sentence", raw
    assert reload_mod.state_path().parent == Path(env).parent / "state"

    back = reload_mod.load_state()
    assert back is not None
    assert back.as_dict() == state.as_dict()


def test_a_corrupt_or_partial_file_is_ignored_not_fatal(env: Path) -> None:
    """A truncated write, a hand-edit, a wrong type: the next start must lose
    the restore, never crash on it."""
    path = reload_mod.state_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    path.write_text("{not json")
    assert reload_mod.load_state() is None

    path.write_text(json.dumps(["not", "a", "dict"]))
    assert reload_mod.load_state() == ReloadState()

    path.write_text(
        json.dumps(
            {
                "tab": 7,  # wrong type: default
                "draft": "keep me",
                "scroll": {"#chat-log": "nope", "#m-traj": 12.0},
                "unknown_key": "ignored",
                "at": "yesterday",
            }
        )
    )
    back = reload_mod.load_state()
    assert back is not None
    assert back.tab == "work", "a wrong type falls back to the default"
    assert back.draft == "keep me"
    assert back.scroll == {"#m-traj": 12.0}, "a non-numeric offset is dropped"
    assert back.at == 0.0


def test_clear_state_applies_once(env: Path) -> None:
    reload_mod.save_state(ReloadState(tab="cost"))
    reload_mod.clear_state()
    assert reload_mod.load_state() is None
    reload_mod.clear_state()  # a second clear is not an error


# -- the idle predicate ------------------------------------------------------


def test_idle_after_the_threshold_with_nothing_open() -> None:
    quiet = {"modal_open": False, "chat_busy": False}
    assert is_idle(now=100.0, last_input=100.0 - reload_mod.IDLE_SECONDS - 1, **quiet)
    assert not is_idle(now=100.0, last_input=100.0 - reload_mod.IDLE_SECONDS + 1, **quiet)


def test_a_keypress_just_now_is_not_idle() -> None:
    assert not is_idle(now=100.0, last_input=99.9, modal_open=False, chat_busy=False)


def test_a_modal_blocks_the_reload() -> None:
    stale = 100.0 - reload_mod.IDLE_SECONDS - 5
    assert not is_idle(now=100.0, last_input=stale, modal_open=True, chat_busy=False)


def test_a_running_turn_blocks_the_reload() -> None:
    stale = 100.0 - reload_mod.IDLE_SECONDS - 5
    assert not is_idle(now=100.0, last_input=stale, modal_open=False, chat_busy=True)


def test_an_update_in_flight_blocks_the_reload() -> None:
    """`uv sync` mid-flight: re-execing would kill a half-synced venv."""
    stale = 100.0 - reload_mod.IDLE_SECONDS - 5
    assert not is_idle(
        now=100.0, last_input=stale, modal_open=False, chat_busy=False, updating=True
    )


def test_the_threshold_is_injectable(monkeypatch: pytest.MonkeyPatch) -> None:
    assert not is_idle(now=100.0, last_input=95.0, modal_open=False, chat_busy=False)
    monkeypatch.setattr(reload_mod, "IDLE_SECONDS", 1.0)
    assert is_idle(now=100.0, last_input=95.0, modal_open=False, chat_busy=False)


# -- what to exec ------------------------------------------------------------


def test_reexec_replays_the_original_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "orig_argv", [sys.executable, "-m", "rework", "--target", "sandbox"])
    assert reload_mod.reexec_argv() == [
        sys.executable,
        "-m",
        "rework",
        "--target",
        "sandbox",
    ]


def test_reexec_falls_back_to_module_form(monkeypatch: pytest.MonkeyPatch) -> None:
    """A `python -c 'from rework.cli import main; main()'` stub (the pyte test,
    a shell wrapper) is not a rework invocation: fall back to `-m rework`."""
    monkeypatch.setattr(sys, "orig_argv", [sys.executable, "-c", "print('hi')"])
    monkeypatch.setattr(sys, "argv", ["rework", "--target", "sandbox"])
    argv = reload_mod.reexec_argv()
    assert argv == [sys.executable, "-m", "rework", "--target", "sandbox"]
    assert (Path(__file__).parent.parent.parent / "src" / "rework" / "__main__.py").is_file(), (
        "the fallback only works because `python -m rework` exists"
    )


def test_reexec_decides_the_argv_of_an_installed_console_script(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    script = tmp_path / "rework"
    script.write_text("#!/usr/bin/env python\n")
    monkeypatch.setattr(sys, "orig_argv", [sys.executable, str(script), "--target", "sandbox"])
    assert reload_mod.reexec_argv() == [sys.executable, str(script), "--target", "sandbox"]


def test_reexec_flushes_and_execs(
    env: Path, execs: list[tuple[str, list[str]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    reload_mod.save_state(ReloadState(pending_toast="updated to abc1234"))
    assert reload_mod.reexec()
    assert len(execs) == 1, execs
    assert execs[0][0] == reload_mod.reexec_argv()[0]


def test_toast_text_degrades_without_a_commit() -> None:
    assert reload_mod.toast_for("abc1234") == "updated to abc1234"
    assert reload_mod.toast_for("", reason="code changed") == "reloaded (code changed)"
    assert reload_mod.toast_for("") == "reloaded"


# -- arming the reload -------------------------------------------------------


async def test_a_code_change_arms_a_reload_and_says_will_reload(
    env: Path, monkeypatch: pytest.MonkeyPatch, execs: list[tuple[str, list[str]]]
) -> None:
    """Finding 15: the note no longer asks for a restart (the shell does it),
    it arms the reload — but NOT while the owner is right there typing."""
    values = ["a"]
    monkeypatch.setattr(runtime, "source_fingerprint", lambda: values[0])
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        values[0] = "b"
        app._check_code()
        await pilot.pause()
        assert app.reload_pending
        assert CODE_CHANGED_IDLE in _note(app)
        assert CODE_CHANGED not in _note(app), "no 'restart to load' any more"
        assert not app.reloaded, "a keypress just happened: never reload mid-sentence"
        app._check_reload()
        assert not app.reloaded
        assert execs == []
    assert not reload_mod.state_path().exists()


async def test_the_reload_waits_for_the_owner_to_go_idle(
    env: Path, monkeypatch: pytest.MonkeyPatch, execs: list[tuple[str, list[str]]]
) -> None:
    app = _app()
    monkeypatch.setattr(reload_mod, "IDLE_SECONDS", 0.3)
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        app.request_reload(reason="test")
        app._last_input = time.monotonic()  # the owner just pressed a key
        app._check_reload()
        assert not app.reloaded, "a keypress is recent"
        await _settle(pilot, 0.5)
        assert app.idle_for_reload(), "nothing open, nothing in flight, quiet for > threshold"
        app._check_reload()
        assert app.reloaded
    assert execs == [], "the scheduled interval, not the test, is what exits"


async def test_a_running_chat_turn_blocks_the_reload_until_it_finishes(
    env: Path, monkeypatch: pytest.MonkeyPatch, execs: list[tuple[str, list[str]]]
) -> None:
    """The feature's core safety property: a turn in flight is never killed."""
    app = _app()
    monkeypatch.setattr(reload_mod, "IDLE_SECONDS", 0.1)
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        work = app.view("work")
        work._sending = True
        app.request_reload(reason="test")
        await _settle(pilot, 0.3)
        app._check_reload()
        assert not app.idle_for_reload()
        assert not app.reloaded, "a turn in flight must survive the reload"
        assert execs == []

        work._sending = False
        work._loading_chat = "c-loading"
        assert app.chat_busy(), "_loading_chat is a turn-wide busy state too"
        work._loading_chat = None
        await _settle(pilot, 0.3)
        assert app.idle_for_reload(), "the turn finished and the app is quiet again"
        app._check_reload()
        assert app.reloaded, "and NOW it reloads"
        assert reload_mod.state_path().exists(), "the UI state was saved before exiting"
    assert execs == []


async def test_a_modal_screen_blocks_the_reload(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The owner is picking a model / reading the help: not idle."""
    app = _app()
    monkeypatch.setattr(reload_mod, "IDLE_SECONDS", 0.0)
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        app.action_help()
        await _settle(pilot)
        app.request_reload(reason="test")
        app._check_reload()
        assert not app.reloaded
        await pilot.press("escape")
        await _settle(pilot)
        app._check_reload()
        assert app.reloaded


# -- the Help menu item ------------------------------------------------------


async def test_the_reload_menu_item_reloads_now(
    env: Path, ids: dict[str, str], execs: list[tuple[str, list[str]]]
) -> None:
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        app.action_reload_rework()
        await pilot.pause()
        assert app.reloaded
    assert execs == [], "the app never execs; run_tui does after run() returns"
    assert not [t for t in app.notifications_sent if "can't reload" in t[1]]


async def test_the_reload_menu_item_refuses_during_a_turn(
    env: Path, ids: dict[str, str], execs: list[tuple[str, list[str]]]
) -> None:
    """On demand must still not kill a running turn: refuse with a toast."""
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        app.view("work")._sending = True
        app.action_reload_rework()
        await pilot.pause()
        assert not app.reloaded
        assert [t for t in app.notifications_sent if "can't reload while a turn" in t[1]]
    assert execs == []


# -- save on exit, restore on start ------------------------------------------


async def test_the_ui_state_is_saved_and_restored_on_the_next_start(
    env: Path, ids: dict[str, str], execs: list[tuple[str, list[str]]]
) -> None:
    """The whole loop, in one test: open a chat, type a draft, reload, and check
    the NEW app puts the tab, the chat and the draft back — then forgets the
    file so it can never apply twice."""
    monkeypatch_commit = "abc1234"
    import rework.tui.reload as rl

    first = _app()
    async with first.run_test(size=SIZE) as pilot:
        await _settle(pilot, 0.8)
        first.action_tab("work")
        first.query_one("#chat-list", RowList).activate(ids["chat"])
        await _settle(pilot, 1.0)
        first.query_one("#prompt", Prompt).text = "an unsent half-sentence"
        await pilot.pause()
        saved = first._save_ui_state(toast=f"updated to {monkeypatch_commit}")
        assert saved.chat_id == ids["chat"], saved.as_dict()
        assert saved.draft == "an unsent half-sentence"
        assert saved.tab == "work"
        assert saved.pending_toast == "updated to abc1234"
    assert rl.state_path().exists()

    second = _app()
    async with second.run_test(size=SIZE) as pilot:
        await _settle(pilot, 1.0)
        assert second.query_one("#prompt", Prompt).text == "an unsent half-sentence"
        assert second.view("work").convo is not None
        assert second.view("work").convo.id == ids["chat"]
        assert [t for t in second.notifications_sent if "updated to abc1234" in t[1]]
    assert not rl.state_path().exists(), "the state file applies exactly once"


async def test_a_stale_state_file_is_ignored_field_by_field(env: Path, ids: dict[str, str]) -> None:
    """Ids that are gone (a deleted chat / mission / gate) must be skipped and
    the rest of the restore must still land: no crash, no toast about it."""
    from rework.tui import reload as rl

    rl.save_state(
        ReloadState(
            tab="cost",
            chat_id="chat-that-is-gone",
            mission_id="mission-that-is-gone",
            gate_id="gate-that-is-gone",
            draft="still mine",
            pending_toast="",
            at=1.0,
        )
    )
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot, 0.8)
        assert app.active_tab == "cost", "a known tab still restores"
        assert app.view("work").convo is None, "an unknown chat id is skipped"
        assert app.view("dashboard").mission_id is None
        assert app.view("gates").selected is None
        assert app.query_one("#prompt", Prompt).text == "still mine"
        assert app.swallowed == []
    assert not rl.state_path().exists()


async def test_a_corrupt_state_file_does_not_break_the_start(
    env: Path, ids: dict[str, str]
) -> None:
    from rework.tui import reload as rl

    rl.state_path().parent.mkdir(parents=True, exist_ok=True)
    rl.state_path().write_text("{oops")
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot, 0.6)
        assert app.active_tab == "work"
    assert app.swallowed == []


# -- run_tui re-execs --------------------------------------------------------


def test_run_tui_reexecs_after_a_reload(
    env: Path, execs: list[tuple[str, list[str]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`run_tui` inspects the app after `run()` and re-execs ONLY when the app
    exited for a reload."""
    import rework.tui.app as app_mod

    class ReloadingApp:
        return_code: int | None = 0
        reloaded = True

        def run(self) -> None:
            reload_mod.save_state(ReloadState(pending_toast="updated to abc1234"))

    monkeypatch.setattr(app_mod, "ReworkApp", ReloadingApp)
    app_mod.run_tui()
    assert len(execs) == 1, execs
    assert reload_mod.load_state() is not None, "the toast survives for the new process"


def test_run_tui_does_not_reexec_a_plain_exit(
    env: Path, execs: list[tuple[str, list[str]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    import rework.tui.app as app_mod

    class PlainApp:
        return_code: int | None = 0

        def run(self) -> None:
            return

    monkeypatch.setattr(app_mod, "ReworkApp", PlainApp)
    app_mod.run_tui()
    assert execs == []
