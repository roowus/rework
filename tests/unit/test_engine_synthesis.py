"""Synthesis rule: mission outcome counts only the latest attempt per step
(found live in mission 2: a 503-failed investigator, retried green, must not
fail the mission)."""

from __future__ import annotations

import asyncio

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


async def test_failed_step_gets_one_in_mission_retry(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The retry rule (found live in mission 3): a FAILED step retries once
    before the plan moves on; a second failure moves on (no infinite loop)."""
    import json as _json

    from conftest import (
        AUDIT_CLEAN,
        _auditor_script,
        _coder_script,
        _investigator_script,
    )
    from rework.kernel.state.records import Mission as M
    from rework.missions import PLAN_FIX

    store = await open_sqlite(str(tmp_path / "rt.sqlite3"))
    try:
        objective = "make add(2, 3) == 5"
        plan = _json.loads(_json.dumps(PLAN_FIX).replace("$OBJECTIVE", objective))
        await store.create_mission(M(id="mrt", objective=objective, plan=plan))

        from conftest import ScriptedProvider, role_catalog
        from rework.orchestrator.daemon import sqlite_checkpointer_factory
        from rework.roles.registry import autodiscover

        class ExplodingOnceProvider(ScriptedProvider):
            """Fails every flash-model call exactly twice, then scripts run."""

            def __init__(self) -> None:
                super().__init__()
                self.flash_failures_left = 2

            async def complete(self, request):  # type: ignore[no-untyped-def]
                if self.flash_failures_left > 0 and "glm/flash" in request.model:
                    self.flash_failures_left -= 1
                    raise RuntimeError("429 rate limit reached for requests")
                return await super().complete(request)

        provider = ExplodingOnceProvider()
        provider.route("Investigate any brief", _investigator_script())
        provider.route("Fulfill the task", _coder_script("tests green"))
        provider.route("Find issues", _auditor_script(AUDIT_CLEAN))

        # make catalog entries match the provider's model id string
        catalog = role_catalog()
        from rework.kernel.llm.catalog import ModelEntry

        entries = [
            ModelEntry(id="flash", provider="fake", model="glm/flash"),
            ModelEntry(id="pro", provider="fake", model="glm/pro"),
            ModelEntry(id="fake", provider="fake", model="fake-1"),
        ]
        catalog2 = type(catalog)(entries)

        runner = MissionRunner(
            "mrt",
            store,
            InProcessEventBus(store),
            autodiscover(),
            catalog2,
            sqlite_checkpointer_factory(str(tmp_path / "rt-ck.sqlite3")),
            tmp_path,
            poll_interval_s=0.01,
            provider=provider,
        )
        task = asyncio.create_task(runner.run())
        for _ in range(600):
            m = await store.get_mission("mrt")
            if m is not None and m.status in {
                MissionStatus.DONE,
                MissionStatus.FAILED,
                MissionStatus.AWAITING_GATE,
            }:
                break
            await asyncio.sleep(0.05)
        runs = await store.list_runs("mrt")
        inv_attempts = [r for r in runs if r.step == "investigate"]
        assert len(inv_attempts) >= 2, "a failed investigator must get an in-mission retry"
        if inv_attempts[-1].status.value == "completed":
            m = await store.get_mission("mrt")
            assert m is not None and m.status in {MissionStatus.DONE, MissionStatus.AWAITING_GATE}
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=5)
        except (TimeoutError, asyncio.CancelledError):
            pass
    finally:
        await store.aclose()


async def test_second_synthesis_never_emits_a_second_mission_done(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Reproduced live (audit 2026-11-25): a finished mission that got re-run
    (`resume` used to allow it) emitted a second `mission_done`. Synthesis is a
    compare-and-set into the terminal status now: an already-DONE mission is
    left exactly as it is."""
    store = await open_sqlite(str(tmp_path / "syn2.sqlite3"))
    try:
        await store.create_mission(Mission(id="m2", objective="x", status=MissionStatus.RUNNING))
        bus = InProcessEventBus(store)
        runner = MissionRunner("m2", store, bus, None, None, None, tmp_path)  # type: ignore[arg-type]
        await runner._synthesize("m2", ship_decision=True, summary="first")
        await runner._synthesize("m2", ship_decision=True, summary="second")
        events = await store.events_since(0, mission_id="m2")
        assert [e.type.value for e in events].count("mission_done") == 1
        row = await store.get_mission("m2")
        assert row is not None and row.result["summary"] == "first"
    finally:
        await store.aclose()


async def test_synthesis_after_an_owner_cancel_keeps_cancelled(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The owner cancelled after the runner last looked: the runner's DONE must
    not overwrite it, but the artifacts still land on the row."""
    store = await open_sqlite(str(tmp_path / "syn3.sqlite3"))
    try:
        await store.create_mission(Mission(id="m3", objective="x", status=MissionStatus.RUNNING))
        rid = run_id("m3", "s", "echo", 1)
        await store.create_run(
            Run(id=rid, mission_id="m3", role="echo", step="s", status=RunStatus.COMPLETED)
        )
        await store.update_run(rid, artifact={"kind": "echo"})
        runner = MissionRunner("m3", store, InProcessEventBus(store), None, None, None, tmp_path)  # type: ignore[arg-type]
        real = store.get_mission
        calls = {"n": 0}

        async def cancel_on_second_read(mid: str):  # type: ignore[no-untyped-def]
            calls["n"] += 1
            if calls["n"] == 2:  # after the read synthesis decides on, before its write
                await store.update_mission(mid, status=MissionStatus.CANCELLED)
            return await real(mid)

        store.get_mission = cancel_on_second_read  # type: ignore[method-assign]
        await runner._synthesize("m3", ship_decision=True)
        store.get_mission = real  # type: ignore[method-assign]
        row = await store.get_mission("m3")
        assert row is not None and row.status == MissionStatus.CANCELLED
        assert row.result["artifacts"]["echo:s"] == {"kind": "echo"}
    finally:
        await store.aclose()
