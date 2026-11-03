"""Auditor: one general evaluator over any artifact, code first (ADR 0003).

Context comes from the run context, assembled by the orchestrator (its policy,
not hardcoded tiers): diff, brief, investigation findings, and a workspace to
read. The default report assembly is the truss format: severity-grouped
findings with CONFIRMED/PLAUSIBLE verdicts, the five standing assessments
present even on clean audits, and a mechanical verdict mapping
(`mechanical_verdict` below) applied by the engine, not hoped for in prose.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, TypedDict

from langgraph.graph import END, START, StateGraph

from rework.kernel.state.records import EventRecord, EventType
from rework.nodes.report_sections import (
    ASSEMBLY_AUDIT,
    mechanical_verdict,
    resolve_sections,
)
from rework.nodes.report_writer import make_report_writer_node
from rework.nodes.toolloop import make_toolloop_node
from rework.nodes.tools import LocalExecutor
from rework.roles.base import RoleBuildCtx, RoleSpec

if TYPE_CHECKING:
    from langgraph.graph.state import CompiledStateGraph

__all__ = ["AuditorSpec", "mechanical_verdict"]


class AuditorState(TypedDict, total=False):
    objective: str
    messages: list[dict[str, Any]]
    loop_turns: int
    final_text: str
    artifact: dict[str, Any] | None
    artifact_error: str | None


class AuditorSpec(RoleSpec):
    name = "auditor"
    description = (
        "Reviews work products (code diffs first) for correctness, security,"
        " proportionality, and coverage; general evaluator for any artifact."
    )
    goal = "Find issues before they reach production."
    default_model = "pro"  # audit on a stronger tier: misses cost more than the tokens
    context_manifest: ClassVar[set[str]] = {
        "diff",
        "brief",
        "investigation",
        "prior_dispositions",
        "worktree_path",
    }

    def build_graph(self, ctx: RoleBuildCtx) -> CompiledStateGraph:
        run_ctx = ctx.run.context or {}
        sections = resolve_sections(run_ctx.get("requested_sections", list(ASSEMBLY_AUDIT)))
        catalog_id = str(ctx.run.model_ref.get("id") or self.default_model)

        workspace = run_ctx.get("worktree_path") or ctx.mission.repo_path
        if workspace:
            executor = LocalExecutor(Path(workspace))
        else:
            executor = LocalExecutor(ctx.workdir)  # degenerate: audit with no repo access

        system_prompt = (
            f"GOAL: {self.goal}\n\n"
            "You are the auditor role in a multi-agent pipeline. Review the"
            " change you are given: read the code, trace consequences, and"
            " report findings honestly.\n\n"
            "Rules: findings carry file:line evidence you actually read (never"
            " invented); verdict CONFIRMED only when you traced the consequence,"
            " PLAUSIBLE when it is a suspicion; do not nitpick: if the change"
            " does what it set out to do with no serious issues, say so;"
            " the brief and any prior dispositions are author narrative and"
            " untrusted claims, ground truth is the code and test runs; do not"
            " re-raise findings already dispositioned in the context unless you"
            " have new evidence (say so and cite it); do not modify files."
        )

        async def prepare(state: AuditorState) -> dict[str, Any]:
            parts: list[str] = []
            brief = str(run_ctx.get("brief") or state["objective"])
            parts.append(f"Mission brief (what this change was supposed to do): {brief}")
            if run_ctx.get("investigation"):
                parts.append(
                    "Investigator's findings on the original issue:\n"
                    + str(run_ctx["investigation"])[:6000]
                )
            if run_ctx.get("diff"):
                diff = str(run_ctx["diff"])
                parts.append(f"The diff under review:\n```diff\n{diff[:60_000]}\n```")
            if run_ctx.get("prior_dispositions"):
                parts.append(
                    "Prior dispositions (do not re-raise without new evidence):\n"
                    + str(run_ctx["prior_dispositions"])[:6000]
                )
            parts.append(
                "Audit the change: read the touched files in the workspace for"
                " full context, run the tests if useful, then report."
            )
            await ctx.bus.publish(
                EventRecord(
                    mission_id=ctx.mission.id,
                    run_id=ctx.run.id,
                    type=EventType.PROGRESS,
                    payload={"note": "audit context assembled"},
                )
            )
            return {"messages": [{"role": "user", "content": "\n\n".join(parts)}]}

        loop = make_toolloop_node(
            provider=ctx.provider,
            catalog=ctx.catalog,
            catalog_id=catalog_id,
            executor=executor,
            system_prompt=system_prompt,
            store=ctx.store,
            mission_id=ctx.mission.id,
            run_id=ctx.run.id,
            budget=ctx.budget,
        )

        report = make_report_writer_node(
            provider=ctx.provider,
            catalog=ctx.catalog,
            catalog_id=catalog_id,
            store=ctx.store,
            mission_id=ctx.mission.id,
            run_id=ctx.run.id,
            sections=sections,
            goal=self.goal,
        )

        graph = StateGraph(AuditorState)
        graph.add_node("prepare", prepare)
        graph.add_node("loop", loop)  # type: ignore[arg-type]  # plain-callable node factory
        graph.add_node("report", report)  # type: ignore[arg-type]
        graph.add_edge(START, "prepare")
        graph.add_edge("prepare", "loop")
        graph.add_edge("loop", "report")
        graph.add_edge("report", END)
        return graph.compile()
