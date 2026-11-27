"""Git operations for coder runs: isolated worktree + branch per run, diff,
commit. All mechanical, all via subprocess in the target repo (CLAUDE.md rule 2:
asyncio subprocess only). Crash-safe by design: a worktree left behind by a
dead daemon is reclaimed by `prune_worktrees` at mission end or boot.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from rework.nodes.failure_text import HTTP_5XX, RATE_LIMIT, marker_regex, scrub

# The fallback commit identity: a deployment that configures nothing
# (REWORK_GIT_AUTHOR_NAME/EMAIL) gets this, exactly as `commit()` always has.
DEFAULT_GIT_AUTHOR: tuple[str, str] = ("rework", "rework@rework.local")


@dataclass
class MergeResult:
    """What `merge_base_into_branch` did. Exactly one of the fields below is
    meaningful, in this order: skipped_reason, up_to_date, merged, conflicted,
    failed.

    `conflicted` means git left UNMERGED PATHS behind; a merge git REFUSED
    outright (unrelated histories, an index it could not write) is `failed`
    with git's own words in `detail`, never a conflict with zero paths."""

    skipped_reason: str | None = None
    up_to_date: bool = False
    merged: bool = False
    sha: str | None = None
    conflicted: bool = False
    conflicting: list[str] = field(default_factory=list)
    abort_failed: str | None = None
    failed: bool = False
    detail: str = ""
    # sha of the stashed staged work an abort of a LEFTOVER merge would have
    # thrown away (`refs/rework/pre-merge-abort`), when there was any
    discarded_work: str | None = None


async def _run(repo: Path, *args: str, timeout_s: float = 60.0) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(repo),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    # Read incrementally: after a timed-out communicate() the pipe is abandoned
    # and a second communicate() returns b"" on real asyncio, so the only way
    # to keep what git wrote before it hung (the `remote:` banner, `fatal:
    # early EOF`) is to already hold the bytes when the deadline hits.
    assert proc.stdout is not None
    chunks: list[bytes] = []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    timed_out = False
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            timed_out = True
            break
        try:
            chunk = await asyncio.wait_for(proc.stdout.read(4096), timeout=remaining)
        except TimeoutError:
            timed_out = True
            break
        if not chunk:
            break
        chunks.append(chunk)
    if not timed_out:
        # EOF only means the pipe closed: git can close it and keep running, so
        # the exit-code wait spends what is left of the SAME deadline.
        try:
            await asyncio.wait_for(proc.wait(), timeout=max(deadline - loop.time(), 0.001))
        except TimeoutError:
            timed_out = True
    if timed_out:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=5)
        # what git wrote just before the deadline is still in the pipe: keep
        # it (found under load: the banner landed after the last read)
        with contextlib.suppress(TimeoutError):
            chunks.append(await asyncio.wait_for(proc.stdout.read(), timeout=1))
    text = b"".join(chunks).decode(errors="replace").strip()
    if timed_out:
        note = f"(git timeout after {timeout_s:g}s)"
        return 124, f"{text}\n{note}" if text else note
    return proc.returncode or 0, text


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
    worktree: Path, message: str, author: tuple[str, str] = DEFAULT_GIT_AUTHOR
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
_PUSH_REJECTION_PATTERNS = (
    re.compile(r"\b(?:http|status|error|returned error)[/:\s.\d]*\b403\b"),
    re.compile(r"\b403\s+forbidden\b"),
)

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
# Anchored to the END of the line so `(failed)` can never be the tail of a
# reasoned refusal such as `(stale info)` / `(fetch first)`.
_PUSH_REASONLESS_REJECTION = re.compile(r"\[remote rejected\][^\n]*\(failed\)\s*$", re.MULTILINE)
_PUSH_REJECTION_RE = marker_regex(_PUSH_REJECTION_MARKERS)
_PUSH_TRANSIENT_RE = marker_regex(_PUSH_TRANSIENT_MARKERS)


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
    text = scrub(output)
    # GitHub answers a secondary rate limit / abuse detection with a 403, the
    # permission status: it is a "wait and retry", so it is checked first
    if RATE_LIMIT.search(text):
        return True
    if _PUSH_REJECTION_RE.search(text):
        return False
    if any(pattern.search(text) for pattern in _PUSH_REJECTION_PATTERNS):
        return False
    # a 5xx is the forge failing, not deciding: checked before the weaker
    # transient markers (a wasted retry costs seconds; a false "real
    # rejection" costs the mission's PR)
    if HTTP_5XX.search(text) or _PUSH_TRANSIENT_RE.search(text):
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


