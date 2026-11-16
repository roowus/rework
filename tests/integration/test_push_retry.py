"""A transient push failure must not throw a finished mission away.

The live case (mission m-26591d66f460): GitHub answered one push with a
reasonless `! [remote rejected] <branch> -> <branch> (failed)`, the single
unclassified attempt raised, and a finished, audited mission went FAILED even
though its commit was still in the worktree. A manual retry minutes later
worked.

A local bare repo CANNOT reproduce that: a `pre-receive` hook that exits 1 makes
git print `(pre-receive hook declined)`, which is a real rejection (and the
classifier says so on purpose). So the transient case is faked where GitHub
actually is: a `git` shim first on PATH that intercepts only `push`, prints
GitHub's exact failure text and exits 1 the first time, then execs the real git.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from conftest import (
    AUDIT_CLEAN,
    ScriptedProvider,
    _auditor_script,
    _coder_script,
    _investigator_script,
    role_catalog,
)
from rework.kernel.events.bus import InProcessEventBus
from rework.kernel.state.records import Mission, MissionStatus
from rework.kernel.state.sqlite import open_sqlite
from rework.missions import PLAN_FIX_PR
from rework.nodes.gh import GHError, pr_create, pr_failure_is_transient
from rework.nodes.git_ops import PushError, push_branch, push_failure_is_transient
from rework.orchestrator.delivery import deliver_pr, retry_pr
from rework.orchestrator.engine import MissionRunner
from rework.roles.registry import autodiscover

FAST = (0.0, 0.0, 0.0)

REAL_GIT = shutil.which("git") or "/usr/bin/git"

# GitHub's actual transient answer: a `remote:` banner, the forge's
# "Create a pull request" hint (which reads like success), and the reasonless
# `(failed)` line LAST. The classifier must call this transient.
GITHUB_TRANSIENT_FAILURE = (
    "remote: \n"
    "remote: Create a pull request for 'rework/test' on GitHub by visiting:        \n"
    "remote:      https://github.com/roowus/toyrepo/pull/new/rework/test        \n"
    "remote: \n"
    "To https://github.com/roowus/toyrepo.git\n"
    " ! [remote rejected] rework/test -> rework/test (failed)\n"
    "error: failed to push some refs to 'https://github.com/roowus/toyrepo.git'\n"
)

# A real rejection: the remote decided, retrying changes nothing.
NON_FAST_FORWARD_FAILURE = (
    "To /tmp/origin.git\n"
    " ! [rejected]        rework/test -> rework/test (non-fast-forward)\n"
    "error: failed to push some refs to '/tmp/origin.git'\n"
    "hint: Updates were rejected because the tip of your current branch is behind\n"
)


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _make_repo(tmp_path: Path) -> tuple[Path, Path]:
    """origin.git (bare) + work repo with a `rework/test` branch to push."""
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
    return bare, work


def _global_flags(args: list[str]) -> list[str]:
    """Leading `-c k=v` / `-C path` options, which precede the subcommand."""
    i = 0
    while i < len(args):
        if args[i] in ("-c", "-C") and i + 1 < len(args):
            i += 2
        elif args[i].startswith("-"):
            i += 1
        else:
            break
    return args[:i]


def _fail_push_shim(tmp_path: Path, failure: str, *, times: int = 1) -> tuple[Path, Path]:
    """Put a `git` on PATH that fails the first `times` pushes with GitHub's
    exact output, then execs the real git. Returns (bindir, counter)."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    counter = tmp_path / "push-attempts"
    message = tmp_path / "git-message.txt"
    message.write_text(failure)
    shim = bindir / "git"
    shim.write_text(
        "#!/usr/bin/env python3\n"
        "import os, subprocess, sys\n"
        f"COUNTER = {str(counter)!r}\n"
        f"MESSAGE = {str(message)!r}\n"
        f"REAL = {REAL_GIT!r}\n"
        f"TIMES = {times}\n"
        "args = sys.argv[1:]\n"
        "i = 0\n"
        "while i < len(args):\n"
        "    if args[i] in ('-c', '-C') and i + 1 < len(args):\n"
        "        i += 2\n"
        "    elif args[i].startswith('-'):\n"
        "        i += 1\n"
        "    else:\n"
        "        break\n"
        "rest = args[i:]\n"
        "if rest[:1] != ['push']:\n"
        "    os.execv(REAL, [REAL, *args])\n"
        "seen = 0\n"
        "if os.path.exists(COUNTER):\n"
        "    seen = int(open(COUNTER).read().strip() or 0)\n"
        "seen += 1\n"
        "open(COUNTER, 'w').write(str(seen))\n"
        "if seen <= TIMES:\n"
        "    sys.stderr.write(open(MESSAGE).read())\n"
        "    sys.exit(1)\n"
        "os.execv(REAL, [REAL, *args])\n"
    )
    shim.chmod(0o755)
    return bindir, counter


def _push_attempts(counter: Path) -> int:
    return int(counter.read_text().strip() or 0) if counter.exists() else 0


def _remote_refs(bare: Path) -> list[str]:
    return _git("for-each-ref", "--format=%(refname:short)", cwd=bare).stdout.split()


