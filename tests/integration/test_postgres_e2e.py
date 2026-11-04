"""The M4-critical path: the ENGINE (concurrent tasks, not the sequential
conformance suite) against Postgres state AND Postgres checkpoints in one
database. Skipped unless REWORK_TEST_POSTGRES is set (CI sets it).

Found live while wiring the rewvis deploy: the store's single psycopg
connection interleaved queries across concurrent engine tasks ('another
command is already in progress'; aiosqlite never shows this because its
thread serializes). PostgresStore now serializes per-task. This test exists
so that fix (and anything touching the concurrency shape) stays proven.
"""

from __future__ import annotations

import os
import pathlib

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("REWORK_TEST_POSTGRES"), reason="REWORK_TEST_POSTGRES not set"
)


async def test_engine_runs_end_to_end_on_postgres_state_and_checkpoints() -> None:
    import asyncio

    from rework.config import Config, open_store_from_config
    from rework.kernel.events.bus import InProcessEventBus
    from rework.kernel.gates import GateManager
    from rework.kernel.llm.catalog import ModelCatalog, ModelEntry
    from rework.kernel.state.records import Mission, MissionStatus
    from rework.orchestrator.daemon import checkpointer_factory_for
    from rework.orchestrator.engine import MissionRunner
    from rework.roles.registry import autodiscover

    url = os.environ["REWORK_TEST_POSTGRES"]
    config = Config(db=url, checkpoints_db=url)
    store = await open_store_from_config(config)
    bus = InProcessEventBus(store)
    mission_id = "pg_e2e_echo"
    await store.create_mission(Mission(id=mission_id, objective="echo me"))
    catalog = ModelCatalog([ModelEntry(id="fake", provider="fake", model="fake-1")])
    runner = MissionRunner(
        mission_id,
        store,
        bus,
        autodiscover(),
        catalog,
        checkpointer_factory_for(config),
        pathlib.Path("/tmp/rework-pg-e2e-work"),
        poll_interval_s=0.05,
    )
    task = asyncio.create_task(runner.run())
    gates: list = []
    for _ in range(200):
        gates = await store.list_open_gates(mission_id)
        if gates:
            break
        await asyncio.sleep(0.1)
    assert gates, "gate never opened on postgres"
    # decide from a SECOND store (cross-process approve path)
    second = await open_store_from_config(config)
    try:
        await GateManager(second, InProcessEventBus(second)).decide(gates[0].id, approved=True)
    finally:
        await second.aclose()
    await asyncio.wait_for(task, timeout=30)
    m = await store.get_mission(mission_id)
    assert m is not None and m.status == MissionStatus.DONE
    await store.aclose()
