"""Update the rework checkout: fetch origin/main, fast-forward, `uv sync`.

The owner's deployment is a git checkout run with uv (docs/ops.md), so an
update is exactly four git steps plus a sync: refuse when the tree is not in a
shape a fast-forward can land in (dirty, not on `main`, diverged), otherwise
fetch, fast-forward ONLY, and re-sync the environment. Nothing here restarts
anything: a running TUI or daemon reports the change and the owner restarts
(docs/plans/m7-findings.md, finding 28 — "nothing restarts itself"). The ONE
exception is `rework update --target NAME`, which restarts the *remote* systemd
service it just updated: that is the whole point of a deploy.

Everything degrades quietly: a missing checkout, an offline host or a broken
`uv` is a reason string, never an exception out of a background timer.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from rework.kernel.state.records import MissionStatus
from rework.runtime import find_repo_root

BRANCH = "main"
REMOTE = "origin"

# a fetch/merge is seconds; `uv sync` can install a whole environment
GIT_TIMEOUT_S = 120.0
UV_TIMEOUT_S = 900.0

# the daemon's opt-in auto-update cadence (serve --auto-update / REWORK_AUTO_UPDATE=1)
AUTO_UPDATE_SECONDS = 900.0

# the checkout and service name the deployment uses (docs/ops.md: the manual
# deploy was `cd ~/projects/rework && git pull --ff-only && uv sync && sudo
# systemctl restart rework`); `rework update --target NAME` runs this remotely
REMOTE_CHECKOUT = "~/projects/rework"
REMOTE_SERVICE = "rework"

# "mission busy": the same set tui/actions.ACTIVE uses for "a verb is legal",
# so the TUI, the CLI and the daemon never disagree about what in-flight means
ACTIVE_STATUSES = frozenset(
    {"pending", "running", "awaiting_gate", "on_hold"}  # == tui.actions.ACTIVE
)


def remote_update_command(*, check: bool = False) -> str:
    """What `rework update --target NAME` runs over ssh, as ONE line (the same
    shape as docs/ops.md's manual deploy, with the update itself delegated to
    the remote's own `rework update` so both paths share one implementation)."""
    flag = " --check" if check else ""
    if check:
        return f"cd {REMOTE_CHECKOUT} && uv run rework update{flag}"
    return (
        f"cd {REMOTE_CHECKOUT} && uv run rework update{flag}"
        f" && sudo systemctl restart {REMOTE_SERVICE}"
        f" && sleep 3 && systemctl is-active {REMOTE_SERVICE}"
    )


@dataclass(frozen=True)
class UpdateCheck:
    """What `origin/main` looks like from here, as a REPORT (nothing moved)."""

    repo_root: Path | None = None
    local: str | None = None  # HEAD sha
    remote: str | None = None  # origin/main sha
    ahead: int = 0  # commits origin/main has that HEAD does not
    behind: int = 0  # commits HEAD has that origin/main does not (diverged)
    error: str | None = None  # offline / no remote / not a checkout

    @property
    def available(self) -> bool:
        return self.error is None and self.ahead > 0


@dataclass(frozen=True)
class UpdateResult:
    """The outcome of `run_update`: a refusal is a REASON, never a traceback.

    `old`/`new`/`subjects` are filled in as soon as the fast-forward lands, so
    a failing `uv sync` still tells the owner what the tree moved to."""

    ok: bool
    reason: str | None = None
    old: str | None = None
    new: str | None = None
    subjects: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.ok and self.old and self.new and self.old != self.new)


def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_S,
        check=False,
    )


def _rev(repo_root: Path, rev: str) -> str | None:
    proc = _git(repo_root, "rev-parse", rev)
    sha = proc.stdout.strip()
    return sha if proc.returncode == 0 and sha else None


def _count(repo_root: Path, spec: str) -> int | None:
    proc = _git(repo_root, "rev-list", "--count", spec)
    text = proc.stdout.strip()
    return int(text) if proc.returncode == 0 and text.isdigit() else None


