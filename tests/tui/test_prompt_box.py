"""The orchestrator prompt box on the Dashboard's mission detail page.

The brief is the mission objective, painted into `#m-header` together with the
status and the plan metadata (one interpolated string, one widget). It used to
be an unbounded Static, so a long prompt ate most of the page and pushed the
flow strip, the run list and the tabs off screen. It now sits in a fixed-height
`#m-prompt` box of `PROMPT_BOX_ROWS` rows: a long brief scrolls INSIDE the box
and a one-line hint says how much is below the fold.

These tests mount the real app against a tmp SQLite store (the `seed` fixture)
and assert on what the terminal actually shows, because "the box is bounded"
is a geometry claim: the painted strings alone would pass with the old
unbounded header.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Static

sys.path.insert(0, str(Path(__file__).parent))

from seed import seed

from rework.kernel.state.records import Mission, MissionStatus
from rework.tui.app import ReworkApp
from rework.tui.views.dashboard import PROMPT_BOX_ROWS, PROMPT_HINT_ROWS, PromptBox
from rework.tui.widgets.base import RowList

SIZE = (140, 42)
#: body rows the brief gets: the box, minus the hint row it always reserves
BODY_ROWS = PROMPT_BOX_ROWS - PROMPT_HINT_ROWS


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "tui.sqlite3"
    monkeypatch.setenv("REWORK_DB", str(db))
    monkeypatch.setenv("REWORK_LIBRARY_DIR", str(tmp_path / "lib"))
    monkeypatch.setenv("REWORK_RESULTS_DIR", str(tmp_path / "results"))
    monkeypatch.setenv("REWORK_REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("REWORK_TUI_SETTINGS", str(tmp_path / "tui.json"))
    return db


@pytest.fixture
async def ids(env: Path) -> dict[str, str]:
    return await seed(str(env), history_days=20)


async def _settle(pilot: Any, t: float = 0.4) -> None:
    await pilot.pause(t)


async def _wait_for(pilot: Any, condition: Callable[[], bool], *, tries: int = 100) -> None:
    """Poll `condition` then let the caller assert (same idea as test_tui.py):
    a fixed sleep before a geometry assertion races on a loaded machine."""
    for _ in range(tries):
        if condition():
            return
        await pilot.pause(0.05)


def _brief(lines: int) -> str:
    """A deterministic N-line objective, one recognisable string per line."""
    return "\n".join(f"brief line {i:02d}" for i in range(1, lines + 1))


async def _add_brief(db: Path, mid: str, lines: int) -> str:
    """One mission whose objective is `lines` lines long, through the real store."""
    from rework.kernel.state.sqlite import open_sqlite

    store = await open_sqlite(str(db))
    try:
        mission = await store.create_mission(
            Mission(
                id=mid,
                objective=_brief(lines),
                status=MissionStatus.DONE,
                created_at=time.time(),
            )
        )
    finally:
        await store.aclose()
    return mission.id


def _screen_text(app: ReworkApp) -> list[str]:
    """The rendered screen, one string per terminal row.

    `render_strips()` is the compositor's own output, so this is what the owner
    sees: a line that was clipped or scrolled out of the box is absent here even
    though it is still in the widget's markup."""
    return [strip.text.rstrip() for strip in app.screen._compositor.render_strips()]


def _shown_lines(app: ReworkApp) -> list[str]:
    return [line for line in _screen_text(app) if "brief line" in line]


async def test_a_two_line_brief_shows_no_hint_and_cannot_scroll(
    ids: dict[str, str], env: Path
) -> None:
    """(a) A short brief fills nothing: no hint row, no scrollbar, and the box
    still occupies its full band so the page below it does not move."""
    mid = await _add_brief(env, "m-shortbrief0", 2)
    app = ReworkApp(poll_seconds=3600.0)
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        app.open_mission(mid)
        await _settle(pilot)
        box = app.query_one("#m-prompt", PromptBox)
        header = box.query_one("#m-header", Static)
        hint = box.query_one("#m-prompt-more", Static)
        await _wait_for(pilot, lambda: "brief line 01" in str(header.render()))
        text = str(header.render())
        assert "brief line 01" in text and "brief line 02" in text
        assert box.region.height == PROMPT_BOX_ROWS, (box.region, PROMPT_BOX_ROWS)
        assert header.virtual_size.height <= BODY_ROWS, header.virtual_size
        assert not box.has_class("has-more"), "no hint when the brief fits"
        assert str(hint.render()) == ""
        assert str(hint.styles.visibility) == "hidden"
        assert box.max_scroll_y == 0, "a two-line brief must not scroll"
        assert not box.show_vertical_scrollbar
        # both lines really are on screen
        assert any("brief line 01" in line for line in _shown_lines(app))
        assert any("brief line 02" in line for line in _shown_lines(app))


