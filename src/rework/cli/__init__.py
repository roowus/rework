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
from rework.budget_policy import estimate_budget
from rework.config import default_results_dir
from rework.nodes.report_sections import accepted_findings_section, mechanical_verdict

app = typer.Typer(
    name="rework",
    help=(
        "Portable multi-agent workflow engine: an orchestrator supervising role "
        "workflows, with a compounding cross-harness library."
    ),
    no_args_is_help=True,
    add_completion=False,
    # `-h` as well as `--help`: without this only the long form is registered
    # and `rework -h` was a "No such option" error (no command claims -h)
    context_settings={"help_option_names": ["-h", "--help"]},
)


@app.callback()
def root(
    target: str | None = typer.Option(
        None,
        "--target",
        help=(
            "Named connection target from ~/.rework/targets.toml (see `rework"
            " targets`). Defaults to REWORK_TARGET, then the file's `default`."
        ),
    ),
) -> None:
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
    title: str | None = typer.Option(
        None,
        help="A few words naming the mission (default: derived from the objective).",
    ),
    plan: str = typer.Option("echo", help="Playbook: echo | fix | fix-pr | draft"),
    orchestrator: str | None = typer.Option(
        None,
        help="Catalog id for the mission's orchestrator (default: REWORK_DEFAULT_MODEL).",
    ),
    effort: str | None = typer.Option(None, help="Orchestrator effort: low | medium | high"),
    budget: float | None = typer.Option(
        None,
        help="USD cap before the mission pauses to ask (default: estimated from the"
        " task's size, calibrated from what finished missions cost; a template"
        " plan with --mechanical gets no cap unless you pass one here).",
    ),
    mechanical: bool = typer.Option(
        False,
        "--mechanical",
        help="Legacy: run the canned step pipeline instead of an LLM orchestrator.",
    ),
    priority: str = typer.Option(
        "normal",
        "--priority",
        help="Pickup priority: normal | high. High missions are picked up before"
        " normal ones; the oldest first within a level.",
    ),
) -> None:
    """Create a mission; the daemon picks it up. An LLM orchestrator runs it
    (ADR 0007), following the plan as a playbook: `fix` = investigate, code,
    audit, then a ship gate; `fix-pr` opens a PR instead (no gate by default,
    unless the project's `pr_gate` setting or the orchestrator asks for one);
    `draft` is document work (research with the web tools, write files, audit
    the claims) and has no gate: no branch push and no PR."""

    async def go() -> str:
        from rework.config import Config, open_store_from_config
        from rework.missions import create_mission, resolve_project

        # a template plan has NEVER carried a cap of its own: stamping an
        # estimated one on it (see create_mission's comment) would make every
        # `--plan fix` mission pause at a burn gate it never used to have. So
        # the estimate is only for the orchestrated default path; a template
        # mission gets a cap only when the owner asked for one with --budget.
        # `--mechanical` runs the canned template pipeline; without it the plan
        # name is just the orchestrator's playbook (ADR 0007). (an unknown plan
        # is create_mission's error to raise, one place)
        template = mechanical
        store = None
        try:
            if template and budget is None:
                typer.echo(
                    "budget: no cap (a template plan runs uncapped unless --budget)", err=True
                )
                cap, why = None, ""
            else:
                store = await open_store_from_config(Config.from_env())
                # a refused cap raises HERE, before any mission row exists
                cap, why = await _opening_budget(store, objective, budget)
                # stderr on purpose: stdout is the bare mission id, so `rework
                # new ...` in a script prints exactly one machine-readable line
                typer.echo(f"budget: ${cap:.2f} ({why})", err=True)

            orch_model = None
            if not mechanical:
                orch_model = orchestrator or default_orchestrator_model()
            project_id = await resolve_project(project)
            mission = await create_mission(
                objective,
                repo=repo,
                repo_dir=repo_dir,
                project_id=project_id,
                title=title,
                plan=plan,
                priority=priority,
                orchestrator_model=orch_model,
                orchestrator_effort=effort,
                budget_usd=cap,
                store=store,
            )
        finally:
            # ALWAYS, including the refused-budget path: an open sqlite handle
            # keeps the process alive after the error is printed
            if store is not None:
                await store.aclose()
        return mission.id

    try:
        typer.echo(asyncio.run(go()))
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


async def _opening_budget(store: Any, objective: str, budget: float | None) -> tuple[float, str]:
    """(usd, why) for a new mission: explicit --budget, else the estimate for a
    task of this size, else REWORK_BUDGET_USD for a mission with no task text.

    An explicit cap always wins (the owner said a number). Otherwise the
    ESTIMATE wins and `REWORK_BUDGET_USD` is only the fallback when there is no
    text to size (an empty objective): a deployment that still carries the old
    blanket default must not flatten `rework new` back to a single number.
    The estimate is calibrated with what finished missions actually cost here
    (`typical_finished_cost`), which only nudges it inside its own band."""
    from rework.budget_policy import typical_finished_cost
    from rework.missions import BUDGET_ERROR, _cap_usd

    if budget is not None:
        usd = _cap_usd(budget)
        if usd is None:
            # refuse BEFORE the mission is created: a NaN/inf/-3 cap persisted
            # into plan["budget"] is a mission `ledger.py` can never cap
            raise ValueError(f"--budget {budget:g}: {BUDGET_ERROR}")
        return usd, "from --budget"
    history: float | None = None
    if (objective or "").strip():
        try:  # a slow/unreadable ledger must not block creating a mission
            history = await typical_finished_cost(store)
        except Exception:  # pragma: no cover - typical_finished_cost never raises
            history = None
    usd, why = estimate_budget(objective, cost_history=history)
    import os as _os

    configured = _cap_usd(_os.environ.get("REWORK_BUDGET_USD"))
    if not (objective or "").strip() and configured is not None:
        return configured, "REWORK_BUDGET_USD, the deployment default (no task text)"
    if history:
        why = f"{why}; nudged by typical finished-mission cost ${history:.2f}"
    return usd, why


def default_orchestrator_model(catalog: Any = None) -> str:
    """The configured default model (REWORK_DEFAULT_MODEL, default 'flash'):
    the same cheap strong model every role runs on, resolved against the
    catalog. Cost control starts here. `catalog` is optional so a caller that
    already loaded one (or a test) can pass it instead of re-reading
    ~/.rework/models.json."""
    from rework.config import default_model_id

    return default_model_id(catalog)


project_app = typer.Typer(no_args_is_help=True, add_completion=False, help="Projects (ADR 0005).")
app.add_typer(project_app, name="project")

mission_app = typer.Typer(
    no_args_is_help=True, add_completion=False, help="Mission owner actions (retry-pr)."
)
app.add_typer(mission_app, name="mission")


@project_app.command("assign")
def project_assign(
    project: str = typer.Argument(..., help="Project name or id."),
    missions: list[str] = typer.Argument(..., help="Mission ids to label."),  # noqa: B008
) -> None:
    """Label existing missions with a project (fixes missions created without one)."""

    async def go() -> list[str]:
        from rework.config import Config, open_store_from_config
        from rework.missions import resolve_project

        project_id = await resolve_project(project)
        assert project_id is not None
        store = await open_store_from_config(Config.from_env())
        try:
            done = []
            for mid in missions:
                if await store.update_mission(mid, project_id=project_id) is None:
                    typer.echo(f"no mission {mid}", err=True)
                else:
                    done.append(mid)
            return done
        finally:
            await store.aclose()

    try:
        labeled = asyncio.run(go())
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"{project}: {len(labeled)} mission(s) labeled")


