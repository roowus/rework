"""The sqlite store retries a transient write-lock error, once, at the seam.

`@retry_transient_methods` on SqliteStore is the only retry for that backend
(Postgres has the `_serialized` wrapper), so this pins the seam itself: one
public method retries a transient error, a non-transient one propagates at once.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

from rework.kernel.state.records import (
    Charge,
    Chat,
    ChatMessage,
    EventRecord,
    Mission,
    MissionStatus,
)
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


# --- the hole the audit found: a transient failure at COMMIT ------------------
#
# sqlite reports "database is locked" at COMMIT time, after the DML of the call
# has already been applied. Because `open_sqlite` opens the connection in
# autocommit (`isolation_level=None`), each `execute()` commits by itself, so
# the old seam replayed the whole method body ON TOP of the work the failed
# attempt had already stored: two chat messages, two missions, a crash count of
# 2 for one crash. The seam now runs one explicit transaction per attempt and
# rolls the aborted attempt back (see `_atomic` in sqlite.py), so a retried call
# stores its work exactly once.


class _LockedAtCommit:
    """The store's own connection, failing the FIRST commit with sqlite's
    transient class and forwarding everything else untouched.

    Commit is the honest injection point: it is where sqlite surfaces its write
    lock, and it is after the statement the old code duplicated.
    """

    def __init__(self, real: Any) -> None:
        self._real = real
        self.commits = 0
        self.fail_next = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    async def commit(self) -> None:
        self.commits += 1
        if self.fail_next:
            self.fail_next = False
            raise sqlite3.OperationalError("database is locked")
        await self._real.commit()


async def _arm(flaky: Any) -> None:
    """Fail the NEXT commit (the attempt under test), then count from zero."""
    flaky.commits = 0
    flaky.fail_next = True


async def _store_with_failing_commit(tmp_path: Path, name: str) -> tuple[SqliteStore, Any]:
    store = await open_sqlite(str(tmp_path / f"{name}.sqlite3"))
    flaky = _LockedAtCommit(store._db)
    store._db = flaky  # type: ignore[assignment]
    return store, flaky


async def test_a_transient_commit_failure_does_not_duplicate_a_chat_message(
    tmp_path: Path,
) -> None:
    store, flaky = await _store_with_failing_commit(tmp_path, "chatmsg")
    try:
        chat = await store.create_chat(Chat(id="c1", title="hi"))
        await _arm(flaky)  # fail the commit of the call under test, not create_chat's
        sent = await store.append_chat_message(
            ChatMessage(chat_id=chat.id, role="user", display="hello")
        )
        assert flaky.commits == 2, "one failed commit, then the retry's commit"
        messages = await store.chat_messages(chat.id)
        assert [m.display for m in messages] == ["hello"], "the message was stored twice"
        assert sent.id is not None
    finally:
        await store.aclose()


async def test_a_transient_commit_failure_bumps_the_crash_count_once(tmp_path: Path) -> None:
    store, flaky = await _store_with_failing_commit(tmp_path, "crashcount")
    try:
        now = time.time()
        await store.create_mission(
            Mission(id="m1", objective="crashy", created_at=now, updated_at=now)
        )
        await _arm(flaky)
        assert await store.increment_transient_crash_count("m1") == 1
        assert flaky.commits == 2
        bump = await store.get_mission("m1")
        assert bump is not None
        row = await store._fetch_one(
            "SELECT transient_crash_count FROM missions WHERE id = ?", ("m1",)
        )
        assert row is not None and row["transient_crash_count"] == 1, "one crash spent two requeues"
    finally:
        await store.aclose()


async def test_a_transient_commit_failure_creates_one_mission(tmp_path: Path) -> None:
    store, flaky = await _store_with_failing_commit(tmp_path, "createmission")
    try:
        baseline = len(await store.list_missions(limit=100))
        await _arm(flaky)
        now = time.time()
        created = await store.create_mission(
            Mission(id="m-once", objective="only once", created_at=now, updated_at=now)
        )
        assert flaky.commits == 2
        assert created.id == "m-once"
        missions = await store.list_missions(limit=100)
        assert [m.id for m in missions].count("m-once") == 1
        assert len(missions) == baseline + 1, "the failed attempt's INSERT survived the retry"
    finally:
        await store.aclose()


async def test_a_transient_commit_failure_emits_one_event(tmp_path: Path) -> None:
    store, flaky = await _store_with_failing_commit(tmp_path, "event")
    try:
        await _arm(flaky)
        event = await store.append_event(EventRecord(mission_id="m1", payload={"n": 1}))
        assert flaky.commits == 2
        stored = await store.events_since(0, mission_id="m1")
        assert len(stored) == 1, "the retried attempt stored the event twice"
        assert event.seq is not None and stored[0].seq == event.seq
    finally:
        await store.aclose()


async def test_a_transient_commit_failure_bills_one_charge(tmp_path: Path) -> None:
    store, flaky = await _store_with_failing_commit(tmp_path, "charge")
    try:
        await _arm(flaky)
        await store.charge(
            Charge(
                mission_id="m1",
                run_id="r1",
                model="fake-1",
                input_tokens=1,
                output_tokens=1,
                cost_usd=0.5,
            )
        )
        assert flaky.commits == 2
        assert len(await store.run_charges("r1")) == 1, "the owner was billed twice"
    finally:
        await store.aclose()


async def test_a_transient_commit_failure_does_not_leave_a_transaction_open(
    tmp_path: Path,
) -> None:
    """The successful attempt commits exactly once and leaves nothing open, so
    the next call starts from a clean connection."""
    store, flaky = await _store_with_failing_commit(tmp_path, "clean")
    try:
        now = time.time()
        await store.create_mission(
            Mission(id="m1", objective="clean", created_at=now, updated_at=now)
        )
        await _arm(flaky)
        await store.update_mission("m1", status=MissionStatus.PENDING)
        assert not store._db.in_transaction, "the seam left a transaction open"
        flaky.commits = 0
        await store.update_mission("m1", status=MissionStatus.RUNNING)
        assert flaky.commits == 1, "a successful call commits exactly once"
        after = await store.get_mission("m1")
        assert after is not None and after.status == MissionStatus.RUNNING
    finally:
        await store.aclose()


async def test_the_retry_rolls_the_aborted_attempt_back_between_attempts(
    tmp_path: Path,
) -> None:
    """The seam's cleanup is wired to the store's rollback: a transient failure
    must be followed by a rollback BEFORE the retry, exactly like the Postgres
    seam's `cleanup=self._end_idle_transaction`."""
    store, flaky = await _store_with_failing_commit(tmp_path, "cleanup")
    try:
        rollbacks = 0
        real_rollback = store._rollback

        async def counting_rollback() -> None:
            nonlocal rollbacks
            rollbacks += 1
            await real_rollback()

        store._rollback = counting_rollback  # type: ignore[method-assign]
        await _arm(flaky)
        event = await store.append_event(EventRecord(mission_id="m1"))
        assert rollbacks == 1, "one rollback for the one failed attempt"
        assert event.seq is not None
        assert len(await store.events_since(0, mission_id="m1")) == 1
    finally:
        await store.aclose()
