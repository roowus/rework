"""The mechanical rails: one implementation, both drivers.

rework has two drivers (the LLM orchestrator, ADR 0007, and the frozen template
runner used by evals and `--mechanical`). Before this module each driver owned
some of the finish rules and not others, so a `--mechanical` fix-pr mission could
ship without the "audited after the last change" check that an orchestrator
mission cannot skip. The asymmetry was invisible because the two paths look
equivalent and are used for different things.

Everything here is a pure rule over store rows: no LLM, no driver state. The
orchestrator calls `audit_problem`/`findings_problem` from `finish(done)`; the
template runner calls the same functions immediately before its `_final_gates`.
Callers turn a returned string into their own error type (a ToolError for the
model, a failed synthesis for the runner).

`summary_problem` and `coverage_problem` are the two judgment-call rules: they
describe what a caller must supply, not what a driver must refuse. Both drivers
can enforce them; only the orchestrator has text to check today.
"""

from __future__ import annotations

import re
from typing import Any

from rework.kernel.state.records import Run, RunStatus
from rework.nodes.report_sections import blocking_findings, open_blockers
from rework.nodes.testing import handoff_tests_gap

# -- which roles write code ---------------------------------------------------
#
# The harness (ADR 0015) leaves the same `coder_handoff` as the coder, so every
# place that used to look for role == "coder" must look for this instead: the
# audit rail, the auto-wiring, the continue-branch scan and the ship picker.
WRITING_ROLES = frozenset({"coder", "harness"})

# The artifact a code-writing role leaves (roles/builtin/coding.py). Consumers
# look a producer up by THIS, not by role name: a new code-writing role that
# leaves the same handoff is wired, delivered and audited with no list to edit.
CODER_HANDOFF = "coder_handoff"


def writes_code(run_role: str) -> bool:
    return run_role in WRITING_ROLES


def is_code_handoff(run: Run) -> bool:
    """A run whose artifact is a code-writing handoff (coder or harness)."""
    return (run.artifact or {}).get("kind") == CODER_HANDOFF or (
        writes_code(run.role) and bool((run.artifact or {}).get("branch"))
    )


def latest_handoff(runs: list[Run], *, completed_only: bool = True) -> Run | None:
    """The newest code-writing handoff with a branch (runs are created_at-ordered)."""
    found = [
        r
        for r in runs
        if is_code_handoff(r)
        and (r.artifact or {}).get("branch")
        and (not completed_only or r.status == RunStatus.COMPLETED)
    ]
    return found[-1] if found else None


# -- which runs count ---------------------------------------------------------


def completed(runs: list[Run], role: str) -> list[Run]:
    """Finished runs of one role, oldest first (list_runs is created_at-ordered)."""
    return [r for r in runs if r.role == role and r.status == RunStatus.COMPLETED]


def plan_allows_unaudited_done(plan: dict[str, Any] | None) -> bool:
    """True only for the `audit` playbook (`orchestrated_plan` stamps
    plan["playbook"]) and only while it can never change or ship anything: no
    coder step, no gates and no publish. `draft` also has none of those but
    writes files into the project, so the shape alone is not enough. The
    audit-rail exemption for a survey-only mission is keyed on this, not on
    which runs happened to be spawned."""
    plan = plan or {}
    if plan.get("playbook") != "audit":
        return False
    steps = plan.get("steps") or []
    has_coder = any(isinstance(s, dict) and s.get("role") == "coder" for s in steps)
    return not has_coder and not plan.get("gates") and not plan.get("publish")


def _cannot_write_code(plan: dict[str, Any] | None) -> bool:
    """True when nothing in this plan can change a repository, so there is no
    diff to audit.

    No coder/harness step, no publish, and not a coding playbook. `draft` writes
    files into the project, so it is excluded even though it has no coder step;
    `audit` is handled by its own surveyor rule before this is reached. A plan
    with no `steps` at all is the engine's echo default (DEFAULT_PLAN)."""
    plan = plan or {}
    if plan.get("publish"):
        return False
    if str(plan.get("playbook") or "") in ("draft", "fix", "fix-pr"):
        return False
    steps = plan.get("steps")
    if steps is None:
        return True  # no plan at all: DEFAULT_PLAN (echo)
    return not any(isinstance(s, dict) and writes_code(str(s.get("role"))) for s in steps)


# -- what ships must have been audited ---------------------------------------


