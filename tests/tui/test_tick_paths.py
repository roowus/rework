"""The unchanged-poll path (`Hub.subscribe_tick` / `View.on_tick`).

An unchanged snapshot is not broadcast, so the live polls that used to ride the
broadcast now ride the tick. Each test changes ONE underlying row (an event, a
chat message, a charge, a library item, a decided gate) while the compared
snapshot data stays identical, and asserts the view's data still updates: that
is the silent-dead-live-view failure mode. A broadcast counter proves the change
really arrived through the tick path.

The second half pins the time text: a tick does not repaint the lists, but
"3m ago" must keep moving (View.time_repaint_due, every TIME_REPAINT_SECONDS)."""

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


async def _until(pilot: Any, cond: Any, seconds: float = 3.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if cond():
            return True
        await pilot.pause(0.1)
    return bool(cond())


# -- one test per view: the tick path keeps the live poll alive ----------------


async def test_tick_polls_mission_card_events(ids: dict[str, str], env: Path) -> None:
    mid = ids["running"]
    app = ReworkApp(poll_seconds=0.2)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.6)
        view = app.view("work")
        card = MissionCard(mid)
        view.query_one("#chat-log", Trajectory).add(card)
        assert await _until(pilot, lambda: card.mission is not None and card.last_seq > 0)
        before = card.last_seq
        counter = Counter(app)
        store = await _store(env)
        try:
            await store.append_event(
                _traj(mid, f"{mid}:coder:1", "tool_call", time.time(), name="x")
            )
        finally:
            await store.aclose()
        assert await _until(pilot, lambda: card.last_seq > before), "card stopped polling events"
        assert counter.ticks > 0 and counter.broadcasts == 0, "must arrive via the tick path"
        assert not app.swallowed


async def test_tick_polls_shared_chat(ids: dict[str, str], env: Path) -> None:
    app = ReworkApp(poll_seconds=0.2)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.6)
        view = app.view("work")
        view.open_chat(ids["chat"])
        assert await _until(pilot, lambda: view._chat_cursor > 0)
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
        assert await _until(pilot, lambda: view._chat_cursor > before), "chat poll is dead"
        assert counter.ticks > 0 and counter.broadcasts == 0, "must arrive via the tick path"
        assert not app.swallowed


async def test_tick_polls_gate_steps(ids: dict[str, str], env: Path) -> None:
    app = ReworkApp(poll_seconds=0.2)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.6)
        await pilot.press("alt+3")
        await pilot.pause(0.4)
        view = app.view("gates")
        view.select_gate(ids["gate"])
        assert await _until(pilot, lambda: view._steps_ready_for == ids["gate"])
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
        assert await _until(pilot, lambda: view._steps_seq > before), "step poll is dead"
        assert counter.ticks > 0 and counter.broadcasts == 0, "must arrive via the tick path"
        assert not app.swallowed


async def test_tick_reloads_gate_history(ids: dict[str, str], env: Path) -> None:
    app = ReworkApp(poll_seconds=0.2)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.6)
        await pilot.press("alt+3")
        await pilot.pause(0.4)
        view = app.view("gates")
        view.mode = "decided"
        view._load_history()
        await pilot.pause(0.5)
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
        assert await _until(pilot, lambda: len(view.history) > before), "history poll is dead"
        assert counter.ticks > 0 and counter.broadcasts == 0, "must arrive via the tick path"
        assert not app.swallowed


async def test_tick_reloads_cost(ids: dict[str, str], env: Path) -> None:
    app = ReworkApp(poll_seconds=0.2)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.6)
        await pilot.press("alt+5")
        await pilot.pause(0.8)
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
        assert await _until(pilot, lambda: "tick-probe-model" in str(view.by)), "cost reload dead"
        assert counter.ticks > 0 and counter.broadcasts == 0, "must arrive via the tick path"
        assert not app.swallowed


async def test_tick_rechecks_library(ids: dict[str, str], env: Path) -> None:
    import os

    from rework.library.curation import contribute
    from rework.library.model import ItemType
    from rework.library.store import GitLibrary, open_library_store

    await seed_library(str(env), os.environ["REWORK_LIBRARY_DIR"])
    app = ReworkApp(poll_seconds=0.2)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.6)
        await pilot.press("alt+4")
        await pilot.pause(0.8)
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
        assert await _until(pilot, lambda: any(i.name == "tick-probe-skill" for i in view.items)), (
            "library recheck is dead"
        )
        assert counter.ticks > 0 and counter.broadcasts == 0, "must arrive via the tick path"
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
    view._time_painted_at = time.monotonic() - TIME_REPAINT_SECONDS - 1


