"""A blocked role parks until someone answers, and costs nothing while it waits.

ADR 0011. The properties under test are the ones the old 45 minute timeout broke:

- silence never turns into a guess (the role waits as long as it takes),
- a parked role gives its ROLE slot back (a second run is admitted meanwhile),
- a parked mission gives its MISSION slot back,
- a restart consumes an answer written while the daemon was down, and does not
  ask again,
- the escalation's kind survives into the gate, so "blocked" is not a generic
  hold in the gates tab.
"""

from __future__ import annotations

import asyncio

from rework.kernel.gates import GateManager
from rework.kernel.state.records import (
    GateKind,
    Mission,
    Run,
    RunStatus,
)
from rework.nodes.escalate import EscalationAwareExecutor, _ask_id
from rework.orchestrator.limits import RoleLimiter


class _Ctx:
    """The slice of RoleBuildCtx the escalation tool touches."""

    def __init__(self, store, bus, run, mission, park=None) -> None:
        self.store = store
        self.bus = bus
        self.run = run
        self.mission = mission
        self.gates = GateManager(store, bus)
        self._park = park

    def parked(self):
        from contextlib import nullcontext

        return self._park() if self._park is not None else nullcontext()


class _Inner:
    """A ToolExecutor that answers nothing; the ask is the only tool used."""

    async def execute(self, name: str, args: dict) -> str:  # pragma: no cover - unused
        raise AssertionError(f"unexpected tool {name}")

    def specs(self, allowed=None):  # pragma: no cover - unused
        return []


async def _mission_with_run(store, *, orchestrated: bool):
    mission = Mission(
        id="m-park",
        objective="park",
        plan={"orchestrator": {"model": "fake"}} if orchestrated else {"kind": "fix"},
    )
    await store.create_mission(mission)
    run = Run(id="m-park:code:coder:1", mission_id=mission.id, role="coder", step="code")
    await store.create_run(run)
    await store.update_run(run.id, status=RunStatus.RUNNING)
    return mission, await store.get_run(run.id)


async def test_a_role_parks_past_the_old_timeout_and_never_guesses(store) -> None:
    """No deadline: the ask returns the answer, not a guessed instruction."""
    mission, run = await _mission_with_run(store, orchestrated=True)
    ctx = _Ctx(store, _NoBus(), run, mission)
    ex = EscalationAwareExecutor(_Inner(), ctx, poll_s=0.01)

    task = asyncio.create_task(ex.execute("ask_orchestrator", {"question": "which branch?"}))
    await _wait_for_ask(store, run.id)

    answer = "ship it on main"
    fresh = await store.get_run(run.id)
    await store.update_run(run.id, escalation={**(fresh.escalation or {}), "answer": answer})

    result = await asyncio.wait_for(task, timeout=5)
    assert result == f"orchestrator: {answer}"
    assert "best judgment" not in result
    assert "time limit" not in result


async def test_an_unanswered_ask_waits_indefinitely(store) -> None:
    """The whole point: with no answer and no abort, the role is still waiting
    after any amount of time that used to trigger the guess."""
    mission, run = await _mission_with_run(store, orchestrated=True)
    ctx = _Ctx(store, _NoBus(), run, mission)
    ex = EscalationAwareExecutor(_Inner(), ctx, poll_s=0.01)

    task = asyncio.create_task(ex.execute("ask_orchestrator", {"question": "still blocked"}))
    await _wait_for_ask(store, run.id)

    await asyncio.sleep(0.5)
    assert not task.done(), "an unanswered ask must never resolve on its own"

    await store.update_run(run.id, status=RunStatus.CANCELLED if False else RunStatus.ABORTED)
    result = await asyncio.wait_for(task, timeout=5)
    assert result == "the orchestrator stopped this run"


async def test_a_parked_role_does_not_hold_a_role_slot(store) -> None:
    """Cap 1: while the first run parks, a second run must be admitted. Before
    ADR 0011 the parked run held its slot for the whole wait."""
    mission, run = await _mission_with_run(store, orchestrated=True)
    limiter = RoleLimiter(cap=1, poll_interval_s=0.01)
    await limiter.acquire(run.id)
    assert limiter.running == 1

    ctx = _Ctx(
        store,
        _NoBus(),
        run,
        mission,
        park=lambda: limiter.while_waiting(run.id),
    )
    ex = EscalationAwareExecutor(_Inner(), ctx, poll_s=0.01)
    task = asyncio.create_task(ex.execute("ask_orchestrator", {"question": "hold"}))
    await _wait_for_ask(store, run.id)

    assert limiter.running == 0, "a parked role must hand its slot back"
    other = await asyncio.wait_for(asyncio.shield(_acquire(limiter, "other")), timeout=2)
    assert other is False, "the second run is admitted immediately"

    # the slot comes back only when the answer does, and only once the other
    # run has finished with it: a resumed run QUEUES like anything else
    limiter.release("other")
    fresh = await store.get_run(run.id)
    await store.update_run(run.id, escalation={**(fresh.escalation or {}), "answer": "ok"})
    await asyncio.wait_for(task, timeout=5)
    assert run.id in limiter._held, "the answer takes the slot back before resuming"


