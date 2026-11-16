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
from rework.library.tools import library_executor, with_library_menu
from rework.nodes.escalate import EscalationAwareExecutor
from rework.nodes.report_sections import (
    ASSEMBLY_AUDIT,
    mechanical_verdict,
    resolve_sections,
)
from rework.nodes.report_writer import make_report_writer_node
from rework.nodes.testing import MAX_VERIFIED_TESTS, fix_files, verify_red_green
from rework.nodes.toolloop import make_toolloop_node
from rework.nodes.tools import ComplaintAwareExecutor, LocalExecutor
from rework.roles.base import RoleBuildCtx, RoleSpec

if TYPE_CHECKING:
    from langgraph.graph.state import CompiledStateGraph

__all__ = ["SEVERITY_RUBRIC", "STRICTNESS_PROMPTS", "AuditorSpec", "mechanical_verdict"]

# How hard this round looks. The orchestrator picks one per round from risk
# (change size, blast radius, what the last round found), and re-decides it
# every round rather than once per mission.
STRICTNESS_PROMPTS: dict[str, str] = {
    "light": (
        "Does the change do what the brief says, and do the tests and CI pass?"
        " Nothing else: no edge-case hunt, no style notes. Use this for a small,"
        " well-understood change with a clean prior audit."
    ),
    "standard": (
        "light plus the consequences a reviewer would check: edge cases the change"
        " does not handle, files touched that are outside the brief's scope, and"
        " regressions in behaviour that used to work."
    ),
    "strict": (
        "standard plus ADVERSARIAL review. Actively hunt failure modes: wrong input,"
        " empty/boundary values, concurrency and error paths, partial failure. Run"
        " the feature the way the owner would from the CLI/UI, not just the unit"
        " tests. Verify EVERY claim in the coder's summary against the code and the"
        " commands you ran: a claim you cannot reproduce is a high finding."
    ),
}