# -- merge origin/<base> into the run branch before the PR -------------------
#
# Found live: mission PRs arrived CONFLICTING with main (4 to 8 files each)
# because other missions merged while this one worked. The clean merge belongs
# in the branch that is pushed, so the PR arrives mergeable. A REAL conflict is
# never guessed at: the merge is aborted, the branch is left byte-identical to
# what the coder left, and the PR still opens with the conflicting paths in the
# body so the owner decides. A merge git REFUSES outright (unrelated histories,
# an index it cannot write) is not a conflict: it is reported as failed with
# git's own words, because claiming conflicts with zero named paths is a lie.


async def has_origin(repo: Path) -> bool:
    """True when `repo` has an `origin` remote. Repo-less missions and upload
    projects have none, and the merge step is then a silent no-op."""
    code, _ = await _run(repo, "remote", "get-url", "origin")
    return code == 0


async def fetch_origin(repo: Path, base: str, timeout_s: float = 120.0) -> tuple[bool, str]:
    """`git fetch origin <base>`: brings origin/<base> up to date so the merge
    sees what main really is. Returns (ok, output); never raises."""
    code, out = await _run(repo, "fetch", "origin", base, timeout_s=timeout_s)
    return code == 0, out


async def conflict_paths(repo: Path) -> list[str]:
    """The unmerged paths of a stopped merge.

    `diff --name-only --diff-filter=U` is the primary source. A delete/modify
    conflict has only ONE stage entry, and `ls-files -u` still lists it, so the
    two are merged (ls-files first: it is the one that sees every unmerged
    entry). Order is kept stable for the PR body."""
    found: list[str] = []
    code, out = await _run(repo, "ls-files", "-u")
    if code == 0:
        for line in out.splitlines():
            # "<mode> <sha> <stage>\t<path>"
            _, _, path = line.partition("\t")
            path = path.strip()
            if path and path not in found:
                found.append(path)
    code, out = await _run(repo, "diff", "--name-only", "--diff-filter=U")
    if code == 0:
        for line in out.splitlines():
            path = line.strip()
            if path and path not in found:
                found.append(path)
    return sorted(found)


async def merge_in_progress(repo: Path) -> bool:
    code, _ = await _run(repo, "rev-parse", "-q", "--verify", "MERGE_HEAD")
    return code == 0


PRE_MERGE_ABORT_REF = "refs/rework/pre-merge-abort"


async def stash_staged_work(repo: Path) -> str | None:
    """Keep a recoverable copy of staged work before an abort would discard it.

    `git stash create` writes the stash commit WITHOUT touching the index or the
    working tree, so it is safe here; it refuses while the index holds unmerged
    entries (a stopped merge usually does), and then there is nothing extra to
    lose, so any failure is simply "nothing stashed". Returns the sha it made,
    pinned under `refs/rework/pre-merge-abort` so `git gc` cannot collect it.
    """
    code, out = await _run(repo, "stash", "create", "rework: pre-merge-abort")
    sha = out.strip().splitlines()[-1].strip() if code == 0 and out.strip() else ""
    if not re.fullmatch(r"[0-9a-f]{40,64}", sha):
        return None
    if not await keep_ref(repo, PRE_MERGE_ABORT_REF, sha):
        return None
    return sha


async def keep_ref(repo: Path, ref: str, sha: str) -> bool:
    """Pin a commit under `ref`, so an unreferenced stash cannot be collected."""
    code, _ = await _run(repo, "update-ref", ref, sha)
    return code == 0


async def abort_merge(repo: Path) -> tuple[bool, str]:
    """Undo a stopped merge so the worktree holds exactly what the coder left
    (no conflict markers, no staged partial merge, same HEAD)."""
    code, out = await _run(repo, "merge", "--abort")
    if code != 0:
        return False, out
    if await merge_in_progress(repo):
        return False, f"MERGE_HEAD still present after --abort: {out}"
    return True, out


