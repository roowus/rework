"""Owner controls on a mission: pause, resume, cancel.

Every verb is a guarded transition (kernel/state/transitions.py): it applies
only to a mission still in a status the verb makes sense for, decided in the
same statement that writes it. A refusal raises IllegalTransition naming the
status the mission is really in, so a stale screen gets "it is done", not a
resurrected mission.
"""

from __future__ import annotations

from rework.kernel.state.records import Mission, MissionStatus
from rework.kernel.state.store import StateStore
from rework.kernel.state.transitions import (
    HELD_FROM_KEY,
    PAUSABLE,
    TERMINAL,
    IllegalTransition,
    resume_target,
)

ACTIVE = frozenset(set(MissionStatus) - TERMINAL)

# A CAS can lose to a concurrent writer (the engine moving RUNNING <->
# AWAITING_GATE). Re-read and retry a few times; each attempt is one statement.
_ATTEMPTS = 5


async def _get(store: StateStore, mission_id: str) -> Mission:
    mission = await store.get_mission(mission_id)
    if mission is None:
        raise KeyError(mission_id)
    return mission


async def pause(store: StateStore, mission_id: str) -> Mission:
    """Stop spending, keep everything resumable. Records where the mission was
    paused from, so resume can put it back exactly there."""
    for _ in range(_ATTEMPTS):
        mission = await _get(store, mission_id)
        if mission.status == MissionStatus.ON_HOLD:
            return mission
        if mission.status not in PAUSABLE:
            raise IllegalTransition(mission_id, mission.status, "pause")
        result = {**(mission.result or {}), HELD_FROM_KEY: mission.status.value}
        moved = await store.transition_mission(
            mission_id, MissionStatus.ON_HOLD, from_={mission.status}, result=result
        )
        if moved is not None:
            return moved
    raise IllegalTransition(mission_id, (await _get(store, mission_id)).status, "pause")


async def resume(store: StateStore, mission_id: str) -> Mission:
    """Back to the status the mission was paused from (a paused pending
    mission returns to the pickup queue). Only an ON_HOLD mission resumes: a
    finished one is refused instead of re-run."""
    mission = await _get(store, mission_id)
    if mission.status != MissionStatus.ON_HOLD:
        raise IllegalTransition(mission_id, mission.status, "resume")
    result = dict(mission.result or {})
    target = resume_target(result)
    result.pop(HELD_FROM_KEY, None)
    moved = await store.transition_mission(
        mission_id, target, from_={MissionStatus.ON_HOLD}, result=result
    )
    if moved is None:
        raise IllegalTransition(mission_id, (await _get(store, mission_id)).status, "resume")
    return moved


async def cancel(store: StateStore, mission_id: str) -> Mission:
    """Stop the mission for good. Any unfinished status may be cancelled; a
    finished one is left as it is (cancelling a DONE mission whose PR is open
    would rewrite history)."""
    moved = await store.transition_mission(mission_id, MissionStatus.CANCELLED, from_=ACTIVE)
    if moved is None:
        mission = await _get(store, mission_id)
        if mission.status == MissionStatus.CANCELLED:
            return mission
        raise IllegalTransition(mission_id, mission.status, "cancel")
    return moved


VERBS = {"pause": pause, "resume": resume, "cancel": cancel}
