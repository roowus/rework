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
    Run,
    RunStatus,
)
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

        seen: set[str] = set()
        tile_seen: set[str] = set()
        for _ in range(14):
            text = row_text()
            tile = str(dash.query_one("#st-running", Stat).render())
            # every single sample, poll repaints included, carries a frame
            assert _has_frame(text), f"blank running row: {text!r}"
            assert _has_frame(tile) and "working" in tile, f"tile flipped: {tile!r}"
            assert "[#" not in tile and "[/]" not in tile, f"raw markup leaked: {tile!r}"
            seen |= {f for f in FRAMES if f in text}
            tile_seen |= {f for f in FRAMES if f in tile}
            await pilot.pause(0.07)
        assert len(seen) >= 2, f"the running glyph never changed: {seen}"
        assert len(tile_seen) >= 2, f"the running tile never changed: {tile_seen}"

        dash.open_mission(ids["running"])
        await pilot.pause(0.6)
        tail = dash.query_one("#m-traj", Trajectory)._spinner
        assert tail is not None and "working" in str(tail.render())
        assert _has_frame(str(tail.render()))


def _assert_running_surfaces_have_a_frame(dash, ids) -> None:  # type: ignore[no-untyped-def]
    rows = dash.query_one("#mission-list", RowList).query(Row)
    row = next(r._markup for r in rows if r.key == ids["running"])
    assert _has_frame(row), f"running row has no frame: {row!r}"
    projs = " ".join(r._markup for r in dash.query_one("#proj-list", RowList).query(Row))
    assert _has_frame(projs), f"project badge has no frame: {projs!r}"
    tile = str(dash.query_one("#st-running", Stat).render())
    assert _has_frame(tile) and "working" in tile, f"tile has no frame: {tile!r}"
    assert "[#" not in tile and "[/]" not in tile


async def test_poll_path_paints_a_frame_never_a_blank(env: Path) -> None:
    """The store poll paints with NO frame argument. It must still render a real
    one-cell frame (not a coloured empty span), focused or not."""
    ids = await seed(str(env), history_days=20)
    await _append_fresh_event(env, ids["running"])
    async with ReworkApp(poll_seconds=30).run_test(size=SIZE) as pilot:
        app = pilot.app
        await pilot.pause(0.5)
        app.action_tab("dashboard")
        await pilot.pause(0.3)
        dash = app.view("dashboard")
        for _ in range(5):
            dash._paint_overview()  # the poll path: no frame, no now
            _assert_running_surfaces_have_a_frame(dash, ids)
            await pilot.pause(0.05)
        # no empty coloured span anywhere in the running row
        assert "[#a394f7][/]" not in str(
            [r._markup for r in dash.query_one("#mission-list", RowList).query(Row)]
        )

        # unfocused: the tick stops, the still frame from the poll stays visible
        app.app_focus = False
        await pilot.pause(0.3)
        dash._paint_overview()
        _assert_running_surfaces_have_a_frame(dash, ids)
        await pilot.pause(0.3)
        _assert_running_surfaces_have_a_frame(dash, ids)


async def test_poll_path_mission_header_has_a_frame(env: Path) -> None:
    ids = await seed(str(env), history_days=20)
    await _append_fresh_event(env, ids["running"])
    async with ReworkApp(poll_seconds=30).run_test(size=SIZE) as pilot:
        app = pilot.app
        await pilot.pause(0.5)
        app.action_tab("dashboard")
        dash = app.view("dashboard")
        dash.open_mission(ids["running"])
        await pilot.pause(0.6)
        app.app_focus = False
        dash._paint_mission_static()  # poll path
        from textual.widgets import Static

        header = str(dash.query_one("#m-header", Static).render())
        assert _has_frame(header) and "working" in header, header
        runs = " ".join(r._markup for r in dash.query_one("#run-list", RowList).query(Row))
        assert _has_frame(runs), runs


def test_run_mark_without_a_frame_is_never_blank() -> None:
    from rework.tui.theme import run_mark

    for frame in (None, ""):
        out = run_mark(False, frame)
        assert _has_frame(out) and "[#a394f7][/]" not in out


