"""Surveyor: reads a codebase cold and returns a cited, structured map.
Genuinely read-only: the tool surface is the read-only wrapper (nodes/readonly),
mounted outermost: structured tools only (no shell), each building its own argv,
so it cannot write, edit, push or publish whatever the model tries.
Surveyor maps, auditor verifies another agent's diff, coder changes.

Graph: mechanical repo-map provision (zero LLM), free agentic tool loop over
read-only tools, rigid report-writer over the survey sections, then a
mechanical finalize that caps risks plus gaps and guarantees a report body.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, ClassVar, TypedDict

from langgraph.graph import END, START, StateGraph

from rework.kernel.state.records import EventRecord, EventType
from rework.nodes.readonly import ReadOnlyExecutor
from rework.nodes.repo_map import build_repo_map
from rework.nodes.report_sections import ASSEMBLY_SURVEYOR, resolve_sections
from rework.nodes.report_writer import make_report_writer_node
from rework.nodes.toolloop import make_toolloop_node
from rework.nodes.tools import LocalExecutor
from rework.roles.base import RoleBuildCtx, RoleSpec

if TYPE_CHECKING:
    from langgraph.graph.state import CompiledStateGraph

MAX_RISKS_AND_GAPS = 40
_SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}


class SurveyorState(TypedDict, total=False):
    objective: str
    repo_map: str
    messages: list[dict[str, Any]]
    loop_turns: int
    final_text: str
    artifact: dict[str, Any] | None
    artifact_error: str | None


def _rank(risk: Any) -> int:
    sev = str(risk.get("severity", "")).lower() if isinstance(risk, dict) else ""
    return _SEVERITY_RANK.get(sev, len(_SEVERITY_RANK))


def cap_risks_and_gaps(artifact: dict[str, Any]) -> dict[str, Any]:
    """Cap risks plus gaps at MAX_RISKS_AND_GAPS: worst risks first, then gaps.
    When the cap bites the artifact says so (`cut_note`): a silent cut would
    read as a complete survey."""
    risks = artifact.get("risks")
    gaps = artifact.get("gaps")
    risks = sorted(risks, key=_rank) if isinstance(risks, list) else []
    gaps = list(gaps) if isinstance(gaps, list) else []
    total = len(risks) + len(gaps)
    out = dict(artifact)
    kept_risks = risks[:MAX_RISKS_AND_GAPS]
    kept_gaps = gaps[: max(0, MAX_RISKS_AND_GAPS - len(kept_risks))]
    if "risks" in artifact:
        out["risks"] = kept_risks
    if "gaps" in artifact:
        out["gaps"] = kept_gaps
    dropped = total - len(kept_risks) - len(kept_gaps)
    if dropped > 0:
        out["cut_note"] = (
            f"{dropped} of {total} risks and gaps were cut at the cap of"
            f" {MAX_RISKS_AND_GAPS} (worst risks kept first, then gaps)."
        )
    return out


def _bullets(items: Any, keys: tuple[str, ...]) -> list[str]:
    lines: list[str] = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict):
            head = " ".join(str(item[k]) for k in keys if item.get(k))
            lines.append(f"- {head}")
        else:
            lines.append(f"- {item}")
    return lines


def render_survey_md(artifact: dict[str, Any]) -> str:
    """The markdown body rebuilt from the structured fields (used when the
    writer left `report` empty, and to carry the cut note)."""
    lines = [f"# {artifact.get('title') or 'Survey'}", "", str(artifact.get("summary") or "")]
    for key, head, fields in (
        ("layout", "Layout", ("path", "purpose")),
        ("entry_points", "Entry points", ("name", "where", "purpose")),
        ("dependencies", "Dependencies", ("name", "kind", "purpose")),
        ("config", "Configuration", ("name", "where", "purpose")),
        ("risks", "Risks", ("severity", "area", "detail", "where")),
        ("gaps", "Gaps", ()),
        ("next_steps", "Next steps", ()),
    ):
        body = _bullets(artifact.get(key), fields)
        if body:
            lines += ["", f"## {head}", *body]
    return "\n".join(lines).strip()


def finalize_survey(artifact: dict[str, Any]) -> dict[str, Any]:
    out = cap_risks_and_gaps(artifact)
    report = out.get("report")
    if not isinstance(report, str) or not report.strip():
        report = render_survey_md(out)
    if out.get("cut_note"):
        report = f"{report.rstrip()}\n\n_{out['cut_note']}_"
    out["report"] = report
    return out


class SurveyorSpec(RoleSpec):
    name = "surveyor"
    description = (
        "Reads and maps a codebase cold and returns a cited survey: layout, entry"
        " points, dependencies, config, tests, CI, risks and gaps, each with"
        " file:line. Read-only: never changes code and cannot publish. Use it to"
        " understand, audit or assess a codebase; use `auditor` to check a diff."
    )
    goal = (
        "Survey a codebase you have never seen: read it, map it, and report what"
        " is there with file:line citations. Change nothing."
    )
    default_model = ""
    context_manifest: ClassVar[set[str]] = {"brief", "investigation"}

    def build_graph(self, ctx: RoleBuildCtx) -> CompiledStateGraph:
        catalog_id = self.resolve_model(ctx)
        run_ctx = ctx.run.context or {}
        sections = resolve_sections(list(ASSEMBLY_SURVEYOR))

        system_prompt = (
            f"GOAL: {self.goal}\n\n"
            "You are the surveyor role in a multi-agent pipeline. A separate"
            " writer stage turns your transcript into the structured survey, so"
            " your job is the survey itself: read the workspace and learn what is"
            " there.\n\n"
            "You are READ-ONLY and must never change anything: do not create,"
            " edit, move or delete files, do not commit, push or open a pull"
            " request, and do not implement or prototype a fix. You have NO shell."
            " Your only tools are read_file, list_dir, search_code (text search),"
            " repo_tree (file tree) and git_read (git log, blame, show, diff,"
            " ls-files, ls-tree, status, rev-parse, shortlog, describe). They are"
            " structured and read-only, paths must be relative to the workspace,"
            " and any other tool name is refused. You are not the auditor: you do"
            " not verify another agent's diff or decide about shipping.\n\n"
            "Cover: layout, entry points, dependencies, configuration, tests, CI,"
            " then risks and gaps. Evidence over claims: cite file path AND line"
            " numbers you actually read; never invent paths, symbols or findings;"
            " when you did not read something, say so as a gap. Start from the"
            " workspace map, read the manifests, entry points and CI config first,"
            " and sample the rest rather than reading everything. Stop once the"
            " map is complete."
        )

        async def prepare(state: SurveyorState) -> dict[str, Any]:
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
            parts = [
                f"Survey brief: {brief}",
                f"Workspace root (your cwd, the mission's checkout): {ctx.read_root}",
                f"Workspace map:\n{repo_map}",
            ]
            if run_ctx.get("investigation"):
                parts.append(
                    "An investigator's earlier findings (verify, do not trust):\n"
                    + str(run_ctx["investigation"])[:6000]
                )
            return {
                "repo_map": repo_map,
                "messages": [{"role": "user", "content": "\n\n".join(parts)}],
            }

        loop = make_toolloop_node(
            provider=ctx.provider,
            catalog=ctx.catalog,
            catalog_id=catalog_id,
            # outermost: the read-only filter must wrap every other wrapper
            executor=ReadOnlyExecutor(
                LocalExecutor(ctx.read_root, mission_id=ctx.mission.id), ctx.read_root
            ),
            system_prompt=system_prompt,
            store=ctx.store,
            mission_id=ctx.mission.id,
            run_id=ctx.run.id,
            budget=ctx.budget,
            effort=ctx.run.model_ref.get("effort"),
            bus=ctx.bus,
            resume_file=ctx.workdir / "loop.json",
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

        async def finalize(state: SurveyorState) -> dict[str, Any]:
            artifact = state.get("artifact")
            if not isinstance(artifact, dict):
                return {}
            return {"artifact": finalize_survey(artifact)}

        graph = StateGraph(SurveyorState)
        graph.add_node("prepare", prepare)
        graph.add_node("loop", loop)  # type: ignore[arg-type]  # node factories are plain callables
        graph.add_node("report", report)  # type: ignore[arg-type]
        graph.add_node("finalize", finalize)
        graph.add_edge(START, "prepare")
        graph.add_edge("prepare", "loop")
        graph.add_edge("loop", "report")
        graph.add_edge("report", "finalize")
        graph.add_edge("finalize", END)
        return graph.compile()
