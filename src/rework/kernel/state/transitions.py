"""The mission state machine: which status moves are legal, in one table.

Before this module every writer set `missions.status` blind: the CLI and the TUI
wrote whatever the button said, and the engine wrote RUNNING after each gate
without looking. So `rework resume` on a DONE mission resurrected it (the daemon
then re-ran its final gates and emitted a second `mission_done`), a stale TUI
Cancel could flip a mission that had just finished, and an owner cancel landing
while the engine waited on a gate was overwritten by the engine's RUNNING.

Now a status change is a compare-and-set: `StateStore.transition_mission(id, to,
from_=...)` updates the row only while its status is still one of `from_`, in one
statement, and returns None when it is not. `from_` defaults to every status
this table allows to reach `to`, so a caller that names nothing still cannot
make an illegal move.

Terminal statuses (done, failed, cancelled) are final: nothing leaves them.
`resume` restores the status a mission had when it was paused (`held_from` in
the result), so a paused PENDING mission goes back to the pickup queue instead
of claiming to run with no runner behind it.
"""

from __future__ import annotations

from typing import Any

from rework.kernel.state.records import MissionStatus as S

TERMINAL: frozenset[S] = frozenset({S.DONE, S.FAILED, S.CANCELLED})

# from -> the statuses it may move to
ALLOWED: dict[S, frozenset[S]] = {
    S.PENDING: frozenset({S.RUNNING, S.ON_HOLD, S.CANCELLED, S.FAILED}),
    # RUNNING -> PENDING is the daemon's transient-crash requeue
    S.RUNNING: frozenset({S.AWAITING_GATE, S.ON_HOLD, S.PENDING, S.DONE, S.FAILED, S.CANCELLED}),
    S.AWAITING_GATE: frozenset({S.RUNNING, S.ON_HOLD, S.PENDING, S.DONE, S.FAILED, S.CANCELLED}),
    # leaving ON_HOLD to RUNNING/AWAITING_GATE/PENDING is a resume (to whatever
    # it was paused from); the burn gate also finishes a held mission
    S.ON_HOLD: frozenset({S.RUNNING, S.AWAITING_GATE, S.PENDING, S.DONE, S.FAILED, S.CANCELLED}),
    S.DONE: frozenset(),
    S.FAILED: frozenset(),
    S.CANCELLED: frozenset(),
}

# the statuses a pause is allowed from, and where `held_from` points back to
PAUSABLE: frozenset[S] = frozenset({S.PENDING, S.RUNNING, S.AWAITING_GATE})
HELD_FROM_KEY = "held_from"


def sources(to: S) -> frozenset[S]:
    """Every status this table allows to move to `to`. Same-status writes are
    allowed too (idempotent retries must not be refused)."""
    return frozenset({s for s, targets in ALLOWED.items() if to in targets} | {to})


def legal(frm: S, to: S) -> bool:
    return frm == to or to in ALLOWED[frm]


class IllegalTransition(ValueError):
    """An owner-facing refusal: the mission is not in a status this verb
    applies to (e.g. resume on a finished mission)."""

    def __init__(self, mission_id: str, current: S, verb: str) -> None:
        self.mission_id = mission_id
        self.current = current
        self.verb = verb
        super().__init__(f"cannot {verb} mission {mission_id}: it is {current.value}")


def resume_target(result: dict[str, Any] | None) -> S:
    """Where `resume` sends a paused mission: the status it was paused from.

    A missing or unusable record (a mission paused before this existed) resumes
    to RUNNING, which is what resume always did; the daemon re-derives the rest
    from the store."""
    raw = (result or {}).get(HELD_FROM_KEY)
    try:
        status = S(str(raw))
    except ValueError:
        return S.RUNNING
    return status if status in PAUSABLE else S.RUNNING
