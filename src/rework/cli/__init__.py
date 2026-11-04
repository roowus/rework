"""rework CLI: serve/new/ls/attach/gates/approve. The CLI is a client: every
command opens its own store connection, so it works from any machine sharing
the database (that is the cross-device design, docs/design.md)."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import typer

from rework import __version__
from rework.nodes.report_sections import mechanical_verdict

app = typer.Typer(
    name="rework",
    help=(
        "Portable multi-agent workflow engine: an orchestrator supervising role "
        "workflows, with a compounding cross-harness library."
    ),
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def root() -> None:
    """rework: see `rework --help` for commands."""


@app.command()
def version() -> None:
    """Print the rework version."""
    typer.echo(f"rework {__version__}")


PLAN_FIX: dict = {
    "steps": [
        {
            "step": "investigate",
            "role": "investigator",
            "model": "flash",
            "context": {
                "brief": "$OBJECTIVE",
                "depth": "quick",
                "requested_sections": [
                    "summary",
                    "findings",
                    "suggested_fix",
                    "proposed_tests",
                    "blockers",
                ],
            },
        },
        {
            "step": "code",
            "role": "coder",
            "model": "flash",
            "context": {"brief": "$OBJECTIVE"},
            "context_from": {"investigation": {"step": "investigate"}},
        },
        {
            "step": "audit",
            "role": "auditor",
            "model": "pro",
            "context": {"brief": "$OBJECTIVE"},
            "context_from": {
                "diff": {"step": "code", "field": "diff"},
                "worktree_path": {"step": "code", "field": "worktree"},
                "investigation": {"step": "investigate"},
            },
            "on_request_changes": {"revise": "code", "max_rounds": 2},
        },
    ],
    "gates": ["ship"],
}

PLAN_FIX_PR: dict = {
    "steps": PLAN_FIX["steps"],
    "gates": ["open_pr"],
}


def _prepare_repo(repo_arg: str, mission_id: str) -> str:
    """A git repo is used in place; a plain directory is copied into the
    state workspace and git-initialized, so missions stay self-contained
    and the example repos are never mutated."""
    import shutil as _shutil
    import subprocess as _sp
    from pathlib import Path as _Path

    src = _Path(repo_arg).expanduser().resolve()
    if not src.is_dir():
        raise typer.BadParameter(f"repo not found: {src}")
    if (src / ".git").exists():
        return str(src)
    from rework.config import DEFAULT_STATE_DIR

    dest = DEFAULT_STATE_DIR / "work" / mission_id / "repo"
    if dest.exists():
        _shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    _shutil.copytree(
        src,
        dest,
        ignore=_shutil.ignore_patterns(".git", "__pycache__", "*.pyc", ".venv", "node_modules"),
    )

    def _git(*args: str) -> None:
        _sp.run(["git", *args], cwd=dest, check=True, capture_output=True)

    _git("init", "-b", "main")
    _git("add", "-A")
    _git("-c", "user.name=rework", "-c", "user.email=rework@rework.local", "commit", "-m", "init")
    return str(dest)


@app.command()
def new(
    objective: str,
    repo: str | None = None,
    plan: str = typer.Option("echo", help="echo | fix | fix-pr"),
) -> None:
    """Create a mission; the daemon picks it up. `--plan fix` runs the
    investigate -> code -> audit pipeline with a ship gate; `fix-pr` swaps the
    ship gate for an open-PR gate (branch push + gh pr create)."""

    async def go() -> str:
        from rework.config import Config, open_store_from_config
        from rework.kernel.events.bus import InProcessEventBus
        from rework.kernel.state.records import (
            EventRecord,
            EventType,
            Mission,
            new_id,
        )

        mission_id = new_id("m")
        repo_path = _prepare_repo(repo, mission_id) if repo else None
        store = await open_store_from_config(Config.from_env())
        try:
            bus = InProcessEventBus(store)
            plan_spec: dict | None = None
            if plan in ("fix", "fix-pr"):
                if not repo_path:
                    raise typer.BadParameter(f"--plan {plan} requires --repo")
                import json as _json

                template = PLAN_FIX_PR if plan == "fix-pr" else PLAN_FIX
                plan_spec = _json.loads(_json.dumps(template).replace("$OBJECTIVE", objective))
            mission = await store.create_mission(
                Mission(
                    id=mission_id,
                    objective=objective,
                    repo_path=repo_path,
                    plan=plan_spec,
                )
            )
            await bus.publish(
                EventRecord(
                    mission_id=mission.id,
                    type=EventType.MISSION_CREATED,
                    payload={"objective": objective, "plan": plan},
                )
            )
            return mission.id
        finally:
            await store.aclose()

    typer.echo(asyncio.run(go()))


def _fmt_run_stats(stats: dict[str, Any]) -> list[str]:
    lines = [
        "run stats (mechanical, from the ledger):",
        f"  model calls: {stats.get('model_calls', 0)}"
        f"  models: {', '.join(stats.get('models', ['?']))}",
        f"  tokens: {stats.get('input_tokens', 0):,} in /"
        f" {stats.get('output_tokens', 0):,} out"
        f"  (cached: {stats.get('cached_tokens', 0):,})",
        f"  cost: ${stats.get('cost_usd', 0.0):.4f} (catalog estimate)"
        f"  elapsed: {stats.get('elapsed_seconds', 0.0):.1f}s",
    ]
    return lines


def _render_artifact(role: str, artifact: dict[str, Any]) -> list[str]:
    out: list[str] = []
    if role == "investigator":
        out.append("SUMMARY")
        out.append(f"  {artifact.get('summary', '')}")
        findings = artifact.get("findings", [])
        if findings:
            out.append("FINDINGS")
            for f in findings:
                out.append(
                    f"  [{f.get('severity')}/{f.get('verdict')} conf {f.get('confidence')}]"
                    f" {f.get('title')}"
                )
                out.append(f"    {f.get('detail')}")
                out.append(f"    evidence: {f.get('evidence')}")
        fix = artifact.get("suggested_fix")
        if fix:
            out.append("SUGGESTED FIX")
            out.append(f"  {fix}")
        tests = artifact.get("proposed_tests", [])
        if tests:
            out.append("PROPOSED TESTS")
            for t in tests:
                out.append(f"  {t.get('name')}: {t.get('exercises')}")
        ruled = artifact.get("ruled_out", [])
        if ruled:
            out.append("RULED OUT")
            for r in ruled:
                out.append(f"  {r.get('area')}: {r.get('evidence')}")
    elif role == "coder":
        out.append("HANDOFF")
        out.append(f"  branch: {artifact.get('branch')}")
        out.append(f"  commit: {artifact.get('commit')}")
        out.append("SUMMARY (coder's own words)")
        out.append(f"  {artifact.get('summary', '')}")
        out.append("DIFF (mechanical)")
        for line in str(artifact.get("diff", "")).splitlines()[:60]:
            out.append(f"  {line}")
    elif role == "auditor":
        out.append("SUMMARY")
        out.append(f"  {artifact.get('summary', '')}")
        findings = artifact.get("findings", [])
        if findings:
            out.append("FINDINGS")
            for f in findings:
                out.append(
                    f"  [{f.get('severity')}/{f.get('verdict')}] {f.get('title')}"
                    f"  ({', '.join(f.get('evidence', [])) or 'no file evidence'})"
                )
                out.append(f"    {f.get('detail')}")
        else:
            out.append("FINDINGS: none (clean audit)")
        sa = artifact.get("standing_assessments")
        if sa:
            out.append("STANDING ASSESSMENTS")
            for k, v in sa.items():
                out.append(f"  {k}: {v}")
        dispositions = artifact.get("dispositions", [])
        if dispositions:
            out.append("DISPOSITIONS")
            for d in dispositions:
                out.append(f"  {d.get('finding_id')}: {d.get('action')} | {d.get('reason')}")
    blockers = artifact.get("blockers", [])
    if blockers:
        out.append("BLOCKERS (needs an answer)")
        for b in blockers:
            out.append(f"  Q: {b.get('question')}")
            out.append(f"     options: {b.get('options')}")
            out.append(f"     recommendation: {b.get('recommendation')}")
    stats = artifact.get("run_stats")
    if stats:
        out.append("")
        out.extend(_fmt_run_stats(stats))
    return out


def _md_findings(findings: list[dict[str, Any]]) -> list[str]:
    if not findings:
        return ["_None. Explicitly clean, not silence._"]
    lines: list[str] = [
        "| # | severity | verdict | conf | finding |",
        "|---|---|---|---|---|",
    ]
    for i, f in enumerate(findings, 1):
        lines.append(
            f"| F{i} | {f.get('severity')} | {f.get('verdict')} | {f.get('confidence')}"
            f" | {f.get('title')} |"
        )
    lines.append("")
    for i, f in enumerate(findings, 1):
        lines.append(f"#### F{i}: {f.get('title')}")
        lines.append("")
        lines.append(str(f.get("detail", "")))
        lines.append("")
        evidence = f.get("evidence") or []
        if evidence:
            lines.append("**Evidence:**")
            lines.append("")
            for e in evidence:
                lines.append(f"- `{e}`")
            lines.append("")
    return lines


def _md_blockers(blockers: list[dict[str, Any]]) -> list[str]:
    if not blockers:
        return []
    lines: list[str] = []
    for i, b in enumerate(blockers, 1):
        lines.append(f"### Blocker {i}: {b.get('question')}")
        lines.append("")
        lines.append(f"- **options:** {' / '.join(b.get('options', []))}")
        lines.append(f"- **recommendation:** {b.get('recommendation')}")
        lines.append(f"- **why it cannot self-answer:** {b.get('why_cannot_answer')}")
        lines.append("")
    return lines


def _md_run_stats(stats: dict[str, Any]) -> list[str]:
    return [
        "## Run stats (mechanical, from the ledger)",
        "",
        "| model calls | input | output | cached | cost (est) | elapsed |",
        "|---|---|---|---|---|---|",
        f"| {stats.get('model_calls', 0)} | {stats.get('input_tokens', 0):,}"
        f" | {stats.get('output_tokens', 0):,} | {stats.get('cached_tokens', 0):,}"
        f" | ${stats.get('cost_usd', 0.0):.4f} | {stats.get('elapsed_seconds', 0.0):.1f}s |",
        "",
        f"models: {', '.join(stats.get('models', ['?']))}",
        "",
    ]


_SEVERITY_GROUPS = [
    ("critical", "Critical"),
    ("high", "Important"),
    ("medium", "Minor"),
    ("low", "Minor"),
]


def _truss_group(severity: str) -> str:
    low = severity.lower()
    for needle, label in _SEVERITY_GROUPS:
        if low == needle:
            return label
    return "Minor"


def _md_audit_truss(artifact: dict[str, Any], stats: dict[str, Any], run: Any) -> list[str]:
    """The auditor section, laid out like the owner's truss pr-audit report:
    header line, context paragraph, findings grouped by severity as numbered
    narrative blocks, assessments, warnings, machine-readable block. The
    truss report is the format reference (ADR 0003); the artifact is ours."""
    out: list[str] = []
    models = ", ".join(stats.get("models", ["?"]))
    head = str(artifact.get("head") or stats.get("commit") or "")
    out.append(f"### 🔍 Audit — {models}" + (f" · head `{head[:10]}`" if head else ""))
    out.append("")

    # context paragraph: what was reviewed, its size, its verification
    summary = str(artifact.get("summary", "")).strip()
    if summary:
        out.append(summary)
        out.append("")

    # findings grouped by severity, numbered B1.. down the review
    findings = artifact.get("findings", [])
    groups: dict[str, list[tuple[int, dict[str, Any]]]] = {
        "Critical": [],
        "Important": [],
        "Minor": [],
    }
    for i, f in enumerate(findings, 1):
        groups[_truss_group(str(f.get("severity", "low")))].append((i, f))
    if not findings:
        out.append("Findings: none. Explicitly clean, not silence.")
        out.append("")
    for label in ("Critical", "Important", "Minor"):
        group = groups[label]
        if not group:
            continue
        out.append(f"**{label}**")
        out.append("")
        for i, f in group:
            loc = f.get("file") or ""
            if f.get("line"):
                loc += f":{f['line']}"
            where = f" — `{loc}`" if loc else ""
            out.append(f"**B{i}. {f.get('title')}**{where}")
            out.append("")
            out.append(str(f.get("detail", "")))
            out.append("")
            evidence = f.get("evidence") or []
            if evidence:
                out.append("Evidence: " + " · ".join(f"`{e}`" for e in evidence))
                out.append("")

    sa = artifact.get("standing_assessments")
    if sa:
        out.append("### Assessments")
        out.append("")
        for k, v in sa.items():
            out.append(f"**{k}:** {v}")
            out.append("")

    warnings = artifact.get("warnings", [])
    if warnings:
        out.append("### Warnings (coverage limits)")
        out.append("")
        for w in warnings:
            out.append(f"- {w}")
        out.append("")

    dispositions = artifact.get("dispositions", [])
    if dispositions:
        out.append("### Dispositions")
        out.append("")
        for d in dispositions:
            out.append(f"- `{d.get('finding_id')}` **{d.get('action')}** — {d.get('reason')}")
        out.append("")

    # machine-readable block: the same audit as data; must agree with the prose
    machine: dict[str, Any] = {
        "version": 1,
        "head": head,
        "verdict": mechanical_verdict(artifact),
        "findings": findings,
        "warnings": warnings,
    }
    out.append("### Machine-readable block")
    out.append("")
    out.append("```json")
    out.append(json.dumps(machine, indent=2))
    out.append("```")
    out.append("")

    out.append(
        f"_audit model: {models} · run {run.attempt if run else '?'} ·"
        f" {stats.get('model_calls', 0)} calls, {stats.get('elapsed_seconds', 0.0):.1f}s_"
    )
    out.append("")
    return out


def _render_md(runs: list[tuple[Any, dict[str, Any]]], spend: Any, mission: Any) -> str:
    wall_clock = max(0.0, mission.updated_at - mission.created_at)
    out = [
        f"# mission {mission.id}  [{mission.status.value}]",
        "",
        f"**Objective:** {mission.objective}",
        "",
        "## At a glance",
        "",
        "| step | role | attempt | status | calls | cost (est) | elapsed |",
        "|---|---|---|---|---|---|---|",
    ]
    total_calls = 0
    for run, artifact in runs:
        stats = artifact.get("run_stats", {})
        calls = stats.get("model_calls", 0)
        total_calls += calls
        out.append(
            f"| {run.step} | {run.role} | {run.attempt} | {run.status.value} | {calls}"
            f" | ${stats.get('cost_usd', 0.0):.4f} | {stats.get('elapsed_seconds', 0.0):.1f}s |"
        )
    out += [
        f"| **total** | | | | **{total_calls}** | **${spend.spent_usd:.4f}**"
        f" | **{_fmt_duration(wall_clock)}** |",
        "",
        f"tokens: {spend.input_tokens:,} in / {spend.output_tokens:,} out"
        f" (cached {spend.cached_tokens:,})",
        "",
    ]
    for run, artifact in runs:
        out.append(f"## {run.step} ({run.role}) attempt {run.attempt}: {run.status.value}")
        out.append("")
        if not artifact:
            out.append("_no artifact_")
            out.append("")
            continue
        role = run.role
        if role == "investigator":
            out.append("### Summary")
            out.append("")
            out.append(str(artifact.get("summary", "")))
            out.append("")
            out.append("### Findings")
            out.append("")
            out.extend(_md_findings(artifact.get("findings", [])))
            if artifact.get("suggested_fix"):
                out.append("### Suggested fix")
                out.append("")
                out.append(str(artifact.get("suggested_fix")))
                out.append("")
            tests = artifact.get("proposed_tests", [])
            if tests:
                out.append("### Proposed tests")
                out.append("")
                for t in tests:
                    out.append(f"- **{t.get('name')}:** {t.get('exercises')}")
                out.append("")
            ruled = artifact.get("ruled_out", [])
            if ruled:
                out.append("### Ruled out")
                out.append("")
                for r in ruled:
                    out.append(f"- **{r.get('area')}:** {r.get('evidence')}")
                out.append("")
        elif role == "coder":
            out.append("### Handoff")
            out.append("")
            out.append(f"- branch: `{artifact.get('branch')}`")
            out.append(f"- commit: `{artifact.get('commit')}`")
            out.append("")
            out.append("### Summary (coder's own words)")
            out.append("")
            out.append(str(artifact.get("summary", "")))
            out.append("")
            out.append("### Diff (mechanical)")
            out.append("")
            out.append("```diff")
            out.append(str(artifact.get("diff", "")))
            out.append("```")
            out.append("")
        elif role == "auditor":
            out.extend(_md_audit_truss(artifact, stats=artifact.get("run_stats", {}), run=run))
        out.extend(_md_blockers(artifact.get("blockers", [])))
        stats = artifact.get("run_stats")
        if stats and role != "auditor":
            out.append("### Run stats (mechanical, from the ledger)")
            out.append("")
            out.extend(_md_run_stats(stats))
    out.append("## Mission total")
    out.append("")
    out.append(
        f"**${spend.spent_usd:.4f}**  ·  {spend.input_tokens:,} in /"
        f" {spend.output_tokens:,} out  ·  cached {spend.cached_tokens:,}"
        f"  ·  wall clock {_fmt_duration(wall_clock)}"
    )
    out.append("")
    return "\n".join(out)


def _fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, sec = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m {sec:02d}s"


@app.command()
def report(
    mission_id: str,
    md: bool = typer.Option(False, "--md", help="write a markdown file and open-friendly output"),
    out: str | None = typer.Option(None, "--out", help="markdown output path (implies --md)"),
) -> None:
    """Render a mission's reports readably: each role's artifact plus the
    mechanical run-stats section (tokens, cache, cost, time). --md writes
    a markdown file (default ~/.rework/reports/<mission-id>.md)."""

    async def go() -> None:
        from rework.config import Config, open_store_from_config

        store = await open_store_from_config(Config.from_env())
        try:
            mission = await store.get_mission(mission_id)
            if mission is None:
                typer.secho(f"no such mission: {mission_id}", fg=typer.colors.RED)
                raise typer.Exit(1)
            runs = await store.list_runs(mission_id)
            prepared: list[tuple[Any, dict[str, Any]]] = []
            for run in runs:
                artifact = dict(run.artifact or {})
                if artifact and "run_stats" not in artifact:
                    # artifacts written before run_stats existed get live stats
                    from rework.nodes.run_stats import attach_run_stats

                    artifact = await attach_run_stats(store, run.id, artifact)
                prepared.append((run, artifact))
            spend = await store.mission_spend(mission_id)

            if md or out:
                from pathlib import Path as _Path

                from rework.config import DEFAULT_STATE_DIR

                path = _Path(out) if out else DEFAULT_STATE_DIR / "reports" / f"{mission_id}.md"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(_render_md(prepared, spend, mission), encoding="utf-8")
                typer.echo(f"report written: {path}")
                for run, artifact in prepared:
                    status_line = (
                        f"{run.step} ({run.role}) attempt {run.attempt}: {run.status.value}"
                    )
                    stats = (artifact or {}).get("run_stats", {})
                    typer.echo(
                        f"  {status_line}  {stats.get('model_calls', 0)} calls,"
                        f" ${stats.get('cost_usd', 0.0):.4f}"
                    )
                typer.echo(
                    f"mission total: ${spend.spent_usd:.4f}  {spend.input_tokens:,} in /"
                    f" {spend.output_tokens:,} out  (cached {spend.cached_tokens:,})"
                )
                return

            typer.echo(f"mission {mission.id}  [{mission.status.value}]")
            typer.echo(f"objective: {mission.objective}")
            if mission.result:
                typer.echo(f"result: {json.dumps(mission.result)[:200]}")
            typer.echo()
            for run, artifact in prepared:
                typer.echo(f"--- {run.step} ({run.role}) attempt {run.attempt}: {run.status.value}")
                if not artifact:
                    typer.echo("  (no artifact)")
                    continue
                for line in _render_artifact(run.role, artifact):
                    typer.echo(line)
                typer.echo()
            wall_clock = _fmt_duration(max(0.0, mission.updated_at - mission.created_at))
            typer.echo(
                f"mission total: ${spend.spent_usd:.4f}  {spend.input_tokens:,} in /"
                f" {spend.output_tokens:,} out  (cached {spend.cached_tokens:,})"
                f"  wall clock {wall_clock}"
            )
        finally:
            await store.aclose()

    asyncio.run(go())


@app.command()
def ls(limit: int = 20) -> None:
    """List missions, newest first."""

    async def go() -> list[tuple[str, str, str]]:
        from rework.config import Config, open_store_from_config

        store = await open_store_from_config(Config.from_env())
        try:
            missions = await store.list_missions(limit=limit)
            return [(m.id, m.status.value, m.objective) for m in missions]
        finally:
            await store.aclose()

    for mid, status, objective in asyncio.run(go()):
        typer.echo(f"{mid}  {status:<14}  {objective}")


@app.command()
def gates(mission_id: str | None = None) -> None:
    """List open approval gates (optionally for one mission)."""

    async def go() -> list[tuple[str, str, str]]:
        from rework.config import Config, open_store_from_config

        store = await open_store_from_config(Config.from_env())
        try:
            open_gates = await store.list_open_gates(mission_id)
            return [(g.id, g.kind.value, g.question) for g in open_gates]
        finally:
            await store.aclose()

    rows = asyncio.run(go())
    if not rows:
        typer.echo("no open gates")
        return
    for gid, kind, question in rows:
        typer.echo(f"{gid}  [{kind}]  {question}")


@app.command()
def complaints(
    status: str | None = typer.Option(
        None, help="filter: open|triaged|issue_open|resolved|wontfix"
    ),
    kind: str | None = typer.Option(
        None,
        help=(
            "filter: workflow_gripe|needed_info|tool_missing"
            "|prompt_issue|bug_verified|process_issue"
        ),
    ),
    limit: int = 30,
) -> None:
    """The complaints inbox: agent feedback on the workflow itself (ADR 0004)."""

    async def go() -> list[tuple[str, str, str, str]]:
        from rework.config import Config, open_store_from_config
        from rework.kernel.state.records import ComplaintKind, ComplaintStatus

        store = await open_store_from_config(Config.from_env())
        try:
            rows = await store.list_complaints(
                status=ComplaintStatus(status) if status else None,
                kind=ComplaintKind(kind) if kind else None,
                limit=limit,
            )
            return [(c.id, c.kind.value, c.source_role or "?", c.summary) for c in rows]
        finally:
            await store.aclose()

    try:
        rows = asyncio.run(go())
    except ValueError as exc:
        typer.secho(f"bad filter: {exc}", fg=typer.colors.RED)
        raise typer.Exit(1) from None
    if not rows:
        typer.echo("no complaints")
        return
    for cid, ckind, role, summary in rows:
        typer.echo(f"{cid}  [{ckind}]  {role}:  {summary}")


@app.command()
def approve(
    gate_id: str,
    note: str | None = None,
    reject: bool = typer.Option(False, help="reject instead of approve"),
) -> None:
    """Decide an open gate. Works from any machine sharing the database."""

    async def go() -> tuple[bool, str]:
        from rework.config import Config, open_store_from_config
        from rework.kernel.events.bus import InProcessEventBus
        from rework.kernel.gates import GateManager

        store = await open_store_from_config(Config.from_env())
        try:
            gates = GateManager(store, InProcessEventBus(store))
            gate = await gates.decide(gate_id, approved=not reject, note=note)
            if gate is None:
                return False, "gate not found"
            return True, gate.status.value
        finally:
            await store.aclose()

    ok, status = asyncio.run(go())
    if not ok:
        typer.secho(status, fg=typer.colors.RED)
        raise typer.Exit(1)
    typer.echo(f"gate {gate_id}: {status}")


@app.command()
def attach(mission_id: str, follow: bool = True) -> None:
    """Stream a mission's events (replay from the beginning, then follow)."""

    async def go() -> None:
        from rework.config import Config, open_store_from_config

        store = await open_store_from_config(Config.from_env())
        try:
            cursor = 0
            last_status = ""
            while True:
                mission = await store.get_mission(mission_id)
                if mission is None:
                    typer.secho(f"no such mission: {mission_id}", fg=typer.colors.RED)
                    return
                if mission.status.value != last_status:
                    typer.secho(f"== {mission.status.value.upper()} ==", fg=typer.colors.CYAN)
                    last_status = mission.status.value
                for event in await store.events_since(cursor, mission_id=mission_id):
                    assert event.seq is not None
                    cursor = event.seq
                    stamp = time.strftime("%H:%M:%S", time.localtime(event.ts))
                    who = event.run_id.rsplit(":", 2)[0].split(":")[-1] if event.run_id else ""
                    note = event.payload.get("note") or event.payload.get("question") or ""
                    typer.echo(f"{stamp}  {event.type.value:<16} {who:<12} {note}".rstrip())
                if not follow or mission.status.value in {"done", "failed", "cancelled"}:
                    return
                await asyncio.sleep(0.5)
        finally:
            await store.aclose()

    try:
        asyncio.run(go())
    except KeyboardInterrupt:
        typer.echo("\ndetached")