def _uv_sync(root: Path) -> subprocess.CompletedProcess[str]:
    """One seam for `uv sync`, so the update tests never need a real uv."""
    return subprocess.run(
        ["uv", "sync"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=UV_TIMEOUT_S,
        check=False,
    )


def _short(sha: str | None) -> str:
    return (sha or "?")[:12]


def check_update(repo_root: Path | None = None) -> UpdateCheck:
    """How far `origin/main` is ahead, WITHOUT touching the working tree.

    A fetch updates the remote-tracking ref only (never HEAD, never a file), so
    this is safe to run from a background timer. Any failure — offline, no
    remote, not a checkout — comes back as `error`, never as an exception.
    """
    root = repo_root if repo_root is not None else find_repo_root()
    if root is None:
        return UpdateCheck(error="cannot find the git checkout rework runs from")
    fetch = _git(root, "fetch", REMOTE, BRANCH)
    if fetch.returncode != 0:
        detail = (fetch.stderr or fetch.stdout).strip().splitlines()
        return UpdateCheck(
            repo_root=root,
            error=f"git fetch {REMOTE} {BRANCH} failed: {detail[-1] if detail else 'no output'}",
        )
    local = _rev(root, "HEAD")
    remote = _rev(root, f"{REMOTE}/{BRANCH}")
    if local is None or remote is None:
        return UpdateCheck(repo_root=root, error=f"no {REMOTE}/{BRANCH} to compare with")
    ahead = _count(root, f"HEAD..{REMOTE}/{BRANCH}") or 0
    behind = _count(root, f"{REMOTE}/{BRANCH}..HEAD") or 0
    return UpdateCheck(repo_root=root, local=local, remote=remote, ahead=ahead, behind=behind)


def check_remote(repo_root: Path | None = None) -> UpdateCheck | None:
    """The background-timer flavour of `check_update`: "unknown" is None.

    A timer must never raise and must never nag, so a missing checkout or an
    unreachable origin is silence (None), not an error to display."""
    check = check_update(repo_root)
    return None if check.error else check


def run_update(repo_root: Path | None = None, *, sync: bool = True) -> UpdateResult:
    """Fast-forward the checkout to `origin/main` and `uv sync` it.

    Refuses, with a reason and without moving anything, when the tree is dirty,
    when HEAD is not on `main`, or when the histories diverged (origin/main is
    not a fast-forward of HEAD). On success returns the old and new sha and the
    subject lines in between.
    """
    root = repo_root if repo_root is not None else find_repo_root()
    if root is None:
        return UpdateResult(ok=False, reason="cannot find the git checkout rework runs from")

    # -- the refusals, all read-only, all before anything moves ---------------
    status = _git(root, "status", "--porcelain")
    if status.returncode != 0:
        detail = (status.stderr or status.stdout).strip().splitlines()
        return UpdateResult(
            ok=False,
            reason=f"git status failed in {root}: {detail[-1] if detail else 'no output'}",
        )
    if status.stdout.strip():
        dirty = len(status.stdout.strip().splitlines())
        return UpdateResult(
            ok=False,
            reason=(
                f"uncommitted changes in {root} ({dirty} path(s)); commit or stash them"
                " before updating"
            ),
        )

    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    name = branch.stdout.strip()
    if branch.returncode != 0 or name != BRANCH:
        return UpdateResult(
            ok=False,
            reason=f"not on {BRANCH} (on {name or 'an unknown branch'}); switch to {BRANCH} first",
        )

    old = _rev(root, "HEAD")
    if old is None:
        return UpdateResult(ok=False, reason=f"cannot read HEAD in {root}")

    fetch = _git(root, "fetch", REMOTE, BRANCH)
    if fetch.returncode != 0:
        detail = (fetch.stderr or fetch.stdout).strip().splitlines()
        return UpdateResult(
            ok=False,
            reason=f"git fetch {REMOTE} {BRANCH} failed: {detail[-1] if detail else 'no output'}",
        )
    remote = _rev(root, f"{REMOTE}/{BRANCH}")
    if remote is None:
        return UpdateResult(ok=False, reason=f"no {REMOTE}/{BRANCH} to update to")
    if remote == old:
        return UpdateResult(ok=True, old=old, new=old, subjects=[])

    behind = _count(root, f"{REMOTE}/{BRANCH}..HEAD")
    if behind is None or behind > 0:
        return UpdateResult(
            ok=False,
            reason=(
                f"{REMOTE}/{BRANCH} is not a fast-forward of HEAD ({behind or '?'} local"
                f" commit(s) {REMOTE}/{BRANCH} does not have); reconcile by hand"
            ),
        )

    merge = _git(root, "merge", "--ff-only", f"{REMOTE}/{BRANCH}")
    if merge.returncode != 0:
        detail = (merge.stderr or merge.stdout).strip().splitlines()
        return UpdateResult(
            ok=False,
            old=old,
            reason=f"git merge --ff-only failed: {detail[-1] if detail else 'no output'}",
        )
    new = _rev(root, "HEAD") or old
    subjects = [
        line
        for line in _git(root, "log", "--format=%s", f"{old}..{new}").stdout.splitlines()
        if line.strip()
    ]

    if sync:
        try:
            proc = _uv_sync(root)
        except (OSError, subprocess.SubprocessError) as exc:
            return UpdateResult(
                ok=False,
                reason=f"uv sync failed to start in {root}: {exc}",
                old=old,
                new=new,
                subjects=subjects,
            )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip().splitlines()
            return UpdateResult(
                ok=False,
                reason=f"uv sync failed ({detail[-1] if detail else 'no output'})",
                old=old,
                new=new,
                subjects=subjects,
            )
    return UpdateResult(ok=True, old=old, new=new, subjects=subjects)


def should_auto_update(*, ahead: bool, busy: bool) -> bool:
    """The daemon's whole policy in one line, so it can be tested directly: an
    update must be AVAILABLE and nothing may be in flight. A mission mid-run is
    never dropped for a deploy (finding 28)."""
    return bool(ahead and not busy)


async def mission_busy(store: object) -> bool:
    """Is any mission running, queued or waiting on a gate?

    Derived from store rows (rule 3: the store is the source of truth), using
    the same status set tui/actions.ACTIVE uses, plus open gates — a gate is
    someone's decision, so a deploy must not land under it either."""
    for status in MissionStatus:
        if status.value not in ACTIVE_STATUSES:
            continue
        rows = await store.list_missions(status=status, limit=1)  # type: ignore[attr-defined]
        if rows:
            return True
    gates = await store.list_open_gates()  # type: ignore[attr-defined]
    return bool(gates)


def describe_update(result: UpdateResult) -> str:
    """One human line for a refusal, a no-op or a landed update (the CLI, the
    TUI toast and the daemon log all say the same thing)."""
    if not result.ok:
        return f"update refused: {result.reason}"
    if not result.changed:
        return f"already up to date ({_short(result.new)})"
    return (
        f"updated {_short(result.old)} -> {_short(result.new)}"
        f" ({len(result.subjects)} commit(s)); restart to load"
    )
