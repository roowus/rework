"""PR previews (ADR 0008): the pieces of `rework preview` that can be tested
without a network, uv, or a daemon. Git runs for real against local bare repos;
everything that would spawn a daemon or a TUI is monkeypatched away."""

from __future__ import annotations

import asyncio
import os
import socket
import sqlite3
import subprocess
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rework import preview as preview_mod
from rework.cli import app

runner = CliRunner()


def _git(*args: str, cwd: Path) -> str:
    out = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return out.stdout.strip()


@pytest.fixture
def pr_repo(tmp_path: Path) -> tuple[Path, str]:
    """A bare 'origin' B and a work repo W whose PR #7 head is W's HEAD."""
    bare = tmp_path / "origin.git"
    bare.mkdir()
    subprocess.run(["git", "init", "--bare"], cwd=bare, check=True, capture_output=True)
    work = tmp_path / "work"
    work.mkdir()
    _git("init", "-b", "main", cwd=work)
    _git("config", "user.email", "r@r", cwd=work)
    _git("config", "user.name", "r", cwd=work)
    (work / "pyproject.toml").write_text('[project]\nname = "rework"\n')
    (work / "README.md").write_text("# rework\n")
    _git("add", "-A", cwd=work)
    _git("commit", "-m", "init", cwd=work)
    _git("remote", "add", "origin", str(bare), cwd=work)
    _git("push", "origin", "main", cwd=work)
    sha = _git("rev-parse", "HEAD", cwd=work)
    _git("update-ref", "refs/pull/7/head", sha, cwd=bare)
    return work, sha


@pytest.fixture(autouse=True)
def preview_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "previews"
    monkeypatch.setenv("REWORK_PREVIEW_DIR", str(root))
    monkeypatch.delenv("REWORK_PREVIEW", raising=False)
    monkeypatch.delenv("REWORK_PREVIEW_ALLOW_MISSIONS", raising=False)
    return root


def test_fetch_worktree_checks_out_the_pr_head(pr_repo: tuple[Path, str]) -> None:
    work, sha = pr_repo
    worktree = preview_mod.fetch_worktree(7, work)
    assert worktree == preview_mod.preview_dir(7) / "repo"
    assert worktree.is_dir()
    assert _git("rev-parse", "HEAD", cwd=worktree) == sha
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=worktree) == "rework/preview-pr-7"


def test_copy_state_db_preserves_rows_and_drops_the_lock(tmp_path: Path) -> None:
    src = tmp_path / "state.sqlite3"
    dst = tmp_path / "copy" / "state.sqlite3"
    conn = sqlite3.connect(src)
    conn.execute("CREATE TABLE missions (id TEXT)")
    conn.execute("INSERT INTO missions VALUES ('m-1')")
    conn.execute("CREATE TABLE daemon_lock (owner TEXT)")
    conn.execute("INSERT INTO daemon_lock VALUES ('daemon-1')")
    conn.commit()
    conn.close()

    preview_mod.copy_state_db(src, dst)

    copy = sqlite3.connect(dst)
    try:
        assert copy.execute("SELECT id FROM missions").fetchall() == [("m-1",)]
        assert copy.execute("SELECT count(*) FROM daemon_lock").fetchone()[0] == 0
    finally:
        copy.close()


def test_copy_state_db_without_a_lock_table(tmp_path: Path) -> None:
    src = tmp_path / "state.sqlite3"
    dst = tmp_path / "copy" / "state.sqlite3"
    conn = sqlite3.connect(src)
    conn.execute("CREATE TABLE missions (id TEXT)")
    conn.commit()
    conn.close()
    preview_mod.copy_state_db(src, dst)  # no daemon_lock: not an error
    assert dst.exists()


def test_free_port_can_be_bound() -> None:
    port = preview_mod.free_port()
    assert isinstance(port, int) and port > 0
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", port))


def test_prepare_state_on_postgres_gives_a_fresh_sqlite_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REWORK_DB", "postgresql://postgres@host:5432/rework_state")
    assert preview_mod.is_postgres(os.environ["REWORK_DB"])
    state_db, notice = preview_mod.prepare_state(7, preview_mod.source_state_db())
    assert state_db == preview_mod.preview_dir(7) / "state.sqlite3"
    assert notice is not None
    assert "SQLite" in notice and "Postgres" in notice


def test_prepare_state_copies_a_sqlite_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / "state.sqlite3"
    conn = sqlite3.connect(src)
    conn.execute("CREATE TABLE missions (id TEXT)")
    conn.execute("INSERT INTO missions VALUES ('m-2')")
    conn.commit()
    conn.close()
    monkeypatch.setenv("REWORK_DB", str(src))
    state_db, notice = preview_mod.prepare_state(7, preview_mod.source_state_db())
    assert notice is None
    copy = sqlite3.connect(state_db)
    try:
        assert copy.execute("SELECT id FROM missions").fetchall() == [("m-2",)]
    finally:
        copy.close()


