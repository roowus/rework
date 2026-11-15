"""The unchanged-poll path (`Hub.subscribe_tick` / `View.on_tick`).

An unchanged snapshot is not broadcast, so the live polls that used to ride the
broadcast now ride the tick. Each test changes ONE underlying row (an event, a
chat message, a charge, a library item, a decided gate) while the compared
snapshot data stays identical, and asserts the view's data still updates: that
is the silent-dead-live-view failure mode. A broadcast counter proves the change
really arrived through the tick path.

The second half pins the time text: a tick does not repaint the lists, but
"3m ago" must keep moving (View.time_repaint_due, every TIME_REPAINT_SECONDS).

Deterministic by construction: poll_seconds is an hour so the app's own timer
never fires, the test drives each poll itself (`_tick`: one `Hub.refresh`, then
every worker it started runs to completion), the clock `ago()` reads is a fixture,
and the time cadence is moved through `View._time_painted_at`. No test waits for
wall time to pass or loops until a deadline."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from textual.widgets import Static

sys.path.insert(0, str(Path(__file__).parent))

from seed import _traj, seed, seed_library

from rework.kernel.state.records import (
    Charge,
    ChatMessage,
    Gate,
    GateKind,
    Mission,
    MissionStatus,
)
from rework.tui import theme
from rework.tui.app import ReworkApp
from rework.tui.hub import midnight
from rework.tui.views.base import TIME_REPAINT_SECONDS
from rework.tui.widgets.cards import MissionCard
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
    return db


@pytest.fixture
async def ids(env: Path) -> dict[str, str]:
    return await seed(str(env), history_days=20)


async def _store(db: Path) -> Any:
    from rework.kernel.state.sqlite import open_sqlite

    return await open_sqlite(str(db))


class Counter:
    """Counts broadcasts and ticks off a live hub."""

    def __init__(self, app: ReworkApp) -> None:
        self.broadcasts = 0
        self.ticks = 0
        app.hub.subscribe(lambda _s: setattr(self, "broadcasts", self.broadcasts + 1))
        app.hub.subscribe_tick(lambda _s: setattr(self, "ticks", self.ticks + 1))


async def _settle(app: ReworkApp, pilot: Any) -> None:
    """Handle every queued message and finish every running worker (a worker
    can post a message that starts another, hence the second pause)."""
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


async def _boot(app: ReworkApp, pilot: Any) -> None:
    """Let the mount-time poll finish, so the next poll is an unchanged one."""
    await _settle(app, pilot)
    assert app.hub.snapshot.loaded


async def _tick(app: ReworkApp, pilot: Any) -> None:
    """One poll, driven by the test, then everything it started completes."""
    await _settle(app, pilot)
    await app.hub.refresh()
    await _settle(app, pilot)


def _app() -> ReworkApp:
    return ReworkApp(poll_seconds=3600.0)  # the app's own timer never fires in a test


# -- one test per view: the tick path keeps the live poll alive ----------------


async def test_tick_polls_mission_card_events(ids: dict[str, str], env: Path) -> None:
    mid = ids["running"]
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _boot(app, pilot)
        view = app.view("work")
        card = MissionCard(mid)
        view.query_one("#chat-log", Trajectory).add(card)
        await _tick(app, pilot)
        assert card.mission is not None and card.last_seq > 0
        before = card.last_seq
        counter = Counter(app)
        store = await _store(env)
        try:
            await store.append_event(
                _traj(mid, f"{mid}:coder:1", "tool_call", time.time(), name="x")
            )
        finally:
            await store.aclose()
        await _tick(app, pilot)
        assert card.last_seq > before, "card stopped polling events"
        assert counter.ticks == 1 and counter.broadcasts == 0, "must arrive via the tick path"
        assert not app.swallowed


async def test_tick_polls_shared_chat(ids: dict[str, str], env: Path) -> None:
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _boot(app, pilot)
        view = app.view("work")
        view.open_chat(ids["chat"])
        await _settle(app, pilot)
        assert view._chat_cursor > 0
        before = view._chat_cursor
        counter = Counter(app)
        store = await _store(env)
        try:
            chat = await store.get_chat(ids["chat"])
            # ts == the chat's updated_at: appending bumps updated_at to the
            # message ts, and a moved updated_at would be a snapshot change
            await store.append_chat_message(
                ChatMessage(
                    chat_id=ids["chat"],
                    role="user",
                    body={"role": "user", "content": "from another instance"},
                    display="from another instance",
                    ts=chat.updated_at,
                )
            )
        finally:
            await store.aclose()
        await _tick(app, pilot)
        assert view._chat_cursor > before, "chat poll is dead"
        assert counter.ticks == 1 and counter.broadcasts == 0, "must arrive via the tick path"
        assert not app.swallowed


async def test_tick_polls_gate_steps(ids: dict[str, str], env: Path) -> None:
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _boot(app, pilot)
        await pilot.press("alt+3")
        await _settle(app, pilot)
        view = app.view("gates")
        view.select_gate(ids["gate"])
        await _settle(app, pilot)
        assert view._steps_ready_for == ids["gate"]
        before = view._steps_seq
        counter = Counter(app)
        store = await _store(env)
        try:
            await store.append_event(
                _traj(
                    ids["waiting"],
                    f"{ids['waiting']}:coder:1",
                    "tool_call",
                    time.time(),
                    name="run_command",
                    arguments="command=pytest -q",
                )
            )
        finally:
            await store.aclose()
        await _tick(app, pilot)
        assert view._steps_seq > before, "step poll is dead"
        assert counter.ticks == 1 and counter.broadcasts == 0, "must arrive via the tick path"
        assert not app.swallowed


async def test_tick_reloads_gate_history(ids: dict[str, str], env: Path) -> None:
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _boot(app, pilot)
        await pilot.press("alt+3")
        await _settle(app, pilot)
        view = app.view("gates")
        view.mode = "decided"
        view._load_history()
        await _settle(app, pilot)
        before = len(view.history)
        counter = Counter(app)
        store = await _store(env)
        try:
            # created and decided between two polls: never in the open list, so
            # the compared snapshot data does not move
            await store.create_gate(
                Gate(
                    id="gate-tick-history",
                    mission_id=ids["waiting"],
                    kind=GateKind.SHIP,
                    question="decided elsewhere",
                )
            )
            await store.decide_gate("gate-tick-history", True)
        finally:
            await store.aclose()
        await _tick(app, pilot)
        assert len(view.history) > before, "history poll is dead"
        assert counter.ticks == 1 and counter.broadcasts == 0, "must arrive via the tick path"
        assert not app.swallowed


async def test_tick_reloads_cost(ids: dict[str, str], env: Path) -> None:
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _boot(app, pilot)
        await pilot.press("alt+5")
        await _settle(app, pilot)
        view = app.view("cost")
        view._loaded_at = 0.0  # the 60 s reload is due
        counter = Counter(app)
        store = await _store(env)
        try:
            # yesterday: spend_today (part of the compared snapshot) stays put
            await store.charge(
                Charge(
                    mission_id="m-hist00000000",
                    model="tick-probe-model",
                    input_tokens=10,
                    output_tokens=10,
                    cost_usd=1.0,
                    ts=midnight() - 3600,
                )
            )
        finally:
            await store.aclose()
        await _tick(app, pilot)
        assert "tick-probe-model" in str(view.by), "cost reload dead"
        assert counter.ticks == 1 and counter.broadcasts == 0, "must arrive via the tick path"
        assert not app.swallowed


async def test_tick_rechecks_library(ids: dict[str, str], env: Path) -> None:
    import os

    from rework.library.curation import contribute
    from rework.library.model import ItemType
    from rework.library.store import GitLibrary, open_library_store

    await seed_library(str(env), os.environ["REWORK_LIBRARY_DIR"])
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _boot(app, pilot)
        await pilot.press("alt+4")
        await _settle(app, pilot)
        view = app.view("library")
        assert view.items, "the seeded items are listed"
        counter = Counter(app)
        index = await open_library_store(str(env))
        git = GitLibrary(Path(os.environ["REWORK_LIBRARY_DIR"]), author="rework <r@test.local>")
        try:
            await contribute(
                index,
                git,
                type_=ItemType.SKILL,
                name="tick-probe-skill",
                description="added by another instance",
                body="# probe",
                tags=["probe"],
            )
        finally:
            await index.aclose()
        await _tick(app, pilot)
        assert any(i.name == "tick-probe-skill" for i in view.items), "library recheck is dead"
        assert counter.ticks == 1 and counter.broadcasts == 0, "must arrive via the tick path"
        assert not app.swallowed


# -- time text keeps moving on an unchanged snapshot ----------------------------


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Move the wall clock `ago()` reads, nothing else (asyncio keeps monotonic)."""
    state = SimpleNamespace(offset=0.0)
    real = time.time
    shim = SimpleNamespace(
        time=lambda: real() + state.offset, strftime=time.strftime, localtime=time.localtime
    )
    monkeypatch.setattr(theme, "time", shim)
    return state


