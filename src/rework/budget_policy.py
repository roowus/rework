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
import math
import os
import re
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
    MissionStatus,
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


# -- estimating the opening cap ---------------------------------------------
#
# The cap decides how much work happens before the owner is asked, so it should
# follow the SIZE OF THE TASK, not the deployment default: a typo fix does not
# need a feature's budget and a migration does not fit in a typo's. A keyword
# heuristic on purpose: `rework new` must not spend money (or wait on a
# provider) just to price a mission it has not started yet.

DOCS_USD = (1.0, 2.0)
FEATURE_USD = (3.0, 5.0)
RISKY_USD = (8.0, 10.0)
NO_TEXT_USD = 2.0
MIN_USD = 0.5

SHORT_TEXT_CHARS = 120  # a one-line change
FEATURE_TEXT_CHARS = 300  # past this it is probably more than one file
LONG_TEXT_CHARS = 1500  # a long brief is a big brief
HUGE_TEXT_CHARS = 3000
MANY_REQUIREMENTS = 4  # a numbered list this long is a programme, not a fix

# "this is small" signals: a docs/typo/comment/readme-level change.
_DOCS_SIGNALS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern)
    for pattern in (
        r"\bdocs?\b",
        r"\bdocumentation\b",
        r"\btypos?\b",
        r"\bcomments?\b",
        r"\brenames?\b",
        r"\bone-liner?\b",
        r"\breadme\b",
        r"\bsmall fix\b",
        r"\bbumps?\b",
        r"\bversions?\b",
    )
)

# "this is large and risky" signals, named the way the reason line names them.
_RISK_SIGNALS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("migration", re.compile(r"\bmigrat\w*\b")),
    ("schema", re.compile(r"\bschemas?\b")),
    ("auth", re.compile(r"\bauth\b")),
    ("authentication", re.compile(r"\bauthentication\b")),
    ("permission", re.compile(r"\bpermissions?\b")),
    ("security", re.compile(r"\bsecurity\b")),
    ("rewrite", re.compile(r"\brewrites?\b")),
    ("refactor everything", re.compile(r"\brefactor\b[^.!?\n]{0,30}\beverything\b")),
    ("every", re.compile(r"\bevery\b")),
    ("all of", re.compile(r"\ball of\b")),
    ("architecture", re.compile(r"\barchitecture\b")),
    ("concurrency", re.compile(r"\bconcurren\w*\b")),
    ("race", re.compile(r"\brace\b")),
    ("performance across", re.compile(r"\bperformance\b[^.!?\n]{0,30}\bacross\b")),
)

_NUMBERED_REQUIREMENT = re.compile(r"(?m)^\s*\d+[.)]")


def _round_half(value: float) -> float:
    """Nearest $0.50, never below the floor a mission needs to start."""
    return max(MIN_USD, round(float(value) * 2) / 2)


def _history_usd(cost_history: Any) -> float | None:
    """A usable typical mission cost, or None: garbage, NaN, inf and <= 0 are
    ignored rather than raised on (an estimate must never stop a mission)."""
    if cost_history is None or isinstance(cost_history, bool):
        return None
    try:
        value = float(cost_history)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return value