def test_list_and_remove_preview(pr_repo: tuple[Path, str]) -> None:
    work, sha = pr_repo
    worktree = preview_mod.fetch_worktree(7, work)
    preview_mod.write_descriptor(
        7,
        {
            "pr": 7,
            "head_sha": sha,
            "branch": "rework/preview-pr-7",
            "worktree": str(worktree),
            "state_db": str(preview_mod.preview_dir(7) / "state.sqlite3"),
            "host": "127.0.0.1",
            "port": 9999,
            "pid": None,
            "daemon_url": "http://127.0.0.1:9999",
            "created_at": "2026-11-09T00:00:00+00:00",
            "repo_dir": str(work),
        },
    )

    rows = preview_mod.list_previews()
    assert len(rows) == 1
    assert rows[0]["pr"] == 7 and rows[0]["port"] == 9999
    assert rows[0]["alive"] is False

    preview_mod.remove_preview(7)
    assert not preview_mod.preview_dir(7).exists()
    assert not preview_mod.descriptor_path(7).exists()
    assert str(worktree) not in _git("worktree", "list", cwd=work)


def test_daemon_dispatch_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    from rework.orchestrator import daemon as daemon_mod

    calls: list[str] = []

    def _record(coro):
        coro.close()
        calls.append("task")

    monkeypatch.setattr(daemon_mod.asyncio, "create_task", _record)
    monkeypatch.setattr(daemon_mod, "MissionRunner", lambda *a, **k: calls.append("runner"))

    d = object.__new__(daemon_mod.Daemon)
    d._runners = {}

    monkeypatch.setenv("REWORK_PREVIEW", "7")
    monkeypatch.delenv("REWORK_PREVIEW_ALLOW_MISSIONS", raising=False)
    assert daemon_mod.preview_dispatch_allowed() is False
    asyncio.run(d._spawn_runner("m-1"))
    assert calls == []  # the guard returns before any runner exists
    assert d._runners == {}

    monkeypatch.setenv("REWORK_PREVIEW_ALLOW_MISSIONS", "1")
    assert daemon_mod.preview_dispatch_allowed() is True
    asyncio.run(d._spawn_runner("m-1"))
    assert "task" in calls

    monkeypatch.delenv("REWORK_PREVIEW")
    monkeypatch.delenv("REWORK_PREVIEW_ALLOW_MISSIONS", raising=False)
    assert daemon_mod.preview_dispatch_allowed() is True


def test_cli_preview_list_on_an_empty_root() -> None:
    result = runner.invoke(app, ["preview", "--list"])
    assert result.exit_code == 0, result.output
    assert "no previews" in result.output


def test_tui_command_is_registered() -> None:
    result = runner.invoke(app, ["tui", "--help"])
    assert result.exit_code == 0, result.output
    assert "No such command" not in result.output


def test_fetch_worktree_is_re_runnable(pr_repo: tuple[Path, str]) -> None:
    work, sha = pr_repo
    first = preview_mod.fetch_worktree(7, work)
    second = preview_mod.fetch_worktree(7, work)  # must not fail on the registration
    assert first == second
    assert _git("rev-parse", "HEAD", cwd=second) == sha

    # a new commit on the PR head is picked up by the second call
    (work / "README.md").write_text("# rework\n\nmore\n")
    _git("add", "-A", cwd=work)
    _git("commit", "-m", "more", cwd=work)
    new_sha = _git("rev-parse", "HEAD", cwd=work)
    _git("push", "origin", "main", cwd=work)
    _git("update-ref", "refs/pull/7/head", new_sha, cwd=work.parent / "origin.git")
    preview_mod.fetch_worktree(7, work)
    assert _git("rev-parse", "HEAD", cwd=second) == new_sha


def test_remove_preview_without_a_descriptor(pr_repo: tuple[Path, str]) -> None:
    work, _ = pr_repo
    worktree = preview_mod.fetch_worktree(7, work)
    preview_mod.descriptor_path(7).unlink(missing_ok=True)

    preview_mod.remove_preview(7, work)

    assert not preview_mod.preview_dir(7).exists()
    assert str(worktree) not in _git("worktree", "list", cwd=work)


