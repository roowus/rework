"""The dedupe claim on EACH backend the test suite runs (conftest's `store` fixture:
sqlite always, postgres when REWORK_TEST_POSTGRES is set, and skipped cleanly here).

The Postgres half of the claim (ON CONFLICT on dedupe_key, the CONCURRENTLY index that
open_postgres builds) has no other runtime coverage: test_link_drop.py runs on sqlite."""

from __future__ import annotations

import os

import pytest

from rework.kernel.state.records import Charge, EventRecord, EventType, Mission, new_id


async def test_keyed_writes_land_once_on_each_backend(store) -> None:  # type: ignore[no-untyped-def]
    mission = await store.create_mission(Mission(id=new_id("m"), objective="dedupe", plan={}))
    run_id = new_id("r")
    charge = Charge(
        mission_id=mission.id,
        run_id=run_id,
        model="flash",
        cost_usd=0.5,
        dedupe_key=f"{run_id}:loop:1",
    )
    await store.charge(charge)
    await store.charge(charge)  # a retry after the ack was lost
    assert [c.dedupe_key for c in await store.run_charges(run_id)] == [f"{run_id}:loop:1"]

    event = EventRecord(mission_id=mission.id, type=EventType.INFO, dedupe_key="k-once")
    await store.append_event(event)
    await store.append_event(event)
    infos = [
        e for e in await store.events_since(0, mission_id=mission.id) if e.type == EventType.INFO
    ]
    assert len(infos) == 1


async def test_dedupe_indexes_are_idempotent_across_reopens(store) -> None:  # type: ignore[no-untyped-def]
    """Every daemon start runs the index step again: on postgres it must be a no-op
    (the index is valid) rather than a second build or an error."""
    from rework.kernel.state.postgres import PostgresStore, open_postgres

    if not isinstance(store, PostgresStore):
        pytest.skip("postgres-only: the index step runs in open_postgres")
    url = os.environ["REWORK_TEST_POSTGRES"]
    for _ in range(2):
        reopened = await open_postgres(url)
        await reopened.aclose()
