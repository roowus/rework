"""PR previews (ADR 0008): run a pull request's build as a second, fully
isolated rework instance without touching the real one.

One command does it all: `rework preview <n>` fetches the PR head into a git
worktree under ``~/.rework/previews/<n>/``, syncs its venv, copies the state DB,
picks a free port, starts the preview's own daemon in the background, and runs
that build's TUI in the same terminal. When the TUI exits the daemon is stopped
and the preview removed, unless ``--keep`` was passed.

Everything here is deliberately plain: subprocess calls, no daemon imports, so
the pieces can be unit-tested without spawning anything.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DESCRIPTOR_KEYS = (
    "pr",
    "head_sha",
    "branch",
    "worktree",
    "state_db",
    "host",
    "port",
    "pid",
    "daemon_url",
    "created_at",
    "repo_dir",
)


def preview_root() -> Path:
    """Where previews live: REWORK_PREVIEW_DIR, default ~/.rework/previews."""
    return Path(os.environ.get("REWORK_PREVIEW_DIR", "~/.rework/previews")).expanduser()


def preview_dir(n: int) -> Path:
    return preview_root() / str(n)


def descriptor_path(n: int) -> Path:
    return preview_root() / f"{n}.json"


def is_postgres(db: str) -> bool:
    """Postgres state cannot be copied with sqlite's backup API. Path() collapses
    "postgresql://" to "postgresql:/", so the single-slash forms count too."""
    return db.startswith(
        ("postgres://", "postgresql://", "postgresql+", "postgres:/", "postgresql:/")
    )


def free_port() -> int:
    """Ask the OS for an unused localhost port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _ps_command(pid: int) -> str | None:
    """The full command line of pid via ps(1) (macOS and Linux both accept
    `-p <pid> -o command=`), or None when ps cannot tell us."""
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def is_alive(pid: int | None, marker: str | None = None) -> bool:
    """True only for a live process that is recognisably ours: `os.kill(pid, 0)`
    also succeeds for an unrelated process after pid reuse, so the command line
    must mention `rework` (or the preview worktree). Never signal a pid this
    says False for."""
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError):
        return False
    command = _ps_command(int(pid))
    if command is None:
        return False
    markers = ["rework"]
    if marker:
        markers.append(str(marker))
    return any(m in command for m in markers)


def stop_group(pid: int | str | None) -> None:
    """SIGTERM then SIGKILL the whole process group of a backgrounded preview
    daemon. The descriptor records the `uv run` wrapper's pid (uv does not
    exec), so killing the pid alone leaves the real daemon running; spawn_daemon
    used start_new_session, so that pid is the group leader."""
    try:
        pgid = int(pid)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return
    if not is_alive(pgid):
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except OSError:
            try:
                os.kill(pgid, sig)
            except OSError:
                return
        for _ in range(50):  # up to ~5s for the group to go away
            if not is_alive(pgid):
                return
            time.sleep(0.1)