async def _fresh_mission(env: Path, mid: str, **kw: Any) -> None:
    store = await _store(env)
    try:
        await store.create_mission(
            Mission(id=mid, objective="TIMEPROBE objective", created_at=time.time(), **kw)
        )
    finally:
        await store.aclose()


def _due(view: Any) -> None:
    """Move the view's time stamp back: the cadence is due on the next tick."""
    view._time_painted_at = time.monotonic() - TIME_REPAINT_SECONDS - 1


def _count(view: Any, name: str) -> SimpleNamespace:
    """Count calls of one paint method, still running it."""
    seen = SimpleNamespace(n=0)
    real = getattr(view, name)

    def counting(*a: Any, **kw: Any) -> Any:
        seen.n += 1
        return real(*a, **kw)

    setattr(view, name, counting)
    return seen


async def test_dashboard_time_text_advances_on_an_unchanged_snapshot(
    ids: dict[str, str], env: Path, clock: SimpleNamespace
) -> None:
    await _fresh_mission(env, "m-timeprobe01")
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _boot(app, pilot)
        dash = app.view("dashboard")
        dash._enriched_at = time.time() + 3600  # the enrich throttle is not under test
        lst = dash.query_one("#mission-list")

        def row() -> str:
            return next(r._markup for r in lst.query("Row") if r.key == "m-timeprobe01")

        assert "just now" in row()
        counter = Counter(app)
        painted = _count(dash, "_paint_overview")
        clock.offset = 600.0  # ten minutes pass; the data does not change
        await _tick(app, pilot)
        assert counter.ticks == 1 and counter.broadcasts == 0
        assert painted.n == 0, "inside the cadence an unchanged tick must not repaint"
        assert "just now" in row()
        _due(dash)
        await _tick(app, pilot)
        assert painted.n == 1 and counter.broadcasts == 0
        assert "just now" not in row() and "10m ago" in row(), row()
        # and the stamp moved: the next tick is inside the cadence again
        assert not dash.time_repaint_due()
        await _tick(app, pilot)
        assert painted.n == 1
        assert not app.swallowed


