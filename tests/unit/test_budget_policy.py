"""The raise policy: how many raises are free, how far they may go, and where
the two knobs are read from.

Found live: every dollar over the cap cost the owner a gate, including the
first two of a $2 mission. So the first raises inside 2x the ORIGINAL budget
are taken automatically — and only those. `REWORK_BUDGET_AUTO_RAISES=0` (or a
project setting) puts the human back in front of every raise.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from rework.budget_policy import (
    DEFAULT_CEILING_FACTOR,
    DEFAULT_MAX_AUTO_RAISES,
    budget_policy,
    ensure_original,
    raises_summary,
    record_raise,
    try_auto_raise,
)
from rework.kernel.state.records import EventType, GateStatus, Mission


@pytest.fixture(autouse=True)
def _no_ambient_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """The tests below are about the resolution order: an ambient
    ~/.rework/rework.env (which CI does not have, but a dev box might) must not
    leak a policy into them."""
    monkeypatch.delenv("REWORK_BUDGET_AUTO_RAISES", raising=False)
    monkeypatch.delenv("REWORK_BUDGET_CEILING_FACTOR", raising=False)


# -- settings resolution ----------------------------------------------------


def test_defaults_when_nothing_is_configured() -> None:
    assert budget_policy(None) == (DEFAULT_MAX_AUTO_RAISES, DEFAULT_CEILING_FACTOR)
    assert budget_policy({}) == (2, 2.0)


def test_env_sets_the_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REWORK_BUDGET_AUTO_RAISES", "5")
    monkeypatch.setenv("REWORK_BUDGET_CEILING_FACTOR", "3.5")
    assert budget_policy(None) == (5, 3.5)


def test_project_settings_beat_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REWORK_BUDGET_AUTO_RAISES", "5")
    monkeypatch.setenv("REWORK_BUDGET_CEILING_FACTOR", "3.5")
    settings: dict[str, Any] = {"budget_auto_raises": 1, "budget_ceiling_factor": 1.5}
    assert budget_policy(settings) == (1, 1.5)


@pytest.mark.parametrize(
    "settings",
    [
        {"budget_auto_raises": "many", "budget_ceiling_factor": "lots"},
        {"budget_auto_raises": -1, "budget_ceiling_factor": 0.5},  # below the original
        {"budget_auto_raises": None, "budget_ceiling_factor": None},
        {"budget_auto_raises": True, "budget_ceiling_factor": float("nan")},
    ],
)
def test_invalid_values_are_ignored_not_raised(
    settings: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A typo in a project setting must fall through to the env/default, never
    stop a mission."""
    monkeypatch.setenv("REWORK_BUDGET_AUTO_RAISES", "4")
    monkeypatch.setenv("REWORK_BUDGET_CEILING_FACTOR", "2.5")
    assert budget_policy(settings) == (4, 2.5)


def test_zero_auto_raises_is_valid_and_disables(monkeypatch: pytest.MonkeyPatch) -> None:
    """0 is not 'unset': it is the kill switch."""
    monkeypatch.setenv("REWORK_BUDGET_AUTO_RAISES", "5")
    assert budget_policy({"budget_auto_raises": 0})[0] == 0
    monkeypatch.setenv("REWORK_BUDGET_AUTO_RAISES", "0")
    assert budget_policy(None)[0] == 0


# -- plan state -------------------------------------------------------------


def test_ensure_original_treats_a_legacy_mission_as_original_now() -> None:
    """A mission created before this policy has no original cap: the ceiling is
    measured from where it stands, not from 0 (which would gate everything)."""
    plan: dict[str, Any] = {"budget": {"max_usd": 3.0, "ask_every_usd": 3.0}}
    budget = ensure_original(plan)
    assert budget["original_max_usd"] == 3.0
    assert budget["auto_raises"] == 0
    assert budget["raises"] == []


def test_raises_summary_is_empty_without_raises_and_lists_them_after() -> None:
    assert raises_summary(None) == ""
    assert raises_summary({"budget": {"max_usd": 2.0}}) == ""
    plan: dict[str, Any] = {
        "budget": {
            "max_usd": 4.0,
            "original_max_usd": 2.0,
            "raises": [
                {"old": 2.0, "new": 3.0, "reason": "one more coder", "auto": True},
                {"old": 3.0, "new": 4.0, "reason": "audit loop", "auto": True},
            ],
        }
    }
    assert raises_summary(plan) == (
        "Earlier raises: $2.00 -> $3.00 (auto): one more coder; "
        "$3.00 -> $4.00 (auto): audit loop. Original budget $2.00."
    )


# -- the shared raise path --------------------------------------------------


async def _mission(store: Any, cap: float = 2.0) -> Mission:
    return await store.create_mission(
        Mission(
            id="mb",
            objective="budget policy",
            plan={"budget": {"max_usd": cap, "ask_every_usd": cap, "original_max_usd": cap}},
        )
    )


