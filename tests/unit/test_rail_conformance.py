"""Every finish rail fires on both drivers (ADR 0012).

rework has two drivers: the LLM orchestrator and the FROZEN template runner
(evals, `--mechanical`). Before ADR 0012 each owned some rails and not others, so
a `--mechanical` fix-pr mission could ship without an audit taken after the last
change. That asymmetry was invisible because both paths look equivalent.

The rails now live in `rework.orchestrator.rails` as pure functions over store
rows, so this table tests the SAME implementation twice: once as the orchestrator
calls it and once as the template runner calls it. If a future rail is added to
one driver's call site and not the other, this test cannot see it (both call
`rails.audit_problem`), so the behavioural check below is backed by
`test_the_template_runner_calls_the_shared_audit_rail`, which asserts the call
site directly.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from rework.kernel.state.records import Mission, Run, RunStatus
from rework.nodes.testing import handoff_tests_gap
from rework.orchestrator.rails import (
    audit_problem,
    findings_problem,
    summary_is_substantive,
    summary_problem,
)


def _run(role: str, status: RunStatus, *, at: float, artifact: dict | None = None) -> Run:
    return Run(
        id=f"m:r:{role}:{at}",
        mission_id="m",
        role=role,
        step=role,
        status=status,
        created_at=at,
        updated_at=at,
        artifact=artifact,
    )


def _clean_audit(at: float) -> Run:
    return _run(
        "auditor",
        RunStatus.COMPLETED,
        at=at,
        artifact={"verdict": "approve", "summary": "clean", "findings": [], "blockers": []},
    )


def _coder(at: float, tests: dict | None) -> Run:
    return _run(
        "coder",
        RunStatus.COMPLETED,
        at=at,
        artifact={
            "kind": "coder_handoff",
            "branch": "rework/x",
            "commit": "abc",
            "diff": "diff",
            "changed_files": ["a.py"],
            **({"tests": tests} if tests is not None else {}),
        },
    )


GOOD_TESTS = {
    "added": ["tests/test_a.py"],
    "changed": [],
    "red_green_verified": True,
    "commands": ["pytest -q"],
    "result": "red then green",
    "not_testable_reason": "",
}


# -- the rails, as each driver exercises them -------------------------------
#
# Each entry is (rail name, a scenario that MUST refuse, a scenario that MUST
# pass). The scenarios are store rows, which is all either driver reads.

RAILS: list[tuple[str, dict[str, Any], dict[str, Any]]] = [
    (
        "audit_required_for_done",
        {"runs": [_coder(1.0, GOOD_TESTS)], "plan": {}},
        {"runs": [_coder(1.0, GOOD_TESTS), _clean_audit(2.0)], "plan": {}},
    ),
    (
        "fresh_audit_after_coder",
        {
            "runs": [_coder(1.0, GOOD_TESTS), _clean_audit(2.0), _coder(3.0, GOOD_TESTS)],
            "plan": {},
        },
        {"runs": [_coder(1.0, GOOD_TESTS), _clean_audit(2.0)], "plan": {}},
    ),
    (
        "coder_tests_block_required",
        {
            "runs": [_coder(1.0, {"red_green_verified": True, "commands": []}), _clean_audit(2.0)],
            "plan": {},
        },
        {"runs": [_coder(1.0, GOOD_TESTS), _clean_audit(2.0)], "plan": {}},
    ),
    (
        "survey_only_audit_playbook_exempt",
        # the scenario this rail ALLOWS (no coder, a completed surveyor, an
        # audit playbook); the parametrized test inverts its expectations
        {
            "runs": [_run("surveyor", RunStatus.COMPLETED, at=1.0, artifact={"summary": "map"})],
            "plan": {"playbook": "audit"},
        },
        {
            "runs": [_run("surveyor", RunStatus.COMPLETED, at=1.0, artifact={"summary": "map"})],
            "plan": {"playbook": "fix"},
        },
    ),
]


def _audit_scenario(scenario: dict[str, Any]) -> str | None:
    return audit_problem(scenario["runs"], scenario["plan"])


DRIVERS = ("orchestrated", "template")


@pytest.mark.parametrize("rail,refuse,allow", RAILS, ids=[r[0] for r in RAILS])
@pytest.mark.parametrize("driver", DRIVERS)
def test_each_rail_refuses_and_allows_on_both_drivers(
    driver: str, rail: str, refuse: dict[str, Any], allow: dict[str, Any]
) -> None:
    """The rule is one function; the driver only decides what to do with the
    result (a ToolError for the model, a failed synthesis for the runner).
    `survey_only_audit_playbook_exempt` is inverted on purpose: its "refuse"
    scenario is the one the rail ALLOWS, so it is checked by the pass/fail
    expectations below."""
    refused = _audit_scenario(refuse)
    allowed = _audit_scenario(allow)
    if rail == "survey_only_audit_playbook_exempt":
        assert allowed is not None, "a fix plan with no audit is refused"
        assert refused is None, "a survey-only audit playbook is exempt"
        return
    assert refused is not None, f"{rail} must refuse under {driver}"
    assert allowed is None, f"{rail} must allow under {driver}"


def test_the_findings_rail_is_the_same_function_both_sides() -> None:
    audit = _run(
        "auditor",
        RunStatus.COMPLETED,
        at=2.0,
        artifact={
            "verdict": "approve with fixes",
            "findings": [{"title": "the zero-results path is untested", "severity": "medium"}],
            "blockers": [],
        },
    )
    runs = [_coder(1.0, GOOD_TESTS), audit]

    assert findings_problem(runs, []) is not None, "an uncovered finding is refused"
    covered = [{"finding": "the zero-results path is untested", "reason": "out of scope"}]
    assert findings_problem(runs, covered) is None


def test_the_summary_rail_is_the_same_function_both_sides() -> None:
    assert summary_problem("done") is not None
    assert summary_problem("FINISHED ALREADY.") is not None
    assert summary_problem("add now returns a + b in calc.py so add(2, 3) is 5") is None
    assert summary_is_substantive("x" * 25)


def test_the_template_runner_calls_the_shared_audit_rail() -> None:
    """The behavioural table cannot catch a driver that forgets to call the
    rail, so this pins the call site: the frozen runner checks `audit_problem`
    immediately before its final gates."""
    import inspect

    from rework.orchestrator.engine import MissionRunner

    source = inspect.getsource(MissionRunner._run_template_once)
    assert "audit_problem(" in source, "the template runner must call the shared rail"
    assert source.index("audit_problem(") < source.index("_final_gates("), (
        "the rail must run BEFORE the final gates, or a bad mission reaches the ship gate"
    )


def test_the_orchestrator_calls_the_same_rail() -> None:
    import inspect

    from rework.orchestrator.agent import MissionOrchestrator

    source = inspect.getsource(MissionOrchestrator._enforce_audit_fresh)
    assert "_audit_problem(" in source or "audit_problem(" in source


def test_handoff_tests_gap_is_shared_not_reimplemented() -> None:
    """The tests block rule lives in nodes/testing.py and is reached from rails.
    A second copy would drift, which is the whole failure mode this ADR is
    about."""
    import inspect

    import rework.orchestrator.rails as rails

    assert "handoff_tests_gap" in inspect.getsource(rails)
    # a claimed red-green with no commands is the canonical gap
    gap = handoff_tests_gap(_coder(1.0, {"red_green_verified": True, "commands": []}).artifact)
    assert gap is not None


async def test_a_template_mission_with_a_live_coder_cannot_finish_unaudited(store) -> None:
    """The end-to-end shape: the frozen runner refuses rather than shipping."""
    mission = Mission(
        id="m-template",
        objective="x",
        plan={"kind": "fix", "steps": [{"step": "code", "role": "coder"}]},
    )
    await store.create_mission(mission)
    await store.create_run(_coder(1.0, GOOD_TESTS))

    runs = await store.list_runs(mission.id)
    problem = audit_problem(runs, mission.plan)
    assert problem is not None
    assert "no auditor has completed" in problem


def test_the_plan_kind_guard_accepts_what_the_mission_creator_stamps() -> None:
    """`create_mission` stamps `kind`, so a real template mission passes the
    engine's driver guard. A caller-built plan carries its own steps instead."""
    from rework.missions import PLAN_FIX, TEMPLATE_KINDS

    assert "fix" in TEMPLATE_KINDS
    # a template plan is a JSON-able spec with steps; the engine treats one
    # with steps as runnable regardless of kind
    spec = json.loads(json.dumps(PLAN_FIX))
    assert spec["steps"]


