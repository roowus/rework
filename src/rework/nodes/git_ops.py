"""Git operations for coder runs: isolated worktree + branch per run, diff,
commit. All mechanical, all via subprocess in the target repo (CLAUDE.md rule 2:
asyncio subprocess only). Crash-safe by design: a worktree left behind by a
dead daemon is reclaimed by `prune_worktrees` at mission end or boot.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from rework.nodes.tools import ToolExecutor


async def _run(repo: Path, *args: str, timeout_s: float = 60.0) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(repo),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError:
        proc.terminate()
        return 124, "(git timeout)"
    return proc.returncode or 0, out.decode(errors="replace").strip()


async def has_repo(path: Path) -> bool:
    code, _ = await _run(path, "rev-parse", "--is-inside-work-tree")
    return code == 0


async def make_worktree(repo: Path, worktree_path: Path, branch: str) -> Path:
    """Isolated checkout sharing the repo's history. Branch is created if new,
    reused if it already exists (idempotent dispatch/recovery)."""
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    code, out = await _run(repo, "worktree", "add", "-B", branch, str(worktree_path))
    if code != 0:
        raise RuntimeError(f"git worktree add failed: {out}")
    return worktree_path


async def remove_worktree(repo: Path, worktree_path: Path, force: bool = False) -> None:
    args = ["worktree", "remove"]
    if force:
        args.append("--force")
    await _run(repo, *args, str(worktree_path))


async def prune_worktrees(repo: Path) -> None:
    """Reclaim worktrees whose directories are gone (daemon died mid-mission)."""
    await _run(repo, "worktree", "prune")


async def stage_all(worktree: Path) -> str:
    code, out = await _run(worktree, "add", "-A")
    if code != 0:
        raise RuntimeError(f"git add failed: {out}")
    return out


async def commit(worktree: Path, message: str) -> str | None:
    """Returns the commit sha, or None when there was nothing to commit."""
    code, out = await _run(worktree, "commit", "-m", message)
    if code != 0:
        if "nothing to commit" in out:
            return None
        raise RuntimeError(f"git commit failed: {out}")
    code, sha = await _run(worktree, "rev-parse", "HEAD")
    if code != 0:
        raise RuntimeError(f"git rev-parse failed: {sha}")
    return sha


async def diff_base(worktree: Path, base: str = "HEAD~1") -> str:
    """The run's changes. Default base is the worktree's own first parent:
    per-run branches start at the repo head, so HEAD~1 isolates this run's
    work once it has committed. Falls back to the full branch diff vs main."""
    code, out = await _run(worktree, "diff", base)
    if code == 0 and out:
        return out
    code, out = await _run(worktree, "diff", f"main...{await current_branch(worktree)}")
    return out


async def current_branch(worktree: Path) -> str:
    code, out = await _run(worktree, "rev-parse", "--abbrev-ref", "HEAD")
    if code != 0:
        raise RuntimeError(f"git rev-parse --abbrev-ref failed: {out}")
    return out


async def head_sha(worktree: Path) -> str | None:
    code, out = await _run(worktree, "rev-parse", "HEAD")
    return out if code == 0 else None


def worktree_executor(worktree: Path) -> ToolExecutor:
    """A LocalExecutor jailed to the worktree: the coder's tools only see its
    own checkout (CLAUDE.md rule 6, the v1 isolation line)."""
    from rework.nodes.tools import LocalExecutor

    return LocalExecutor(worktree)
