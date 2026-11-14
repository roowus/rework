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
import contextlib
import fcntl
import io
import os
import shutil
import subprocess
import tarfile
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

from aiohttp import web

from rework.kernel.state.store import StateStore
from rework.roles.builtin.coder import DEFAULT_GITIGNORE

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
    into the project area and a pull gives back what was pushed. Sync.

    Every path must exist: a typo'd path used to pack to an EMPTY tarball, so
    the daemon answered 200 and `add-files` looked like it had worked while
    nothing was added. `ValueError` is what the CLI turns into a red message
    and exit 1."""
    roots: list[Path] = []
    for src in paths:
        root = src.expanduser().resolve()
        if not root.exists():
            raise ValueError(f"{src}: no such file or folder")
        roots.append(root)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for root in roots:
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
    Sync by design like the rest of the packing helpers; to_thread wraps.

    `-c core.hooksPath=/dev/null` is not negotiable: an uploaded bundle can
    plant `.git/hooks/*` (uploads are tar files from the network), and a plain
    `git commit` would then EXECUTE the planted hook on the daemon."""
    subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=dest,
        check=True,
        capture_output=True,
    )


def _git_out(dest: Path, *args: str) -> str:
    """stdout of a git command, "" when it fails (callers treat that as
    "nothing changed" rather than failing a mission over bookkeeping)."""
    try:
        done = subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", *args],
            cwd=dest,
            capture_output=True,
            text=True,
        )
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
    """Extract a received tarball into `dest`, member by member.

    `filter="data"` alone is not enough: it does not use the member's own type
    to decide whether a nested member may be written, so `s` (a symlink to
    `.git`) plus `s/hooks/pre-commit` writes into the repo next to it — and the
    next `git commit` runs that hook on the daemon. Only regular files and
    directories are extracted at all, and anything `_excluded()` rejects (the
    whole junk list, `.git` included, at any depth) is never written."""
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        members = [info for info in tar.getmembers() if _safe_member(info)]
        tar.extractall(dest, members=members, filter="data")


def _safe_member(info: tarfile.TarInfo) -> bool:
    """A member is written only if it is a plain file or directory whose
    normalised, non-escaping path survives `_excluded()`."""
    if not (info.isreg() or info.isdir()):
        return False  # symlinks/hardlinks/devices: never, they are a write-elsewhere trick
    name = info.name.replace("\\", "/")
    rel = Path(name)
    if rel.is_absolute() or ".." in rel.parts:
        return False  # py3.12's data filter also blocks this; keep it explicit
    return not _excluded(Path(os.path.normpath(name)))


def stage_bundle(workroot: Path, data: bytes, name: str | None = None) -> Path:
    """Extract an uploaded bundle under workroot/workspaces/<id>, git-init it
    (the coder's worktree stage needs a repo, mirroring the local
    _prepare_repo), and return the staged path. Sync; to_thread wraps."""
    ws_id = f"ws-{uuid.uuid4().hex[:12]}"
    dest = workroot / "workspaces" / ws_id / (name or "repo")
    unpack_into(dest, data)
    git_init_commit(dest, "init")  # empty folder still stages a repo
    return dest


def seed_mission_workspace(project_id: str, dest: Path) -> Path:
    """Upload transport: a repo-less project's files area IS the mission's
    workspace, so copy it into the workroot and git-init the copy exactly as
    stage_bundle does. Roles, worktrees and read_root then need no special
    case. An empty (or missing) files area still yields a git-initialised
    workspace, so the mission starts with nothing but can still write.
    Idempotent across restarts: an existing seeded workspace is reused.

    This is a READ of the area, so it takes `_area_lock`: seeding out of a
    half-written area (an `add-files` POST landing at the same moment) would
    start the mission from a torn tree. The lock is taken ONCE per path —
    `ensure_files_area` never locks, so there is nothing to re-enter."""
    if (dest / ".git").is_dir():
        return dest  # already seeded by an earlier attempt: nothing to read
    with _area_lock(project_id):
        area = ensure_files_area(project_id)
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            area,
            dest,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns(*EXCLUDE_DIRS, *EXCLUDE_SUFFIXES),
        )
    git_init_commit(dest, "init")
    return dest


