"""The sqlite store retries a transient write-lock error, once, at the seam.

`@retry_transient_methods` on SqliteStore is the only retry for that backend
(Postgres has the `_serialized` wrapper), so this pins the seam itself: one
public method retries a transient error, a non-transient one propagates at once.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from rework.kernel.state.records import Mission
from rework.kernel.state.sqlite import SqliteStore, open_sqlite


async def test_transient_lock_error_is_retried(tmp_path: Path) -> None:
    store = await open_sqlite(str(tmp_path / "retry.sqlite3"))
    try:
        now = time.time()
        created = await store.create_mission(
            Mission(id="m1", objective="retried", created_at=now, updated_at=now)
        )
        assert created.id == "m1"

        real = store._fetch_one
        calls = 0

        async def flaky(sql: str, params: tuple[object, ...]) -> sqlite3.Row | None:
            nonlocal calls
            calls += 1
            if calls == 1:  # sqlite's transient class: the write lock
                raise sqlite3.OperationalError("database is locked")
            return await real(sql, params)

        store._fetch_one = flaky  # type: ignore[method-assign]
        got = await store.get_mission("m1")
        assert got is not None and got.objective == "retried"
        assert calls == 2, "a transient error must be followed by a retry"
    finally:
        await store.aclose()


async def test_logical_error_is_not_retried(tmp_path: Path) -> None:
    assert isinstance(await open_sqlite(str(tmp_path / "noretry.sqlite3")), SqliteStore)
    store = await open_sqlite(str(tmp_path / "noretry2.sqlite3"))
    try:
        real = store._fetch_one
        calls = 0

        async def broken(sql: str, params: tuple[object, ...]) -> sqlite3.Row | None:
            nonlocal calls
            calls += 1
            raise sqlite3.OperationalError("no such column: nope")

        store._fetch_one = broken  # type: ignore[method-assign]
        with pytest.raises(sqlite3.OperationalError, match="no such column"):
            await store.get_mission("m1")
        assert calls == 1, "a logical error must propagate on the first attempt"
        store._fetch_one = real  # type: ignore[method-assign]
    finally:
        await store.aclose()
