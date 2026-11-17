"""The SAME transient-error propagation, on the ORCHESTRATED path.

`rework new` produces a plan with `orchestrator` set (missions.orchestrated_plan),
and that is the path the incident's chat write ran on. `MissionRunner.run`
dispatches `MissionOrchestrator(...).run()` there, which never reaches
`_pump_until_terminal` (the only reader of the supervisor's fatal latch). So a
transient error out of a spawned role run used to be set and never raised: the
role task died, its run row never left INTENT/RUNNING, the orchestrator's `_wait`
polled a run that could never go terminal, and the mission stalled (or ended
FAILED) instead of being requeued.

The fix: the orchestrator's `_owner_gate` (which both its turn loop and `_wait`
pass before every iteration) re-raises the latch, and it retires the finished
task's exception on the way so nothing warns "Task exception was never
retrieved". The error then leaves `runner.run()` and `Daemon._log_runner_error`
requeues the mission to PENDING.

Red pre-fix (base 51dc38d): `runner.run()` stalls on the wait loop until the
test's own timeout, or returns normally with the mission FAILED. Either way the
pytest.raises(Deadlock) assertion does not see the deadlock.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from conftest import ScriptedProvider, make_git_repo, role_catalog
from rework.kernel.events.bus import InProcessEventBus
from rework.kernel.llm.types import ChatResult, ToolCall, Usage
from rework.kernel.state.records import EventType, Mission, MissionStatus, new_id
from rework.kernel.state.sqlite import SqliteStore, open_sqlite
from rework.missions import orchestrated_plan
from rework.orchestrator.agent import MARKER
from rework.orchestrator.daemon import Daemon, sqlite_checkpointer_factory
from rework.orchestrator.engine import MissionRunner
from rework.roles.registry import autodiscover


class Deadlock(Exception):
    """Duck-typed psycopg deadlock: the classifier only reads `sqlstate`."""

    def __init__(self) -> None:
        super().__init__("deadlock detected on UPDATE runs")
        self.sqlstate = "40P01"


async def test_a_transient_role_error_reaches_the_daemon_from_the_orchestrator(
    tmp_path: Path,
) -> None:
    repo = make_git_repo(tmp_path / "repo")
    db_path = str(tmp_path / "state.sqlite3")
    ck_path = str(tmp_path / "checkpoints.sqlite3")
    store: SqliteStore = await open_sqlite(db_path)
    try:
        bus = InProcessEventBus(store)
        plan = orchestrated_plan("fix", "pro")
        assert plan.get("orchestrator"), "this test is about the orchestrated path"
        mission = await store.create_mission(
            Mission(id=new_id("m"), objective="survive", repo_path=str(repo), plan=plan)
        )
        provider = ScriptedProvider()
        provider.route(
            MARKER,
            [
                ChatResult(
                    text="Investigating first.",
                    tool_calls=[
                        ToolCall(
                            id="o1",
                            name="spawn_role",
                            arguments={
                                "role": "investigator",
                                "step": "investigate",
                                "brief": "find why add(2, 3) != 5",
                                "model": "flash",
                            },
                        ),
                        ToolCall(id="o2", name="wait", arguments={}),
                    ],
                    usage=Usage(input_tokens=500, output_tokens=40),
                )
            ],
        )
        runner = MissionRunner(
            mission.id,
            store,
            bus,
            autodiscover(),
            role_catalog(),
            sqlite_checkpointer_factory(ck_path),
            tmp_path / "work",
            poll_interval_s=0.05,
            provider=provider,
        )

        # The retry seam is bypassed on purpose: this is the raw driver error
        # escaping a role run, the shape that used to become a dead role task.
        real_update_run = store.update_run
        raised: list[bool] = []

        async def flaky_update_run(run_id: str, **kwargs: Any) -> Any:
            status = kwargs.get("status")
            if not raised and getattr(status, "value", None) == "running":
                raised.append(True)
                raise Deadlock()
            return await real_update_run(run_id, **kwargs)

        store.update_run = flaky_update_run  # type: ignore[method-assign]

        # bounded on purpose: "the mission stalls forever on the wait loop" is
        # one of the failure modes this test has to rule out, not hang on
        with pytest.raises(Deadlock) as caught:
            await asyncio.wait_for(runner.run(), timeout=20)

        daemon = Daemon(
            store,
            bus,
            None,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            tmp_path,
            SimpleNamespace(concurrency=1, max_roles=1, poll_interval_s=0.05, git_author=None),  # type: ignore[arg-type]
        )
        await daemon._log_runner_error(mission.id, caught.value)

        mission_after = await store.get_mission(mission.id)
        assert mission_after is not None
        assert mission_after.status == MissionStatus.PENDING, (
            "a transient database error must requeue the mission, never fail it"
        )
        result = mission_after.result or {}
        assert "40P01" in result["transient_db_error"]
        # the bound is a time budget now: the crash number is a diagnostic
        assert "crash 1" in result["crash"]
        assert "into the outage" in result["crash"]
        events = await store.events_since(0, mission_id=mission.id, limit=200)
        assert EventType.MISSION_FAILED not in [e.type for e in events]
        runs = await store.list_runs(mission.id)
        assert [r.status.value for r in runs] != ["failed"], (
            "the transient error must not be recorded as a failed run"
        )
        assert await store.next_pending_missions(limit=10) != [], "the queue claims it again"
    finally:
        await store.aclose()
