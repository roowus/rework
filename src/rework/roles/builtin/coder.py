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
from rework.nodes.git_ops import (
    commit as git_commit,
)
from rework.nodes.git_ops import (
    diff_base,
    ensure_worktree,
    stage_all,
)
from rework.nodes.toolloop import make_toolloop_node
from rework.nodes.tools import ComplaintAwareExecutor, LazyExecutor
from rework.roles.base import RoleBuildCtx, RoleSpec

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
        " with real test runs, hands off a mechanical diff."
    )
    goal = (
        "Fulfill the task completely and make the code work reliably and well:"
        " implement it properly, prove it with real test runs, and report"
        " exactly what passed and what failed."
    )
    default_model = "flash"
    context_manifest: ClassVar[set[str]] = {"brief", "investigation", "requested_scope"}

    def build_graph(self, ctx: RoleBuildCtx) -> CompiledStateGraph:
        repo_str = ctx.mission.repo_path
        if not repo_str:
            raise ValueError("coder role requires mission.repo_path (a git repo)")
        repo = Path(repo_str)
        branch = "rework/" + ctx.run.id.replace(":", "-")
        worktree_path = ctx.workdir / "worktree"
        executor = LazyExecutor()
        catalog_id = str(ctx.run.model_ref.get("id") or self.default_model)

        system_prompt = (
            f"GOAL: {self.goal}\n\n"
            "You are the coder role in a multi-agent pipeline. You are inside an"
            " isolated git worktree on your own branch: edit files freely, run"
            " tests and builds, iterate until it works. A mechanical stage"
            " commits and packages the diff after you finish, so do not worry"
            " about git: just make the changes and verify them.\n\n"
            "Rules: run the test suite (or build/lint) and include the actual"
            " result in your final summary; state plainly what passes and what"
            " does not; if blocked, stop and say why instead of pretending."
        )

        async def prepare(state: CoderState) -> dict[str, Any]:
            run_ctx = ctx.run.context or {}
            worktree, base_sha = await ensure_worktree(repo, worktree_path, branch)
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
            message = f"{state['objective']}\n\nrework run: {ctx.run.id}"
            sha = await git_commit(wt, message, author=ctx.git_author)
            diff = await diff_base(wt, base=state.get("base_sha"))
            artifact = {
                "kind": "coder_handoff",
                "branch": branch,
                "commit": sha,
                "base_sha": state.get("base_sha"),
                "summary": state.get("final_text") or "",
                "worktree": str(wt),
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

        loop = make_toolloop_node(
            provider=ctx.provider,
            catalog=ctx.catalog,
            catalog_id=catalog_id,
            executor=ComplaintAwareExecutor(
                executor, ctx.store, ctx.mission.id, ctx.run.id, self.name
            ),
            system_prompt=system_prompt,
            store=ctx.store,
            mission_id=ctx.mission.id,
            run_id=ctx.run.id,
            budget=ctx.budget,
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