@project_app.command("create")
def project_create(
    name: str,
    transport: str | None = typer.Option(
        None,
        help="git (daemon clones origins) | upload (daemon keeps the files)."
        " Defaults from --repo: given repos -> git, none -> upload.",
    ),
    results: str | None = typer.Option(
        None,
        help="push_branch | fetch | both. Defaults from --repo:"
        " given repos -> push_branch, none -> fetch.",
    ),
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
        from rework.missions import derive_transport
        from rework.missions import is_git_url as missions_is_git_url

        repos: list[dict] = []
        for spec in repo or []:
            if "=" not in spec:
                raise typer.BadParameter(f"--repo wants NAME=URL, got {spec!r}")
            rname, url = spec.split("=", 1)
            kind = "github" if missions_is_git_url(url) else "path"
            repos.append({"name": rname, "kind": kind, "url": url})
        # repos decide the transport (ADR 0005): a repo-less project travels by
        # upload/fetch, never git/push_branch, because it has no origin to push to
        derived_transport, derived_results = derive_transport({"repos": repos})
        if transport == "git" and not repos:
            raise typer.BadParameter("transport=git needs at least one --repo NAME=URL")
        if transport is not None and transport != derived_transport:
            # storing the contradiction made `project show` print a transport the
            # project does not have (and every reader derives it anyway): refuse
            # it instead of quietly disagreeing with the repo list
            raise typer.BadParameter(
                "--transport upload needs no --repo; drop one"
                if transport == "upload"
                else f"--transport {transport} contradicts the repos"
                f" (that project is transport={derived_transport})"
            )
        # the DERIVED pair is what gets stored, so settings never contradict the
        # repo list; an explicit --results still wins where `derive_transport`
        # honours it (a git project's push_branch | fetch | both)
        settings_transport = derived_transport
        settings_results = results or derived_results
        cfg = Config.from_env()
        store = await open_store_from_config(cfg)
        try:
            settings = {
                "transport": settings_transport,
                "results": settings_results,
                "repos": repos,
            }
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
        from rework.missions import derive_transport

        store = await open_store_from_config(Config.from_env())
        try:
            rows = []
            for p in await store.list_projects():
                repos = (p.settings or {}).get("repos") or []
                transport, results = derive_transport(p.settings)
                rows.append(
                    f"{p.id}  {p.name}  transport={transport}  results={results}"
                    f"  repos={len(repos)}"
                )
            return rows
        finally:
            await store.aclose()

    for line in asyncio.run(go()):
        typer.echo(line)


@project_app.command("add-files")
def project_add_files(
    project: str = typer.Argument(..., help="Project name or id."),
    paths: list[str] = typer.Argument(..., help="Local files/folders to send."),  # noqa: B008
    daemon_url: str | None = typer.Option(None, help="Override REWORK_DAEMON_URL."),
) -> None:
    """Copy local files into a repo-less project's files area on the daemon.
    Every mission on that project then starts from them."""

    async def go() -> tuple[str, list[str]]:
        from rework.config import Config, open_store_from_config
        from rework.missions import resolve_project

        project_id = await resolve_project(project)
        assert project_id is not None
        store = await open_store_from_config(Config.from_env())
        try:
            found = await store.get_project(project_id)
        finally:
            await store.aclose()
        from rework.missions import project_is_upload

        if found is None or not project_is_upload(found.settings):
            raise ValueError(f"project {project} is git transport: push a branch, not files")
        from rework.project_files import add_project_files

        changed = await asyncio.to_thread(add_project_files, project_id, list(paths), daemon_url)
        return project_id, changed

    try:
        project_id, changed = asyncio.run(go())
    except ValueError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    if not changed:
        typer.echo(f"{project_id}: no changes (files identical)")
        return
    typer.echo(f"{project_id}: {len(changed)} path(s) in the project files area")
    for line in changed:
        typer.echo(f"  {line}")


@project_app.command("pull")
def project_pull(
    project: str = typer.Argument(..., help="Project name or id."),
    dest: str = typer.Argument(..., help="Local folder to unpack the files into."),
    daemon_url: str | None = typer.Option(None, help="Override REWORK_DAEMON_URL."),
) -> None:
    """Fetch a repo-less project's files area from the daemon into DEST."""

    async def go() -> str:
        from rework.config import Config, open_store_from_config
        from rework.missions import resolve_project

        project_id = await resolve_project(project)
        assert project_id is not None
        store = await open_store_from_config(Config.from_env())
        try:
            found = await store.get_project(project_id)
        finally:
            await store.aclose()
        from rework.missions import project_is_upload

        if found is None or not project_is_upload(found.settings):
            raise ValueError(f"project {project} is git transport: clone its repo instead")
        from pathlib import Path as _Path

        from rework.project_files import pull_project_archive

        path = await asyncio.to_thread(pull_project_archive, project_id, _Path(dest), daemon_url)
        return str(path)

    try:
        path = asyncio.run(go())
    except ValueError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"project files pulled into {path}")


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
    dest = Path(out or default_results_dir() / mission_id)
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
    result = getattr(mission, "result", None) or {}
    accepted_section = accepted_findings_section(result.get("accepted_findings"))
    if accepted_section:
        # what the mission shipped with knowingly open, and why: same text as
        # the PR body (one renderer, so the two cannot drift)
        out += [*accepted_section, ""]
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
    to_library: bool = typer.Option(
        False,
        "--to-library",
        "--library",
        help="hand the report off into the Library as a report item (idempotent).",
    ),
    from_file: str | None = typer.Option(
        None, "--from-file", help="Body source for --to-library: a markdown file."
    ),
    title: str | None = typer.Option(None, "--title", help="Item name (default: Report: <id>)."),
    tag: list[str] = typer.Option([], "--tag", help="Extra item tag, repeatable."),  # noqa: B008
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the planned item and write nothing."
    ),
) -> None:
    """Render a mission's reports readably: each role's artifact plus the
    mechanical run-stats section (tokens, cache, cost, time). --md writes
    a markdown file (default ~/rework/reports/<mission-id>.md).

    --to-library copies the report BODY into the Library instead, as one report
    item per mission: re-running updates that item, never a duplicate. The body
    comes from --from-file, else the mission's largest changed docs/*.md in its
    surviving worktree or branch, else missions.result.summary clearly labelled
    as a summary. --from-file is an error when the file is missing or empty."""

    async def go() -> None:
        from rework.config import Config, open_store_from_config

        store = await open_store_from_config(Config.from_env())
        try:
            mission = await store.get_mission(mission_id)
            if mission is None:
                typer.secho(f"no such mission: {mission_id}", fg=typer.colors.RED)
                raise typer.Exit(1)
            if to_library:
                await _report_hand_off(
                    store,
                    mission,
                    from_file=from_file,
                    title=title,
                    extra_tags=list(tag),
                    dry_run=dry_run,
                )
                return
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

                from rework.config import default_reports_dir

                path = _Path(out) if out else default_reports_dir() / f"{mission_id}.md"
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


