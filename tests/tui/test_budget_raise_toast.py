"""The budget-raise toast fires ONCE per raise.

Found live: a raise nobody notices is a raise the policy wasted, so the shell
toasts `budget_raised` events. It tails them through a seq cursor
(`Hub._budget_seq` -> `TuiData.events_after` -> `StateStore.events_since`, the
same mechanism the gate toasts use via `Snapshot.gates` + `_seen_gates`), so a
raise announced on one poll must not be announced again on the next — the
owner's whole complaint was being told the same thing every two seconds.

These tests drive the real Hub over a real (tmp) sqlite store: the failure
mode they pin cannot be seen from a fake data layer, because it lives in the
cursor argument that crosses Hub -> TuiData -> the store query.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from rework.kernel.state.records import EventRecord, EventType, Mission
from rework.tui.app import ReworkApp
from rework.tui.hub import Hub

SIZE = (140, 42)


async def _store(db: Path) -> Any:
    from rework.kernel.state.sqlite import open_sqlite

    return await open_sqlite(str(db))


async def _seed(db: Path) -> None:
    store = await _store(db)
    try:
        await store.create_mission(
            Mission(
                id="m-raise0001",
                objective="raise the budget once",
                plan={"budget": {"max_usd": 2.0, "original_max_usd": 2.0}},
            )
        )
    finally:
        await store.aclose()


async def _raise(db: Path, old: float, new: float, reason: str = "one more coder") -> None:
    store = await _store(db)
    try:
        await store.append_event(
            EventRecord(
                mission_id="m-raise0001",
                type=EventType.BUDGET_RAISED,
                payload={"old": old, "new": new, "reason": reason, "auto": True},
            )
        )
    finally:
        await store.aclose()


async def test_a_raise_appears_in_exactly_one_snapshot(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """THE test: one BUDGET_RAISED row, two consecutive refreshes. The first
    snapshot (primed to "now") carries nothing, the second carries the raise —
    and the third, over the same store, carries NOTHING. Without the cursor the
    third re-includes the row and the owner is toasted on every poll."""
    db = tmp_path / "hub.sqlite3"
    monkeypatch.setenv("REWORK_DB", str(db))
    await _seed(db)

    from rework.tui.data import TuiData

    data = TuiData()
    hub = Hub(data)
    try:
        first = await hub.refresh()
        assert first.budget_raises == [], "a fresh shell must not announce old raises"

        await _raise(db, 2.0, 4.0)
        second = await hub.refresh()
        assert [e.payload["new"] for e in second.budget_raises] == [4.0]

        third = await hub.refresh()
        assert third.budget_raises == [], "the same raise was toasted twice"
    finally:
        await data.aclose()


async def test_the_cursor_advances_over_the_whole_batch(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A burst of ordinary events between two raises must not starve the tail:
    the cursor moves past the batch, and the NEXT raise still gets its one
    toast. (Advancing only to the last BUDGET_RAISED would also work; not
    advancing at all is the bug this pins.)"""
    db = tmp_path / "hub.sqlite3"
    monkeypatch.setenv("REWORK_DB", str(db))
    await _seed(db)

    from rework.tui.data import TuiData

    data = TuiData()
    hub = Hub(data)
    try:
        await hub.refresh()  # prime to "now"
        await _raise(db, 2.0, 3.0)
        assert [e.payload["new"] for e in (await hub.refresh()).budget_raises] == [3.0]
        assert (await hub.refresh()).budget_raises == []

        store = await _store(db)
        try:
            for i in range(3):
                await store.append_event(
                    EventRecord(
                        mission_id="m-raise0001",
                        type=EventType.INFO,
                        payload={"i": i},
                    )
                )
        finally:
            await store.aclose()
        await _raise(db, 3.0, 4.0)
        assert [e.payload["new"] for e in (await hub.refresh()).budget_raises] == [4.0]
        assert (await hub.refresh()).budget_raises == []
    finally:
        await data.aclose()


async def test_the_shell_toasts_one_raise_once(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The point of the whole path: the toast. The app polls every 0.3s, so a
    raise left in the window is re-toasted several times in a couple of
    seconds; this asserts exactly one `Budget $2.00 -> $4.00 (auto)` toast
    across many polls."""
    db = tmp_path / "tui.sqlite3"
    monkeypatch.setenv("REWORK_DB", str(db))
    monkeypatch.setenv("REWORK_LIBRARY_DIR", str(tmp_path / "lib"))
    monkeypatch.setenv("REWORK_RESULTS_DIR", str(tmp_path / "results"))
    monkeypatch.setenv("REWORK_REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("REWORK_TUI_SETTINGS", str(tmp_path / "tui.json"))
    monkeypatch.setenv("REWORK_NOTIFY", "toast")
    await _seed(db)

    def budget_toasts(app: ReworkApp) -> list[str]:
        return [text for channel, text in app.notifications_sent if "Budget $" in text]

    app = ReworkApp(poll_seconds=0.3)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.8)  # the first poll primes the cursor
        assert budget_toasts(app) == []
        await _raise(db, 2.0, 4.0)
        await pilot.pause(1.6)  # several polls over the same event window
        assert budget_toasts(app) == ["Budget $2.00 -> $4.00 (auto)"]
