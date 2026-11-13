"""`rework update`: fetch origin/main, fast-forward ONLY, `uv sync`.

Git runs for real against a bare `origin` and a clone of it in tmp (the
convention tests/test_preview.py uses): the refusals are the whole point of the
feature, so they are proved against real dirty / off-main / diverged
checkouts rather than mocks. `sync=False` skips `uv sync`; nothing here needs a
network or a real uv.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from rework import update


def _git(*args: str, cwd: Path) -> str:
    # -c user.* on every call: never depend on the machine's global git config
    out = subprocess.run(
        ["git", "-c", "user.email=r@r", "-c", "user.name=r", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


def _commit(cwd: Path, subject: str) -> str:
    (cwd / f"{subject.replace(' ', '-')}.txt").write_text(subject)
    _git("add", "-A", cwd=cwd)
    _git("commit", "-m", subject, cwd=cwd)
    return _git("rev-parse", "HEAD", cwd=cwd)


@dataclass(frozen=True)
class Repos:
    bare: Path  # origin, bare
    work: Path  # where new origin commits are made and pushed from
    clone: Path  # the deployed checkout under test

    def commit(self, subject: str) -> str:
        """A new commit on origin/main, pushed; returns its sha."""
        sha = _commit(self.work, subject)
        _git("push", "origin", "main", cwd=self.work)
        return sha

    def origin_head(self) -> str:
        return _git("rev-parse", "main", cwd=self.bare)

    def head(self) -> str:
        return _git("rev-parse", "HEAD", cwd=self.clone)

    def status(self) -> str:
        return _git("status", "--porcelain", cwd=self.clone)


@pytest.fixture()
def repos(tmp_path: Path) -> Repos:
    """A bare origin and a clone of it on main, one commit each."""
    bare = tmp_path / "origin.git"
    bare.mkdir()
    _git("init", "--bare", "-b", "main", cwd=bare)
    work = tmp_path / "work"
    work.mkdir()
    _git("init", "-b", "main", cwd=work)
    (work / "README.md").write_text("# rework\n")
    _git("add", "-A", cwd=work)
    _git("commit", "-m", "init", cwd=work)
    _git("remote", "add", "origin", str(bare), cwd=work)
    _git("push", "origin", "main", cwd=work)
    clone = tmp_path / "clone"
    _git("clone", "-b", "main", str(bare), str(clone), cwd=tmp_path)
    return Repos(bare=bare, work=work, clone=clone)


def test_run_update_fast_forwards_the_clone_to_origin(repos: Repos) -> None:
    old = repos.head()
    repos.commit("second commit")
    third = repos.commit("third commit")

    result = update.run_update(repos.clone, sync=False)

    assert result.ok, result.reason
    assert result.changed
    assert result.old == old
    assert result.new == third == repos.origin_head()
    # git log prints newest-first and run_update keeps git's order
    assert result.subjects == ["third commit", "second commit"]
    assert repos.head() == third
    assert repos.status() == ""


def test_run_update_says_already_up_to_date_without_moving(repos: Repos) -> None:
    old = repos.head()

    result = update.run_update(repos.clone, sync=False)

    assert result.ok, result.reason
    assert result.changed is False
    assert result.old == result.new == old
    assert repos.head() == old
    assert update.describe_update(result).startswith("already up to date")


def test_run_update_refuses_a_dirty_working_tree(repos: Repos) -> None:
    old = repos.head()
    new_origin = repos.commit("lands while the tree is dirty")
    (repos.clone / "scratch.txt").write_text("not committed\n")

    result = update.run_update(repos.clone, sync=False)

    assert result.ok is False
    assert "uncommitted" in (result.reason or "")
    assert new_origin != old  # origin really had something to apply
    assert repos.head() == old  # HEAD untouched
    assert (repos.clone / "scratch.txt").read_text() == "not committed\n"
    assert "scratch.txt" in repos.status()


def test_run_update_refuses_off_main(repos: Repos) -> None:
    repos.commit("on origin")
    _git("checkout", "-b", "feature", cwd=repos.clone)
    old = repos.head()

    result = update.run_update(repos.clone, sync=False)

    assert result.ok is False
    assert "not on main" in (result.reason or "")
    assert "feature" in (result.reason or "")
    assert repos.head() == old
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=repos.clone) == "feature"
    assert repos.status() == ""


def test_run_update_refuses_a_diverged_history(repos: Repos) -> None:
    local = _commit(repos.clone, "local only")
    theirs = repos.commit("origin only")

    result = update.run_update(repos.clone, sync=False)

    assert result.ok is False
    # the DIVERGENCE branch, not git's own merge failure: "not a fast-forward"
    assert "is not a fast-forward" in (result.reason or "")
    assert theirs != local
    assert repos.head() == local  # the local commit is still there, nothing merged
    assert repos.status() == ""
    # the fetch that ran first did update the remote-tracking ref
    assert _git("rev-parse", "origin/main", cwd=repos.clone) == theirs


def test_check_update_reports_how_far_ahead_and_moves_nothing(repos: Repos) -> None:
    before = repos.head()
    for subject in ("one", "two", "three"):
        repos.commit(subject)
    origin = repos.origin_head()

    check = update.check_update(repos.clone)

    assert check.error is None
    assert check.ahead == 3
    assert check.available is True
    assert check.local == before
    assert check.remote == origin
    # a report, not an update: HEAD and the working tree are untouched
    assert repos.head() == before
    assert repos.status() == ""
    # the fetch DID update the remote-tracking ref: that is what --check reads
    assert _git("rev-parse", "origin/main", cwd=repos.clone) == origin


def test_check_update_is_not_available_when_up_to_date(repos: Repos) -> None:
    check = update.check_update(repos.clone)

    assert check.error is None
    assert check.ahead == 0
    assert check.available is False
    assert check.local == check.remote


def test_should_auto_update_needs_an_update_and_an_idle_daemon() -> None:
    assert update.should_auto_update(ahead=True, busy=False) is True
    assert update.should_auto_update(ahead=True, busy=True) is False
    assert update.should_auto_update(ahead=False, busy=False) is False


# --- a hung git / uv is an OUTCOME, not an exception -----------------------
# The daemon runs check_remote from a background timer (asyncio.to_thread), so
# a `git fetch` that hits its timeout must come back as the module's usual
# error/refusal shape: a TimeoutExpired escaping the thread would kill the
# whole daemon process, which is exactly what "offline is silence" forbids.
# `subprocess.run` is patched to RAISE, the way a real timeout does — not the
# module's own helpers, so these tests fail against the pre-fix code.

_REAL_RUN = subprocess.run


def _raises_on(monkeypatch: pytest.MonkeyPatch, *, needle: str) -> None:
    """Make the real `subprocess.run` raise TimeoutExpired when `needle` is in
    the argv (the hung command); every other call runs for real."""

    def run(argv: list[str], *args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        if needle in argv:
            raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout") or 120.0)
        return _REAL_RUN(argv, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(subprocess, "run", run)


def test_a_timeout_from_subprocess_run_becomes_a_completed_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _raises_on(monkeypatch, needle="fetch")

    proc = update._git(Path("/tmp"), "fetch", "origin", "main")  # must not raise

    assert proc.returncode == update._TIMEOUT_RC
    assert "timed out" in proc.stderr
    assert proc.stdout == ""


def test_check_update_reports_a_timed_out_fetch_instead_of_raising(
    repos: Repos, monkeypatch: pytest.MonkeyPatch
) -> None:
    _raises_on(monkeypatch, needle="fetch")

    check = update.check_update(repos.clone)  # must not raise

    assert check.error is not None
    assert "timed out" in check.error
    assert check.available is False
    # the background-timer flavour: a stall is "unknown", i.e. silence
    assert update.check_remote(repos.clone) is None


def test_run_update_refuses_after_a_timed_out_fetch(
    repos: Repos, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = repos.head()
    repos.commit("never fetched")
    _raises_on(monkeypatch, needle="fetch")

    result = update.run_update(repos.clone, sync=False)  # must not raise

    assert result.ok is False
    assert "timed out" in (result.reason or "")
    assert repos.head() == old  # nothing moved
    assert update.describe_update(result).startswith("update refused")


def test_run_update_reports_a_uv_sync_timeout_after_the_fast_forward(
    repos: Repos, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = repos.head()
    new = repos.commit("landed, but the env did not sync")
    _raises_on(monkeypatch, needle="uv")

    result = update.run_update(repos.clone)  # sync=True

    assert result.ok is False
    assert "uv sync timed out" in (result.reason or "")
    # the tree DID move: the owner is still told where it landed
    assert result.old == old
    assert result.new == new == repos.head()


# --- `rework update --target NAME`: the CLI path ---------------------------
# main() rejects a --target AFTER the subcommand (it is not a global option),
# but `update` owns a --target of its own. Only a real `python -m rework` run
# exercises main(), so these are subprocess tests (the tests/test_cli.py
# convention). `ssh` is a stub on PATH: nothing here touches a network.

MAIN_TARGETS_TOML = """\
default = "sandbox"

