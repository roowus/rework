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


@app.command()
def new(
    objective: str,
    repo: str | None = typer.Option(
        None,
        help="A local path (staged where rework new runs), a git URL (the daemon"
        " clones it), or a repo NAME defined on --project.",
    ),
    repo_dir: str | None = typer.Option(
        None,
        help="A local folder of non-git working files: packed and uploaded"
        " to the daemon (ADR 0005 upload transport).",
    ),
    project: str | None = typer.Option(None, help="Project id (p-...) to attach."),
    plan: str = typer.Option("echo", help="echo | fix | fix-pr"),
) -> None:
    """Create a mission; the daemon picks it up. `--plan fix` runs the
    investigate -> code -> audit pipeline with a ship gate; `fix-pr` swaps the
    ship gate for an open-PR gate (branch push + gh pr create)."""

    async def go() -> str:
        from rework.missions import create_mission, resolve_project

        project_id = await resolve_project(project)
        mission = await create_mission(
            objective, repo=repo, repo_dir=repo_dir, project_id=project_id, plan=plan
        )
        return mission.id

    try:
        typer.echo(asyncio.run(go()))
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


project_app = typer.Typer(no_args_is_help=True, add_completion=False, help="Projects (ADR 0005).")
app.add_typer(project_app, name="project")


@project_app.command("create")
def project_create(
    name: str,
    transport: str = typer.Option("git", help="git (daemon clones origins) | upload"),
    results: str = typer.Option("push_branch", help="push_branch | fetch | both"),
    # typer needs list for repeatable options (tuple[str, ...] chokes on the
    # Ellipsis); ruff B008 flags any list-annotated default, so noqa is the
    # only clean escape (matches the existing ASYNC240 noqa above)
    repo: list[str] | None = typer.Option(  # noqa: B008
        None,
        help="Repo reference NAME=URL, repeatable. A URL marks it github-kind;"
        " a local path marks it path-kind (the daemon must see that path).",
    ),
) -> None:
    """Create a project: a label with settings and zero-or-more repo refs."""

    async def go() -> str:
        from rework.config import Config, open_store_from_config
        from rework.kernel.state.records import Project
        from rework.missions import is_git_url as missions_is_git_url

        repos: list[dict] = []
        for spec in repo or []:
            if "=" not in spec:
                raise typer.BadParameter(f"--repo wants NAME=URL, got {spec!r}")
            rname, url = spec.split("=", 1)
            kind = "github" if missions_is_git_url(url) else "path"
            repos.append({"name": rname, "kind": kind, "url": url})
        cfg = Config.from_env()
        store = await open_store_from_config(cfg)
        try:
            settings = {"transport": transport, "results": results, "repos": repos}
            p = await store.create_project(Project(name=name, settings=settings))
            return p.id
        finally:
            await store.aclose()

    typer.echo(asyncio.run(go()))


@project_app.command("ls")
def project_ls() -> None:
    """List projects, newest first."""

    async def go() -> list[str]:
        from rework.config import Config, open_store_from_config

        store = await open_store_from_config(Config.from_env())
        try:
            rows = []
            for p in await store.list_projects():
                repos = (p.settings or {}).get("repos") or []
                rows.append(
                    f"{p.id}  {p.name}  transport={(p.settings or {}).get('transport', 'git')}"
                    f"  repos={len(repos)}"
                )
            return rows
        finally:
            await store.aclose()

    for line in asyncio.run(go()):
        typer.echo(line)


@project_app.command("show")
def project_show(project_id: str) -> None:
    """One project's full settings."""

    async def go() -> str:
        import json as _json

        from rework.config import Config, open_store_from_config

        store = await open_store_from_config(Config.from_env())
        try:
            p = await store.get_project(project_id)
        finally:
            await store.aclose()
        if p is None:
            raise typer.BadParameter(f"project not found: {project_id}")
        return f"{p.id}  {p.name}\n{_json.dumps(p.settings, indent=2)}"

    typer.echo(asyncio.run(go()))


