"""Mission PRs must arrive MERGEABLE, not stale.

Found live: most PRs opened by missions arrived conflicting with main (4 to 8
files each), because other missions merged while this one worked. So the push
that opens the PR is preceded by `git merge origin/main` in the coder's branch.

A real conflict is never guessed at: the merge is aborted, the branch is left
byte-identical to what the coder left, and the PR still opens with the
conflicting paths in the body and an INFO event. Every path that delivers a PR
(`_create_pr` AND `retry_pr`, the Retry PR button) goes through `deliver_pr`,
which is why the merge lives there.
"""

from __future__ import annotations

import asyncio
import json
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
from rework.nodes.git_ops import MergeResult, merge_base_into_branch
from rework.orchestrator import delivery
from rework.orchestrator.delivery import deliver_pr, retry_pr
from rework.orchestrator.engine import MissionRunner
from rework.roles.registry import autodiscover

BRANCH = "rework/test"


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _commit(repo: Path, message: str) -> str:
    _git("add", "-A", cwd=repo)
    _git("-c", "user.email=r@r", "-c", "user.name=r", "commit", "-m", message, cwd=repo)
    return _git("rev-parse", "HEAD", cwd=repo).stdout.strip()


def _rev(repo: Path, ref: str) -> str:
    return _git("rev-parse", ref, cwd=repo).stdout.strip()


def _movable_repo(tmp_path: Path) -> tuple[Path, Path, Path]:
    """(bare origin, coder work repo on `rework/test`, a second clone of origin).

    The coder's branch is cut from main BEFORE the second clone advances main,
    which is the live situation: main moves while the mission works."""
    bare = tmp_path / "origin.git"
    bare.mkdir()
    _git("init", "--bare", "-b", "main", cwd=bare)
    work = tmp_path / "work"
    work.mkdir()
    _git("init", "-b", "main", cwd=work)
    (work / "a.txt").write_text("line one\n")
    _commit(work, "init")
    _git("remote", "add", "origin", str(bare), cwd=work)
    _git("push", "-u", "origin", "main", cwd=work)
    _git("checkout", "-b", BRANCH, cwd=work)
    (work / "b.txt").write_text("coder work\n")
    _commit(work, "coder work")
    other = tmp_path / "other"
    _git("clone", str(bare), str(other), cwd=tmp_path)
    return bare, work, other


def _is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    return (
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", ancestor, descendant],
            cwd=repo,
            capture_output=True,
        ).returncode
        == 0
    )


def _merge_head_present(repo: Path) -> bool:
    return (
        subprocess.run(
            ["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"], cwd=repo, capture_output=True
        ).returncode
        == 0
    )


def _advance_main(other: Path, files: dict[str, str], message: str = "another mission") -> str:
    """Another mission merges into main: a fresh commit on the bare origin."""
    for name, text in files.items():
        (other / name).write_text(text)
    sha = _commit(other, message)
    _git("push", "origin", "main", cwd=other)
    return sha


def _unrelated_main(tmp_path: Path) -> tuple[Path, Path]:
    """(bare origin, work repo on `rework/test`) where origin/main is a SECOND,
    independent history: git REFUSES the merge (`refusing to merge unrelated
    histories`) instead of conflicting, so there are no unmerged paths."""
    bare = tmp_path / "origin.git"
    bare.mkdir()
    _git("init", "--bare", "-b", "main", cwd=bare)
    work = tmp_path / "work"
    work.mkdir()
    _git("init", "-b", "main", cwd=work)
    (work / "a.txt").write_text("line one\n")
    _commit(work, "init")
    _git("remote", "add", "origin", str(bare), cwd=work)
    _git("checkout", "-b", BRANCH, cwd=work)
    (work / "b.txt").write_text("coder work\n")
    _commit(work, "coder work")
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    _git("init", "-b", "main", cwd=unrelated)
    (unrelated / "z.txt").write_text("a different root commit\n")
    _commit(unrelated, "unrelated root")
    _git("remote", "add", "origin", str(bare), cwd=unrelated)
    _git("push", "-f", "origin", "main", cwd=unrelated)
    return bare, work


def _stopped_merge(work: Path) -> None:
    """Leave the worktree exactly as a crash mid-merge does: MERGE_HEAD set and
    `<<<<<<<` markers on disk (the merge is expected to conflict)."""
    subprocess.run(
        ["git", "-c", "user.email=r@r", "-c", "user.name=r", "merge", "origin/main"],
        cwd=work,
        capture_output=True,
    )
    assert _merge_head_present(work), "the test must reproduce a stopped merge"


