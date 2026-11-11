"""Reclaim disk from finished missions (`rework gc`).

Every role run gets its own git worktree under the workroot
(`<mission>_<step>_<role>_<n>/worktree`), each with its own virtualenv once a
role runs `uv run`; found 2026-11-10: 160 of them held 8.1 GB on the Mac and
nothing ever removed them. A run directory is removable when its mission is
finished (done, failed or cancelled) and has not changed for a while.

Kept on purpose:
- `missions/<id>/repo`, the mission's clone: the Preview button and
  `rework preview` fetch PRs through it;
- anything of an ACTIVE mission (a revision coder continues the previous
  coder's worktree);
- directories that do not name a mission this database knows (another
  database's work, or not ours to delete).
"""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FINISHED = {"done", "failed", "cancelled"}


@dataclass
class Candidate:
    path: Path
    mission_id: str
    bytes: int


def _size(path: Path) -> int:
    """Bytes deleting `path` actually frees: files hard-linked elsewhere (uv
    links virtualenv files from its cache on Linux) free nothing, so they do
    not count (found on rewvis: 913 MB 'to free' in a 648 MB directory)."""
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file() and not p.is_symlink():
                st = p.stat()
                if st.st_nlink == 1:
                    total += st.st_size
        except OSError:
            continue
    return total


def mission_of(run_dir: Path) -> str | None:
    """`m-58d05d4101c7_code6_coder_1` -> `m-58d05d4101c7`; None for anything
    that is not a run directory (`missions/`, bare `m-<id>` workspaces)."""
    name = run_dir.name
    if not name.startswith("m-") or "_" not in name:
        return None
    return name.split("_", 1)[0]


def collect(
    workroot: Path,
    missions: dict[str, tuple[str, float]],
    older_than_s: float,
    now: float | None = None,
    measure: bool = True,
) -> list[Candidate]:
    """Run directories of finished missions idle for `older_than_s`.
    `missions` maps id -> (status, updated_at)."""
    now = time.time() if now is None else now
    out: list[Candidate] = []
    if not workroot.is_dir():
        return out
    for run_dir in sorted(workroot.iterdir()):
        if not run_dir.is_dir():
            continue
        mid = mission_of(run_dir)
        if mid is None or mid not in missions:
            continue
        status, updated = missions[mid]
        if status not in FINISHED or now - float(updated or 0) < older_than_s:
            continue
        out.append(Candidate(run_dir, mid, _size(run_dir) if measure else 0))
    return out


def remove(candidates: list[Candidate], workroot: Path) -> int:
    """Delete the run directories, then let each mission's clone forget the
    worktrees it registered. Returns bytes freed."""
    freed = 0
    for c in candidates:
        shutil.rmtree(c.path, ignore_errors=True)
        if not c.path.exists():
            freed += c.bytes
    for mid in {c.mission_id for c in candidates}:
        repo = workroot / "missions" / mid / "repo"
        if (repo / ".git").exists():
            subprocess.run(["git", "worktree", "prune"], cwd=repo, capture_output=True, check=False)
    return freed


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} GB"


def mission_table(missions: list[Any]) -> dict[str, tuple[str, float]]:
    return {
        m.id: (getattr(m.status, "value", str(m.status)), float(m.updated_at or 0))
        for m in missions
    }
