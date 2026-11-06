"""At the budget cap a role PAUSES for the burn gate instead of quitting: it
carries on when the owner extends the budget, stops when the mission is
cancelled, and falls back to stopping if nobody holds the mission."""

from __future__ import annotations

import asyncio

import pytest

from rework.kernel.budget.ledger import Budget
from rework.kernel.state.records import Charge, Mission, MissionStatus
from rework.nodes import toolloop


async def _mission(store, cap: float) -> None:  # type: ignore[no-untyped-def]
    await store.create_mission(
        Mission(
            id="mb", objective="x", status=MissionStatus.RUNNING, plan={"budget": {"max_usd": cap}}
        )
    )
    await store.charge(Charge(mission_id="mb", model="m", cost_usd=1.5))


async def test_role_continues_when_the_owner_raises_the_cap(store) -> None:  # type: ignore[no-untyped-def]
    await _mission(store, 1.0)
    budget = Budget(max_usd=1.0)
    assert await toolloop._budget_exceeded(store, "mb", budget) is not None

    async def owner() -> None:
        await store.update_mission("mb", status=MissionStatus.ON_HOLD)
        await asyncio.sleep(0.2)
        await store.update_mission(
            "mb", status=MissionStatus.RUNNING, plan={"budget": {"max_usd": 3.0}}
        )

    task = asyncio.create_task(owner())
    assert await toolloop._wait_for_budget(store, "mb", budget) is True
    await task
    # the role's stale cap no longer matters: the plan's current cap wins
    assert await toolloop._budget_exceeded(store, "mb", budget) is None


async def test_role_stops_when_the_mission_is_cancelled(store) -> None:  # type: ignore[no-untyped-def]
    await _mission(store, 1.0)
    await store.update_mission("mb", status=MissionStatus.CANCELLED)
    assert await toolloop._wait_for_budget(store, "mb", Budget(max_usd=1.0)) is False


async def test_role_gives_up_when_nobody_holds_the_mission(
    store, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    await _mission(store, 1.0)
    monkeypatch.setattr(toolloop, "BUDGET_HOLD_GRACE_S", 0.5)
    assert await toolloop._wait_for_budget(store, "mb", Budget(max_usd=1.0)) is False
