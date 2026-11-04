"""Workspace transport (ADR 0005): move non-git working files from the machine
that creates a mission to the machine that runs it.

One-shot per mission, no continuous sync: the CLI packs a folder into a
tarball, POSTs it to the daemon's workspace API, and the daemon stages it under
its workroot. The mission then carries a path that is real on the daemon's
disk, which is the whole fix for the cross-device path trap.

Principle (owner-directed): rework never transports code. Git repos are cloned
from their origin by the daemon; this module exists for workflow-level project
files that no VCS owns.

The HTTP surface is deliberately tiny (three routes) and binds to localhost by
default; pointing REWORK_HTTP_HOST at a tailnet IP makes it tailnet-only.
"""

from __future__ import annotations

import asyncio
import io
import os
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


def stage_bundle(workroot: Path, data: bytes, name: str | None = None) -> Path:
    """Extract an uploaded bundle under workroot/workspaces/<id> and return the
    staged path (the daemon writes it onto the mission). Sync; to_thread wraps."""
    ws_id = f"ws-{uuid.uuid4().hex[:12]}"
    dest = workroot / "workspaces" / ws_id / (name or "repo")
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        tar.extractall(dest, filter="data")  # py3.12 filter blocks path traversal
    return dest


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

    POST /workspaces/<name>          body: tar.gz -> staged dir (returns path)
    GET  /workspaces/results/<mid>   the mission's final worktree as tar.gz
    GET  /healthz                    liveness for the CLI
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
            return web.json_response(
                {"error": f"body is not a readable tar.gz: {exc}"}, status=400
            )
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

    app = web.Application()
    app.router.add_get("/healthz", healthz)
    app.router.add_post("/workspaces/{name}", upload)
    app.router.add_get("/workspaces/results/{mission_id}", result)
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