async def test_a_forty_line_brief_is_bounded_with_a_hint(ids: dict[str, str], env: Path) -> None:
    """(b) A long brief keeps the box at PROMPT_BOX_ROWS, names what is hidden,
    and leaves the flow strip, the run list and the tabs where they were."""
    mid = await _add_brief(env, "m-longbrief01", 40)
    app = ReworkApp(poll_seconds=3600.0)
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        app.open_mission(mid)
        await _settle(pilot)
        box = app.query_one("#m-prompt", PromptBox)
        header = box.query_one("#m-header", Static)
        hint = box.query_one("#m-prompt-more", Static)
        await _wait_for(pilot, lambda: box.has_class("has-more"))
        assert box.region.height == PROMPT_BOX_ROWS, (box.region, PROMPT_BOX_ROWS)
        # the brief is far taller than the box: that is what the hint is for
        height = header.virtual_size.height
        assert height > BODY_ROWS, (height, BODY_ROWS)
        assert box.max_scroll_y == height - BODY_ROWS
        hidden = height - BODY_ROWS
        assert str(hint.render()) == f"{hidden} more lines · scroll", str(hint.render())
        assert str(hint.styles.visibility) == "visible"
        # the hint is the box's last row, inside the box
        assert hint.region.bottom == box.region.bottom
        # the last brief line is NOT on screen yet
        assert not any("brief line 40" in line for line in _shown_lines(app))
        # everything under the box still renders below it
        flow = app.query_one("#m-flow", Static)
        assert flow.region.y >= box.region.bottom
        runlist = app.query_one("#run-list", RowList)
        assert runlist.region.y >= flow.region.bottom
        assert runlist.region.height > 0
        assert app.query_one("#m-pages").region.height > 0


async def test_the_tab_body_height_is_the_same_for_a_short_and_a_long_brief(
    ids: dict[str, str], env: Path
) -> None:
    """The page does not move: 2 lines and 40 lines leave the run list and the
    tab body at exactly the same place and size. This is the owner's complaint
    made into a number (the unbounded header shrank `.doc` by 38 rows)."""
    short = await _add_brief(env, "m-shortbrief1", 2)
    long = await _add_brief(env, "m-longbrief02", 40)

    async def measure(mid: str) -> tuple[Any, ...]:
        app = ReworkApp(poll_seconds=3600.0)
        async with app.run_test(size=SIZE) as pilot:
            await _settle(pilot)
            app.open_mission(mid)
            await _settle(pilot)
            runlist = app.query_one("#run-list", RowList)
            return (
                app.query_one("#m-prompt").region,
                app.query_one("#m-flow").region,
                runlist.region,
                app.query_one("#m-pages").region,
            )

    short_geom = await measure(short)
    long_geom = await measure(long)
    assert short_geom == long_geom, (short_geom, long_geom)


async def test_arrow_keys_scroll_the_focused_box_to_the_later_lines(
    ids: dict[str, str], env: Path
) -> None:
    """(c) Focused, the box takes the arrows and scrolls its own content: the
    last line of a 40-line brief becomes visible, and focus stays in the box."""
    mid = await _add_brief(env, "m-longbrief03", 40)
    app = ReworkApp(poll_seconds=3600.0)
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        app.open_mission(mid)
        await _settle(pilot)
        box = app.query_one("#m-prompt", PromptBox)
        await _wait_for(pilot, lambda: box.has_class("has-more"))
        assert not any("brief line 40" in line for line in _shown_lines(app))
        box.focus()
        await _settle(pilot)
        assert box.has_focus
        for _ in range(BODY_ROWS):
            await pilot.press("down")
        await _settle(pilot)
        assert box.scroll_y > 0
        assert box.has_focus, "the box keeps focus while it scrolls"
        # enough downs to reach the fold: the first line is gone, later ones show
        for _ in range(40):
            await pilot.press("down")
        await _settle(pilot)
        assert box.scroll_y == box.max_scroll_y
        shown = _shown_lines(app)
        assert any("brief line 40" in line for line in shown), shown
        assert not any("brief line 01" in line for line in shown), shown


async def test_dashboard_keys_still_work_with_the_box_focused(
    ids: dict[str, str], env: Path
) -> None:
    """(d) The box must not swallow the page's keys: "/" (focus search), esc
    (back), the alt+ tab switches and ctrl+l keep working while it has focus."""
    mid = await _add_brief(env, "m-longbrief04", 40)
    app = ReworkApp(poll_seconds=3600.0)
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        app.open_mission(mid)
        await _settle(pilot)
        dash = app.view("dashboard")
        box = app.query_one("#m-prompt", PromptBox)
        box.focus()
        await _settle(pilot)
        assert box.has_focus
        # "/" is the view's own binding: still reachable from a focused box
        await pilot.press("slash")
        await _settle(pilot)
        assert app.query_one("#mission-search").has_focus
        # ctrl+l goes back to the prompt bar from anywhere
        await pilot.press("ctrl+l")
        await _settle(pilot)
        assert app.query_one("#prompt").has_focus
        # alt+1..alt+5 are priority bindings: they switch tabs regardless
        await pilot.press("alt+1")
        await _settle(pilot)
        assert app.active_tab == "work"
        await pilot.press("alt+2")
        await _settle(pilot)
        assert app.active_tab == "dashboard"
        assert dash.mission_id == mid
        # esc still goes back to the mission list from the detail page
        await pilot.press("escape")
        await _settle(pilot)
        assert dash.mission_id is None
        assert app.is_running, "esc in the box must not arm quit"


async def test_arrows_move_the_run_list_when_the_box_is_not_focused(
    ids: dict[str, str], env: Path
) -> None:
    """(d, other half) Unfocused, the box takes nothing: the run list keeps its
    arrows and its selection does not move the brief."""
    mid = await _add_brief(env, "m-longbrief05", 40)
    app = ReworkApp(poll_seconds=3600.0)
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        app.open_mission(mid)
        await _settle(pilot)
        box = app.query_one("#m-prompt", PromptBox)
        runlist = app.query_one("#run-list", RowList)
        await _wait_for(pilot, lambda: box.has_class("has-more"))
        assert runlist.has_focus, "open_mission focuses the run list"
        assert runlist.selectable, runlist.selectable
        first = runlist.selected
        await pilot.press("down")
        await _settle(pilot)
        assert runlist.selected != first, "down still moves the run list"
        assert runlist.has_focus
        assert box.scroll_y == 0, "the unfocused box did not scroll"
