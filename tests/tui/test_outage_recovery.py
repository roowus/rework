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
from textual.widgets import Static

sys.path.insert(0, str(Path(__file__).parent))

from seed import seed

from rework.kernel.state.records import ChatMessage, EventRecord, GateStatus
from rework.tui import actions, spinner
from rework.tui.app import ReworkApp
from rework.tui.spinner import FRAMES, IDLE_GLYPH
from rework.tui.theme import run_mark
from rework.tui.views import dashboard as dashboard_view
from rework.tui.widgets.base import Stat
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

        # the real user path, while the link is down: refused loudly, not lost
        data = app.hub.data
        data._reconnecting_hint = ""  # only this decide may set it again
        toasts = len(app.notifications_sent)
        assert await actions.decide(app, ids["gate"], True) is False
        assert data._reconnecting_hint, "a decide that cannot reach the store flags the outage"
        sent = [text for _, text in app.notifications_sent[toasts:]]
        assert any(t.startswith("Could not answer gate") for t in sent), sent
        store = await _fresh_store(env)
        try:
            assert (await store.get_gate(ids["gate"])).status is GateStatus.OPEN
        finally:
            await store.aclose()

        repaints.clear()  # only a repaint caused by the recovery counts
        await _recover(app, pilot, factory)
        assert app.hub.snapshot.online is True
        assert repaints, "the open mission repainted when the link came back"
        assert dash.full_mission is not None, "header and runs are readable again"
        assert await data.gates(ids["waiting"]), "the gates of the open mission are readable"

        # the retried click now lands, with a note and a typed decision
        note, decision = "ship it", {"answer": "yes"}
        assert await actions.decide(app, ids["gate"], True, note=note, decision=decision) is True
        store = await data.store()
        first = await store.get_gate(ids["gate"])
        assert first.status is GateStatus.APPROVED
        assert first.decided_at is not None
        mission = await store.get_mission(ids["waiting"])
        events = len(await store.events_since(0, ids["waiting"]))

        # a repeated or contradicting click lands exactly once: same verdict
        # re-reports True, the opposite is refused, and nothing is rewritten
        assert await actions.decide(app, ids["gate"], True, note="again", decision={"a": 1})
        assert await actions.decide(app, ids["gate"], False, note="no") is False
        again = await store.get_gate(ids["gate"])
        assert again.status is GateStatus.APPROVED
        assert again.decided_at == first.decided_at
        assert again.decision_note == first.decision_note == note
        assert again.decision == first.decision == decision
        after = await store.get_mission(ids["waiting"])
        assert after.status == mission.status, "a repeat does not resume the mission again"
        assert len(await store.events_since(0, ids["waiting"])) == events


async def _fresh_store(env: Path) -> Any:
    """A direct, second connection to the same database: what a daemon on the
    other side of the link would see, independent of the TUI's own store."""
    from rework.config import Config, open_store_from_config

    return await open_store_from_config(Config.from_env())


def _texts(log: Trajectory) -> list[str]:
    """What each painted row says (Trajectory children carry no ids)."""
    return [str(getattr(child, "_head", "")) for child in log.children]


async def _chat_through_outage(
    env: Path, app: ReworkApp, pilot: Any, factory: FlakyFactory, ids: dict[str, str], text: str
) -> tuple[Any, Any, int]:
    """Open the seeded chat, drop the link, write a user row to the store while
    it is down, and return (work view, log, rows painted before the outage)."""
    work = _work(app)
    work.open_chat(ids["chat"])
    await _tick(app, pilot)
    log = work.query_one("#chat-log", Trajectory)
    painted = len(log.children)
    assert painted > 0, "the transcript loaded"
    await _go_offline(app, pilot, factory)
    store = await _fresh_store(env)
    try:
        await store.append_chat_message(
            ChatMessage(chat_id=ids["chat"], role="user", body={"content": text}, display=text)
        )
    finally:
        await store.aclose()
    return work, log, painted


async def _assert_chat_intact(
    app: ReworkApp, pilot: Any, work: Any, log: Trajectory, painted: int, text: str, ids: Any
) -> None:
    assert app.hub.snapshot.online is True
    assert work.convo is not None and work.convo.id == ids["chat"]
    assert sum(text in t for t in _texts(log)) == 1, "the new row is painted exactly once"
    assert len(log.children) == painted + 1, "nothing lost, nothing added twice"
    head = await app.hub.data.chat_messages_max_id(ids["chat"])
    assert work._chat_cursor == work._rendered_max_id == head
    await _tick(app, pilot)  # one more poll: no duplicate, no loss
    assert len(log.children) == painted + 1
    assert sum(text in t for t in _texts(log)) == 1


