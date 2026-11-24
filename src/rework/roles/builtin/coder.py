"""Coder: free agentic loop inside a per-run git worktree + branch, then a
mechanical handoff (commit + diff + the loop's own test evidence).

The worktree is created by the prepare node and bound to a LazyExecutor; the
base sha is persisted onto the run context so a resumed run diffs against the
base it started with. Handoff is fully mechanical: no LLM invents the diff.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, TypedDict

from langgraph.graph import END, START, StateGraph

from rework.library.tools import library_executor, with_library_menu
from rework.nodes.escalate import EscalationAwareExecutor
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
        executor = LazyExecutor(mission_id=ctx.mission.id, factory=ctx.executor_factory)
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
            "TESTS ARE A DELIVERABLE (red-green; a test that cannot fail is worse"
            " than no test, because it looks like safety). Write the test FIRST,"
            " run it, and watch it FAIL for the RIGHT reason: the behaviour is"
            " missing, not a typo, not an ImportError, not a collection error"
            " (those are the wrong red: fix the test until it fails because the"
            " behaviour is absent). Then implement and watch it pass. Never edit"
            " an assertion to get green: report the disagreement as a finding"
            " instead. Record the actual commands and their real results. If a"
            " change truly cannot be tested, say why in not_testable_reason."
            " End your final message with a fenced json block, exactly:"
            ' {"tests": {"added": [files], "changed": [files],'
            ' "red_green_verified": true|false, "commands": [what you ran],'
            ' "result": "red then green: ...", "not_testable_reason": ""}}.'
            " A handoff with no tests block and no not_testable_reason is"
            " incomplete and will be refused.\n\n"
            "Never write under ~/.rework or any path outside your worktree."
            " Use temporary directories for any probes or tests that touch"
            " rework's own config or state."
        )

        from rework.roles.builtin.coding import (
            edited_progress,
            ensure_attached_node,
            handoff_node,
            prepare_node,
        )

        ensure_attached = ensure_attached_node(ctx, executor)
        prepare = prepare_node(ctx, executor)
        handoff = handoff_node(ctx)
        edited = edited_progress

        loop = make_toolloop_node(
            provider=ctx.provider,
            catalog=ctx.catalog,
            catalog_id=catalog_id,
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
        graph.add_node("loop", loop)  # type: ignore[call-overload]
        graph.add_node("handoff", handoff)
        graph.add_edge(START, "prepare")
        graph.add_edge("prepare", "loop")
        graph.add_edge("loop", "handoff")
        graph.add_edge("handoff", END)
        return graph.compile()


# commit_message and DEFAULT_GITIGNORE live in coding.py now, next to the
# handoff that uses them; re-exported here because existing callers and tests
# import them from this module.
from rework.roles.builtin.coding import (  # noqa: E402  (re-export)
    DEFAULT_GITIGNORE,
    SUBJECT_MAX,
    commit_message,
)

__all__ = [
    "DEFAULT_GITIGNORE",
    "SUBJECT_MAX",
    "CoderSpec",
    "CoderState",
    "commit_message",
]
