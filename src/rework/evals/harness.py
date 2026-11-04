"""Eval harness v1: task fixtures + graders + score history + `rework eval run`.

The hill-climb discipline (docs/design.md#evals): change ONE variable, run the
suite, compare scores over time. Grader ladder starts at rung 1 (mechanical:
tests pass, diff exists) because rung 2 (LLM-judge) needs a funded catalog and
rung 3 is the owner. Score history is event rows on a sentinel mission
(`eval:<task_id>`), so any machine sharing the database can read it and the
store stays the single source of truth (CLAUDE.md rule 3).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rework.kernel.state.records import (
    EventRecord,
    EventType,
    Mission,
)
from rework.kernel.state.store import StateStore
from rework.orchestrator.engine import MissionRunner
from rework.roles.registry import RoleRegistry

EVAL_MISSION_PREFIX = "eval:"

# the harness's own agent identity: eval commits must never be attributed to a
# deployment's rework identity, and retries must be attributable on the report
EVAL_GIT_AUTHOR = ("rework-eval", "rework-eval@rework.local")


@dataclass
class EvalTask:
    """One task fixture: a repo seed directory, an objective, a plan template,
    and a mechanical grader over the mission's runs."""

    id: str
    objective: str
    repo: Path  # a directory (copied per run); may be a git repo already
    plan: dict[str, Any]  # steps/gates template ($OBJECTIVE substituted)
    grader: str = "tests_pass"  # grader registry key


@dataclass
class EvalScore:
    task_id: str
    mission_id: str
    passed: bool
    grade: dict[str, Any] = field(default_factory=dict)
    cost_usd: float = 0.0
    elapsed_s: float = 0.0


def grade_tests_pass(runs_by_step: dict[str, Any], workroot: Path) -> dict[str, Any]:
    """Rung 1 grader: the mission reached DONE, the coder produced a diff, and
    pytest on the final worktree passes. Mechanical, no LLM."""
    from rework.nodes.report_sections import mechanical_verdict

    grade: dict[str, Any] = {}
    mission_done = runs_by_step.get("_mission_status") == "done"
    grade["mission_done"] = mission_done
    coder = runs_by_step.get("code")
    diff = (coder.artifact or {}).get("diff") if coder is not None else None
    grade["diff_present"] = bool(diff)
    audit = runs_by_step.get("audit")
    # a missing/failed audit must not read as approval (found live: both audit
    # attempts died in a 429 storm and the grade said audit=approve)
    audit_ran = audit is not None and audit.status.value == "completed"
    audit_artifact = audit.artifact if audit is not None else None
    grade["audit_verdict"] = mechanical_verdict(audit_artifact or {}) if audit_ran else "no_audit"
    grade["audit_completed"] = audit_ran

    # run the tests on the coder's final worktree, not trust its summary
    tests_pass = False
    test_output = ""
    if coder is not None and coder.artifact:
        worktree = coder.artifact.get("worktree")
        if worktree and Path(str(worktree)).is_dir():
            tests_pass, test_output = _run_pytest(Path(str(worktree)))
    grade["tests_pass"] = tests_pass
    grade["test_output_tail"] = test_output
    grade["passed"] = bool(mission_done and diff and tests_pass and audit_ran)
    grade["worktree"] = str((coder.artifact or {}).get("worktree") if coder is not None else "")
    del workroot  # grader signature stability for rung-2/3 (judge needs scratch space)
    return grade


def _run_pytest(worktree: Path, timeout_s: float = 120.0) -> tuple[bool, str]:
    """Sync on purpose: graders are CPU/blocking work, callers wrap in
    asyncio.to_thread (a nested asyncio.run would explode inside run_task).
    Uses sys.executable: the interpreter running rework has pytest (dev dep);
    a bare `python3` may not (found live: homebrew python has none)."""
    import subprocess
    import sys

    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q"],
            cwd=str(worktree),
            capture_output=True,
            timeout=timeout_s,
            text=True,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode == 0, output[-2000:]
    except subprocess.TimeoutExpired:
        return False, f"(timeout after {timeout_s:.0f}s)"


GRADERS = {"tests_pass": grade_tests_pass}


def builtin_tasks() -> list[EvalTask]:
    """Task fixtures that ship with rework. toyrepo/planted-bug is the seed."""
    examples = Path(__file__).resolve().parents[3] / "examples"
    return [
        EvalTask(
            id="toyrepo-planted-bug",
            objective="make add(2, 3) == 5 in the toy calculator",
            repo=examples / "toyrepo",
            plan={
                "steps": [
                    {
                        "step": "investigate",
                        "role": "investigator",
                        "model": "flash",
                        "context": {
                            "brief": "make add(2, 3) == 5 in the toy calculator",
                            "depth": "quick",
                            "requested_sections": ["summary", "findings", "suggested_fix"],
                        },
                    },
                    {
                        "step": "code",
                        "role": "coder",
                        "model": "flash",
                        "context": {"brief": "make add(2, 3) == 5 in the toy calculator"},
                        "context_from": {"investigation": {"step": "investigate"}},
                    },
                    {
                        "step": "audit",
                        "role": "auditor",
                        "model": "pro",
                        "context": {"brief": "make add(2, 3) == 5 in the toy calculator"},
                        "context_from": {
                            "diff": {"step": "code", "field": "diff"},
                            "worktree_path": {"step": "code", "field": "worktree"},
                            "investigation": {"step": "investigate"},
                        },
                        "on_request_changes": {"revise": "code", "max_rounds": 2},
                    },
                ],
                "gates": [],
            },
            grader="tests_pass",
        )
    ]