async def test_open_mission_header_time_advances(
    ids: dict[str, str], env: Path, clock: SimpleNamespace
) -> None:
    await _fresh_mission(env, "m-timeprobe02", status=MissionStatus.DONE)
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _boot(app, pilot)
        dash = app.view("dashboard")
        dash._enriched_at = time.time() + 3600
        app.open_mission("m-timeprobe02")
        await _settle(app, pilot)
        header = dash.query_one("#m-header", Static)
        assert "started just now" in str(header.render()), str(header.render())
        clock.offset = 3 * 3600.0
        await _tick(app, pilot)
        assert "started just now" in str(header.render()), "inside the cadence: no repaint"
        _due(dash)
        await _tick(app, pilot)
        assert "started 3h ago" in str(header.render()), str(header.render())


async def test_an_unchanged_poll_does_not_repaint_the_open_mission(
    ids: dict[str, str], env: Path
) -> None:
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _boot(app, pilot)
        dash = app.view("dashboard")
        dash._enriched_at = time.time() + 3600
        app.open_mission(ids["running"])
        await _settle(app, pilot)
        assert dash.full_mission is not None
        runs = _count(dash, "_paint_runs")
        tabs = _count(dash, "_paint_detail_tabs")
        head = _count(dash, "_paint_mission_static")
        counter = Counter(app)
        await _tick(app, pilot)
        await _tick(app, pilot)
        assert counter.ticks == 2 and counter.broadcasts == 0
        assert (runs.n, tabs.n, head.n) == (0, 0, 0), "nothing changed: nothing repaints"
        _due(dash)  # the time cadence repaints the lot once
        await _tick(app, pilot)
        assert (runs.n, tabs.n, head.n) == (1, 1, 1)
        # a run changing is a real change: it repaints without waiting for the cadence
        store = await _store(env)
        try:
            await store.append_event(
                _traj(
                    ids["running"],
                    f"{ids['running']}:coder:1",
                    "tool_call",
                    time.time(),
                    name="y",
                )
            )
        finally:
            await store.aclose()
        await _tick(app, pilot)
        assert (runs.n, tabs.n, head.n) == (2, 2, 2)
        assert not app.swallowed