async def _report_hand_off(
    store: Any,
    mission: Any,
    *,
    from_file: str | None,
    title: str | None,
    extra_tags: list[str],
    dry_run: bool,
) -> None:
    """`rework report --to-library`: one report item per mission, re-run safe.

    Body resolution and the dedupe key live in rework.library.handoff (dedupe
    on provenance.mission_id, so a second run updates instead of minting an
    `l-<hex>` duplicate the way `library add` does). Errors are usage errors
    with a non-zero exit and nothing written."""
    from rework.config import Config
    from rework.library import handoff
    from rework.library.store import GitLibrary

    config = Config.from_env()
    try:
        body = handoff.from_file(from_file) if from_file else None
    except ValueError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(1) from exc
    if body is None:
        body = await handoff.resolve_from_mission(store, mission)
    if body is None:
        body = handoff.summary_body(mission)
    if body is None:
        typer.secho(
            f"mission {mission.id} has no report to hand off: no --from-file, no docs"
            " markdown in a surviving worktree or branch, and no result summary."
            " Pass --from-file PATH.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(1)

    name = title or f"Report: {mission.id}"
    git = GitLibrary(_library_root(), author=config.git_author)
    index = await open_library_index(config.db)
    try:
        item, created, verb = await handoff.hand_off(
            index,
            git,
            mission,
            body=body,
            name=name,
            extra_tags=extra_tags,
            dry_run=dry_run,
        )
    finally:
        await index.aclose()
    for line in handoff.describe(item, body, dry_run, verb):
        typer.echo(line)
    if dry_run:
        typer.echo("(dry run; nothing written)")
    elif created:
        typer.echo(f"handed off to the library; read it back with `rework library show {item.id}`")


def _one_line(text: str | None, width: int = 72) -> str:
    """Flatten `text` to ONE line for a table column, clipped with an ellipsis.
    Objectives are free text (often multi-line, e.g. a numbered brief), and `ls`
    prints one row per mission. The TUI has the same helper (tui/theme.clip),
    which the CLI must not import."""
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= width else flat[: width - 1].rstrip() + "\u2026"


@app.command()
def ls(limit: int = 20) -> None:
    """List missions, newest first, one line each."""

    async def go() -> list[tuple[str, str, str]]:
        from rework.config import Config, open_store_from_config

        store = await open_store_from_config(Config.from_env())
        try:
            missions = await store.list_missions(limit=limit)
            return [(m.id, m.status.value, _one_line(m.title or m.objective)) for m in missions]
        finally:
            await store.aclose()

    for mid, status, name in asyncio.run(go()):
        typer.echo(f"{mid}  {status:<14}  {name}")


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
def cancel(mission_id: str) -> None:
    """Cancel a mission. Works from any machine: the daemon notices the status
    and stops the roles within one poll; spent money stays spent."""
    _set_mission_status(mission_id, "cancelled")


@app.command()
def pause(mission_id: str) -> None:
    """Pause a mission: stop spending, keep everything resumable.

    Live roles are stopped; their checkpoints survive, so `rework resume` (or
    the UI) continues from where they were."""
    _set_mission_status(mission_id, "on_hold")


@app.command()
def resume(mission_id: str) -> None:
    """Resume a paused mission (or un-cancel one that never got far)."""
    _set_mission_status(mission_id, "running")


@app.command()
def prioritize(
    mission_id: str,
    priority: str = typer.Option(
        "high",
        "--priority",
        help="normal | high. Defaults to high: prioritizing is the common case,"
        " and --priority normal puts a mission back in the normal queue.",
    ),
) -> None:
    """Set a mission's pickup priority (high missions are picked up first,
    oldest first within a level). Does not touch a running mission."""
    from rework.missions import PRIORITIES

    level = (priority or "high").strip().lower()
    if level not in PRIORITIES:
        raise typer.BadParameter(f"unknown priority {priority!r}; priorities: normal, high")

    async def go() -> str:
        from rework.config import Config, open_store_from_config

        store = await open_store_from_config(Config.from_env())
        try:
            updated = await store.update_mission(mission_id, priority=level)
            if updated is None:
                typer.echo(f"unknown mission {mission_id}", err=True)
                raise typer.Exit(1)
            return updated.priority
        finally:
            await store.aclose()

    typer.echo(f"{mission_id}: priority {asyncio.run(go())}")


def _set_mission_status(mission_id: str, status: str) -> None:
    from rework.kernel.state.records import MissionStatus

    async def go() -> str:
        from rework.config import Config, open_store_from_config

        store = await open_store_from_config(Config.from_env())
        try:
            mission = await store.get_mission(mission_id)
            if mission is None:
                typer.echo(f"unknown mission {mission_id}", err=True)
                raise typer.Exit(1)
            updated = await store.update_mission(mission_id, status=MissionStatus(status))
            assert updated is not None
            return updated.status.value
        finally:
            await store.aclose()

    typer.echo(f"{mission_id}: {asyncio.run(go())}")


@app.command("retry-pr")
def retry_pr_cmd(mission_id: str) -> None:
    """Retry the PR delivery of a mission that finished but whose push or
    `gh pr create` failed (a transient remote hiccup must not cost the work).

    Pushes the recorded branch and opens the missing PR with the persisted
    title/body; on success the pending marker is cleared, on failure the
    remote's FULL message is printed and nothing else is touched.
    """
    from rework.orchestrator.delivery import retry_pr

    async def go() -> tuple[bool, str]:
        from rework.config import Config, open_store_from_config
        from rework.kernel.events.bus import InProcessEventBus

        store = await open_store_from_config(Config.from_env())
        try:
            return await retry_pr(store, InProcessEventBus(store), mission_id)
        finally:
            await store.aclose()

    ok, detail = asyncio.run(go())
    if ok:
        typer.echo(f"{mission_id}: PR opened: {detail}")
        return
    # the whole error, every line: the useful one is the LAST (git prints the
    # rejection reason after the remote banner and the forge's hints)
    typer.echo(f"{mission_id}: PR still not delivered:\n{detail}", err=True)
    raise typer.Exit(1)


# the same command under the `mission` group the docs use (`rework mission
# retry-pr ID`); the top-level spelling stays, like every other mission command
mission_app.command("retry-pr")(retry_pr_cmd)


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


@app.command("complaints-triage")
def complaints_triage_cmd(
    min_cluster: int = typer.Option(2, help="Complaints that must stack up before an issue opens."),
) -> None:
    """Cluster similar OPEN complaints and file ONE GitHub issue per loud
    cluster (ADR 0004). Members flip to issue_open with the issue URL. Run
    from a repo checkout so gh targets the right repo."""
    import asyncio as _asyncio

    from rework.config import Config, open_store_from_config
    from rework.library.triage import triage as _triage

    async def go() -> list[dict]:
        store = await open_store_from_config(Config.from_env())
        try:
            return await _triage(store, min_cluster=min_cluster)
        finally:
            await store.aclose()

    results = _asyncio.run(go())
    if not results:
        typer.echo("nothing loud enough to triage")
        return
    for r in results:
        if r.get("issue"):
            typer.echo(f"issue {r['issue']} <- {', '.join(r['cluster'])}")
        else:
            typer.secho(f"{r.get('note')} ({', '.join(r['cluster'])})", fg=typer.colors.RED)


@app.command()
def approve(
    gate_id: str,
    note: str | None = None,
    reject: bool = typer.Option(False, help="reject instead of approve"),
    answer: str | None = typer.Option(
        None, help="typed answer handed to the paused role (free text feedback)"
    ),
    answer_json: str | None = typer.Option(None, help="typed answer as JSON, for structured gates"),
) -> None:
    """Decide an open gate. Works from any machine sharing the database.

    `--answer` / `--answer-json` send a TYPED answer the paused role receives as
    its interrupt() result, instead of a bare yes/no — that is how feedback like
    "don't touch the parser" reaches the agent rather than stopping at a note."""

    async def go() -> tuple[bool, str]:
        import json as _json

        from rework.config import Config, open_store_from_config
        from rework.kernel.events.bus import InProcessEventBus
        from rework.kernel.gates import GateManager

        decision: dict[str, Any] | None = None
        if answer_json is not None:
            try:
                parsed = _json.loads(answer_json)
            except ValueError as exc:
                raise typer.BadParameter(f"--answer-json is not valid JSON: {exc}") from exc
            if not isinstance(parsed, dict):
                raise typer.BadParameter("--answer-json must be a JSON object")
            decision = parsed
        elif answer is not None:
            decision = {"feedback": answer}

        store = await open_store_from_config(Config.from_env())
        try:
            gates = GateManager(store, InProcessEventBus(store))
            gate = await gates.decide(gate_id, approved=not reject, note=note, decision=decision)
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
def message(
    mission_id: str,
    text: str = typer.Argument(..., help="Message text to send to the mission."),
) -> None:
    """Send a message to a mission. The orchestrator receives it and wakes."""

    async def go() -> None:
        from rework.config import Config, open_store_from_config
        from rework.missions import send_owner_message

        store = await open_store_from_config(Config.from_env())
        try:
            if await store.get_mission(mission_id) is None:
                typer.echo(f"unknown mission {mission_id}", err=True)
                raise typer.Exit(1)
            await send_owner_message(mission_id, text, store=store)
        finally:
            await store.aclose()

    try:
        asyncio.run(go())
    except typer.Exit:
        raise
    except Exception as exc:
        typer.secho(f"failed to send message: {exc}", fg=typer.colors.RED)
        raise typer.Exit(1) from exc
    typer.echo(f"{mission_id}: message sent")


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


def daemon_workroot(config: Any) -> Path:
    """Where the daemon keeps mission clones and role worktrees."""
    from rework.config import DEFAULT_STATE_DIR

    if config.is_postgres:
        return DEFAULT_STATE_DIR / "work"
    return Path(config.db).expanduser().parent / "work"


@app.command()
def gc(
    older_than_days: float = typer.Option(
        2.0, "--older-than-days", help="Only missions finished at least this long ago."
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="List what would be removed."),
) -> None:
    """Remove role worktrees and role scratch dirs of finished missions (mission
    clones are kept)."""
    import tempfile

    from rework import gc as gc_mod
    from rework.config import Config, open_store_from_config

    config = Config.from_env()
    workroot = daemon_workroot(config)
    scratch_root = Path(tempfile.gettempdir())

    async def go() -> dict[str, tuple[str, float]]:
        store = await open_store_from_config(config)
        try:
            return gc_mod.mission_table(await store.list_missions(limit=100_000))
        finally:
            await store.aclose()

    missions = asyncio.run(go())
    found = gc_mod.collect(workroot, missions, older_than_days * 86400)
    finished = {mid for mid, (status, _) in missions.items() if status in gc_mod.FINISHED}
    # scratch dirs have no idle window: they are dead the moment the mission is
    scratch = gc_mod.collect_scratch(scratch_root, finished)
    total = sum(c.bytes for c in found) + sum(c.bytes for c in scratch)
    for c in [*found, *scratch]:
        typer.echo(
            f"{'would remove' if dry_run else 'remove'} {c.path.name}  {gc_mod.human(c.bytes)}"
        )
    if dry_run:
        typer.echo(
            f"{len(found)} run dirs, {len(scratch)} scratch dirs,"
            f" {gc_mod.human(total)} (dry run; nothing removed)"
        )
        return
    freed = gc_mod.remove(found, workroot) + gc_mod.remove_scratch(scratch)
    typer.echo(
        f"removed {len(found)} run dirs and {len(scratch)} scratch dirs,"
        f" freed {gc_mod.human(freed)}"
    )


@app.command()
def tui() -> None:
    """Open the TUI dashboard (the same one bare `rework` starts)."""
    from rework.tui import run_tui

    run_tui()


@app.command()
def preview(
    pr: int | None = typer.Argument(None, help="Pull request number."),
    repo_dir: Path | None = typer.Option(None, "--repo-dir", help="Fetch into."),  # noqa: B008
    port: int | None = typer.Option(None, "--port", help="Fixed port (default: a free one)."),
    keep: bool = typer.Option(
        False, "--keep", help="Leave the preview running when the TUI exits."
    ),
    allow_missions: bool = typer.Option(
        False, "--allow-missions", help="Let the preview daemon dispatch role runs."
    ),
    list_: bool = typer.Option(False, "--list", help="List known previews."),
    rm: int | None = typer.Option(None, "--rm", help="Stop and delete preview <n>."),
) -> None:
    """Run pull request <n>'s build as a fully isolated second instance
    (ADR 0008): its own worktree, venv, state copy, dirs and port."""
    import subprocess as _subprocess

    from rework import preview as preview_mod

    if list_:
        rows = preview_mod.list_previews()
        if not rows:
            typer.echo("no previews")
            return
        typer.echo("pr  branch                    port   pid    alive  created")
        for row in rows:
            typer.echo(
                f"{row.get('pr')!s:<3} {row.get('branch', '')!s:<24} "
                f"{row.get('port', '')!s:<6} {row.get('pid', '')!s:<6} "
                f"{'yes' if row.get('alive') else 'no':<6} {row.get('created_at', '')}"
            )
        return
    if rm is not None:
        preview_mod.remove_preview(rm, repo_dir)
        typer.echo(f"removed preview #{rm}")
        return
    if pr is None:
        typer.secho("give a pull request number (or --list / --rm)", fg=typer.colors.RED)
        raise typer.Exit(2)
    if repo_dir is None:
        top = _subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True
        )
        if top.returncode != 0:
            typer.secho("not inside a git repo; pass --repo-dir", fg=typer.colors.RED)
            raise typer.Exit(2)
        repo_dir = Path(top.stdout.strip())
    try:
        preview_mod.create_preview(pr, repo_dir, keep, allow_missions, port)
    except _subprocess.CalledProcessError as exc:
        typer.secho(f"command failed: {exc.cmd}", fg=typer.colors.RED)
        raise typer.Exit(2) from exc
    except (OSError, RuntimeError) as exc:
        # create_preview tears the preview down before re-raising: report the
        # cause on one line instead of a traceback
        typer.secho(f"preview failed: {exc}", fg=typer.colors.RED)
        raise typer.Exit(2) from exc


