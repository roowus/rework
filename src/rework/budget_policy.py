"""Budget policy: how many raises happen WITHOUT asking, and how far above the
original cap they may go.

Found live: every dollar over the cap cost the owner a gate, even the first
two dollars of a $2 mission, and an orchestrator that had investigated once
had to wake somebody up just to keep going. So the rail is now:

  cap $2 -> $3 auto -> $4 auto -> (further raises ask the owner)

A raise is automatic only when ALL of these hold (see `try_auto_raise`):
`auto_raises < max_auto_raises`, the wanted cap is still inside
`original_max_usd * ceiling_factor`, and it actually raises the current cap.
`max_auto_raises = 0` disables auto-raising entirely (every raise gates), which
is what the pre-policy tests and a cautious deployment pin.

Where the two knobs come from: `Project.settings["budget_auto_raises"]` /
`["budget_ceiling_factor"]` when present and valid, else the environment
(`REWORK_BUDGET_AUTO_RAISES` default 2, `REWORK_BUDGET_CEILING_FACTOR` default
2.0), else the defaults. There is no global settings file: `~/.rework/rework.env`
is the global level, and `load_env_file` already feeds it into the environment.
Invalid values are ignored (they fall through to the next level) rather than
raising: a typo in a project setting must never stop a mission.

The state lives in the plan JSON, next to the cap it describes:

    plan["budget"] = {max_usd, ask_every_usd, original_max_usd, auto_raises,
                      raises: [{at, old, new, reason, auto}]}

`original_max_usd` is what the mission was created with, so the ceiling cannot
creep: legacy missions (created before the policy) get their current cap as the
original one (`ensure_original`).
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from rework.kernel.events.bus import EventBus
from rework.kernel.gates import notify_text
from rework.kernel.state.records import (
    EventRecord,
    EventType,
    Gate,
    GateKind,
    GateStatus,
    Mission,
    new_id,
)
from rework.kernel.state.store import StateStore

log = logging.getLogger("rework.budget_policy")

# Two automatic raises, never past twice the original cap: enough for the
# "investigate once, then finish" shape without letting a runaway mission
# spend a day of budget unattended.
DEFAULT_MAX_AUTO_RAISES = 2
DEFAULT_CEILING_FACTOR = 2.0


def _count(value: Any) -> int | None:
    """A raise count, or None when the value is not one (>= 0 whole number)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value.strip()) if isinstance(value, str) else int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _factor(value: Any) -> float | None:
    """A ceiling factor, or None when the value is not one (>= 1.0)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number < 1.0:  # NaN or below the original cap
        return None
    return number


def budget_policy(project_settings: dict[str, Any] | None = None) -> tuple[int, float]:
    """(max_auto_raises, ceiling_factor) for this project, or the global defaults.

    Project settings win when valid, then the environment, then the defaults.
    `ceiling_factor` multiplies the ORIGINAL budget, not the current one."""
    settings = project_settings or {}
    max_auto_raises = _count(settings.get("budget_auto_raises"))
    if max_auto_raises is None:
        max_auto_raises = _count(os.environ.get("REWORK_BUDGET_AUTO_RAISES"))
    if max_auto_raises is None:
        max_auto_raises = DEFAULT_MAX_AUTO_RAISES
    factor = _factor(settings.get("budget_ceiling_factor"))
    if factor is None:
        factor = _factor(os.environ.get("REWORK_BUDGET_CEILING_FACTOR"))
    if factor is None:
        factor = DEFAULT_CEILING_FACTOR
    return max_auto_raises, factor


def ensure_original(plan: dict[str, Any]) -> dict[str, Any]:
    """The plan's budget dict, with `original_max_usd` / `auto_raises` / `raises`
    filled in, and stored back on `plan`.

    A mission created before this policy has no original cap: its current cap
    IS the original one, so the ceiling is measured from where it stands."""
    budget = dict(plan.get("budget") or {})
    if budget.get("original_max_usd") is None:
        budget["original_max_usd"] = float(budget.get("max_usd") or 0.0)
    budget["auto_raises"] = int(budget.get("auto_raises") or 0)
    budget["raises"] = list(budget.get("raises") or [])
    plan["budget"] = budget
    return budget


def raises_summary(plan: dict[str, Any] | None) -> str:
    """One line for the owner's gate question, e.g.

    'Earlier raises: $2.00 -> $3.00 (auto): reason; $3.00 -> $4.00 (auto):
    reason. Original budget $2.00.' — empty when nothing was raised yet."""
    budget = (plan or {}).get("budget") or {}
    raises = [r for r in (budget.get("raises") or []) if isinstance(r, dict)]
    if not raises:
        return ""
    parts: list[str] = []
    for entry in raises:
        tag = "(auto)" if entry.get("auto") else ""
        old = float(entry.get("old") or 0.0)
        new = float(entry.get("new") or 0.0)
        part = f"${old:.2f} -> ${new:.2f} {tag}".strip()
        reason = str(entry.get("reason") or "").strip()
        if reason:
            part = f"{part}: {reason}"
        parts.append(part)
    summary = "Earlier raises: " + "; ".join(parts) + "."
    original = budget.get("original_max_usd")
    if original is not None:
        summary = f"{summary} Original budget ${float(original):.2f}."
    return summary