def _merge_events(notes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The INFO payloads the merge step publishes (they carry `base`)."""
    return [n for n in notes if "base" in n]


def _fake_gh(tmp_path: Path) -> tuple[Path, Path]:
    """A fake `gh` that records the PR body to a file and prints a URL."""
    body_file = tmp_path / "gh-body.txt"
    fake = tmp_path / "fake-gh"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, pathlib\n"
        f"body_file = pathlib.Path({str(body_file)!r})\n"
        "args = sys.argv[1:]\n"
        "if '--body' in args:\n"
        "    body_file.write_text(args[args.index('--body') + 1])\n"
        "print('https://github.com/roowus/repo/pull/1')\n"
    )
    fake.chmod(0o755)
    return fake, body_file


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


async def _notes(store: Any, mission_id: str) -> list[dict[str, Any]]:
    events = await store.events_since(0, mission_id=mission_id, limit=1000)
    return [dict(e.payload) for e in events]


# ---------------------------------------------------------------------------
# a clean merge rides along with the coder's commits
# ---------------------------------------------------------------------------


async def test_a_clean_merge_with_main_is_pushed_with_the_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bare, work, other = _movable_repo(tmp_path)
    main_sha = _advance_main(other, {"other.txt": "another mission's work\n"})
    fake_gh, body_file = _fake_gh(tmp_path)
    monkeypatch.setenv("REWORK_GH_BIN", str(fake_gh))
    coder_sha = _rev(work, "HEAD")

    store = await open_sqlite(str(tmp_path / "m.sqlite3"))
    try:
        mission = await _done_mission(store, work, "mmerge000001")
        url = await deliver_pr(
            store,
            InProcessEventBus(store),
            mission.id,
            branch=BRANCH,
            worktree=str(work),
            title="rework: Deliver",
            body="## What changed\n\nsomething\n",
        )
        assert url == "https://github.com/roowus/repo/pull/1"

        # the pushed tip is origin/<branch> in the BARE repo: it contains main
        # and the coder's own commit, i.e. the PR would be mergeable
        tip = _rev(bare, f"refs/heads/{BRANCH}")
        assert tip == _rev(work, "HEAD"), "the merge commit is what got pushed"
        assert _rev(bare, "refs/heads/main") == main_sha
        assert _is_ancestor(bare, main_sha, tip), "main must be an ancestor of the pushed branch"
        assert _is_ancestor(bare, coder_sha, tip), "the coder's commit must still be there"
        log = _git("log", "--oneline", f"refs/heads/{BRANCH}", cwd=bare).stdout
        assert "Merge origin/main into rework/test" in log, log
        assert "another mission" in log, "the other mission's commit is not lost"

        # a clean worktree: a later `git add -A` picks up no junk
        assert _git("status", "--porcelain", cwd=work).stdout == ""
        body = body_file.read_text()
        assert "Merge with main" not in body, "a clean merge needs no body note"
        notes = await _notes(store, mission.id)
        assert any("merged main into" in str(n.get("note", "")) for n in notes), notes
    finally:
        await store.aclose()


# ---------------------------------------------------------------------------
# a real conflict: branch untouched, PR still opens, paths in the body
# ---------------------------------------------------------------------------


async def test_a_conflict_leaves_the_branch_untouched_and_still_opens_the_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bare, work, other = _movable_repo(tmp_path)
    _advance_main(other, {"a.txt": "line one\nanother mission's edit\n"})
    (work / "a.txt").write_text("line one\ncoder's edit\n")
    coder_tip = _commit(work, "coder edit")
    fake_gh, body_file = _fake_gh(tmp_path)
    monkeypatch.setenv("REWORK_GH_BIN", str(fake_gh))

    store = await open_sqlite(str(tmp_path / "c.sqlite3"))
    try:
        mission = await _done_mission(store, work, "mconflict001")
        url = await deliver_pr(
            store,
            InProcessEventBus(store),
            mission.id,
            branch=BRANCH,
            worktree=str(work),
            title="rework: Deliver",
            body="## What changed\n\nsomething\n",
        )
        assert url == "https://github.com/roowus/repo/pull/1", "the PR must still open"

        # nothing beyond the coder's own commits reached the remote
        assert _rev(bare, f"refs/heads/{BRANCH}") == coder_tip
        # the branch is byte-identical to what the coder left
        assert _rev(work, "HEAD") == coder_tip
        assert _git("status", "--porcelain", cwd=work).stdout == ""
        assert not _merge_head_present(work), "no merge may be left in progress"
        content = (work / "a.txt").read_text()
        assert content == "line one\ncoder's edit\n", content
        assert "<<<<<<<" not in content

        body = body_file.read_text()
        assert "Merge with main" in body, body
        assert "was **not** merged" in body
        assert "`a.txt`" in body, "the owner sees WHICH paths conflict"

        notes = await _notes(store, mission.id)
        merge_events = [n for n in notes if "conflicting_paths" in n]
        assert merge_events, notes
        assert merge_events[0]["conflicting_paths"] == ["a.txt"]
        assert "main was not merged" in str(merge_events[0]["note"])
    finally:
        await store.aclose()


# ---------------------------------------------------------------------------
# the retry surface merges too (it shares deliver_pr)
# ---------------------------------------------------------------------------


async def test_retry_pr_also_merges_main_before_pushing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bare, work, other = _movable_repo(tmp_path)
    fake_gh, _ = _fake_gh(tmp_path)
    monkeypatch.setenv("REWORK_GH_BIN", str(fake_gh))
    main_sha = _advance_main(other, {"later.txt": "merged while the PR was pending\n"})

    store = await open_sqlite(str(tmp_path / "r.sqlite3"))
    try:
        mission = await _done_mission(
            store,
            work,
            "mmerge000002",
            pr_pending=True,
            pr_branch=BRANCH,
            pr_worktree=str(work),
            pr_title="rework: Deliver",
            pr_body="body",
        )
        ok, detail = await retry_pr(store, InProcessEventBus(store), mission.id)
        assert ok, detail
        tip = _rev(bare, f"refs/heads/{BRANCH}")
        assert _is_ancestor(bare, main_sha, tip), "the retried delivery must merge main too"
    finally:
        await store.aclose()


# ---------------------------------------------------------------------------
# silent no-ops: no origin, wrong branch, dirty worktree, already up to date
# ---------------------------------------------------------------------------


async def test_a_repo_without_origin_is_a_silent_no_op(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repo-less / upload mission has no origin: nothing is fetched or merged,
    no body note is added, no merge event is published."""
    repo = tmp_path / "no-origin"
    repo.mkdir()
    _git("init", "-b", "main", cwd=repo)
    (repo / "a.txt").write_text("x\n")
    _commit(repo, "init")
    _git("checkout", "-b", BRANCH, cwd=repo)
    (repo / "b.txt").write_text("y\n")
    _commit(repo, "work")
    tip = _rev(repo, "HEAD")

    fake_gh, body_file = _fake_gh(tmp_path)
    monkeypatch.setenv("REWORK_GH_BIN", str(fake_gh))

    async def no_push(*_a: Any, **_k: Any) -> None:
        return None

    # there is nowhere to push to, so the push itself is stubbed: the point here
    # is that the merge step must already have been a no-op
    monkeypatch.setattr("rework.orchestrator.delivery.push_branch", no_push)
    store = await open_sqlite(str(tmp_path / "n.sqlite3"))
    try:
        mission = await _done_mission(store, repo, "mnoorigin0001")
        body = "## What changed\n\nsomething\n"
        url = await deliver_pr(
            store,
            InProcessEventBus(store),
            mission.id,
            branch=BRANCH,
            worktree=str(repo),
            title="rework: Deliver",
            body=body,
        )
        assert url == "https://github.com/roowus/repo/pull/1"
        assert _rev(repo, "HEAD") == tip, "nothing was merged"
        assert body_file.read_text() == body, "no body note for a repo with no origin"
        notes = await _notes(store, mission.id)
        assert not [n for n in notes if "base" in n or "conflicting_paths" in n], notes
    finally:
        await store.aclose()


async def test_a_failed_fetch_is_reported_and_the_pr_still_opens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fetch that cannot read the base must not block the PR, but it is NOT a
    silent skip either: the owner gets an INFO event carrying git's reason, the
    branch is left exactly as the coder left it, the PR still opens, and no
    merge paragraph claims anything happened.

    The bare origin is created with a `master` branch, so it has no `main` ref
    at all and `git fetch origin main` fails locally: no network access and no
    unreachable remote to blame is involved."""
    bare = tmp_path / "origin.git"
    bare.mkdir()
    _git("init", "--bare", "-b", "master", cwd=bare)
    work = tmp_path / "work"
    work.mkdir()
    _git("init", "-b", "main", cwd=work)
    (work / "a.txt").write_text("line one\n")
    _commit(work, "init")
    # a real `origin`, but it has no `main` ref to fetch
    _git("remote", "add", "origin", str(bare), cwd=work)
    _git("remote", "add", "depot", str(bare), cwd=work)
    _git("checkout", "-b", BRANCH, cwd=work)
    (work / "b.txt").write_text("coder work\n")
    coder_tip = _commit(work, "coder work")
    assert _git("status", "--porcelain", cwd=work).stdout == "", "the worktree is committed"
    fake_gh, body_file = _fake_gh(tmp_path)
    monkeypatch.setenv("REWORK_GH_BIN", str(fake_gh))
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    body = "## What changed\n\nsomething\n"

    result = await merge_base_into_branch(work, BRANCH)
    assert result.skipped_reason == "fetch-failed", result
    assert _rev(work, "HEAD") == coder_tip, "a failed fetch moves nothing"

    store = await open_sqlite(str(tmp_path / "fetchfail.sqlite3"))
    try:
        mission = await _done_mission(store, work, "mfetchfail01")
        url = await deliver_pr(
            store,
            InProcessEventBus(store),
            mission.id,
            branch=BRANCH,
            worktree=str(work),
            title="rework: Deliver",
            body=body,
        )
        assert url == "https://github.com/roowus/repo/pull/1", "the PR still opens"
        # the branch is untouched and the push carried the coder's tip
        assert _rev(work, "HEAD") == coder_tip, "nothing was merged"
        assert _git("status", "--porcelain", cwd=work).stdout == ""
        assert _rev(bare, f"refs/heads/{BRANCH}") == coder_tip, "delivery still pushed"
        # ... and the published body says nothing about a merge
        assert body_file.read_text() == body, "no merge paragraph for a failed fetch"
        events = _merge_events(await _notes(store, mission.id))
        assert len(events) == 1, events
        assert "fetch failed" in str(events[0]["note"]), events[0]
        assert "did not match" in str(events[0]["error"]) or "couldn't find remote ref" in str(
            events[0]["error"]
        ), events[0]
    finally:
        await store.aclose()


async def test_the_merge_helper_skips_a_repo_without_origin(tmp_path: Path) -> None:
    repo = tmp_path / "plain"
    repo.mkdir()
    _git("init", "-b", "main", cwd=repo)
    (repo / "a.txt").write_text("x\n")
    _commit(repo, "init")
    assert await merge_base_into_branch(repo, "main") == MergeResult(skipped_reason="no-origin")


async def test_the_merge_helper_skips_when_the_worktree_is_on_another_branch(
    tmp_path: Path,
) -> None:
    """`_create_pr` falls back to the daemon's shared clone when the coder
    artifact has no worktree: merging there would move the wrong ref."""
    _, work, other = _movable_repo(tmp_path)
    _git("checkout", "main", cwd=work)  # the daemon clone's branch, not the coder's
    _advance_main(other, {"other.txt": "elsewhere\n"})
    before = _rev(work, "HEAD")

    result = await merge_base_into_branch(work, BRANCH)

    assert result.skipped_reason == "wrong-branch"
    assert _rev(work, "HEAD") == before, "the wrong branch must not be moved"
    assert _git("status", "--porcelain", cwd=work).stdout == ""


async def test_the_merge_helper_skips_a_dirty_worktree(tmp_path: Path) -> None:
    _, work, other = _movable_repo(tmp_path)
    _advance_main(other, {"other.txt": "elsewhere\n"})
    (work / "b.txt").write_text("uncommitted edit\n")
    before = _rev(work, "HEAD")

    result = await merge_base_into_branch(work, BRANCH)

    assert result.skipped_reason == "dirty-worktree"
    assert _rev(work, "HEAD") == before
    assert (work / "b.txt").read_text() == "uncommitted edit\n", "the edit is left alone"


async def test_the_merge_helper_reports_up_to_date_without_a_merge_commit(
    tmp_path: Path,
) -> None:
    _, work, _ = _movable_repo(tmp_path)  # main never moved
    before = _rev(work, "HEAD")

    result = await merge_base_into_branch(work, BRANCH)

    assert result.up_to_date is True
    assert result.merged is False
    assert _rev(work, "HEAD") == before, "no empty merge commit"


# ---------------------------------------------------------------------------
# end to end through the engine: a mission whose main moved while it worked
# still pushes a branch that contains main
# ---------------------------------------------------------------------------


def _toyrepo_with_moving_main(tmp_path: Path) -> tuple[Path, Path]:
    """A committed copy of examples/toyrepo with a bare `origin` whose main
    ALREADY moved on (another mission merged while this one worked)."""
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
    (dest / ".gitignore").write_text("__pycache__/\n")
    _commit(dest, "init")
    _git("remote", "add", "origin", str(bare), cwd=dest)
    _git("push", "-u", "origin", "main", cwd=dest)
    other = tmp_path / "other"
    _git("clone", str(bare), str(other), cwd=tmp_path)
    _advance_main(other, {"from-another-mission.txt": "landed first\n"})
    return dest, bare


async def test_a_mission_pr_is_pushed_merged_with_main_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest, bare = _toyrepo_with_moving_main(tmp_path)
    main_sha = _rev(bare, "refs/heads/main")
    fake_gh, body_file = _fake_gh(tmp_path)
    monkeypatch.setenv("REWORK_GH_BIN", str(fake_gh))

    from rework.orchestrator.daemon import sqlite_checkpointer_factory

    store = await open_sqlite(str(tmp_path / "e2e.sqlite3"))
    try:
        objective = "write docs: add a line to README saying hello from rework"
        plan = json.loads(json.dumps(PLAN_FIX_PR).replace("$OBJECTIVE", objective))
        mission = await store.create_mission(
            Mission(id="mmergee2e001", objective=objective, repo_path=str(dest), plan=plan)
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
        await asyncio.wait_for(asyncio.create_task(runner.run()), timeout=120)

        done = await store.get_mission(mission.id)
        assert done is not None and done.status == MissionStatus.DONE
        assert (done.result or {}).get("pr_url") == "https://github.com/roowus/repo/pull/1"
        branches = [
            str((r.artifact or {}).get("branch"))
            for r in await store.list_runs(mission.id)
            if r.role == "coder" and (r.artifact or {}).get("branch")
        ]
        assert branches
        tip = _rev(bare, f"refs/heads/{branches[-1]}")
        assert _is_ancestor(bare, main_sha, tip), (
            "the pushed branch contains the other mission's merge, so the PR is mergeable"
        )
        assert "from-another-mission" in _git("log", "--name-only", tip, cwd=bare).stdout
        assert "Merge origin/main" in _git("log", "--oneline", tip, cwd=bare).stdout
        worktree = Path(
            str(
                next(
                    (r.artifact or {}).get("worktree")
                    for r in await store.list_runs(mission.id)
                    if (r.artifact or {}).get("worktree")
                )
            )
        )
        assert _git("status", "--porcelain", cwd=worktree).stdout == "", "no junk left behind"
        body = body_file.read_text()
        assert "Merge with main" not in body, "a clean merge adds no note"
    finally:
        await store.aclose()


# ---------------------------------------------------------------------------
# a merge git REFUSED is not a conflict
# ---------------------------------------------------------------------------


async def test_a_refused_merge_is_reported_as_failed_not_as_a_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unrelated histories: git REFUSES the merge and names no unmerged path.
    The PR must not claim a conflict, must not print `- (git reported no paths)`,
    and must carry git's real reason instead."""
    bare, work = _unrelated_main(tmp_path)
    fake_gh, body_file = _fake_gh(tmp_path)
    monkeypatch.setenv("REWORK_GH_BIN", str(fake_gh))
    coder_tip = _rev(work, "HEAD")

    store = await open_sqlite(str(tmp_path / "u.sqlite3"))
    try:
        mission = await _done_mission(store, work, "mrefused0001")
        url = await deliver_pr(
            store,
            InProcessEventBus(store),
            mission.id,
            branch=BRANCH,
            worktree=str(work),
            title="rework: Deliver",
            body="## What changed\n\nsomething\n",
        )
        assert url == "https://github.com/roowus/repo/pull/1", "the PR must still open"

        # the refused merge changed nothing
        assert _rev(work, "HEAD") == coder_tip
        assert _rev(bare, f"refs/heads/{BRANCH}") == coder_tip
        assert _git("status", "--porcelain", cwd=work).stdout == ""
        assert not _merge_head_present(work)

        body = body_file.read_text()
        assert "Merge with main" in body, body
        assert "was **not** merged" in body
        assert "git refused the merge" in body, body
        assert "Conflicting paths:" not in body, "no path list for a refused merge"
        assert "the merge conflicts" not in body
        assert "git reported no paths" not in body
        assert "unrelated histories" in body, body
        assert "<<<<<<<" not in body

        notes = _merge_events(await _notes(store, mission.id))
        assert len(notes) == 1, notes
        assert "conflicting_paths" not in notes[0], notes
        assert "refused the merge" in str(notes[0]["note"])
        assert "unrelated histories" in str(notes[0]["error"])
        assert "abort" not in str(notes[0].get("error")), "nothing to abort: no bogus warning"
    finally:
        await store.aclose()


async def test_the_merge_helper_reports_a_refused_merge_as_failed(tmp_path: Path) -> None:
    _, work = _unrelated_main(tmp_path)
    result = await merge_base_into_branch(work, BRANCH)

    assert result.failed is True
    assert result.conflicted is False
    assert result.conflicting == [], "a refused merge names no path"
    assert "unrelated histories" in result.detail, result.detail
    assert result.abort_failed is None, "there was nothing to abort, so no warning"


# ---------------------------------------------------------------------------
# a merge left behind by a dead daemon is aborted, never committed
# ---------------------------------------------------------------------------


async def test_a_leftover_merge_is_aborted_before_the_dirty_check(tmp_path: Path) -> None:
    """A crash mid-merge leaves MERGE_HEAD and `<<<<<<<` markers in the worktree.
    That must be cleaned up here: a later `stage_all` (`git add -A`) would
    otherwise commit the markers into the branch."""
    bare, work, other = _movable_repo(tmp_path)
    (work / "a.txt").write_text("line one\ncoder's edit\n")
    coder_tip = _commit(work, "coder edit")
    _advance_main(other, {"a.txt": "line one\nanother mission's edit\n"})
    _git("fetch", "origin", "main", cwd=work)
    _stopped_merge(work)  # the crash: a merge stopped with an unmerged index
    assert "<<<<<<<" in (work / "a.txt").read_text()

    result = await merge_base_into_branch(work, BRANCH)

    assert result.skipped_reason != "dirty-worktree", "the leftover merge must be aborted"
    assert result.conflicted is True, result
    assert result.conflicting == ["a.txt"]
    assert not _merge_head_present(work), "MERGE_HEAD must be gone"
    content = (work / "a.txt").read_text()
    assert "<<<<<<<" not in content, "no conflict markers may survive"
    assert content == "line one\ncoder's edit\n", content
    assert _rev(work, "HEAD") == coder_tip
    assert _git("status", "--porcelain", cwd=work).stdout == "", "nothing may stay staged"
    assert _rev(bare, "refs/heads/main") != coder_tip  # sanity: main really moved


async def test_delivery_cleans_a_leftover_merge_and_still_opens_the_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same crash seen through delivery: the push that follows carries no
    conflict markers, whatever the leftover merge was."""
    bare, work, other = _movable_repo(tmp_path)
    (work / "a.txt").write_text("line one\ncoder's edit\n")
    coder_tip = _commit(work, "coder edit")
    _advance_main(other, {"a.txt": "line one\nanother mission's edit\n"})
    _git("fetch", "origin", "main", cwd=work)
    _stopped_merge(work)  # the crash: a stopped merge, markers on disk
    fake_gh, body_file = _fake_gh(tmp_path)
    monkeypatch.setenv("REWORK_GH_BIN", str(fake_gh))

    store = await open_sqlite(str(tmp_path / "cleanup.sqlite3"))
    try:
        mission = await _done_mission(store, work, "mleftover001")
        url = await deliver_pr(
            store,
            InProcessEventBus(store),
            mission.id,
            branch=BRANCH,
            worktree=str(work),
            title="rework: Deliver",
            body="## What changed\n\nsomething\n",
        )
        assert url == "https://github.com/roowus/repo/pull/1"
        assert not _merge_head_present(work), "the delivery must abort the leftover merge"
        assert _rev(work, "HEAD") == coder_tip
        assert _git("status", "--porcelain", cwd=work).stdout == ""
        pushed = _git("show", f"refs/heads/{BRANCH}:a.txt", cwd=bare).stdout
        assert "<<<<<<<" not in pushed, pushed
        assert pushed.rstrip("\n") == "line one\ncoder's edit", pushed
        # the leftover merge is the same conflict, so the body says so honestly
        body = body_file.read_text()
        assert "`a.txt`" in body, body
        assert "git reported no paths" not in body
    finally:
        await store.aclose()


# ---------------------------------------------------------------------------
# the merge commit carries the DEPLOYMENT's configured identity
# ---------------------------------------------------------------------------


def _author_of(repo: Path, ref: str = "HEAD") -> str:
    return _git("log", "-1", "--format=%an <%ae>", ref, cwd=repo).stdout.strip()


def _rework_authors(repo: Path, ref: str = "HEAD") -> set[str]:
    """The identities of the commits REWORK made on this history (the fixture's
    own `r@r` seed commits are not rework's)."""
    lines = _git("log", "--format=%an <%ae>", ref, cwd=repo).stdout.splitlines()
    return {line.strip() for line in lines if line.strip().startswith("rework <")}


CONFIGURED_AUTHOR = ("rework", "rework@rework.rewis")


async def test_the_merge_commit_uses_the_configured_git_author(tmp_path: Path) -> None:
    """Requirement: the merge must not add a SECOND author to one branch's
    history. `REWORK_GIT_AUTHOR_EMAIL=rework@rework.rewis` used to leave the
    coder's commit as `rework <...rewis>` and the merge commit as
    `rework@rework.local`."""
    _, work, other = _movable_repo(tmp_path)
    _advance_main(other, {"other.txt": "another mission's work\n"})
    coder_tip = _rev(work, "HEAD")

    result = await merge_base_into_branch(work, BRANCH, "main", author=CONFIGURED_AUTHOR)

    assert result.merged is True, result
    assert _rev(work, "HEAD") != coder_tip, "a merge commit was made"
    assert _author_of(work) == "rework <rework@rework.rewis>"
    # ONE author on the whole branch: the hardcoded default must not appear
    assert "rework@rework.local" not in _git("log", "--format=%ae", cwd=work).stdout
    assert _rework_authors(work) == {"rework <rework@rework.rewis>"}


async def test_the_engine_threads_the_configured_author_to_the_merge_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: a deployment whose config sets the git author gets a merge
    commit with THAT author, the same one its coder commits carry."""
    dest, bare = _toyrepo_with_moving_main(tmp_path)
    fake_gh, _ = _fake_gh(tmp_path)
    monkeypatch.setenv("REWORK_GH_BIN", str(fake_gh))

    from rework.orchestrator.daemon import sqlite_checkpointer_factory

    store = await open_sqlite(str(tmp_path / "author.sqlite3"))
    try:
        objective = "write docs: add a line to README saying hello from rework"
        plan = json.loads(json.dumps(PLAN_FIX_PR).replace("$OBJECTIVE", objective))
        mission = await store.create_mission(
            Mission(id="mauthore2e01", objective=objective, repo_path=str(dest), plan=plan)
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
            sqlite_checkpointer_factory(str(tmp_path / "ck-auth.sqlite3")),
            tmp_path / "work",
            poll_interval_s=0.05,
            git_author=CONFIGURED_AUTHOR,
            provider=provider,
        )
        await asyncio.wait_for(asyncio.create_task(runner.run()), timeout=120)

        done = await store.get_mission(mission.id)
        assert done is not None and done.status == MissionStatus.DONE
        branch = next(
            str((r.artifact or {}).get("branch"))
            for r in await store.list_runs(mission.id)
            if r.role == "coder" and (r.artifact or {}).get("branch")
        )
        tip = _rev(bare, f"refs/heads/{branch}")
        assert "Merge origin/main" in _git("log", "-1", "--format=%s", tip, cwd=bare).stdout
        assert _author_of(bare, tip) == "rework <rework@rework.rewis>"
        # the coder's own commits sit right below: one author, no second identity
        assert "rework@rework.local" not in _git("log", "--format=%ae", tip, cwd=bare).stdout
        assert _rework_authors(bare, tip) == {"rework <rework@rework.rewis>"}
    finally:
        await store.aclose()


async def test_a_skip_for_a_dirty_worktree_is_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The code and docs agree on silence: a skip the owner cannot act on adds
    no INFO event and no PR-body note (the same as the no-origin case)."""
    bare, work, other = _movable_repo(tmp_path)
    _advance_main(other, {"other.txt": "another mission's work\n"})
    (work / "b.txt").write_text("uncommitted edit\n")
    fake_gh, body_file = _fake_gh(tmp_path)
    monkeypatch.setenv("REWORK_GH_BIN", str(fake_gh))
    body = "## What changed\n\nsomething\n"

    store = await open_sqlite(str(tmp_path / "quiet.sqlite3"))
    try:
        mission = await _done_mission(store, work, "mquitskip001")
        url = await deliver_pr(
            store,
            InProcessEventBus(store),
            mission.id,
            branch=BRANCH,
            worktree=str(work),
            title="rework: Deliver",
            body=body,
        )
        assert url == "https://github.com/roowus/repo/pull/1"
        assert body_file.read_text() == body, "a silent skip adds no body note"
        assert _merge_events(await _notes(store, mission.id)) == []
        assert (work / "b.txt").read_text() == "uncommitted edit\n", "the edit is left alone"
        assert _rev(bare, f"refs/heads/{BRANCH}") == _rev(work, "HEAD")
    finally:
        await store.aclose()


# ---------------------------------------------------------------------------
# a retried delivery never republishes a stale merge paragraph
# ---------------------------------------------------------------------------

_REAL_PUSH = delivery.push_branch


async def test_a_retried_delivery_drops_the_conflict_paragraph_from_attempt_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Attempt 1 conflicts AND then fails to push; attempt 2 merges cleanly.

    Attempt 1's body is persisted with its "the merge conflicts ... `a.txt`"
    paragraph, so a retry that re-used that persisted body would publish a
    conflict that no longer exists. The merge paragraph is recomputed, never
    re-read."""
    bare, work, other = _movable_repo(tmp_path)
    (work / "a.txt").write_text("line one\ncoder's edit\n")
    _commit(work, "coder edits a.txt")
    # another mission takes the same file while this one worked: attempt 1 CONFLICTS
    pre_main_sha = _rev(bare, "refs/heads/main")
    (other / "a.txt").write_text("line one\nanother mission's edit\n")
    _commit(other, "another mission edits a.txt")
    _git("push", "origin", "main", cwd=other)
    fake_gh, body_file = _fake_gh(tmp_path)
    monkeypatch.setenv("REWORK_GH_BIN", str(fake_gh))

    async def failing_push(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("git push rejected: the remote hung up")

    store = await open_sqlite(str(tmp_path / "stale.sqlite3"))
    try:
        mission = await _done_mission(store, work, "mstalebody01")
        monkeypatch.setattr(delivery, "push_branch", failing_push)
        first = await deliver_pr(
            store,
            InProcessEventBus(store),
            mission.id,
            branch=BRANCH,
            worktree=str(work),
            title="rework: Deliver",
            body="## What changed\n\nsomething\n",
        )
        assert first is None, "attempt 1 must fail at the push"
        pending = await store.get_mission(mission.id)
        pending_result = (pending.result if pending else None) or {}
        persisted = str(pending_result.get("pr_body") or "")
        assert pending_result.get("pr_pending") is True
        # attempt 1 did report its conflict to the owner (the INFO event) ...
        notes = [n for n in await _notes(store, mission.id) if "conflicting_paths" in n]
        assert notes and notes[0]["conflicting_paths"] == ["a.txt"], notes
        # ... but only the coder's own body is persisted: the merge paragraph is
        # recomputed on every attempt, never stored
        assert "Merge with main" not in persisted, persisted
        assert "a.txt" not in persisted, persisted
        assert persisted.strip() == "## What changed\n\nsomething"

        # the other mission's conflicting commit is dropped from main (its
        # branch was not ready) and main moves on with an UNRELATED file, so
        # attempt 2 merges CLEANLY and still produces a real merge commit. A
        # `git revert` here needs a commit identity the bare clone has not got,
        # so main is restored by force-pushing the pre-conflict sha instead.
        _git("reset", "--hard", pre_main_sha, cwd=other)
        _git("push", "-f", "origin", f"{pre_main_sha}:main", cwd=other)
        assert _rev(bare, "refs/heads/main") == pre_main_sha
        _advance_main(other, {"unrelated.txt": "landed while the PR was pending\n"})
        monkeypatch.setattr(delivery, "push_branch", _REAL_PUSH)

        ok, detail = await retry_pr(store, InProcessEventBus(store), mission.id)
        assert ok, detail

        body = body_file.read_text()
        assert "Merge with main" not in body, body
        assert "Conflicting paths" not in body, body
        assert "`a.txt`" not in body, "the retry must not claim a conflict that is gone"
        assert "## What changed" in body, "the coder's own body is still there"

        # ... and the branch really was merged with main before the push
        tip = _rev(bare, f"refs/heads/{BRANCH}")
        assert _is_ancestor(bare, _rev(bare, "refs/heads/main"), tip), (
            "the retried push contains main"
        )
        assert (
            "Merge origin/main into rework/test" in _git("log", "--oneline", tip, cwd=bare).stdout
        )
        final = await store.get_mission(mission.id)
        assert ((final.result if final else None) or {}).get("pr_status") == "opened"
    finally:
        await store.aclose()


async def test_a_persisted_body_with_an_old_merge_note_is_cleaned_on_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row persisted by an older rework already has the note baked into
    `pr_body`: the retry must strip it instead of republishing it."""
    bare, work, other = _movable_repo(tmp_path)
    _advance_main(other, {"other.txt": "another mission's work\n"})  # a clean merge
    fake_gh, body_file = _fake_gh(tmp_path)
    monkeypatch.setenv("REWORK_GH_BIN", str(fake_gh))
    stale = (
        "## What changed\n\nsomething\n\n## Merge with main\n\n"
        f"`main` was **not** merged into `{BRANCH}`: the merge conflicts, so "
        "this branch needs a human to resolve it before merging.\n\n"
        "Conflicting paths:\n\n- `a.txt`\n"
    )

    store = await open_sqlite(str(tmp_path / "oldnote.sqlite3"))
    try:
        mission = await _done_mission(
            store,
            work,
            "moldnote0001",
            pr_pending=True,
            pr_branch=BRANCH,
            pr_worktree=str(work),
            pr_title="rework: Deliver",
            pr_body=stale,
        )
        ok, detail = await retry_pr(store, InProcessEventBus(store), mission.id)
        assert ok, detail
        body = body_file.read_text()
        assert "Merge with main" not in body, body
        assert "a.txt" not in body, body
        assert body.startswith("## What changed"), body
        assert (
            "Merge origin/main into rework/test"
            in _git("log", "--oneline", f"refs/heads/{BRANCH}", cwd=bare).stdout
        )
    finally:
        await store.aclose()