async def test_gates_time_text_advances_on_an_unchanged_snapshot(
    ids: dict[str, str], env: Path, clock: SimpleNamespace
) -> None:
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _boot(app, pilot)
        await pilot.press("alt+3")
        await _settle(app, pilot)
        view = app.view("gates")
        view.select_gate(ids["gate"])
        await _settle(app, pilot)
        assert view._steps_ready_for == ids["gate"]
        head = view.query_one("#gate-head", Static)
        lst = view.query_one("#gate-list")
        assert "waiting 20m" in str(head.render())
        counter = Counter(app)
        clock.offset = 2 * 3600.0
        await _tick(app, pilot)
        assert "waiting 20m" in str(head.render()), "inside the cadence: no repaint"
        _due(view)
        await _tick(app, pilot)
        assert "waiting 2h" in str(head.render()), str(head.render())
        assert counter.broadcasts == 0
        rows = [r._markup for r in lst.query("Row")]
        assert rows and all("20m ago" not in m for m in rows), rows
        assert not app.swallowed


async def test_mission_card_time_text_advances_on_an_unchanged_snapshot(
    ids: dict[str, str], env: Path, clock: SimpleNamespace
) -> None:
    # a finished card is never polled again: only the cadence can move its text
    await _fresh_mission(env, "m-timeprobe03", status=MissionStatus.DONE)
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _boot(app, pilot)
        view = app.view("work")
        card = MissionCard("m-timeprobe03")
        view.query_one("#chat-log", Trajectory).add(card)
        await _tick(app, pilot)
        assert card.tail_filled
        head = card.query_one(".mc-head", Static)
        assert "just now" in str(head.render())
        counter = Counter(app)
        clock.offset = 900.0
        await _tick(app, pilot)
        assert "just now" in str(head.render()), "inside the cadence: no repaint"
        _due(view)
        await _tick(app, pilot)
        assert "15m ago" in str(head.render()), str(head.render())
        assert counter.broadcasts == 0
        assert not app.swallowed


async def test_work_chat_list_time_text_advances_on_an_unchanged_snapshot(
    ids: dict[str, str], env: Path, clock: SimpleNamespace
) -> None:
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _boot(app, pilot)
        view = app.view("work")
        lst = view.query_one("#chat-list")

        def row() -> str:
            return next(r._markup for r in lst.query("Row") if "auth audit" in r._markup)

        assert "just now" in row(), row()
        painted = _count(view, "_paint_chat_list")
        counter = Counter(app)
        clock.offset = 2 * 3600.0
        await _tick(app, pilot)
        assert painted.n == 0 and "just now" in row(), "inside the cadence: no repaint"
        _due(view)
        await _tick(app, pilot)
        assert painted.n == 1 and "2h ago" in row(), row()
        assert counter.broadcasts == 0
        assert not app.swallowed


async def test_library_time_text_advances_on_an_unchanged_snapshot(
    ids: dict[str, str], env: Path, clock: SimpleNamespace
) -> None:
    import os

    await seed_library(str(env), os.environ["REWORK_LIBRARY_DIR"])
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _boot(app, pilot)
        await pilot.press("alt+4")
        await _settle(app, pilot)
        view = app.view("library")
        assert view.items
        lst = view.query_one("#lib-list")
        first = view.items[0]
        view.selected = first
        view._paint_reader()
        meta = view.query_one("#lib-meta", Static)

        def row() -> str:
            return next(r._markup for r in lst.query("Row") if r.key == first.id)

        assert "just now" in row() and "updated just now" in str(meta.render())
        counter = Counter(app)
        clock.offset = 5 * 3600.0
        await _tick(app, pilot)
        assert "just now" in row(), "inside the cadence: no repaint"
        _due(view)
        await _tick(app, pilot)
        assert "5h ago" in row() and "updated 5h ago" in str(meta.render()), row()
        assert counter.broadcasts == 0
        assert not app.swallowed
