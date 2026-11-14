"""Workspace transport (ADR 0005): move non-git working files from the machine
that creates a mission to the machine that runs it.

One-shot per mission, no continuous sync: the CLI packs a folder into a
tarball, POSTs it to the daemon's workspace API, and the daemon stages it under
its workroot. The mission then carries a path that is real on the daemon's
disk, which is the whole fix for the cross-device path trap.

Principle (owner-directed): rework never transports code. Git repos are cloned
from their origin by the daemon; this module exists for workflow-level project
files that no VCS owns.

The HTTP surface is deliberately tiny and binds to localhost by default;
pointing REWORK_HTTP_HOST at a tailnet IP makes it tailnet-only.

Repo-less ("upload" transport) projects keep a PERSISTENT files area on the
daemon host (~/.rework/projects/<id>/files, git-initialised, no remotes, never
pushed). `rework project add-files` seeds it, each mission seeds from it and
saves its new/changed files back to it, and `rework project pull` brings it
back — the same one-shot pack/unpack transport, with a version store at rest.
"""

from __future__ import annotations

import asyncio
import io
import os
import shutil
import subprocess
import tarfile
import uuid
from pathlib import Path

from aiohttp import web

from rework.kernel.state.store import StateStore

# junk that must never travel or come back (mirrors _prepare_repo's ignore
# list and the coder DEFAULT_GITIGNORE)
EXCLUDE_DIRS = {
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".DS_Store",
}
EXCLUDE_SUFFIXES = (".pyc", ".pyo")