def _env_truthy(name: str) -> bool:
    """REWORK_AUTO_UPDATE=1/true/yes (case-insensitive) enables the flag."""
    import os as _os

    return _os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


@app.command()
def serve(
    auto_update: bool = typer.Option(
        False,
        "--auto-update",
        help=(
            "When origin/main is ahead and nothing is in flight, pull + `uv sync` and"
            " exit so the service restarts on the new code (also REWORK_AUTO_UPDATE=1)."
        ),
    ),
) -> None:
    """Run the headless daemon (one per database; leader-locked)."""
    from rework.logs import setup_logging

    log_path = setup_logging("daemon", console=True)
    typer.echo(f"logs: {log_path}")

    from rework.config import Config, load_catalog, open_store_from_config

    config = Config.from_env()
    workroot = daemon_workroot(config)
    updating = auto_update or _env_truthy("REWORK_AUTO_UPDATE")

    async def go() -> None:
        from rework.logs import asyncio_exception_handler

        asyncio.get_running_loop().set_exception_handler(asyncio_exception_handler)
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
            # one-time-ish bridge for rows indexed before `body` was a column:
            # cheap and idempotent (empty rows only), and never fatal (a
            # backfill failure must not cost roles their library)
            try:
                from rework.library.curation import backfill_bodies

                filled, _skipped = await backfill_bodies(index, library.git)
                if filled:
                    typer.secho(f"backfilled {filled} library bod(y/ies)", fg=typer.colors.CYAN)
            except Exception as exc:
                typer.secho(f"library body backfill skipped ({exc})", fg=typer.colors.YELLOW)
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
            auto_update=updating,
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