# ---------------------------------------------------------------------------
# (a) a remote that rejects once, then accepts: the push is retried and lands
# ---------------------------------------------------------------------------


def _toyrepo_with_remote(tmp_path: Path) -> tuple[Path, Path]:
    """A committed copy of examples/toyrepo whose `origin` is a bare repo."""
    source = Path(__file__).parents[2] / "examples" / "toyrepo"
    dest = tmp_path / "mission" / "toyrepo"
    dest.mkdir(parents=True)
    for f in source.iterdir():
        if f.is_file():
            shutil.copy(f, dest / f.name)
    bare = tmp_path / "origin.git"
    bare.mkdir()
    _git("init", "--bare", "-b", "main", cwd=bare)
    _git("init", "-b", "main", cwd=dest)
    _git("add", "-A", cwd=dest)
    _git("-c", "user.email=r@r", "-c", "user.name=r", "commit", "-m", "init", cwd=dest)
    _git("remote", "add", "origin", str(bare), cwd=dest)
    _git("push", "-u", "origin", "main", cwd=dest)
    return dest, bare


async def test_push_branch_retries_a_transient_rejection_and_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bare, work = _make_repo(tmp_path)
    bindir, counter = _fail_push_shim(tmp_path, GITHUB_TRANSIENT_FAILURE, times=1)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    await push_branch(work, "rework/test", backoff_s=FAST)

    assert _push_attempts(counter) == 2, "the transient rejection must be retried exactly once"
    assert "rework/test" in _remote_refs(bare), "the retried push must land the branch"