def _seed_repo(task: EvalTask, repo_dest: Path) -> None:
    """Fresh git repo copy per run: evals must never mutate the fixture.
    Sync (blocking) on purpose; run_task calls it via asyncio.to_thread."""
    import shutil
    import subprocess

    repo_dest.mkdir(parents=True, exist_ok=True)
    for f in task.repo.iterdir():
        if f.name not in {".git", "__pycache__"}:
            if f.is_dir():
                shutil.copytree(f, repo_dest / f.name)
            else:
                shutil.copy(f, repo_dest / f.name)
    git_env = ["-c", f"user.name={EVAL_GIT_AUTHOR[0]}", "-c", f"user.email={EVAL_GIT_AUTHOR[1]}"]
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_dest, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=repo_dest, check=True, capture_output=True)
    subprocess.run(
        ["git", *git_env, "commit", "-m", "eval seed"],
        cwd=repo_dest,
        check=True,
        capture_output=True,
    )


async def run_task(
    task: EvalTask,
    store: StateStore,
    registry: RoleRegistry,
    catalog: Any,
    make_checkpointer: Any,
    workroot: Path,
    provider: Any = None,
) -> EvalScore:
    """Run one task in-process (no daemon) and grade it. The eval mission id is
    deterministic per (task, attempt-batch) so re-runs are visible in history."""
    mission_id = f"{EVAL_MISSION_PREFIX}{task.id}-{new_stamp()}"
    # seed the fixture repo as a SIBLING of the workroot, not inside it: role
    # worktrees live under workroot, and (the run_command jail being advisory,
    # see nodes/tools.py) a role that cd's to an absolute path must not be able
    # to reach the pristine fixture or a previous run's worktree from there.
    repo_dest = workroot.parent / "eval-fixtures" / mission_id.replace(":", "_") / "repo"
    await asyncio.to_thread(_seed_repo, task, repo_dest)

    mission = await store.create_mission(
        Mission(
            id=mission_id,
            objective=f"[eval] {task.objective}",
            repo_path=str(repo_dest),
            plan=task.plan,
        )
    )
    from rework.kernel.events.bus import InProcessEventBus

    runner = MissionRunner(
        mission.id,
        store,
        InProcessEventBus(store),
        registry,
        catalog,
        make_checkpointer,
        workroot / mission_id.replace(":", "_"),
        poll_interval_s=0.25,
        git_author=EVAL_GIT_AUTHOR,
        provider=provider,
    )
    started = time.time()
    await runner.run()
    elapsed = time.time() - started

    runs = await store.list_runs(mission.id)
    fresh_mission = await store.get_mission(mission.id)
    by_step: dict[str, Any] = {r.step: r for r in runs}
    by_step["_mission_status"] = fresh_mission.status.value if fresh_mission is not None else "?"
    # graders may run pytest (blocking): keep it off the event loop
    grade = await asyncio.to_thread(GRADERS[task.grader], by_step, workroot)
    spend = await store.mission_spend(mission.id)
    score = EvalScore(
        task_id=task.id,
        mission_id=mission_id,
        passed=bool(grade.get("passed")),
        grade=grade,
        cost_usd=spend.spent_usd,
        elapsed_s=elapsed,
    )
    await _record_score(store, score)
    return score


def new_stamp() -> str:
    import uuid

    return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]


async def _record_score(store: StateStore, score: EvalScore) -> None:
    """Score history: one event row per task per run, on the sentinel mission.
    `rework eval history <task>` reads these."""
    sentinel = f"{EVAL_MISSION_PREFIX}history"
    if await store.get_mission(sentinel) is None:
        await store.create_mission(Mission(id=sentinel, objective="eval score history"))
    await store.append_event(
        EventRecord(
            mission_id=sentinel,
            type=EventType.INFO,
            payload={
                "kind": "eval_score",
                "task": score.task_id,
                "mission": score.mission_id,
                "passed": score.passed,
                "grade": score.grade,
                "cost_usd": score.cost_usd,
                "elapsed_s": score.elapsed_s,
            },
        )
    )


async def score_history(store: StateStore, task_id: str | None = None) -> list[dict[str, Any]]:
    """The score history for one task (or all), oldest first."""
    sentinel = f"{EVAL_MISSION_PREFIX}history"
    if await store.get_mission(sentinel) is None:
        return []
    events = await store.events_since(0, mission_id=sentinel, limit=10000)
    scores = [
        e.payload
        for e in events
        if e.payload.get("kind") == "eval_score"
        and (task_id is None or e.payload.get("task") == task_id)
    ]
    return scores