def finite_usd(value: Any) -> float | None:
    """A finite amount of money, or None: NaN / inf / non-numeric / bool all
    count as "not a number the owner said" and must never reach a cap.

    Found live: NaN passed the old `float(args["max_usd"])` guard, `NaN <= cap`
    and `NaN > ceiling` are both False, so a NaN cap was persisted and
    `spent > max_usd` was False for ever — a mission with no cap at all."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _in_band(value: float, band: tuple[float, float], history: float | None) -> float:
    """`value` nudged toward a typical finished-mission cost, INSIDE `band`:
    history says whether this shop's missions run cheap or expensive, it does
    not get to turn a typo into a migration."""
    low, high = band
    if history is None:
        return min(max(value, low), high)
    window_low, window_high = history * 0.75, history * 1.5
    if window_high < low:  # history runs cheaper than this band: its floor
        return low
    if window_low > high:  # history runs dearer: its ceiling, not beyond it
        return high
    return min(max(value, max(low, window_low)), min(high, window_high))


def estimate_budget(text: str, *, cost_history: float | None = None) -> tuple[float, str]:
    """(usd rounded to 0.5, one-line reason) sized to the task's complexity.

    A keyword/structural heuristic on the lowercased text: same text, same
    answer, always (no model call, no store, no network, never raises).

      * a RISKY signal — migration, schema, auth, authentication, permission,
        security, rewrite, "refactor ... everything", "every", "all of",
        architecture, concurrency, race, "performance ... across" — or a brief
        with >= 4 numbered requirements (`1.`, `2)` at line starts) or one
        longer than 1500 chars, costs $8.00; more than one such signal (or a
        brief over 3000 chars) $10.00. A risky signal OUTRANKS a smallness
        signal on purpose: "auth docs" is a large job, not a docs job.
      * docs / typo / comment / rename / one-line / readme / small fix / bump /
        version costs $1.00 for a one-liner (< 120 chars), $2.00 otherwise.
      * everything else is a normal feature: $3.00 when the text is short,
        $5.00 when it looks like more than one file (> 300 chars).
      * empty (or non-text) input is $2.00 — the blanket default this replaces
        — with a "no task text: default" reason.

    `cost_history` (a typical finished-mission cost) only nudges WITHIN the
    chosen band, clamped to [0.75x, 1.5x] of the history, so an estimate
    without history is identical to one with it at the band's own value and no
    band is ever crossed. The reason names the signal that chose the band."""
    raw = text if isinstance(text, str) else ""
    stripped = raw.strip()
    if not stripped:
        return NO_TEXT_USD, "no task text: default"
    history = _history_usd(cost_history)
    lowered = stripped.lower()
    length = len(stripped)

    signals = list(dict.fromkeys(name for name, pat in _RISK_SIGNALS if pat.search(lowered)))
    requirements = len(_NUMBERED_REQUIREMENT.findall(raw))
    weight = len(signals)
    if requirements >= MANY_REQUIREMENTS:
        weight += 1
    if length > LONG_TEXT_CHARS:
        weight += 1
    if length > HUGE_TEXT_CHARS:
        weight += 1
    if weight:
        named = ", ".join(signals)
        if not named:
            named = (
                f"{requirements} numbered requirements"
                if requirements >= MANY_REQUIREMENTS
                else f"{length}-character brief"
            )
        usd = RISKY_USD[1] if weight > 1 else RISKY_USD[0]
        return _round_half(_in_band(usd, RISKY_USD, history)), f"risky change: {named}"

    if any(pattern.search(lowered) for pattern in _DOCS_SIGNALS):
        tiny = length < SHORT_TEXT_CHARS
        reason = (
            "docs/typo-sized change: small, one-file"
            if tiny
            else "docs/typo-sized change: still one small change"
        )
        usd = DOCS_USD[0] if tiny else DOCS_USD[1]
        return _round_half(_in_band(usd, DOCS_USD, history)), reason

    short = length < FEATURE_TEXT_CHARS
    usd = FEATURE_USD[0] if short else FEATURE_USD[1]
    return (
        _round_half(_in_band(usd, FEATURE_USD, history)),
        "normal feature-sized change",
    )


FINISHED_SAMPLE = 20  # missions sampled to calibrate an estimate


async def typical_finished_cost(store: StateStore, *, limit: int = FINISHED_SAMPLE) -> float | None:
    """The MEDIAN total cost of this store's last `limit` finished missions, or
    None when there is no history (a fresh deployment).

    This is the calibration half of requirement 1: `estimate_budget`'s keyword
    bands say how big a task looks, this says what missions actually cost here,
    and `_in_band` uses it only to nudge INSIDE the chosen band. Never raises
    and never blocks a mission: an unreadable ledger is simply "no history"."""
    try:
        missions = await store.list_missions(status=MissionStatus.DONE, limit=limit)
    except Exception:  # the ledger is an optimisation, not a requirement
        log.warning("could not read finished missions to calibrate the estimate", exc_info=True)
        return None
    costs: list[float] = []
    for mission in missions or []:
        try:
            spend = await store.mission_spend(mission.id)
        except Exception:
            continue
        cost = finite_usd(getattr(spend, "spent_usd", None))
        if cost is not None and cost > 0:
            costs.append(cost)
    if not costs:
        return None
    costs.sort()
    middle = len(costs) // 2
    if len(costs) % 2:
        return costs[middle]
    return (costs[middle - 1] + costs[middle]) / 2.0


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
    if finite_usd(new) is None or finite_usd(old) is None:
        # never write a cap nobody can compare against: NaN/inf would make
        # every later `spent > max_usd` False, i.e. no cap at all
        log.warning("refusing a non-finite budget raise %r -> %r", old, new)
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
    finite = finite_usd(wanted)
    if finite is None:
        # NaN/inf compares False both ways: `NaN <= cap` and `NaN > ceiling`
        # would both let it through, and the cap would become unbounded
        log.warning("refusing to auto-raise to a non-finite cap %r", wanted)
        return False
    wanted = finite
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
