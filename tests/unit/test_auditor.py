"""Auditor tests: mechanical verdict mapping, blind-round context assembly,
and a scripted audit that reads the coder's handoff diff and reports."""

from __future__ import annotations

import json
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from rework.kernel.budget.ledger import Budget
from rework.kernel.events.bus import InProcessEventBus
from rework.kernel.gates import GateManager
from rework.kernel.llm.adapters.fake import FakeProvider
from rework.kernel.llm.catalog import ModelCatalog, ModelEntry
from rework.kernel.llm.types import ChatResult, ToolCall, Usage
from rework.kernel.state.records import Mission, Run, run_id
from rework.kernel.state.sqlite import SqliteStore, open_sqlite
from rework.nodes.report_sections import blocking_findings, mechanical_verdict
from rework.roles.base import RoleBuildCtx
from rework.roles.registry import autodiscover

# -- the mechanical verdict mapping --------------------------------------------


def test_verdict_mapping() -> None:
    critical = {"findings": [{"severity": "critical"}, {"severity": "low"}]}
    high = {"findings": [{"severity": "low"}, {"severity": "high"}]}
    clean = {"findings": [{"severity": "low"}]}
    empty = {"findings": []}
    blockers = {"findings": [], "blockers": [{"question": "is prod affected?"}]}
    assert mechanical_verdict(critical) == "request changes"
    assert mechanical_verdict(high) == "approve with fixes"
    assert mechanical_verdict(clean) == "approve"
    assert mechanical_verdict(empty) == "approve"
    # fail-closed: nothing to audit is NOT an approval
    assert mechanical_verdict(None) == "not audited"
    assert mechanical_verdict({}) == "not audited"
    # open blockers count, and they were ignored before this change
    assert mechanical_verdict(blockers) == "request changes"


def test_a_coverage_finding_counts_toward_the_verdict() -> None:
    """A vacuous test is a coverage entry, not a `findings` row: it must still block."""
    vacuous = {"findings": [], "coverage": [{"kind": "vacuous_test", "severity": "high"}]}
    assert len(blocking_findings(vacuous)) == 1
    assert mechanical_verdict(vacuous) == "approve with fixes"
    assert mechanical_verdict(vacuous) != mechanical_verdict({"findings": []})


# -- scripted audit ----------------------------------------------------------------


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=r@r", "-c", "user.name=r", "commit", "-m", "init"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return repo


@pytest.fixture
async def audit_ctx(tmp_path: Path) -> AsyncIterator[RoleBuildCtx]:
    repo = _init_repo(tmp_path / "mission")
    run_dir = tmp_path / "runs" / "run1"
    run_dir.mkdir(parents=True)
    store: SqliteStore = await open_sqlite(str(tmp_path / "audit.sqlite3"))
    try:
        bus = InProcessEventBus(store)
        mission = await store.create_mission(
            Mission(id="ma", objective="fix add()", repo_path=str(repo))
        )
        run = await store.create_run(
            Run(
                id=run_id("ma", "audit", "auditor", 1),
                mission_id="ma",
                role="auditor",
                step="audit",
                context={
                    "brief": "make add() actually add",
                    "diff": "--- a/calc.py\n+++ b/calc.py\n-    return a - b\n+    return a + b\n",
                    "worktree_path": str(repo),  # orchestrator policy: audit in place here
                },
            )
        )
        catalog = ModelCatalog(
            [
                ModelEntry(id="pro", provider="fake", model="fake-pro"),
                ModelEntry(id="flash", provider="fake", model="fake-flash"),
            ]
        )
        yield RoleBuildCtx(
            run=run,
            mission=mission,
            catalog=catalog,
            provider=FakeProvider(),
            store=store,
            bus=bus,
            gates=GateManager(store, bus),
            budget=Budget(max_usd=5.0),
            workdir=run_dir,
        )
    finally:
        await store.aclose()