@app.command()
def serve() -> None:
    """Run the headless daemon (one per database; leader-locked)."""
    import logging
    from pathlib import Path

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    from rework.config import Config, load_catalog, open_store_from_config

    config = Config.from_env()
    workroot = Path(config.db).expanduser().parent / "work"

    async def go() -> None:
        from rework.kernel.events.bus import InProcessEventBus
        from rework.orchestrator.daemon import Daemon, DaemonLocked, sqlite_checkpointer_factory
        from rework.roles.registry import autodiscover

        store = await open_store_from_config(config)
        bus = InProcessEventBus(store)
        daemon = Daemon(
            store=store,
            bus=bus,
            registry=autodiscover(),
            catalog=load_catalog(),
            make_checkpointer=sqlite_checkpointer_factory(config.checkpoints_db),
            workroot=workroot,
            config=config,
        )
        try:
            await daemon.run_forever()
        except DaemonLocked as exc:
            typer.secho(str(exc), fg=typer.colors.RED)
            raise typer.Exit(1) from exc

    asyncio.run(go())


eval_app = typer.Typer(no_args_is_help=True, add_completion=False, help="Eval harness v1.")
app.add_typer(eval_app, name="eval")


@eval_app.command("run")
def eval_run(
    task: str | None = typer.Argument(None, help="task id (default: every builtin task)"),
) -> None:
    """Run an eval task end-to-end in this process and grade it (rung 1:
    mechanical). Cost is real when the catalog points at real models."""

    async def go() -> list[tuple[str, bool, str, str]]:
        from rework.config import Config, load_catalog, open_store_from_config
        from rework.evals.harness import builtin_tasks, run_task
        from rework.orchestrator.daemon import sqlite_checkpointer_factory
        from rework.roles.registry import autodiscover

        config = Config.from_env()
        store = await open_store_from_config(config)
        try:
            registry = autodiscover()
            tasks = builtin_tasks()
            if task:
                tasks = [t for t in tasks if t.id == task]
                if not tasks:
                    raise typer.BadParameter(f"no such task: {task}")
            workroot = Path(config.db).expanduser().parent / "eval-work"  # noqa: ASYNC240
            checkpointer = sqlite_checkpointer_factory(config.checkpoints_db)
            rows = []
            for t in tasks:
                score = await run_task(t, store, registry, load_catalog(), checkpointer, workroot)
                rows.append(
                    (t.id, score.passed, f"${score.cost_usd:.4f}", f"{score.elapsed_s:.1f}s")
                )
            return rows
        finally:
            await store.aclose()

    rows = asyncio.run(go())
    for tid, passed, cost, elapsed in rows:
        typer.secho(
            f"{'PASS' if passed else 'FAIL'}  {tid}  {cost}  {elapsed}",
            fg=typer.colors.GREEN if passed else typer.colors.RED,
        )
    typer.echo("history: rework eval history")


@eval_app.command("history")
def eval_history(task: str | None = typer.Argument(None)) -> None:
    """Score history for a task (or all tasks), oldest first."""

    async def go() -> list[dict[str, Any]]:
        from rework.config import Config, open_store_from_config
        from rework.evals.harness import score_history

        store = await open_store_from_config(Config.from_env())
        try:
            return await score_history(store, task)
        finally:
            await store.aclose()

    scores = asyncio.run(go())
    if not scores:
        typer.echo("no eval history yet")
        return
    for s in scores:
        mark = "PASS" if s.get("passed") else "FAIL"
        grade = s.get("grade") or {}
        typer.echo(
            f"{s.get('mission', '?'):<44} {mark}"
            f"  tests={grade.get('tests_pass')}  diff={grade.get('diff_present')}"
            f"  audit={grade.get('audit_verdict')}"
            f"  ${s.get('cost_usd', 0.0):.4f}"
        )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
