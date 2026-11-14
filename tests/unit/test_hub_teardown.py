"""Teardown is not an outage: a poll that resumes after aclose() must not log.

Textual's `run_test` teardown closes the app (hence the store) but never joins
the app's workers, so a refresh suspended in a data await resumes with the
store already closed. That used to be reported as `offline: Cannot operate on a
closed database.` plus a `hub listener failed` traceback ending in
`NoMatches: No nodes match 'MenuBar'` — one extra record on disk 0.6-1 s into
teardown, which the toast tests read as a second error for one failure."""

from __future__ import annotations

import asyncio
import logging

import pytest
from textual.css.query import NoMatches

from rework.tui.hub import Hub, Snapshot


class FakeData:
    """Misses in exactly the way a closed store does."""

    _reconnecting_hint = ""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.calls = 0

    async def missions(self, limit: int = 60) -> list[object]:
        self.calls += 1
        await self.release.wait()  # teardown happens here
        raise RuntimeError("Cannot operate on a closed database.")

    async def aclose(self) -> None:
        pass


async def test_a_refresh_that_resumes_after_aclose_logs_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    data = FakeData()
    hub = Hub(data)  # type: ignore[arg-type]
    seen: list[Snapshot] = []
    hub.subscribe(seen.append)
    caplog.set_level(logging.DEBUG, logger="rework.tui")

    task = asyncio.create_task(hub.refresh())
    await asyncio.sleep(0)  # the poll is now suspended inside missions()
    await hub.aclose()
    data.release.set()
    assert await task is hub.snapshot

    assert data.calls == 1, "the poll did run and then resumed"
    assert seen == [], "nothing is broadcast into a tearing-down app"
    assert hub.snapshot.error == "", "a closed store is not an outage"
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING], caplog.records


async def test_refresh_after_aclose_does_not_query_at_all() -> None:
    data = FakeData()
    data.release.set()
    hub = Hub(data)  # type: ignore[arg-type]
    await hub.aclose()
    await hub.refresh()
    assert data.calls == 0


async def test_guard_after_aclose_degrades_silently(caplog: pytest.LogCaptureFixture) -> None:
    hub = Hub(FakeData())  # type: ignore[arg-type]
    await hub.aclose()
    caplog.set_level(logging.DEBUG, logger="rework.tui")

    async def boom() -> None:
        raise RuntimeError("Cannot operate on a closed database.")

    assert await hub.guard(boom, "default") == "default"
    assert hub.snapshot.online, "a detail query dying at teardown is not 'offline'"
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING], caplog.records


def _no_callback(_snap: Snapshot) -> None:
    raise NoMatches("No nodes match 'MenuBar' on Screen(id='_default')")


def _broken_callback(_snap: Snapshot) -> None:
    raise RuntimeError("a real view bug")


def test_a_listener_losing_its_widget_at_teardown_is_not_an_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    hub = Hub(FakeData())  # type: ignore[arg-type]
    hub.subscribe(_no_callback)
    hub._closing = True
    caplog.set_level(logging.DEBUG, logger="rework.tui")
    hub._broadcast()
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING], caplog.records


def test_a_live_nomatches_is_still_a_bug(caplog: pytest.LogCaptureFixture) -> None:
    """Negative control for the guard above: NOT closing means the selector is
    wrong, and that must keep its traceback."""
    hub = Hub(FakeData())  # type: ignore[arg-type]
    hub.subscribe(_no_callback)
    caplog.set_level(logging.DEBUG, logger="rework.tui")
    hub._broadcast()
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert [r.getMessage() for r in errors] == ["hub listener failed"], caplog.records


def test_a_closing_listener_raising_something_else_still_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Only NoMatches is excused by teardown; an unexpected error is not."""
    hub = Hub(FakeData())  # type: ignore[arg-type]
    hub.subscribe(_broken_callback)
    hub._closing = True
    caplog.set_level(logging.DEBUG, logger="rework.tui")
    hub._broadcast()
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR] == [
        "hub listener failed"
    ], caplog.records