async def test_a_parked_mission_releases_its_mission_slot(store) -> None:
    """The other slot: the old interrupt pause (gone, ADR 0023) used to hold
    REWORK_CONCURRENCY while a
    mission sat on an escalation gate."""
    from rework.orchestrator.limits import MissionSlot, PrioritySlots

    slots = PrioritySlots(1)
    slot = MissionSlot(slots, "m-park")
    await slot.acquire()
    assert slot.held

    async def park():
        return slot.while_waiting()

    async with slot.while_waiting():
        assert not slot.held, "a waiting mission must not hold the slot"
        # a second mission could start here
        other = MissionSlot(slots, "m-other")
        await asyncio.wait_for(other.acquire(), timeout=1)
        other.release()
    assert slot.held


async def test_a_restart_consumes_an_answer_instead_of_asking_again(store) -> None:
    """The ask id is derived from (run, question), so a replay after a restart
    finds the answer already on the row."""
    mission, run = await _mission_with_run(store, orchestrated=True)
    question = "which database?"
    ask_id = _ask_id(run.id, question)
    await store.update_run(
        run.id,
        escalation={"id": ask_id, "kind": "blocked", "question": question, "answer": "sqlite"},
    )
    ctx = _Ctx(store, _NoBus(), run, mission)
    ex = EscalationAwareExecutor(_Inner(), ctx, poll_s=0.01)

    result = await asyncio.wait_for(
        ex.execute("ask_orchestrator", {"question": question}), timeout=5
    )

    assert result == "orchestrator: sqlite"
    fresh = await store.get_run(run.id)
    # a rewrite would have added asked_at and cleared the answer; the row is the
    # one the test seeded, untouched
    assert set(fresh.escalation) == {"id", "kind", "question", "answer"}


async def test_a_repeat_ask_is_not_served_the_old_answer(store) -> None:
    """A role that gets an answer, continues, and asks the same text again must
    park for a NEW answer. Handing back the first one would silently answer a
    question nobody asked twice."""
    mission, run = await _mission_with_run(store, orchestrated=True)
    question = "should I also touch the docs?"
    ctx = _Ctx(store, _NoBus(), run, mission)
    ex = EscalationAwareExecutor(_Inner(), ctx, poll_s=0.01)

    first = asyncio.create_task(ex.execute("ask_orchestrator", {"question": question}))
    await _wait_for_ask(store, run.id)
    fresh = await store.get_run(run.id)
    await store.update_run(run.id, escalation={**(fresh.escalation or {}), "answer": "no"})
    assert await asyncio.wait_for(first, timeout=5) == "orchestrator: no"

    second = asyncio.create_task(ex.execute("ask_orchestrator", {"question": question}))
    await asyncio.sleep(0.2)
    assert not second.done(), "a repeat ask must wait for its own answer"

    fresh = await store.get_run(run.id)
    assert fresh.escalation["answer"] is None, "the repeat wrote a fresh question"
    assert fresh.escalation["id"] != _ask_id(run.id, question), "a fresh id, not the first"
    await store.update_run(run.id, escalation={**(fresh.escalation or {}), "answer": "yes"})
    assert await asyncio.wait_for(second, timeout=5) == "orchestrator: yes"


async def test_the_ask_tool_is_only_advertised_when_the_role_asks_for_it(store) -> None:
    """Exposure is a build-time decision, not a side effect of `allowed=None`."""
    mission, run = await _mission_with_run(store, orchestrated=True)
    ctx = _Ctx(store, _NoBus(), run, mission)

    exposed = EscalationAwareExecutor(_Inner(), ctx, poll_s=0.01)
    assert "ask_orchestrator" in [s.name for s in exposed.specs()]

    silent = EscalationAwareExecutor(_Inner(), ctx, poll_s=0.01, expose_ask=False)
    assert "ask_orchestrator" not in [s.name for s in silent.specs()]


async def test_the_escalation_kind_survives_into_the_gate(store) -> None:
    """`blocked` used to collapse to GENERIC. It is a question for the owner,
    and the exact kind rides on the gate so the UI can show it."""
    from rework.orchestrator.engine import MissionRunner

    gate_kind = MissionRunner._gate_kind({"kind": "blocked"})
    assert gate_kind is GateKind.QUESTION
    assert MissionRunner._gate_kind({"kind": "needs_context"}) is GateKind.QUESTION
    assert MissionRunner._gate_kind({"kind": "generic"}) is GateKind.GENERIC
    assert MissionRunner._gate_kind({"kind": "ship"}) is GateKind.SHIP


class _NoBus:
    async def publish(self, event) -> None:
        return None


async def _wait_for_ask(store, run_id: str, timeout_s: float = 5.0) -> None:
    async def poll():
        while True:
            run = await store.get_run(run_id)
            if run is not None and (run.escalation or {}).get("question"):
                return
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), timeout=timeout_s)


async def _acquire(limiter: RoleLimiter, run_id: str) -> bool:
    return await limiter.acquire(run_id)