def _raised_budget(
    plan: dict[str, Any], budget: dict[str, Any], old: float, new: float, reason: str, auto: bool
) -> dict[str, Any]:
    """Apply one raise to the in-memory plan: new cap + one entry in `raises`."""
    budget["max_usd"] = float(new)
    if auto:
        budget["auto_raises"] = int(budget.get("auto_raises") or 0) + 1
    budget["raises"] = [
        *(budget.get("raises") or []),
        {
            "at": time.time(),
            "old": float(old),
            "new": float(new),
            "reason": reason,
            "auto": bool(auto),
        },
    ]
    ensure_original(plan)
    plan["budget"] = budget
    return budget


async def record_raise(
    store: StateStore,
    mission: Mission | None,
    old: float,
    new: float,
    reason: str = "",
    *,
    auto: bool = False,
) -> dict[str, Any]:
    """Persist a raise the owner approved (or the policy took) so the gate
    question and the cost view can show the history. Returns the new budget
    dict so a caller holding a stale plan copy can refresh it."""
    if mission is None:
        return {}
    fresh = await store.get_mission(mission.id) or mission
    plan = dict(fresh.plan or {})
    budget = ensure_original(plan)
    budget = _raised_budget(plan, budget, old, new, reason, auto)
    await store.update_mission(mission.id, plan=plan)
    return budget


async def _history_row(
    store: StateStore, mission_id: str, old: float, new: float, reason: str
) -> Gate:
    """An ALREADY DECIDED permission gate, so the gate history shows the raise
    instead of it appearing out of nowhere. Written straight to the store on
    purpose: no GATE_OPENED event, no `notify_owner` (the owner is not needed
    and must not get two notifications for one raise)."""
    # written ALREADY decided in one insert: inserting it OPEN and then
    # deciding it leaves a moment where a poller (the TUI, the owner's phone)
    # could see an open gate for a question nobody has to answer.
    gate = Gate(
        id=new_id("gate"),
        mission_id=mission_id,
        kind=GateKind.PERMISSION,
        question=f"Auto raise: budget ${old:.2f} -> ${new:.2f}? {reason}".strip(),
        status=GateStatus.APPROVED,
        decision_note="auto",
        decision={"budget_to": new, "auto": True, "asked_by": "budget_policy"},
        spec={"budget_to": new, "auto": True, "asked_by": "budget_policy"},
        decided_at=time.time(),
    )
    return await store.create_gate(gate)


async def try_auto_raise(
    store: StateStore,
    bus: EventBus | None,
    mission: Mission | None,
    wanted: float,
    reason: str = "",
    *,
    max_auto_raises: int | None = None,
    ceiling_factor: float | None = None,
) -> bool:
    """Raise the mission's cap without asking, if the policy allows it.

    True when the cap moved (plan updated, history row written, BUDGET_RAISED
    published, notifier run). False means the caller must ask the owner."""
    if mission is None:
        return False
    allowed, default_factor = budget_policy()
    limit = allowed if max_auto_raises is None else max_auto_raises
    factor = default_factor if ceiling_factor is None else ceiling_factor
    fresh = await store.get_mission(mission.id) or mission
    plan = dict(fresh.plan or {})
    budget = ensure_original(plan)
    cap = float(budget.get("max_usd") or 0.0)
    original = float(budget.get("original_max_usd") or 0.0)
    already = int(budget.get("auto_raises") or 0)
    wanted = float(wanted)
    if wanted <= cap:  # not a raise
        return False
    if already >= limit:  # the owner's turn
        return False
    if wanted > original * factor:  # outside the automatic band
        return False
    reason = str(reason or "").strip()
    budget = _raised_budget(plan, budget, cap, wanted, reason, auto=True)
    await store.update_mission(mission.id, plan=plan)
    await _history_row(store, mission.id, cap, wanted, reason)
    if bus is not None:
        try:
            await bus.publish(
                EventRecord(
                    mission_id=mission.id,
                    type=EventType.BUDGET_RAISED,
                    payload={"old": cap, "new": wanted, "reason": reason, "auto": True},
                )
            )
        except Exception:  # the store is the source of truth; never fail a raise
            log.warning("could not publish BUDGET_RAISED", exc_info=True)
    notify_text(
        f"Budget ${cap:.2f} -> ${wanted:.2f} (auto): {reason}".rstrip(": "),
        mission.id,
        extra_env={"REWORK_BUDGET_OLD": f"{cap:.2f}", "REWORK_BUDGET_NEW": f"{wanted:.2f}"},
    )
    return True
