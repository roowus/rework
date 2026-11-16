"""PR delivery: push the finished branch, open the PR, and never let a
transport hiccup throw a finished mission away.

Found live (mission m-26591d66f460): `push_branch` failed once with GitHub's
reasonless `! [remote rejected] <branch> -> <branch> (failed)`, the RuntimeError
escaped to the daemon's runner-error handler, and a fully audited, gate-approved
mission was marked FAILED even though its commit was sitting in the worktree. A
manual retry a few minutes later worked.

So delivery failures are a STATE, not a crash: the mission keeps its work and
summary, `result` carries `pr_pending` + the full `pr_error` + everything needed
to try again (branch, worktree, title, body), and both `rework mission retry-pr`
and the TUI's "Retry PR" button come through here — one code path, one record.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from rework.kernel.state.records import EventRecord, EventType, Mission
from rework.kernel.state.store import StateStore
from rework.nodes.gh import pr_create
from rework.nodes.git_ops import push_branch

log = logging.getLogger("rework.delivery")

# The keys `_create_pr`/retry write into mission.result. `_synthesize` rebuilds
# the result dict from the runs, so it must carry these over explicitly.
DELIVERY_RESULT_KEYS = (
    "pr_url",
    "pr_pending",
    "pr_status",
    "pr_error",
    "pr_attempts",
    "pr_branch",
    "pr_worktree",
    "pr_title",
    "pr_body",
)


def delivery_pending(mission: Mission | None) -> bool:
    """True when this mission finished but its PR still has to be delivered."""
    if mission is None:
        return False
    return bool(((mission.result or {}) or {}).get("pr_pending"))


async def _publish(bus: Any, mission_id: str, payload: dict[str, Any]) -> None:
    if bus is None:
        return
    try:
        await bus.publish(EventRecord(mission_id=mission_id, type=EventType.INFO, payload=payload))
    except Exception:  # an event must never break delivery
        log.debug("delivery event publish failed", exc_info=True)


def _error_text(exc: BaseException) -> str:
    """The FULL failure text. git puts the line that matters LAST — after the
    `remote:` banner and the forge's "Create a pull request" hint — so nothing
    here may trim (a 2000-char prefix used to end before the reason)."""
    text = str(exc)
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


async def _write_result(
    store: StateStore, mission_id: str, updates: dict[str, Any]
) -> Mission | None:
    current = await store.get_mission(mission_id)
    result = {**((current.result if current else None) or {})}
    result.update(updates)
    for key, value in updates.items():
        if value is None:
            result.pop(key, None)
    return await store.update_mission(mission_id, result=result)


async def _safe_write(store: StateStore, mission_id: str, updates: dict[str, Any]) -> None:
    """Delivery bookkeeping must never escape: a store failure here would
    re-create the original bug (a finished mission dying in the runner)."""
    try:
        await _write_result(store, mission_id, updates)
    except Exception:
        log.exception("could not record PR delivery state for %s: %r", mission_id, updates)


async def deliver_pr(
    store: StateStore,
    bus: Any,
    mission_id: str,
    *,
    branch: str,
    worktree: str | Path,
    title: str,
    body: str,
) -> str | None:
    """Push `branch` and open the PR from it. Returns the PR URL, or None when
    delivery failed — in which case the mission is left retryable and the full
    error is recorded. Never raises for a delivery failure: the work is already
    done and the mission must not die for a flaky remote."""
    info: dict[str, Any] = {
        "pr_branch": branch,
        "pr_worktree": str(worktree),
        "pr_title": title,
        "pr_body": body,
    }
    try:
        await push_branch(Path(worktree), branch)
        url = await pr_create(Path(worktree), head=branch, title=title, body=body)
    except Exception as exc:
        error = _error_text(exc)
        attempts = int(getattr(exc, "attempts", 1) or 1)
        log.warning("PR delivery for %s failed: %s", mission_id, error)
        await _safe_write(
            store,
            mission_id,
            {
                **info,
                "pr_pending": True,
                "pr_status": "failed",
                "pr_error": error,
                "pr_attempts": attempts,
            },
        )
        await _publish(
            bus,
            mission_id,
            {
                "note": f"PR not delivered for {branch}; the work is kept and "
                "`rework mission retry-pr` (or the TUI's Retry PR) can finish it",
                "error": error,
                "branch": branch,
            },
        )
        return None
    await _safe_write(
        store,
        mission_id,
        {
            "pr_url": url,
            "pr_pending": False,
            "pr_status": "opened",
            "pr_error": None,
            "pr_attempts": 1,
        },
    )
    await _publish(bus, mission_id, {"note": f"PR opened: {url}"})
    return url


def _fallback_body(mission: Mission) -> str:
    """A PR body for a mission whose delivery record predates this fix (no
    persisted body). The rich body is built by the engine and persisted with
    the failure, so this only covers hand-made rows."""
    summary = str(((mission.result or {}) or {}).get("summary") or "")
    return (
        f"Produced by rework mission `{mission.id}`.\n\n"
        f"## What changed\n\n{summary}\n\n"
        f"Full report: `rework report {mission.id} --md`.\n"
    )


def _pr_title(mission: Mission) -> str:
    name = mission.title or mission.objective.strip().splitlines()[0]
    return f"rework: {name if len(name) <= 72 else name[:71].rstrip() + '…'}"


def _is_checkout(path: str) -> bool:
    """A usable git checkout: an existing dir with a `.git` (dir, or the file a
    linked worktree has)."""
    if not path:
        return False
    p = Path(path)
    return p.is_dir() and (p / ".git").exists()


async def retry_pr(store: StateStore, bus: Any, mission_id: str) -> tuple[bool, str]:
    """Retry push+PR for a finished mission from its persisted delivery record.

    Shared by the CLI (`rework mission retry-pr ID`) and the TUI, so the two can
    never disagree. Returns (ok, PR URL on success / the full error on failure).
    """
    mission = await store.get_mission(mission_id)
    if mission is None:
        return False, f"unknown mission {mission_id}"
    # the SAME predicate the engine uses before it will ever push (engine.py
    # `_final_gates`): a plan that does not publish has no branch to deliver.
    # A draft mission (an audit, a written report) has no branch to publish, so
    # retry must not create one: the branch fallback below would otherwise push
    # a coder's branch and open a PR for a plan that never publishes.
    plan = dict(mission.plan or {})
    # legacy `fix-pr` missions predate the `publish` key (their plan is
    # {"playbook": "fix-pr", "gates": []} or {"kind": "fix-pr", ...}) and can
    # carry a pending PR, so the playbook / kind names a publishing plan too
    publishes = (
        plan.get("publish")
        or "open_pr" in (plan.get("gates") or [])
        or "fix-pr" in (plan.get("playbook"), plan.get("kind"))
    )
    if not publishes:
        return False, (
            f"mission {mission_id} does not publish "
            f"(plan {plan.get('playbook') or plan.get('kind') or 'default'!r}): "
            "there is no branch to push and no pull request to retry; a read-and-report "
            "mission's deliverable is its Report tab"
        )
    result = dict(mission.result or {})
    branch = str(result.get("pr_branch") or "")
    if not branch:
        runs = await store.list_runs(mission_id)
        coders = [r for r in runs if r.role == "coder" and (r.artifact or {}).get("branch")]
        if coders:
            branch = str((coders[-1].artifact or {}).get("branch") or "")
            result.setdefault("pr_worktree", (coders[-1].artifact or {}).get("worktree"))
    if not branch:
        return False, (
            f"mission {mission_id} has no recorded branch to push; "
            "its PR delivery state is unknown (nothing to retry)"
        )
    recorded = str(result.get("pr_worktree") or "")
    worktree = recorded
    if not _is_checkout(recorded):
        # the coder worktree may have been reclaimed since the failure: the
        # branch is usually still a ref of the mission's own repo
        worktree = str(mission.repo_path or "")
    if not _is_checkout(worktree):
        checked = [p for p in dict.fromkeys([recorded, str(mission.repo_path or "")]) if p]
        error = (
            f"the worktree for branch {branch} of mission {mission_id} is gone "
            f"(checked: {', '.join(checked) or 'no recorded path'}), so the delivery "
            "cannot be retried automatically; re-create a checkout containing that "
            "branch (or push it by hand) and run retry-pr again"
        )
        previous = str(result.get("pr_error") or "")
        if previous:
            error += f"\nunderlying error from the original attempt:\n{previous}"
        log.warning("%s", error)
        return False, error
    url = await deliver_pr(
        store,
        bus,
        mission_id,
        branch=branch,
        worktree=worktree,
        title=str(result.get("pr_title") or _pr_title(mission)),
        body=str(result.get("pr_body") or _fallback_body(mission)),
    )
    if url is None:
        current = await store.get_mission(mission_id)
        return False, str(((current.result if current else None) or {}).get("pr_error") or "")
    return True, url