async def merge_base_into_branch(
    worktree: Path,
    branch: str,
    base: str = "main",
    author: tuple[str, str] = DEFAULT_GIT_AUTHOR,
) -> MergeResult:
    """Merge `origin/<base>` into the CHECKED OUT `branch`, before the push.

    `author` is the deployment's configured commit identity (Config.git_author,
    the same tuple every mission commit is made with): the merge commit must not
    add a second author to one branch's history.

    Returns a structured result; never raises for the expected cases:
      skipped_reason: no-origin / wrong-branch / dirty-worktree / fetch-failed
      up_to_date:     origin/<base> is already an ancestor of the branch
      merged + sha:   clean merge commit, worktree left clean
      conflicted + conflicting: unmerged paths exist, abort already run
      failed + detail: git refused the merge and named no paths (unrelated
                      histories, an index that could not be written)
    """
    if not await has_origin(worktree):
        return MergeResult(skipped_reason="no-origin")
    # _create_pr falls back to the daemon's shared clone when the artifact has
    # no worktree: its HEAD is NOT the coder branch (often detached), and
    # merging there would move the wrong ref.
    current = await head_branch_name(worktree)
    if current != branch:
        return MergeResult(skipped_reason="wrong-branch")
    # A merge a DEAD daemon left in progress (MERGE_HEAD + unmerged index) makes
    # `status --porcelain` non-empty, so the dirty guard below would skip it and
    # leave the conflict markers in place for a later `git add -A` to commit.
    # Nothing can be resuming that merge: the only merge this worktree ever runs
    # is ours, synchronously, in this process. So abort it first and continue.
    discarded: str | None = None
    if await merge_in_progress(worktree):
        # whatever staged work the abort discards is kept on a ref first, so a
        # crash that left BOTH a stopped merge and staged edits loses nothing
        discarded = await stash_staged_work(worktree)
        await abort_merge(worktree)  # best effort: the dirty guard still holds
    code, out = await _run(worktree, "status", "--porcelain")
    if code != 0:
        return MergeResult(skipped_reason="dirty-worktree", discarded_work=discarded)
    if out.strip():
        # uncommitted work: a merge could clobber it, and stage_all would fold
        # it into the merge commit. Not ours to clean up here.
        return MergeResult(skipped_reason="dirty-worktree", discarded_work=discarded)
    fetched, fetch_out = await fetch_origin(worktree, base)
    if not fetched:
        return MergeResult(
            skipped_reason="fetch-failed", detail=fetch_out, discarded_work=discarded
        )
    remote_base = f"origin/{base}"
    code, _ = await _run(worktree, "merge-base", "--is-ancestor", remote_base, "HEAD")
    if code == 0:
        return MergeResult(up_to_date=True, discarded_work=discarded)
    code, out = await _run(
        worktree,
        "-c",
        f"user.name={author[0]}",
        "-c",
        f"user.email={author[1]}",
        "merge",
        "--no-ff",
        "-m",
        f"Merge {remote_base} into {branch}",
        remote_base,
        timeout_s=120.0,
    )
    if code != 0:
        # paths FIRST: after --abort the unmerged index entries are gone
        paths = await conflict_paths(worktree)
        if not paths:
            # git REFUSED the merge instead of conflicting (unrelated histories,
            # an index it could not write): it left nothing to abort, and the PR
            # body must not claim conflicts with zero named paths.
            abort_failed = None
            if await merge_in_progress(worktree):  # a partial merge did start
                aborted, abort_out = await abort_merge(worktree)
                abort_failed = None if aborted else (abort_out or out)
            return MergeResult(
                failed=True,
                abort_failed=abort_failed,
                detail=out,
                discarded_work=discarded,
            )
        aborted, abort_out = await abort_merge(worktree)
        return MergeResult(
            conflicted=True,
            conflicting=paths,
            abort_failed=None if aborted else (abort_out or out),
            detail=out,
            discarded_work=discarded,
        )
    sha = await head_sha(worktree)
    return MergeResult(merged=True, sha=sha, discarded_work=discarded)


async def head_branch_name(worktree: Path) -> str:
    """The branch checked out here, or "" when HEAD is detached (the daemon's
    shared clone). Unlike `current_branch` this never raises."""
    code, out = await _run(worktree, "rev-parse", "--abbrev-ref", "HEAD")
    name = out.strip()
    if code != 0 or name == "HEAD":
        return ""
    return name
