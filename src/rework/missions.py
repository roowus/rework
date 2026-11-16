"""Mission creation: ONE implementation shared by the CLI and the TUI.

Repo resolution (ADR 0005): a git URL is stored as repo_origin for the daemon
to clone; a local path is staged where the creating process runs; a plain
folder (repo_dir) is packed and uploaded to the daemon's workspace API. The
fix/fix-pr plans are the canned investigate -> code -> audit pipelines.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

from rework.kernel.state.records import (
    PRIORITIES,
    Chat,
    EventRecord,
    EventType,
    Mission,
    check_priority,
    new_id,
)
from rework.kernel.state.store import StateStore

PLAN_FIX: dict = {
    "steps": [
        {
            "step": "investigate",
            "role": "investigator",
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
            "context": {"brief": "$OBJECTIVE"},
            "context_from": {"investigation": {"step": "investigate"}},
        },
        {
            "step": "audit",
            "role": "auditor",
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

# `publish` marks the plans that end by pushing the coder branch and opening
# a PR. It is separate from `gates`: publishing is mechanical, gating it is
# the optional exception (ask_before_pr / project setting), so a fix-pr plan
# has no gate by default but still publishes.
PLAN_FIX_PR: dict = {"steps": PLAN_FIX["steps"], "gates": [], "publish": "pr"}

PLANS = ("echo", "fix", "fix-pr", "draft")

# a derived mission title is a few words for lists (PR titles, `rework ls`,
# commit subjects): the whole objective is not one (found live: PR #24 was
# titled "rework: Follow-up to #22 (just merged, NOT deployed to rewvis...")
TITLE_WORDS = 8
TITLE_CHARS = 60
_ENUMERATOR = re.compile(r"^(?:[-*•\u2013\u2014]|\(?\d+[.)])\s+")
_SENTENCE_END = re.compile(r"(?<=[.!?])(?:\s|$)")
_QUOTES = "\"'\u201c\u201d\u2018\u2019"


def derive_title(objective: str) -> str | None:
    """A short, deterministic mission title from the objective; None when there
    is nothing usable. No model call: `rework new` must not spend money (or
    wait on a provider) just to name a mission.

    Rule: the first non-empty line, minus leading list/enumerator noise
    ("(1) ", "1. ", "- ", "bullet") and surrounding quotes/markdown; then the
    first sentence if there is more; capped at ~8 words and ~60 chars; trailing
    punctuation/ellipsis trimmed so a title never ends in "..." or an ellipsis.
    """
    line = next((ln.strip() for ln in (objective or "").splitlines() if ln.strip()), "")
    if not line:
        return None
    line = _ENUMERATOR.sub("", line)
    line = _SENTENCE_END.split(line, maxsplit=1)[0]
    line = line.replace("`", "").replace("**", "")  # markdown decoration, anywhere
    line = " ".join(line.split()).strip("#~ ")
    if line[:1] and line[0] in _QUOTES:  # a quote the objective wrapped the line in
        opener, line = line[0], line[1:].lstrip()
        if opener in line:
            line = line.replace(opener, "", 1)
    line = line.rstrip(_QUOTES + "`*_#~ ")
    words = line.split()
    if len(words) > TITLE_WORDS:
        line = " ".join(words[:TITLE_WORDS])
    line = line[:TITLE_CHARS].rstrip(" .,;:!?-\u2013\u2014\u2026")
    if line.count("(") > line.count(")"):
        line = line[: line.index("(")].rstrip(" .,;:!?-\u2013\u2014\u2026")  # cut group
    return line or None


# a playbook's FINAL gates. `draft` deliberately has none: its results are
# the project's files, so there is nothing to push and no PR to open.
PLAYBOOK_GATES: dict[str, list[str]] = {"echo": [], "fix": ["ship"], "fix-pr": [], "draft": []}

DEFAULT_BUDGET_USD = 2.0  # what a deployment gets when nothing usable is set
PLAYBOOK_PUBLISH: dict[str, str] = {"echo": "", "fix": "", "fix-pr": "pr", "draft": ""}

# Words that make an objective read-and-report: the deliverable is writing,
# not a change to the repository. Matched on word boundaries, case-insensitively.
READS_ONLY_TRIGGERS = (
    "audit",
    "review the code base",
    "review the codebase",
    "analyse",
    "analyze",
    "map out",
    "survey",
    "write a report",
    "document how",
    "investigate what",
    "explain how",
)
# Words that name a change to make: when one is present the work ends in a
# repository change, whatever else the objective says.
CHANGE_VETOES = (
    "fix",
    "add",
    "rename",
    "bump",
    "migrate",
    "implement",
    "refactor",
    "update",
    "remove",
    "upgrade",
    "patch",
    "change",
)


def _mentions(needle: str, haystack: str) -> bool:
    pattern = r"\b" + r"\s+".join(re.escape(part) for part in needle.split()) + r"\b"
    return re.search(pattern, haystack) is not None


def reads_only_plan(objective: str, text: str = "") -> str | None:
    """The plan a read-and-report objective should DEFAULT to: "draft" when the
    work's deliverable is writing (an audit, a survey, a report), else None.

    This is a DEFAULT-selection heuristic only. It is consulted when the caller
    did not name a plan, and it never overrides an explicit plan.

    Triggers are matched case-insensitively on word boundaries in the objective
    (plus `text`, when given). Any veto word that names a change to make wins,
    so "audit the code base and fix the two crashes" is a normal fix mission.
    """
    body = f"{objective or ''}\n{text or ''}".lower()
    if not body.strip():
        return None
    if any(_mentions(word, body) for word in CHANGE_VETOES):
        return None
    if any(_mentions(word, body) for word in READS_ONLY_TRIGGERS):
        return "draft"
    return None


BUDGET_ERROR = "budget must be a finite number greater than 0"


def _cap_usd(value: Any) -> float | None:
    """A usable cap, or None: NaN / inf / non-numeric / bool / <= 0 are "not a
    budget the owner said" and must never reach `plan["budget"]`."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def default_budget() -> tuple[float, float]:
    """(max_usd, ask_every_usd): $2 then ask again every $2 unless the
    deployment says otherwise (REWORK_BUDGET_USD / REWORK_BUDGET_STEP_USD).

    An env value that is not a finite, positive number (`inf`, `nan`, `0`,
    `-3`, a typo) is ignored and the default applies: `orchestrated_plan`
    stamps this into `original_max_usd` for EVERY caller that omits a budget,
    and `spent > NaN` is False for ever, i.e. no cap at all."""
    import os

    cap = _cap_usd(os.environ.get("REWORK_BUDGET_USD"))
    if cap is None:
        cap = DEFAULT_BUDGET_USD
    step = _cap_usd(os.environ.get("REWORK_BUDGET_STEP_USD"))
    return cap, (cap if step is None else step)