def _target_usage_error(message: str) -> SystemExit:
    """One error shape for every --target problem: stderr, exit 2.

    SystemExit rather than typer.Exit because these fire BEFORE app() runs:
    a typer.Exit raised in main() itself is never seen by click's exception
    handling and falls out of the console entrypoint as a traceback."""
    typer.secho(message, fg=typer.colors.RED, err=True)
    return SystemExit(2)


def parse_target_flag(argv: list[str]) -> tuple[str | None, list[str]]:
    """The global `--target NAME` / `--target=NAME` from the argv prefix.

    Global options belong BEFORE the subcommand, so the scan stops at the
    first non-flag token (the command name) or at `--`: `rework --target
    sandbox ls` parses to ("sandbox", ["ls"]), while `rework ls --target
    sandbox` parses to (None, ["ls", ...]); main() turns that into a usage
    error before anything is applied (the silent wrong-target run this
    feature exists to prevent). The NAME itself is only consumed when it
    cannot be a command: anything starting with `-` (like `--help`) is left
    alone, so `rework --target --help` is a missing name, not a target
    literally called `--help`. Returns (name_or_None, rest): rest is the
    argv with the parsed global prefix removed. Every OTHER leading flag is
    KEPT in rest, in its original order: an empty rest means "no arguments,
    open the TUI", so silently dropping `--help` / `-h` made `rework --help`
    look like a bare `rework` and start the dashboard instead of printing help.
    Because they are kept, rest's FIRST element is not necessarily the
    subcommand: `rework --help update` parses to ["--help", "update"], so the
    command name comes from _subcommand() and never from rest[0]. A glued
    short option such as `-n3` is kept in rest like any other foreign leading
    flag (it used to be skipped together with its glued value).
    """
    kept: list[str] = []  # leading flags that are not ours, preserved in order
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            return None, kept + argv[i:]  # end of options, by definition
        if arg.startswith("-") and arg != "-":
            if arg == "--target":
                if i + 1 < len(argv) and not argv[i + 1].startswith("-"):
                    return argv[i + 1], kept + argv[i + 2 :]
                return None, kept + argv[i:]  # a missing NAME: env/default decide
            if arg.startswith("--target="):
                value = arg[len("--target=") :]
                return (value or None), kept + argv[i + 1 :]  # --target= is a missing name
            # some other global flag (--help, -h, a glued short option like
            # -n3): kept in rest, never dropped, so typer decides what it means
            kept.append(arg)
            i += 1
            continue
        return None, kept + argv[i:]  # the subcommand: the global-option scan is over
    return None, kept


def _subcommand(rest: list[str]) -> str:
    """The first NON-FLAG token of rest: the subcommand itself, or "" when rest
    is flags only. Leading flags are kept in rest, so rest[0] can be `--help`.

    Stops at the `--` end-of-options marker and yields "": every token after
    `--` is a positional, so none of them can claim to be the subcommand. That
    is what click resolves (`rework -- serve` runs no `serve` command), so the
    helper mirrors click instead of reading `serve` out of the operands.
    """
    for arg in rest:
        if arg == "--":
            return ""
        if not arg.startswith("-"):
            return arg
    return ""


def _subcommand_names() -> set[str]:
    """Top-level command and group names (used to flag `rework --target ls`
    as a missing NAME rather than "no target named 'ls'")."""
    from typer.main import get_command as _get_command

    return set(getattr(_get_command(app), "commands", {}))


def main() -> None:
    import os as _os
    import sys

    from rework.config import load_env_file

    load_env_file()  # provider keys: ~/.rework/rework.env, never overriding the env

    # Named connection targets (~/.rework/targets.toml), BEFORE anything reads
    # REWORK_DB / REWORK_DAEMON_URL: apply what the launch asked for (--target
    # NAME, else REWORK_TARGET, else the file's default) with setdefault
    # semantics, so an explicit REWORK_DB still wins (previews, systemd).
    _argv = sys.argv[1:]
    _flag, _rest = parse_target_flag(_argv)
    _explicit = any(a == "--target" or a.startswith("--target=") for a in _argv)
    # `rework update --target NAME` is NOT the global target flag: the update
    # command owns a --target of its own (a remote host to update over ssh), so
    # the after-the-subcommand rejection below must not fire for it.
    _sub = _subcommand(_rest)
    if _explicit and _flag is None and _sub != "update":
        # --target on the argv line without a consumable NAME: either it was
        # left off entirely (`rework --target --help`, `rework --target=`, the
        # trailing `--target`), or it sits after the subcommand, where it is
        # NOT a global option and must not be guessed at. Either way: a clear
        # usage error BEFORE anything is applied (no env writes on a broken
        # invocation — the silent wrong-target run this feature prevents).
        raise _target_usage_error(
            "--target needs a name and must come before the subcommand,"
            " e.g. `rework --target sandbox ls` (see `rework targets`)"
        )
    if _flag in _subcommand_names():
        # `rework --target version`: the NAME would have to be the subcommand
        # itself, which is not a target; a clear usage error beats Typer's
        # generic "missing command" and beats inventing a target named `ls`.
        raise _target_usage_error("--target needs a name (see `rework targets`)")

    # Resolution runs on EVERY invocation, flag or not: REWORK_TARGET and
    # targets.toml's `default` must reach bare `rework` and every subcommand,
    # not just launches that spelled out --target (the pre-fix bug: a bare
    # `rework` with REWORK_TARGET set silently talked to the wrong instance).
    try:
        from rework.targets import resolve_target

        resolve_target(flag=_flag, env_var=_os.environ.get("REWORK_TARGET"))
    except KeyError as exc:
        raise _target_usage_error(str(exc.args[0])) from None

    # bare `rework` (no arguments at all, or `rework --target NAME` with no
    # subcommand) opens the TUI dashboard; everything else keeps the command
    # surface (`rework --help`, `rework new ...`)
    if not _rest:
        from rework.tui import run_tui

        run_tui()
        return
    if _sub not in ("serve", "logs", "library-mcp"):
        from rework.logs import setup_logging

        setup_logging("cli")
    app()


