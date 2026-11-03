"""Synthesis rule: mission outcome counts only the latest attempt per step
(found live in mission 2: a 503-failed investigator, retried green, must not
fail the mission)."""

from __future__ import annotations

from rework.kernel.events.bus import InProcessEventBus
from rework.kernel.state.records import (
    Mission,
    MissionStatus,
    Run,
    RunStatus,
    run_id,
)
from rework.kernel.state.sqlite import open_sqlite
from rework.orchestrator.engine import MissionRunner


async def test_latest_attempt_outcome_wins(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = await open_sqlite(str(tmp_path / "syn.sqlite3"))
    try:
        await store.create_mission(Mission(id="msyn", objective="x"))
        for attempt, status in ((1, RunStatus.FAILED), (2, RunStatus.COMPLETED)):
            rid = run_id("msyn", "investigate", "investigator", attempt)
            await store.create_run(
                Run(
                    id=rid,
                    mission_id="msyn",
                    role="investigator",
                    step="investigate",
                    attempt=attempt,
                    status=status,
                )
            )
            await store.update_run(rid, artifact={"kind": "report", "n": attempt})
        await store.update_mission("msyn", status=MissionStatus.RUNNING)

        runner = MissionRunner(
            "msyn",
            store,
            InProcessEventBus(store),
            None,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            tmp_path,  # type: ignore[arg-type]
        )
        await runner._synthesize("msyn", ship_decision=True)

        mission = await store.get_mission("msyn")
        assert mission is not None and mission.status == MissionStatus.DONE
        assert mission.result["artifacts"]["investigator:investigate"] == {
            "kind": "report",
            "n": 2,
        }
    finally:
        await store.aclose()


async def test_failed_latest_attempt_fails_mission(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = await open_sqlite(str(tmp_path / "syn2.sqlite3"))
    try:
        await store.create_mission(Mission(id="msyn2", objective="x"))
        rid = run_id("msyn2", "audit", "auditor", 1)
        await store.create_run(
            Run(
                id=rid,
                mission_id="msyn2",
                role="auditor",
                step="audit",
                status=RunStatus.FAILED,
            )
        )
        runner = MissionRunner(
            "msyn2",
            store,
            InProcessEventBus(store),
            None,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            tmp_path,  # type: ignore[arg-type]
        )
        await runner._synthesize("msyn2", ship_decision=None)
        mission = await store.get_mission("msyn2")
        assert mission is not None and mission.status == MissionStatus.FAILED
    finally:
        await store.aclose()