def _sqlite_backup(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    src_conn = sqlite3.connect(str(src))
    try:
        dst_conn = sqlite3.connect(str(dst))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


def copy_state_db(src: Path, dst: Path) -> None:
    """Online backup of the state DB, then drop the daemon lock: the preview
    must be able to take the lock its source holds."""
    _sqlite_backup(src, dst)
    conn = sqlite3.connect(str(dst))
    try:
        try:
            conn.execute("DELETE FROM daemon_lock")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # no daemon_lock table in this schema version
    finally:
        conn.close()


def fresh_sqlite(path: Path) -> None:
    """Deliberately create an empty SQLite database at path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    sqlite3.connect(str(path)).close()


def prepare_state(n: int, src_db: Path) -> tuple[Path, str | None]:
    """The preview's state DB. Postgres cannot be copied this way, so a preview
    of a Postgres-backed instance starts on a fresh SQLite file instead; a
    missing sqlite source does too, out loud (`sqlite3.connect` would silently
    create an empty file anyway). The notice is printed and returned."""
    pdir = preview_dir(n)
    pdir.mkdir(parents=True, exist_ok=True)
    state_db = pdir / "state.sqlite3"
    if is_postgres(str(src_db)):
        notice = "REWORK_DB is Postgres; starting preview on a fresh SQLite database"
    elif not src_db.exists():
        fresh_sqlite(state_db)
        notice = f"source DB {src_db} not found; starting preview on a fresh SQLite database"
    else:
        copy_state_db(src_db, state_db)
        return state_db, None
    print(notice)
    return state_db, notice


def prepare_checkpoints(n: int) -> Path | None:
    """Copy the checkpoints DB alongside the state copy, when it is a local
    sqlite file (Postgres checkpoints stay shared, so the preview just points at
    them; a missing file is not an error)."""
    raw = os.environ.get("REWORK_CHECKPOINTS_DB")
    if not raw:
        return None
    src = Path(raw).expanduser()
    if is_postgres(str(src)) or not src.exists():
        return None
    dst = preview_dir(n) / "checkpoints.sqlite3"
    copy_state_db(src, dst)
    return dst


def source_state_db() -> Path:
    return Path(os.environ.get("REWORK_DB", "~/.rework/state.sqlite3")).expanduser()


def registered_worktrees(repo_dir: Path) -> list[tuple[str, str]]:
    """(path, branch) for every worktree git has registered, from
    `git worktree list --porcelain`."""
    out = subprocess.run(
        ["git", "-C", str(repo_dir), "worktree", "list", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    entries: list[tuple[str, str]] = []
    path, branch = "", ""
    for line in [*out.splitlines(), ""]:
        if line.startswith("worktree "):
            path, branch = line[len("worktree ") :], ""
        elif line.startswith("branch "):
            branch = line[len("branch ") :].removeprefix("refs/heads/")
        elif not line.strip() and path:
            entries.append((path, branch))
            path, branch = "", ""
    return entries


def fetch_worktree(n: int, repo_dir: Path) -> Path:
    """Fetch refs/pull/<n>/head into origin/pr/<n> and check it out as a
    worktree on branch rework/preview-pr-<n>.

    Re-runnable: when the branch is already checked out at the conventional
    path the worktree is reset onto the freshly fetched ref instead of failing
    with "already used by worktree", and a registration at a stale path is
    removed first."""
    worktree = preview_dir(n) / "repo"
    worktree.parent.mkdir(parents=True, exist_ok=True)
    branch = f"rework/preview-pr-{n}"
    ref = f"origin/pr/{n}"
    subprocess.run(
        ["git", "-C", str(repo_dir), "fetch", "origin", f"+refs/pull/{n}/head:refs/remotes/{ref}"],
        check=True,
    )
    for path, registered in registered_worktrees(repo_dir):
        if registered != branch:
            continue
        if Path(path) == worktree:
            subprocess.run(["git", "-C", str(worktree), "reset", "--hard", ref], check=True)
            return worktree
        subprocess.run(
            ["git", "-C", str(repo_dir), "worktree", "remove", "--force", path],
            capture_output=True,
        )
        subprocess.run(["git", "-C", str(repo_dir), "worktree", "prune"], capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo_dir), "worktree", "add", "-B", branch, str(worktree), ref],
        check=True,
    )
    return worktree


def head_sha(worktree: Path) -> str:
    out = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


def write_descriptor(n: int, data: dict[str, Any]) -> Path:
    path = descriptor_path(n)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
    return path


def load_descriptor(n: int) -> dict[str, Any] | None:
    path = descriptor_path(n)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def preview_env(
    n: int,
    worktree: Path,
    state_db: Path,
    port: int,
    *,
    allow_missions: bool = False,
    checkpoints_db: Path | None = None,
) -> dict[str, str]:
    """The full environment for the preview's daemon and TUI: its own state,
    library, logs, results, reports and TUI settings; its own port."""
    pdir = preview_dir(n)
    for name in ("library", "logs", "results", "reports"):
        (pdir / name).mkdir(parents=True, exist_ok=True)
    daemon_url = f"http://127.0.0.1:{port}"
    env = dict(os.environ)
    env["REWORK_PREVIEW"] = str(n)
    if allow_missions:
        env["REWORK_PREVIEW_ALLOW_MISSIONS"] = "1"
    else:
        env.pop("REWORK_PREVIEW_ALLOW_MISSIONS", None)
    env["REWORK_DB"] = str(state_db)
    if checkpoints_db is not None:
        env["REWORK_CHECKPOINTS_DB"] = str(checkpoints_db)
    env["REWORK_HTTP_HOST"] = "127.0.0.1"
    env["REWORK_HTTP_PORT"] = str(port)
    env["REWORK_DAEMON_URL"] = daemon_url
    env["REWORK_LIBRARY_DIR"] = str(pdir / "library")
    env["REWORK_LOG_DIR"] = str(pdir / "logs")
    env["REWORK_RESULTS_DIR"] = str(pdir / "results")
    env["REWORK_REPORTS_DIR"] = str(pdir / "reports")
    env["REWORK_TUI_SETTINGS"] = str(pdir / "tui.json")
    return env


def tail_log(path: Path, lines: int = 20) -> str:
    try:
        content = path.read_text(errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(content[-lines:])


def spawn_daemon(n: int, worktree: Path, env: dict[str, str]) -> subprocess.Popen[bytes]:
    """Start the preview build's own daemon in the background, logging to the
    preview dir. Its own session, so the TUI's terminal signals do not reach it
    and teardown can signal the whole group."""
    log_path = preview_dir(n) / "daemon.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_path.open("ab")
    try:
        proc = subprocess.Popen(
            ["uv", "run", "--project", str(worktree), "rework", "serve"],
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            cwd=str(worktree),
            start_new_session=True,
        )
    except Exception:
        log_file.close()
        raise
    proc._rework_log = log_file  # type: ignore[attr-defined]
    return proc


def wait_ready(daemon_url: str, proc: subprocess.Popen[bytes], timeout_s: float = 60.0) -> bool:
    """Wait until the daemon answers GET /healthz with 200 and an ok body (the
    route in rework/orchestrator/workspace.py). Any other answer -- a stranger's
    service on the port, a 404, a refused connection -- is not our daemon, so
    keep polling; give up early once the process died."""
    deadline = time.monotonic() + timeout_s
    url = daemon_url.rstrip("/") + "/healthz"
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(url, timeout=2.0) as resp:
                if resp.status == 200 and "ok" in resp.read(2048).decode("utf-8", "replace"):
                    return True
        except urllib.error.HTTPError:
            pass
        except Exception:
            pass
        time.sleep(0.5)
    return False


def stop_daemon(proc: subprocess.Popen[bytes] | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except OSError:
        try:
            proc.terminate()
        except OSError:
            pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    log_file = getattr(proc, "_rework_log", None)
    if log_file is not None:
        log_file.close()


def run_preview_tui(n: int, worktree: Path, env: dict[str, str]) -> int:
    """The preview build's TUI in this terminal, labelled PREVIEW #<n>."""
    env2 = dict(env)
    env2["REWORK_TARGET_NAME"] = f"PREVIEW #{n}"
    return subprocess.run(
        ["uv", "run", "--project", str(worktree), "rework", "tui"],
        env=env2,
        cwd=str(worktree),
    ).returncode


def keep_notice(n: int, daemon_url: str) -> None:
    """How to get back into a preview that was left running."""
    print(f"preview #{n} kept: {descriptor_path(n)}")
    print(f"  daemon: {daemon_url}")
    print(f"  attach: REWORK_PREVIEW={n} rework tui")
    print(f"  remove: rework preview --rm {n}")


def create_preview(
    n: int,
    repo_dir: Path,
    keep: bool = False,
    allow_missions: bool = False,
    port: int | None = None,
) -> int:
    """Build, start, drive and (unless kept) tear down preview #n."""
    repo_dir = Path(repo_dir).expanduser()
    worktree_created = False
    try:
        worktree = fetch_worktree(n, repo_dir)
        worktree_created = True
        subprocess.run(["uv", "sync", "--project", str(worktree)], check=True)

        state_db, _ = prepare_state(n, source_state_db())
        checkpoints_db = prepare_checkpoints(n)
    except BaseException:
        # a setup failure must not leak a registered worktree
        if worktree_created and not keep:
            remove_preview(n, repo_dir)
        raise

    port = port or free_port()
    daemon_url = f"http://127.0.0.1:{port}"
    descriptor = {
        "pr": n,
        "head_sha": head_sha(worktree),
        "branch": f"rework/preview-pr-{n}",
        "worktree": str(worktree),
        "state_db": str(state_db),
        "host": "127.0.0.1",
        "port": port,
        "pid": None,
        "daemon_url": daemon_url,
        "created_at": datetime.now(UTC).isoformat(),
        "repo_dir": str(repo_dir),
    }
    write_descriptor(n, descriptor)

    env = preview_env(
        n,
        worktree,
        state_db,
        port,
        allow_missions=allow_missions,
        checkpoints_db=checkpoints_db,
    )
    print(f"starting preview daemon on {daemon_url} (logs: {preview_dir(n) / 'daemon.log'})")
    proc = spawn_daemon(n, worktree, env)
    descriptor["pid"] = proc.pid
    write_descriptor(n, descriptor)

    log_path = preview_dir(n) / "daemon.log"
    if not wait_ready(daemon_url, proc):
        print(f"preview daemon for PR {n} did not come up; last log lines:")
        print(tail_log(log_path))
        stop_daemon(proc)
        if not keep:
            remove_preview(n, repo_dir)
        raise RuntimeError(f"preview daemon for PR {n} failed to start (see {log_path})")

    try:
        rc = run_preview_tui(n, worktree, env)
    except BaseException:
        stop_daemon(proc)
        if not keep:
            remove_preview(n, repo_dir)
        raise
    if rc != 0:
        # the TUI is a client: a failed one says nothing about the daemon, so
        # report where its log is before any teardown removes it
        print(f"preview #{n}: TUI exited with status {rc}; daemon log: {log_path}")
        if keep:
            keep_notice(n, daemon_url)
        else:
            stop_daemon(proc)
            remove_preview(n, repo_dir)
        return rc
    if keep:
        keep_notice(n, daemon_url)
    else:
        stop_daemon(proc)
        remove_preview(n, repo_dir)
    return 0


def list_previews() -> list[dict[str, Any]]:
    """Every descriptor on disk, with a liveness flag for its daemon."""
    root = preview_root()
    rows: list[dict[str, Any]] = []
    if not root.exists():
        return rows
    for path in sorted(root.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        data["alive"] = is_alive(data.get("pid"))
        rows.append(data)
    return rows


def resolve_repo_dir(desc: dict[str, Any], repo_dir: Path | None) -> Path | None:
    """Which repo holds the preview's worktree registration: an explicit
    --repo-dir, the descriptor, or the git repo we are standing in."""
    if repo_dir is not None:
        return Path(repo_dir).expanduser()
    if desc.get("repo_dir"):
        return Path(str(desc["repo_dir"])).expanduser()
    top = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if top.returncode == 0 and top.stdout.strip():
        return Path(top.stdout.strip())
    return None


def remove_preview(n: int, repo_dir: Path | None = None) -> None:
    """Stop the preview's daemon and delete everything it owns.

    Works with a missing or corrupt descriptor too: the worktree at the
    conventional path is unregistered from the resolved repo (never just
    rmtree'd out from under git), and when no repo can be resolved the exact
    manual commands are printed instead of wedging the registration."""
    desc = load_descriptor(n) or {}
    stop_group(desc.get("pid"))
    branch = str(desc.get("branch") or f"rework/preview-pr-{n}")
    worktree = desc.get("worktree")
    path = Path(str(worktree)) if worktree else preview_dir(n) / "repo"
    resolved = resolve_repo_dir(desc, repo_dir)
    if resolved is None:
        print(f"preview #{n}: no repo to unregister its worktree from; run manually:")
        print("  git worktree prune")
        print(f"  git branch -D {branch}")
    else:
        subprocess.run(
            ["git", "-C", str(resolved), "worktree", "remove", "--force", str(path)],
            capture_output=True,
        )
        subprocess.run(["git", "-C", str(resolved), "worktree", "prune"], capture_output=True)
        subprocess.run(["git", "-C", str(resolved), "branch", "-D", branch], capture_output=True)
    shutil.rmtree(preview_dir(n), ignore_errors=True)
    descriptor_path(n).unlink(missing_ok=True)