@app.command("targets")
def targets_cmd() -> None:
    """List named targets from ~/.rework/targets.toml: which is default, is
    the database reachable, does the daemon answer /healthz."""
    import httpx as _httpx

    from rework.config import DEFAULT_STATE_DIR
    from rework.targets import TARGETS_PATH, load_targets_file

    file = load_targets_file()
    if not file.targets:
        typer.echo(f"no targets configured ({TARGETS_PATH} missing or empty)")
        typer.echo("see docs/ops.md, section 'Targets': set one up, then use rework --target NAME")
        raise typer.Exit(1)

    async def db_reachable(db: str) -> bool:
        from rework.targets import db_reachable as _probe

        return await asyncio.to_thread(_probe, db)

    def daemon_reachable(url: str) -> bool:
        try:
            resp = _httpx.get(url.rstrip("/") + "/healthz", timeout=3.0)
        except Exception:
            return False
        return resp.status_code == 200 and "ok" in resp.text[:2048]

    async def check(target: Any) -> tuple[bool, bool]:
        assert target.db and target.daemon_url
        return (
            await db_reachable(target.db),
            await asyncio.to_thread(daemon_reachable, target.daemon_url),
        )

    async def check_all() -> dict[str, tuple[bool, bool]]:
        names = list(file.targets)
        return dict(
            zip(
                names,
                await asyncio.gather(*(check(file.targets[n]) for n in names)),
                strict=True,
            )
        )

    results = asyncio.run(check_all())
    for name, target in file.targets.items():
        db_ok, daemon_ok = results[name]
        mark = "*" if name == file.default else " "
        typer.echo(
            f"{mark} {name:<12}"
            f" db={'up' if db_ok else 'UNREACHABLE':<12}"
            f" daemon={'up' if daemon_ok else 'UNREACHABLE':<12}"
            f" {target.ssh or '-'}"
        )
    typer.echo(
        f"default marked *; REWORK_DB/REWORK_DAEMON_URL/REWORK_LIBRARY_DIR come"
        f" from {DEFAULT_STATE_DIR / 'targets.toml'}"
    )


