"""Toasts are logged: a toast that fades is gone, so tui.log keeps the record.

Only errors that went through `_swallow` used to reach tui.log (with a
traceback). Every toast the owner sees passes through `ReworkApp.notify`, so
that is where they are logged — one line each, at the toast's severity, with
both title and message, and a burst of identical ones collapsed the way
`_swallow` already collapses its toasts (finding 35: tui.log held 2,400+
identical warnings before offline transitions were logged instead of every
poll). `rework logs tui` reads these records."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import pytest

from rework import logs
from rework.tui.app import ERROR_STORM_WINDOW, ReworkApp

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


async def test_a_warning_toast_is_logged_with_its_title_and_message(
    env: Path, tui_log: Path
) -> None:
    """The toast line carries both title and message, at the toast's severity,
    with component "tui" — so `rework logs tui` shows what the owner saw."""
    app = ReworkApp(poll_seconds=0.3)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        app.notify("Mission parked at the gate", title="Gates", severity="warning")
        await pilot.pause()

    hits = [r for r in _records(tui_log) if r.get("level") == "warning"]
    assert len(hits) == 1, hits
    assert hits[0]["component"] == "tui"
    assert "Gates" in hits[0]["msg"] and "Mission parked at the gate" in hits[0]["msg"], hits[0]
    assert app.notifications_sent == [("toast", "Mission parked at the gate")], (
        "logging must not change what is displayed"
    )


async def test_an_information_toast_without_a_title_is_logged_as_the_message(
    env: Path, tui_log: Path
) -> None:
    """A missing title must not produce ": message" or crash."""
    app = ReworkApp(poll_seconds=0.3)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        app.notify("Copied 42 chars")
        await pilot.pause()

    hits = [r for r in _records(tui_log) if r.get("msg") == "Copied 42 chars"]
    assert len(hits) == 1, hits
    assert hits[0]["level"] == "info"


async def test_a_swallowed_error_is_logged_exactly_once(env: Path, tui_log: Path) -> None:
    """`_swallow` already logs the traceback at error level: its toast must not
    add a second error record for the same failure."""
    app = ReworkApp(poll_seconds=0.3)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        assert app.is_running, "the swallow path needs a running app"
        try:
            raise RuntimeError("kaboom in a view")  # a real traceback
        except RuntimeError as exc:
            app._handle_exception(exc)
        await pilot.pause()

    records = _records(tui_log)
    errors = [r for r in records if r.get("level") == "error"]
    assert len(errors) == 1, errors
    assert errors[0]["msg"] == "swallowed UI error", errors[0]
    assert "RuntimeError: kaboom in a view" in errors[0]["exc"], errors[0]
    assert not [r for r in records if "RuntimeError: kaboom in a view" in r.get("msg", "")], (
        f"the toast path logged the swallowed error a second time: {records}"
    )


async def test_a_burst_of_identical_toasts_is_logged_once(env: Path, tui_log: Path) -> None:
    """A storm of the same toast collapses: one record, not N (and the toast
    still shows every time — Textual's notify is untouched)."""
    app = ReworkApp(poll_seconds=0.3)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        for _ in range(5):
            app.notify("Hub is offline", title="Offline", severity="warning")
        await pilot.pause()

        def warnings() -> list[dict[str, Any]]:
            return [r for r in _records(tui_log) if r.get("level") == "warning"]

        assert [r["msg"] for r in warnings()] == ["Offline: Hub is offline"], warnings()
        assert len(app.notifications_sent) == 5, "every toast is still shown"

        # once the window has passed, the next identical toast reports the count
        app._last_toast_at = time.monotonic() - ERROR_STORM_WINDOW - 1
        app.notify("Hub is offline", title="Offline", severity="warning")
        await pilot.pause()
        assert [r["msg"] for r in warnings()] == [
            "Offline: Hub is offline",
            "Offline: Hub is offline (4 more identical, suppressed)",
        ], warnings()

        # a different message is its own toast, not collapsed into the last one
        app.notify("Hub is back", title="Offline", severity="warning")
        app.notify("Hub is offline", title="Offline", severity="warning")
        await pilot.pause()
        assert [r["msg"] for r in warnings()] == [
            "Offline: Hub is offline",
            "Offline: Hub is offline (4 more identical, suppressed)",
            "Offline: Hub is back",
            "Offline: Hub is offline",
        ], warnings()
