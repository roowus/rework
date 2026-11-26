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

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from rework.kernel.state.records import EventRecord, EventType, Mission
from rework.kernel.state.store import StateStore
from rework.nodes.failure_text import scrub
from rework.nodes.gh import pr_create
from rework.nodes.git_ops import (
    DEFAULT_GIT_AUTHOR,
    PRE_MERGE_ABORT_REF,
    MergeResult,
    abort_merge,
    merge_base_into_branch,
    merge_in_progress,
    push_branch,
)

log = logging.getLogger("rework.delivery")

# How long a process may hold the delivery lock without a heartbeat before
# another (or the same, after a crash) may take it. A push + PR create is
# seconds; this is generous so a slow remote is not mistaken for a dead one.
DELIVERY_LOCK_TTL_S = 120.0
DELIVERY_HEARTBEAT_S = 20.0


class DeliveryBusy(RuntimeError):
    """The delivery lock could not be taken in time. The caller records the
    mission as retryable; it never delivers without the lock."""


class DeliveryLockLost(RuntimeError):
    """This process stopped holding the delivery lock part way through (it was
    taken over after our heartbeats stopped landing). The next step is not
    taken: another delivery may already be merging and pushing."""


class DeliveryGuard:
    """Handed to the body of `serialized_delivery`. `check()` is called between
    steps (before the merge, the push and the PR) and raises DeliveryLockLost
    once the lock is not ours. Steps are not interrupted mid-flight: killing a
    `git push` half way leaves the remote in an unknown state, while refusing
    the NEXT step is always safe."""

    def __init__(self, store: StateStore, owner: str) -> None:
        self._store = store
        self.owner = owner
        self.lost = False

    async def check(self) -> None:
        if not self.lost:
            try:
                self.lost = not await self._store.heartbeat_delivery_lock(self.owner)
            except Exception as exc:  # unreachable store: the beat task decides
                log.warning("delivery lock check failed: %s", exc)
        if self.lost:
            raise DeliveryLockLost(
                "the delivery lock was taken over during this delivery; "
                "stopped before the next step (retry-pr finishes it)"
            )


@contextlib.asynccontextmanager
async def serialized_delivery(
    store: StateStore, *, wait_s: float | None = None
) -> AsyncIterator[DeliveryGuard]:
    """Hold the ONE delivery lock while merging, pushing and opening a PR.

    Two missions finishing together both merged main and both pushed, so both
    PRs were "clean" against the main each one saw and the owner rebased one by
    hand. A process-local asyncio.Lock cannot fix that: `rework retry-pr` runs
    from a laptop against the same database, so the lock has to live in the
    store (ADR 0013).

    Fenced (audit 2026-11-25, 2.3): a lock that cannot be taken within
    `wait_s` raises DeliveryBusy (it used to deliver WITHOUT the lock), and a
    heartbeat that returns False, or that keeps failing for the takeover
    window, marks the guard lost so the body stops before its next step.
    """
    owner = f"deliver-{uuid.uuid4().hex[:12]}"
    # A holder that is ALIVE heartbeats every DELIVERY_HEARTBEAT_S, so a row
    # untouched for a few heartbeats is dead and can be taken over at once.
    # Without the short stale window a crashed delivery blocks the next one for
    # the full TTL, which is four minutes of a mission doing nothing.
    takeover_after = DELIVERY_HEARTBEAT_S * 3
    deadline = time.monotonic() + (DELIVERY_LOCK_TTL_S * 2 if wait_s is None else wait_s)
    while not await store.acquire_delivery_lock(owner, stale_after_s=takeover_after):
        if time.monotonic() >= deadline:
            raise DeliveryBusy(
                "another delivery held the lock for too long; this one is left "
                "retryable instead of pushing unserialized"
            )
        await asyncio.sleep(1.0)
    guard = DeliveryGuard(store, owner)

    async def beat() -> None:
        last_ok = time.monotonic()
        while True:
            await asyncio.sleep(DELIVERY_HEARTBEAT_S)
            try:
                if not await store.heartbeat_delivery_lock(owner):
                    guard.lost = True
                    log.error("delivery lock %s taken over", owner)
                    return
                last_ok = time.monotonic()
            except Exception as exc:
                # keep trying: a lost heartbeat must not kill a delivery by
                # itself, but past the takeover window the lock may be gone
                log.warning("delivery heartbeat failed: %s", exc)
                if time.monotonic() - last_ok >= takeover_after - DELIVERY_HEARTBEAT_S:
                    guard.lost = True
                    return

    task = asyncio.create_task(beat())
    try:
        yield guard
    finally:
        task.cancel()
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await task
        try:
            await store.release_delivery_lock(owner)
        except Exception as exc:  # pragma: no cover - release is best effort
            log.warning("release of the delivery lock failed: %s", exc)


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