def test_remove_preview_leaves_an_unrelated_process_alone(pr_repo: tuple[Path, str]) -> None:
    work, _ = pr_repo
    worktree = preview_mod.fetch_worktree(7, work)
    victim = subprocess.Popen(["sleep", "30"])
    preview_mod.write_descriptor(
        7,
        {
            "pr": 7,
            "branch": "rework/preview-pr-7",
            "worktree": str(worktree),
            "pid": victim.pid,
            "port": 9998,
            "daemon_url": "http://127.0.0.1:9998",
            "repo_dir": str(work),
        },
    )
    try:
        preview_mod.remove_preview(7)
        assert victim.poll() is None  # not ours: the ps identity check refuses it
    finally:
        victim.kill()
        victim.wait()


def test_create_preview_tears_down_a_half_built_preview(
    pr_repo: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    work, _ = pr_repo
    real_run = subprocess.run

    def fake_run(cmd, **kwargs):  # let git through, stub uv
        if cmd[:2] == ["uv", "sync"]:
            return subprocess.CompletedProcess(cmd, 0)
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(
        preview_mod,
        "subprocess",
        types.SimpleNamespace(run=fake_run, TimeoutExpired=subprocess.TimeoutExpired),
    )

    def boom(*args, **kwargs):
        raise RuntimeError("state prep failed")

    monkeypatch.setattr(preview_mod, "prepare_state", boom)
    with pytest.raises(RuntimeError, match="state prep failed"):
        preview_mod.create_preview(7, work)

    assert not preview_mod.preview_dir(7).exists()
    assert str(preview_mod.preview_dir(7) / "repo") not in _git("worktree", "list", cwd=work)


class _FakeProc:
    def poll(self) -> int | None:
        return None


def _http_server(status: int, body: bytes) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def test_wait_ready_rejects_a_stranger_service() -> None:
    httpd = _http_server(404, b"nope")
    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        assert preview_mod.wait_ready(url, _FakeProc(), timeout_s=1.0) is False
    finally:
        httpd.shutdown()


def test_wait_ready_accepts_the_daemon_healthz() -> None:
    httpd = _http_server(200, b'{"ok": true}')
    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        assert preview_mod.wait_ready(url, _FakeProc(), timeout_s=5.0) is True
    finally:
        httpd.shutdown()


def test_prepare_state_with_a_missing_source_db_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("REWORK_DB", str(tmp_path / "nope.sqlite3"))
    state_db, notice = preview_mod.prepare_state(7, preview_mod.source_state_db())
    out = capsys.readouterr().out
    assert notice is not None and "fresh" in notice
    assert "fresh" in out
    assert state_db.exists()
    conn = sqlite3.connect(state_db)
    try:
        assert conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0
    finally:
        conn.close()


class _FakeDaemon:
    pid = 0

    def poll(self) -> int | None:
        return None


@pytest.fixture
def fake_build(pr_repo: tuple[Path, str], monkeypatch: pytest.MonkeyPatch) -> Path:
    """A create_preview that really fetches, but fakes uv/the daemon/the TUI."""
    work, _ = pr_repo
    real_run = subprocess.run

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["uv", "sync"]:
            return subprocess.CompletedProcess(cmd, 0)
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(
        preview_mod,
        "subprocess",
        types.SimpleNamespace(run=fake_run, TimeoutExpired=subprocess.TimeoutExpired),
    )
    monkeypatch.setattr(preview_mod, "spawn_daemon", lambda *a, **k: _FakeDaemon())
    monkeypatch.setattr(preview_mod, "wait_ready", lambda *a, **k: True)
    monkeypatch.setattr(preview_mod, "stop_daemon", lambda proc: None)
    return work


def test_create_preview_reports_a_failed_tui_and_cleans_up(
    fake_build: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch_run = preview_mod.run_preview_tui
    preview_mod.run_preview_tui = lambda n, worktree, env: 3
    try:
        assert preview_mod.create_preview(7, fake_build) == 3
    finally:
        preview_mod.run_preview_tui = monkeypatch_run
    out = capsys.readouterr().out
    assert "daemon.log" in out and "status 3" in out
    assert not preview_mod.preview_dir(7).exists()


def test_create_preview_keeps_a_failed_tui_when_asked(
    fake_build: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch_run = preview_mod.run_preview_tui
    preview_mod.run_preview_tui = lambda n, worktree, env: 3
    try:
        assert preview_mod.create_preview(7, fake_build, keep=True) == 3
    finally:
        preview_mod.run_preview_tui = monkeypatch_run
    out = capsys.readouterr().out
    assert "daemon.log" in out
    assert "REWORK_PREVIEW=7 rework tui" in out  # how to attach again
    assert preview_mod.descriptor_path(7).exists()
    preview_mod.remove_preview(7, fake_build)
