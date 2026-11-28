"""The coding role's mechanical ends: worktree preparation and the handoff.

A coding role is three things: a worktree and branch, a LOOP that makes the
change, and a mechanical handoff that commits and packages it. Only the loop
differs between a role whose loop is rework's own tool loop (the `coder`) and one
whose loop is an external coding harness (`harness`, ADR 0015). Everything else
is identical, and identical by CONSTRUCTION rather than by two copies that
happen to agree: both build their graph from the functions here.

The handoff is what the rest of the system trusts: `kind: "coder_handoff"` with
the branch, commit, diff, changed files and the tests block. The auditor's
red-green check, the ship gate and the PR body all read that shape, so a harness
that leaves a commit is deliverable exactly like the native coder.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from rework.kernel.state.records import EventRecord, EventType
from rework.nodes.git_ops import (
    commit as git_commit,
)
from rework.nodes.git_ops import (
    diff_base,
    ensure_worktree,
    has_changes,
    head_sha,
    stage_all,
    unstage_nested_repos,
)
from rework.nodes.testing import changed_files, parse_tests_block
from rework.roles.base import RoleBuildCtx

# written into the worktree at prepare when absent: tool runs generate junk
# (pycache, caches) that must never reach the handoff diff
DEFAULT_GITIGNORE = """__pycache__/
*.py[cod]
.pytest_cache/
.mypy_cache/
.ruff_cache/
.venv/
venv/
node_modules/
dist/
build/
*.egg-info/
.DS_Store
"""

SUBJECT_MAX = 72  # a commit subject, not the brief


def branch_for(ctx: RoleBuildCtx) -> str:
    """The branch this run works on. One rule, so the other roles' auto-wiring
    (which finds a coder run by its artifact's branch) cannot disagree with the
    branch that actually exists."""
    return "rework/" + ctx.run.id.replace(":", "-")


def worktree_for(ctx: RoleBuildCtx) -> Path:
    return ctx.workdir / "worktree"


def commit_message(title: str | None, objective: str, step: str, run_id: str) -> str:
    """A short subject, the brief in the body. Found live: the subject was the
    whole brief, and GitHub's squash merge made that paragraph the commit
    title on main (#5)."""
    import re

    subject = (title or "").strip()
    if not subject:
        first = re.split(r"(?<=[.!?])\s|\n", objective.strip(), maxsplit=1)[0]
        subject = first.strip()
    subject = " ".join(subject.split())
    if step.startswith("revise"):
        subject = f"{subject} (revision)"
    if len(subject) > SUBJECT_MAX:
        subject = subject[: SUBJECT_MAX - 1].rstrip() + "…"
    return f"{subject}\n\n{objective.strip()}\n\nrework run: {run_id}"


def ensure_attached_node(ctx: RoleBuildCtx, executor: Any) -> Any:
    """RESUME SAFETY (found live: 'LazyExecutor used before attach()' killed a
    recovered mission). LangGraph resumes at the node AFTER prepare, so prepare
    never replays on restart, but the executor is rebuilt fresh and unattached.
    The loop calls this first; the worktree already exists by then."""

    async def ensure_attached() -> None:
        if getattr(executor, "attached", True):
            return
        worktree, _base = await ensure_worktree(
            Path(str(ctx.mission.repo_path)), worktree_for(ctx), branch_for(ctx)
        )
        executor.attach(worktree)

    return ensure_attached


def prepare_node(ctx: RoleBuildCtx, executor: Any) -> Any:
    """Create (or reuse) the worktree and branch, and build the brief the loop
    or the harness will follow. Identical for every coding role."""
    repo = Path(str(ctx.mission.repo_path))
    branch = branch_for(ctx)
    worktree_path = worktree_for(ctx)

    async def prepare(state: dict[str, Any]) -> dict[str, Any]:
        run_ctx = ctx.run.context or {}
        # a revision continues the previous coder's branch (its changes are
        # already in the tree); base_sha stays the ORIGINAL base so the auditor
        # sees the whole change, not just this round's delta
        continue_branch = run_ctx.get("continue_branch") or None
        worktree, base_sha = await ensure_worktree(
            repo, worktree_path, branch, start_point=continue_branch
        )
        if run_ctx.get("base_sha"):
            base_sha = str(run_ctx["base_sha"])  # resume: keep the original base
        else:
            await ctx.store.update_run(ctx.run.id, context={**run_ctx, "base_sha": base_sha})
        # tool runs generate junk (pycache, caches) that must never reach the
        # diff, but the ignore rules must not reach it either: this used to
        # write a .gitignore INTO the worktree, the handoff committed it, and
        # every real-model audit on rewvis flagged the unrequested file
        # (found live: m-d69d16642add, m-3c3ea5148697 spent rounds on it).
        # The checkout's own info/exclude is never committed.
        await _exclude_junk(worktree)
        if hasattr(executor, "attach"):
            executor.attach(worktree)
        investigation = run_ctx.get("investigation")
        brief = str(run_ctx.get("brief") or state["objective"])
        parts = [f"Implementation brief: {brief}"]
        if continue_branch:
            parts.append(
                "You are continuing earlier work: your worktree already contains the"
                f" previous attempt's changes (branch {continue_branch}). Change only"
                " what the brief asks for; do not start over."
            )
        if run_ctx.get("previous_attempt"):
            parts.append(
                "The previous attempt's handoff note (trust it; do not re-read files"
                " it already summarized unless you must edit them):\n"
                + str(run_ctx["previous_attempt"])[:6000]
            )
        if isinstance(investigation, dict) and (
            investigation.get("tests") or investigation.get("no_tests_reason")
        ):
            parts.append(
                "Tests the investigator proposed (start from these: write the"
                " smallest one that catches the bug, watch it fail, then fix):\n"
                + json.dumps(
                    {
                        "tests": investigation.get("tests") or [],
                        "no_tests_reason": investigation.get("no_tests_reason") or "",
                    },
                    indent=1,
                )[:3000]
            )
        if investigation:
            parts.append(
                "Investigator's findings (context from the orchestrator):\n"
                + str(investigation)[:8000]
            )
        parts.append(
            "Write the test first and watch it fail for the right reason, then"
            " implement the change and watch it pass; report the actual output."
            " Finish with a clear summary of what you changed and what passed,"
            " ending with the fenced json tests block."
        )
        await ctx.bus.publish(
            EventRecord(
                mission_id=ctx.mission.id,
                run_id=ctx.run.id,
                type=EventType.PROGRESS,
                payload={"note": f"worktree ready on {branch}"},
            )
        )
        return {
            "worktree": str(worktree),
            "base_sha": base_sha,
            "messages": [{"role": "user", "content": "\n\n".join(parts)}],
        }

    return prepare


async def _exclude_junk(worktree: Path) -> None:
    """Append DEFAULT_GITIGNORE to the worktree's git info/exclude (shared by
    every worktree of the repo: written once, idempotent)."""
    from rework.nodes.git_ops import _run

    code, path = await _run(worktree, "rev-parse", "--git-path", "info/exclude")
    if code != 0 or not path.strip():
        return
    exclude = Path(path.strip())
    if not exclude.is_absolute():
        exclude = worktree / exclude

    def write() -> None:
        current = exclude.read_text() if exclude.exists() else ""
        if DEFAULT_GITIGNORE.strip() in current:
            return
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text(f"{current.rstrip()}\n# rework: tool-run junk\n{DEFAULT_GITIGNORE}")

    await asyncio.to_thread(write)


def handoff_node(ctx: RoleBuildCtx) -> Any:
    """Commit the worktree, package the diff and the tests block, and attach run
    stats. Runs on the HOST for every coding role: a role never writes history
    or pushes (ADR 0014)."""
    branch = branch_for(ctx)

    async def handoff(state: dict[str, Any]) -> dict[str, Any]:
        wt = Path(state["worktree"])
        await stage_all(wt)
        excluded = await unstage_nested_repos(wt)
        if excluded:
            await ctx.bus.publish(
                EventRecord(
                    mission_id=ctx.mission.id,
                    run_id=ctx.run.id,
                    type=EventType.PROGRESS,
                    payload={
                        "note": "handoff: left nested git repos out of the commit: "
                        + ", ".join(excluded)
                    },
                )
            )
        message = commit_message(ctx.mission.title, state["objective"], ctx.run.step, ctx.run.id)
        sha = await git_commit(wt, message, author=ctx.git_author)
        if sha is None:
            # nothing left to commit: the coder may have committed with its own
            # shell. The branch head is what was delivered either way. Found
            # live (rewvis, a fix-pr mission): a coder that ran `git commit`
            # itself handed off commit=None, the audit rail then saw no commit,
            # and the orchestrator spent 8 more runs trying to "produce a sha".
            head = await head_sha(wt)
            base = str(state.get("base_sha") or "")
            if head and head != base:
                sha = head
        diff = await diff_base(wt, base=state.get("base_sha"))
        tests_block = parse_tests_block(state.get("final_text") or "")
        artifact = {
            "kind": "coder_handoff",
            "branch": branch,
            "commit": sha,
            "base_sha": state.get("base_sha"),
            "summary": state.get("final_text") or "",
            "worktree": str(wt),
            **({"tests": tests_block} if tests_block is not None else {}),
            **({"excluded_from_commit": excluded} if excluded else {}),
            # from the FULL diff: the stored copy is cut, and a file listed
            # only past the cut would escape the auditor's red-green revert
            "changed_files": changed_files(diff),
            "diff": diff[:100_000],
        }
        from rework.nodes.run_stats import attach_run_stats

        artifact = await attach_run_stats(ctx.store, ctx.run.id, artifact)
        await ctx.bus.publish(
            EventRecord(
                mission_id=ctx.mission.id,
                run_id=ctx.run.id,
                type=EventType.INFO,
                payload={"note": f"handoff: commit {str(sha)[:10] if sha else 'none'}"},
            )
        )
        return {"artifact": artifact}

    return handoff


async def edited_progress(state: dict[str, Any]) -> bool:
    """Progress check for the loop: has the worktree changed yet? A harness does
    not use it (it reports its own progress), but the native coder does."""
    wt = state.get("worktree")
    if not wt:
        return True  # nothing to check; never nudge blindly
    return await has_changes(Path(str(wt)), state.get("base_sha"))