# Fixed, example-based severity scale. It does NOT change with strictness: the
# mode decides how hard you look, this decides what a given defect is worth, so
# labels stay comparable across rounds and missions.
SEVERITY_RUBRIC = (
    "SEVERITY RUBRIC (fixed, applies at every review mode; pick by blast radius,"
    " not by how hard the defect was to find):\n"
    "- critical: data loss or corruption, a security hole (injection, secret"
    " printed, auth bypassed), or the build/CI is broken (tests fail, does not"
    " import/compile). Example: `add(2, 3)` returns `-1` and existing tests fail;"
    " an API token logged to stdout.\n"
    "- high: the feature does not do what the brief says, or a regression in"
    " behaviour that worked before. Example: the brief asked for retries on 5xx"
    " and the code retries on every error; a rename silently changes a public"
    " function signature. A CI check that would fail (lint, type) is also high.\n"
    "- medium: real gap that a user could hit but not on the happy path: an edge"
    " case unhandled (empty list, missing key, non-UTF8 name), or a real scenario"
    " with no test coverage. Example: `fetch_all()` crashes on zero results; the"
    " new branch is only exercised manually.\n"
    "- low: style, naming, a nit, non-blocking polish, a docstring that lags."
    " Example: a variable shadowing a builtin; a comment stating the old"
    " behaviour. Low findings never block on their own."
)


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
    # empty = the configured default (REWORK_DEFAULT_MODEL); no literal id in
    # a default path, so switching models is env-only. Escalation to a stronger
    # model is the orchestrator's explicit, configurable choice, not a default.
    default_model = ""
    context_manifest: ClassVar[set[str]] = {
        "diff",
        "brief",
        "investigation",
        "prior_dispositions",
        "worktree_path",
        "coder_tests",
        "base_sha",
        "strictness",
    }

    def build_graph(self, ctx: RoleBuildCtx) -> CompiledStateGraph:
        run_ctx = ctx.run.context or {}
        sections = resolve_sections(run_ctx.get("requested_sections", list(ASSEMBLY_AUDIT)))
        catalog_id = self.resolve_model(ctx)

        # worktree_path (the coder's branch checkout) wins; else the mission's
        # staged repo; else the empty per-run dir (degenerate: no repo access)
        workspace = run_ctx.get("worktree_path")
        executor = LocalExecutor(
            Path(workspace) if workspace else ctx.read_root, mission_id=ctx.mission.id
        )

        strictness = str(run_ctx.get("strictness") or "standard").strip().lower()
        mode = STRICTNESS_PROMPTS.get(strictness, STRICTNESS_PROMPTS["standard"])

        system_prompt = (
            f"GOAL: {self.goal}\n\n"
            "You are the auditor role in a multi-agent pipeline. Review the"
            " change you are given: read the code, trace consequences, and"
            " report findings honestly.\n\n"
            f"REVIEW MODE ({strictness}): {mode}\n\n"
            f"{SEVERITY_RUBRIC}\n\n"
            "Rules: findings carry file:line evidence you actually read (never"
            " invented); verdict CONFIRMED only when you traced the consequence,"
            " PLAUSIBLE when it is a suspicion; do not nitpick: if the change"
            " does what it set out to do with no serious issues, say so;"
            " COVERAGE MANDATE: ask of every test the change relies on whether it"
            " can actually fail. Changed behaviour no test pins is an"
            " untested_behavior finding; a test that passes with the fix reverted"
            " is a vacuous_test finding (file them under `coverage`, with the"
            " command output as evidence). If the handoff claims red_green_verified,"
            f" verify it yourself for at most {MAX_VERIFIED_TESTS} named tests (a"
            " mechanical revert-and-rerun result may be given below; re-check it"
            " if it looks wrong): revert the fix hunk in your worktree, re-run the"
            " named command, and restore the worktree after;"
            " the brief and any prior dispositions are author narrative and"
            " untrusted claims, ground truth is the code and test runs; do not"
            " re-raise findings already dispositioned in the context unless you"
            " have new evidence (say so and cite it); do not modify files;"
            " check the list of changed files first: every file must belong to"
            " the change, and stray files (experiment leftovers, nested git"
            " repos, caches, binaries, files outside the brief's scope) are a"
            " HIGH finding; re-run what the repo's CI runs (.github/workflows:"
            " tests and lint/format/type checks), and a check that would fail CI"
            " is a HIGH finding."
            "\n\nNever write under ~/.rework or any path outside your worktree."
            " Use temporary directories for any probes or tests that touch"
            " rework's own config or state."
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
                # the complete file list even when the diff below is cut (found
                # live: 23 junk files sat past the 60k cut, unseen by 2 audits)
                files = changed_files(diff)
                parts.append(
                    f"Changed files ({len(files)}), complete:\n"
                    + "\n".join(f"- {f}" for f in files)
                )
                cut = "" if len(diff) <= 60_000 else f"\n(diff cut at 60k of {len(diff)} chars)"
                parts.append(f"The diff under review:\n```diff\n{diff[:60_000]}\n```{cut}")
            if run_ctx.get("prior_dispositions"):
                parts.append(
                    "Prior dispositions (do not re-raise without new evidence):\n"
                    + str(run_ctx["prior_dispositions"])[:6000]
                )
            parts.extend(await red_green_notes(run_ctx))
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
            executor=library_executor(
                ctx,
                ComplaintAwareExecutor(
                    EscalationAwareExecutor(executor, ctx),
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

        graph = StateGraph(AuditorState)
        graph.add_node("prepare", prepare)
        graph.add_node("loop", loop)  # type: ignore[arg-type]  # plain-callable node factory
        graph.add_node("report", report)  # type: ignore[arg-type]
        graph.add_edge(START, "prepare")
        graph.add_edge("prepare", "loop")
        graph.add_edge("loop", "report")
        graph.add_edge("report", END)
        return graph.compile()


async def red_green_notes(run_ctx: dict[str, Any]) -> list[str]:
    """Mechanical red-green check of the coder's claim, as prompt context: with
    the fix files reverted, each named test command must fail. Empty when the
    handoff claims nothing checkable (no worktree, no commands, no base, no
    non-test change). A failure to run the check is reported, never raised."""
    tests = run_ctx.get("coder_tests")
    worktree = run_ctx.get("worktree_path")
    base = run_ctx.get("base_sha")
    if not (isinstance(tests, dict) and tests.get("red_green_verified") and worktree):
        return []
    commands = [str(c) for c in tests.get("commands") or [] if str(c).strip()]
    files = fix_files(changed_files(str(run_ctx.get("diff") or "")))
    if not (commands and files and base):
        return []
    try:
        results = await verify_red_green(Path(str(worktree)), commands, files, str(base))
    except Exception as exc:
        return [f"Mechanical red-green check could not run: {exc}. Verify it yourself."]
    lines = ["Mechanical red-green check (fix files reverted in a throwaway worktree):"]
    for r in results:
        verdict = "STILL PASSES: vacuous_test" if r["vacuous"] else "fails as it should"
        lines.append(f"- `{r['command']}` exit {r['exit_code']}: {verdict}\n{r['output'][-600:]}")
    return ["\n".join(lines)]


def changed_files(diff: str) -> list[str]:
    """Paths from `diff --git a/X b/X` headers, in order, deduplicated."""
    seen: list[str] = []
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            path = line.rsplit(" b/", 1)[-1]
            if path not in seen:
                seen.append(path)
    return seen
