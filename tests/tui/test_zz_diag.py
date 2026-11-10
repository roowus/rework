"""TEMP diagnostic - delete me."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from rework.tui.app import ReworkApp
from rework.tui.widgets.modals import Form

SIZE = (140, 42)


def _app() -> ReworkApp:
    return ReworkApp(poll_seconds=0.3)


async def test_diag(ids: dict[str, str], env: Path) -> None:
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.5)
        title = next(t for t in app.query(".menu-title") if "Mission" in str(t.render()))
        await pilot.click(title)
        await pilot.pause()
        entry = next(e for e in app.screen.query(".menu-item") if "Preview a PR" in str(e.render()))
        await pilot.click(entry)
        await pilot.pause(0.5)
        form = app.screen
        print("SCREEN", type(form))
        assert isinstance(form, Form)
        form.query_one("#f-pr").value = "abc"
        form.query_one("#f-repo").value = "/tmp"
        from rework.tui.widgets.base import Btn
        btn = [b for b in form.query(Btn) if b.action == "submit" and b.display][0]
        print("CLICK RESULT", await pilot.click(btn))
        await pilot.pause(1.0)
        print("NOTIFICATIONS_SENT", app.notifications_sent)
        print("TEXTUAL NOTIFICATIONS", [n.message for n in app._notifications])
        print("SWALLOWED", app.swallowed)
        print("SCREEN AFTER", type(app.screen))
