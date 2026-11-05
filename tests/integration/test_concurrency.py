"""M3 concurrency: two missions run at once through one store (the daemon's
job: one MissionRunner per mission, all sharing the state). Proves parallel
writers hold the seam and each runner's supervisor state stays its own."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from conftest import role_catalog
from rework.kernel.events.bus import InProcessEventBus
from rework.kernel.state.records import Mission, MissionStatus, new_id
from rework.kernel.state.sqlite import SqliteStore, open_sqlite
from rework.orchestrator.daemon import sqlite_checkpointer_factory
from rework.orchestrator.engine import MissionRunner
from rework.roles.base import RoleBuildCtx, RoleSpec
from rework.roles.registry import RoleRegistry


class SlowSpec(RoleSpec):
    """A role that takes a beat: long enough for both missions to overlap."""

    name = "slow"
    description = "test role: sleeps then completes"
    goal = "sleep on command"
    default_model = "flash"

    def build_graph(self, ctx: RoleBuildCtx):  # type: ignore[no-untyped-def]
        from langgraph.graph import END, START, StateGraph

        async def work(state: dict) -> dict:  # type: ignore[no-untyped-def]
            await asyncio.sleep(0.4)
            return {"artifact": {"kind": "slow", "mission": ctx.mission.id}}

        graph = StateGraph(dict)  # type: ignore[arg-type]
        graph.add_node("work", work)
        graph.add_edge(START, "work")
        graph.add_edge("work", END)
        return graph.compile()


@pytest.fixture
def registry() -> RoleRegistry:
    reg = RoleRegistry()
    reg.register(SlowSpec)
    return reg


@pytest.fixture
async def world(tmp_path: Path, registry: RoleRegistry) -> AsyncIterator[dict[str, Any]]:  # type: ignore[no-untyped-def]
    store: SqliteStore = await open_sqlite(str(tmp_path / "conc.sqlite3"))
    try:
        yield {"store": store, "tmp": tmp_path, "registry": registry}
    finally:
        await store.aclose()


def _runner_for(world: dict[str, Any], mission_id: str) -> MissionRunner:
    return MissionRunner(
        mission_id,
        world["store"],
        InProcessEventBus(world["store"]),
        world["registry"],
        role_catalog(),
        sqlite_checkpointer_factory(str(world["tmp"] / f"ck-{mission_id}.sqlite3")),
        world["tmp"] / f"w-{mission_id}",
        poll_interval_s=0.05,
    )


async def test_two_missions_run_concurrently(world: dict[str, Any]) -> None:
    store: SqliteStore = world["store"]
    ids = []
    for i in range(2):
        m = await store.create_mission(
            Mission(
                id=new_id("m"),
                objective=f"mission {i}",
                plan={"steps": [{"step": "do", "role": "slow"}]},
            )
        )
        ids.append(m.id)

    runners = [_runner_for(world, mid) for mid in ids]
    # launch both, then record the overlap window: if the second runner only
    # started after the first finished, the store serialized them (regression)
    started_at = asyncio.get_event_loop().time()
    tasks = [asyncio.create_task(r.run()) for r in runners]
    await asyncio.sleep(0.15)  # both must be mid-flight now (slow takes 0.4s)

    mid_flight = [await store.get_run(f"{mid}:do:slow:1") for mid in ids]
    assert all(r is not None and r.status.value in {"running", "completed"} for r in mid_flight), (
        f"missions did not overlap: {[r.status if r else None for r in mid_flight]}"
    )

    finished_at: list[float] = []

    async def _timed(task: Any) -> None:
        await asyncio.wait_for(task, timeout=10)
        finished_at.append(asyncio.get_event_loop().time())

    await asyncio.gather(*(_timed(t) for t in tasks))
    elapsed = asyncio.get_event_loop().time() - started_at
    finish_spread = max(finished_at) - min(finished_at)

    for mid in ids:
        mission = await store.get_mission(mid)
        assert mission is not None and mission.status == MissionStatus.DONE
        run = await store.get_run(f"{mid}:do:slow:1")
        assert run is not None
        assert (run.artifact or {}).get("mission") == mid  # no cross-mission bleed
    # Wall-clock bounds flake on loaded CI runners (absolute time includes
    # scheduling overhead). What actually distinguishes overlap is the SPREAD
    # between the two finishes: concurrent missions end together, serialized
    # ones are a full role-duration apart (~0.4s).
    assert elapsed < 2.0, f"both missions took absurdly long ({elapsed:.2f}s)"
    assert finish_spread < 0.30, f"missions appear serialized: finishes {finish_spread:.2f}s apart"


async def test_shared_store_survives_parallel_writes(world: dict[str, Any]) -> None:
    """Charges and events from both missions interleave without corruption."""
    store: SqliteStore = world["store"]
    ids = []
    for i in range(3):
        m = await store.create_mission(
            Mission(
                id=new_id("m"),
                objective=f"mission {i}",
                plan={"steps": [{"step": "do", "role": "slow"}]},
            )
        )
        ids.append(m.id)
    runners = [_runner_for(world, mid) for mid in ids]
    tasks = [asyncio.create_task(r.run()) for r in runners]
    for task in tasks:
        await asyncio.wait_for(task, timeout=10)

    for mid in ids:
        mission = await store.get_mission(mid)
        assert mission is not None and mission.status == MissionStatus.DONE
        # each mission's events stayed its own (no cross-contamination)
        events = await store.events_since(0, mission_id=mid)
        assert all(e.mission_id == mid for e in events)
        assert any(e.type.value == "role_spawned" for e in events)