def audit_problem(runs: list[Run], plan: dict[str, Any] | None) -> str | None:
    """Why `done` is not allowed yet, or None when the audit rail is satisfied.

    Rule: when ANY code-writing run exists in the mission (coder or harness,
    whatever its status: completed, failed, aborted, still running), finishing
    needs a completed auditor, and no code-writing run may have finished after
    that audit STARTED.

    Exemptions, in order:
    - the survey-only audit playbook: a COMPLETED surveyor stands in (a survey
      changes nothing, so there is no diff);
    - a plan that cannot write code at all (echo, a read-only plan, a test-only
      role like the chaos suite's `crashy`): nothing ships, nothing to audit.
      A CODING playbook is never exempt even before its coder runs, so a
      mission cannot dodge the rail by spawning nothing.
    """
    audits = completed(runs, "auditor")
    coders = [r for r in runs if writes_code(r.role)]
    if not coders and not audits:
        surveyed = any(r.role == "surveyor" and r.status == RunStatus.COMPLETED for r in runs)
        if surveyed and plan_allows_unaudited_done(plan):
            return None
        if str((plan or {}).get("playbook") or "") == "audit":
            # the audit playbook's evidence IS a completed surveyor: an audit
            # that produced no survey has produced nothing
            return (
                "the audit playbook needs a COMPLETED surveyor: a survey that failed "
                "or never ran leaves the mission with no evidence to report"
            )
        if _cannot_write_code(plan):
            return None
    if not audits:
        return (
            "no auditor has completed in this mission: an auditor must review the coder's "
            "branch before the mission can finish"
        )
    last_audit = audits[-1]
    after = [r for r in coders if (r.updated_at or r.created_at) > last_audit.created_at]
    if after:
        return (
            f"coder {after[-1].id} ({after[-1].status}) ran after the last completed "
            f"audit {last_audit.id}: those changes are unaudited"
        )
    if coders:
        gap = handoff_tests_gap(coders[-1].artifact)
        if gap:
            return f"coder {coders[-1].id} handoff is incomplete on tests: {gap}"
    return None


# -- open findings need an explicit accepted_findings entry ------------------


def findings_problem(runs: list[Run], accepted: list[dict[str, str]]) -> str | None:
    """Open medium-or-higher findings or blockers that `accepted` does not
    cover, or None. This is a judgment rail: shipping with them is allowed, but
    every one must be named with a reason."""
    audits = completed(runs, "auditor")
    if not audits:
        return None  # audit_problem already refused this
    artifact = audits[-1].artifact or {}
    open_items = [str(f.get("title") or "") for f in blocking_findings(artifact)]
    open_items += [str(b.get("question") or "") for b in open_blockers(artifact)]
    open_items = [item for item in open_items if item]
    if not open_items:
        return None
    covered = {str(a.get("finding") or "").strip().lower() for a in accepted}
    covered.discard("")
    missing = [item for item in open_items if not any(covers(key, item) for key in covered)]
    if not missing:
        return None
    return (
        "the latest audit still has open medium-or-higher findings/blockers that "
        "accepted_findings does not cover: " + "; ".join(missing)
    )


_COVER_MIN_CHARS = 20  # a containment match needs a real title, not a fragment


def covers(accepted_key: str, open_item: str) -> bool:
    """Does an accepted_findings entry name this open finding/blocker? Exact
    match first, then a containment match either way so a long title quoted
    slightly differently still counts (a model quotes titles imperfectly).

    Containment is deliberately strict: the excerpt must be at least
    _COVER_MIN_CHARS long, so one vague entry ("tests", "the caller may crash")
    cannot silently cover several unrelated findings. This is a soft matching
    heuristic for the judgment-call path, never a bypass of `audit_problem`,
    which is mechanical and exact."""
    if not accepted_key:
        return False
    item = open_item.strip().lower()
    if accepted_key == item:
        return True
    if len(accepted_key) < _COVER_MIN_CHARS:
        return False
    return accepted_key in item or item in accepted_key


# -- text rails ---------------------------------------------------------------

_MIN_SUMMARY_CHARS = 20

_STATUS_WORDS = frozenset(
    "done finished complete completed merged ok okay lgtm good shipped".split()
)
_STATUS_FILLER = frozenset(
    "a all already and been everything for has have here i is it its just now s "
    "the to up we".split()
)


def reads_like_status_note(summary: str) -> bool:
    """`done!`, `FINISHED ALREADY.`, `all good`, `LGTM`: a state, not a change."""
    words = re.sub(r"[^a-z0-9 ]+", " ", summary.lower()).split()
    rest = [w for w in words if w not in _STATUS_FILLER]
    return bool(rest) and all(w in _STATUS_WORDS for w in rest)


def summary_is_substantive(summary: str) -> bool:
    """Usable as a PR body: not blank, not very short, not a status note."""
    text = " ".join(summary.split())
    return len(text) >= _MIN_SUMMARY_CHARS and not reads_like_status_note(text)


def summary_problem(summary: str) -> str | None:
    """Why this summary cannot be the mission's story, or None."""
    if summary_is_substantive(summary):
        return None
    return (
        "summary must say what changed for the owner: it becomes the PR body "
        "('## What changed'), so name the change and why it was made. A status "
        "note such as 'finished', 'done' or 'all good' is refused."
    )


def accepted_findings(raw: Any) -> list[dict[str, str]]:
    """Sanitize a caller's accepted_findings: entries without both a finding
    reference and a reason are dropped (they are the model filling the field in
    to get past the rail, and are then refused as uncovered)."""
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        finding = str(item.get("finding") or item.get("title") or item.get("id") or "").strip()
        reason = str(item.get("reason") or "").strip()
        if finding and reason:
            out.append({"finding": finding, "reason": reason})
    return out