@app.command()
def fetch(
    mission_id: str,
    out: str | None = typer.Option(
        None, help="Directory to extract into (default ./<mission-id>)."
    ),
) -> None:
    """Download the mission's final worktree (ADR 0005 fetch results path)."""
    import os as _os
    import tarfile as _tarfile

    import httpx as _httpx

    from rework.orchestrator.workspace import workspace_url_from_env

    token = _os.environ.get("REWORK_TOKEN")
    url = f"{workspace_url_from_env().rstrip('/')}/workspaces/results/{mission_id}"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    resp = _httpx.get(url, headers=headers, timeout=300.0)
    if resp.status_code == 404:
        typer.secho(
            f"nothing to fetch for {mission_id} (no finished worktree)",
            fg=typer.colors.RED,
        )
        raise typer.Exit(1)
    if resp.status_code != 200:
        typer.secho(f"fetch failed ({resp.status_code}): {resp.text[:200]}", fg=typer.colors.RED)
        raise typer.Exit(1)
    dest = Path(out or mission_id)
    dest.mkdir(parents=True, exist_ok=True)
    import io as _io

    with _tarfile.open(fileobj=_io.BytesIO(resp.content), mode="r:gz") as tar:
        tar.extractall(dest, filter="data")
    typer.echo(f"extracted to {dest}/")


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
    """Findings as severity-grouped narrative blocks (the truss shape the
    owner picked): one render per finding, no parallel table repeating the
    same titles (the clutter complaint)."""
    if not findings:
        return ["_None. Explicitly clean, not silence._"]
    lines: list[str] = []
    groups: dict[str, list[tuple[int, dict[str, Any]]]] = {
        "Critical": [],
        "Important": [],
        "Minor": [],
    }
    for i, f in enumerate(findings, 1):
        groups[_truss_group(str(f.get("severity", "low")))].append((i, f))
    for label in ("Critical", "Important", "Minor"):
        group = groups[label]
        if not group:
            continue
        lines.append(f"**{label}**")
        lines.append("")
        for i, f in group:
            meta = f" ({f.get('verdict')}, conf {f.get('confidence')})"
            lines.append(f"**F{i}. {f.get('title')}**{meta}")
            lines.append("")
            lines.append(str(f.get("detail", "")))
            lines.append("")
            evidence = f.get("evidence") or []
            if evidence:
                lines.append("Evidence: " + " · ".join(f"`{e}`" for e in evidence))
                lines.append("")
    return lines


def _md_section(key: str, artifact: dict[str, Any]) -> list[str]:
    """One report section rendered to md. Keys mirror SECTION_REGISTRY; the
    caller decides WHICH sections and in what order (the artifact's
    sections_order, else the role assembly)."""
    out: list[str] = []
    if key == "summary":
        out.append(str(artifact.get("summary", "")))
        out.append("")
        return out
    if key == "findings":
        out.extend(_md_findings(artifact.get("findings", [])))
        return out
    if key == "suggested_fix":
        out.append(str(artifact.get("suggested_fix", "")))
        out.append("")
        return out
    if key == "proposed_tests":
        for t in artifact.get("proposed_tests", []):
            out.append(f"- **{t.get('name')}:** {t.get('exercises')}")
        out.append("")
        return out
    if key == "ruled_out":
        for r in artifact.get("ruled_out", []):
            out.append(f"- **{r.get('area')}:** {r.get('evidence')}")
        out.append("")
        return out
    if key == "classification":
        c = artifact.get("classification") or {}
        out.append(f"- **complexity:** {c.get('complexity', '?')}")
        out.append(f"- **priority:** {c.get('priority', '?')}")
        if c.get("why"):
            out.append(f"- **why:** {c.get('why')}")
        out.append("")
        return out
    if key == "standing_assessments":
        for k, v in (artifact.get("standing_assessments") or {}).items():
            out.append(f"**{k}:** {v}")
            out.append("")
        return out
    if key == "warnings":
        for w in artifact.get("warnings", []):
            out.append(f"- {w}")
        out.append("")
        return out
    if key == "dispositions":
        for d in artifact.get("dispositions", []):
            out.append(f"- `{d.get('finding_id')}` **{d.get('action')}** — {d.get('reason')}")
        out.append("")
        return out
    if key == "blockers":
        for i, b in enumerate(artifact.get("blockers", []), 1):
            out.append(f"**Blocker {i}: {b.get('question')}**")
            out.append("")
            out.append(f"- options: {' / '.join(b.get('options', []))}")
            out.append(f"- recommendation: {b.get('recommendation')}")
            out.append(f"- why it cannot self-answer: {b.get('why_cannot_answer')}")
            out.append("")
        return out
    return out


