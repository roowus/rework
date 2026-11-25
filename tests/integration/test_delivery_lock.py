"""Delivery is serialized, and both push paths merge main first (ADR 0013).

Before this, two missions that finished together merged and pushed
independently: both PRs were "clean" against the main each one had seen, and the
owner rebased one by hand. A process-local lock could not fix it, because
`rework retry-pr` runs from another process against the same database.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from rework.kernel.state.records import Mission
from rework.orchestrator.delivery import DELIVERY_HEARTBEAT_S, serialized_delivery


def _bare_origin(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True
    )
    work = tmp_path / "seed"
    work.mkdir()
    (work / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "init", "-b", "main"], cwd=work, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=work, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=r@r", "-c", "user.name=r", "commit", "-m", "init"],
        cwd=work,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", str(origin)], cwd=work, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "push", "-u", "origin", "main"], cwd=work, check=True, capture_output=True
    )
    return origin


def _branch_worktree(tmp_path: Path, origin: Path, name: str) -> Path:
    """A clone with its own branch, like a coder's worktree."""
    clone = tmp_path / f"clone-{name}"
    subprocess.run(["git", "clone", str(origin), str(clone)], check=True, capture_output=True)
    subprocess.run(
        ["git", "checkout", "-b", f"rework/{name}"], cwd=clone, check=True, capture_output=True
    )
    (clone / f"{name}.py").write_text("y = 2\n")
    subprocess.run(["git", "add", "-A"], cwd=clone, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=r@r", "-c", "user.name=r", "commit", "-m", name],
        cwd=clone,
        check=True,
        capture_output=True,
    )
    return clone


async def test_only_one_delivery_runs_at_a_time(store) -> None:
    """A second delivery waits for the first to finish."""
    events: list[str] = []

    async def deliver(tag: str, hold: float) -> None:
        async with serialized_delivery(store):
            events.append(f"{tag}:start")
            await asyncio.sleep(hold)
            events.append(f"{tag}:end")

    await asyncio.gather(deliver("a", 0.2), deliver("b", 0.05))

    # never interleaved: a's end precedes b's start
    assert events == ["a:start", "a:end", "b:start", "b:end"] or events == [
        "b:start",
        "b:end",
        "a:start",
        "a:end",
    ], events


async def test_the_lock_is_released_even_when_delivery_raises(store) -> None:
    with pytest.raises(RuntimeError):
        async with serialized_delivery(store):
            raise RuntimeError("push exploded")

    # the next delivery takes it immediately
    assert await store.acquire_delivery_lock("probe", stale_after_s=0.1) is True
    await store.release_delivery_lock("probe")


async def test_a_second_process_waits_on_the_same_lock(tmp_path: Path) -> None:
    """`rework retry-pr` from another process: the lock is in the DB, so a
    DIFFERENT store instance sees it. This is the case an asyncio.Lock cannot
    cover."""
    from rework.kernel.state.sqlite import open_sqlite

    db = str(tmp_path / "lock.sqlite3")
    store = await open_sqlite(db)
    other = await open_sqlite(db)
    try:
        assert await store.acquire_delivery_lock("daemon", stale_after_s=60.0) is True
        assert await other.acquire_delivery_lock("laptop", stale_after_s=60.0) is False
        await store.release_delivery_lock("daemon")
        assert await other.acquire_delivery_lock("laptop", stale_after_s=60.0) is True
        await other.release_delivery_lock("laptop")
    finally:
        await other.aclose()
        await store.aclose()


async def test_a_crashed_holder_does_not_block_forever(store, monkeypatch) -> None:
    """A holder that died leaves a row nobody heartbeats. The next delivery must
    take it over in a few heartbeats, not wait out the whole TTL."""
    from rework.orchestrator import delivery as delivery_mod

    # shrink the cadence so the test does not sleep for a minute
    monkeypatch.setattr(delivery_mod, "DELIVERY_HEARTBEAT_S", 0.05)
    assert await store.acquire_delivery_lock("crashed", stale_after_s=0.01) is True
    await asyncio.sleep(0.2)

    async def live() -> None:
        return None

    started = asyncio.get_event_loop().time()
    async with delivery_mod.serialized_delivery(store):
        pass
    took = asyncio.get_event_loop().time() - started
    assert took < 5, f"a dead holder must not block for {took:.1f}s"
    assert not await store.heartbeat_delivery_lock("crashed")


def _ship_fixture(tmp_path: Path) -> tuple[Path, Path]:
    """Synchronous, before the test's first await (ruff ASYNC221): an origin
    with main moved on, and a branch that left it earlier."""
    origin = _bare_origin(tmp_path)
    worktree = _branch_worktree(tmp_path, origin, "ship")

    mover = tmp_path / "mover"
    subprocess.run(["git", "clone", str(origin), str(mover)], check=True, capture_output=True)
    (mover / "new.py").write_text("z = 3\n")
    subprocess.run(["git", "add", "-A"], cwd=mover, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=r@r", "-c", "user.name=r", "commit", "-m", "later"],
        cwd=mover,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "push", "origin", "main"], cwd=mover, check=True, capture_output=True)
    return origin, worktree


async def test_the_ship_path_merges_main_before_pushing(tmp_path: Path, store) -> None:
    """`_deliver_branch` used to push without merging, so a ship-approved branch
    could arrive conflicting. It now takes the same merge+lock path as
    `deliver_pr`."""
    from rework.kernel.events.bus import InProcessEventBus
    from rework.kernel.llm.catalog import ModelCatalog
    from rework.orchestrator.engine import MissionRunner
    from rework.roles.registry import autodiscover

    origin, worktree = _ship_fixture(tmp_path)
    mission = await store.create_mission(
        Mission(id="m-ship", objective="ship", repo_origin=str(origin), repo_path=str(worktree))
    )
    # _deliver_branch touches only the store, the bus and git
    runner = MissionRunner(
        mission.id,
        store,
        InProcessEventBus(store),
        autodiscover(),
        ModelCatalog([]),
        None,  # type: ignore[arg-type]
        tmp_path / "work",
        git_author=("r", "r@r"),
    )
    await runner._deliver_branch(mission, {"code": _fake_coder_run(worktree, "rework/ship")})

    assert "later" in _log(worktree), (
        f"the ship path must merge main before pushing:\n{_log(worktree)}"
    )


def _log(worktree: Path) -> str:
    return subprocess.run(
        ["git", "log", "--oneline", "-4"], cwd=worktree, capture_output=True, text=True
    ).stdout


def _fake_coder_run(worktree: Path, branch: str) -> Any:
    """The two fields `_deliver_branch` reads off a coder run."""

    return SimpleNamespace(role="coder", artifact={"branch": branch, "worktree": str(worktree)})


@pytest.mark.parametrize("heartbeat", [DELIVERY_HEARTBEAT_S])
def test_heartbeat_interval_is_shorter_than_the_ttl(heartbeat: float) -> None:
    """A live holder must beat before another process calls it stale."""
    from rework.orchestrator.delivery import DELIVERY_LOCK_TTL_S

    assert heartbeat < DELIVERY_LOCK_TTL_S / 2