# -- the stamp sweep ----------------------------------------------------------


class _FakeStore:
    """Counts events_since calls; can be told to die like a dropped connection."""

    def __init__(self, events: list[EventRecord]) -> None:
        self.events = events
        self.calls: list[dict] = []  # type: ignore[type-arg]
        self.fail = False
        self.runs: list[Run] = []
        self.run_calls = 0

    async def list_runs(self, mission_id):  # type: ignore[no-untyped-def]
        self.run_calls += 1
        return [r for r in self.runs if r.mission_id == mission_id]

    async def events_since(self, seq, mission_id=None, limit=500, *, newest_first=False):  # type: ignore[no-untyped-def]
        self.calls.append({"mission_id": mission_id, "limit": limit})
        if self.fail:
            raise ConnectionError("server closed the connection")
        rows = [e for e in self.events if e.mission_id == mission_id]
        # mirror the real store: newest window, handed back oldest-first
        return rows[-limit:]


def _ev(mission: str, run: str | None, ts: float, seq: int) -> EventRecord:
    return EventRecord(
        seq=seq, mission_id=mission, run_id=run, type=EventType.PROGRESS, payload={}, ts=ts
    )


def _data_with(store: _FakeStore):  # type: ignore[no-untyped-def]
    from rework.tui.data import TuiData

    data = TuiData()

    async def _store() -> _FakeStore:
        return store

    async def _drop() -> None:
        return None

    data.store = _store  # type: ignore[method-assign,assignment]
    data.drop_store = _drop  # type: ignore[method-assign]
    return data


async def test_event_stamps_are_one_small_query_per_running_mission() -> None:
    from rework.tui.hub import EVENT_WINDOW, STAMP_MISSIONS, Hub, Snapshot

    now = time.time()
    events = []
    for n in range(40):
        events.append(_ev(f"m{n}", f"m{n}:code:coder:1", now - 10 - n, 2 * n))
        events.append(_ev(f"m{n}", f"m{n}:test:tester:1", now - 100 - n, 2 * n + 1))
    store = _FakeStore(events)
    hub = Hub(_data_with(store))  # type: ignore[arg-type]
    snap = Snapshot()
    snap.missions = [_mission(f"m{n}", MissionStatus.RUNNING) for n in range(40)]
    await hub._event_stamps(snap)
    assert len(store.calls) == STAMP_MISSIONS, "one query per mission, capped"
    assert all(c["limit"] == EVENT_WINDOW <= 50 for c in store.calls)
    # newest per mission AND per run come out of that single result
    assert snap.last_event["m0"] == pytest.approx(now - 10)
    assert snap.last_event["m0:test:tester:1"] == pytest.approx(now - 100)
    assert snap.last_event["*"] == pytest.approx(now - 10)


async def test_event_stamp_failure_is_unknown_not_a_stale_stamp() -> None:
    from rework.tui.hub import Hub, Snapshot

    now = time.time()
    store = _FakeStore([_ev("m1", "m1:code:coder:1", now - 2000, 1)])
    hub = Hub(_data_with(store))  # type: ignore[arg-type]
    snap = Snapshot()
    snap.missions = [_mission("m1", MissionStatus.RUNNING)]
    await hub._event_stamps(snap)
    assert snap.last_event["m1"] == pytest.approx(now - 2000)  # a good read first

    store.fail = True  # then the store dies: the old stamp must NOT be replayed
    snap2 = Snapshot()
    snap2.missions = snap.missions
    await hub._event_stamps(snap2)
    assert snap2.last_event == {}
    assert not is_stalled(now, snap2.last_event.get("m1"))  # unknown animates


