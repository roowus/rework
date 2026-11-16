"""_pump_until_terminal re-raises a role task's exception only when no task will
ever drive the run. These tests pin the guard from each side: a real error on a
dead task is raised, while an owner cancel, a cancelled task, a finished task and
an idle gate-wait run are never turned into an error. The pump is driven on a
bare MissionRunner (object.__new__) with a fake store, so no database or daemon
is needed."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any

import pytest

from rework.kernel.state.records import Mission, MissionStatus, Run, RunStatus
from rework.orchestrator.engine import MissionRunner

MISSION = "m-pump"


class FakeStore:
    def __init__(self, mission: MissionStatus = MissionStatus.RUNNING) -> None:
        self.mission = mission
        self.runs: dict[str, Run] = {}
        self.polls = 0
        self.on_get_run: Callable[[FakeStore], Awaitable[None]] | None = None

    async def get_mission(self, mission_id: str) -> Mission:
        return Mission(id=mission_id, objective="pump", status=self.mission)

    async def get_run(self, run_id: str) -> Run | None:
        self.polls += 1
        if self.on_get_run is not None:
            await self.on_get_run(self)
        return self.runs.get(run_id)


def make_run(status: RunStatus = RunStatus.RUNNING) -> Run:
    return Run(id=f"{MISSION}:do:r:1", mission_id=MISSION, role="r", step="do", status=status)


def make_runner(store: FakeStore, tasks: dict[str, asyncio.Task[Any]]) -> MissionRunner:
    runner = object.__new__(MissionRunner)
    runner._store = store  # type: ignore[assignment]
    runner._poll = 0.001
    runner._plan = None
    runner.mission_id = MISSION
    runner._supervisor = SimpleNamespace(tasks=tasks)  # type: ignore[assignment]
    return runner


async def parked() -> None:
    await asyncio.Event().wait()


async def dies_on_cancel() -> None:
    """A role whose cancel cleanup itself fails: its task ends with RuntimeError."""
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError as exc:
        raise RuntimeError("cleanup refused") from exc


async def dies_with(error: BaseException) -> None:
    raise error


async def test_owner_cancel_ends_the_pump_cancelled_with_its_task_cancelled() -> None:
    """Owner cancel through the pump path: the mission is CANCELLED, the role task
    is cancelled, and the pump returns "cancelled" instead of raising."""
    store = FakeStore(MissionStatus.CANCELLED)
    run = make_run()
    store.runs[run.id] = run
    task = asyncio.create_task(parked())
    task.cancel()
    await asyncio.wait([task])

    runner = make_runner(store, {run.id: task})
    assert await runner._pump_until_terminal([run]) == "cancelled"


async def test_owner_cancel_during_the_pass_wins_over_a_task_that_dies_on_cancel() -> None:
    """The owner cancels while the pump is between its checks: the cancelled role's
    cleanup raises. The owner's cancel must still end the mission CANCELLED; the
    cleanup's RuntimeError must not surface as the mission's error."""
    store = FakeStore(MissionStatus.RUNNING)
    run = make_run()
    store.runs[run.id] = run
    task = asyncio.create_task(dies_on_cancel())
    await asyncio.sleep(0)  # let the role park on its event

    async def owner_cancels_mid_pass(s: FakeStore) -> None:
        if s.polls == 1:
            s.mission = MissionStatus.CANCELLED
            task.cancel()
            await asyncio.wait([task])

    store.on_get_run = owner_cancels_mid_pass
    runner = make_runner(store, {run.id: task})
    assert await runner._pump_until_terminal([run]) == "cancelled"
    assert task.done() and isinstance(task.exception(), RuntimeError)


async def test_a_dead_task_with_a_real_error_is_raised_not_polled_for_ever() -> None:
    """The livelock fix: no task drives the RUNNING row and its error path could not
    record FAILED, so the pump must end with the task's exception."""
    store = FakeStore(MissionStatus.RUNNING)
    run = make_run()
    store.runs[run.id] = run
    task = asyncio.create_task(dies_with(OSError("attempt to write a readonly database")))
    await asyncio.wait([task])

    runner = make_runner(store, {run.id: task})
    with pytest.raises(OSError, match="readonly database"):
        await asyncio.wait_for(runner._pump_until_terminal([run]), timeout=5)


