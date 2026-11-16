"""Surveyor role: genuinely read-only (enforced by the executor wrapper), a
typed cited artifact, a plan that can never publish, and a report that renders
the whole map."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from rework.cli import _render_md
from rework.kernel.budget.ledger import Budget
from rework.kernel.events.bus import InProcessEventBus
from rework.kernel.gates import GateManager
from rework.kernel.llm.adapters.fake import FakeProvider
from rework.kernel.llm.types import ChatResult, ToolCall, Usage
from rework.kernel.state.records import Mission, MissionStatus, Run, RunStatus, run_id
from rework.kernel.state.sqlite import SqliteStore, open_sqlite
from rework.missions import (
    PLAN_AUDIT,
    PLANS,
    PLAYBOOK_GATES,
    PLAYBOOK_PUBLISH,
    orchestrated_plan,
)
from rework.nodes.readonly import READ_ONLY_TOOL_NAMES, ReadOnlyExecutor, validate_command
from rework.nodes.report_sections import ASSEMBLY_SURVEYOR, build_report_schema, resolve_sections
from rework.nodes.tools import ComplaintAwareExecutor, LocalExecutor
from rework.orchestrator.agent import DEFAULT_TURNS, PLAYBOOKS
from rework.roles.base import RoleBuildCtx
from rework.roles.builtin.surveyor import MAX_RISKS_AND_GAPS, finalize_survey
from rework.roles.registry import autodiscover

WRITE_TOOLS = {"write_file", "edit_file", "apply_patch", "git_commit", "open_pr", "git_push"}

ARTIFACT: dict[str, Any] = {
    "title": "tiny app",
    "summary": "A one-file greeter.",
    "layout": [{"path": "app.py", "purpose": "the greeter"}],
    "entry_points": [{"name": "greet", "where": "app.py:1-2", "purpose": "say hello"}],
    "dependencies": [],
    "config": [],
    "tests": {"framework": "none", "command": "", "where": "", "notes": "no tests"},
    "ci": {"system": "none", "where": "", "checks": []},
    "risks": [
        {
            "severity": "high",
            "area": "input handling",
            "detail": "name.upper() assumes a str",
            "where": "app.py:2",
        }
    ],
    "gaps": ["no tests were found"],
    "next_steps": ["add a test for greet"],
    "report": "# tiny app\n\nOne-file greeter; risk at app.py:2.",
}


@pytest.fixture
async def sv_ctx(tmp_path: Path) -> AsyncIterator[RoleBuildCtx]:
    from conftest import make_git_repo, role_catalog

    repo = make_git_repo(tmp_path)
    (repo / "app.py").write_text("def greet(name):\n    return f'hello {name.upper()}'\n")
    store: SqliteStore = await open_sqlite(str(tmp_path / "sv.sqlite3"))
    try:
        bus = InProcessEventBus(store)
        mission = await store.create_mission(
            Mission(id="mi", objective="map this repo", repo_path=str(repo))
        )
        run = await store.create_run(
            Run(
                id=run_id("mi", "survey", "surveyor", 1),
                mission_id="mi",
                role="surveyor",
                step="survey",
                context={"brief": "map this repo"},
            )
        )
        yield RoleBuildCtx(
            run=run,
            mission=mission,
            catalog=role_catalog(),
            provider=FakeProvider(),
            store=store,
            bus=bus,
            gates=GateManager(store, bus),
            budget=Budget(max_usd=5.0),
            workdir=tmp_path,
        )
    finally:
        await store.aclose()


def _push(fake: FakeProvider, result: ChatResult) -> None:
    fake.push(result)


async def test_surveyor_scripted_run_yields_cited_artifact(sv_ctx: RoleBuildCtx) -> None:
    fake = sv_ctx.provider
    assert isinstance(fake, FakeProvider)
    _push(
        fake,
        ChatResult(
            text="reading",
            tool_calls=[
                ToolCall(id="t1", name="read_file", arguments={"path": "app.py"}),
                ToolCall(id="t2", name="write_file", arguments={"path": "x", "content": "y"}),
            ],
            usage=Usage(input_tokens=20, output_tokens=10),
            stop_reason="tool_calls",
        ),
    )
    _push(fake, ChatResult(text="mapped", usage=Usage(input_tokens=5, output_tokens=5)))
    _push(fake, ChatResult(text=json.dumps(ARTIFACT), usage=Usage(input_tokens=9, output_tokens=9)))

    graph = autodiscover().get("surveyor").build_graph(sv_ctx)
    graph.checkpointer = InMemorySaver()
    config: dict[str, Any] = {"configurable": {"thread_id": "t-sv"}}
    final = await graph.ainvoke({"objective": "map this repo"}, config=config)

    artifact = final["artifact"]
    assert artifact is not None, final.get("artifact_error")
    assert {
        "title", "summary", "layout", "entry_points", "dependencies", "config",
        "tests", "ci", "risks", "gaps", "next_steps", "report",
    } <= set(artifact)  # fmt: skip
    assert artifact["risks"][0]["where"] == "app.py:2"
    assert artifact["report"].strip()
    tool_text = " ".join(m["content"] for m in final["messages"] if m.get("role") == "tool")
    assert "name.upper()" in tool_text  # the read happened
    assert "read-only" in tool_text  # the write_file call was refused, not run
    assert not (Path(sv_ctx.mission.repo_path or "") / "x").exists()


def test_surveyor_tool_menu_is_read_only(sv_ctx: RoleBuildCtx) -> None:
    spec = autodiscover().get("surveyor")
    assert spec.name == "surveyor"
    # the executor the role mounts: outermost wrapper over the complaint wrapper
    # that would otherwise re-add file_complaint
    inner = ComplaintAwareExecutor(LocalExecutor(Path(".")), sv_ctx.store, "mi", "r", "surveyor")
    assert "file_complaint" in {s.name for s in inner.specs()}
    names = {s.name for s in ReadOnlyExecutor(inner).specs()}
    assert names <= READ_ONLY_TOOL_NAMES
    assert names == {"read_file", "list_dir", "run_command"}
    assert not names & WRITE_TOOLS


async def test_surveyor_system_prompt_refuses_change(sv_ctx: RoleBuildCtx) -> None:
    fake = sv_ctx.provider
    assert isinstance(fake, FakeProvider)
    _push(fake, ChatResult(text="done", usage=Usage(input_tokens=1, output_tokens=1)))
    _push(fake, ChatResult(text=json.dumps(ARTIFACT), usage=Usage(input_tokens=1, output_tokens=1)))
    graph = autodiscover().get("surveyor").build_graph(sv_ctx)
    graph.checkpointer = InMemorySaver()
    await graph.ainvoke({"objective": "map"}, config={"configurable": {"thread_id": "t-p"}})
    system = fake.requests[0].system
    assert "READ-ONLY and must never change anything" in system
    assert "do not commit, push or open a pull request" in system
    assert "—" not in system  # no em dashes
    assert {t.name for t in fake.requests[0].tools} == READ_ONLY_TOOL_NAMES


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def execute(self, name: str, args: dict[str, Any]) -> str:
        self.calls.append((name, args))
        return "ok"

    def specs(self, allowed: set[str] | None = None) -> list[Any]:
        return []


@pytest.mark.parametrize(
    "name,args",
    [
        ("write_file", {"path": "a", "content": "b"}),
        ("open_pr", {}),
        ("file_complaint", {"summary": "x"}),
        ("ask_orchestrator", {"question": "x"}),
    ],
)
async def test_wrapper_refuses_non_read_only_tools(name: str, args: dict[str, Any]) -> None:
    inner = _Recorder()
    out = await ReadOnlyExecutor(inner).execute(name, args)
    assert out.startswith("error:") and "read-only" in out
    assert inner.calls == []


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf x",
        "echo x > f",
        "a && b",
        "ls; rm x",
        "ls | rm x",
        "cat f >> g",
        "cat < f",
        "sleep 9 &",
        "ls `id`",
        "ls $(id)",
        "ls\nrm x",
        "git commit -m x",
        "git push",
        "git --no-pager checkout main",
        "git -c core.pager=sh log",
        "git branch -D x",
        "git branch newname",
        "git diff --output=out.patch",
        "find . -delete",
        "find . -exec rm {} ;",
        "sort -o out f",
        "sed -i s/a/b/ f",
        "sed -n -i 1p f",
        "awk 'BEGIN{system(\"id\")}'",
        "awk '{print > \"f\"}'",
        "/bin/rm x",
        "FOO=1 ls",
        "python -c 1",
        "",
    ],
)
async def test_wrapper_refuses_mutating_commands(command: str) -> None:
    assert validate_command(command) is not None
    inner = _Recorder()
    out = await ReadOnlyExecutor(inner).execute("run_command", {"command": command})
    assert out.startswith("error: refused") and "git is limited to" in out
    assert inner.calls == []


@pytest.mark.parametrize(
    "command",
    [
        "git log --oneline",
        "git --no-pager log --oneline -n 5 -- src",
        "git blame -L 1,5 app.py",
        "git branch --list",
        "git status --short",
        "grep -rn foo .",
        "grep -rn 'a|b' src | head -20",
        "rg -n 'x > y' src",
        "find . -name '*.py' | sort | uniq -c | wc -l",
        "sed -n '1,40p' app.py",
        "tree -L 2",
    ],
)
async def test_wrapper_accepts_read_only_commands(command: str) -> None:
    assert validate_command(command) is None
    inner = _Recorder()
    out = await ReadOnlyExecutor(inner).execute("run_command", {"command": command})
    assert out == "ok"
    assert inner.calls == [("run_command", {"command": command})]


async def test_wrapper_runs_real_git_log(tmp_path: Path) -> None:
    from conftest import make_git_repo

    repo = make_git_repo(tmp_path)
    ex = ReadOnlyExecutor(LocalExecutor(repo))
    assert (await ex.execute("run_command", {"command": "git log --oneline"})).strip()
    assert "refused" in await ex.execute(
        "run_command", {"command": "git commit --allow-empty -m x"}
    )


def test_audit_plan_cannot_publish() -> None:
    assert "audit" in PLANS
    assert PLAN_AUDIT["gates"] == []
    assert not PLAN_AUDIT.get("publish")
    assert [s["role"] for s in PLAN_AUDIT["steps"]] == ["investigator", "surveyor"]
    assert PLAYBOOK_GATES["audit"] == []
    assert PLAYBOOK_PUBLISH["audit"] == ""
    assert "audit" in PLAYBOOKS and set(PLAYBOOK_GATES) == set(PLAYBOOKS)
    plan = orchestrated_plan("audit", "pro")
    assert plan["gates"] == [] and plan["publish"] == ""
    assert "coder" not in PLAYBOOKS["audit"].replace("Do NOT spawn a coder", "")


async def test_create_mission_audit_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from conftest import make_git_repo
    from rework.missions import create_mission

    db = tmp_path / "state.sqlite3"
    monkeypatch.setenv("REWORK_DB", str(db))
    repo = make_git_repo(tmp_path / "r")
    mission = await create_mission("map the repo", plan="audit", repo=str(repo))
    assert mission.plan is not None
    assert mission.plan["kind"] == "audit"
    assert mission.plan["gates"] == []
    assert "publish" not in mission.plan
    assert all(s["role"] != "coder" for s in mission.plan["steps"])


def test_survey_sections_and_turns() -> None:
    keys = [s.key for s in resolve_sections(list(ASSEMBLY_SURVEYOR))]
    assert set(keys) == set(ASSEMBLY_SURVEYOR)
    schema = build_report_schema(resolve_sections(list(ASSEMBLY_SURVEYOR)))
    assert set(schema["required"]) == set(ASSEMBLY_SURVEYOR)
    assert DEFAULT_TURNS["surveyor"] == 25


def test_cap_cuts_with_explicit_note() -> None:
    risks = [
        {"severity": "low" if i % 2 else "high", "area": "a", "detail": str(i), "where": "f:1"}
        for i in range(30)
    ]
    out = finalize_survey({**ARTIFACT, "risks": risks, "gaps": [f"g{i}" for i in range(25)]})
    assert len(out["risks"]) + len(out["gaps"]) == MAX_RISKS_AND_GAPS
    assert out["risks"][0]["severity"] == "high"  # worst first
    assert out["cut_note"].startswith("15 of 55 risks and gaps were cut")
    assert "were cut" in out["report"]
    small = finalize_survey({**ARTIFACT, "report": ""})
    assert "cut_note" not in small
    assert "app.py:2" in small["report"]  # empty report rebuilt from the fields


def test_render_md_surveyor_only_mission_shows_the_map() -> None:
    run = SimpleNamespace(step="survey", role="surveyor", attempt=1, status=RunStatus.COMPLETED)
    mission = SimpleNamespace(
        id="m-1",
        status=MissionStatus.DONE,
        objective="map it",
        updated_at=2.0,
        created_at=1.0,
        result={},
    )
    spend = SimpleNamespace(spent_usd=0.0, input_tokens=0, output_tokens=0, cached_tokens=0)
    artifact = {**ARTIFACT, "cut_note": "3 of 43 risks and gaps were cut at the cap of 40."}
    md = _render_md([(run, artifact)], spend, mission)
    assert "A one-file greeter." in md
    assert "### Layout" in md and "app.py" in md
    assert "### Risks" in md and "`app.py:2`" in md
    assert "app.py:1-2" in md  # entry point where
    assert "were cut" in md
    assert "no tests were found" in md
    assert "coder" not in md.replace("(surveyor)", "")