def archive_files_area(project_id: str) -> bytes:
    """The project's files area as a tar.gz (what `rework project pull` gets).

    A READ, so it takes `_area_lock` the same way every mutation does: packing
    while a save-back or an `add-files` POST is writing would ship a torn area
    (and `pack_directory` walks the tree, so it can trip over a file that
    vanished mid-walk)."""
    with _area_lock(project_id):
        return pack_directory(ensure_files_area(project_id))


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


def _z_fields(raw: str) -> list[str]:
    """NUL-split git `-z` output. NUL separation is the only parse that
    survives a non-ASCII name: without `-z`, git octal-quotes anything outside
    ASCII (`"R\303\251sum\303\251.md"`) and the quoted name never matches a real
    file, so the file silently vanished from the area and from the report."""
    fields = raw.split("\0")
    if fields and fields[-1] == "":
        fields.pop()  # trailing NUL terminates the last entry
    return fields


def _status_changes(dest: Path) -> list[tuple[str, str]]:
    """[(path, "new"|"changed")] from git status, deletions skipped."""
    out: list[tuple[str, str]] = []
    fields = _z_fields(_git_out(dest, "status", "--porcelain", "-z", "--untracked-files=all"))
    index = 0
    while index < len(fields):
        # porcelain is "XY <path>": both status columns are always present, so
        # the path starts at offset 3 even when Y is a space ("R  new.md")
        field = fields[index]
        code, rel = field[:2], field[3:]
        index += 1
        if code[:1] in ("R", "C"):
            index += 1  # "R  new\0old\0": the new name is what the area gets
        if not rel or _excluded(Path(rel)):
            continue
        out.append((rel, "new" if code.strip() == "??" else "changed"))
    return out


def _untracked_new(dest: Path) -> list[tuple[str, str]]:
    """[(path, "new")] for the tree's untracked files, IGNORING its .gitignore.

    The coder role writes DEFAULT_GITIGNORE (dist/, build/) into every
    worktree, and `git status` then hides a real deliverable written to
    `dist/out.txt`: it used to be silently dropped from the files area AND the
    report. Listing without `--exclude-standard` surfaces it; `_excluded()`
    still drops the genuine junk (.git, node_modules, __pycache__, …) at any
    depth, so a worktree-local ignore file cannot smuggle junk back in."""
    out: list[tuple[str, str]] = []
    for rel in _z_fields(_git_out(dest, "ls-files", "--others", "-z")):
        if not rel or _excluded(Path(rel)):
            continue
        out.append((rel, "new"))
    return out


def update_files_area(project_id: str, data: bytes, message: str) -> list[str]:
    """Unpack a bundle over the project's files area and commit it. Returns the
    touched paths as "a.md (new)" / "b.md (changed)". Sync; to_thread wraps.

    Uploading files the area already holds is a success with an empty list,
    not an error: there is simply nothing to commit."""
    with _area_lock(project_id):
        area = ensure_files_area(project_id)
        unpack_into(area, data)
        _git(area, "add", "-A")
        if not _has_staged_changes(area):
            return []  # identical files again: nothing to commit, a success
        changes = _staged_changes(area)
        _commit(area, message)
        return [f"{rel} ({kind})" for rel, kind in changes]


def _name_status_changes(raw: str) -> list[tuple[str, str]]:
    """[(path, "new"|"changed")] from `git diff --name-status -z` output.

    NUL-separated records are "<CODE>\0<path>\0" (a rename/removal adds the
    NEW path then the old), so codes and paths alternate. Deletions are
    skipped: a repo-less project is additive by design."""
    out: list[tuple[str, str]] = []
    fields = _z_fields(raw)
    index = 0
    while index < len(fields):
        code = fields[index]
        index += 1
        if index >= len(fields):
            break
        rel = fields[index]
        index += 1
        if code[:1] in ("R", "C") and index < len(fields):
            rel = fields[index]  # keep the new name, not the old one
            index += 1
        if code[:1] == "D" or not rel or _excluded(Path(rel)):
            continue
        out.append((rel, "new" if code[:1] == "A" else "changed"))
    return out


