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


def is_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError):
        return False
    return True


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


def prepare_state(n: int, src_db: Path) -> tuple[Path, str | None]:
    """The preview's state DB. Postgres cannot be copied this way, so a preview
    of a Postgres-backed instance starts on a fresh SQLite file instead."""
    pdir = preview_dir(n)
    pdir.mkdir(parents=True, exist_ok=True)
    state_db = pdir / "state.sqlite3"
    if is_postgres(str(src_db)):
        return state_db, "REWORK_DB is Postgres; starting preview on a fresh SQLite database"
    copy_state_db(src_db, state_db)
    return state_db, None


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


def fetch_worktree(n: int, repo_dir: Path) -> Path:
    """Fetch refs/pull/<n>/head into origin/pr/<n> and check it out as a
    worktree on branch rework/preview-pr-<n>."""
    worktree = preview_dir(n) / "repo"
    worktree.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo_dir),
            "fetch",
            "origin",
            f"+refs/pull/{n}/head:refs/remotes/origin/pr/{n}",
        ],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repo_dir),
            "worktree",
            "add",
            "-B",
            f"rework/preview-pr-{n}",
            str(worktree),
            f"origin/pr/{n}",
        ],
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
    """Wait for any HTTP answer from the daemon (even 404 counts as up). Give up
    early if the process died."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        try:
            urllib.request.urlopen(daemon_url + "/healthz", timeout=2.0).close()
            return True
        except urllib.error.HTTPError:
            return True
        except Exception:
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


def create_preview(
    n: int,
    repo_dir: Path,
    keep: bool = False,
    allow_missions: bool = False,
    port: int | None = None,
) -> int:
    """Build, start, drive and (unless kept) tear down preview #n."""
    repo_dir = Path(repo_dir).expanduser()
    worktree = fetch_worktree(n, repo_dir)
    subprocess.run(["uv", "sync", "--project", str(worktree)], check=True)

    state_db, notice = prepare_state(n, source_state_db())
    if notice:
        print(notice)
    checkpoints_db = prepare_checkpoints(n)

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

    if not wait_ready(daemon_url, proc):
        log_path = preview_dir(n) / "daemon.log"
        print(f"preview daemon for PR {n} did not come up; last log lines:")
        print(tail_log(log_path))
        stop_daemon(proc)
        raise RuntimeError(f"preview daemon for PR {n} failed to start (see {log_path})")

    try:
        run_preview_tui(n, worktree, env)
    finally:
        if keep:
            print(f"preview #{n} kept: {descriptor_path(n)}")
            print(f"  daemon: {daemon_url}")
            print(f"  remove: rework preview --rm {n}")
        else:
            stop_daemon(proc)
            remove_preview(n)
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


def remove_preview(n: int) -> None:
    """Stop the preview's daemon and delete everything it owns."""
    desc = load_descriptor(n) or {}
    pid = desc.get("pid")
    if is_alive(pid):
        try:
            os.kill(int(pid), signal.SIGTERM)
        except OSError:
            pass
    repo_dir = desc.get("repo_dir")
    worktree = desc.get("worktree")
    branch = desc.get("branch") or f"rework/preview-pr-{n}"
    if repo_dir:
        if worktree:
            subprocess.run(
                ["git", "-C", str(repo_dir), "worktree", "remove", "--force", str(worktree)],
                capture_output=True,
            )
        subprocess.run(
            ["git", "-C", str(repo_dir), "branch", "-D", str(branch)], capture_output=True
        )
    shutil.rmtree(preview_dir(n), ignore_errors=True)
    descriptor_path(n).unlink(missing_ok=True)
