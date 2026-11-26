"""Mission status changes are guarded transitions (audit 2026-11-25, 2.1).

Reproduced live before the fix: `rework resume` on a DONE mission set it to
running, and the daemon re-ran its final gates and emitted a second
`mission_done`. These pin the rules at the service and engine level.
"""

from __future__ import annotations

from typing import Any

import pytest

from rework.kernel.state.records import Mission, MissionStatus
from rework.kernel.state.sqlite import open_sqlite
from rework.kernel.state.transitions import ALLOWED, TERMINAL, IllegalTransition, legal
from rework.services import missions as svc


@pytest.fixture
async def store(tmp_path: Any) -> Any:
    s = await open_sqlite(str(tmp_path / "state.sqlite3"))
    yield s
    await s.aclose()


async def _mission(store: Any, status: MissionStatus, mid: str = "m-1") -> Mission:
    return await store.create_mission(Mission(id=mid, objective="x", status=status))


def test_terminal_statuses_are_final() -> None:
    for status in TERMINAL:
        assert ALLOWED[status] == frozenset()
        for other in MissionStatus:
            assert legal(status, other) is (other == status)


@pytest.mark.parametrize("status", sorted(TERMINAL))
async def test_resume_and_pause_refuse_a_finished_mission(store: Any, status: Any) -> None:
    await _mission(store, status)
    for verb in (svc.resume, svc.pause):
        with pytest.raises(IllegalTransition) as err:
            await verb(store, "m-1")
        assert status.value in str(err.value)
    row = await store.get_mission("m-1")
    assert row.status == status


async def test_cancel_does_not_rewrite_a_done_mission(store: Any) -> None:
    """A stale screen's Cancel must not turn a DONE mission (PR open) into
    CANCELLED."""
    await _mission(store, MissionStatus.DONE)
    with pytest.raises(IllegalTransition):
        await svc.cancel(store, "m-1")
    assert (await store.get_mission("m-1")).status == MissionStatus.DONE


async def test_cancel_is_idempotent(store: Any) -> None:
    await _mission(store, MissionStatus.RUNNING)
    assert (await svc.cancel(store, "m-1")).status == MissionStatus.CANCELLED
    assert (await svc.cancel(store, "m-1")).status == MissionStatus.CANCELLED


@pytest.mark.parametrize(
    "start", [MissionStatus.PENDING, MissionStatus.RUNNING, MissionStatus.AWAITING_GATE]
)
async def test_resume_returns_to_where_it_was_paused(store: Any, start: Any) -> None:
    """A paused PENDING mission used to resume as RUNNING with no runner and no
    slot behind it; it now goes back to the pickup queue."""
    await _mission(store, start)
    paused = await svc.pause(store, "m-1")
    assert paused.status == MissionStatus.ON_HOLD
    resumed = await svc.resume(store, "m-1")
    assert resumed.status == start
    assert "held_from" not in (resumed.result or {})


async def test_resume_of_a_legacy_hold_goes_to_running(store: Any) -> None:
    """A mission paused before held_from existed resumes as it always did."""
    await _mission(store, MissionStatus.ON_HOLD)
    assert (await svc.resume(store, "m-1")).status == MissionStatus.RUNNING


async def test_unknown_mission(store: Any) -> None:
    with pytest.raises(KeyError):
        await svc.cancel(store, "m-nope")


async def test_engine_gate_return_does_not_overwrite_an_owner_cancel(store: Any) -> None:
    """The engine's AWAITING_GATE -> RUNNING after a gate is a compare-and-set:
    an owner cancel that landed while it waited stands."""
    await _mission(store, MissionStatus.AWAITING_GATE)
    await svc.cancel(store, "m-1")
    moved = await store.transition_mission(
        "m-1", MissionStatus.RUNNING, from_={MissionStatus.AWAITING_GATE}
    )
    assert moved is None
    assert (await store.get_mission("m-1")).status == MissionStatus.CANCELLED