def _has_staged_changes(area: Path) -> bool:
    """True when the area's index holds something to commit (`git diff --cached
    --quiet` exits 1 then; `--quiet` prints nothing either way, so the exit code
    is the only signal). This guard is what keeps an EMPTY `git commit` — which
    exits 1 and surfaced as "Project files NOT updated" — from ever running."""
    try:
        done = subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "diff", "--cached", "--quiet"],
            cwd=area,
            capture_output=True,
        )
    except OSError:
        return False
    return done.returncode == 1


def _staged_changes(area: Path) -> list[tuple[str, str]]:
    """[(path, "new"|"changed")] actually staged in the area's index right now
    (the same parse `mission_changes` uses, over `--cached`)."""
    return _name_status_changes(_git_out(area, "diff", "--cached", "--name-status", "-z"))


def _stage_paths(area: Path, rels: list[str]) -> None:
    """Force-stage exactly these paths. A bare `git add -A` let the AREA's own
    `.gitignore` (an owner-authored `*.log`) hide a file the mission really
    produced: the save-back then looked like a no-op and the deliverable stayed
    unversioned in the area. `-f` is the whole point here; callers only pass
    paths `_excluded()` accepted, so junk can never be force-added.

    `--literal-pathspecs` (a GLOBAL option, so it precedes the subcommand) is
    what makes these paths DATA: a mission-produced `:x.md` or `*.txt` is
    pathspec magic to git, which exited 128 and turned the whole save-back into
    a reported failure even though the files had already been copied."""
    if not rels:
        return
    _git(area, "--literal-pathspecs", "add", "-f", "--", *rels)


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
    for rel, kind in _name_status_changes(_git_out(tree, "diff", "--name-status", "-z", base)):
        changes[rel] = kind
    # untracked files never show in a diff — and they must be listed WITHOUT
    # the tree's own .gitignore, or the coder's boilerplate dist//build/ entry
    # makes a real deliverable invisible (see _untracked_new)
    for rel, kind in _untracked_new(tree):
        changes.setdefault(rel, kind)
    return sorted(changes.items())


def save_mission_files(project_id: str, trees: list[Path], message: str) -> list[str]:
    """Upload transport: copy what a finished mission produced into the
    project's files area and commit it there with a message naming the source.

    `trees` are the mission's own checkouts OLDEST FIRST — the seeded
    workspace, then each role worktree in run order — because for each path
    the LAST copy wins. Passing them newest-first (reversed) once let an
    earlier coder's stale revision overwrite the audit-revised FINAL one.
    Returns the report lines, e.g. ["a.md (new)", "b.md (changed)"]. Sync;
    to_thread wraps."""
    with _area_lock(project_id):
        area = ensure_files_area(project_id)
        kinds: dict[str, str] = {}
        for tree in trees:
            if not (tree / ".git").exists():
                continue
            for rel, kind in mission_changes(tree):
                source = tree / rel
                if not source.is_file():
                    continue  # a second checkout no longer has it: the other one wins
                if _template_gitignore(source, tree):
                    # the coder role writes DEFAULT_GITIGNORE into every
                    # worktree: it is scaffolding, not mission output
                    continue
                target = area / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                kinds[rel] = kind
        # stage the mission's own paths, FORCEd: the area may carry an
        # owner-authored .gitignore (`*.log`) that a bare `git add -A` would
        # honour, leaving a real deliverable unversioned and the save-back
        # looking like a no-op. kinds already passed `_excluded()`.
        _stage_paths(area, sorted(kinds))
        staged = _staged_changes(area)
        if not staged:
            # nothing new to record (a second save of the same trees, a
            # recovery re-synthesize, or a mission whose output the area
            # already holds): the area has the content, so this is a no-op
            # SUCCESS — never a `git commit` with an empty index, which exits 1
            # and used to be reported as "Project files NOT updated"
            return []
        _commit(area, message)
        # report what the area actually versioned, not every file copied
        return [f"{rel} ({kind})" for rel, kind in staged]


