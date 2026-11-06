"""Investigator: brief in, structured findings out. Read-only over the workspace.

Graph (ADR 0003): mechanical repo-map provision (zero LLM), free agentic tool
loop (depth-hinted, budget-enforced), rigid report-writer over the requested
sections. Web search and library search join as tool specs at M5/M6; the loop
node does not change when they do.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, ClassVar, TypedDict

from langgraph.graph import END, START, StateGraph

from rework.kernel.state.records import EventRecord, EventType
from rework.library.tools import library_executor, with_library_menu
from rework.nodes.repo_map import build_repo_map
from rework.nodes.report_sections import resolve_sections
from rework.nodes.report_writer import make_report_writer_node
from rework.nodes.toolloop import make_toolloop_node
from rework.nodes.tools import ComplaintAwareExecutor, LocalExecutor
from rework.roles.base import RoleBuildCtx, RoleSpec

if TYPE_CHECKING:
    from langgraph.graph.state import CompiledStateGraph


class InvestigatorState(TypedDict, total=False):
    objective: str
    repo_map: str
    messages: list[dict[str, Any]]
    loop_turns: int
    final_text: str
    artifact: dict[str, Any] | None
    artifact_error: str | None


class InvestigatorSpec(RoleSpec):
    name = "investigator"
    description = (
        "Investigates a brief against a workspace and reports structured"
        " findings with evidence: issues, feasibility, unknowns."
    )
    goal = (
        "Investigate any brief: issues, feasibility, unknowns. Find as much"
        " relevant information as possible and report it honestly."
    )
    default_model = "flash"
    context_manifest: ClassVar[set[str]] = {"brief", "depth", "requested_sections"}

    def build_graph(self, ctx: RoleBuildCtx) -> CompiledStateGraph:
        catalog_id = str(ctx.run.model_ref.get("id") or self.default_model)
        run_ctx = ctx.run.context or {}
        sections = resolve_sections(run_ctx.get("requested_sections"))
        depth = str(run_ctx.get("depth") or "quick")
        depth_note = (
            "Be thorough: chase hypotheses to ground truth before concluding."
            if depth == "deep"
            else "Be efficient: stop once the brief is answered; note what you skipped."
        )

        system_prompt = (
            f"GOAL: {self.goal}\n\n"
            "You are the investigator role in a multi-agent pipeline. A separate"
            " writer stage turns your transcript into the structured report, so"
            " your job is the investigation itself: read the workspace, chase the"
            " brief, and state what you found with evidence.\n\n"
            "Rules: evidence over claims (cite file:line you actually read);"
            " never invent paths, symbols, or findings; run read-only commands"
            " (grep, git log, tests) as needed; if you cannot answer something,"
            " say so plainly so it becomes a blocker instead of a guess.\n\n"
            "Stay in your lane: do not implement, prototype or test a fix (no"
            " scratch copies, no edited files): that is the coder's job, and an"
            " auditor checks it. Describe the fix in words with exact locations"
            " and stop once the brief is answered."
        )

        async def prepare(state: InvestigatorState) -> dict[str, Any]:
            repo_map = await asyncio.to_thread(build_repo_map, ctx.read_root)
            brief = str(run_ctx.get("brief") or state["objective"])
            await ctx.bus.publish(
                EventRecord(
                    mission_id=ctx.mission.id,
                    run_id=ctx.run.id,
                    type=EventType.PROGRESS,
                    payload={"note": "repo map built"},
                )
            )
            first_message = (
                f"Investigation brief: {brief}\n\n"
                f"Workspace root (your cwd, the mission's checkout): {ctx.read_root}\n\n"
                f"Workspace map:\n{repo_map}\n\n"
                f"Requested report sections: {', '.join(s.key for s in sections)}\n"
                f"Depth guidance: {depth_note}"
            )
            return {
                "repo_map": repo_map,
                "messages": [{"role": "user", "content": first_message}],
            }

        loop = make_toolloop_node(
            provider=ctx.provider,
            catalog=ctx.catalog,
            catalog_id=catalog_id,
            executor=library_executor(
                ctx,
                ComplaintAwareExecutor(
                    LocalExecutor(ctx.read_root),
                    ctx.store,
                    ctx.mission.id,
                    ctx.run.id,
                    self.name,
                ),
            ),
            system_prompt=with_library_menu(ctx, system_prompt),
            store=ctx.store,
            mission_id=ctx.mission.id,
            run_id=ctx.run.id,
            budget=ctx.budget,
            effort=ctx.run.model_ref.get("effort"),
            bus=ctx.bus,
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

        graph = StateGraph(InvestigatorState)
        graph.add_node("prepare", prepare)
        graph.add_node("loop", loop)  # type: ignore[arg-type]  # node factories are plain callables
        graph.add_node("report", report)  # type: ignore[arg-type]
        graph.add_edge(START, "prepare")
        graph.add_edge("prepare", "loop")
        graph.add_edge("loop", "report")
        graph.add_edge("report", END)
        return graph.compile()