async def test_auditor_reads_context_and_reports(audit_ctx: RoleBuildCtx) -> None:
    fake = audit_ctx.provider
    assert isinstance(fake, FakeProvider)
    fake.push(
        ChatResult(
            text="reading the file",
            tool_calls=[ToolCall(id="t1", name="read_file", arguments={"path": "calc.py"})],
            usage=Usage(input_tokens=20, output_tokens=10),
            stop_reason="tool_calls",
        )
    )
    fake.push(
        ChatResult(
            text="the change is correct and tested; no issues.",
            usage=Usage(input_tokens=15, output_tokens=8),
            stop_reason="end_turn",
        )
    )
    fake.push(
        ChatResult(
            text=json.dumps(
                {
                    "summary": "Fix is correct: add() now adds. No findings.",
                    "findings": [],
                    "standing_assessments": {
                        "proportionality": "proportionate (no accessory)",
                        "complexity": "trivial, appropriate",
                        "priority": "appropriate",
                        "coverage": "adequate for the one-line change",
                        "business_logic_risk": "none",
                    },
                    "coverage": [],
                    "warnings": [],
                    "blockers": [],
                }
            ),
            usage=Usage(input_tokens=40, output_tokens=50),
        )
    )

    spec = autodiscover().get("auditor")
    graph = spec.build_graph(audit_ctx)
    graph.checkpointer = InMemorySaver()
    config: dict[str, Any] = {"configurable": {"thread_id": "t-audit"}}
    final = await graph.ainvoke({"objective": "audit the fix"}, config=config)

    artifact = final["artifact"]
    assert artifact["summary"].startswith("Fix is correct")
    assert artifact["findings"] == []
    assert artifact["standing_assessments"]["business_logic_risk"] == "none"
    assert mechanical_verdict(artifact) == "approve"

    # round-1 context: brief + diff present, the tool read the file
    first = fake.requests[0]
    assert "Mission brief" in first.messages[0]["content"]
    assert "```diff" in first.messages[0]["content"]
    # the auditor reads the workspace; it must NOT gain outbound web access
    # (investigator and coder have web_search/fetch_url, the auditor does not)
    offered = {s.name for s in first.tools}
    assert "read_file" in offered
    assert offered.isdisjoint({"web_search", "fetch_url"})
    assert "GOAL:" in first.system
    tool_messages = [m for m in final["messages"] if m.get("role") == "tool"]
    assert any("def add" in m["content"] for m in tool_messages)


# -- accepted_findings: soft matching + the one shared renderer ----------------


def test_covers_needs_a_real_title_not_a_fragment() -> None:
    from rework.orchestrator.agent import _covers

    open_item = "No test for the zero-results path"
    # an exact title, or a long enough excerpt of it, covers
    assert _covers(open_item.lower(), open_item)
    assert _covers("the zero-results path", open_item)
    assert _covers("no test for the zero-results", open_item)
    # a short, vague entry must NOT cover it (nor an unrelated one)
    assert not _covers("tests", open_item)
    assert not _covers("the caller may crash", open_item)
    assert not _covers("", open_item)


def test_accepted_findings_section_is_shared_by_pr_body_and_report() -> None:
    """One renderer: the PR body and the markdown report cannot drift."""
    from rework.nodes.report_sections import accepted_findings_section

    accepted = [
        {"finding": "No test for the zero-results path", "reason": "follow-up filed"},
        {"finding": "Zero results may crash the caller", "reason": ""},
        {"finding": "", "reason": "dropped: no finding named"},
    ]
    section = accepted_findings_section(accepted)
    assert section == [
        "### Accepted findings (shipped knowingly)",
        "",
        "- **No test for the zero-results path** — follow-up filed",
        "- **Zero results may crash the caller** — no reason recorded",
    ], "entries without a reason still render; entries without a finding do not"
    assert accepted_findings_section(None) == []
    assert accepted_findings_section([]) == []