async def test_try_auto_raise_moves_the_cap_and_leaves_a_trail(
    store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One auto raise: plan updated, auto_raises counted, a decided history gate
    (no open gate), a BUDGET_RAISED event and the notifier."""
    from rework.kernel.events.bus import InProcessEventBus

    out = tmp_path / "got"
    monkeypatch.setenv(
        "REWORK_NOTIFY_CMD",
        'echo "$REWORK_NOTIFY_KIND $REWORK_MISSION_ID $REWORK_BUDGET_OLD'
        f' $REWORK_BUDGET_NEW $REWORK_NOTIFY_MESSAGE" > {out}',
    )
    bus = InProcessEventBus(store)
    mission = await _mission(store)
    task = None
    assert await try_auto_raise(store, bus, mission, 3.0, "one more coder") is True
    # the notifier is fire-and-forget: wait for the one it created
    from rework.kernel.gates import _notify_tasks

    while _notify_tasks:
        task = next(iter(_notify_tasks))
        await task

    fresh = await store.get_mission("mb")
    budget = (fresh.plan or {})["budget"]
    assert budget["max_usd"] == 3.0
    assert budget["original_max_usd"] == 2.0
    assert budget["auto_raises"] == 1
    assert [r["new"] for r in budget["raises"]] == [3.0]
    assert budget["raises"][0]["auto"] is True

    events = await store.events_since(0, mission_id="mb")
    raised = [e for e in events if e.type == EventType.BUDGET_RAISED]
    assert len(raised) == 1
    assert raised[0].payload == {
        "old": 2.0,
        "new": 3.0,
        "reason": "one more coder",
        "auto": True,
    }

    gates = await store.list_gates("mb")
    assert len(gates) == 1 and gates[0].status == GateStatus.APPROVED
    assert gates[0].kind.value == "permission"
    assert gates[0].decision_note == "auto"
    assert (gates[0].spec or {})["budget_to"] == 3.0
    assert (gates[0].spec or {})["auto"] is True
    # no OPEN gate and no GATE_OPENED: nobody has to answer anything
    assert await store.list_open_gates("mb") == []
    assert not [e for e in events if e.type == EventType.GATE_OPENED]

    assert out.exists(), "REWORK_NOTIFY_CMD must run for an automatic raise"
    line = out.read_text().strip()
    assert line.startswith("budget_raise mb 2.00 3.00 ")
    assert "$2.00 -> $3.00 (auto): one more coder" in line


async def test_two_auto_raises_then_the_owner_is_asked(store: Any) -> None:
    """The whole point: 2 free, the 3rd asks — and each one is counted."""
    from rework.kernel.events.bus import InProcessEventBus

    bus = InProcessEventBus(store)
    mission = await _mission(store)
    assert await try_auto_raise(store, bus, mission, 3.0, "first") is True
    assert await try_auto_raise(store, bus, mission, 4.0, "second") is True
    assert await try_auto_raise(store, bus, mission, 5.0, "third") is False
    fresh = await store.get_mission("mb")
    budget = (fresh.plan or {})["budget"]
    assert budget["max_usd"] == 4.0, "the refused raise must not move the cap"
    assert budget["auto_raises"] == 2


async def test_a_raise_beyond_the_ceiling_asks_even_as_the_first(store: Any) -> None:
    from rework.kernel.events.bus import InProcessEventBus

    mission = await _mission(store)
    assert await try_auto_raise(store, InProcessEventBus(store), mission, 4.01, "greedy") is False
    assert (
        await try_auto_raise(store, InProcessEventBus(store), mission, 4.0, "at the edge") is True
    )


async def test_zero_auto_raises_disables_the_policy(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rework.kernel.events.bus import InProcessEventBus

    monkeypatch.setenv("REWORK_BUDGET_AUTO_RAISES", "0")
    mission = await _mission(store)
    assert await try_auto_raise(store, InProcessEventBus(store), mission, 3.0, "nope") is False
    fresh = await store.get_mission("mb")
    assert ((fresh.plan or {})["budget"])["max_usd"] == 2.0


async def test_no_raise_at_or_below_the_current_cap(store: Any) -> None:
    from rework.kernel.events.bus import InProcessEventBus

    mission = await _mission(store)
    bus = InProcessEventBus(store)
    assert await try_auto_raise(store, bus, mission, 2.0, "same") is False
    assert await try_auto_raise(store, bus, mission, 1.0, "lower") is False


async def test_record_raise_keeps_manual_raises_in_the_summary(store: Any) -> None:
    mission = await _mission(store)
    await record_raise(store, mission, 2.0, 6.0, "owner said yes", auto=False)
    fresh = await store.get_mission("mb")
    plan = fresh.plan or {}
    assert plan["budget"]["max_usd"] == 6.0
    assert plan["budget"]["auto_raises"] == 0, "a manual raise is not an auto one"
    assert raises_summary(plan) == (
        "Earlier raises: $2.00 -> $6.00: owner said yes. Original budget $2.00."
    )