def _tar_filter(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    parts = Path(info.name).parts
    if any(part in EXCLUDE_DIRS for part in parts):
        return None
    if info.name.endswith(EXCLUDE_SUFFIXES):
        return None
    return info


def pack_directory(src: Path) -> bytes:
    """Pack a directory tree into a tar.gz (in-memory, exclusions applied).
    Sync by design; async callers wrap in to_thread."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path in sorted(src.rglob("*")):
            if not path.is_file() and not path.is_dir():
                continue  # sockets/symlink weirdness: skip, transport is for plain files
            rel = path.relative_to(src)
            tar.add(path, arcname=str(rel), filter=_tar_filter)
    return buf.getvalue()


def pack_paths(paths: list[Path]) -> bytes:
    """Pack an explicit list of local files/folders into one tar.gz. Each path
    lands under its own name, so `add-files NAME notes.md docs/` merges both
    into the project area and a pull gives back what was pushed. Sync."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for src in paths:
            root = src.expanduser().resolve()
            if root.is_file():
                tar.add(root, arcname=root.name, filter=_tar_filter)
                continue
            for child in sorted(root.rglob("*")):
                if not child.is_file() and not child.is_dir():
                    continue  # sockets/symlink weirdness: transport is for plain files
                tar.add(
                    child,
                    arcname=str(Path(root.name) / child.relative_to(root)),
                    filter=_tar_filter,
                )
    return buf.getvalue()


def _git(dest: Path, *args: str) -> None:
    """Run git in `dest` (mechanical identity, no remotes, never pushed).
    Sync by design like the rest of the packing helpers; to_thread wraps."""
    subprocess.run(["git", *args], cwd=dest, check=True, capture_output=True)


def _git_out(dest: Path, *args: str) -> str:
    """stdout of a git command, "" when it fails (callers treat that as
    "nothing changed" rather than failing a mission over bookkeeping)."""
    try:
        done = subprocess.run(["git", *args], cwd=dest, capture_output=True, text=True)
    except OSError:
        return ""
    return done.stdout if done.returncode == 0 else ""


def git_init_commit(dest: Path, message: str = "init") -> None:
    """git-init `dest` and commit whatever is in it (an empty tree still
    commits). Every staged or seeded tree needs a repo: the coder's worktree
    stage assumes one, mirroring the local _prepare_repo."""
    _git(dest, "init", "-b", "main")
    _git(dest, "add", "-A")
    _git(
        dest,
        "-c",
        "user.name=rework",
        "-c",
        "user.email=rework@rework.local",
        "commit",
        "-m",
        message,
        "--allow-empty",
    )


def _excluded(rel: Path) -> bool:
    return any(part in EXCLUDE_DIRS for part in rel.parts) or rel.name.endswith(EXCLUDE_SUFFIXES)


def unpack_into(dest: Path, data: bytes) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        tar.extractall(dest, filter="data")  # py3.12 filter blocks path traversal


def stage_bundle(workroot: Path, data: bytes, name: str | None = None) -> Path:
    """Extract an uploaded bundle under workroot/workspaces/<id>, git-init it
    (the coder's worktree stage needs a repo, mirroring the local
    _prepare_repo), and return the staged path. Sync; to_thread wraps."""
    ws_id = f"ws-{uuid.uuid4().hex[:12]}"
    dest = workroot / "workspaces" / ws_id / (name or "repo")
    unpack_into(dest, data)
    git_init_commit(dest, "init")  # empty folder still stages a repo
    return dest


def seed_mission_workspace(src: Path, dest: Path) -> Path:
    """Upload transport: a repo-less project's files area IS the mission's
    workspace, so copy it into the workroot and git-init the copy exactly as
    stage_bundle does. Roles, worktrees and read_root then need no special
    case. An empty (or missing) files area still yields a git-initialised
    workspace, so the mission starts with nothing but can still write.
    Idempotent across restarts: an existing seeded workspace is reused."""
    if (dest / ".git").is_dir():
        return dest
    dest.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(
            src,
            dest,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns(*EXCLUDE_DIRS, *EXCLUDE_SUFFIXES),
        )
    git_init_commit(dest, "init")
    return dest


def ensure_files_area(project_id: str) -> Path:
    """The project's persistent files area (upload transport, ADR 0005),
    git-initialised on first use: a local version store with no remotes, so a
    mission's save-back is an auditable commit and nothing is ever pushed."""
    from rework.config import project_files_dir

    area = project_files_dir(project_id)
    area.mkdir(parents=True, exist_ok=True)
    if not (area / ".git").is_dir():
        git_init_commit(area, "init")
    return area


def _status_changes(dest: Path) -> list[tuple[str, str]]:
    """[(path, "new"|"changed")] from git status, deletions skipped."""
    out: list[tuple[str, str]] = []
    for line in _git_out(dest, "status", "--porcelain", "--untracked-files=all").splitlines():
        if len(line) < 4:
            continue
        code, rel = line[:2], line[3:].strip().strip('"')
        if not rel or _excluded(Path(rel)):
            continue
        out.append((rel, "new" if code.strip() == "??" else "changed"))
    return out


def update_files_area(project_id: str, data: bytes, message: str) -> list[str]:
    """Unpack a bundle over the project's files area and commit it. Returns the
    touched paths as "a.md (new)" / "b.md (changed)". Sync; to_thread wraps."""
    area = ensure_files_area(project_id)
    unpack_into(area, data)
    changes = _status_changes(area)
    if not changes:
        return []
    _git(area, "add", "-A")
    _git(
        area,
        "-c",
        "user.name=rework",
        "-c",
        "user.email=rework@rework.local",
        "commit",
        "-m",
        message,
    )
    return [f"{rel} ({kind})" for rel, kind in changes]


def mission_changes(tree: Path) -> list[tuple[str, str]]:
    """[(path, "new"|"changed")] this tree gained relative to the snapshot it
    was seeded from: `git_init_commit` made that snapshot the repo's root
    commit, so a diff against the root covers commits made on a coder's branch
    as well as uncommitted edits. Deletions are deliberately NOT carried back
    into the project's area (a repo-less project is additive by design)."""
    roots = _git_out(tree, "rev-list", "--max-parents=0", "HEAD").split()
    if not roots:
        return []
    base = roots[-1]  # several roots means merged histories; the seeded one is oldest
    changes: dict[str, str] = {}
    for line in _git_out(tree, "diff", "--name-status", base).splitlines():
        parts = line.split("\t")
        if len(parts) < 2 or parts[0][:1] == "D":
            continue
        rel = parts[-1].strip()
        if rel and not _excluded(Path(rel)):
            changes[rel] = "new" if parts[0][:1] == "A" else "changed"
    for rel, kind in _status_changes(tree):  # untracked files never show in a diff
        changes.setdefault(rel, kind)
    return sorted(changes.items())


def save_mission_files(project_id: str, trees: list[Path], message: str) -> list[str]:
    """Upload transport: copy what a finished mission produced into the
    project's files area and commit it there with a message naming the source.

    `trees` are the mission's own checkouts in priority order (the seeded
    workspace first, then role worktrees, so a coder's branch wins). Returns
    the report lines, e.g. ["a.md (new)", "b.md (changed)"]. Sync; to_thread
    wraps."""
    area = ensure_files_area(project_id)
    kinds: dict[str, str] = {}
    for tree in trees:
        if not (tree / ".git").exists():
            continue
        for rel, kind in mission_changes(tree):
            if not (tree / rel).is_file():
                continue  # a second checkout no longer has it: the other one wins
            target = area / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(tree / rel, target)
            kinds[rel] = kind
    if not kinds:
        return []
    _git(area, "add", "-A")
    _git(
        area,
        "-c",
        "user.name=rework",
        "-c",
        "user.email=rework@rework.local",
        "commit",
        "-m",
        message,
    )
    return [f"{rel} ({kind})" for rel, kind in sorted(kinds.items())]


def pack_worktree(worktree: Path) -> bytes:
    """Pack a finished worktree for the fetch results path."""
    return pack_directory(worktree)


def _token_from_env() -> str | None:
    return os.environ.get("REWORK_TOKEN") or None


def _authorized(request: web.Request, token: str | None) -> bool:
    if token is None:
        return True  # tailnet-only deployments need no shared secret
    header = request.headers.get("Authorization", "")
    return header == f"Bearer {token}"


def build_workspace_app(store: StateStore, workroot: Path) -> web.Application:
    """The daemon's workspace API:

    POST /workspaces/<name>              body: tar.gz -> staged dir (returns path)
    GET  /workspaces/results/<mid>       the mission's final worktree as tar.gz
    POST /projects/<id>/files            body: tar.gz -> project files area + commit
    GET  /projects/<id>/files/archive    the project files area as tar.gz
    GET  /healthz                        liveness for the CLI
    """

    token = _token_from_env()

    async def healthz(_: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def upload(request: web.Request) -> web.Response:
        if not _authorized(request, token):
            return web.json_response({"error": "unauthorized"}, status=401)
        name = request.match_info["name"]
        if not name.replace("-", "").replace("_", "").isalnum():
            return web.json_response({"error": "bad workspace name"}, status=400)
        data = await request.read()
        if not data:
            return web.json_response({"error": "empty body (want a tar.gz)"}, status=400)
        try:
            dest = await asyncio.to_thread(stage_bundle, workroot, data, name)
        except (tarfile.TarError, EOFError, OSError) as exc:
            return web.json_response({"error": f"body is not a readable tar.gz: {exc}"}, status=400)
        return web.json_response({"workspace_id": dest.parent.name, "path": str(dest)})

    async def result(request: web.Request) -> web.Response:
        if not _authorized(request, token):
            return web.json_response({"error": "unauthorized"}, status=401)
        mission_id = request.match_info["mission_id"]
        mission = await store.get_mission(mission_id)
        if mission is None:
            return web.json_response({"error": f"unknown mission {mission_id}"}, status=404)
        worktree: str | None = None
        for run in reversed(await store.list_runs(mission_id)):  # latest attempt wins
            artifact = run.artifact or {}
            if artifact.get("worktree"):
                worktree = str(artifact["worktree"])
                break
        if worktree is None or not await asyncio.to_thread(Path(worktree).is_dir):
            return web.json_response(
                {"error": f"mission {mission_id} has no finished worktree to fetch"},
                status=404,
            )
        data = await asyncio.to_thread(pack_worktree, Path(worktree))
        return web.Response(
            body=data,
            content_type="application/gzip",
            headers={"Content-Disposition": f'attachment; filename="{mission_id}-result.tar.gz"'},
        )

    def _bad_project_id(project_id: str) -> bool:
        return not project_id.replace("-", "").replace("_", "").isalnum()

    async def project_files_upload(request: web.Request) -> web.Response:
        """Upload local files into a repo-less project's persistent area."""
        if not _authorized(request, token):
            return web.json_response({"error": "unauthorized"}, status=401)
        project_id = request.match_info["project_id"]
        if _bad_project_id(project_id):
            return web.json_response({"error": "bad project id"}, status=400)
        if await store.get_project(project_id) is None:
            return web.json_response({"error": f"unknown project {project_id}"}, status=404)
        data = await request.read()
        if not data:
            return web.json_response({"error": "empty body (want a tar.gz)"}, status=400)
        source = (request.query.get("source") or "add-files").strip()
        message = f"{source}: files added to project {project_id}"
        try:
            changed = await asyncio.to_thread(update_files_area, project_id, data, message)
        except (tarfile.TarError, EOFError, OSError) as exc:
            return web.json_response({"error": f"body is not a readable tar.gz: {exc}"}, status=400)
        return web.json_response({"project_id": project_id, "changed": changed})

    async def project_files_archive(request: web.Request) -> web.Response:
        """The project's files area as a tarball (an empty area is a valid,
        empty archive: pulling a new project must not be an error)."""
        if not _authorized(request, token):
            return web.json_response({"error": "unauthorized"}, status=401)
        project_id = request.match_info["project_id"]
        if _bad_project_id(project_id):
            return web.json_response({"error": "bad project id"}, status=400)
        if await store.get_project(project_id) is None:
            return web.json_response({"error": f"unknown project {project_id}"}, status=404)
        area = await asyncio.to_thread(ensure_files_area, project_id)
        data = await asyncio.to_thread(pack_directory, area)
        return web.Response(
            body=data,
            content_type="application/gzip",
            headers={"Content-Disposition": f'attachment; filename="{project_id}-files.tar.gz"'},
        )

    app = web.Application()
    app.router.add_get("/healthz", healthz)
    app.router.add_post("/workspaces/{name}", upload)
    app.router.add_get("/workspaces/results/{mission_id}", result)
    app.router.add_post("/projects/{project_id}/files", project_files_upload)
    app.router.add_get("/projects/{project_id}/files/archive", project_files_archive)
    return app


async def start_workspace_server(
    store: StateStore, workroot: Path, host: str, port: int
) -> web.AppRunner:
    """Run the workspace API inside the daemon's existing event loop."""
    runner = web.AppRunner(build_workspace_app(store, workroot))
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    return runner


def workspace_url_from_env() -> str:
    """Where a client reaches the daemon's workspace API (the creating machine
    reads this; the daemon's tailnet address is the usual value)."""
    default_port = os.environ.get("REWORK_HTTP_PORT", "8941")
    return os.environ.get("REWORK_DAEMON_URL", f"http://127.0.0.1:{default_port}")


def default_bind() -> tuple[str, int]:
    """The daemon's bind address: localhost by default, tailnet-only when the
    owner points REWORK_HTTP_HOST at a tailnet IP (a public IP would expose it;
    that is a deliberate act)."""
    host = os.environ.get("REWORK_HTTP_HOST", "127.0.0.1")
    port = int(os.environ.get("REWORK_HTTP_PORT", "8941"))
    return host, port
