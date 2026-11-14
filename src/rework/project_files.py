"""Client side of a repo-less project's files area (upload transport, ADR 0005).

`rework project add-files` pushes local files to the daemon and
`rework project pull` brings the area back; both speak to the daemon's
workspace API. The HTTP bits live here (sync by design, like the pack helpers)
so the CLI and the TUI share one implementation instead of two.
"""

from __future__ import annotations

import io
import os
import tarfile
from pathlib import Path

from rework.orchestrator.workspace import pack_paths, workspace_url_from_env


def _auth_headers() -> dict[str, str]:
    token = os.environ.get("REWORK_TOKEN")
    return {"Authorization": f"Bearer {token}"} if token else {}


def _base_url(daemon_url: str | None = None) -> str:
    return (daemon_url or workspace_url_from_env()).rstrip("/")


def add_project_files(
    project_id: str, paths: list[str], daemon_url: str | None = None
) -> list[str]:
    """Pack the given local paths and POST them into the project's files area.
    Returns the daemon's list of touched paths ("a.md (new)"). Sync by design:
    the CLI calls it directly and the TUI runs it in a worker thread."""
    import httpx

    bundle = pack_paths([Path(p) for p in paths])
    url = f"{_base_url(daemon_url)}/projects/{project_id}/files"
    resp = httpx.post(
        url,
        content=bundle,
        headers=_auth_headers(),
        params={"source": "add-files"},
        timeout=300.0,
    )
    _check(resp, f"add-files for project {project_id}")
    return [str(item) for item in resp.json().get("changed") or []]


def pull_project_archive(project_id: str, dest: Path, daemon_url: str | None = None) -> Path:
    """GET the project's files area as a tarball and unpack it into `dest`."""
    import httpx

    url = f"{_base_url(daemon_url)}/projects/{project_id}/files/archive"
    resp = httpx.get(url, headers=_auth_headers(), timeout=300.0)
    _check(resp, f"pull for project {project_id}")
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz") as tar:
        tar.extractall(dest, filter="data")
    return dest


def _check(resp: object, what: str) -> None:
    status = int(getattr(resp, "status_code", 0))
    if status == 200:
        return
    detail = str(getattr(resp, "text", ""))[:200]
    raise ValueError(f"{what} failed ({status}): {detail}")
