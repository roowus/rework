"""What the TUI logs (and does not log) while the app is tearing down.

Teardown removes the app's children before it closes the store, so a worker
resuming after its data awaits finds an empty view: `query_one` raises
`NoMatches`, the worker dies with a WorkerFailed traceback, and the hub's
broadcast turns the same race into a spurious "offline:" warning plus a "hub
listener failed" error — records the owner sees in `rework logs tui` after a
clean quit (m7 findings #19; the toast-log flake: 9 of 101 runs).

Both guards are deliberately narrow: the paint guard swallows ONLY NoMatches,
and the hub still logs a genuine listener failure when it is not closing, so a
live bug cannot hide behind a teardown fix."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import pytest
from textual.app import App, ComposeResult
from textual.css.query import NoMatches

from rework import logs
from rework.tui.hub import Hub
from rework.tui.views.base import View
from rework.tui.views.dashboard import DashboardView


class ViewApp(App[None]):
    """A bare app mounting one view, so a paint can run against a real DOM."""

    def __init__(self, view: View) -> None:
        super().__init__()
        self._view = view

    def compose(self) -> ComposeResult:
        yield self._view


class BareView(View):
    TAB = "bare"

    def compose(self) -> ComposeResult:
        return iter(())


class FakeHub:
    """Just the two things DashboardView._enrich touches: data + guard."""

    def __init__(self, data: FakeData) -> None:
        self.data = data

    async def guard(self, call: Any, default: Any) -> Any:
        try:
            return await call()
        except Exception:
            return default


class BareDashboard(DashboardView):
    """The real dashboard, mounted with no children (which is what teardown
    leaves behind) and a fake hub (no store needed)."""

    def __init__(self, data: FakeData) -> None:
        super().__init__()
        self._fake = FakeHub(data)

    def compose(self) -> ComposeResult:
        return iter(())  # teardown already removed the children

    def _paint_head(self) -> None:
        pass  # the real dashboard's on_mount paints the breadcrumb it lacks

    @property
    def hub(self) -> Any:
        return self._fake


class FakeData:
    """An empty store: every query answers "nothing yet"."""

    def __init__(self) -> None:
        self._reconnecting_hint = ""
        self.closed = False

    async def missions(self, limit: int = 60) -> list[Any]:
        return []

    async def open_gates(self) -> list[Any]:
        return []

    async def projects(self) -> list[Any]:
        return []

    async def chats(self, limit: int = 200) -> list[Any]:
        return []

    async def spend_history(
        self, bucket: str = "total", since: float | None = None, group_by: str | None = None
    ) -> list[Any]:
        return []

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def tui_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("REWORK_LOG_DIR", str(tmp_path / "logs"))
    root = logging.getLogger()
    saved = list(root.handlers)
    logs._configured.discard("tui")
    path = logs.setup_logging("tui")
    yield path
    for handler in list(root.handlers):
        if handler not in saved:
            root.removeHandler(handler)
            handler.close()
    logs._configured.discard("tui")


def _records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _loud(path: Path) -> list[dict[str, Any]]:
    return [r for r in _records(path) if r.get("level") in ("warning", "error", "critical")]


# -- the hub: no false outage while closing -----------------------------------


async def test_a_refresh_after_aclose_returns_quietly(tui_log: Path) -> None:
    """A heartbeat tick that lands after the close polls nothing and says
    nothing (before: the query died on the closed store and tui.log got an
    "offline:" warning for a clean quit)."""
    data = FakeData()
    hub = Hub(data)  # type: ignore[arg-type]
    before = await hub.refresh()
    assert before.loaded
    await hub.aclose()
    assert data.closed

    after = await hub.refresh()
    assert after is before, "a closed hub returns its last snapshot untouched"
    assert _loud(tui_log) == [], _loud(tui_log)


async def test_a_refresh_in_flight_when_teardown_starts_is_not_an_outage(tui_log: Path) -> None:
    """The race behind the flake: a refresh is suspended in its query when
    aclose() runs; the query resumes on the closed store. That must not be
    logged as an outage nor broadcast into the half-torn-down DOM."""
    started = asyncio.Event()
    release = asyncio.Event()

    class Slow(FakeData):
        async def missions(self, limit: int = 60) -> list[Any]:
            started.set()
            await release.wait()
            raise RuntimeError("Cannot operate on a closed database.")

    hub = Hub(Slow())  # type: ignore[arg-type]
    seen: list[Any] = []
    hub.subscribe(seen.append)

    task = asyncio.create_task(hub.refresh())
    await started.wait()
    await hub.aclose()  # teardown marks the hub, then yields on the store
    release.set()
    snap = await task

    assert snap is hub.snapshot, "a refresh that lost its race keeps the old snapshot"
    assert seen == [], "nothing is broadcast into a closing app"
    assert _loud(tui_log) == [], _loud(tui_log)


async def test_a_listener_raising_no_matches_while_closing_logs_nothing(tui_log: Path) -> None:
    """Teardown removes the children the listeners paint into, so NoMatches is
    expected then — and only then."""
    hub = Hub(FakeData())  # type: ignore[arg-type]
    hub._closing = True

    def listener(_: Any) -> None:
        raise NoMatches("No nodes match 'MenuBar'")

    hub.subscribe(listener)
    hub._broadcast()
    assert _loud(tui_log) == [], _loud(tui_log)


async def test_a_genuine_listener_failure_still_logs_an_error(tui_log: Path) -> None:
    """The live app keeps its traceback: a broken view is a bug, not a
    teardown."""
    hub = Hub(FakeData())  # type: ignore[arg-type]

    def listener(_: Any) -> None:
        raise RuntimeError("a broken listener")

    hub.subscribe(listener)
    hub._broadcast()

    errors = [r for r in _records(tui_log) if r.get("level") == "error"]
    assert [r["msg"] for r in errors] == ["hub listener failed"], errors
    assert "RuntimeError: a broken listener" in errors[0]["exc"], errors[0]


async def test_a_listener_raising_no_matches_while_live_still_logs_an_error(tui_log: Path) -> None:
    """NoMatches is only excused during teardown; while the app runs it is a
    real bug (a listener querying the wrong node) and keeps its error."""
    hub = Hub(FakeData())  # type: ignore[arg-type]

    def listener(_: Any) -> None:
        raise NoMatches("No nodes match '#typo'")

    hub.subscribe(listener)
    hub._broadcast()

    errors = [r for r in _records(tui_log) if r.get("level") == "error"]
    assert [r["msg"] for r in errors] == ["hub listener failed"], errors


# -- the views: a paint into a torn-down DOM -----------------------------------


async def test_a_paint_into_a_missing_node_returns_quietly() -> None:
    """The shape of every guarded worker paint (await data, then paint)."""
    view = BareView()
    async with ViewApp(view).run_test() as pilot:
        await pilot.pause()
        with pytest.raises(NoMatches):
            view.query_one("#gone")  # the negative control: it does raise
        with view.painting():
            view.query_one("#gone")  # guarded: no raise


async def test_a_genuine_error_in_the_paint_still_propagates() -> None:
    """The guard is narrow: only NoMatches is swallowed, so a real bug in a
    paint is not hidden behind the teardown fix."""
    view = BareView()
    async with ViewApp(view).run_test() as pilot:
        await pilot.pause()
        with pytest.raises(RuntimeError, match="a real painting bug"):
            with view.painting():
                raise RuntimeError("a real painting bug")


async def test_a_worker_that_paints_into_a_missing_node_completes() -> None:
    """End to end through a Textual worker, the way the views run it: unguarded
    this exits the worker with WorkerFailed (whose traceback lands in tui.log
    and, with exit_on_error, in the app), guarded it finishes."""
    view = BareView()
    app = ViewApp(view)
    async with app.run_test() as pilot:
        await pilot.pause()

        async def load_then_paint() -> None:
            await asyncio.sleep(0)  # the data await
            with view.painting():
                view.query_one("#gone")

        worker = app.run_worker(load_then_paint())
        await worker.wait()
        assert worker.is_finished
        assert worker.error is None, worker.error


async def test_dashboard_enrich_paint_on_a_view_without_children_returns(
    tui_log: Path,
) -> None:
    """`_enrich` awaits cost queries and then paints the stats strip; if the
    app closed meanwhile, '#st-running' is gone. The paint returns instead of
    killing the worker (this is the exact NoMatches the m7 flake reported)."""
    view = BareDashboard(FakeData())
    app = ViewApp(view)
    async with app.run_test() as pilot:
        await pilot.pause()
        with pytest.raises(NoMatches):
            view._paint_overview()  # the negative control: unguarded it raises

        before = len(_records(tui_log))
        await view._enrich().wait()
        assert _loud(tui_log)[before:] == [], "the guarded paint logged nothing"