# -- the freshness rule compares COMMITS (audit 2026-11-25, 3.3) -------------


def _audit_of(sha: str, at: float) -> Run:
    run = _clean_audit(at)
    return run.model_copy(update={"artifact": {**(run.artifact or {}), "reviewed_sha": sha}})


def test_a_later_write_to_an_old_coder_row_is_not_an_unaudited_change() -> None:
    """The clock rule counted ANY later write to a coder row (a status flip, a
    model_ref update on resume) as new code. The audit reviewed the commit the
    coder left, so it is fresh."""
    coder = _coder(1.0, GOOD_TESTS).model_copy(update={"updated_at": 9.0})
    assert audit_problem([coder, _audit_of("abc", 2.0)], {}) is None


def test_a_new_commit_after_the_audit_is_unaudited() -> None:
    newer = _coder(3.0, GOOD_TESTS)
    newer = newer.model_copy(
        update={"id": "m:r:coder:3", "artifact": {**(newer.artifact or {}), "commit": "def"}}
    )
    problem = audit_problem([_coder(1.0, GOOD_TESTS), _audit_of("abc", 2.0), newer], {})
    assert problem is not None and "def" in problem and "abc" in problem


def test_a_coder_still_running_after_the_audit_is_unaudited() -> None:
    live = _run("coder", RunStatus.RUNNING, at=3.0)
    problem = audit_problem([_coder(1.0, GOOD_TESTS), _audit_of("abc", 2.0), live], {})
    assert problem is not None and "still running" in problem


def test_an_audit_without_a_recorded_commit_keeps_the_clock_rule() -> None:
    """Rows from before reviewed_sha existed."""
    later = _coder(3.0, GOOD_TESTS)
    assert audit_problem([_coder(1.0, GOOD_TESTS), _clean_audit(2.0), later], {}) is not None
