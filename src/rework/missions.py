"""Mission creation: ONE implementation shared by the CLI and the TUI.

Repo resolution (ADR 0005): a git URL is stored as repo_origin for the daemon
to clone; a local path is staged where the creating process runs; a plain
folder (repo_dir) is packed and uploaded to the daemon's workspace API. The
fix/fix-pr plans are the canned investigate -> code -> audit pipelines.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rework.kernel.state.records import (
    EventRecord,
    EventType,
    Mission,
    new_id,
)
from rework.kernel.state.store import StateStore

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

PLAN_FIX_PR: dict = {"steps": PLAN_FIX["steps"], "gates": ["open_pr"]}

PLANS = ("echo", "fix", "fix-pr")

PLAYBOOK_GATES: dict[str, list[str]] = {"echo": [], "fix": ["ship"], "fix-pr": ["open_pr"]}


def default_budget() -> tuple[float, float]:
    """(max_usd, ask_every_usd): $2 then ask again every $2 unless the
    deployment says otherwise (REWORK_BUDGET_USD / REWORK_BUDGET_STEP_USD)."""
    import os

    cap = float(os.environ.get("REWORK_BUDGET_USD", "2"))
    step = float(os.environ.get("REWORK_BUDGET_STEP_USD", str(cap)))
    return cap, step


def orchestrated_plan(
    playbook: str,
    orchestrator_model: str,
    orchestrator_effort: str | None = None,
    *,
    model: str | None = None,
    effort: str | None = None,
    budget_usd: float | None = None,
    budget_step_usd: float | None = None,
) -> dict[str, Any]:
    """The plan of an orchestrated mission (ADR 0007): which model runs the
    orchestrator, the playbook it follows by default, the fixed final gates
    and the budget rail. `model` / `effort` are the default role model the
    orchestrator may override per spawn."""
    cap, step = default_budget()
    spec: dict[str, Any] = {
        "kind": "auto",
        "playbook": playbook,
        "orchestrator": {"model": orchestrator_model, "effort": orchestrator_effort},
        "gates": list(PLAYBOOK_GATES.get(playbook, [])),
        "budget": {
            "max_usd": cap if budget_usd is None else budget_usd,
            "ask_every_usd": step if budget_step_usd is None else budget_step_usd,
        },
    }
    if model:
        spec["model"] = model
    if effort:
        spec["effort"] = effort
    return spec


def is_git_url(value: str) -> bool:
    return value.startswith(("http://", "https://", "git@", "ssh://")) or value.endswith(".git")


def prepare_repo(repo_arg: str, mission_id: str) -> str:
    """A git repo is used in place; a plain directory is copied into the
    state workspace and git-initialized, so missions stay self-contained
    and the example repos are never mutated. Sync by design."""
    import shutil as _shutil
    import subprocess as _sp

    from rework.config import DEFAULT_STATE_DIR

    src = Path(repo_arg).expanduser().resolve()
    if not src.is_dir():
        raise ValueError(f"repo not found: {src}")
    if (src / ".git").exists():
        return str(src)
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


def upload_repo_dir(dir_arg: str, ws_name: str, daemon_url: str | None = None) -> str:
    """The upload transport: pack a local folder and POST it to the daemon's
    workspace API; the mission carries the daemon-side staged path."""
    import os as _os

    import httpx as _httpx

    from rework.orchestrator.workspace import (
        pack_directory,
        workspace_url_from_env,
    )

    src = Path(dir_arg).expanduser().resolve()
    if not src.is_dir():
        raise ValueError(f"folder not found: {src}")
    token = _os.environ.get("REWORK_TOKEN")
    url = f"{(daemon_url or workspace_url_from_env()).rstrip('/')}/workspaces/{ws_name}"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    data = pack_directory(src)
    resp = _httpx.post(url, content=data, headers=headers, timeout=300.0)
    if resp.status_code != 200:
        raise ValueError(
            f"daemon upload failed ({resp.status_code}): {resp.text[:200]}. "
            "Is the daemon running, and is REWORK_DAEMON_URL pointed at it?"
        )
    return str(resp.json()["path"])


async def resolve_repo(
    *,
    repo: str | None,
    repo_dir: str | None,
    project_id: str | None,
    mission_id: str,
) -> dict[str, Any]:
    """The three-way --repo resolution shared by CLI and TUI. Returns
    {repo_path, repo_origin} plus display notes for interactive callers."""

    repo_path: str | None = None
    repo_origin: str | None = None
    notes: list[str] = []
    if repo_dir:
        # packing + POST can take seconds on big folders: keep the caller's
        # loop (the TUI) responsive by running the sync transport in a thread
        import asyncio as _asyncio

        repo_path = await _asyncio.to_thread(upload_repo_dir, repo_dir, f"{mission_id}-files")
        notes.append(f"uploaded and staged on the daemon: {repo_path}")
    elif repo:
        import asyncio as _asyncio

        if is_git_url(repo):
            repo_origin = repo  # the daemon clones this itself (ADR 0005)
            notes.append(f"origin: the daemon will clone {repo}")
        else:
            local_exists = await _asyncio.to_thread(lambda: Path(repo).expanduser().exists())
            if local_exists:
                repo_path = await _asyncio.to_thread(prepare_repo, repo, mission_id)
                notes.append(f"staged locally: {repo_path}")
            elif project_id:
                origin, path = await _project_repo(project_id, repo)
                repo_origin, repo_path = origin, path
                notes.append(f"project ref {repo!r} resolved")
            else:
                raise ValueError(f"repo not found: {repo}")
    elif project_id:
        # no repo named: a project with exactly one repo means that one (the
        # chat usually leaves `repo` empty and lets the project decide)
        origin, path = await _project_repo(project_id, None)
        repo_origin, repo_path = origin, path
        if origin or path:
            notes.append("using the project's repo")
    return {"repo_path": repo_path, "repo_origin": repo_origin, "notes": notes}


async def _project_repo(project_id: str, name: str | None) -> tuple[str | None, str | None]:
    """(repo_origin, repo_path) for a project's repo ref. `name` None picks the
    project's only repo, or nothing when it has none; several repos and no name
    is ambiguous and raises. A URL becomes an origin the daemon clones; anything
    else is a path the daemon can see (ADR 0005)."""
    from rework.config import Config, open_store_from_config

    store = await open_store_from_config(Config.from_env())
    try:
        found = await store.get_project(project_id)
    finally:
        await store.aclose()
    refs = [r for r in ((found.settings if found else {}).get("repos") or []) if r]
    if name is None:
        if not refs:
            return None, None
        if len(refs) > 1:
            names = ", ".join(str(r.get("name")) for r in refs)
            raise ValueError(f"project {project_id} has several repos ({names}); name one")
        ref = refs[0]
    else:
        ref = next((r for r in refs if r.get("name") == name), None)
        if ref is None:
            raise ValueError(f"repo {name!r} not defined on project {project_id}")
    url = str(ref.get("url") or "")
    if is_git_url(url) or ref.get("kind") == "github":
        return url, None
    return None, url


async def create_mission(
    objective: str,
    *,
    repo: str | None = None,
    repo_dir: str | None = None,
    project_id: str | None = None,
    plan: str = "echo",
    model: str | None = None,
    effort: str | None = None,
    store: StateStore | None = None,
    chat_id: str | None = None,
    orchestrator_model: str | None = None,
    orchestrator_effort: str | None = None,
    budget_usd: float | None = None,
    budget_step_usd: float | None = None,
) -> Mission:
    """Create a mission row (+ created event) and return it. When `store` is
    given it is used and NOT closed (the TUI holds one open); otherwise a
    connection is opened and closed here (CLI behavior).

    `model` and `effort` are the mission-level defaults the TUI exposes: they
    land in the plan, so every step without its own override uses them."""
    from rework.config import Config, open_store_from_config

    mission_id = new_id("m")
    resolved = await resolve_repo(
        repo=repo, repo_dir=repo_dir, project_id=project_id, mission_id=mission_id
    )
    if plan not in PLANS:
        raise ValueError(f"unknown plan {plan!r}; plans: {', '.join(PLANS)}")
    if plan in ("fix", "fix-pr") and not resolved["repo_path"] and not resolved["repo_origin"]:
        raise ValueError(f"--plan {plan} requires a repo (URL, path, folder, or project ref)")
    plan_spec: dict | None = None
    if orchestrator_model:
        # ADR 0007: an LLM orchestrator runs the mission; the plan name is the
        # playbook it follows by default, the gates and budget are rails
        plan_spec = orchestrated_plan(
            plan,
            orchestrator_model,
            orchestrator_effort,
            model=model,
            effort=effort,
            budget_usd=budget_usd,
            budget_step_usd=budget_step_usd,
        )
    elif plan in ("fix", "fix-pr"):
        template = PLAN_FIX_PR if plan == "fix-pr" else PLAN_FIX
        plan_spec = json.loads(json.dumps(template).replace("$OBJECTIVE", objective))
        plan_spec["kind"] = plan
        if model:
            # a mission-wide model choice replaces the template's per-step pins
            for step in plan_spec["steps"]:
                step.pop("model", None)
            plan_spec["model"] = model
        if effort:
            plan_spec["effort"] = effort

    async def _go(own_store: StateStore) -> Mission:
        from rework.kernel.events.bus import InProcessEventBus

        mission = await own_store.create_mission(
            Mission(
                id=mission_id,
                objective=objective,
                repo_path=resolved["repo_path"],
                repo_origin=resolved["repo_origin"],
                project_id=project_id,
                chat_id=chat_id,
                plan=plan_spec,
            )
        )
        bus = InProcessEventBus(own_store)
        await bus.publish(
            EventRecord(
                mission_id=mission.id,
                type=EventType.MISSION_CREATED,
                payload={"objective": objective, "plan": plan},
            )
        )
        return mission

    if store is not None:
        return await _go(store)
    own = await open_store_from_config(Config.from_env())
    try:
        return await _go(own)
    finally:
        await own.aclose()


async def resolve_project(project_ref: str | None) -> str | None:
    """project id OR name -> validated id (raises when not found); None -> None."""
    if not project_ref:
        return None
    from rework.config import Config, open_store_from_config

    store = await open_store_from_config(Config.from_env())
    try:
        found = await store.get_project(project_ref)
        if found is None:
            by_name = [p for p in await store.list_projects() if p.name == project_ref]
            found = by_name[0] if len(by_name) == 1 else None
    finally:
        await store.aclose()
    if found is None:
        raise ValueError(f"project not found: {project_ref}")
    return found.id
