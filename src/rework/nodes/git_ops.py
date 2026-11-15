"""Git operations for coder runs: isolated worktree + branch per run, diff,
commit. All mechanical, all via subprocess in the target repo (CLAUDE.md rule 2:
asyncio subprocess only). Crash-safe by design: a worktree left behind by a
dead daemon is reclaimed by `prune_worktrees` at mission end or boot.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
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
        # keep whatever git had already written before it hung: replacing it
        # with a bare placeholder threw away the only evidence of the failure
        partial = b""
        with contextlib.suppress(Exception):
            partial, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        text = partial.decode(errors="replace").strip()
        note = f"(git timeout after {timeout_s:g}s)"
        return 124, f"{text}\n{note}" if text else note
    return proc.returncode or 0, out.decode(errors="replace").strip()


async def has_repo(path: Path) -> bool:
    code, _ = await _run(path, "rev-parse", "--is-inside-work-tree")
    return code == 0


async def clone_origin(url: str, dest: Path, timeout_s: float = 300.0) -> Path:
    """Git transport (ADR 0005): the daemon clones a mission's repo from its
    origin so the row carries a path that is real on THIS machine. Credentials
    come from the host's git config (gh auth setup-git on the owner's
    deployments)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        "git",
        "clone",
        url,
        str(dest),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError:
        proc.terminate()
        raise RuntimeError(f"git clone {url} timed out") from None
    if proc.returncode != 0:
        detail = out.decode(errors="replace").strip()
        raise RuntimeError(f"git clone {url} failed: {detail[:400]}")
    return dest


def _has_entries(path: Path) -> bool:
    try:
        return any(path.iterdir())
    except OSError:
        return False


async def make_worktree(repo: Path, worktree_path: Path, branch: str) -> Path:
    """Isolated checkout sharing the repo's history. Branch is created if new,
    reused if it already exists (idempotent dispatch/recovery)."""
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    code, out = await _run(repo, "worktree", "add", "-B", branch, str(worktree_path))
    if code != 0:
        raise RuntimeError(f"git worktree add failed: {out}")
    return worktree_path


async def ensure_worktree(
    repo: Path, worktree_path: Path, branch: str, start_point: str | None = None
) -> tuple[Path, str]:
    """Create-or-reuse. Returns (worktree, base_sha). The base sha is the branch
    point on first creation; callers persist it (run context) so a resumed run
    diffs against the same base it started with."""
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    has_entries = await asyncio.to_thread(_has_entries, worktree_path)
    if has_entries:
        code, cur = await _run(worktree_path, "rev-parse", "--abbrev-ref", "HEAD")
        if code == 0 and cur.strip() == branch:
            code, sha = await _run(worktree_path, "rev-parse", "HEAD")
            if code != 0:
                raise RuntimeError(f"git rev-parse failed: {sha}")
            return worktree_path, sha
        raise RuntimeError(f"{worktree_path} exists but is not worktree branch {branch}")
    args = ["worktree", "add", "-B", branch, str(worktree_path)]
    if start_point:
        args.append(start_point)  # e.g. the previous coder's branch (revisions)
    code, out = await _run(repo, *args)
    if code != 0:
        raise RuntimeError(f"git worktree add failed: {out}")
    code, sha = await _run(worktree_path, "rev-parse", "HEAD")
    if code != 0:
        raise RuntimeError(f"git rev-parse failed: {sha}")
    return worktree_path, sha


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


async def has_changes(worktree: Path, base: str | None = None) -> bool:
    """True when the worktree differs from `base`: uncommitted edits, or new
    commits since base."""
    code, out = await _run(worktree, "status", "--porcelain")
    if code == 0 and out.strip():
        return True
    if base:
        code, head = await _run(worktree, "rev-parse", "HEAD")
        if code == 0 and head.strip() and head.strip() != base:
            return True
    return False


async def unstage_nested_repos(worktree: Path) -> list[str]:
    """Unstage newly added git repositories inside the worktree: gitlinks
    (a nested clone, mode 160000) and bare-repo layouts (HEAD + objects/ +
    refs/). Found live: a coder's manual experiment left `origin.git/` and a
    clone `work/` in its worktree and the handoff's `git add -A` committed both.
    Returns the excluded paths; the files stay on disk, out of the commit."""
    code, out = await _run(worktree, "diff", "--cached", "--raw", "--diff-filter=A", "-z")
    if code != 0 or not out:
        return []
    fields = out.split("\0")
    excluded: set[str] = set()
    added: list[str] = []
    for meta, path in zip(fields[0::2], fields[1::2], strict=False):
        if not path:
            continue
        if meta.split(" ")[1:2] == ["160000"]:
            excluded.add(path)
        else:
            added.append(path)
    for path in added:
        for parent in Path(path).parents:
            if str(parent) == ".":
                break
            d = worktree / parent
            if (d / "HEAD").is_file() and (d / "objects").is_dir() and (d / "refs").is_dir():
                excluded.add(str(parent))
                break
    roots = sorted(p for p in excluded if not any(p.startswith(o + "/") for o in excluded))
    for path in roots:
        # --cached -f: index only (files stay on disk); -f because a newly
        # staged gitlink is otherwise refused
        await _run(worktree, "rm", "-r", "-q", "-f", "--cached", "--", path)
    return roots


async def commit(
    worktree: Path,
    message: str,
    author: tuple[str, str] = ("rework", "rework@rework.local"),
) -> str | None:
    """Returns the commit sha, or None when there was nothing to commit.
    Explicit agent identity: runs on machines with no git config (CI runners)."""
    code, out = await _run(
        worktree,
        "-c",
        f"user.name={author[0]}",
        "-c",
        f"user.email={author[1]}",
        "commit",
        "-m",
        message,
    )
    if code != 0:
        if "nothing to commit" in out:
            return None
        raise RuntimeError(f"git commit failed: {out}")
    code, sha = await _run(worktree, "rev-parse", "HEAD")
    if code != 0:
        raise RuntimeError(f"git rev-parse failed: {sha}")
    return sha