async def test_dashboard_time_text_advances_on_an_unchanged_snapshot(
    ids: dict[str, str], env: Path, clock: SimpleNamespace
) -> None:
    await _fresh_mission(env, "m-timeprobe01")
    app = ReworkApp(poll_seconds=0.2)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.8)
        dash = app.view("dashboard")
        lst = dash.query_one("#mission-list")

        def row() -> str:
            return next(r._markup for r in lst.query("Row") if r.key == "m-timeprobe01")

        assert "just now" in row()
        await dash.workers.wait_for_complete()  # the first enrich paints when it ends
        counter = Counter(app)
        painted = 0
        real = dash._paint_overview

        def counting() -> None:
            nonlocal painted
            painted += 1
            real()

        dash._paint_overview = counting  # type: ignore[method-assign]
        clock.offset = 600.0  # ten minutes pass; the data does not change
        await pilot.pause(1.0)
        assert counter.ticks >= 2 and counter.broadcasts == 0
        assert painted == 0, "inside the cadence an unchanged tick must not repaint"
        assert "just now" in row()
        _due(dash)
        await pilot.pause(0.6)
        assert painted >= 1 and counter.broadcasts == 0
        assert "just now" not in row() and "10m ago" in row(), row()
        # and the stamp moved: the next tick is inside the cadence again
        seen = painted
        await pilot.pause(0.6)
        assert painted == seen
        assert not app.swallowed


async def test_open_mission_header_time_advances(
    ids: dict[str, str], env: Path, clock: SimpleNamespace
) -> None:
    await _fresh_mission(env, "m-timeprobe02", status=MissionStatus.DONE)
    app = ReworkApp(poll_seconds=0.2)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.6)
        dash = app.view("dashboard")
        app.open_mission("m-timeprobe02")
        header = dash.query_one("#m-header", Static)
        assert await _until(pilot, lambda: "started just now" in str(header.render()))
        clock.offset = 3 * 3600.0
        _due(dash)
        assert await _until(pilot, lambda: "started 3h ago" in str(header.render())), str(
            header.render()
        )


async def test_gates_time_text_advances_on_an_unchanged_snapshot(
    ids: dict[str, str], env: Path, clock: SimpleNamespace
) -> None:
    app = ReworkApp(poll_seconds=0.2)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.6)
        await pilot.press("alt+3")
        await pilot.pause(0.4)
        view = app.view("gates")
        view.select_gate(ids["gate"])
        head = view.query_one("#gate-head", Static)
        lst = view.query_one("#gate-list")
        assert await _until(pilot, lambda: view._steps_ready_for == ids["gate"])
        await pilot.pause(0.3)  # the activation message has been handled
        await view.workers.wait_for_complete()
        assert "waiting 20m" in str(head.render())
        counter = Counter(app)
        clock.offset = 2 * 3600.0
        await pilot.pause(0.8)
        assert "waiting 20m" in str(head.render()), "inside the cadence: no repaint"
        _due(view)
        assert await _until(pilot, lambda: "waiting 2h" in str(head.render())), str(head.render())
        assert counter.broadcasts == 0
        rows = [r._markup for r in lst.query("Row")]
        assert rows and all("20m ago" not in m for m in rows), rows
        assert not app.swallowed


async def test_mission_card_time_text_advances_on_an_unchanged_snapshot(
    ids: dict[str, str], env: Path, clock: SimpleNamespace
) -> None:
    # a finished card is never polled again: only the cadence can move its text
    await _fresh_mission(env, "m-timeprobe03", status=MissionStatus.DONE)
    app = ReworkApp(poll_seconds=0.2)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.6)
        view = app.view("work")
        card = MissionCard("m-timeprobe03")
        view.query_one("#chat-log", Trajectory).add(card)
        assert await _until(pilot, lambda: card.tail_filled)
        await view.workers.wait_for_complete()  # the backfill paints once more
        await pilot.pause(0.3)
        head = card.query_one(".mc-head", Static)
        assert "just now" in str(head.render())
        counter = Counter(app)
        clock.offset = 900.0
        await pilot.pause(0.8)
        assert "just now" in str(head.render()), "inside the cadence: no repaint"
        _due(view)
        assert await _until(pilot, lambda: "15m ago" in str(head.render())), str(head.render())
        assert counter.broadcasts == 0
        assert not app.swallowed