async def test_quiet_run_pushed_out_of_the_window_is_found_by_one_wider_read() -> None:
    now = time.time()
    store = _FakeStore([])
    quiet, busy = "m1:test:tester:1", "m1:code:coder:1"
    store.runs = [
        Run(id=quiet, mission_id="m1", role="tester", step="test", status=RunStatus.RUNNING),
        Run(id=busy, mission_id="m1", role="coder", step="code", status=RunStatus.RUNNING),
    ]
    store.events = [_ev("m1", quiet, now - 3000, 1)]
    store.events += [_ev("m1", busy, now - 60 + n * 0.1, 2 + n) for n in range(80)]
    data = _data_with(store)

    out = await data.recent_event_stamps("m1", limit=40)
    assert out[quiet] == pytest.approx(now - 3000), "the quiet run was dropped by the window"
    assert out[busy] > now - 60
    # window + list_runs + one wider read: the documented worst case for a mission
    assert [c["limit"] for c in store.calls] == [40, 400] and store.run_calls == 1


async def test_unsaturated_window_costs_one_query_and_no_run_lookup() -> None:
    now = time.time()
    store = _FakeStore([_ev("m1", "m1:code:coder:1", now - 5, 1)])
    store.runs = [
        Run(id="m1:test:tester:1", mission_id="m1", role="tester", step="t", status="running")  # type: ignore[arg-type]
    ]
    out = await _data_with(store).recent_event_stamps("m1", limit=40)
    assert "m1:test:tester:1" not in out, "a run with no events has no key (unknown)"
    assert len(store.calls) == 1 and store.run_calls == 0


# -- run-level stall detection --------------------------------------------------

LIVE, SILENT = "m-two:code:coder:1", "m-two:test:tester:1"


def _two_runs() -> list[Run]:
    return [
        Run(id=LIVE, mission_id="m-two", role="coder", step="code", status=RunStatus.RUNNING),
        Run(id=SILENT, mission_id="m-two", role="tester", step="test", status=RunStatus.RUNNING),
    ]


def _rows_by_key(rows) -> dict[str, str]:  # type: ignore[no-untyped-def]
    return {r.key: r.markup for r in rows}


async def test_silent_run_is_idle_while_a_live_sibling_animates(env: Path) -> None:
    """The wedged-one-run case. Every run event also stamps its mission, so a
    mission-granular max keeps the silent run looking alive: this goes red if
    that max (or any mission fallback) returns."""
    await seed(str(env), history_days=20)
    async with ReworkApp(poll_seconds=30).run_test(size=SIZE) as pilot:
        await pilot.pause(0.5)
        dash = pilot.app.view("dashboard")
        now = time.time()
        dash.mission_id = "m-two"
        dash.runs = _two_runs()
        dash.snap.last_event = {
            "m-two": now - 1,  # the mission is busy (the live run just spoke)
            LIVE: now - 1,
            SILENT: now - STALL_SECONDS - 120,
            "*": now - 1,
        }
        rows = _rows_by_key(dash._run_rows(FRAMES[4], now))
        assert FRAMES[4] in rows[LIVE] and "◌" not in rows[LIVE]
        assert "◌" in rows[SILENT] and not _has_frame(rows[SILENT]), rows[SILENT]

        # an unknown run (no key of its own) animates, even when the mission
        # stamp is old: the mission stamp is never substituted
        dash.snap.last_event = {"m-two": now - STALL_SECONDS - 120, LIVE: now - 1}
        rows = _rows_by_key(dash._run_rows(FRAMES[4], now))
        assert FRAMES[4] in rows[SILENT] and "◌" not in rows[SILENT]