# The base every mission PR is opened against (gh's own default).
PR_BASE = "main"


# `git merge` output can be long (hints, a full status block); the PR body gets
# the reason, not the tutorial.
MERGE_DETAIL_LIMIT = 1200

# The heading every merge paragraph starts with.
MERGE_NOTE_HEADING = "## Merge with main"

# The marker THIS feature writes immediately before the heading it appends, so
# the paragraph added by an earlier attempt can be told apart from a
# `## Merge with main` heading the coder's own summary (or an auditor finding)
# legitimately quotes: that text is embedded in the body verbatim.
MERGE_NOTE_SENTINEL = "<!-- rework:merge-note -->"

# The phrases `_merge_note` writes. A row stored by an OLDER rework carries no
# sentinel, so the only way to tell our paragraph from a coder body that merely
# quotes the heading is the text itself: a trailing `## Merge with main`
# section is stripped only when it reads like one of these notes.
MERGE_NOTE_MARKERS = (
    "**not** merged into",
    "Conflicting paths:",
    "Staged work that the abort",
)


def _body_with_merge_note(body: str, note: str) -> str:
    """The coder's body with THIS delivery's merge paragraph appended."""
    return f"{body.rstrip()}\n\n{MERGE_NOTE_SENTINEL}\n{note}\n"


def _without_merge_note(body: str) -> str:
    """Drop a **Merge with main** paragraph an EARLIER delivery attempt added.

    The merge is re-run on every delivery (retry included), so a note from a
    failed attempt must never survive into the next one: attempt 1 that
    conflicted and then failed to push used to persist its body, and the retry
    that merged cleanly republished "the merge conflicts ... `a.txt`" for a
    conflict that no longer existed.

    Only a paragraph this feature appended may be stripped. A body that merely
    CONTAINS the heading (a summary quoting docs, an auditor finding) has to
    survive intact: with the sentinel present the cut is at the sentinel, and
    for rows persisted by an older rework (no sentinel) the trailing section
    must also READ like our note, i.e. carry one of MERGE_NOTE_MARKERS. A coder
    summary that quotes the heading and then ends in prose or `###`
    subsections used to lose that tail."""
    index = body.find(MERGE_NOTE_SENTINEL)
    if index >= 0:
        return body[:index].rstrip() + "\n"
    # legacy rows: no marker to cut at, so the section has to look like ours
    index = body.rfind(f"\n\n{MERGE_NOTE_HEADING}")
    if index < 0:
        return body
    section = body[index + 2 :]
    if any(line.startswith("## ") for line in section.splitlines()[1:]):
        # still followed by another section: the coder's own heading
        return body
    if not any(marker in section for marker in MERGE_NOTE_MARKERS):
        # the coder's own heading, followed only by its own words
        return body
    return body[:index].rstrip() + "\n"


def _truncated(text: str) -> str:
    text = (text or "").strip()
    if len(text) <= MERGE_DETAIL_LIMIT:
        return text
    return text[:MERGE_DETAIL_LIMIT].rstrip() + " [...]"


def _merge_failure_detail(result: MergeResult) -> str:
    """git's own words for a merge that did not run, scrubbed like every other
    failure text and fenced so the body renders it verbatim."""
    text = _truncated(scrub(result.detail)) if result.detail else ""
    if not text:
        return ""
    return f"\n\n```\n{text}\n```"


def _discarded_line(result: MergeResult) -> list[str]:
    """The sentence naming the ref that holds staged work an abort discarded."""
    if not result.discarded_work:
        return []
    return [
        "",
        "Staged work that the abort of that leftover merge would have thrown away "
        f"is kept on `{PRE_MERGE_ABORT_REF}` (`{result.discarded_work}`).",
    ]


