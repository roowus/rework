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
from rework.nodes.report_sections import mechanical_verdict
from rework.roles.base import RoleBuildCtx
from rework.roles.registry import autodiscover

# -- the mechanical verdict mapping --------------------------------------------


def test_verdict_mapping() -> None:
    critical = {"findings": [{"severity": "critical"}, {"severity": "low"}]}
    high = {"findings": [{"severity": "low"}, {"severity": "high"}]}
    clean = {"findings": [{"severity": "low"}]}
    empty = {"findings": []}
    assert mechanical_verdict(critical) == "request changes"
    assert mechanical_verdict(high) == "approve with fixes"
    assert mechanical_verdict(clean) == "approve"
    assert mechanical_verdict(empty) == "approve"


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
    assert "GOAL:" in first.system
    tool_messages = [m for m in final["messages"] if m.get("role") == "tool"]
    assert any("def add" in m["content"] for m in tool_messages)
