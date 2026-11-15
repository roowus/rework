"""Shutdown is not an outage: a refresh that resumes during teardown must not
log a fake "offline", must not repaint a torn-down DOM, and a listener that
queries removed widgets must not report a bug while closing.

Found live (m7 findings #19): Textual's run_test teardown never joins the app's
workers, so a poll suspended in its queries resumes after `Hub.aclose()` — the
query dies on the closed store, the failure was logged as "offline: Cannot
operate on a closed database.", and the broadcast reached listeners whose
`query_one(MenuBar)` targets were already gone (a NoMatches traceback in
tui.log). Two different tests in tests/tui/test_toast_log.py failed across two
full runs, which is the tell that the defect was in production code."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from textual.css.query import NoMatches

from rework.tui.hub import Hub


class Blocking:
    """A data layer suspended mid-poll until the test releases it."""

    _reconnecting_hint = ""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.closing_seen: bool | None = None

    async def missions(self, limit: int = 200) -> list[Any]:
        self.started.set()
        await self.release.wait()
        return []

    async def open_gates(self) -> list[Any]:
        return []

    async def projects(self) -> list[Any]:
        return []

    async def chats(self, limit: int = 200) -> list[Any]:
        return []

    async def spend_history(self, bucket: str = "total", since: float = 0.0) -> list[Any]:
        return []

    async def aclose(self) -> None:
        pass


def _records(caplog: pytest.LogCaptureFixture, level: int) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "rework.tui" and r.levelno >= level]


async def test_a_close_sets_the_flag_before_its_first_await(caplog) -> None:  # type: ignore[no-untyped-def]
    """`aclose` must flag first: everything below it awaits, and a poll that
    resumes in that window has to see `_closing`."""
    data = Blocking()
    hub = Hub(data)  # type: ignore[arg-type]
    observed: list[bool] = []

    class Observing(Blocking):
        async def aclose(self) -> None:
            observed.append(hub._closing)

    hub.data = Observing()  # type: ignore[assignment]
    await hub.aclose()
    assert observed == [True], "the flag was set after the first await"


async def test_a_refresh_that_resumes_after_close_is_not_an_outage(caplog) -> None:  # type: ignore[no-untyped-def]
    """The real race: poll suspended in a query, teardown closes the hub, the
    query finishes. The old snapshot stays, nothing is broadcast, and nothing
    is logged as a warning or an error."""
    data = Blocking()
    hub = Hub(data)  # type: ignore[arg-type]
    hub.snapshot.at = 1.0
    hub.snapshot.error = ""
    broadcasts: list[Any] = []
    hub.subscribe(broadcasts.append)

    caplog.set_level(logging.DEBUG, logger="rework.tui")
    task = asyncio.create_task(hub.refresh())
    await data.started.wait()  # the poll is inside its awaits
    await hub.aclose()
    data.release.set()
    snap = await task

    assert snap is hub.snapshot
    assert snap.at == 1.0, "teardown must not publish a new snapshot"
    assert broadcasts == [], "nothing may be painted into a torn-down DOM"
    assert _records(caplog, logging.WARNING) == [], [
        (r.levelname, r.getMessage()) for r in _records(caplog, logging.WARNING)
    ]


async def test_a_refresh_started_after_close_is_a_noop(caplog) -> None:  # type: ignore[no-untyped-def]
    data = Blocking()
    hub = Hub(data)  # type: ignore[arg-type]
    await hub.aclose()
    caplog.set_level(logging.WARNING, logger="rework.tui")
    assert await hub.refresh() is hub.snapshot
    assert not data.started.is_set(), "no query may run after close"
    assert _records(caplog, logging.WARNING) == []


async def test_a_failed_detail_query_during_close_is_not_an_outage(caplog) -> None:  # type: ignore[no-untyped-def]
    """`guard` degrades to "offline" for a live failure — but not during
    teardown, where the store is simply gone."""

    async def boom() -> Any:
        raise RuntimeError("Cannot operate on a closed database.")

    hub = Hub()  # type: ignore[arg-type]
    caplog.set_level(logging.WARNING, logger="rework.tui")
    await hub.aclose()
    assert await hub.guard(boom, "fallback") == "fallback"
    assert hub.snapshot.online is True, "teardown must not mark the hub offline"
    assert _records(caplog, logging.WARNING) == []


async def test_a_no_matches_listener_is_excused_only_while_closing(caplog) -> None:  # type: ignore[no-untyped-def]
    """The negative control: a live NoMatches is a real bug (wrong selector)
    and must keep its traceback."""

    def missing(snapshot: Any) -> None:
        raise NoMatches("No nodes match 'MenuBar' on Screen(id='_default')")

    hub = Hub()  # type: ignore[arg-type]
    hub.subscribe(missing)
    caplog.set_level(logging.DEBUG, logger="rework.tui")

    hub._broadcast()
    live = _records(caplog, logging.ERROR)
    assert [r.getMessage() for r in live] == ["hub listener failed"]
    assert live[0].exc_info is not None, "a live NoMatches keeps its traceback"

    caplog.clear()
    hub._closing = True
    hub._broadcast()
    assert _records(caplog, logging.ERROR) == []
    assert [r.getMessage() for r in _records(caplog, logging.DEBUG)] == [
        "hub listener stopped on a closed DOM"
    ], "the torn-down query is still noted, at debug"


async def test_a_broken_listener_still_reports_while_closing(caplog) -> None:  # type: ignore[no-untyped-def]
    """Only NoMatches is excused: any other exception is a bug, closing or not,
    and a broken listener must still not starve the others."""
    seen: list[Any] = []

    def broken(snapshot: Any) -> None:
        raise RuntimeError("listener bug")

    hub = Hub()  # type: ignore[arg-type]
    hub.subscribe(broken)
    hub.subscribe(seen.append)
    caplog.set_level(logging.DEBUG, logger="rework.tui")
    hub._closing = True
    hub._broadcast()

    assert [r.getMessage() for r in _records(caplog, logging.ERROR)] == ["hub listener failed"]
    assert len(seen) == 1, "the next listener still ran"
