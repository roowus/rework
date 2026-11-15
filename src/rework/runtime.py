"""Is this process still running the code that is on disk?

A long-running rework process (the TUI, the daemon) can outlive its checkout:
merges land in the same tree while the process keeps the old modules in memory.
Found live (docs/plans/m7-findings.md, finding 28): a crash traceback pointed at
the wrong lines because the TUI ran code older than its checkout. Both processes
poll `CodeChangeWatcher` and SAY SO; neither ever restarts anything.

The fingerprint is deliberately cheap (a merge is rare, a tick is not): it walks
the installed package with `os.scandir`, reads no file contents, and uses file
mtimes plus directory mtimes (so an added or removed module is caught too).
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

# the one sentence both processes use, so the hint and the log line agree.
# The daemon cannot reload itself (a running mission must not be dropped for a
# deploy, finding 28), so it still asks for a restart...
CODE_CHANGED = "rework updated: restart to load"

# ...while the TUI now restarts ITSELF as soon as its owner is idle (finding
# 15), so its note says so instead of asking for something it does itself
CODE_CHANGED_IDLE = "rework updated: will reload when idle"

# a scan must never walk into these: version-control and tool caches are not code
SKIP_DIRS = frozenset(
    {"__pycache__", ".git", ".venv", ".mypy_cache", ".ruff_cache", ".pytest_cache"}
)

# how far up from the package dir to look for the checkout's .git
_GIT_LEVELS = 6


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def _common_dir(gitdir: Path) -> Path:
    """A linked worktree's git dir keeps its refs in the main repo (commondir)."""
    text = _read_text(gitdir / "commondir")
    if not text:
        return gitdir
    path = Path(text)
    return path if path.is_absolute() else (gitdir / path).resolve()


def _read_head(gitdir: Path) -> str | None:
    head = _read_text(gitdir / "HEAD")
    if head is None:
        return None
    if not head.startswith("ref:"):
        return head or None  # detached HEAD: the sha is right there
    name = head.split(":", 1)[1].strip()
    for base in (gitdir, _common_dir(gitdir)):
        sha = _read_text(base / name)
        if sha:
            return sha
    return None


def _git_head(start: Path) -> str | None:
    """HEAD of the checkout containing `start`, read from the git dir itself:
    no subprocess, and `.git` may be a file (worktree/submodule)."""
    directory = start
    for _ in range(_GIT_LEVELS):
        dot = directory / ".git"
        try:
            if dot.is_dir():
                return _read_head(dot)
            if dot.is_file():
                text = _read_text(dot) or ""
                if not text.startswith("gitdir:"):
                    return None
                target = Path(text.split(":", 1)[1].strip())
                if not target.is_absolute():
                    target = (directory / target).resolve()
                return _read_head(target)
        except OSError:
            return None
        if directory.parent == directory:
            return None
        directory = directory.parent
    return None


def short_commit(start: Path | None = None, length: int = 8) -> str:
    """The short sha of the code on disk, or "" when there is none (a pip
    install with no git tree). Read from the git dir, no subprocess."""
    sha = _git_head(start or Path(__file__).resolve().parent)
    return sha[:length] if sha else ""


def find_repo_root(start: Path | None = None) -> Path | None:
    """The checkout the installed package runs from, or None.

    The same upward walk `_git_head` does (`.git` may be a FILE for a worktree
    or a submodule), but it answers with the working-tree DIRECTORY rather than
    a sha: `rework update` has to run git IN that directory.
    """
    directory = (start if start is not None else Path(__file__).resolve().parent).resolve()
    for _ in range(_GIT_LEVELS):
        try:
            if (directory / ".git").exists():
                return directory
        except OSError:
            return None
        if directory.parent == directory:
            return None
        directory = directory.parent
    return None


def _scan(root: Path) -> tuple[int, int]:
    """(number of *.py files, newest mtime_ns) under root, contents never read.
    Directory mtimes count as well: they move when a module is added or deleted."""
    count = 0
    newest = 0
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    if entry.name in SKIP_DIRS:
                        continue
                    newest = max(newest, entry.stat(follow_symlinks=False).st_mtime_ns)
                    stack.append(Path(entry.path))
                elif entry.name.endswith(".py"):
                    count += 1
                    newest = max(newest, entry.stat(follow_symlinks=False).st_mtime_ns)
            except OSError:
                continue
    return count, newest


def source_fingerprint() -> str | None:
    """A cheap change signal for the installed rework package, or None when the
    tree cannot be read. None means "unknown", never "changed": every failure
    degrades silently, because a heartbeat must not die of a stat() call."""
    try:
        root = Path(__file__).resolve().parent
        count, newest = _scan(root)
        if count == 0 and newest == 0:
            return None
        return f"{count}:{newest}:{_git_head(root) or '-'}"
    except Exception:
        return None


class CodeChangeWatcher:
    """Remembers one fingerprint and fires exactly once per change.

    `poll()` is safe to call on every tick: it returns True only on the tick
    that first sees a different fingerprint, then adopts the new value so the
    caller is not nagged. An unreadable tree (None) is "unknown", never a
    change, so a transient stat() failure cannot fake an update.
    """

    def __init__(self, fingerprint_fn: Callable[[], str | None] | None = None) -> None:
        self._fn = fingerprint_fn  # resolved at call time, so it stays patchable
        self.value: str | None = self._read()

    def _read(self) -> str | None:
        fn = self._fn or source_fingerprint
        try:
            return fn()
        except Exception:
            return None

    def poll(self) -> bool:
        current = self._read()
        if current is None or current == self.value:
            return False
        changed = self.value is not None  # an unknown start is not a change
        self.value = current
        return changed