def _template_gitignore(path: Path, tree: Path) -> bool:
    """True for the coder's own boilerplate ignore file at the tree root.
    Saving it back would both pollute the report (a mission that wrote nothing
    would report a file) and, worse, make later `git add -A` calls in the area
    skip dist/ and build/, so real deliverables under those names would never
    be saved."""
    if path.name != ".gitignore" or path.parent != tree:
        return False
    try:
        return path.read_text() == DEFAULT_GITIGNORE
    except (OSError, UnicodeDecodeError):
        return False


def _commit(area: Path, message: str) -> None:
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


AREA_LOCK_TIMEOUT_S = 120.0


@contextlib.contextmanager
def _area_lock(project_id: str, timeout_s: float = AREA_LOCK_TIMEOUT_S) -> Iterator[None]:
    """Serialise every mutation of one project's files area across threads AND
    processes: the HTTP routes (aiohttp's thread pool) and the engine both
    write here, and two concurrent `git add`/`commit` runs race on
    `.git/index.lock`. An flock on a file NEXT TO the area (never inside it, so
    it can never be committed) is the smallest portable tool for both; a plain
    asyncio lock cannot help because the callers are synchronous."""
    from rework.config import project_files_dir

    lock_path = project_files_dir(project_id).parent / ".files.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+") as handle:
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"project {project_id} files area is busy (no lock after {timeout_s:.0f}s)"
                    ) from None
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def pack_worktree(worktree: Path) -> bytes:
    """Pack a finished worktree for the fetch results path."""
    return pack_directory(worktree)


# uploads carry real documents (PDFs, docx, slide decks), not just markdown:
# aiohttp's 1 MiB default rejected them with a bare 413
MAX_UPLOAD_BYTES = 64 * 1024 * 1024


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
        """Upload local files into a repo-less project's persistent area.

        Body: a tar.gz of up to 64 MiB (the app's client_max_size; PDFs and
        docx are routinely over aiohttp's 1 MiB default), or 413 above it.
        `.git` members are dropped: an upload may never plant a git hook."""
        if not _authorized(request, token):
            return web.json_response({"error": "unauthorized"}, status=401)
        project_id = request.match_info["project_id"]
        if _bad_project_id(project_id):
            return web.json_response({"error": "bad project id"}, status=400)
        if await store.get_project(project_id) is None:
            return web.json_response({"error": f"unknown project {project_id}"}, status=404)
        if request.content_length is not None and request.content_length > MAX_UPLOAD_BYTES:
            return web.json_response(
                {"error": f"upload too large (max {MAX_UPLOAD_BYTES // (1024 * 1024)} MiB)"},
                status=413,
            )
        data = await request.read()
        if not data:
            return web.json_response({"error": "empty body (want a tar.gz)"}, status=400)
        source = (request.query.get("source") or "add-files").strip()
        message = f"{source}: files added to project {project_id}"
        try:
            changed = await asyncio.to_thread(update_files_area, project_id, data, message)
        except TimeoutError as exc:
            # the area is locked by a mission's save-back or another upload:
            # 400 "not a readable tar.gz" was a lie, and the client retried a
            # body that was perfectly fine
            return web.json_response(
                {"error": f"project {project_id} files area is busy: {exc}"}, status=503
            )
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
        # the archive is a read of the area, so it locks like every mutation:
        # packing a half-written area (a concurrent save-back) shipped a torn
        # tree to `rework project pull`
        try:
            data = await asyncio.to_thread(archive_files_area, project_id)
        except TimeoutError as exc:
            return web.json_response(
                {"error": f"project {project_id} files area is busy: {exc}"}, status=503
            )
        return web.Response(
            body=data,
            content_type="application/gzip",
            headers={"Content-Disposition": f'attachment; filename="{project_id}-files.tar.gz"'},
        )

    app = web.Application(client_max_size=MAX_UPLOAD_BYTES)
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