async def test_a_cancelled_task_is_never_raised_and_the_pump_keeps_waiting() -> None:
    """A cancelled task (pause, or any cancel that is not the owner's) is not an
    error. With the owner still running the pump keeps waiting, then returns once
    the run completes."""
    store = FakeStore(MissionStatus.RUNNING)
    run = make_run()
    store.runs[run.id] = run
    task = asyncio.create_task(parked())
    task.cancel()
    await asyncio.wait([task])

    async def run_completes_after_a_few_polls(s: FakeStore) -> None:
        if s.polls == 3:
            s.runs[run.id] = make_run(RunStatus.COMPLETED)

    store.on_get_run = run_completes_after_a_few_polls
    runner = make_runner(store, {run.id: task})
    assert await asyncio.wait_for(runner._pump_until_terminal([run]), timeout=5) == "ok"
    assert store.polls >= 3


async def test_an_idle_run_awaiting_the_owner_with_no_task_is_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gate-wait: the run is HALTED_ESCALATED, its role task has returned normally
    (no task drives it), and the owner has not decided yet. The guard must not fire;
    the pump hands the run to the gate path and carries on when the owner decides."""
    store = FakeStore(MissionStatus.RUNNING)
    run = make_run(RunStatus.HALTED_ESCALATED)
    store.runs[run.id] = run

    async def finished() -> None:
        return None

    task = asyncio.create_task(finished())
    await asyncio.wait([task])
    decided: list[str] = []
    runner = make_runner(store, {run.id: task})

    async def owner_decides(halted: list[Run]) -> None:
        decided.extend(r.id for r in halted)
        store.runs[run.id] = make_run(RunStatus.COMPLETED)

    monkeypatch.setattr(runner, "_handle_halted", owner_decides)
    assert await asyncio.wait_for(runner._pump_until_terminal([run]), timeout=5) == "ok"
    assert decided == [run.id]


@pytest.mark.parametrize("terminal", [RunStatus.FAILED, RunStatus.COMPLETED])
async def test_a_task_error_after_the_row_is_terminal_is_not_raised(terminal: RunStatus) -> None:
    """The error path recorded the outcome: the run is terminal, so the mission ends
    on the row, not on the exception."""
    store = FakeStore(MissionStatus.RUNNING)
    run = make_run(terminal)
    store.runs[run.id] = run
    task = asyncio.create_task(dies_with(RuntimeError("after the row was written")))
    await asyncio.wait([task])

    runner = make_runner(store, {run.id: task})
    assert await asyncio.wait_for(runner._pump_until_terminal([run]), timeout=5) == "ok"


async def test_owner_pause_with_a_task_that_dies_on_cancel_pauses_and_resumes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Owner PAUSE through the pump path: pausing cancels the live role, whose cleanup
    raises. The pump must pause (not raise the cleanup error as the mission's death),
    then carry on once resumed and kick the run again."""
    store = FakeStore(MissionStatus.RUNNING)
    run = make_run()
    store.runs[run.id] = run
    task = asyncio.create_task(dies_on_cancel())
    await asyncio.sleep(0)

    async def owner_pauses_mid_pass(s: FakeStore) -> None:
        if s.polls == 1:
            s.mission = MissionStatus.ON_HOLD
            task.cancel()
            await asyncio.wait([task])

    store.on_get_run = owner_pauses_mid_pass
    runner = make_runner(store, {run.id: task})
    paused: list[bool] = []
    kicked: list[str] = []

    async def pause_then_resume() -> str:
        paused.append(True)
        store.mission = MissionStatus.RUNNING
        return "ok"

    async def ensure_started(latest: Run) -> None:
        kicked.append(latest.id)
        store.runs[run.id] = make_run(RunStatus.COMPLETED)

    monkeypatch.setattr(runner, "_pause_until_resumed", pause_then_resume)
    runner._supervisor = SimpleNamespace(  # type: ignore[assignment]
        tasks={run.id: task}, ensure_started=ensure_started
    )
    assert await asyncio.wait_for(runner._pump_until_terminal([run]), timeout=5) == "ok"
    assert paused == [True]
    assert kicked == [run.id]