def budget_rail(max_usd: float, step_usd: float | None = None) -> dict[str, Any]:
    """The plan's budget dict: the cap, how much spend asks again, and the
    raise policy's baseline (`original_max_usd` + an empty raise trail).

    Shared by every plan that carries a cap: `orchestrated_plan` hands its
    rail to ADR 0007 orchestrator missions and `create_mission` to a template
    plan that was created WITH a budget, so both read the same keys.

    THIS is the write sink, so it validates: a NaN / inf / `<= 0` cap raises
    instead of being written. `ledger.py` enforces the cap with `spent >
    max_usd`, which is False for ever against NaN, so one poisoned cap is a
    mission that spends with NO cap at all. A bad `step_usd` is forgiving (it
    falls back to the cap): it only decides when to ask again, not how much
    may be spent."""
    cap = _cap_usd(max_usd)
    if cap is None:
        raise ValueError(BUDGET_ERROR)
    step = _cap_usd(step_usd)
    return {
        "max_usd": cap,
        "ask_every_usd": cap if step is None else step,
        # the raise policy's baseline: automatic raises are capped at
        # original_max_usd * ceiling_factor, so the ceiling cannot creep
        "original_max_usd": cap,
        "auto_raises": 0,
        "raises": [],
    }


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
    max_usd = cap if budget_usd is None else budget_usd
    spec: dict[str, Any] = {
        "kind": "auto",
        "playbook": playbook,
        "orchestrator": {"model": orchestrator_model, "effort": orchestrator_effort},
        "gates": list(PLAYBOOK_GATES.get(playbook, [])),
        "publish": PLAYBOOK_PUBLISH.get(playbook, ""),
        "budget": budget_rail(max_usd, step if budget_step_usd is None else budget_step_usd),
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


TRANSPORT_GIT = "git"
TRANSPORT_UPLOAD = "upload"
RESULTS_PUSH = "push_branch"
RESULTS_FETCH = "fetch"


def derive_transport(settings: dict[str, Any] | None) -> tuple[str, str]:
    """(transport, results) derived from a project's settings.

    Repos decide: a project with at least one repo reference travels by git and
    delivers a pushed branch; a project with none has no VCS to travel by, so
    it uses the upload transport and `rework project pull` brings the result
    back. The stored keys are honoured only when they agree with that rule —
    empty repos can never yield git/push_branch, because a fix mission would
    then need an origin that does not exist."""
    stored = settings or {}
    repos = [r for r in (stored.get("repos") or []) if r]
    if repos:
        transport = TRANSPORT_GIT
        default_results = RESULTS_PUSH
    else:
        transport = TRANSPORT_UPLOAD
        default_results = RESULTS_FETCH
    results = str(stored.get("results") or default_results)
    if transport == TRANSPORT_UPLOAD and results == RESULTS_PUSH:
        # no origin to push to: a repo-less project fetches, always
        results = default_results
    return transport, results


def project_is_upload(settings: dict[str, Any] | None) -> bool:
    """True for a repo-less ("upload" transport) project."""
    return derive_transport(settings)[0] == TRANSPORT_UPLOAD


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


def _norm_repo(url: str) -> str:
    url = url.strip().rstrip("/").lower()
    url = url.removesuffix(".git")
    if url.startswith("git@github.com:"):
        url = "https://github.com/" + url.split(":", 1)[1]
    return url


async def project_for_repo(repo: str) -> str | None:
    """The single project whose repo refs include this URL, else None."""
    if not repo:
        return None
    from rework.config import Config, open_store_from_config

    want = _norm_repo(repo)
    store = await open_store_from_config(Config.from_env())
    try:
        projects = await store.list_projects()
    finally:
        await store.aclose()
    owners = [
        p.id
        for p in projects
        if any(
            _norm_repo(str(r.get("url") or "")) == want
            for r in (p.settings or {}).get("repos") or []
        )
    ]
    return owners[0] if len(owners) == 1 else None


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


__all__ = ["PRIORITIES", "reads_only_plan"]


def _priority(value: str | None) -> str:
    """Validate a mission priority: normal (default) or high, the two pickup
    levels. Anything else is a caller bug, not something to store."""
    level = (value or "normal").strip().lower()
    return check_priority(level)


async def _upload_project(project_id: str) -> bool:
    """True when this project travels by the upload transport (no repo refs)."""
    from rework.config import Config, open_store_from_config

    store = await open_store_from_config(Config.from_env())
    try:
        found = await store.get_project(project_id)
    finally:
        await store.aclose()
    return project_is_upload(found.settings if found else None)


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
    title: str | None = None,
    priority: str = "normal",
) -> Mission:
    """Create a mission row (+ created event) and return it. When `store` is
    given it is used and NOT closed (the TUI holds one open); otherwise a
    connection is opened and closed here (CLI behavior).

    `model` and `effort` are the mission-level defaults the TUI exposes: they
    land in the plan, so every step without its own override uses them.

    A `budget_usd` that is not a finite number > 0 (`nan`, `inf`, `-3`, `0`)
    raises ValueError before ANY row is written: this is the one public way in,
    so a poisoned cap must not become a mission that can never be capped
    (`ledger.py` compares `spent_usd > max_usd`, False for ever against NaN)."""
    from rework.config import Config, open_store_from_config

    if budget_usd is not None and _cap_usd(budget_usd) is None:
        raise ValueError(BUDGET_ERROR)
    mission_id = new_id("m")
    resolved = await resolve_repo(
        repo=repo, repo_dir=repo_dir, project_id=project_id, mission_id=mission_id
    )
    if project_id is None and (resolved["repo_origin"] or repo):
        # owner: "use project labels correctly". A mission on a repo that a
        # project already owns belongs to that project even when the caller
        # (an old CLI habit, a chat proposal without a project) did not say so.
        project_id = await project_for_repo(resolved["repo_origin"] or repo or "")
    if plan not in PLANS:
        raise ValueError(f"unknown plan {plan!r}; plans: {', '.join(PLANS)}")
    upload_project = project_id is not None and await _upload_project(project_id)
    # draft needs one too: the deliverables are files written into the project
    if (
        plan in ("fix", "fix-pr", "draft")
        and not resolved["repo_path"]
        and not resolved["repo_origin"]
        and not upload_project
    ):
        # a repo-less ("upload" transport) project supplies its own workspace
        # from the daemon's files area, so the repo requirement does not apply
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
    elif plan in ("fix", "fix-pr", "draft"):
        # `draft` needs the template as much as fix/fix-pr do: without it
        # plan_spec stays None, the engine's DEFAULT_PLAN (echo) takes over, and
        # a programmatic caller silently gets a mission that does nothing.
        template = PLAN_FIX_PR if plan == "fix-pr" else PLAN_FIX
        plan_spec = json.loads(json.dumps(template).replace("$OBJECTIVE", objective))
        plan_spec["kind"] = plan
        if budget_usd is not None:
            # ONLY when the caller passed a cap: engine._build_graph enforces
            # plan["budget"]["max_usd"] whenever the key exists, and template
            # missions have never had one, so inventing a default here would
            # put a new cap on every existing `--plan fix` mission.
            plan_spec["budget"] = budget_rail(budget_usd, budget_step_usd)
        if plan == "draft":
            # writing in the project's own files: nothing to ship, no PR
            plan_spec["gates"] = list(PLAYBOOK_GATES["draft"])
        if model:
            # a mission-wide model choice replaces the template's per-step pins
            for step in plan_spec["steps"]:
                step.pop("model", None)
            plan_spec["model"] = model
        if effort:
            plan_spec["effort"] = effort

    effective_chat = chat_id
    if effective_chat is None:
        # a mission created without a chat (the CLI's `rework new`) still gets
        # one so it is reachable everywhere a chat-backed mission is: the Work
        # tab's "to" picker and the Dashboard. The TUI conversation, which
        # supplies its OWN chat_id, is untouched (GAP 1).
        effective_chat = new_id("chat")

    async def _go(own_store: StateStore) -> Mission:
        from rework.kernel.events.bus import InProcessEventBus

        mission = await own_store.create_mission(
            Mission(
                id=mission_id,
                objective=objective,
                repo_path=resolved["repo_path"],
                repo_origin=resolved["repo_origin"],
                project_id=project_id,
                chat_id=effective_chat,
                # an explicit title (the TUI's LLM proposal, `new --title`)
                # always wins; otherwise derive one so every mission has a
                # short name for lists, PR titles and commit subjects
                title=(title or "").strip()[:80] or derive_title(objective),
                priority=_priority(priority),
                plan=plan_spec,
            )
        )
        if chat_id is None:
            # titled from the mission title so it reads like any TUI-created
            # chat; linked back so the chat row knows which mission it owns
            await own_store.create_chat(
                Chat(
                    id=effective_chat,
                    title=(title or "").strip()[:80] or derive_title(objective) or "",
                    project_id=project_id,
                    mission_id=mission.id,
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


async def send_owner_message(mission_id: str, text: str, *, store: StateStore | None = None) -> Any:
    """THE owner->mission message publisher (ADR 0007): appends an OWNER_MESSAGE
    event; a running orchestrator is woken and sees it before its next turn.
    One canonical implementation shared by the TUI (Work tab), the Dashboard
    and `rework message` — do not duplicate the event-bus publish (GAP 1c).

    When `store` is given it is used and NOT closed; otherwise a connection is
    opened and closed here (CLI behavior)."""
    from rework.kernel.state.records import EventRecord, EventType

    if store is not None:
        return await store.append_event(
            EventRecord(mission_id=mission_id, type=EventType.OWNER_MESSAGE, payload={"text": text})
        )
    from rework.config import Config, open_store_from_config

    own = await open_store_from_config(Config.from_env())
    try:
        return await own.append_event(
            EventRecord(mission_id=mission_id, type=EventType.OWNER_MESSAGE, payload={"text": text})
        )
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
