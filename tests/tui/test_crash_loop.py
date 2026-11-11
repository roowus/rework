"""The crash-loop fixes: markup in toasts, error storms, fatal logging, paste.

Found live on the owner's Mac (tui.log): `_swallow` toasted an exception whose
own text contained `[/]`; Textual parsed the toast as markup, raised a SECOND
MarkupError, swallowed and toasted that, and the storm took the TUI down. And
a bracketed paste into the prompt inserted the text twice (Textual dispatches
`_on_paste` down the whole MRO, and `Prompt._on_paste` called super() as well).

These tests drive the real app through the same code path the storm took."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import pytest
from textual import events
from textual.markup import MarkupError

import rework.tui.app as app_module
from rework import logs
from rework.tui.app import ERROR_STORM_WINDOW, ReworkApp
from rework.tui.widgets.prompt import Prompt

SIZE = (140, 42)

# the exact message that started the storm: its own text is a markup fragment
STORMY = "auto closing tag ([/]) has nothing to close"


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
        '[{"id":"flash","provider":"fake","model":"f","tier":1}]',
    )
    return db


@pytest.fixture
def tui_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The TUI's own JSON log file, in a tmp dir (as tests/unit/test_logs.py)."""
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


def _app() -> ReworkApp:
    return ReworkApp(poll_seconds=0.3)


def _records(path: Path) -> list[dict[str, Any]]:
    import json

    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _errors(app: ReworkApp) -> list[str]:
    """Every toast that is an error report (the ones _swallow produces)."""
    return [text for _, text in app.notifications_sent if text.startswith("MarkupError")]


async def test_bracketed_error_message_toasts_without_a_second_error(env: Path) -> None:
    """`[/]` in an exception message must not be parsed as markup: one toast,
    no error raised by rendering it, and no error chained off the first."""
    app = _app()
    async with app.run_test(size=SIZE, notifications=True) as pilot:
        await pilot.pause()
        app._swallow(MarkupError(STORMY))
        await pilot.pause()
        await pilot.pause()
        assert app.is_running, "a toast must never take the app down"
        assert [type(e) for e in app.swallowed] == [MarkupError], (
            "only the error we raised may be swallowed — no MarkupError from the toast"
        )
        assert _errors(app) == [f"MarkupError: {STORMY}"], "exactly one plain-text toast"
        assert STORMY in app.notifications_sent[0][1], "the text is shown, not escaped away"


async def test_identical_error_burst_yields_one_toast(env: Path) -> None:
    """A storm of the same error collapses into a single toast."""
    app = _app()
    async with app.run_test(size=SIZE, notifications=True) as pilot:
        await pilot.pause()
        for _ in range(25):
            app._swallow(ValueError("same failure"))
        await pilot.pause()
        toasts = [t for _, t in app.notifications_sent if "ValueError" in t]
        assert len(app.swallowed) == 25, "every error is still recorded and logged"
        assert toasts == ["ValueError: same failure"], "one toast for the whole burst"

        # once the window has passed, the next identical error reports the count
        app._last_error_at = time.monotonic() - ERROR_STORM_WINDOW - 1
        app._swallow(ValueError("same failure"))
        await pilot.pause()
        toasts = [t for _, t in app.notifications_sent if "ValueError" in t]
        assert len(toasts) == 2
        assert "24 more identical, suppressed" in toasts[-1], toasts


async def test_fatal_exception_is_logged_with_a_traceback_before_exit(
    env: Path, tui_log: Path
) -> None:
    """The fatal path writes the traceback to tui.log before the app exits:
    the owner cannot copy a traceback off a full-screen terminal."""
    app = _app()
    with pytest.raises(RuntimeError, match="kaboom"):
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            app._exit_renderables_ok = lambda: False  # a fatal, not a swallowable, error
            try:
                raise RuntimeError("kaboom")  # a real traceback, as Textual hands it over
            except RuntimeError as exc:
                app._handle_exception(exc)
            await pilot.pause()
    records = _records(tui_log)
    fatal = [r for r in records if "fatal UI error" in r.get("msg", "")]
    assert fatal, records
    assert fatal[-1]["level"] == "error"
    assert "RuntimeError: kaboom" in fatal[-1]["exc"], fatal[-1]
    assert "Traceback (most recent call last)" in fatal[-1]["exc"]


def test_run_tui_logs_how_the_app_ended(tui_log: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """run_tui is the entrypoint: it logs start, a non-zero return code, and a
    crash (with traceback) before letting the exception out."""

    class BoomApp:
        return_code: int | None = 0

        def run(self) -> None:
            raise RuntimeError("tui blew up")

    monkeypatch.setattr(app_module, "ReworkApp", BoomApp)
    with pytest.raises(RuntimeError, match="tui blew up"):
        app_module.run_tui()
    msgs = [r.get("msg", "") for r in _records(tui_log)]
    assert "tui start" in msgs
    crash = next(r for r in _records(tui_log) if "tui crashed" in r.get("msg", ""))
    assert "RuntimeError: tui blew up" in crash["exc"]


async def test_paste_into_the_prompt_inserts_the_text_once(env: Path) -> None:
    """Bracketed paste was inserted twice: Textual dispatches `_on_paste` down
    the MRO, so Prompt's super() call ran TextArea's handler a second time."""
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        prompt = app.query_one("#prompt", Prompt)
        prompt.focus()
        prompt.post_message(events.Paste("hello from the clipboard"))
        await pilot.pause()
        assert prompt.text == "hello from the clipboard", "paste lands exactly once"
        prompt.post_message(events.Paste(" again"))
        await pilot.pause()
        assert prompt.text == "hello from the clipboard again"


async def test_paste_of_an_image_path_attaches_and_does_not_insert(
    env: Path, tmp_path: Path
) -> None:
    """The image branch still consumes the paste (no path text in the prompt)."""
    image = tmp_path / "shot.png"
    image.write_bytes(b"\x89PNG\r\n")
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        prompt = app.query_one("#prompt", Prompt)
        prompt.post_message(events.Paste(str(image)))
        await pilot.pause()
        assert prompt.attachments == [str(image.resolve())]
        assert prompt.text == ""
