"""Coder: free agentic loop inside a per-run git worktree + branch, then a
mechanical handoff (commit + diff + the loop's own test evidence).

The worktree is created by the prepare node and bound to a LazyExecutor; the
base sha is persisted onto the run context so a resumed run diffs against the
base it started with. Handoff is fully mechanical: no LLM invents the diff.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, TypedDict

from langgraph.graph import END, START, StateGraph

from rework.kernel.state.records import EventRecord, EventType
from rework.library.tools import library_executor, with_library_menu
from rework.nodes.escalate import EscalationAwareExecutor
from rework.nodes.git_ops import (
    commit as git_commit,
)
from rework.nodes.git_ops import (
    diff_base,
    ensure_worktree,
    has_changes,
    stage_all,
    unstage_nested_repos,
)
from rework.nodes.toolloop import make_toolloop_node
from rework.nodes.tools import ComplaintAwareExecutor, LazyExecutor
from rework.roles.base import RoleBuildCtx, RoleSpec
from rework.web.tools import web_executor

if TYPE_CHECKING:
    from langgraph.graph.state import CompiledStateGraph


class CoderState(TypedDict, total=False):
    objective: str
    worktree: str
    base_sha: str
    messages: list[dict[str, Any]]
    loop_turns: int
    final_text: str
    artifact: dict[str, Any] | None
    artifact_error: str | None


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


class CoderSpec(RoleSpec):
    name = "coder"
    description = (
        "Implements a change in an isolated worktree + branch, self-checks"
        " with real test runs, hands off a mechanical diff. Can search the public"
        " web (`web_search`) and read pages (`fetch_url`)."
    )
    goal = (
        "Fulfill the task completely and make the code work reliably and well:"
        " implement it properly, prove it with real test runs, and report"
        " exactly what passed and what failed."
    )
    # empty = the configured default (REWORK_DEFAULT_MODEL); no literal id in
    # a default path, so switching models is env-only
    default_model = ""
    context_manifest: ClassVar[set[str]] = {"brief", "investigation", "requested_scope"}

    def build_graph(self, ctx: RoleBuildCtx) -> CompiledStateGraph:
        repo_str = ctx.mission.repo_path
        if not repo_str:
            raise ValueError("coder role requires mission.repo_path (a git repo)")
        repo = Path(repo_str)
        branch = "rework/" + ctx.run.id.replace(":", "-")
        worktree_path = ctx.workdir / "worktree"
        executor = LazyExecutor(mission_id=ctx.mission.id)
        catalog_id = self.resolve_model(ctx)

        system_prompt = (
            f"GOAL: {self.goal}\n\n"
            "You are the coder role in a multi-agent pipeline. You are inside an"
            " isolated git worktree on your own branch: edit files freely, run"
            " tests and builds, iterate until it works. A mechanical stage"
            " commits and packages the diff after you finish, so do not worry"
            " about git: just make the changes and verify them.\n\n"
            "Rules: work ONLY inside your current working directory (your"
            " worktree); never cd to an absolute path outside it, the commit"
            " stage diffs this worktree and anything done elsewhere is lost."
            " Before you finish, run what the repo's CI runs (read"
            " .github/workflows: tests AND lint/format/type checks; run the"
            " formatter rather than only checking) and include the actual"
            " result in your final summary; state plainly what passes and what"
            " does not; if blocked, stop and say why instead of pretending.\n\n"
            "Keep it proportionate: make the smallest change that fully meets the"
            " brief. Read the code and use the existing tests; do not write"
            " throwaway probe scripts or experiments unless the brief asks for a"
            " measurement. Run the relevant tests after your change, and again"
            " only if you changed something.\n\n"
            "Never write under ~/.rework or any path outside your worktree."
            " Use temporary directories for any probes or tests that touch"
            " rework's own config or state."
        )

        async def ensure_attached() -> None:
            """RESUME SAFETY (found live: 'LazyExecutor used before attach()'
            killed a recovered mission). LangGraph resumes at the node AFTER
            prepare, so prepare never replays on restart — but the executor is
            rebuilt fresh and unattached. The loop calls this first; the
            worktree already exists by then, so re-deriving it is cheap."""
            if executor.attached:
                return
            worktree, _base = await ensure_worktree(repo, worktree_path, branch)
            executor.attach(worktree)

        async def prepare(state: CoderState) -> dict[str, Any]:
            run_ctx = ctx.run.context or {}
            # a revision continues the previous coder's branch (its changes are
            # already in the tree); base_sha stays the ORIGINAL base so the
            # auditor sees the whole change, not just this round's delta
            continue_branch = run_ctx.get("continue_branch") or None
            worktree, base_sha = await ensure_worktree(
                repo, worktree_path, branch, start_point=continue_branch
            )
            if run_ctx.get("base_sha"):
                base_sha = str(run_ctx["base_sha"])  # resume: keep the original base
            else:
                await ctx.store.update_run(ctx.run.id, context={**run_ctx, "base_sha": base_sha})
            gitignore = worktree / ".gitignore"
            if not gitignore.exists():
                await asyncio.to_thread(gitignore.write_text, DEFAULT_GITIGNORE)
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
            if investigation:
                parts.append(
                    "Investigator's findings (context from the orchestrator):\n"
                    + str(investigation)[:8000]
                )
            parts.append(
                "Implement the change, then verify: run the tests and report the"
                " actual output. Finish with a clear summary of what you changed"
                " and what passed."
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

        async def handoff(state: CoderState) -> dict[str, Any]:
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
            message = commit_message(
                ctx.mission.title, state["objective"], ctx.run.step, ctx.run.id
            )
            sha = await git_commit(wt, message, author=ctx.git_author)
            diff = await diff_base(wt, base=state.get("base_sha"))
            artifact = {
                "kind": "coder_handoff",
                "branch": branch,
                "commit": sha,
                "base_sha": state.get("base_sha"),
                "summary": state.get("final_text") or "",
                "worktree": str(wt),
                **({"excluded_from_commit": excluded} if excluded else {}),
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

        async def edited(state: dict[str, Any]) -> bool:
            wt = state.get("worktree")
            if not wt:
                return True  # nothing to check; never nudge blindly
            return await has_changes(Path(str(wt)), state.get("base_sha"))

        loop = make_toolloop_node(
            provider=ctx.provider,
            catalog=ctx.catalog,
            catalog_id=catalog_id,
            charge_scope=self.name,  # one scope per loop node: keys never collide
            executor=web_executor(
                library_executor(
                    ctx,
                    ComplaintAwareExecutor(
                        EscalationAwareExecutor(executor, ctx),
                        ctx.store,
                        ctx.mission.id,
                        ctx.run.id,
                        self.name,
                    ),
                )
            ),
            system_prompt=with_library_menu(ctx, system_prompt),
            store=ctx.store,
            mission_id=ctx.mission.id,
            run_id=ctx.run.id,
            budget=ctx.budget,
            effort=ctx.run.model_ref.get("effort"),
            bus=ctx.bus,
            resume_file=ctx.workdir / "loop.json",
            on_start=ensure_attached,
            progress_check=edited,
        )

        graph = StateGraph(CoderState)
        graph.add_node("prepare", prepare)
        graph.add_node("loop", loop)  # type: ignore[arg-type]  # plain-callable node factory
        graph.add_node("handoff", handoff)
        graph.add_edge(START, "prepare")
        graph.add_edge("prepare", "loop")
        graph.add_edge("loop", "handoff")
        graph.add_edge("handoff", END)
        return graph.compile()


SUBJECT_MAX = 72


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