def _merge_note(result: MergeResult, branch: str) -> str | None:
    """The PR-body paragraph for a merge that did not happen (real conflicts, or
    git refusing the merge). None for every other outcome: those are silent."""
    if result.failed:
        return "\n".join(
            [
                "## Merge with main",
                "",
                f"`{PR_BASE}` was **not** merged into `{branch}`: git refused the merge "
                "(it reported no conflicting paths), so the branch is unchanged and the "
                "PR opens as it is." + _merge_failure_detail(result),
                *_discarded_line(result),
            ]
        )
    if not result.conflicted:
        return None
    paths = result.conflicting or []
    listed = "\n".join(f"- `{path}`" for path in paths)
    lines = [
        "## Merge with main",
        "",
        f"`{PR_BASE}` was **not** merged into `{branch}`: the merge conflicts, so "
        "this branch needs a human to resolve it before merging.",
        "",
        "Conflicting paths:",
        "",
        listed,
    ]
    if result.abort_failed:
        lines += [
            "",
            "The merge abort also failed, so the branch may need a manual cleanup "
            f"(`git merge --abort`): {result.abort_failed}",
        ]
    lines += _discarded_line(result)
    return "\n".join(lines)


# how a silent skip is named when discarded work forces it to be reported
_SKIP_REASONS = {
    "wrong-branch": "the worktree is not on the coder branch",
    "dirty-worktree": "the worktree has uncommitted changes",
    "no-origin": "the repo has no origin",
}


def _merge_event(result: MergeResult, branch: str) -> dict[str, Any] | None:
    """The INFO payload for the merge step, or None when there is nothing worth
    an event (no origin, up to date, and the silent skips: wrong branch, dirty
    worktree). An event never carries a merge note without saying so.

    A silent skip that DISCARDED staged work is not silent: the owner has to be
    able to find that stash, so the skip is reported after all."""
    if result.failed:
        payload: dict[str, Any] = {
            "note": f"{PR_BASE} was not merged into {branch}: git refused the merge "
            "and named no conflicting paths, so the branch is unchanged",
            "error": _truncated(scrub(result.detail)) or "git merge failed",
            "base": PR_BASE,
        }
        if result.discarded_work:
            payload["pre_merge_abort_ref"] = PRE_MERGE_ABORT_REF
            payload["pre_merge_abort_sha"] = result.discarded_work
        return payload
    if result.conflicted:
        payload = {
            "note": f"{PR_BASE} was not merged into {branch}: the merge conflicts, so "
            "the branch is unchanged and the PR lists the conflicting paths",
            "conflicting_paths": list(result.conflicting),
            "base": PR_BASE,
        }
        if result.abort_failed:
            payload["error"] = f"git merge --abort failed: {result.abort_failed}"
        if result.discarded_work:
            payload["pre_merge_abort_ref"] = PRE_MERGE_ABORT_REF
            payload["pre_merge_abort_sha"] = result.discarded_work
        return payload
    if result.merged:
        payload = {
            "note": f"merged {PR_BASE} into {branch} before opening the PR "
            f"(merge commit {result.sha or 'unknown'})",
            "merge_sha": result.sha,
            "base": PR_BASE,
        }
        if result.discarded_work:
            payload["pre_merge_abort_ref"] = PRE_MERGE_ABORT_REF
            payload["pre_merge_abort_sha"] = result.discarded_work
        return payload
    if result.skipped_reason == "fetch-failed":
        # the remote could not be read: the owner may want to know why
        return {
            "note": f"{PR_BASE} was not merged into {branch} before the PR "
            "(the fetch failed); the PR opens as it is",
            "error": _truncated(scrub(result.detail)) or "git fetch failed",
            "base": PR_BASE,
        }
    if result.discarded_work:
        # a leftover merge (aborted above) coexisted with this skip: staging was
        # thrown away, and only the ref makes it recoverable
        return {
            "note": f"{PR_BASE} was not merged into {branch} before the PR "
            f"({_SKIP_REASONS.get(result.skipped_reason or '', 'skipped')}); staged "
            f"work discarded by aborting a leftover merge is kept on "
            f"{PRE_MERGE_ABORT_REF}",
            "skipped_reason": result.skipped_reason,
            "pre_merge_abort_ref": PRE_MERGE_ABORT_REF,
            "pre_merge_abort_sha": result.discarded_work,
            "base": PR_BASE,
        }
    # dirty-worktree / wrong-branch: silent, like no-origin. Nothing the owner
    # can act on, and a repo-less mission must stay byte-identical.
    return None


async def _abort_any_merge(worktree: Path) -> None:
    try:
        if await merge_in_progress(worktree):
            await abort_merge(worktree)
    except Exception:
        log.debug("could not abort a merge in %s", worktree, exc_info=True)