async def diff_base(worktree: Path, base: str | None = None) -> str:
    """The run's changes. With a recorded base sha: everything committed since
    that point (stable across resumes). Without one: merge-base diff against
    main, falling back to HEAD~1."""
    if base is not None:
        code, out = await _run(worktree, "diff", base)
        if code == 0:
            return out
    for base_branch in ("main", "master"):
        code, out = await _run(
            worktree, "diff", f"{base_branch}...{await current_branch(worktree)}"
        )
        if code == 0:
            return out
    code, out = await _run(worktree, "diff", "HEAD~1")
    return out


async def current_branch(worktree: Path) -> str:
    code, out = await _run(worktree, "rev-parse", "--abbrev-ref", "HEAD")
    if code != 0:
        raise RuntimeError(f"git rev-parse --abbrev-ref failed: {out}")
    return out


# -- push delivery: transient vs. real rejection -----------------------------
#
# Found live (mission m-26591d66f460): GitHub answered one push with
# `! [remote rejected] <branch> -> <branch> (failed)` with no reason given, and
# the single unclassified attempt threw a finished mission away. A manual retry
# minutes later succeeded. So: retry what the remote can plausibly answer
# differently next time, never retry what it has decided (non-fast-forward,
# protected branch, push protection, auth).

PUSH_ATTEMPTS = 3
PUSH_BACKOFF_S: tuple[float, ...] = (1.0, 2.0, 4.0)

# The remote said NO and will say no again: retrying only buries the real
# message under "3 attempts".
_PUSH_REJECTION_MARKERS = (
    "non-fast-forward",
    "fetch first",
    "protected branch",
    "gh006",
    "push declined due to repository rule violations",
    "gh013",
    "permission denied",
    "authentication failed",
    "denied to",
)
_PUSH_REJECTION_PATTERNS = (re.compile(r"\b403\b"),)

# The transport (or the forge behind it) hiccuped: a retry IS the fix.
_PUSH_TRANSIENT_MARKERS = (
    "connection reset",
    "connection refused",
    "connection timed out",
    "timed out",
    "timeout",
    "rpc failed",
    "unable to access",
    "could not resolve host",
    "early eof",
    "tls",
    "ssl",
    "broken pipe",
    "http 500",
    "http 502",
    "http 503",
    "http 504",
    "500 internal server error",
    "502 bad gateway",
    "503 service unavailable",
    "504 gateway timeout",
)
# `! [remote rejected] b -> b (failed)`: the remote refused the ref update and
# did NOT say why. This is the live case; treat it as transient.
_PUSH_REASONLESS_REJECTION = re.compile(r"\[remote rejected\].*\(failed\)")


class PushError(RuntimeError):
    """A push that did not happen. `retryable` says whether the remote might
    answer differently next time; `output` keeps git's FULL output, every line
    (the line that matters is the LAST one, after the forge's hints)."""

    def __init__(
        self, message: str, *, output: str = "", retryable: bool = False, attempts: int = 1
    ) -> None:
        super().__init__(message)
        self.output = output
        self.retryable = retryable
        self.attempts = attempts


def push_failure_is_transient(output: str, code: int = 1) -> bool:
    """Is this push failure worth another attempt?

    Real rejections (the remote decided) win over transient markers, because a
    protected-branch or auth failure also mentions hosts, TLS and timeouts."""
    if code == 124:  # git was killed on timeout: no answer is a transient answer
        return True
    text = (output or "").lower()
    if any(marker in text for marker in _PUSH_REJECTION_MARKERS):
        return False
    if any(pattern.search(text) for pattern in _PUSH_REJECTION_PATTERNS):
        return False
    if any(marker in text for marker in _PUSH_TRANSIENT_MARKERS):
        return True
    return bool(_PUSH_REASONLESS_REJECTION.search(text))


async def push_branch(
    worktree: Path,
    branch: str,
    *,
    attempts: int | None = None,
    backoff_s: tuple[float, ...] | None = None,
) -> None:
    """Push the run's branch to origin. For self-hosting: agents push
    branches, humans merge (CLAUDE.md dogfooding rules).

    Transient failures (a reasonless `remote rejected ... (failed)`, a dropped
    connection, an HTTP 5xx, a timeout) are retried with backoff; a real
    rejection is raised on the first answer, with the remote's own words."""
    tries = PUSH_ATTEMPTS if attempts is None else attempts
    delays = PUSH_BACKOFF_S if backoff_s is None else backoff_s
    out = ""
    code = 1
    for attempt in range(1, tries + 1):
        code, out = await _run(worktree, "push", "-u", "origin", branch, timeout_s=120.0)
        if code == 0:
            return
        if not push_failure_is_transient(out, code):
            raise PushError(f"git push rejected: {out}", output=out, attempts=attempt)
        if attempt < tries and delays:
            await asyncio.sleep(delays[min(attempt - 1, len(delays) - 1)])
    raise PushError(
        f"git push failed after {tries} attempts (transient): {out}",
        output=out,
        retryable=True,
        attempts=tries,
    )


async def head_sha(worktree: Path) -> str | None:
    code, out = await _run(worktree, "rev-parse", "HEAD")
    return out if code == 0 else None


def worktree_executor(worktree: Path) -> ToolExecutor:
    """A LocalExecutor jailed to the worktree: the coder's tools only see its
    own checkout (CLAUDE.md rule 6, the v1 isolation line)."""
    from rework.nodes.tools import LocalExecutor

    return LocalExecutor(worktree)
