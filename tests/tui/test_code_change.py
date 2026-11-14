"""The shell notices that the code on disk changed (finding 28: the owner's TUI
ran code older than its checkout, so a crash traceback pointed at the wrong
lines). It must SAY SO in the status bar and in tui.log, exactly once, and it
must not drop what the owner is doing: it ARMS a reload that only happens once
they are idle (finding 15; the exec itself lives in tests/tui/test_reload.py)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Static

from rework import logs, runtime
from rework.runtime import CODE_CHANGED_IDLE
from rework.tui.app import ReworkApp

SIZE = (140, 42)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "tui.sqlite3"
    monkeypatch.setenv("REWORK_DB", str(db))
    monkeypatch.setenv("REWORK_LIBRARY_DIR", str(tmp_path / "lib"))
    monkeypatch.setenv("REWORK_RESULTS_DIR", str(tmp_path / "results"))
    monkeypatch.setenv("REWORK_REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("REWORK_TUI_SETTINGS", str(tmp_path / "tui.json"))
    monkeypatch.setenv("REWORK_NOTIFY", "none")
    monkeypatch.setenv("REWORK_MODELS", '[{"id":"flash","provider":"fake","model":"f","tier":1}]')
    return db


@pytest.fixture
def tui_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("REWORK_LOG_DIR", str(tmp_path / "logs"))
    root = logging.getLogger()
    saved = list(root.handlers)
    logs._configured.discard("tui")
    path = logs.setup_logging("tui")
    yield path
    for handler in list(root.handlers):
        if handler not in saved:
            root.removeHandler(handler)
            handler.close()
    logs._configured.discard("tui")


def _records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _note(app: ReworkApp) -> str:
    return str(app.query_one("#tabbar-note", Static).render())


async def test_a_code_change_shows_a_persistent_note_and_logs_once(
    env: Path, tui_log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    values = ["a"]
    monkeypatch.setattr(runtime, "source_fingerprint", lambda: values[0])
    app = ReworkApp(poll_seconds=0.3)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        assert _note(app) == "", "nothing to say before the code changes"
        assert app._code_watcher is not None, "the shell tracks its code on mount"

        values[0] = "b"
        app._check_code()
        await pilot.pause()
        assert CODE_CHANGED_IDLE in _note(app), _note(app)
        assert app.reload_pending, "new code on disk arms a reload"
        assert not app.reloaded, "but never while the owner is right here"

        # the note is PERSISTENT and the log line is written once, not per tick
        app._check_code()
        await pilot.pause()
        assert CODE_CHANGED_IDLE in _note(app), _note(app)
        assert not app.reloaded
        hits = [r for r in _records(tui_log) if r.get("msg") == CODE_CHANGED_IDLE]
        assert len(hits) == 1, hits
        assert hits[0]["level"] == "warning", hits[0]
        assert hits[0]["component"] == "tui", hits[0]


async def test_an_unchanged_checkout_says_nothing(
    env: Path, tui_log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime, "source_fingerprint", lambda: "same")
    app = ReworkApp(poll_seconds=0.3)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        app._check_code()
        await pilot.pause()
        assert _note(app) == ""
        assert not [r for r in _records(tui_log) if r.get("msg") == CODE_CHANGED_IDLE]
        assert not app.reload_pending


async def test_the_shell_checks_its_own_interval_not_the_heartbeat(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fingerprint walk stays off the 2s refresh path: it has its own,
    much slower interval."""
    from rework.tui.app import CODE_CHECK_SECONDS

    app = ReworkApp(poll_seconds=0.3)
    intervals: list[tuple[float, Any]] = []
    monkeypatch.setattr(app, "set_interval", lambda t, f: intervals.append((t, f)))
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
    assert CODE_CHECK_SECONDS > 10.0, "a stat walk must not run every couple of seconds"
    assert (CODE_CHECK_SECONDS, app._check_code) in intervals, intervals
