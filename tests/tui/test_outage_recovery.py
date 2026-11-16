"""Outage and recovery: what the owner sees when the link to the store drops.

The live failure was a dropped database link. Nothing here sleeps or opens a
socket: the store factory is wrapped so the next N calls raise
`psycopg.OperationalError("server closed the connection")`, which is what a dead
link produces and which `TuiData` classifies as a connection error, so the real
reconnect ladder runs. The app is built with `poll_seconds=3600.0` so its own
timer never fires and every poll is driven by the test.
"""

from __future__ import annotations

import contextlib
import inspect
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).parent))

from seed import seed

from rework.kernel.state.records import GateStatus
from rework.tui import spinner
from rework.tui.app import ReworkApp
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


OUTAGE = 10**9  # an outage is unconditional, not a count of failed opens


class FlakyFactory:
    """The real store factory, with a switch to drop the link for the next N
    calls. `fail_next` is how a test says "the connection is down"; the count is
    read back to prove that no query slipped past the backoff."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.fail_next = 0
        self.calls = 0
        self.dropped = 0

    async def __call__(self) -> Any:
        self.calls += 1
        if self.fail_next:
            self.fail_next -= 1
            self.dropped += 1
            raise psycopg.OperationalError("server closed the connection")
        out = self._inner()
        if inspect.isawaitable(out):
            out = await out
        return out


def _settle(app: ReworkApp, pilot: Any) -> Any:
    return pilot.pause()


async def _tick(app: ReworkApp, pilot: Any) -> None:
    """One poll, driven by the test, plus every worker it started."""
    await pilot.pause()
    await app.hub.refresh()
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


async def _boot(app: ReworkApp, pilot: Any) -> None:
    await _tick(app, pilot)
    assert app.hub.snapshot.loaded


def _zero_backoff(app: ReworkApp) -> None:
    """Every deadline that could swallow the next poll: the hub's own backoff,
    the dashboard's full-mission fetch, and each work card's fetch."""
    app.hub._next_try = 0.0
    for view in app.views():
        for attr, value in (("_full_next_try", 0.0), ("_full_failures", 0)):
            if hasattr(view, attr):
                setattr(view, attr, value)
        for card in view.query(MissionCard):
            for attr, value in (("fetch_next_try", 0.0), ("fetch_failures", 0)):
                if hasattr(card, attr):
                    setattr(card, attr, value)


async def _recover(app: ReworkApp, pilot: Any, factory: FlakyFactory) -> None:
    """Bring the link back: stop failing, drop the dead store, poll once."""
    factory.fail_next = 0
    await app.hub.data.drop_store()
    _zero_backoff(app)
    await _tick(app, pilot)


async def _go_offline(app: ReworkApp, pilot: Any, factory: FlakyFactory) -> None:
    """Kill the connection at the factory: the pooled store can no longer be
    re-opened, which is the owner's outage. The cache is dropped so the factory
    really is asked again on the next query."""
    factory.fail_next = OUTAGE
    await app.hub.data.drop_store()
    await _tick(app, pilot)
    assert app.hub.data._reconnecting_hint, "the data layer is flagged as reconnecting"
    assert app.hub.snapshot.online is False


def _dash(app: ReworkApp) -> Any:
    for view in app.views():
        if hasattr(view, "open_mission") and hasattr(view, "_paint_key"):
            return view
    raise AssertionError("no dashboard view")  # pragma: no cover


def _work(app: ReworkApp) -> Any:
    for view in app.views():
        if hasattr(view, "open_chat"):
            return view
    raise AssertionError("no work view")  # pragma: no cover


@contextlib.asynccontextmanager
async def _running(env: Path) -> AsyncIterator[tuple[ReworkApp, Any, FlakyFactory]]:
    app = ReworkApp(poll_seconds=3600.0)  # the app's own timer never fires here
    async with app.run_test(size=SIZE) as pilot:
        await _boot(app, pilot)
        factory = FlakyFactory(app.hub.data._store_factory)
        app.hub.data._store_factory = factory
        yield app, pilot, factory


async def test_offline_marker_shown_and_last_good_data_kept(env: Path, ids: dict[str, str]) -> None:
    async with _running(env) as (app, pilot, factory):
        before = [m.id for m in app.hub.snapshot.missions]
        assert before, "the seed painted rows before the link dropped"
        await _go_offline(app, pilot, factory)
        snap = app.hub.snapshot
        assert snap.online is False
        assert snap.error, "the marker is a hint string, not a bare word"
        assert "OperationalError" in snap.error
        assert snap.loaded, "the last good snapshot is still served"
        assert [m.id for m in snap.missions] == before, "rows are kept, not blanked"
        assert snap.mission(ids["running"]) is not None


async def test_recovers_without_restart(env: Path, ids: dict[str, str]) -> None:
    async with _running(env) as (app, pilot, factory):
        data = app.hub.data
        epoch = data.store_epoch
        await _go_offline(app, pilot, factory)
        assert app.hub.snapshot.online is False
        await _recover(app, pilot, factory)
        snap = app.hub.snapshot
        assert snap.online is True
        assert snap.error == ""
        assert snap.loaded and snap.missions
        # same app, same data service: recovery is a reconnect, not a restart
        assert app.is_running
        assert app.hub.data is data
        assert data.store_epoch != epoch, "the store really was re-opened"
        assert (await data.get_mission(ids["running"])) is not None


async def test_open_mission_survives_outage(env: Path, ids: dict[str, str]) -> None:
    async with _running(env) as (app, pilot, factory):
        dash = _dash(app)
        dash.open_mission(ids["waiting"])
        await _tick(app, pilot)
        assert dash.full_mission is not None
        repaints: list[int] = []
        real = dash._paint_runs

        def counting() -> None:
            repaints.append(1)
            real()

        dash._paint_runs = counting
        await _go_offline(app, pilot, factory)
        assert app.hub.snapshot.online is False
        assert dash.mission_id == ids["waiting"], "the page stays open through the outage"
        await _recover(app, pilot, factory)
        assert app.hub.snapshot.online is True
        assert repaints, "the open mission repainted when the link came back"
        assert dash.full_mission is not None, "header and runs are readable again"

        data = app.hub.data
        store = await data.store()
        gates = await data.gates(ids["waiting"])
        assert gates, "the gates of the open mission are readable"
        assert await data.decide_gate(ids["gate"], True) is True
        # a retried click lands once: the same verdict re-reports True, the
        # opposite verdict is refused, and the row keeps the first answer
        assert await data.decide_gate(ids["gate"], True) is True
        assert await data.decide_gate(ids["gate"], False) is False
        gate = await store.get_gate(ids["gate"])
        assert gate.status is GateStatus.APPROVED


async def test_open_chat_survives_outage(env: Path, ids: dict[str, str]) -> None:
    async with _running(env) as (app, pilot, factory):
        work = _work(app)
        work.open_chat(ids["chat"])
        await _tick(app, pilot)
        log = work.query_one("#chat-log", Trajectory)
        painted = len(log.children)
        assert painted > 0, "the transcript loaded"
        await _go_offline(app, pilot, factory)
        assert app.hub.snapshot.online is False
        assert len(work.query_one("#chat-log", Trajectory).children) == painted
        await _recover(app, pilot, factory)
        assert app.hub.snapshot.online is True
        assert work.convo is not None and work.convo.id == ids["chat"]
        assert len(log.children) == painted, "the transcript is intact"
        # the store is readable again: the next poll can pick up new rows
        assert await app.hub.data.chat_messages_max_id(ids["chat"]) > 0


async def test_long_outage_recovers_without_duplicate_rows(env: Path, ids: dict[str, str]) -> None:
    async with _running(env) as (app, pilot, factory):
        work = _work(app)
        work.open_chat(ids["chat"])
        await _tick(app, pilot)
        log = work.query_one("#chat-log", Trajectory)
        painted = len(log.children)
        await _go_offline(app, pilot, factory)
        # an outage longer than the backoff cap: the deadline is now far out
        factory.fail_next = OUTAGE
        app.hub._next_try = time.monotonic() + 3600.0
        calls = factory.calls
        await app.hub.refresh()
        await pilot.pause()
        assert factory.calls == calls, "nothing polls before the deadline expires"
        await _recover(app, pilot, factory)
        assert app.hub.snapshot.online is True
        rows = [w.id for w in log.children if w.id]
        assert len(rows) == len(set(rows)), "no row is painted twice"
        assert len(log.children) >= painted, "the cursor did not lose a row"
        mark = getattr(work, "_rendered_max_id", 0)
        assert isinstance(mark, int)


async def test_spinner_idle_while_offline(env: Path, ids: dict[str, str]) -> None:
    async with _running(env) as (app, pilot, factory):
        mid = ids["running"]
        before = app.hub.snapshot.last_event.get(mid)
        assert before is not None, "the seeded running mission has an activity stamp"
        await _go_offline(app, pilot, factory)
        after = app.hub.snapshot.last_event.get(mid)
        # an outage cannot manufacture a fresh stamp: it is the old one or unknown
        assert after is None or after <= before
        if after is None:  # unknown is "nothing observed", never "stalled"
            assert spinner.is_stalled(time.time(), after) is False
        assert app.hub.snapshot.online is False
        await _recover(app, pilot, factory)
        assert app.hub.snapshot.online is True
        assert app.hub.snapshot.last_event.get(mid) == before, "the stamp is back after recovery"
