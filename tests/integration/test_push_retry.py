"""A transient push failure must not throw a finished mission away.

The live case (mission m-26591d66f460): GitHub answered one push with a
reasonless `! [remote rejected] <branch> -> <branch> (failed)`, the single
unclassified attempt raised, and a finished, audited mission went FAILED even
though its commit was still in the worktree. A manual retry minutes later
worked. These tests use a real bare remote whose `pre-receive` hook rejects the
first push only (a marker file), so the retry path is proven end to end.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

import pytest

from rework.kernel.events.bus import InProcessEventBus
from rework.kernel.state.records import Mission, MissionStatus
from rework.kernel.state.sqlite import SqliteStore, open_sqlite
from rework.nodes.gh import GHError, pr_create, pr_failure_is_transient
from rework.nodes.git_ops import PushError, push_branch, push_failure_is_transient
from rework.orchestrator.delivery import deliver_pr, retry_pr

FAST = (0.0, 0.0, 0.0)


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _reject_once_remote(tmp_path: Path) -> tuple[Path, Path, Path]:
    """origin.git whose pre-receive hook fails ONCE (marker file), plus a work
    repo with a `wip` branch pushed nowhere yet."""
    bare = tmp_path / "origin.git"
    bare.mkdir()
    _git("init", "--bare", "-b", "main", cwd=bare)
    work = tmp_path / "work"
    work.mkdir()
    _git("init", "-b", "main", cwd=work)
    (work / "a.txt").write_text("hello\n")
    _git("add", "-A", cwd=work)
    _git("-c", "user.email=r@r", "-c", "user.name=r", "commit", "-m", "init", cwd=work)
    _git("remote", "add", "origin", str(bare), cwd=work)
    _git("push", "-u", "origin", "main", cwd=work)
    _git("checkout", "-b", "rework/test", cwd=work)
    (work / "b.txt").write_text("work\n")
    _git("add", "-A", cwd=work)
    _git("-c", "user.email=r@r", "-c", "user.name=r", "commit", "-m", "work", cwd=work)

    marker = tmp_path / "hook-fired"
    hook = bare / "hooks" / "pre-receive"
    hook.write_text(
        f'#!/bin/sh\nif [ ! -f "{marker}" ]; then\n  touch "{marker}"\n  exit 1\nfi\nexit 0\n'
    )
    hook.chmod(0o755)
    return bare, work, marker


async def test_push_branch_retries_a_transient_rejection_and_succeeds(
    tmp_path: Path,
) -> None:
    bare, work, marker = _reject_once_remote(tmp_path)
    await push_branch(work, "rework/test", backoff_s=FAST)
    assert marker.exists(), "the hook must have rejected the first attempt"
    refs = _git("for-each-ref", "--format=%(refname:short)", cwd=bare).stdout
    assert "rework/test" in refs, "the retried push must land the branch on the remote"


async def test_push_branch_gives_up_after_the_attempt_cap(tmp_path: Path) -> None:
    bare, work, _ = _reject_once_remote(tmp_path)
    hook = bare / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n")  # always rejects
    with pytest.raises(PushError) as caught:
        await push_branch(work, "rework/test", attempts=2, backoff_s=FAST)
    assert caught.value.retryable
    assert "after 2 attempts" in str(caught.value)


@pytest.mark.parametrize(
    "output",
    [
        " ! [rejected]        wip -> wip (non-fast-forward)\n"
        "error: failed to push some refs\nhint: Updates were rejected\n"
        "hint: See the 'Note about fast-forwards'",
        "remote: error: GH006: Protected branch update failed for refs/heads/main.\n"
        " ! [remote rejected] main -> main (protected branch hook declined)",
        "remote: error: GH013: push declined due to repository rule violations",
        "remote: Permission to o/r.git denied to someone.\nfatal: Authentication failed",
    ],
)
def test_real_rejections_are_not_transient(output: str) -> None:
    assert push_failure_is_transient(output) is False


@pytest.mark.parametrize(
    "output",
    [
        " ! [remote rejected] wip -> wip (failed)",
        "error: RPC failed; HTTP 503 curl 22\nThe requested URL returned error: 503",
        "fatal: unable to access 'https://github.com/o/r.git/': Could not resolve host: github.com",
        "error: RPC failed; curl 56 OpenSSL SSL_read: connection reset by peer",
    ],
)
def test_transient_push_failures_are_classified(output: str) -> None:
    assert push_failure_is_transient(output) is True


async def test_git_timeout_keeps_the_real_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_run` used to swap git's output for the bare "(git timeout)"; the
    partial output is the only evidence of a push that hung mid-negotiation."""
    import rework.nodes.git_ops as git_ops

    class FakeProc:
        returncode = 1

        def __init__(self) -> None:
            self.calls = 0

        def terminate(self) -> None:
            pass

        async def communicate(self) -> tuple[bytes, bytes]:
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError
            return (b"remote: banner line\nfatal: early EOF\n", b"")

    async def fake_exec(*args: Any, **kwargs: Any) -> Any:
        return FakeProc()

    monkeypatch.setattr(git_ops.asyncio, "create_subprocess_exec", fake_exec)
    code, out = await git_ops._run(Path("."), "push", timeout_s=0.01)
    assert code == 124
    assert "fatal: early EOF" in out, "the real output must survive the timeout"
    assert "timeout" in out