async def test_mission_delivery_retries_a_reject_once_remote_and_records_the_pr_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point, end to end: a fix-pr mission whose push is rejected ONCE
    still finishes DONE with a PR URL, and the retry is what got it there."""
    import json

    dest, bare = _toyrepo_with_remote(tmp_path)

    fake_gh = tmp_path / "fake-gh"
    fake_gh.write_text("#!/bin/sh\necho https://github.com/roowus/toyrepo/pull/11\n")
    fake_gh.chmod(0o755)
    monkeypatch.setenv("REWORK_GH_BIN", str(fake_gh))
    bindir, counter = _fail_push_shim(tmp_path, GITHUB_TRANSIENT_FAILURE, times=1)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    from rework.orchestrator.daemon import sqlite_checkpointer_factory

    store = await open_sqlite(str(tmp_path / "pr.sqlite3"))
    try:
        objective = "write docs: add a line to README saying hello from rework"
        plan = json.loads(json.dumps(PLAN_FIX_PR).replace("$OBJECTIVE", objective))
        mission = await store.create_mission(
            Mission(id="mretryonce", objective=objective, repo_path=str(dest), plan=plan)
        )
        provider = ScriptedProvider()
        provider.route("Investigate any brief", _investigator_script())
        provider.route("Fulfill the task", _coder_script("docs line added, tests still green"))
        provider.route("Find issues", _auditor_script(AUDIT_CLEAN))
        runner = MissionRunner(
            mission.id,
            store,
            InProcessEventBus(store),
            autodiscover(),
            role_catalog(),
            sqlite_checkpointer_factory(str(tmp_path / "ck.sqlite3")),
            tmp_path / "work",
            poll_interval_s=0.05,
            provider=provider,
        )
        await asyncio.wait_for(asyncio.create_task(runner.run()), timeout=60)

        done = await store.get_mission(mission.id)
        assert done is not None
        assert done.status == MissionStatus.DONE, "one rejected push must not fail the mission"
        assert done.result["pr_url"] == "https://github.com/roowus/toyrepo/pull/11"
        assert not done.result.get("pr_pending")
        assert _push_attempts(counter) == 2, "the first push was rejected, the retry landed it"
        branches = [
            str((r.artifact or {}).get("branch"))
            for r in await store.list_runs(mission.id)
            if r.role == "coder" and (r.artifact or {}).get("branch")
        ]
        assert branches, "the coder recorded a branch"
        refs = _remote_refs(bare)
        assert any(branch in refs for branch in branches), (
            f"the branch really reached the remote (refs={refs})"
        )
    finally:
        await store.aclose()


async def test_push_branch_gives_up_after_the_attempt_cap_for_a_persistent_hiccup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bare, work = _make_repo(tmp_path)
    bindir, counter = _fail_push_shim(tmp_path, GITHUB_TRANSIENT_FAILURE, times=99)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    with pytest.raises(PushError) as caught:
        await push_branch(work, "rework/test", attempts=3, backoff_s=FAST)

    assert caught.value.retryable
    assert caught.value.attempts == 3
    assert _push_attempts(counter) == 3, "exactly the cap, no more"
    assert "[remote rejected]" in str(caught.value), "the remote's own words are kept"
    assert "rework/test" not in _remote_refs(bare)


# ---------------------------------------------------------------------------
# (b) real rejections are reported on the FIRST answer, not retried
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "failure",
    [
        NON_FAST_FORWARD_FAILURE,
        "remote: error: GH006: Protected branch update failed for refs/heads/main.\n"
        " ! [remote rejected] main -> main (protected branch hook declined)\n",
        "remote: error: GH013: push declined due to repository rule violations\n"
        " ! [remote rejected] x -> x (push declined due to repository rule violations)\n",
        "remote: Permission to o/r.git denied to someone.\nfatal: Authentication failed\n",
    ],
)
async def test_a_real_rejection_is_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    bare, work = _make_repo(tmp_path)
    bindir, counter = _fail_push_shim(tmp_path, failure, times=99)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    with pytest.raises(PushError) as caught:
        await push_branch(work, "rework/test", backoff_s=FAST)

    assert _push_attempts(counter) == 1, "a real rejection must not be retried"
    assert not caught.value.retryable
    assert caught.value.attempts == 1
    assert "rework/test" not in _remote_refs(bare), "the rejected push never landed"
    message = str(caught.value)
    # git's full output, plainly: the reason line survives, every line of it
    for line in failure.strip().splitlines():
        assert line.strip() in message
    assert "after 3 attempts" not in message


@pytest.mark.parametrize(
    "output",
    [
        " ! [remote rejected] wip -> wip (failed)",
        "error: RPC failed; HTTP 503 curl 22\nThe requested URL returned error: 503",
        "fatal: unable to access 'https://github.com/o/r.git/': Could not resolve host: github.com",
        "error: RPC failed; curl 56 OpenSSL SSL_read: connection reset by peer",
        "(git timeout after 120s)",
    ],
)
def test_transient_push_failures_are_classified(output: str) -> None:
    code = 124 if output.startswith("(git timeout") else 1
    assert push_failure_is_transient(output, code) is True


@pytest.mark.parametrize(
    "output",
    [
        NON_FAST_FORWARD_FAILURE,
        "remote: error: GH006: Protected branch update failed for refs/heads/main.\n"
        " ! [remote rejected] main -> main (protected branch hook declined)",
        "remote: error: GH013: push declined due to repository rule violations",
        "remote: Permission to o/r.git denied to someone.\nfatal: Authentication failed",
        " ! [remote rejected] x -> x (pre-receive hook declined)",
    ],
)
def test_real_rejections_are_not_transient(output: str) -> None:
    assert push_failure_is_transient(output) is False


async def test_git_timeout_keeps_the_real_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A REAL subprocess: a `git` on PATH that writes two lines to stderr and
    then hangs. `_run` used to swap git's output for the bare "(git timeout)"
    (a second communicate() after a timeout returns b"" on real asyncio); the
    partial output is the only evidence of a push that hung mid-negotiation."""
    import rework.nodes.git_ops as git_ops

    bindir = tmp_path / "hangbin"
    bindir.mkdir()
    shim = bindir / "git"
    shim.write_text(
        "#!/bin/sh\necho 'remote: banner line' >&2\necho 'fatal: early EOF' >&2\nexec sleep 30\n"
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    code, out = await git_ops._run(tmp_path, "push", timeout_s=1)

    assert code == 124
    lines = out.splitlines()
    assert lines[:2] == ["remote: banner line", "fatal: early EOF"], out
    assert lines[-1] == "(git timeout after 1s)"
    assert push_failure_is_transient(out, code) is True


# ---------------------------------------------------------------------------
# (c) a persistent delivery failure leaves a finished mission finished ——
#     work + summary kept, the FULL error recorded, retryable
# ---------------------------------------------------------------------------


async def test_delivery_failure_keeps_a_finished_mission_done_and_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bare, work = _make_repo(tmp_path)
    # a rejection that repeats, and is longer than the old 2000-char cut so the
    # test also proves nothing is truncated on the way into the mission row
    padding = "".join(f"remote: hint line {i:04d} about this remote hiccup\n" for i in range(60))
    bindir, _ = _fail_push_shim(tmp_path, f"{padding}{GITHUB_TRANSIENT_FAILURE}", times=99)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    store = await open_sqlite(str(tmp_path / "d.sqlite3"))
    try:
        mission = await store.create_mission(
            Mission(
                id="mdeliver0001",
                objective="deliver the thing",
                title="Deliver",
                status=MissionStatus.DONE,
                plan={"kind": "fix-pr", "publish": "pr"},
                repo_path=str(work),
                result={"summary": "the meaningful summary"},
            )
        )
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
        assert stored.result["pr_attempts"] == 3, "the push retries then gives up"

        error = stored.result["pr_error"]
        assert len(error) > 2000, "the recorded error must not be truncated"
        assert "hint line 0000" in error, "the head of the output survives"
        assert "hint line 0059" in error, "content past 2000 chars survives"
        assert "[remote rejected]" in error
        # git puts the reason LAST, after the forge's "Create a pull request" hint
        assert error.splitlines()[-1].strip().startswith("error: failed to push some refs")
        assert _remote_refs(bare) == ["main"], "the work is only in the worktree, not the remote"
    finally:
        await store.aclose()


async def test_the_daemon_runner_error_handler_keeps_push_output_whole(
    tmp_path: Path,
) -> None:
    """Second half of (c): if a delivery error ever does escape a runner, the
    daemon's handler must not cut it at 2000 chars (git's reason is last)."""
    from rework.orchestrator.daemon import Daemon

    store = await open_sqlite(str(tmp_path / "err.sqlite3"))
    try:
        mission = await store.create_mission(
            Mission(id="merr00000001", objective="x", status=MissionStatus.RUNNING)
        )
        daemon = Daemon.__new__(Daemon)  # the handler only needs these two
        daemon._store = store
        daemon._bus = InProcessEventBus(store)
        exc = PushError(
            "git push failed after 3 attempts (transient): "
            + "remote: hint line\n" * 200
            + GITHUB_TRANSIENT_FAILURE,
            output=GITHUB_TRANSIENT_FAILURE,
            retryable=True,
            attempts=3,
        )
        await daemon._log_runner_error(mission.id, exc)
        stored = await store.get_mission(mission.id)
        assert stored is not None
        # the push error is not a finished mission: this one really is failed
        assert stored.status == MissionStatus.FAILED
        error = stored.result["error"]
        assert len(error) > 2000, "a push error is kept whole in the mission row"
        assert error.splitlines()[-1].strip().startswith("error: failed to push some refs"), (
            "the LAST line of git's output survives the daemon's error record"
        )
        assert "! [remote rejected] rework/test -> rework/test (failed)" in error
    finally:
        await store.aclose()


# ---------------------------------------------------------------------------
# (d) the retry surface: shared by `rework mission retry-pr ID` and the TUI
# ---------------------------------------------------------------------------


async def test_retry_pr_succeeds_on_the_second_attempt_and_clears_the_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bare, work = _make_repo(tmp_path)
    fake_gh = tmp_path / "fake-gh"
    log = tmp_path / "gh-calls.log"
    fake_gh.write_text('#!/bin/sh\necho "$@" >> ' + str(log) + "\necho https://x/pull/7\n")
    fake_gh.chmod(0o755)
    monkeypatch.setenv("REWORK_GH_BIN", str(fake_gh))
    plain_path = os.environ["PATH"]
    store = await open_sqlite(str(tmp_path / "d2.sqlite3"))
    try:
        mission = await store.create_mission(
            Mission(
                id="mretry000001",
                objective="deliver the thing",
                title="Deliver",
                status=MissionStatus.DONE,
                plan={"kind": "fix-pr", "publish": "pr"},
                repo_path=str(work),
                result={"summary": "kept"},
            )
        )
        # first delivery: the push is rejected for good (gh is never reached)
        bindir, _ = _fail_push_shim(tmp_path, GITHUB_TRANSIENT_FAILURE, times=99)
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
        assert (
            await deliver_pr(
                store,
                None,
                mission.id,
                branch="rework/test",
                worktree=str(work),
                title="rework: Deliver",
                body="body",
            )
            is None
        )
        pending = await store.get_mission(mission.id)
        assert pending is not None
        assert pending.result["pr_pending"] is True
        assert not log.exists(), "no PR was attempted while the push kept failing"

        # the remote recovers; the surface the CLI and the TUI share retries
        monkeypatch.setenv("PATH", plain_path)
        ok, detail = await retry_pr(store, None, mission.id)
        assert ok, detail
        assert detail == "https://x/pull/7"

        after = await store.get_mission(mission.id)
        assert after is not None
        assert after.status == MissionStatus.DONE
        assert after.result["pr_pending"] is False
        assert after.result["pr_status"] == "opened"
        assert after.result["pr_url"] == "https://x/pull/7"
        assert "pr_error" not in after.result, "a successful retry clears the recorded error"
        assert after.result["summary"] == "kept"
    finally:
        await store.aclose()
    assert "rework/test" in _remote_refs(bare), "the retried push landed the branch"
    assert "pr create" in log.read_text()


def test_cli_mission_retry_pr_delivers_and_reports_the_url(tmp_path: Path) -> None:
    """`rework mission retry-pr ID` is the documented owner surface."""
    from typer.testing import CliRunner

    from rework.cli import app

    bare, work = _make_repo(tmp_path)
    db = tmp_path / "cli.sqlite3"
    fake_gh = tmp_path / "fake-gh"
    fake_gh.write_text("#!/bin/sh\necho https://x/pull/8\n")
    fake_gh.chmod(0o755)

    def seed() -> None:
        async def go() -> None:
            store = await open_sqlite(str(db))
            try:
                await store.create_mission(
                    Mission(
                        id="mcli00000001",
                        objective="deliver",
                        title="Deliver",
                        status=MissionStatus.DONE,
                        plan={"kind": "fix-pr", "publish": "pr"},
                        repo_path=str(work),
                        result={
                            "summary": "kept",
                            "pr_pending": True,
                            "pr_branch": "rework/test",
                            "pr_worktree": str(work),
                            "pr_title": "rework: Deliver",
                            "pr_body": "body",
                        },
                    )
                )
            finally:
                await store.aclose()

        asyncio.run(go())

    seed()
    env = {
        "REWORK_DB": str(db),
        "REWORK_GH_BIN": str(fake_gh),
        "PATH": os.environ["PATH"],
    }
    result = CliRunner().invoke(app, ["mission", "retry-pr", "mcli00000001"], env=env)
    assert result.exit_code == 0, result.output
    assert "PR opened: https://x/pull/8" in result.output

    async def read() -> Mission | None:
        store = await open_sqlite(str(db))
        try:
            return await store.get_mission("mcli00000001")
        finally:
            await store.aclose()

    after = asyncio.run(read())
    assert after is not None
    assert after.result["pr_pending"] is False
    assert after.result["pr_url"] == "https://x/pull/8"
    assert "rework/test" in _remote_refs(bare)


def test_cli_retry_pr_prints_the_full_error_and_exits_nonzero(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from rework.cli import app

    bare, work = _make_repo(tmp_path)
    db = tmp_path / "cli2.sqlite3"
    bindir, _ = _fail_push_shim(tmp_path, GITHUB_TRANSIENT_FAILURE, times=99)

    async def seed() -> None:
        store = await open_sqlite(str(db))
        try:
            await store.create_mission(
                Mission(
                    id="mcli00000002",
                    objective="deliver",
                    title="Deliver",
                    status=MissionStatus.DONE,
                    plan={"kind": "fix-pr", "publish": "pr"},
                    repo_path=str(work),
                    result={
                        "pr_pending": True,
                        "pr_branch": "rework/test",
                        "pr_worktree": str(work),
                    },
                )
            )
        finally:
            await store.aclose()

    asyncio.run(seed())
    env = {"REWORK_DB": str(db), "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"}
    result = CliRunner().invoke(app, ["retry-pr", "mcli00000002"], env=env)
    assert result.exit_code == 1
    combined = result.output + (result.stderr if result.stderr_bytes else "")
    assert "[remote rejected]" in combined, "the owner sees git's own words"
    assert "(failed)" in combined
    assert "error: failed to push some refs" in combined
    assert _remote_refs(bare) == ["main"], "nothing was pushed, the mission is still retryable"


# ---------------------------------------------------------------------------
# gh's own retry rules (unchanged behaviour, pinned here)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# audit round 2: no-raise guarantee, dead worktree, end-to-end DONE write
# ---------------------------------------------------------------------------


async def _done_mission(store: Any, work: Path, mid: str, **result: Any) -> Mission:
    return await store.create_mission(
        Mission(
            id=mid,
            objective="deliver the thing",
            title="Deliver",
            status=MissionStatus.DONE,
            plan={"kind": "fix-pr", "publish": "pr"},
            repo_path=str(work),
            result={"summary": "kept", **result},
        )
    )


async def test_a_plain_runtime_error_from_pr_create_still_leaves_the_mission_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not a PushError/GHError: a bare RuntimeError must be recorded, not raised."""
    _, work = _make_repo(tmp_path)
    long_error = "".join(f"gh line {i:04d}\n" for i in range(400)) + "the LAST line"

    async def boom(*_a: Any, **_k: Any) -> str:
        raise RuntimeError(long_error)

    monkeypatch.setattr("rework.orchestrator.delivery.pr_create", boom)
    store = await open_sqlite(str(tmp_path / "d.sqlite3"))
    try:
        mission = await _done_mission(store, work, "mplainerr0001")
        url = await deliver_pr(
            store,
            InProcessEventBus(store),
            mission.id,
            branch="rework/test",
            worktree=str(work),
            title="t",
            body="b",
        )
        assert url is None
        stored = await store.get_mission(mission.id)
        assert stored is not None
        assert stored.status == MissionStatus.DONE
        assert stored.result["summary"] == "kept"
        assert stored.result["pr_pending"] is True
        assert stored.result["pr_error"].endswith("the LAST line"), "untruncated"
        assert len(stored.result["pr_error"]) > 2000
    finally:
        await store.aclose()


async def test_a_store_or_bus_failure_in_delivery_bookkeeping_does_not_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, work = _make_repo(tmp_path)

    class ExplodingBus:
        async def publish(self, *_a: Any, **_k: Any) -> None:
            raise OSError("bus is down")

    async def boom(*_a: Any, **_k: Any) -> str:
        raise RuntimeError("gh exploded")

    store = await open_sqlite(str(tmp_path / "d.sqlite3"))
    try:
        mission = await _done_mission(store, work, "mstorefail001")

        async def write_fails(*_a: Any, **_k: Any) -> Any:
            raise OSError("disk is full")

        monkeypatch.setattr(store, "update_mission", write_fails)
        # failure path: write fails AND the bus fails -> still returns None
        monkeypatch.setattr("rework.orchestrator.delivery.pr_create", boom)
        args: dict[str, Any] = {"branch": "rework/test", "worktree": str(work)}
        args |= {"title": "t", "body": "b"}
        assert await deliver_pr(store, ExplodingBus(), mission.id, **args) is None

        # success path: the PR exists, so the URL is returned even if the
        # bookkeeping that records it fails
        async def ok(*_a: Any, **_k: Any) -> str:
            return "https://x/pull/1"

        monkeypatch.setattr("rework.orchestrator.delivery.pr_create", ok)
        assert await deliver_pr(store, ExplodingBus(), mission.id, **args) == "https://x/pull/1"
    finally:
        await store.aclose()


async def test_retry_pr_with_both_worktree_and_repo_gone_returns_an_explicit_error(
    tmp_path: Path,
) -> None:
    store = await open_sqlite(str(tmp_path / "d.sqlite3"))
    try:
        gone = tmp_path / "deleted-worktree"
        gone_repo = tmp_path / "deleted-repo"  # neither exists
        mission = await store.create_mission(
            Mission(
                id="mgone0000001",
                objective="deliver the thing",
                status=MissionStatus.DONE,
                plan={"kind": "fix-pr", "publish": "pr"},
                repo_path=str(gone_repo),
                result={
                    "summary": "kept",
                    "pr_pending": True,
                    "pr_branch": "rework/gone",
                    "pr_worktree": str(gone),
                    "pr_error": "PushError: the original failure text",
                },
            )
        )
        ok, detail = await retry_pr(store, None, mission.id)  # must not raise
        assert ok is False
        assert "worktree" in detail and "is gone" in detail
        assert "cannot be retried automatically" in detail
        assert "rework/gone" in detail
        assert str(gone) in detail and str(gone_repo) in detail
        assert "PushError: the original failure text" in detail, "underlying error kept whole"
        assert "FileNotFoundError" not in detail
    finally:
        await store.aclose()


async def test_retry_pr_falls_back_to_the_repo_when_the_recorded_worktree_is_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bare, work = _make_repo(tmp_path)
    fake_gh = tmp_path / "fake-gh"
    fake_gh.write_text("#!/bin/sh\necho https://x/pull/8\n")
    fake_gh.chmod(0o755)
    monkeypatch.setenv("REWORK_GH_BIN", str(fake_gh))
    store = await open_sqlite(str(tmp_path / "d.sqlite3"))
    try:
        mission = await _done_mission(
            store,
            work,
            "mfallback0001",
            pr_pending=True,
            pr_branch="rework/test",
            pr_worktree=str(tmp_path / "deleted-worktree"),
        )
        ok, detail = await retry_pr(store, None, mission.id)
        assert ok, detail
        assert detail == "https://x/pull/8"
        assert "rework/test" in _remote_refs(bare)
    finally:
        await store.aclose()


async def test_mission_whose_delivery_persistently_fails_stays_done_with_the_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end through MissionRunner/Agent: the FINAL persisted row, after
    `_synthesize` rebuilds the result, still says DONE + pr_pending + the whole
    error (git's reason is the last line). Guards `DELIVERY_RESULT_KEYS` in
    `_synthesize`: without that copy the marker is overwritten and this fails."""
    import json

    dest, bare = _toyrepo_with_remote(tmp_path)
    fake_gh = tmp_path / "fake-gh"
    fake_gh.write_text("#!/bin/sh\necho https://github.com/roowus/toyrepo/pull/12\n")
    fake_gh.chmod(0o755)
    monkeypatch.setenv("REWORK_GH_BIN", str(fake_gh))
    monkeypatch.setattr("rework.nodes.git_ops.PUSH_BACKOFF_S", FAST)
    bindir, counter = _fail_push_shim(tmp_path, GITHUB_TRANSIENT_FAILURE, times=99)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    from rework.orchestrator.daemon import sqlite_checkpointer_factory

    store = await open_sqlite(str(tmp_path / "pr.sqlite3"))
    try:
        objective = "write docs: add a line to README saying hello from rework"
        plan = json.loads(json.dumps(PLAN_FIX_PR).replace("$OBJECTIVE", objective))
        mission = await store.create_mission(
            Mission(id="mpersistent01", objective=objective, repo_path=str(dest), plan=plan)
        )
        provider = ScriptedProvider()
        provider.route("Investigate any brief", _investigator_script())
        provider.route("Fulfill the task", _coder_script("docs line added, tests still green"))
        provider.route("Find issues", _auditor_script(AUDIT_CLEAN))
        runner = MissionRunner(
            mission.id,
            store,
            InProcessEventBus(store),
            autodiscover(),
            role_catalog(),
            sqlite_checkpointer_factory(str(tmp_path / "ck.sqlite3")),
            tmp_path / "work",
            poll_interval_s=0.05,
            provider=provider,
        )
        await asyncio.wait_for(asyncio.create_task(runner.run()), timeout=60)

        final = await store.get_mission(mission.id)
        assert final is not None
        assert final.status == MissionStatus.DONE, "a persistent push failure must not fail it"
        assert final.status != MissionStatus.FAILED
        # PLAN_FIX_PR has no orchestrator, so the summary is the coder's artifact
        assert any(k.startswith("coder") for k in final.result["artifacts"]), "the work is kept"
        assert final.result["pr_pending"] is True
        assert final.result["pr_status"] == "failed"
        assert not final.result.get("pr_url")
        error = final.result["pr_error"]
        assert "[remote rejected]" in error
        assert error.splitlines()[-1].strip().startswith("error: failed to push some refs"), (
            "git's LAST line survives, untruncated"
        )
        assert final.result["pr_branch"], "everything needed to retry is persisted"
        assert _push_attempts(counter) == 3, "retried up to the cap, then gave up"
        assert _remote_refs(bare) == ["main"]
    finally:
        await store.aclose()


@pytest.mark.parametrize(
    "output",
    [
        # a branch/path that merely CONTAINS a marker word is not a rejection
        "To https://github.com/o/r.git\n"
        " ! [remote rejected] rework/fix-permission-denied-gate"
        " -> rework/fix-permission-denied-gate (failed)\n",
        # progress counters are not HTTP statuses; a path containing `ssl` is not TLS
        "Writing objects: 100% (512/512)\n ! [remote rejected] wip -> wip (failed)",
    ],
)
def test_marker_words_inside_names_do_not_flip_the_classification(output: str) -> None:
    assert push_failure_is_transient(output) is True


@pytest.mark.parametrize(
    "output",
    [
        " ! [remote rejected] wip -> wip (stale info)",
        " ! [remote rejected] wip -> wip (fetch first)",
        " ! [remote rejected] wip -> wip (pre-receive hook declined)",
        " ! [remote rejected] wip -> wip (failed to update ref: locked)",
        "error: refs 5432 pushed to /var/ssl-ca/timeout-box.git",
    ],
)
def test_reasoned_or_unrelated_output_is_not_mistaken_for_a_transient_failure(
    output: str,
) -> None:
    assert push_failure_is_transient(output) is False


def test_pr_classification_ignores_marker_words_in_names_and_numbers() -> None:
    assert pr_failure_is_transient("created 512 objects, 5432 bytes") is False
    assert pr_failure_is_transient("could not open /tmp/server-permission/x: timed out") is True
    assert pr_failure_is_transient("HTTP 502: Bad Gateway for branch rework/permission-fix") is True


@pytest.mark.parametrize(
    "output",
    [
        # bare curl form, no `RPC failed` / `HTTP` prefix
        "error: The requested URL returned error: 503",
        "fatal: unable to access: The requested URL returned error: 502",
        # proxy/init noise that merely contains a weak word, then a 5xx
        "invalid response from proxy\nerror: The requested URL returned error: 504",
    ],
)
def test_a_bare_5xx_is_retryable_for_push_and_pr(output: str) -> None:
    assert push_failure_is_transient(output) is True
    assert pr_failure_is_transient(output) is True


def test_weak_words_are_not_definitive_non_retry_markers() -> None:
    # `invalid` / bare `authentication` are not decisions by the forge: they
    # must not turn a timeout into a "real rejection"
    assert pr_failure_is_transient("invalid response from proxy: timed out") is True
    assert pr_failure_is_transient("authentication proxy init: connection reset") is True
    # ...and on their own they do not make anything retryable either
    assert pr_failure_is_transient("invalid response") is False
    assert push_failure_is_transient("invalid response") is False
    # the forge's real decisions still win
    assert pr_failure_is_transient("gh: Authentication failed (HTTP 401)") is False
    assert pr_failure_is_transient("GraphQL: Resource not accessible: forbidden") is False
    assert pr_failure_is_transient("remote: error: GH006: Protected branch update failed") is False


@pytest.mark.parametrize(
    "output",
    [
        "remote: You have exceeded a secondary rate limit. Please wait a few minutes.\n"
        "fatal: unable to access 'https://github.com/o/r.git/':"
        " The requested URL returned error: 403",
        "remote: You have triggered an abuse detection mechanism.\n"
        "error: The requested URL returned error: 403",
        "error: The requested URL returned error: 429 Too Many Requests",
    ],
)
def test_a_rate_limit_403_is_retryable_for_push_and_pr(output: str) -> None:
    assert push_failure_is_transient(output) is True
    assert pr_failure_is_transient(output) is True


@pytest.mark.parametrize(
    "output",
    [
        "remote: Permission to o/r.git denied to someone.\n"
        "fatal: unable to access 'https://github.com/o/r.git/':"
        " The requested URL returned error: 403",
        "error: The requested URL returned error: 403",
        "remote: error: GH006: Protected branch update failed for refs/heads/main.",
    ],
)
def test_a_plain_permission_403_is_still_not_retried(output: str) -> None:
    assert push_failure_is_transient(output) is False


async def test_git_that_closes_its_pipes_and_keeps_running_is_still_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """EOF on the pipe is not process exit: a git that writes a line, closes
    stdout/stderr and then keeps running must still hit the deadline (the
    post-EOF wait used to be unbounded and wedged the caller forever)."""
    import time

    import rework.nodes.git_ops as git_ops

    bindir = tmp_path / "closebin"
    bindir.mkdir()
    shim = bindir / "git"
    shim.write_text("#!/bin/sh\necho 'last words'\nexec 1>&- 2>&-\nsleep 30\n")
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    started = time.monotonic()
    code, out = await asyncio.wait_for(git_ops._run(tmp_path, "push", timeout_s=1), timeout=10)

    assert time.monotonic() - started < 3
    assert code == 124
    assert out.splitlines()[0] == "last words"
    assert out.splitlines()[-1] == "(git timeout after 1s)"


# ---------------------------------------------------------------------------
# a plan that does not publish has no branch to deliver: retry must refuse
# ---------------------------------------------------------------------------

DRAFT_REFUSAL = "does not publish"


async def _draft_mission_with_a_coder_branch(store: Any, work: Path, mid: str) -> Mission:
    """A finished `draft` mission that DOES carry a coder branch artifact: the
    exact shape that used to let retry-pr push and open a PR for a mission
    whose deliverable is its Report tab."""
    from rework.kernel.state.records import Run, RunStatus, run_id
    from rework.missions import orchestrated_plan

    mission = await store.create_mission(
        Mission(
            id=mid,
            objective="audit the release process",
            title="Audit the release process",
            status=MissionStatus.DONE,
            plan=orchestrated_plan("draft", "pro"),
            repo_path=str(work),
            result={"summary": "kept"},
        )
    )
    await store.create_run(
        Run(
            id=run_id(mid, "code", "coder", 1),
            mission_id=mid,
            role="coder",
            step="code",
            status=RunStatus.COMPLETED,
            artifact={"branch": "rework/test", "worktree": str(work)},
        )
    )
    return mission


async def test_retry_pr_refuses_a_draft_mission_that_carries_a_coder_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The branch fallback in retry_pr used to push any coder branch. For a
    draft mission there is nothing to publish, so it must refuse before git or
    gh is reached at all."""
    bare, work = _make_repo(tmp_path)
    calls: list[str] = []

    async def boom(*_a: Any, **_k: Any) -> str:
        calls.append("pr_create")
        raise AssertionError("pr_create must never be called for a draft mission")

    monkeypatch.setattr("rework.orchestrator.delivery.pr_create", boom)
    store = await open_sqlite(str(tmp_path / "draft.sqlite3"))
    try:
        mission = await _draft_mission_with_a_coder_branch(store, work, "mdraft000001")
        ok, detail = await retry_pr(store, None, mission.id)
        assert ok is False, "a draft mission must not be delivered"
        assert DRAFT_REFUSAL in detail, detail
        assert "Report tab" in detail, "the refusal says where the deliverable really is"
        assert calls == [], "pr_create ran for a mission that publishes nothing"
        after = await store.get_mission(mission.id)
        assert after is not None
        assert not (after.result or {}).get("pr_pending"), "nothing was left to retry"
        assert not (after.result or {}).get("pr_url")
    finally:
        await store.aclose()
    assert _remote_refs(bare) == ["main"], "the coder branch was pushed by a retry"


@pytest.mark.parametrize(
    "legacy_plan",
    [
        {"playbook": "fix-pr", "gates": []},
        {"kind": "fix-pr", "gates": []},
    ],
)
async def test_retry_pr_still_delivers_a_legacy_fix_pr_plan_without_a_publish_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, legacy_plan: dict[str, Any]
) -> None:
    """Missions created before the `publish` key existed can carry a pending PR,
    and `rework mission retry-pr` is the operator's only recovery for them."""
    bare, work = _make_repo(tmp_path)
    fake_gh = tmp_path / "fake-gh"
    log = tmp_path / "gh-calls.log"
    fake_gh.write_text('#!/bin/sh\necho "$@" >> ' + str(log) + "\necho https://x/pull/9\n")
    fake_gh.chmod(0o755)
    monkeypatch.setenv("REWORK_GH_BIN", str(fake_gh))
    store = await open_sqlite(str(tmp_path / "legacy.sqlite3"))
    try:
        mission = await store.create_mission(
            Mission(
                id="mlegacy00001",
                objective="fix the thing",
                title="Fix",
                status=MissionStatus.DONE,
                plan=dict(legacy_plan),
                repo_path=str(work),
                result={
                    "summary": "kept",
                    "pr_pending": True,
                    "pr_branch": "rework/test",
                    "pr_worktree": str(work),
                },
            )
        )
        assert "publish" not in mission.plan
        ok, detail = await retry_pr(store, None, mission.id)
        assert ok, detail
        assert detail == "https://x/pull/9"
        after = await store.get_mission(mission.id)
        assert after is not None
        assert after.result["pr_pending"] is False
        assert after.result["pr_url"] == "https://x/pull/9"
    finally:
        await store.aclose()
    assert "rework/test" in _remote_refs(bare), "the retried push landed the branch"
    assert "pr create" in log.read_text()


def test_cli_retry_pr_refuses_a_draft_mission_with_a_clean_message(tmp_path: Path) -> None:
    """The owner surface: an operator who types retry-pr against an audit gets
    the refusal in plain words, not a traceback and not a push."""
    from typer.testing import CliRunner

    from rework.cli import app

    bare, work = _make_repo(tmp_path)
    db = tmp_path / "cli-draft.sqlite3"

    async def seed() -> None:
        store = await open_sqlite(str(db))
        try:
            await _draft_mission_with_a_coder_branch(store, work, "mdraft000002")
        finally:
            await store.aclose()

    asyncio.run(seed())
    env = {"REWORK_DB": str(db), "PATH": os.environ["PATH"]}
    result = CliRunner().invoke(app, ["mission", "retry-pr", "mdraft000002"], env=env)
    combined = result.output + (result.stderr if result.stderr_bytes else "")
    assert result.exit_code == 1, combined
    assert DRAFT_REFUSAL in combined, combined
    assert "Report tab" in combined
    assert "Traceback" not in combined, "the refusal is an expected outcome, not a crash"
    assert _remote_refs(bare) == ["main"], "the retry pushed a draft mission's branch"
