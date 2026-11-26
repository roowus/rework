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
    EventRecord,
    EventType,
    Mission,
    check_priority,
    new_id,
    orchestrator_chat_id,
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
                    "tests",
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
                "coder_tests": {"step": "code", "field": "tests"},
                "base_sha": {"step": "code", "field": "base_sha"},
                "changed_files": {"step": "code", "field": "changed_files"},
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

# `audit` is understanding work: an investigator scopes the question, a
# read-only surveyor maps the codebase. It has no coder step, no gate and no
# `publish`, so it can never push a branch or open a PR.
PLAN_AUDIT: dict = {
    "steps": [
        {
            "step": "investigate",
            "role": "investigator",
            "context": {
                "brief": "$OBJECTIVE",
                "depth": "quick",
                "requested_sections": ["summary", "findings", "blockers"],
            },
        },
        {
            "step": "survey",
            "role": "surveyor",
            "context": {"brief": "$OBJECTIVE"},
            "context_from": {"investigation": {"step": "investigate"}},
        },
    ],
    "gates": [],
}

PLANS = ("echo", "fix", "fix-pr", "draft", "audit")

# The plan kinds the FROZEN template runner drives (ADR 0012). A mission runs
# the LLM orchestrator when plan["orchestrator"] is set; otherwise it must name
# one of these, or it has no driver at all (a caller bug the old silent
# fallthrough to the echo plan used to hide).
TEMPLATE_KINDS = frozenset({"echo", "fix", "fix-pr", "draft", "audit", "mechanical"})

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


# a playbook's FINAL gates. `draft` deliberately has none: its results are the
# mission's Report tab, and the plan does not publish, so there is nothing to
# push and no PR to open. `audit` is a read-only survey: same, no gates.
PLAYBOOK_GATES: dict[str, list[str]] = {
    "echo": [],
    "fix": ["ship"],
    "fix-pr": [],
    "draft": [],
    "audit": [],
}

DEFAULT_BUDGET_USD = 2.0  # what a deployment gets when nothing usable is set
PLAYBOOK_PUBLISH: dict[str, str] = {
    "echo": "",
    "fix": "",
    "fix-pr": "pr",
    "draft": "",
    "audit": "",
}

# --- reads_only_plan: a closed-set DEFAULT-plan heuristic ---------------------
#
# The cost of a wrong verdict is asymmetric. A wrong None only costs the owner a
# relaunch with `--plan draft`; a wrong "draft" silently denies a real fix
# mission its branch and PR. So every uncertain case falls to None, and the
# only way to get "draft" is to pass four small checks in a row.

# 1. TRIGGER: the objective must BEGIN with one of these read-only openers.
_OPENERS = (
    "audit",
    "review",
    "analyse",
    "analyze",
    "survey",
    "assess",
    "evaluate",
    "summarise",
    "summarize",
    "map out",
    "document",
    "investigate",
    "explain",
    "report on",
    "write a report",
    "write up a report",
)
_OPENER = re.compile(
    r"^(?:" + "|".join(r"\s+".join(o.split()) for o in _OPENERS) + r")\b", re.IGNORECASE
)

# 2. CHANGE DENYLIST: change verb STEMS, each followed by `\w*` so every
# inflection hits ("fix" also hits "fixing", "add" also hits "address"). It is
# scanned over the whole objective and is DELIBERATELY over-generous: it can
# only ever turn a "draft" into None, which is the cheap direction, so a stem
# that also catches innocent words ("add" in "address", "port" in "portfolio",
# "risk" because a risk summary usually ends in "and mitigate them") costs
# nothing worth an exception. Do not narrow it; widen it. The only stems left
# out are the ones that would hit a required read-only phrase ("retri" would
# hit "how retries interact", "cover" would hit "test coverage").
_CHANGE_STEMS = (
    "fix", "add", "kill", "stop", "reduc", "tight", "prun", "restructur",
    "automat", "clos", "speed", "harden", "enabl", "disabl", "adjust", "tun",
    "configur", "instal", "upgrad", "downgrad", "patch", "convert",
    "consolidat", "dedup", "extract", "split", "merg", "mov", "remov", "delet",
    "creat", "introduc", "correct", "clean", "optimi", "optimiz", "simplif",
    "improv", "replac", "rewrit", "refactor", "renam", "migrat", "bump",
    "implement", "generat", "build", "ensur", "verif", "check", "identif",
    "trace", "repair", "mak", "writ", "updat", "chang", "port", "adopt",
    "eliminat", "mitigat", "resum", "normalis", "normaliz",
    # beyond the required set: more of the same kind
    "rotat", "risk", "address", "apply", "appli", "resolv", "redesign",
    "overhaul", "revert", "rollback", "deprecat", "remediat", "tweak",
    "polish", "increas", "decreas", "rais", "lower", "drop", "backfill",
    "rework", "revis", "swap", "wire", "ship",
)  # fmt: skip
_CHANGE_DENYLIST = re.compile(r"\b(?:" + "|".join(_CHANGE_STEMS) + r")\w*", re.IGNORECASE)

# 3. CLAUSE-SEPARATOR GUARD: a second clause means a second deliverable, and we
# cannot tell a report verb from a work verb, so a second clause is never a
# draft. The one exception is the closed trailing phrases below.
_CLAUSE_BOUNDARY = re.compile(
    r"[,;:.\-\u2013\u2014()]|\b(?:and|then|but|so|to|because|since|plus|with"
    r"|for\s+the\s+purpose\s+of|in\s+order\s+to"
    # more of the same: subordinators and particles that start a purpose or a
    # condition ("audit it until the tests pass"); extra words can only give None
    r"|until|unless|such|if|while|when|whenever|once|after|before|which|that"
    r"|where|by|via|using|into|away|up|down)\b",
    re.IGNORECASE,
)