async def merge_main_before_push(
    bus: Any,
    mission_id: str,
    branch: str,
    worktree: str | Path,
    author: tuple[str, str] = DEFAULT_GIT_AUTHOR,
) -> str | None:
    """Merge origin/main into `branch` in its worktree, before the push.

    Returns the paragraph to append to the PR body for a merge that did not
    happen for a reason the owner must see (real conflicts, or git refusing the
    merge), and None for every silent outcome including the repo-less one.
    Never raises and never blocks the PR: any failure here is an INFO event and
    an ordinary push, because most PRs arrive conflicting with main while other
    missions merged, and a PR that opens stale is still better than a mission
    that dies."""
    try:
        result = await merge_base_into_branch(Path(worktree), branch, PR_BASE, author)
    except Exception as exc:  # a merge must never fail the delivery
        # a half-finished merge would make the later `git add -A` commit
        # conflict markers, so leave no merge behind even on a crash
        await _abort_any_merge(Path(worktree))
        log.warning("merging %s into %s for %s failed: %r", PR_BASE, branch, mission_id, exc)
        await _publish(
            bus,
            mission_id,
            {
                "note": f"{PR_BASE} was not merged into {branch} before the PR "
                "(merge step failed); the PR opens as it is",
                "error": _error_text(exc),
                "base": PR_BASE,
            },
        )
        return None
    event = _merge_event(result, branch)
    if event is not None:
        await _publish(bus, mission_id, event)
    if result.conflicted:
        log.info("%s conflicts with %s: %s", branch, PR_BASE, result.conflicting)
    return _merge_note(result, branch)


async def deliver_pr(
    store: StateStore,
    bus: Any,
    mission_id: str,
    *,
    branch: str,
    worktree: str | Path,
    title: str,
    body: str,
    author: tuple[str, str] = DEFAULT_GIT_AUTHOR,
) -> str | None:
    """Push `branch` and open the PR from it. Returns the PR URL, or None when
    delivery failed — in which case the mission is left retryable and the full
    error is recorded. Never raises for a delivery failure: the work is already
    done and the mission must not die for a flaky remote.

    `author` is the configured commit identity (`Config.git_author`), so the
    merge commit this makes carries the same author as the mission's commits.
    """
    # the coder's own body, without any merge paragraph: the merge note is
    # recomputed on every attempt and never persisted, so a retry cannot
    # republish a conflict that has since been resolved
    body = _without_merge_note(body)
    info: dict[str, Any] = {
        "pr_branch": branch,
        "pr_worktree": str(worktree),
        "pr_title": title,
        "pr_body": body,
    }
    # merge main INTO the branch first, so the PR arrives mergeable instead of
    # conflicting with everything other missions merged while this one worked.
    # A real conflict leaves the branch untouched and is reported in the body.
    #
    # The WHOLE sequence (merge, push, PR) is under one delivery lock (ADR
    # 0012): two missions finishing together used to merge and push
    # independently, so both PRs were clean against the main each one saw and
    # the owner rebased one by hand. One lock, not one per step, because a gap
    # between the merge and the push is exactly the window that caused it.
    try:
        async with serialized_delivery(store) as guard:
            await guard.check()
            note = await merge_main_before_push(bus, mission_id, branch, worktree, author)
            if note:
                body = _body_with_merge_note(body, note)
            await guard.check()
            await push_branch(Path(worktree), branch)
            await guard.check()
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
                "pr_status": "busy" if isinstance(exc, DeliveryBusy) else "failed",
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


async def retry_pr(
    store: StateStore,
    bus: Any,
    mission_id: str,
    author: tuple[str, str] = DEFAULT_GIT_AUTHOR,
) -> tuple[bool, str]:
    """Retry push+PR for a finished mission from its persisted delivery record.

    Shared by the CLI (`rework mission retry-pr ID`) and the TUI, so the two can
    never disagree. `author` is that deployment's `Config.git_author`, so the
    merge commit pushed here carries the same author as the mission's commits.
    Returns (ok, PR URL on success / the full error on failure).
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
        from rework.orchestrator.rails import latest_handoff

        # any code-writing handoff (coder OR harness, ADR 0015)
        handoff = latest_handoff(runs, completed_only=False)
        if handoff is not None:
            branch = str((handoff.artifact or {}).get("branch") or "")
            result.setdefault("pr_worktree", (handoff.artifact or {}).get("worktree"))
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
        author=author,
    )
    if url is None:
        current = await store.get_mission(mission_id)
        return False, str(((current.result if current else None) or {}).get("pr_error") or "")
    return True, url