def _update_remote(target_name: str, *, check: bool) -> None:
    """Update a REMOTE host's checkout over ssh (the deploy path of docs/ops.md)
    and restart its service. This one DOES restart: the remote daemon is not
    the process running this command (finding 28 is about self-restarts)."""
    import subprocess as _sp

    from rework import update as _update
    from rework.targets import TARGETS_PATH, load_targets_file

    target = load_targets_file().targets.get(target_name)
    if target is None:
        typer.secho(
            f"no target named {target_name!r} in {TARGETS_PATH} (see `rework targets`)",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(1)
    if not target.ssh:
        typer.secho(
            f"target {target_name!r} has no `ssh` host in {TARGETS_PATH}; add one, e.g."
            f' ssh = "ubuntu@100.107.125.118"',
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(1)
    try:
        proc = _sp.run(
            ["ssh", target.ssh, _update.remote_update_command(check=check)],
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        # no `ssh` on PATH at all (or it cannot be exec'd): the deploy cannot
        # even start, so say that instead of dying with a traceback
        typer.secho(f"could not run ssh: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from None
    if proc.stdout:
        typer.echo(proc.stdout, nl=False)
    if proc.stderr:
        typer.secho(proc.stderr, fg=typer.colors.RED, err=True, nl=False)
    if proc.returncode != 0:
        raise typer.Exit(proc.returncode)


@app.command("update")
def update_cmd(
    check: bool = typer.Option(
        False, "--check", help="Report only: say whether origin/main is ahead; change nothing."
    ),
    target_name: str | None = typer.Option(
        None,
        "--target",
        "-t",
        help="Update the named target's checkout over ssh, then restart its service.",
    ),
) -> None:
    """Update this checkout to origin/main: fetch, fast-forward, `uv sync`.

    Refuses (exit 1) rather than guessing when the working tree is dirty, the
    branch is not `main`, or the history diverged. With `--target NAME` the same
    update runs on that target's ssh host and restarts its service."""
    from rework import update as _update

    if target_name is not None:
        _update_remote(target_name, check=check)
        return

    repo_root = _update.find_repo_root() or Path.cwd()
    if check:
        found = _update.check_update(repo_root)
        if found.error:
            typer.secho(f"cannot check for updates: {found.error}", fg=typer.colors.RED, err=True)
            raise typer.Exit(1)
        if found.ahead:
            typer.echo(f"origin/main is {found.ahead} commits ahead")
        else:
            typer.echo("up to date")
        return

    result = _update.run_update(repo_root)
    if not result.ok:
        typer.secho(result.reason, fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    if not result.changed:
        typer.echo(f"already up to date ({result.new})")
        return
    typer.echo(result.old)
    typer.echo(result.new)
    for subject in result.subjects:
        typer.echo(subject)


@app.command("sync-config")
def sync_config_cmd(
    target_name: str = typer.Argument(..., help="A target with an `ssh` host in targets.toml."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would change; never touch the remote host."
    ),
) -> None:
    """MERGE ~/.rework/models.json and the REWORK_*_KEY lines of
    ~/.rework/rework.env into the target's ssh host: local catalog entries add
    or replace by id, the target's own entries survive, and entries whose
    base_url is loopback (localhost / 127.0.0.1 / ::1) - and the keys only they
    use - stay local, because the target could never call them. Prints key
    NAMES and value LENGTHS only, never a secret. REWORK_DB / REWORK_DAEMON_URL
    and every other line in the remote rework.env are left untouched."""
    import subprocess as _sp

    from rework.config import DEFAULT_STATE_DIR
    from rework.kernel.llm.catalog import is_loopback_url
    from rework.targets import TARGETS_PATH, load_targets_file

    file = load_targets_file()
    target = file.targets.get(target_name)
    if target is None:
        typer.secho(
            f"no target named {target_name!r} in {TARGETS_PATH} (see `rework targets`)",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(1)
    if not target.ssh:
        typer.secho(
            f"target {target_name!r} has no `ssh` host in {TARGETS_PATH}; add one, e.g."
            f' ssh = "ubuntu@100.107.125.118"',
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(1)
    host = target.ssh

    def ssh_run(cmd: str, *, input_text: str | None = None) -> str:
        proc = _sp.run(
            ["ssh", host, cmd],
            input=input_text,
            capture_output=True,
            text=True,
            check=True,
        )
        return proc.stdout

    # -- the local catalog, minus what only THIS machine can call --------------
    # A loopback base_url is reachable only where it was written, so such an
    # entry (and a key no kept entry needs) is not synced: the target would
    # only ever fail on it.
    models = DEFAULT_STATE_DIR / "models.json"
    local_doc: Any = None
    try:
        local_doc = json.loads(models.expanduser().read_text())
    except OSError:
        local_doc = None
    except ValueError as exc:
        typer.secho(
            f"warning: local {models} is not valid JSON ({exc}); treating it as empty",
            fg=typer.colors.YELLOW,
            err=True,
        )
        local_doc = None
    local_entries = _models_doc_entries(local_doc)
    if local_doc is not None and not local_entries:
        typer.secho(
            f"warning: local {models} holds no catalog entries; nothing to merge",
            fg=typer.colors.YELLOW,
            err=True,
        )
        local_doc = None

    kept_entries: list[dict[str, Any]] = []
    skipped_entries: list[tuple[str, str]] = []  # (id, base_url)
    skipped_keys: set[str] = set()
    for entry in local_entries:
        entry_id = str(entry.get("id") or "?")
        url = entry.get("base_url")
        if is_loopback_url(url if isinstance(url, str) else None):
            skipped_entries.append((entry_id, str(url)))
            key = _entry_key_env(entry)
            if key:
                skipped_keys.add(key)
            continue
        kept_entries.append(entry)
    kept_keys = {key for key in map(_entry_key_env, kept_entries) if key}
    # a key a kept entry ALSO uses is still needed on the target: only keys that
    # no kept entry needs are dropped
    dropped_keys = {key for key in skipped_keys if key not in kept_keys}

    local_env = DEFAULT_STATE_DIR / "rework.env"
    try:
        local_lines = local_env.expanduser().read_text().splitlines()
    except OSError:
        local_lines = []
    local_keys: dict[str, str] = {}
    for raw in local_lines:
        line = raw.strip()
        if (
            line
            and not line.startswith("#")
            and line.startswith("REWORK_")
            and line.endswith("KEY") is False
            and "=" in line
        ):
            key = line.split("=", 1)[0].strip()
            if key.endswith("_KEY"):
                value = line.split("=", 1)[1].strip()
                local_keys[key] = value

    # remote rework.env: keep every line, replace/insert only REWORK_*_KEY ones
    # that a kept catalog entry may need
    send_keys = {key: value for key, value in local_keys.items() if key not in dropped_keys}
    remote_raw = ssh_run("cat ~/.rework/rework.env 2>/dev/null || true")
    remote_lines: list[str] = remote_raw.splitlines()
    covered: set[str] = set()
    merged: list[str] = []
    for line in remote_lines:
        stripped = line.strip()
        key = stripped.split("=", 1)[0].strip() if "=" in stripped else ""
        if key in send_keys:
            merged.append(f"{key}={send_keys[key]}")
            covered.add(key)
        else:
            merged.append(line)
    for key, value in send_keys.items():
        if key not in covered:
            merged.append(f"{key}={value}")
    if merged and not merged[-1].strip():
        merged = merged[:-1]  # a trailing blank line, not two
    merged_text = "\n".join(merged) + ("\n" if merged else "")
    added = sorted(k for k in send_keys if k not in covered)
    updated = sorted(k for k in send_keys if k in covered)
    key_lines = [f"  add     {key}: {len(send_keys[key])} chars" for key in added]
    key_lines += [f"  replace {key}: {len(send_keys[key])} chars" for key in updated]
    key_lines += [f"  skip {key}: used only by skipped entries" for key in sorted(dropped_keys)]
    if key_lines:
        typer.echo(f"REWORK_*_KEY lines from {local_env}:")
        for line in key_lines:
            typer.echo(line)
    else:
        typer.echo(f"no REWORK_*_KEY lines in {local_env}: nothing to merge")

    # -- remote models.json: read, merge by id, write the merged document ------
    # `ssh_run` cannot tell a failed ssh from an empty file (it returns stdout
    # either way), and an empty read is ALSO what a target with no models.json
    # gives. Those two must not be confused: treating a failed read as "the
    # target has no catalog" makes the merge below see no target entries and
    # then REPLACE the remote file with local-only ones, destroying the
    # target-only entries this command exists to preserve. So the read appends
    # a sentinel that only a completed shell run can produce.
    target_doc: Any = None
    models_sentinel = "__REWORK_MODELS_EOF__"
    remote_models = ssh_run(
        f"cat ~/.rework/models.json 2>/dev/null || true; echo {models_sentinel}"
    )
    if models_sentinel not in remote_models:
        typer.secho(
            f"cannot read {host}:~/.rework/models.json: the ssh read did not complete"
            f" (no {models_sentinel} marker in the output), so the target's catalog"
            " cannot be merged; nothing was written and the local files are untouched",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(1)
    # The marker is echoed LAST (`cat ...; echo <marker>`), so the split is
    # tail-anchored on purpose: a document may legitimately contain the token
    # inside a string, and a head-anchored split would truncate it there.
    remote_models = remote_models.rsplit(models_sentinel, 1)[0]
    if remote_models.endswith("\n"):
        remote_models = remote_models[:-1]
    if remote_models.strip():
        try:
            target_doc = json.loads(remote_models)
        except ValueError as exc:
            typer.secho(
                f"warning: {host}:~/.rework/models.json is not valid JSON ({exc});"
                " treating it as empty and writing a valid merged catalog",
                fg=typer.colors.YELLOW,
                err=True,
            )
            target_doc = None
    target_entries = _models_doc_entries(target_doc)
    target_ids = {str(entry.get("id")) for entry in target_entries}
    local_ids = [str(entry.get("id")) for entry in kept_entries]
    merged_entries = list(kept_entries) + [
        entry for entry in target_entries if str(entry.get("id")) not in set(local_ids)
    ]
    # The ONLY document shape the loader reads is the flat entry list
    # (load_catalog -> ModelCatalog.from_config -> ModelEntry(**item)). Nothing
    # in rework writes the object shape and an object-shaped file makes the
    # target's catalog unreadable, so preserving it could only corrupt the
    # target: sync-config always writes the list (its entries are merged above).
    merged_doc = _models_doc_with(merged_entries)
    if isinstance(target_doc, dict):
        typer.echo(
            "note: the target's models.json is not an entry list the loader can "
            "read; written as a plain entry list"
        )

    if local_doc is None:
        typer.echo(f"no local {models}: models.json not merged")
    else:
        typer.echo(f"merge models.json: {models}    -> {host}:~/.rework/models.json")
        for entry in kept_entries:
            verb = "replace" if str(entry.get("id")) in target_ids else "add    "
            typer.echo(f"  {verb} {entry.get('id')}: {len(json.dumps(entry))} chars")
        for entry in target_entries:
            if str(entry.get("id")) not in set(local_ids):
                typer.echo(f"  keep    {entry.get('id')}: only on the target")
        for entry_id, url in skipped_entries:
            typer.echo(f"  skip {entry_id}: loopback base_url {url}")

    if dry_run:
        typer.echo("dry run: nothing was sent to the remote host")
        return
    if send_keys or local_doc is not None:
        ssh_run("mkdir -p ~/.rework")
    if send_keys:
        ssh_run("touch ~/.rework/rework.env && chmod 600 ~/.rework/rework.env")
        ssh_run(
            "cat > ~/.rework/rework.env && chmod 600 ~/.rework/rework.env", input_text=merged_text
        )
        typer.echo(f"merged rework.env written to {host} (mode 600 preserved)")
    if local_doc is not None:
        ssh_run(
            "cat > ~/.rework/models.json && chmod 600 ~/.rework/models.json",
            input_text=json.dumps(merged_doc, indent=2) + "\n",
        )
        typer.echo(f"merged models.json written to {host}:~/.rework/models.json (mode 600)")


def _models_doc_entries(doc: Any) -> list[dict[str, Any]]:
    """The catalog entry list inside a models.json document: the document
    itself when it is a JSON list, or its `entries` list when it is an object.
    A malformed shape gives [] (and non-dict items are dropped) rather than
    crashing a sync."""
    if isinstance(doc, list):
        raw = doc
    elif isinstance(doc, dict) and isinstance(doc.get("entries"), list):
        raw = doc["entries"]
    else:
        return []
    return [item for item in raw if isinstance(item, dict)]


def _models_doc_with(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The document to write for `entries`: ALWAYS the flat list, the one shape
    the loader can read (ModelCatalog.from_config). The object shape
    `{"version": 2, "entries": [...]}` is deliberately NOT preserved: rework
    cannot read it, so reproducing it would only corrupt the target's catalog.
    """
    return entries


def _entry_key_env(entry: dict[str, Any]) -> str:
    """The env var a raw models.json entry's key lives in ("" when the provider
    needs none). Goes through api_key_env_name so the loopback filter can never
    drift from the lookup the adapters use; an entry too malformed to validate
    contributes no key instead of crashing the sync."""
    from rework.kernel.llm.catalog import ModelEntry, api_key_env_name

    try:
        return api_key_env_name(ModelEntry(**entry))
    except Exception:
        return ""


@app.command()
def logs(
    component: str | None = typer.Argument(None, help="daemon | tui | cli (default: all)"),
    mission: str | None = typer.Option(None, "--mission", "-m", help="Only this mission."),
    level: str | None = typer.Option(None, "--level", "-l", help="Minimum level, e.g. warning."),
    grep: str | None = typer.Option(None, "--grep", "-g", help="Substring filter."),
    lines: int = typer.Option(100, "--lines", "-n"),
    follow: bool = typer.Option(False, "--follow", "-f", help="Keep printing new records."),
) -> None:
    """Read the structured logs in ~/.rework/logs (REWORK_LOG_DIR): every model
    call with latency and tokens, tool calls, role lifecycles, crashes."""
    import time as _time

    from rework.logs import format_record, read_logs

    seen = 0.0
    for rec in read_logs(component, mission=mission, level=level, grep=grep, limit=lines):
        typer.echo(format_record(rec))
        seen = max(seen, float(rec.get("ts", 0)))
    while follow:
        _time.sleep(1.0)
        fresh = [
            r
            for r in read_logs(component, mission=mission, level=level, grep=grep, limit=500)
            if float(r.get("ts", 0)) > seen
        ]
        for rec in fresh:
            typer.echo(format_record(rec))
            seen = max(seen, float(rec.get("ts", 0)))


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
    reusable_because: str | None = typer.Option(
        None,
        "--reusable-because",
        help=(
            "Required for --type skill (unless --force): one sentence on why this"
            " generalizes to a future, DIFFERENT task, not a recap of this one."
        ),
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help=(
            "You are a human at the keyboard: keep --type skill as given, skipping the"
            " generality check (owners are never blocked)."
        ),
    ),
) -> None:
    """Add an item: the body file goes into the library git repo, the metadata
    into the index. Skills/reports/memories auto-accept; TOOLS always start
    proposed and need `rework library activate` (supply-chain gate).

    A skill also needs evidence of generality: a general, reusable technique
    (e.g. "how to write CliRunner tests with a tmp sqlite db"), not a recap of
    one task (e.g. "toyrepo add() planted-bug: one-line fix flips both tests
    green"). Pass --reusable-because, or --force if you are certain."""
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
                reusable_because=reusable_because,
                force=force,
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
        from rework.library.curation import external_gate_status

        index = await open_library_index(Config.from_env().db)
        try:
            item = await index.get_item(item_id)
            if item is None:
                raise typer.BadParameter(f"item not found: {item_id}")
            ok, reason = external_gate_status(item)
            if not ok:
                raise typer.BadParameter(f"cannot activate {item_id}: {reason}")
            item.status = item.status.__class__.ACTIVE
            item.touch()
            await index.upsert_item(item)
            return f"{item.id} is now active ({item.type.value} {item.name!r})"
        finally:
            await index.aclose()

    _ = note
    typer.echo(asyncio.run(go()))


@library_app.command("add-external")
def library_add_external(
    body_file: str = typer.Argument(...),
    name: str = typer.Option(...),
    source_url: str = typer.Option(
        ..., help="Where this came from (host must be allowlisted to activate)."
    ),
    description: str = typer.Option(""),
    tag: list[str] = typer.Option([], "--tag", help="Repeatable."),  # noqa: B008
) -> None:
    """Stage an EXTERNAL item (from the web). Always lands proposed; needs an
    allowlisted host + explicit owner approval to ever go active."""
    from rework.config import Config
    from rework.library.model import (
        ItemStatus,
        ItemType,
        LibraryItem,
        Origin,
        Provenance,
    )
    from rework.library.store import GitLibrary

    src = Path(body_file).expanduser()
    if not src.is_file():
        raise typer.BadParameter(f"body file not found: {src}")
    item = LibraryItem(
        type=ItemType.SKILL,  # external content enters as knowledge, never tools
        name=name,
        description=description,
        tags=list(tag),
        provenance=Provenance(origin=Origin.EXTERNAL, source_url=source_url),
        status=ItemStatus.PROPOSED,
    )

    async def go() -> str:
        lib = GitLibrary(_library_root(), author=Config.from_env().git_author)
        body_text = await asyncio.to_thread(src.read_text)
        item.body_path = await asyncio.to_thread(
            lib.write_body,
            item,
            body_text,
            f"library: add external skill {name!r} from {source_url}",
        )
        # the row carries the body too: the git file is only local history
        item.body = body_text
        index = await open_library_index(Config.from_env().db)
        try:
            await index.upsert_item(item)
        finally:
            await index.aclose()
        return item.id

    typer.echo(asyncio.run(go()))


@library_app.command("approve-external")
def library_approve_external(item_id: str) -> None:
    """The explicit per-item human approval for EXTERNAL ingest (ADR 0001:
    allowlisted sites only + owner reads it first)."""
    from rework.config import Config
    from rework.library.curation import external_gate_status

    async def go() -> str:
        index = await open_library_index(Config.from_env().db)
        try:
            item = await index.get_item(item_id)
            if item is None:
                raise typer.BadParameter(f"item not found: {item_id}")
            if item.provenance.origin.value != "external":
                return f"{item_id} is internal; plain `library activate` is the gate"
            ok, reason = external_gate_status(item)
            if not ok:
                raise typer.BadParameter(reason)
            item.status = item.status.__class__.ACTIVE
            item.touch()
            await index.upsert_item(item)
            return f"{item.id} external ingest approved; now active ({item.name!r})"
        finally:
            await index.aclose()

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
    from rework.library.store import GitLibrary, read_item_body

    async def go() -> str:
        index = await open_library_index(Config.from_env().db)
        try:
            item = await index.get_item(item_id)
        finally:
            await index.aclose()
        if item is None:
            raise typer.BadParameter(f"item not found: {item_id}")
        lib = GitLibrary(_library_root())
        # DB row first; the git file is only the fallback for pre-migration rows
        body = await asyncio.to_thread(read_item_body, item, lib)
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


@library_app.command("backfill-bodies")
def library_backfill_bodies() -> None:
    """Copy this machine's library body files into the index rows that have
    no body yet (rows written before the body column existed).

    Safe to re-run: rows that already carry a body are left alone, and rows
    whose file is not on this machine are skipped."""
    from rework.config import Config
    from rework.library.curation import backfill_bodies
    from rework.library.store import GitLibrary

    async def go() -> tuple[int, int]:
        index = await open_library_index(Config.from_env().db)
        try:
            git = GitLibrary(_library_root(), author=Config.from_env().git_author)
            return await backfill_bodies(index, git)
        finally:
            await index.aclose()

    filled, skipped = asyncio.run(go())
    typer.echo(f"backfilled {filled} item body/bodies from {_library_root()}")
    if skipped:
        typer.echo(f"skipped {skipped} (no local body file on this machine)")


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
