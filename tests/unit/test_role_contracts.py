"""Consumers find a code-writing handoff by its artifact kind, not by role name.

ADR 0015 added the harness role with the SAME `coder_handoff` artifact as the
coder, but four places still looked for `role == "coder"` (audit 2026-11-25,
3.2): an auditor spawned after a harness run got no diff, worktree, tests or
base sha; `retry-pr` could not find a harness branch; the PR path and the TUI
diff view missed it too.
"""

from __future__ import annotations

from typing import Any

from rework.kernel.state.records import Mission, MissionStatus, Run, RunStatus, run_id
from rework.kernel.state.sqlite import open_sqlite
from rework.orchestrator.rails import CODER_HANDOFF, is_code_handoff, latest_handoff

HANDOFF = {
    "kind": CODER_HANDOFF,
    "branch": "rework/m-1-code-harness-1",
    "worktree": "/tmp/nowhere",
    "diff": "--- a\n+++ b\n",
    "tests": {"added": ["t"], "result": "green"},
    "base_sha": "abc123",
    "changed_files": ["a.py"],
    "commit": "def456",
}


def _run(role: str, attempt: int = 1, artifact: dict[str, Any] | None = None) -> Run:
    return Run(
        id=run_id("m-1", "code", role, attempt),
        mission_id="m-1",
        role=role,
        step="code",
        attempt=attempt,
        status=RunStatus.COMPLETED,
        artifact=artifact,
    )


def test_a_harness_run_is_a_code_handoff() -> None:
    assert is_code_handoff(_run("harness", artifact=HANDOFF))
    assert not is_code_handoff(_run("investigator", artifact={"kind": "report"}))
    picked = latest_handoff(
        [_run("coder", 1, {**HANDOFF, "branch": "old"}), _run("harness", 2, HANDOFF)]
    )
    assert picked is not None and picked.role == "harness"


async def test_an_auditor_after_a_harness_run_gets_the_diff(tmp_path: Any) -> None:
    from rework.orchestrator.agent import MissionOrchestrator

    store = await open_sqlite(str(tmp_path / "s.sqlite3"))
    try:
        mission = await store.create_mission(
            Mission(
                id="m-1", objective="x", status=MissionStatus.RUNNING, plan={"orchestrator": {}}
            )
        )
        runs = [_run("harness", artifact=HANDOFF)]
        orch = MissionOrchestrator.__new__(MissionOrchestrator)
        orch.mission = mission
        context = await orch._wire("auditor", runs)
        assert context["diff"] == HANDOFF["diff"]
        assert context["worktree_path"] == HANDOFF["worktree"]
        assert context["coder_tests"] == HANDOFF["tests"]
        assert context["base_sha"] == HANDOFF["base_sha"]
        assert context["changed_files"] == HANDOFF["changed_files"]
    finally:
        await store.aclose()


async def test_retry_pr_finds_a_harness_branch(tmp_path: Any, monkeypatch: Any) -> None:
    from rework.orchestrator import delivery

    store = await open_sqlite(str(tmp_path / "s.sqlite3"))
    try:
        await store.create_mission(
            Mission(
                id="m-1",
                objective="x",
                status=MissionStatus.DONE,
                plan={"publish": "pr"},
                result={"pr_pending": True},
            )
        )
        run = _run("harness", artifact=HANDOFF)
        await store.create_run(run)
        await store.update_run(run.id, artifact=HANDOFF)
        ok, message = await delivery.retry_pr(store, None, "m-1")
        # the worktree does not exist here, so it cannot deliver; what matters
        # is that it FOUND the harness branch (it used to say "no recorded branch")
        assert ok is False
        assert HANDOFF["branch"] in message
        assert "no recorded branch" not in message
    finally:
        await store.aclose()