[targets.sandbox]
db = "sqlite:////tmp/probe/sandbox.db"
daemon_url = "http://127.0.0.1:8941"

[targets.rewvis]
db = "sqlite:////tmp/probe/sandbox.db"
daemon_url = "http://127.0.0.1:8941"
ssh = "ubuntu@100.107.125.118"

[targets.localonly]
db = "sqlite:////tmp/probe/sandbox.db"
daemon_url = "http://127.0.0.1:8941"
"""


@pytest.fixture()
def cli_env(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """(ssh log, env) for a real main() run with a stub `ssh` on PATH."""
    home = tmp_path / "home"
    (home / ".rework").mkdir(parents=True)
    (home / "targets.toml").write_text(MAIN_TARGETS_TOML)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "ssh"
    stub.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$@" >> "$REWORK_TEST_SSH_LOG"\n'
        'echo "remote said ok"\n'
        'exit "${REWORK_TEST_SSH_RC:-0}"\n'
    )
    stub.chmod(0o755)
    log = tmp_path / "ssh.log"
    env = {
        **os.environ,
        "HOME": str(home),
        "REWORK_TARGETS": str(home / "targets.toml"),
        "REWORK_LOG_DIR": str(home / "logs"),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "REWORK_TEST_SSH_LOG": str(log),
    }
    return log, env


def _run_cli(argv: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(Path(sys.executable).parent / "rework"), *argv],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        check=False,
    )


def test_update_target_reaches_the_update_command(cli_env: tuple[Path, dict[str, str]]) -> None:
    _log, env = cli_env
    result = _run_cli(["update", "--target", "ghost"], env)

    # the update command's own error, NOT main()'s global --target usage error
    assert result.returncode == 1, result.stderr
    assert "no target named 'ghost'" in result.stderr
    assert "must come before the subcommand" not in result.stderr


def test_update_target_needs_an_ssh_host(cli_env: tuple[Path, dict[str, str]]) -> None:
    _log, env = cli_env
    result = _run_cli(["update", "--target", "localonly"], env)

    assert result.returncode == 1
    assert "has no `ssh` host" in result.stderr


def test_update_target_runs_the_remote_update_over_ssh(
    cli_env: tuple[Path, dict[str, str]],
) -> None:
    log, env = cli_env
    result = _run_cli(["update", "--target", "rewvis"], env)

    assert result.returncode == 0, result.stderr
    host, command = log.read_text().splitlines()
    assert host == "ubuntu@100.107.125.118"
    assert "uv run rework update" in command
    assert "sudo systemctl restart rework" in command
    assert "remote said ok" in result.stdout


def test_update_target_check_does_not_restart(cli_env: tuple[Path, dict[str, str]]) -> None:
    log, env = cli_env
    result = _run_cli(["update", "--target", "rewvis", "--check"], env)

    assert result.returncode == 0, result.stderr
    _host, command = log.read_text().splitlines()
    assert command.endswith("uv run rework update --check")
    assert "systemctl" not in command


def test_update_target_propagates_the_remote_exit_code(
    cli_env: tuple[Path, dict[str, str]],
) -> None:
    _log, env = cli_env
    result = _run_cli(["update", "--target", "rewvis"], {**env, "REWORK_TEST_SSH_RC": "3"})

    assert result.returncode == 3


def test_a_global_target_after_a_subcommand_is_still_rejected(
    cli_env: tuple[Path, dict[str, str]],
) -> None:
    # the guard the update exemption must not weaken: `rework ls --target NAME`
    # is still a usage error (exit 2), not a silent wrong-target run
    _log, env = cli_env
    result = _run_cli(["ls", "--target", "sandbox"], env)

    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "must come before the subcommand" in result.stderr


def test_update_target_survives_a_missing_ssh_binary(
    cli_env: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    # no `ssh` anywhere on PATH: the deploy cannot start, but that is a clean
    # red error + exit 1, not a FileNotFoundError traceback out of the CLI
    _log, env = cli_env
    empty = tmp_path / "nobin"
    empty.mkdir()
    result = _run_cli(["update", "--target", "rewvis"], {**env, "PATH": str(empty)})

    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "could not run ssh" in result.stderr
    assert "Traceback" not in result.stderr


# --- mission_busy: the daemon's gate on auto-update -------------------------
# The store is the source of truth (CLAUDE.md rule 3), so this asks a real
# sqlite store: an update must never land under an in-flight mission.


async def test_mission_busy_follows_the_active_statuses(tmp_path: Path) -> None:
    from rework.kernel.state.records import Mission, MissionStatus
    from rework.kernel.state.sqlite import open_sqlite

    store = await open_sqlite(str(tmp_path / "state.sqlite3"))
    try:
        assert await update.mission_busy(store) is False  # idle

        for status in MissionStatus:
            mission = await store.create_mission(Mission(id=f"m-{status.value}", objective="o"))
            await store.update_mission(mission.id, status=status)
            busy = await update.mission_busy(store)
            assert busy is (status.value in update.ACTIVE_STATUSES), status
            await store.update_mission(mission.id, status=MissionStatus.DONE)

        assert await update.mission_busy(store) is False  # all finished again
    finally:
        await store.aclose()
