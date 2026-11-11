"""The daemon notices that the checkout it was started from changed (finding
28). It logs ONE warning and keeps running: a running mission must not be
dropped because a merge landed next to it, and restarting is the owner's call.
The fingerprint source is monkeypatched: no real files are touched."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

import pytest

from rework import runtime
from rework.config import Config
from rework.kernel.events.bus import InProcessEventBus
from rework.kernel.state.sqlite import open_sqlite
from rework.orchestrator.daemon import Daemon, sqlite_checkpointer_factory
from rework.runtime import CODE_CHANGED


async def _daemon(tmp_path: Any) -> Daemon:
    db = tmp_path / "state.sqlite3"
    ck = tmp_path / "checkpoints.sqlite3"
    store = await open_sqlite(str(db))
    return Daemon(
        store=store,
        bus=InProcessEventBus(store),
        registry=None,  # type: ignore[arg-type]
        catalog=None,  # type: ignore[arg-type]
        make_checkpointer=sqlite_checkpointer_factory(str(ck)),
        workroot=tmp_path / "work",
        config=Config(db=str(db), checkpoints_db=str(ck), poll_interval_s=0.01),
    )


async def test_a_code_change_is_logged_once_and_does_not_stop_the_daemon(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    values = ["a"]
    monkeypatch.setattr(runtime, "source_fingerprint", lambda: values[0])
    daemon = await _daemon(tmp_path)
    try:
        with caplog.at_level(logging.WARNING, logger="rework.daemon"):
            daemon._code = runtime.CodeChangeWatcher()
            assert daemon._code.value == "a"
            daemon._check_code()
            assert [r.message for r in caplog.records] == [], "no change, no warning"

            values[0] = "b"
            daemon._check_code()
            daemon._check_code()
            hits = [r.message for r in caplog.records if r.message == CODE_CHANGED]
            assert hits == [CODE_CHANGED], caplog.records
            assert caplog.records[-1].levelno == logging.WARNING

            # a daemon that has not started its loop yet must not blow up
            fresh = await _daemon(tmp_path)
            fresh._check_code()
            assert len([r for r in caplog.records if r.message == CODE_CHANGED]) == 1
            await fresh._store.aclose()
    finally:
        await daemon._store.aclose()


async def test_the_running_loop_warns_and_keeps_going(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The check hangs off the poll loop itself: the daemon warns and goes on
    polling (no restart, no exit)."""
    values = ["a"]
    monkeypatch.setattr(runtime, "source_fingerprint", lambda: values[0])
    daemon = await _daemon(tmp_path)
    try:
        with caplog.at_level(logging.WARNING, logger="rework.daemon"):
            task = asyncio.create_task(daemon.run_forever())
            for _ in range(50):
                if daemon._code is not None:
                    break
                await asyncio.sleep(0.01)
            assert daemon._code is not None, "the loop must watch the code on disk"
            values[0] = "b"
            for _ in range(50):
                if any(r.message == CODE_CHANGED for r in caplog.records):
                    break
                await asyncio.sleep(0.01)
            assert not task.done(), "the daemon must keep running"
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        assert [r.message for r in caplog.records if r.message == CODE_CHANGED] == [CODE_CHANGED]
    finally:
        await daemon._store.aclose()