# 4. CLOSED TRAILING REPORT PHRASES, stripped from the END only, at most one.
# This is the only path by which a phrase containing "and" or "to" survives.
# Keep it small: a realistic audit that needs a phrase not listed here is
# answered None (cheap), not with a new pattern.
_TRAILING_PHRASES = (
    "and tell me what you find",
    "and tell me what you found",
    "and tell me what you think",
    "and let me know what you think",
    "and let me know what you find",
    "and tell me",
    "and let me know",
    "and report back",
    "and explain what you find",
    "and summarise what you find",
    "and summarize what you find",
    "to tell me what you find",
    "to tell me what you think",
    "to let me know",
    "and summarise the findings",
    "and summarize the findings",
    "please tell me what you find",
    "tell me what you find",
    "tell me what you think",
    "let me know what you think",
    "let me know what you find",
    "let me know",
    "report back",
)
_TRAILING = re.compile(
    r"[\s,]*\b(?:"
    + "|".join(r"\s+".join(p.split()) for p in sorted(_TRAILING_PHRASES, key=len, reverse=True))
    + r")[\s.!?]*$",
    re.IGNORECASE,
)


def _read_only_opener(text: str) -> re.Match[str] | None:
    """Piece 1: the opener the objective begins with (after a leading
    "please "), or None."""
    return _OPENER.match(text)


def _has_change_verb(text: str) -> bool:
    """Piece 2: any change verb stem anywhere in `text`."""
    return _CHANGE_DENYLIST.search(text) is not None


def _strip_trailing_report_phrase(text: str) -> str:
    """Piece 4: drop ONE closed report phrase from the end of `text`."""
    return _TRAILING.sub("", text, count=1)


def _has_second_clause(text: str) -> bool:
    """Piece 3: any clause boundary left in `text`."""
    return _CLAUSE_BOUNDARY.search(text) is not None


def reads_only_plan(objective: str, text: str = "") -> str | None:
    """The plan a read-and-report objective should DEFAULT to: "draft" when the
    work's deliverable is writing (an audit, a survey, a report), else None.

    This is a DEFAULT-selection heuristic only. It is consulted when the caller
    did not name a plan, and it never overrides an explicit plan.

    A wrong None costs the owner one relaunch with `--plan draft`, while a wrong
    "draft" silently denies a fix mission its branch and PR. So the verdict is
    "draft" only for a closed shape (a read-only opener, no change verb
    anywhere, no second clause) and every uncertain input returns None.

    `text`, when given, is only scanned for change verbs; the opener and the
    clause guard look at the objective alone.
    """
    line = " ".join((objective or "").lower().split()).rstrip(" .!?")
    if line.startswith("please "):
        line = line[len("please ") :]
    opener = _read_only_opener(line)
    if opener is None:
        return None
    # the opener is exempt from the denylist: "write a report" holds "writ"
    rest = line[opener.end() :]
    if _has_change_verb(rest) or _has_change_verb(text or ""):
        return None
    if _has_second_clause(_strip_trailing_report_phrase(rest)):
        return None
    return "draft"


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
    # draft does NOT need one: its deliverable is the Report tab, its roles
    # research with the web tools, and a role with no repo gets an empty
    # per-run workspace (RoleBuildCtx.read_root). Found live (TUI e2e): the
    # chat proposes draft for research ("write me a report on X"), the card
    # offers Launch, and launching failed with "requires a repo".
    if (
        plan in ("fix", "fix-pr", "audit")
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
    elif plan in ("fix", "fix-pr", "draft", "audit"):
        # `draft` needs the template as much as fix/fix-pr do: without it
        # plan_spec stays None, the engine's DEFAULT_PLAN (echo) takes over, and
        # a programmatic caller silently gets a mission that does nothing.
        template = {"fix-pr": PLAN_FIX_PR, "audit": PLAN_AUDIT}.get(plan, PLAN_FIX)
        plan_spec = json.loads(json.dumps(template).replace("$OBJECTIVE", objective))
        plan_spec["kind"] = plan
        if budget_usd is not None:
            # ONLY when the caller passed a cap: engine._build_graph enforces
            # plan["budget"]["max_usd"] whenever the key exists, and template
            # missions have never had one, so inventing a default here would
            # put a new cap on every existing `--plan fix` mission.
            plan_spec["budget"] = budget_rail(budget_usd, budget_step_usd)
        if plan in ("draft", "audit"):
            # draft writes the project's own files and audit changes nothing:
            # either way there is nothing to ship and no PR
            plan_spec["gates"] = list(PLAYBOOK_GATES[plan])
        if model:
            # a mission-wide model choice replaces the template's per-step pins
            for step in plan_spec["steps"]:
                step.pop("model", None)
            plan_spec["model"] = model
        if effort:
            plan_spec["effort"] = effort
    elif plan == "echo":
        # the echo plan runs the frozen template runner too: stamp the kind so
        # the engine's driver guard (ADR 0012) accepts it instead of treating
        # "no orchestrator, no kind" as a caller bug
        plan_spec = {"kind": "echo", "steps": [{"step": "echo", "role": "echo"}]}

    # Agents never create chats. A mission launched without a user chat (the
    # CLI's `rework new`) points at its own orchestrator transcript, which the
    # orchestrator creates lazily on its first turn; this is a metadata pointer
    # only and mints no row, so no mission shell ever shows up as a conversation.
    # A chat_id supplied by the TUI conversation is a genuine user chat: kept.
    effective_chat = chat_id if chat_id is not None else orchestrator_chat_id(mission_id)

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
