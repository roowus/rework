"""PR previews (ADR 0008): the pieces of `rework preview` that can be tested
without a network, uv, or a daemon. Git runs for real against local bare repos;
everything that would spawn a daemon or a TUI is monkeypatched away."""

from __future__ import annotations

import asyncio
import os
import socket
import sqlite3
import subprocess
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