async def test_mission_header_and_tail_name_the_silent_run(env: Path) -> None:
    from textual.widgets import Static

    ids = await seed(str(env), history_days=20)
    async with ReworkApp(poll_seconds=30).run_test(size=SIZE) as pilot:
        app = pilot.app
        await pilot.pause(0.5)
        app.action_tab("dashboard")
        dash = app.view("dashboard")
        dash.open_mission(ids["running"])
        await pilot.pause(0.6)
        mid = ids["running"]
        live, silent = f"{mid}:code:coder:1", f"{mid}:test:tester:1"
        now = time.time()
        dash.runs = [
            Run(id=live, mission_id=mid, role="coder", step="code", status=RunStatus.RUNNING),
            Run(id=silent, mission_id=mid, role="tester", step="test", status=RunStatus.RUNNING),
        ]
        dash.snap.last_event = {mid: now - 1, live: now - 1, silent: now - STALL_SECONDS - 120}
        dash.mtab = "traj"

        def header() -> str:
            return str(dash.query_one("#m-header", Static).render())

        def tail() -> str:
            step = dash.query_one("#m-traj", Trajectory)._spinner
            assert step is not None
            return str(step.render())

        # the header is the mission: it animates, but names the silent run
        dash._paint_mission_static(frame=FRAMES[2], now=now)
        assert FRAMES[2] in header() and "working" in header()
        assert "tester idle 7m" in header(), header()

        # no run selected: the tail reflects the mission (busy)
        dash.run_id = None
        dash._paint_traj_tail(FRAMES[2], now)
        assert FRAMES[2] in tail() and "working" in tail()
        assert "coder" in tail() and "tester" not in tail(), "named the silent run as working"
        # the silent run selected: the tail reflects THAT run
        dash.run_id = silent
        dash._paint_traj_tail(FRAMES[2], now)
        assert "idle" in tail() and "◌" in tail() and not _has_frame(tail()), tail()
        # the live run selected: animating
        dash.run_id = live
        dash._paint_traj_tail(FRAMES[2], now)
        assert FRAMES[2] in tail() and "working" in tail()


async def test_poll_clears_a_stale_tail_while_unfocused(env: Path) -> None:
    """Unfocused, no tick runs. When the run finishes meanwhile, the store poll
    (which does run) must remove the "working" claim."""
    from rework.kernel.state.sqlite import open_sqlite

    ids = await seed(str(env), history_days=20)
    await _append_fresh_event(env, ids["running"])
    async with ReworkApp(poll_seconds=0.3).run_test(size=SIZE) as pilot:
        app = pilot.app
        await pilot.pause(0.5)
        app.action_tab("dashboard")
        dash = app.view("dashboard")
        dash.open_mission(ids["running"])
        await pilot.pause(0.8)
        traj = dash.query_one("#m-traj", Trajectory)
        assert traj._spinner is not None and "working" in str(traj._spinner.render())

        app.app_focus = False
        store = await open_sqlite(str(env))
        try:
            await store.update_run(f"{ids['running']}:code:coder:1", status=RunStatus.COMPLETED)
        finally:
            await store.aclose()
        await pilot.pause(1.5)  # several polls, zero ticks
        assert traj._spinner is None, "stale 'working' claim survived the poll"


async def test_work_card_is_judged_by_its_own_mission_not_the_aggregate(env: Path) -> None:
    from textual.widgets import Static

    await seed(str(env), history_days=20)
    async with ReworkApp(poll_seconds=30).run_test(size=SIZE) as pilot:
        await pilot.pause(0.5)
        work = pilot.app.view("work")
        cards = {}
        for mid in ("m-dead", "m-busy", "m-new"):
            card = MissionCard(mid)
            await work.mount(card)
            card.update_from(_mission(mid, MissionStatus.RUNNING), [])
            cards[mid] = card
        await pilot.pause()
        now = time.time()
        # "*" is fresh because m-busy works; m-dead went quiet; m-new has no stamp
        work.snap.last_event = {"m-busy": now - 1, "m-dead": now - STALL_SECONDS - 90, "*": now - 1}
        work.tick(FRAMES[5], now)

        def head(mid: str) -> str:
            return str(cards[mid].query_one(".mc-head", Static).render())

        assert FRAMES[5] in head("m-busy")
        assert "◌" in head("m-dead") and not _has_frame(head("m-dead"))
        assert FRAMES[5] in head("m-new"), "unknown animates"

        # and the aggregate is never borrowed for a card with no key of its own
        work.snap.last_event = {"m-busy": now - STALL_SECONDS - 9, "*": now - STALL_SECONDS - 9}
        work.tick(FRAMES[6], now)
        assert FRAMES[6] in head("m-new"), "borrowed the stale aggregate"