async def test_open_chat_survives_outage(env: Path, ids: dict[str, str]) -> None:
    async with _running(env) as (app, pilot, factory):
        text = "written while the link was down"
        work, log, painted = await _chat_through_outage(env, app, pilot, factory, ids, text)
        assert len(log.children) == painted, "an outage paints nothing and loses nothing"
        await _recover(app, pilot, factory)
        await _assert_chat_intact(app, pilot, work, log, painted, text, ids)


async def test_long_outage_recovers_without_duplicate_rows(env: Path, ids: dict[str, str]) -> None:
    async with _running(env) as (app, pilot, factory):
        text = "written during a long outage"
        work, log, painted = await _chat_through_outage(env, app, pilot, factory, ids, text)
        # an outage longer than the backoff cap: the deadline is now far out
        app.hub._next_try = time.monotonic() + 3600.0
        calls = factory.calls
        await app.hub.refresh()
        await pilot.pause()
        assert factory.calls == calls, "nothing polls before the deadline expires"
        assert len(log.children) == painted
        await _recover(app, pilot, factory)
        await _assert_chat_intact(app, pilot, work, log, painted, text, ids)


async def _touch_mission(env: Path, mission_id: str) -> None:
    """A fresh event on the mission: the running mission is genuinely working."""
    store = await _fresh_store(env)
    try:
        await store.append_event(EventRecord(mission_id=mission_id, payload={"note": "tick"}))
    finally:
        await store.aclose()


def _row_mark(app: ReworkApp, mission_id: str) -> str:
    """The dashboard row mark of a running mission, rendered with a fixed
    animation frame so a live and a frozen mark cannot be confused."""
    dash = _dash(app)
    mission = app.hub.snapshot.mission(mission_id)
    return dash._row_mark(mission, "running", FRAMES[3], time.time())


def _tile(app: ReworkApp) -> str:
    """The dashboard running tile, painted now with a fixed frame."""
    dash = _dash(app)
    dash._paint_running_tile(FRAMES[3], time.time())
    tile = dash.query_one("#st-running", Stat)
    return f"{tile._value}|{tile._sub}"


async def test_spinner_idle_while_offline(
    env: Path, ids: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # the seeded running mission waits on a gate, so file it under "running"
    # for the tile (the row mark does not depend on the section)
    monkeypatch.setattr(dashboard_view, "section_for", lambda m, snap: "running")
    async with _running(env) as (app, pilot, factory):
        mid = ids["running"]
        animated = run_mark(False, FRAMES[3])
        idle = run_mark(True)
        assert IDLE_GLYPH in idle and FRAMES[3] in animated

        # the link is up and the mission is publishing: it animates
        await _touch_mission(env, mid)
        await _tick(app, pilot)
        fresh = app.hub.snapshot.last_event.get(mid)
        assert fresh is not None and not spinner.is_stalled(time.time(), fresh)
        row = _row_mark(app, mid)
        assert row == animated
        assert "working" in _tile(app)

        # the link drops: the stamp cannot be read, and that is NOT "working"
        await _go_offline(app, pilot, factory)
        assert app.hub.snapshot.last_event.get(mid) is None, "an unreadable stamp is unknown"
        row = _row_mark(app, mid)
        assert row == idle, "an offline running mission shows the idle glyph"
        assert FRAMES[3] not in row
        assert "working" not in _tile(app) and "idle" in _tile(app)

        # the link is back and the stamp is fresh again: it animates again
        await _recover(app, pilot, factory)
        assert app.hub.snapshot.online is True
        assert app.hub.snapshot.last_event.get(mid) == fresh
        row = _row_mark(app, mid)
        assert row == animated
        assert "working" in _tile(app)


async def test_work_card_idle_while_offline(env: Path, ids: dict[str, str]) -> None:
    async with _running(env) as (app, pilot, factory):
        mid = ids["running"]
        await _touch_mission(env, mid)
        await _tick(app, pilot)
        work = _work(app)
        card = MissionCard(mid)
        await work.mount(card)
        card.update_from(app.hub.snapshot.mission(mid), [])
        await pilot.pause()

        def head() -> str:
            return str(card.query_one(".mc-head", Static).render())

        def paint() -> None:
            work.on_tick(app.hub.snapshot)
            work.tick(FRAMES[3], time.time())

        paint()
        assert "working" in head() and FRAMES[3] in head()
        await _go_offline(app, pilot, factory)
        paint()
        assert "working" not in head() and FRAMES[3] not in head()
        assert IDLE_GLYPH in head()
        await _recover(app, pilot, factory)
        paint()
        assert "working" in head() and FRAMES[3] in head()