# presentation order per role when the artifact carries no sections_order
_ROLE_DEFAULT_ORDER: dict[str, tuple[str, ...]] = {
    "investigator": (
        "summary",
        "findings",
        "suggested_fix",
        "proposed_tests",
        "ruled_out",
        "blockers",
    ),
    "auditor": ("summary", "findings", "standing_assessments", "warnings", "blockers"),
}
_SECTION_TITLES = {
    "summary": "Summary",
    "findings": "Findings",
    "suggested_fix": "Suggested fix",
    "proposed_tests": "Proposed tests",
    "ruled_out": "Ruled out",
    "classification": "Classification",
    "standing_assessments": "Standing assessments",
    "warnings": "Warnings (coverage limits)",
    "dispositions": "Dispositions",
    "blockers": "Blockers (need an answer)",
}


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
    header line, context paragraph, then the model's sections_order (else the
    audit assembly): findings grouped by severity as numbered narrative blocks,
    assessments, warnings, dispositions, blockers, machine-readable block."""
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

    from rework.nodes.report_sections import SECTION_REGISTRY, resolve_order

    assembly = ("summary", "findings", "standing_assessments", "warnings", "blockers")
    sections = [SECTION_REGISTRY[k] for k in assembly]
    order = [k for k in resolve_order(artifact, sections) if k != "summary"]

    for key in order:
        out.append(f"### {_SECTION_TITLES.get(key, key)}")
        out.append("")
        out.extend(_md_section(key, artifact))

    # machine-readable block: the same audit as data; must agree with the prose
    machine: dict[str, Any] = {
        "version": 1,
        "head": head,
        "verdict": mechanical_verdict(artifact),
        "findings": artifact.get("findings", []),
        "warnings": artifact.get("warnings", []),
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
        if role == "coder":
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
            stats = artifact.get("run_stats")
            if stats:
                out.append("### Run stats (mechanical, from the ledger)")
                out.append("")
                out.extend(_md_run_stats(stats))
            continue
        if role == "auditor":
            out.extend(_md_audit_truss(artifact, stats=artifact.get("run_stats", {}), run=run))
            continue
        # report roles: the model chose the section set AND the order (its
        # sections_order), sanitized against the role's default assembly
        from rework.nodes.report_sections import (
            SECTION_REGISTRY,
            resolve_order,
        )

        sections = [
            SECTION_REGISTRY[k] for k in _ROLE_DEFAULT_ORDER.get(role, ()) if k in SECTION_REGISTRY
        ]
        order = resolve_order(artifact, sections)
        for key in order:
            out.append(f"### {_SECTION_TITLES.get(key, key)}")
            out.append("")
            out.extend(_md_section(key, artifact))
        stats = artifact.get("run_stats")
        if stats:
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

    from rework.config import DEFAULT_STATE_DIR, Config, load_catalog, open_store_from_config

    config = Config.from_env()
    if config.is_postgres:
        workroot = DEFAULT_STATE_DIR / "work"
    else:
        workroot = Path(config.db).expanduser().parent / "work"

    async def go() -> None:
        from rework.kernel.events.bus import InProcessEventBus
        from rework.orchestrator.daemon import (
            Daemon,
            DaemonLocked,
            checkpointer_factory_for,
        )
        from rework.orchestrator.workspace import default_bind, start_workspace_server
        from rework.roles.registry import autodiscover

        store = await open_store_from_config(config)
        bus = InProcessEventBus(store)

        # the Library (M5): best-effort at boot; a broken library must never
        # stop missions (roles run library-less when this fails)
        library = None
        try:
            from rework.library.store import GitLibrary, open_library_store
            from rework.library.tools import LibraryHandle

            index = await open_library_store(config.db)
            library = LibraryHandle(
                index=index, git=GitLibrary(_library_root(), author=config.git_author)
            )
            typer.secho(f"library at {_library_root()}", fg=typer.colors.CYAN)
        except Exception as exc:
            typer.secho(
                f"library unavailable ({exc}); roles run library-less", fg=typer.colors.YELLOW
            )

        daemon = Daemon(
            store=store,
            bus=bus,
            registry=autodiscover(),
            catalog=load_catalog(),
            make_checkpointer=checkpointer_factory_for(config),
            workroot=workroot,
            config=config,
            library=library,
        )
        # the workspace API rides the daemon's event loop (ADR 0005); binding
        # is localhost by default, tailnet-only when REWORK_HTTP_HOST points at
        # a tailnet IP
        host, port = default_bind()
        server = await start_workspace_server(store, workroot, host, port)
        typer.secho(
            f"workspace API on http://{host}:{port} (tailnet-only when the host is a tailnet IP)",
            fg=typer.colors.CYAN,
        )
        try:
            await daemon.run_forever()
        except DaemonLocked as exc:
            typer.secho(str(exc), fg=typer.colors.RED)
            raise typer.Exit(1) from exc
        finally:
            await server.cleanup()

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
        from rework.config import (
            DEFAULT_STATE_DIR,
            Config,
            load_catalog,
            open_store_from_config,
        )
        from rework.evals.harness import builtin_tasks, run_task
        from rework.orchestrator.daemon import checkpointer_factory_for
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
            if config.is_postgres:
                workroot = DEFAULT_STATE_DIR / "eval-work"
            else:
                workroot = Path(config.db).expanduser().parent / "eval-work"  # noqa: ASYNC240
            checkpointer = checkpointer_factory_for(config)
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
    import sys

    # bare `rework` (no arguments at all) opens the TUI dashboard; everything
    # else keeps the command surface (`rework --help`, `rework new ...`)
    if len(sys.argv) == 1:
        from rework.tui import run_tui

        run_tui()
        return
    app()


if __name__ == "__main__":
    main()


library_app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="The Library: cross-project skills/tools/reports/memories (M5, ADR 0001).",
)
app.add_typer(library_app, name="library")


async def open_library_index(db: str) -> Any:
    """The library index on the same DB substrate as the state store."""
    from rework.library.store import open_library_store

    return await open_library_store(db)


def _library_root() -> Path:
    import os as _os

    from rework.config import DEFAULT_STATE_DIR

    return Path(
        _os.environ.get("REWORK_LIBRARY_DIR", str(DEFAULT_STATE_DIR / "library"))
    ).expanduser()


@library_app.command("add")
def library_add(
    body_file: str = typer.Argument(..., help="Path to the markdown body file."),
    type_: str = typer.Option("skill", "--type", help="skill | tool | report | memory"),
    name: str = typer.Option(..., help="Item name (searchable)."),
    description: str = typer.Option("", help="One-line description (searchable)."),
    tag: list[str] = typer.Option([], "--tag", help="Repeatable."),  # noqa: B008
    project: str | None = typer.Option(None, help="Provenance: project id."),
    mission: str | None = typer.Option(None, help="Provenance: mission id."),
) -> None:
    """Add an item: the body file goes into the library git repo, the metadata
    into the index. Skills/reports/memories auto-accept; TOOLS always start
    proposed and need `rework library activate` (supply-chain gate)."""
    from rework.config import Config
    from rework.library.curation import contribute
    from rework.library.model import ItemType
    from rework.library.store import GitLibrary

    try:
        item_type = ItemType(type_)
    except ValueError as exc:
        raise typer.BadParameter(f"unknown type: {type_}") from exc
    src = Path(body_file).expanduser()
    if not src.is_file():
        raise typer.BadParameter(f"body file not found: {src}")

    async def go() -> str:
        lib = GitLibrary(_library_root(), author=Config.from_env().git_author)
        body = await asyncio.to_thread(src.read_text)
        index = await open_library_index(Config.from_env().db)
        try:
            item, note = await contribute(
                index,
                lib,
                type_=item_type,
                name=name,
                description=description,
                body=body,
                tags=list(tag),
                mission_id=mission,
                project=project,
            )
        finally:
            await index.aclose()
        typer.echo(note)
        return item.id

    typer.echo(asyncio.run(go()))


@library_app.command("activate")
def library_activate(item_id: str, note: str | None = typer.Option(None)) -> None:
    """The human gate for tools: promote a proposed item to active."""
    from rework.config import Config

    async def go() -> str:
        index = await open_library_index(Config.from_env().db)
        try:
            item = await index.get_item(item_id)
            if item is None:
                raise typer.BadParameter(f"item not found: {item_id}")
            item.status = item.status.__class__.ACTIVE
            item.touch()
            await index.upsert_item(item)
            return f"{item.id} is now active ({item.type.value} {item.name!r})"
        finally:
            await index.aclose()

    _ = note
    typer.echo(asyncio.run(go()))


@library_app.command("retire")
def library_retire(item_id: str) -> None:
    """The off switch: a retired item is kept for history, never served."""
    from rework.config import Config

    async def go() -> str:
        index = await open_library_index(Config.from_env().db)
        try:
            item = await index.get_item(item_id)
            if item is None:
                raise typer.BadParameter(f"item not found: {item_id}")
            item.status = item.status.__class__.RETIRED
            item.touch()
            await index.upsert_item(item)
            return f"{item.id} retired ({item.type.value} {item.name!r})"
        finally:
            await index.aclose()

    typer.echo(asyncio.run(go()))


@library_app.command("ls")
def library_ls(
    type_: str | None = typer.Option(None, "--type", help="Filter by type."),
    status: str | None = typer.Option(None, help="Filter by status."),
) -> None:
    """List library items, newest first."""

    async def go() -> list[str]:
        from rework.config import Config
        from rework.library.model import ItemStatus, ItemType

        index = await open_library_index(Config.from_env().db)
        try:
            it = ItemType(type_) if type_ else None
            st = ItemStatus(status) if status else None
            return [
                f"{item.id}  [{item.type.value}/{item.status.value}]"
                f" {item.name}: {item.description[:60]}"
                for item in await index.list_items(type_=it, status=st)
            ]
        finally:
            await index.aclose()

    for line in asyncio.run(go()):
        typer.echo(line)


@library_app.command("show")
def library_show(item_id: str) -> None:
    """One item: metadata plus its body."""
    from rework.config import Config
    from rework.library.store import GitLibrary

    async def go() -> str:
        index = await open_library_index(Config.from_env().db)
        try:
            item = await index.get_item(item_id)
        finally:
            await index.aclose()
        if item is None:
            raise typer.BadParameter(f"item not found: {item_id}")
        lib = GitLibrary(_library_root())
        body = await asyncio.to_thread(lib.read_body, item)
        meta = (
            f"{item.id}  [{item.type.value}/{item.status.value}] v{item.version}"
            f"  uses={item.uses}\n"
            f"name: {item.name}\ndescription: {item.description}\n"
            f"tags: {', '.join(item.tags) or '(none)'}\n"
            f"provenance: {item.provenance.origin.value}"
            f" mission={item.provenance.mission_id} role={item.provenance.role}\n"
        )
        return meta + "\n" + (body or "(no body)")

    typer.echo(asyncio.run(go()))


@library_app.command("search")
def library_search(
    query: str = typer.Argument(...),
    type_: str | None = typer.Option(None, "--type", help="Filter by type."),
) -> None:
    """Keyword search over names, descriptions, and tags."""

    async def go() -> list[str]:
        from rework.config import Config
        from rework.library.model import ItemType

        index = await open_library_index(Config.from_env().db)
        try:
            it = ItemType(type_) if type_ else None
            return [
                f"{item.id}  [{item.type.value}] {item.name}: {item.description[:60]}"
                for item in await index.search(query, type_=it)
            ]
        finally:
            await index.aclose()

    for line in asyncio.run(go()):
        typer.echo(line)


@app.command("library-mcp")
def library_mcp() -> None:
    """Serve the Library over MCP stdio (mount from any MCP client)."""
    from rework.library.mcp import run_stdio

    run_stdio()
