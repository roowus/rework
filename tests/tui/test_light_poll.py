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
    app = ReworkApp(poll_seconds=0.3)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.6)
        await pilot.press("alt+2")
        await pilot.pause(0.5)
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
        await pilot.pause(0.3)
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

    app = ReworkApp(poll_seconds=0.3)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.6)
        dash = app.view("dashboard")
        assert dash.snap.mission(mid).light, "the poll itself is light"
        app.open_mission(mid)
        await pilot.pause(1.0)
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
    app = ReworkApp(poll_seconds=0.2)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.5)
        calls: list[str] = []
        data = app.hub.data
        real = data.get_mission

        async def counting(mission_id: str) -> Any:
            calls.append(mission_id)
            return await real(mission_id)

        data.get_mission = counting  # type: ignore[method-assign]
        app.open_mission(mid)
        await pilot.pause(2.0)  # many polls while the mission is open
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


async def test_idle_owner_is_polled_less_often_and_a_key_resets_it(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = ReworkApp(poll_seconds=0.2)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.5)
        assert app.poll_interval() == 0.2, "active owner: the normal rate"

        refreshes: list[float] = []
        real = app.hub.refresh

        async def counting() -> Any:
            refreshes.append(time.monotonic())
            return await real()

        app.hub.refresh = counting  # type: ignore[method-assign]
        app._last_input = time.monotonic() - 3600  # away for an hour
        app._last_poll = time.monotonic()
        assert app.poll_interval() == app_mod.IDLE_POLL_SECONDS > app.poll_seconds
        await pilot.pause(1.5)  # ~7 timer ticks
        assert refreshes == [], "idle: the ticks skip the poll"

        await pilot.press("a")  # the owner is back
        await pilot.pause(0.5)
        assert app.poll_interval() == 0.2
        assert len(refreshes) >= 2, "a key refreshes at once and returns to the fast rate"


async def test_idle_poll_never_beats_a_slower_configured_rate(env: Path) -> None:
    app = ReworkApp(poll_seconds=30.0)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.2)
        app._last_input = time.monotonic() - 3600
        assert app.poll_interval() == 30.0


# -- a failed full-row fetch does not leave the header on "loading" -----------


async def test_a_failed_full_fetch_shows_the_light_facts_and_recovers(
    ids: dict[str, str],
) -> None:
    from textual.widgets import Static

    mid = ids["running"]
    app = ReworkApp(poll_seconds=0.2)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.5)
        data = app.hub.data
        real = data.get_mission
        broken = True

        async def flaky(mission_id: str) -> Any:
            if broken:
                raise RuntimeError("store unreachable")
            return await real(mission_id)

        data.get_mission = flaky  # type: ignore[method-assign]
        app.open_mission(mid)
        header = app.query_one("#m-header", Static)
        dash = app.view("dashboard")
        row = dash.snap.mission(mid)
        for _ in range(30):
            await pilot.pause(0.1)
            if "could not load" in str(header.render()):
                break
        text = str(header.render())
        assert "loading" not in text, text
        assert "could not load plan/result" in text, text
        assert row.objective in text, "the light objective is shown"
        assert "started" in text
        # the store comes back: the next poll fetches the row and the header is full
        broken = False
        for _ in range(40):
            await pilot.pause(0.1)
            if "could not load" not in str(header.render()):
                break
        assert dash.full_mission is not None and not dash.full_failed
        assert "could not load" not in str(header.render())
        assert "running  fix" in str(header.render()), "the plan label is back"