async def _seed_delivery(store: SqliteStore, tmp_path: Path, branch: str) -> Mission:
    return await store.create_mission(
        Mission(
            id="mdeliver0001",
            objective="deliver the thing",
            title="Deliver",
            status=MissionStatus.DONE,
            plan={"kind": "fix-pr", "publish": "pr"},
            repo_path=str(tmp_path / "work"),
            result={"summary": "the meaningful summary"},
        )
    )


async def test_delivery_failure_keeps_the_mission_done_and_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bare, work, _ = _reject_once_remote(tmp_path)
    hook = bare / "hooks" / "pre-receive"
    # a rejection that repeats: the mission must survive it, not die of it
    hook.write_text(
        "#!/bin/sh\n"
        "echo 'remote: this remote is having a bad day (HTTP 503)' >&2\n"
        "echo ' ! [remote rejected] wip -> wip (failed)' >&2\n"
        "exit 1\n"
    )
    store = await open_sqlite(str(tmp_path / "d.sqlite3"))
    try:
        mission = await _seed_delivery(store, tmp_path, "rework/test")
        url = await deliver_pr(
            store,
            InProcessEventBus(store),
            mission.id,
            branch="rework/test",
            worktree=str(work),
            title="rework: Deliver",
            body="body",
        )
        assert url is None, "delivery must report failure by returning None, not raising"

        stored = await store.get_mission(mission.id)
        assert stored is not None
        assert stored.status == MissionStatus.DONE, "a finished mission must not go FAILED"
        assert stored.result["summary"] == "the meaningful summary", "the work/summary is kept"
        assert stored.result["pr_pending"] is True
        assert stored.result["pr_status"] == "failed"
        # the FULL error, including the LAST line (git puts the reason last)
        assert "HTTP 503" in stored.result["pr_error"]
        assert "[remote rejected]" in stored.result["pr_error"]
        assert stored.result["pr_error"].splitlines()[-1].strip().endswith("(failed)")

        # the retry surface: same helper, and it clears the marker on success
        hook.write_text("#!/bin/sh\nexit 0\n")
        ok, detail = await retry_pr(store, InProcessEventBus(store), mission.id)
        assert ok, detail
        after = await store.get_mission(mission.id)
        assert after is not None
        assert after.result["pr_pending"] is False
        assert after.result["pr_status"] == "opened"
        assert after.result["pr_url"] == detail
        assert "pr_error" not in after.result
    finally:
        await store.aclose()


async def test_retry_pr_reports_the_full_error_when_it_fails_again(
    tmp_path: Path,
) -> None:
    bare, work, _ = _reject_once_remote(tmp_path)
    (bare / "hooks" / "pre-receive").write_text(
        "#!/bin/sh\necho ' ! [remote rejected] wip -> wip (failed)' >&2\nexit 1\n"
    )
    store = await open_sqlite(str(tmp_path / "d2.sqlite3"))
    try:
        mission = await _seed_delivery(store, tmp_path, "rework/test")
        await deliver_pr(
            store,
            None,
            mission.id,
            branch="rework/test",
            worktree=str(work),
            title="t",
            body="b",
        )
        ok, detail = await retry_pr(store, None, mission.id)
        assert not ok
        assert "[remote rejected]" in detail and "(failed)" in detail
    finally:
        await store.aclose()


async def test_gh_pr_create_is_not_retried_for_an_existing_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = tmp_path / "calls.log"
    fake = tmp_path / "fake-gh"
    fake.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> {log}\n'
        "echo 'a pull request for branch \"x\" already exists' >&2\n"
        "exit 1\n"
    )
    fake.chmod(0o755)
    monkeypatch.setenv("REWORK_GH_BIN", str(fake))
    with pytest.raises(GHError) as caught:
        await pr_create(tmp_path, head="x", title="t", body="b", backoff_s=FAST)
    assert not caught.value.retryable
    assert len(log.read_text().strip().splitlines()) == 1, "must be a single attempt"


async def test_gh_pr_create_retries_a_5xx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = tmp_path / "calls.log"
    counter = tmp_path / "n"
    fake = tmp_path / "fake-gh"
    fake.write_text(
        "#!/bin/sh\n"
        f"echo call >> {log}\n"
        f"if [ -f {counter} ]; then echo https://github.com/o/r/pull/9; exit 0; fi\n"
        f"touch {counter}\n"
        "echo 'HTTP 502: Bad Gateway' >&2\n"
        "exit 1\n"
    )
    fake.chmod(0o755)
    monkeypatch.setenv("REWORK_GH_BIN", str(fake))
    url = await pr_create(tmp_path, head="x", title="t", body="b", backoff_s=FAST)
    assert url == "https://github.com/o/r/pull/9"
    assert len(log.read_text().strip().splitlines()) == 2


def test_pr_failure_classification_is_conservative() -> None:
    assert pr_failure_is_transient("HTTP 503 Service Unavailable") is True
    assert pr_failure_is_transient('a pull request for branch "x" already exists') is False
    assert pr_failure_is_transient("gh: Authentication failed") is False
