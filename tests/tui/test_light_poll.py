"""The TUI poll carries slim missions (no plan, no result), views fetch the full
row on demand, an unchanged poll does not repaint, and an idle owner is polled
less often. Real app against a tmp SQLite store, plus a bare Hub with a fake."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from seed import seed

from rework.kernel.state.records import Mission, MissionStatus
from rework.tui import app as app_mod
from rework.tui.app import ReworkApp
from rework.tui.hub import Hub
from rework.tui.widgets.base import RowList

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


# -- the snapshot is light, the detail is full --------------------------------


async def test_snapshot_rows_are_light_and_the_list_still_works(ids: dict[str, str]) -> None:
    app = ReworkApp(poll_seconds=3600.0)
    async with app.run_test(size=SIZE) as pilot:
        await _settle(app, pilot)
        await pilot.press("alt+2")
        await _settle(app, pilot)
        dash = app.view("dashboard")
        rows = dash.snap.missions
        assert rows and all(m.light and m.plan is None and m.result is None for m in rows)
        # grouping and filtering run off the light rows
        lst = app.query_one("#mission-list", RowList)
        assert ids["running"] in lst.keys and ids["waiting"] in lst.keys
        # stats strip: counts by status and recent updates
        by_status = {s: sum(1 for m in rows if m.status.value == s) for s in ("running",)}
        assert by_status["running"] >= 1
        # search reads id/title/objective/project_id/status only
        dash.query_text = "flaky"
        dash._apply_search()
        await _settle(app, pilot)
        assert lst.selectable == [ids["running"]]
        assert not app.swallowed


async def test_drill_down_and_orchestrator_output_load_the_full_row(
    ids: dict[str, str], env: Path
) -> None:
    from textual.widgets import Static

    mid = ids["running"]
    store = await _store(env)
    try:
        await store.update_mission(
            mid,
            plan={"kind": "fix", "orchestrator": {"model": "pro"}, "model": "flash"},
            result={"summary": "THE FINAL SUMMARY"},
        )
    finally:
        await store.aclose()

    app = ReworkApp(poll_seconds=3600.0)
    async with app.run_test(size=SIZE) as pilot:
        await _settle(app, pilot)
        dash = app.view("dashboard")
        assert dash.snap.mission(mid).light, "the poll itself is light"
        app.open_mission(mid)
        await _settle(app, pilot)
        full = dash.full_mission
        assert full is not None and not full.light and full.id == mid
        assert full.plan["orchestrator"] == {"model": "pro"}
        # plan reads: the header (orchestrator model, roles) and the pseudo run id
        header = str(app.query_one("#m-header", Static).render())
        assert "orchestrator pro" in header and "roles flash" in header, header
        assert dash._orchestrator_id() == f"{mid}:orchestrate:orchestrator:1"
        # the only result read in the TUI
        assert "THE FINAL SUMMARY" in dash._orchestrator_output(dash._detail_mission())
        assert not app.swallowed


async def test_the_full_row_is_fetched_once_not_per_poll(ids: dict[str, str]) -> None:
    mid = ids["running"]
    app = ReworkApp(poll_seconds=3600.0)  # the app's own timer never fires; the test polls
    async with app.run_test(size=SIZE) as pilot:
        await _settle(app, pilot)
        calls: list[str] = []
        data = app.hub.data
        real = data.get_mission

        async def counting(mission_id: str) -> Any:
            calls.append(mission_id)
            return await real(mission_id)

        data.get_mission = counting  # type: ignore[method-assign]
        app.open_mission(mid)
        for _ in range(5):  # five polls while the mission is open
            await _settle(app, pilot)
            await app.hub.refresh()
        await _settle(app, pilot)
        assert calls.count(mid) == 1, calls


# -- repaint gate -------------------------------------------------------------


class FakeData:
    _reconnecting_hint = ""

    def __init__(self) -> None:
        self.rows: list[Mission] = [Mission(id="m-1", objective="a", light=True)]
        self.spend = 0.0

    async def missions(self, limit: int = 200) -> list[Mission]:
        return list(self.rows)

    async def open_gates(self) -> list[Any]:
        return []

    async def projects(self) -> list[Any]:
        return []

    async def chats(self, limit: int = 200) -> list[Any]:
        return []

    async def spend_history(self, bucket: str = "total", since: float = 0.0) -> list[Any]:
        return [{"cost_usd": self.spend}]

    async def aclose(self) -> None:
        pass


async def test_an_unchanged_snapshot_does_not_repaint() -> None:
    data = FakeData()
    hub = Hub(data)  # type: ignore[arg-type]
    painted: list[Any] = []
    ticked: list[Any] = []
    hub.subscribe(painted.append)
    hub.subscribe_tick(ticked.append)

    await hub.refresh()
    assert len(painted) == 1 and not ticked, "the first refresh always paints (loaded flip)"
    assert hub.snapshot.loaded

    await hub.refresh()
    await hub.refresh()
    assert len(painted) == 1, "same data: no repaint"
    assert len(ticked) == 2, "but live detail polls still get their tick"

    data.rows[0] = Mission(id="m-1", objective="a", status=MissionStatus.RUNNING, light=True)
    await hub.refresh()
    assert len(painted) == 2, "a changed row repaints"
    data.spend = 1.5
    await hub.refresh()
    assert len(painted) == 3, "so does spend"


async def test_going_offline_and_back_repaints() -> None:
    data = FakeData()
    hub = Hub(data)  # type: ignore[arg-type]
    painted: list[Any] = []
    hub.subscribe(painted.append)
    await hub.refresh()

    async def boom(limit: int = 200) -> list[Mission]:
        raise RuntimeError("down")

    good = data.missions
    data.missions = boom  # type: ignore[method-assign]
    await hub.refresh()
    assert len(painted) == 2 and painted[-1].online is False
    hub._next_try = 0.0  # skip the offline backoff wait
    data.missions = good  # type: ignore[method-assign]
    await hub.refresh()
    assert len(painted) == 3 and painted[-1].online is True


# -- idle backoff -------------------------------------------------------------
#
# The arithmetic, with poll_seconds = 2 and IDLE_POLL_SECONDS = 10: the timer
# ticks every 2 s. Active: every tick polls (2 s). Idle (no key for 30 s): a tick
# polls once 10 s have passed since the last POLL (not the last tick), so every
# 5th tick, 10 s apart, 6 polls a minute. A key polls at once and goes back to 2 s.


async def _settle(app: ReworkApp, pilot: Any) -> None:
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


def test_poll_due_arithmetic() -> None:
    app = ReworkApp(poll_seconds=2.0)
    app._last_poll = 100.0
    app.idle_for_reload = lambda: False  # type: ignore[method-assign]
    assert app.poll_interval() == 2.0
    assert not app.poll_due(101.7) and app.poll_due(101.81) and app.poll_due(102.0)
    app.idle_for_reload = lambda: True  # type: ignore[method-assign]
    assert app.poll_interval() == app_mod.IDLE_POLL_SECONDS == 10.0
    assert not app.poll_due(108.0), "one tick before: not due"
    assert not app.poll_due(109.7) and app.poll_due(109.81) and app.poll_due(110.0)
    # a configured rate slower than the idle one is never sped up
    slow = ReworkApp(poll_seconds=30.0)
    slow.idle_for_reload = lambda: True  # type: ignore[method-assign]
    assert slow.poll_interval() == 30.0
    # the first poll is always due
    fresh = ReworkApp(poll_seconds=2.0)
    fresh.idle_for_reload = lambda: False  # type: ignore[method-assign]
    assert fresh.poll_due(0.0), "no poll yet: the first is due at any clock value"


async def test_an_idle_app_keeps_polling_at_the_slow_rate_and_a_key_resumes_the_fast_one(
    env: Path,
) -> None:
    app = ReworkApp(poll_seconds=3600.0)  # the real timer never fires: the test is the clock
    async with app.run_test(size=SIZE) as pilot:
        await _settle(app, pilot)
        app.poll_seconds = 2.0
        refreshes: list[int] = []
        real = app.hub.refresh

        async def counting() -> Any:
            refreshes.append(1)
            return await real()

        app.hub.refresh = counting  # type: ignore[method-assign]
        app._last_input = time.monotonic() - 3600  # away for an hour
        assert app.poll_interval() == 10.0
        t0 = 1_000_000.0
        app._last_poll = t0

        def run_ticks(seconds: int, *, jitter: float = 0.0) -> list[float]:
            """Timer ticks 2 s apart (each off by +/- jitter); returns poll times."""
            polled: list[float] = []
            for k in range(1, seconds // 2 + 1):
                before = app._last_poll
                app._kick(now=t0 + 2 * k + (jitter if k % 2 else -jitter))
                if app._last_poll != before:
                    polled.append(app._last_poll)
            return polled

        polled = run_ticks(60)
        await _settle(app, pilot)
        assert polled == [t0 + 10 * i for i in range(1, 7)], polled
        assert len(refreshes) == 6, "idle: 6 polls in a minute, one every 10 s"

        # timer jitter does not push a poll a whole tick late (10 s, not 12 s)
        app._last_poll = t0
        polled = run_ticks(60, jitter=0.05)
        await _settle(app, pilot)
        gaps = [b - a for a, b in zip([t0, *polled], polled, strict=False)]
        assert len(polled) == 6 and all(9.8 <= g <= 10.2 for g in gaps), gaps

        # the owner is back: a key polls AT ONCE and restores the fast rate
        app._last_poll = time.monotonic()  # polled a moment ago: nothing is due
        refreshes.clear()
        await pilot.press("a")
        await _settle(app, pilot)
        assert len(refreshes) == 1, "a key after idle polls immediately"
        assert app.poll_interval() == 2.0
        now = app._last_poll
        app._kick(now=now + 1.0)
        assert app._last_poll == now, "active: one tick later is not due"
        app._kick(now=now + 2.0)
        assert app._last_poll == now + 2.0, "active: every 2 s tick polls"
        await _settle(app, pilot)
        assert len(refreshes) == 2


async def test_a_skipped_tick_never_moves_the_poll_stamp(env: Path) -> None:
    app = ReworkApp(poll_seconds=3600.0)
    async with app.run_test(size=SIZE) as pilot:
        await _settle(app, pilot)
        app.poll_seconds = 2.0
        app._last_input = time.monotonic() - 3600
        app._last_poll = 50.0
        for now in (52.0, 54.0, 56.0, 58.0):
            app._kick(now=now)
            assert app._last_poll == 50.0
        app._kick(now=60.0)
        assert app._last_poll == 60.0


async def test_a_tick_during_teardown_is_quiet(env: Path, caplog: pytest.LogCaptureFixture) -> None:
    from textual.app import ScreenStackError
    from textual.css.query import NoMatches

    app = ReworkApp(poll_seconds=3600.0)
    async with app.run_test(size=SIZE) as pilot:
        await _settle(app, pilot)
        app._last_poll = float("-inf")

        # the screens are gone: debug log, no poll, no error
        def gone(now: float) -> bool:
            raise ScreenStackError("No screens on stack")

        app.poll_due = gone  # type: ignore[method-assign]
        with caplog.at_level("DEBUG", logger="rework.tui"):
            app._kick()
        assert app._last_poll == float("-inf")
        assert any("no screens" in r.getMessage() for r in caplog.records)

        # a real NoMatches (the Work view is missing) is NOT swallowed
        def missing(now: float) -> bool:
            raise NoMatches("no #view-work")

        app.poll_due = missing  # type: ignore[method-assign]
        with pytest.raises(NoMatches):
            app._kick()
        del app.poll_due
        # the shutdown flag stops a tick before it reads any screen
        app._closing = True
        app._kick(force=True)
        assert app._last_poll == float("-inf")
        app._closing = False
    app._kick()  # the app has exited: not running, nothing happens, nothing raised
    assert app._last_poll == float("-inf")


# -- a failed full-row fetch does not leave the header on "loading" -----------


async def test_a_failed_full_fetch_shows_the_light_facts_backs_off_and_recovers(
    ids: dict[str, str],
) -> None:
    from textual.widgets import Static

    mid = ids["running"]
    app = ReworkApp(poll_seconds=3600.0)  # the test drives every poll
    async with app.run_test(size=SIZE) as pilot:
        await _settle(app, pilot)
        data = app.hub.data
        real = data.get_mission
        broken = True
        calls: list[str] = []

        async def flaky(mission_id: str) -> Any:
            calls.append(mission_id)
            if broken:
                raise RuntimeError("store unreachable")
            return await real(mission_id)

        data.get_mission = flaky  # type: ignore[method-assign]
        t_open = time.monotonic()
        app.open_mission(mid)
        await _settle(app, pilot)
        header = app.query_one("#m-header", Static)
        dash = app.view("dashboard")
        row = dash.snap.mission(mid)
        text = str(header.render())
        assert "loading" not in text, text
        assert "could not load plan/result" in text, text
        assert row.objective in text, "the light objective is shown"
        assert "started" in text
        assert len(calls) == 1 and dash._full_failures == 1
        assert dash._full_next_try >= t_open + 2.0, "a 2 s backoff was set"

        # inside the backoff window (held open: a loaded box must not close it)
        dash._full_next_try = time.monotonic() + 3600
        for _ in range(5):
            await app.hub.refresh()
            await _settle(app, pilot)
        assert len(calls) == 1, calls

        # the backoff expires: exactly one more attempt, and the wait grows
        dash._full_next_try = 0.0
        t_retry = time.monotonic()
        await app.hub.refresh()
        await _settle(app, pilot)
        assert len(calls) == 2 and dash._full_failures == 2
        assert dash._full_next_try >= t_retry + 4.0, "the backoff doubled"

        # the store comes back: the next attempt after the backoff recovers the header
        broken = False
        dash._full_next_try = 0.0
        await app.hub.refresh()
        await _settle(app, pilot)
        assert dash.full_mission is not None and not dash.full_failed
        assert dash._full_failures == 0
        assert "could not load" not in str(header.render())
        # the status word before the plan label is the spinner's now ("working",
        # or "idle 7m" for a silent run), so assert on the label and its
        # neighbours, not on the word "running"
        assert "fix  ·  polystats" in str(header.render()), "the plan label is back"

        # mid-session: opened fine, then the row changes and the refetch fails.
        # The stale full row stays up, now with the notice, and recovery clears it.

        broken = True
        dash.full_mission = dash.full_mission.model_copy(update={"updated_at": 1.0})
        dash._full_next_try = 0.0
        await app.hub.refresh()
        await _settle(app, pilot)
        assert dash.full_failed and not dash.full_mission.light
        stale = str(header.render())
        assert "could not load plan/result (retrying)" in stale, stale
        assert "fix  ·  polystats" in stale, "the stale plan label is still shown"
        broken = False
        dash._full_next_try = 0.0
        await app.hub.refresh()
        await _settle(app, pilot)
        assert not dash.full_failed
        assert "could not load" not in str(header.render())
