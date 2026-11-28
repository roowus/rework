"""rails.deliverable: ONE answer to "what did this mission deliver".

Found live (m-d69d16642add on rewvis): fix -> audit (request changes) ->
revision -> audit left two coder branches, and the report listed both with
nothing saying which one was the result."""

from __future__ import annotations

from typing import Any

from rework.kernel.state.records import Mission, MissionStatus, Run, RunStatus
from rework.orchestrator.rails import deliverable


def _run(rid: str, role: str, t: float, status: RunStatus = RunStatus.COMPLETED, **art: Any) -> Run:
    step = rid.split(":")[1]
    return Run(
        id=rid,
        mission_id="m",
        role=role,
        step=step,
        status=status,
        artifact=art or None,
        created_at=t,
        updated_at=t,
    )


def _coder(rid: str, t: float, commit: str, **kw: Any) -> Run:
    return _run(
        rid,
        "coder",
        t,
        kind="coder_handoff",
        branch=f"rework/{rid.replace(':', '-')}",
        commit=commit,
        worktree=f"/w/{rid}",
        **kw,
    )


def test_the_revision_is_the_deliverable_and_the_first_fix_is_superseded() -> None:
    runs = [
        _run("m:investigate:investigator:1", "investigator", 1, summary="x"),
        _coder("m:code:coder:1", 2, "324a7b9f72e9"),
        _run("m:audit:auditor:1", "auditor", 3, reviewed_sha="324a7b9f72e9"),
        _coder("m:revise:coder:1", 4, "f6eb0aec9540"),
        _run("m:audit2:auditor:1", "auditor", 5, reviewed_sha="f6eb0ae"),
    ]
    out = deliverable(runs)
    assert out is not None
    assert out["run"] == "m:revise:coder:1"
    assert out["branch"] == "rework/m-revise-coder-1"
    assert out["commit"] == "f6eb0aec9540"
    assert out["audited"] is True, "a short reviewed sha matches the full commit"
    assert out["audit_run"] == "m:audit2:auditor:1"
    assert out["superseded"] == ["m:code:coder:1"]


def test_a_revision_the_last_audit_did_not_see_is_flagged() -> None:
    runs = [
        _coder("m:code:coder:1", 1, "aaa111"),
        _run("m:audit:auditor:1", "auditor", 2, reviewed_sha="aaa111"),
        _coder("m:revise:coder:1", 3, "bbb222"),
    ]
    out = deliverable(runs)
    assert out is not None and out["run"] == "m:revise:coder:1"
    assert out["audited"] is False


def test_failed_and_branchless_runs_are_not_deliverables() -> None:
    runs = [
        _coder("m:code:coder:1", 1, "aaa111"),
        _run("m:revise:coder:1", "coder", 2, RunStatus.FAILED, kind="coder_handoff", branch="b"),
        _run("m:draft:writer:1", "writer", 3, summary="a report, no code"),
    ]
    out = deliverable(runs)
    assert out is not None and out["run"] == "m:code:coder:1"
    assert out["audited"] is False and out["audit_run"] is None
    assert deliverable([_run("m:inv:investigator:1", "investigator", 1, summary="x")]) is None
    assert deliverable([]) is None


def test_a_harness_handoff_counts_like_a_coder() -> None:
    runs = [
        _run(
            "m:code:harness:1",
            "harness",
            1,
            kind="coder_handoff",
            branch="rework/h",
            commit="ccc333",
        )
    ]
    out = deliverable(runs)
    assert out is not None and out["run"] == "m:code:harness:1"


def test_the_report_leads_with_the_deliverable_and_marks_the_superseded_fix() -> None:
    from rework.cli import _render_md
    from rework.kernel.state.records import BudgetState

    runs = [
        _coder("m:code:coder:1", 2, "324a7b9f72e9", summary="first", diff="-a\n+b"),
        _run("m:audit:auditor:1", "auditor", 3, reviewed_sha="324a7b9f72e9"),
        _coder("m:revise:coder:1", 4, "f6eb0aec9540", summary="second", diff="-a\n+c"),
        _run("m:audit2:auditor:1", "auditor", 5, reviewed_sha="f6eb0aec9540"),
    ]
    delivered = deliverable(runs)
    mission = Mission(
        id="m",
        objective="fix add",
        status=MissionStatus.DONE,
        result={"deliverable": delivered, "outcome": "done"},
    )
    md = _render_md([(r, dict(r.artifact or {})) for r in runs], BudgetState(), mission)
    head = md.split("## code (coder)")[0]
    assert "## Delivered" in head
    assert "- branch: `rework/m-revise-coder-1`" in head
    assert "- audit: reviewed by the final audit" in head
    assert "- on the daemon host: `/w/m:revise:coder:1`" in head
    sections = md.split("\n## ")
    first = next(x for x in sections if x.startswith("code (coder)"))
    second = next(x for x in sections if x.startswith("revise (coder)"))
    assert "_superseded: a later revision is what the mission delivered_" in first
    assert "_this is what the mission delivered_" in second
    assert "superseded" not in second


def test_a_mission_finished_before_the_record_still_reports_its_deliverable() -> None:
    from rework.cli import _render_md
    from rework.kernel.state.records import BudgetState

    runs = [
        _coder("m:code:coder:1", 2, "aaa111", summary="first", diff=""),
        _coder("m:revise:coder:1", 4, "bbb222", summary="second", diff=""),
    ]
    old = Mission(id="m", objective="x", status=MissionStatus.DONE, result={"outcome": "done"})
    md = _render_md([(r, dict(r.artifact or {})) for r in runs], BudgetState(), old)
    assert "- branch: `rework/m-revise-coder-1`" in md.split("\n## code")[0]
    assert "- audit: NOT the commit the final audit reviewed" in md
