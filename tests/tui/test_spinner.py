"""The running-work spinner: the pure frame/stall rules, the dashboard row and
trajectory-tail markers, the Work card tick, and the one-timer guarantee."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from seed import seed

from rework.kernel.state.records import (
    EventRecord,
    EventType,
    Mission,
    MissionStatus,
)
from rework.tui import spinner
from rework.tui.app import ANIMATION_SECONDS, ReworkApp
from rework.tui.spinner import (
    FRAMES,
    SECONDS_PER_FRAME,
    STALL_SECONDS,
    frame_for,
    human_duration,
    is_stalled,
)
from rework.tui.widgets.base import Row, RowList, Stat
from rework.tui.widgets.cards import MissionCard
from rework.tui.widgets.trajectory import Trajectory

SIZE = (140, 42)


def _has_frame(text: str) -> bool:
    return any(f in text for f in FRAMES)


# -- pure rules ---------------------------------------------------------------


def test_frame_for_is_a_member_and_deterministic() -> None:
    for i in range(500):
        t = i * 0.037 - 3.0
        assert frame_for(t) in FRAMES
        assert frame_for(t) == frame_for(t)


def test_frame_advances_one_per_interval() -> None:
    base = 1000.0 + SECONDS_PER_FRAME / 2  # mid-frame, so float error cannot flip it
    for i in range(len(FRAMES) * 2):
        want = FRAMES[(int(base / SECONDS_PER_FRAME) + i) % len(FRAMES)]
        assert frame_for(base + i * SECONDS_PER_FRAME) == want
    seen = {frame_for(base + i * SECONDS_PER_FRAME) for i in range(len(FRAMES))}
    assert seen == set(FRAMES)


def test_every_frame_is_one_cell() -> None:
    assert len(FRAMES) == len(set(FRAMES)) > 1
    assert all(len(f) == 1 for f in FRAMES)


def test_is_stalled_boundaries() -> None:
    now = 10_000.0
    assert not is_stalled(now, now - (STALL_SECONDS - 1))
    assert is_stalled(now, now - STALL_SECONDS)
    assert is_stalled(now, now - (STALL_SECONDS + 1))
    assert not is_stalled(now, now)
    assert not is_stalled(now, None)  # no timestamp is not evidence of a stall


def test_human_duration_boundaries() -> None:
    assert human_duration(0) == "0s"
    assert human_duration(59.9) == "59s"
    assert human_duration(60) == "1m"
    assert human_duration(7 * 60 + 30) == "7m"
    assert human_duration(3599) == "59m"
    assert human_duration(3600) == "1h 00m"
    assert human_duration(2 * 3600 + 5 * 60) == "2h 05m"
    assert human_duration(86400) == "1d 0h"
    assert human_duration(-5) == "0s"


# -- the real app -------------------------------------------------------------


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


def _mission(mid: str, status: MissionStatus) -> Mission:
    return Mission(id=mid, objective=f"objective of {mid}", status=status, plan={"kind": "fix"})


async def test_one_animation_timer(env: Path) -> None:
    await seed(str(env), history_days=20)
    async with ReworkApp(poll_seconds=0.3).run_test(size=SIZE) as pilot:
        await pilot.pause(0.5)
        app = pilot.app
        ticks = [t for t in app._timers if t._interval == ANIMATION_SECONDS]
        assert len(ticks) == 1, "exactly one 10fps timer, not one per view"
        assert ticks[0]._callback.__func__ is ReworkApp._tick_frames  # type: ignore[attr-defined]


async def test_row_markers_by_status(env: Path) -> None:
    ids = await seed(str(env), history_days=20)
    async with ReworkApp(poll_seconds=0.3).run_test(size=SIZE) as pilot:
        await pilot.pause(0.5)
        dash = pilot.app.view("dashboard")
        now = time.time()
        frame = FRAMES[3]
        dash.snap.last_event = {"m-run": now - 2, "m-pend": now - 2, "m-hold": now - 2}

        running = dash._mission_row(_mission("m-run", MissionStatus.RUNNING), frame=frame, now=now)
        assert frame in running and "◌" not in running

        for mid, status in (("m-pend", MissionStatus.PENDING), ("m-hold", MissionStatus.ON_HOLD)):
            row = dash._mission_row(_mission(mid, status), frame=frame, now=now)
            assert not _has_frame(row), status
        gate = dash._mission_row(dash.snap.mission(ids["waiting"]), frame=frame, now=now)
        assert not _has_frame(gate), "an awaiting-gate row never animates"


async def test_stale_running_row_is_idle_not_animated(env: Path) -> None:
    await seed(str(env), history_days=20)
    async with ReworkApp(poll_seconds=0.3).run_test(size=SIZE) as pilot:
        await pilot.pause(0.5)
        dash = pilot.app.view("dashboard")
        now = time.time()
        dash.snap.last_event = {"m-old": now - STALL_SECONDS - 60}
        row = dash._mission_row(_mission("m-old", MissionStatus.RUNNING), frame=FRAMES[0], now=now)
        assert "◌" in row and not _has_frame(row)


async def test_work_tick_repaints_running_card(env: Path) -> None:
    await seed(str(env), history_days=20)
    async with ReworkApp(poll_seconds=0.3).run_test(size=SIZE) as pilot:
        await pilot.pause(0.5)
        work = pilot.app.view("work")
        mission = _mission("m-card", MissionStatus.RUNNING)
        card = MissionCard(mission.id)
        await work.mount(card)
        card.update_from(mission, [])
        await pilot.pause()
        now = time.time()
        work.snap.last_event = {mission.id: now - 1}

        def head() -> str:
            from textual.widgets import Static

            return str(card.query_one(".mc-head", Static).render())

        work.tick(FRAMES[1], now)
        first = head()
        work.tick(FRAMES[2], now)
        second = head()
        assert FRAMES[1] in first and FRAMES[2] in second and first != second

        work.snap.last_event = {mission.id: now - STALL_SECONDS - 5}
        work.tick(FRAMES[3], now)
        stale = head()
        assert "◌" in stale and "idle" in stale and not _has_frame(stale)


async def _append_fresh_event(db: Path, mission_id: str) -> None:
    from rework.kernel.state.sqlite import open_sqlite

    store = await open_sqlite(str(db))
    try:
        await store.append_event(
            EventRecord(
                mission_id=mission_id,
                run_id=f"{mission_id}:code:coder:1",
                type=EventType.PROGRESS,
                payload={"trajectory": "tool_call", "name": "grep", "arguments": "x"},
                ts=time.time(),
            )
        )
    finally:
        await store.aclose()


async def test_live_dashboard_row_and_tail_animate(env: Path) -> None:
    """Through the real app and its real 10fps timer: the running row's glyph
    changes on its own, and the trajectory tail shows "working"."""
    ids = await seed(str(env), history_days=20)
    await _append_fresh_event(env, ids["running"])
    async with ReworkApp(poll_seconds=0.3).run_test(size=SIZE) as pilot:
        app = pilot.app
        await pilot.pause(0.5)
        app.action_tab("dashboard")
        await pilot.pause(0.5)
        dash = app.view("dashboard")

        def row_text() -> str:
            rows = dash.query_one("#mission-list", RowList).query(Row)
            return next(r._markup for r in rows if r.key == ids["running"])

        seen = set()
        for _ in range(8):
            text = row_text()
            seen |= {f for f in FRAMES if f in text}
            await pilot.pause(0.17)
        assert len(seen) >= 2, f"the running glyph never changed: {seen}"

        tile = str(dash.query_one("#st-running", Stat).render())
        assert "[#" not in tile and "[/]" not in tile, f"raw markup leaked: {tile!r}"
        assert _has_frame(tile) and "working" in tile

        dash.open_mission(ids["running"])
        await pilot.pause(0.6)
        tail = dash.query_one("#m-traj", Trajectory)._spinner
        assert tail is not None and "working" in str(tail.render())
        assert _has_frame(str(tail.render()))


def test_marker_helper_is_consistent() -> None:
    now = 5000.0
    glyph, animated = spinner.marker(now, now - 1, t=0.0)
    assert animated and glyph in FRAMES
    glyph, animated = spinner.marker(now, now - STALL_SECONDS - 1)
    assert not animated and glyph == spinner.IDLE_GLYPH
