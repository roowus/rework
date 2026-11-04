"""The vertical slice, scripted: investigate -> code -> audit -> ship gate on
a copy of examples/toyrepo, plus the veto path (audit requests changes -> coder
revision -> re-audit -> approve). This is the M2 acceptance in test form.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from conftest import (
    AUDIT_CLEAN,
    AUDIT_VETO,
    ScriptedProvider,
    _auditor_script,
    _coder_script,
    _investigator_script,
    role_catalog,
)
from rework.cli import PLAN_FIX
from rework.kernel.events.bus import InProcessEventBus
from rework.kernel.gates import GateManager
from rework.kernel.llm.types import ChatResult, ToolCall, Usage
from rework.kernel.state.records import Mission, MissionStatus
from rework.kernel.state.sqlite import SqliteStore, open_sqlite
from rework.orchestrator.engine import MissionRunner
from rework.roles.registry import autodiscover


@pytest.fixture
def toy_repo(tmp_path: Path) -> Path:
    """examples/toyrepo as a real git repo (what `rework new --repo` prepares)."""
    source = Path(__file__).parents[2] / "examples" / "toyrepo"
    dest = tmp_path / "mission" / "toyrepo"
    dest.mkdir(parents=True)
    for f in source.iterdir():
        shutil.copy(f, dest / f.name)
    subprocess.run(["git", "init", "-b", "main"], cwd=dest, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=dest, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=r@r", "-c", "user.name=r", "commit", "-m", "init"],
        cwd=dest,
        check=True,
        capture_output=True,
    )
    return dest


@pytest.fixture
async def slice_world(tmp_path: Path, toy_repo: Path) -> AsyncIterator[dict[str, Any]]:  # type: ignore[no-untyped-def]
    store: SqliteStore = await open_sqlite(str(tmp_path / "slice.sqlite3"))
    try:
        objective = "make add(2, 3) == 5 in toyrepo"
        plan = json.loads(json.dumps(PLAN_FIX).replace("$OBJECTIVE", objective))
        mission = await store.create_mission(
            Mission(id="ms", objective=objective, repo_path=str(toy_repo), plan=plan)
        )
        provider = ScriptedProvider()
        world: dict[str, Any] = {
            "store": store,
            "provider": provider,
            "mission": mission,
            "ck": str(tmp_path / "ck.sqlite3"),
            "workroot": tmp_path / "work",
            "registry": autodiscover(),
            "catalog": role_catalog(),
            "tmp_path": tmp_path,
        }
        yield world
    finally:
        await store.aclose()


def _runner(world: dict[str, Any]) -> MissionRunner:
    from rework.orchestrator.daemon import sqlite_checkpointer_factory

    return MissionRunner(
        world["mission"].id,
        world["store"],
        InProcessEventBus(world["store"]),
        world["registry"],
        world["catalog"],
        sqlite_checkpointer_factory(world["ck"]),
        world["workroot"],
        poll_interval_s=0.05,
        provider=world["provider"],
    )


async def test_vertical_slice_happy_path(slice_world: dict[str, Any]) -> None:
    store: SqliteStore = slice_world["store"]
    provider: ScriptedProvider = slice_world["provider"]
    provider.route("Investigate any brief", _investigator_script())
    provider.route("Fulfill the task", _coder_script("tests pass: 2/1 green, add fixed"))
    provider.route("Find issues", _auditor_script(AUDIT_CLEAN))

    runner = _runner(slice_world)
    task = asyncio.create_task(runner.run())

    # wait for the ship gate, then approve from a second connection
    for _ in range(200):
        gates = await store.list_open_gates(slice_world["mission"].id)
        if gates:
            break
        await asyncio.sleep(0.1)
    gates = await store.list_open_gates(slice_world["mission"].id)
    assert gates, "ship gate never opened"
    second: SqliteStore = await open_sqlite(str(slice_world["tmp_path"] / "slice.sqlite3"))
    try:
        await GateManager(second, InProcessEventBus(second)).decide(gates[0].id, approved=True)
    finally:
        await second.aclose()

    await asyncio.wait_for(task, timeout=30)
    mission = await store.get_mission(slice_world["mission"].id)
    assert mission is not None and mission.status == MissionStatus.DONE

    runs = await store.list_runs(mission.id)
    by_step = {r.step: r for r in runs}
    assert by_step["investigate"].status.value == "completed"
    assert by_step["code"].status.value == "completed"
    assert by_step["audit"].status.value == "completed"
    coder_artifact = by_step["code"].artifact or {}
    assert "+    return a + b" in coder_artifact.get("diff", "")
    # investigator findings reached the coder's context (the wiring works)
    assert "planted bug" in str(by_step["code"].context.get("investigation", ""))
    # auditor saw the diff and the worktree (diff-only policy)
    assert "return a + b" in str(by_step["audit"].context.get("diff", ""))


async def test_vertical_slice_veto_triggers_revision(slice_world: dict[str, Any]) -> None:
    store: SqliteStore = slice_world["store"]
    provider: ScriptedProvider = slice_world["provider"]
    provider.route("Investigate any brief", _investigator_script())
    provider.route("Fulfill the task", _coder_script("first attempt summary"))
    provider.route("Find issues", _auditor_script(AUDIT_VETO))  # round 1: veto
    provider.route("Fulfill the task", _coder_script("revision attempt, tests green"))
    provider.route("Find issues", _auditor_script(AUDIT_CLEAN))  # round 2: approve

    runner = _runner(slice_world)
    task = asyncio.create_task(runner.run())
    for _ in range(400):
        gates = await store.list_open_gates(slice_world["mission"].id)
        if gates:
            break
        await asyncio.sleep(0.1)
    gates = await store.list_open_gates(slice_world["mission"].id)
    assert gates
    second: SqliteStore = await open_sqlite(str(slice_world["tmp_path"] / "slice.sqlite3"))
    try:
        await GateManager(second, InProcessEventBus(second)).decide(gates[0].id, approved=True)
    finally:
        await second.aclose()

    await asyncio.wait_for(task, timeout=30)
    runs = await store.list_runs(slice_world["mission"].id)
    coders = [r for r in runs if r.step == "code"]
    audits = [r for r in runs if r.step == "audit"]
    assert len(coders) == 2, "audit veto must trigger a coder revision"
    assert len(audits) == 2
    assert coders[0].attempt == 1 and coders[1].attempt == 2
    assert "PLAUSIBLE" in str(coders[1].context.get("investigation", "")) or "critical" in str(
        coders[1].context.get("investigation", "")
    )
    mission = await store.get_mission(slice_world["mission"].id)
    assert mission is not None and mission.status == MissionStatus.DONE


async def test_role_files_complaint_mid_run(slice_world: dict[str, Any]) -> None:
    """ADR 0004 early landing: a role's file_complaint tool call lands a typed
    complaint row in the store, fire-and-forget, without pausing the mission."""
    store: SqliteStore = slice_world["store"]
    provider: ScriptedProvider = slice_world["provider"]

    # investigator complains about a missing tool, then reports as usual
    script = _investigator_script()
    script.insert(
        0,
        ChatResult(
            text="I keep wanting a search tool",
            tool_calls=[
                ToolCall(
                    id="cp1",
                    name="file_complaint",
                    arguments={
                        "kind": "tool_missing",
                        "summary": "no search tool in the workspace",
                        "detail": "kept grepping by hand; a search tool would help",
                        "evidence": ["3 manual grep commands in this session"],
                        "suggestion": "add a search tool to the builtin pack",
                    },
                )
            ],
            usage=Usage(input_tokens=10, output_tokens=5),
            stop_reason="tool_calls",
        ),
    )
    provider.route("Investigate any brief", script)
    provider.route("Fulfill the task", _coder_script("tests pass: 2/1 green, add fixed"))
    provider.route("Find issues", _auditor_script(AUDIT_CLEAN))

    runner = _runner(slice_world)
    task = asyncio.create_task(runner.run())
    for _ in range(200):
        gates = await store.list_open_gates(slice_world["mission"].id)
        if gates:
            await GateManager(store, InProcessEventBus(store)).decide(gates[0].id, approved=True)
            break
        await asyncio.sleep(0.1)
    await asyncio.wait_for(task, timeout=30)

    # the mission still completed (fire-and-forget) and the complaint landed
    mission = await store.get_mission(slice_world["mission"].id)
    assert mission is not None and mission.status == MissionStatus.DONE
    complaints = await store.list_complaints()
    assert len(complaints) == 1
    c = complaints[0]
    assert c.kind.value == "tool_missing"
    assert c.source_role == "investigator"
    assert c.source_mission_id == slice_world["mission"].id
    assert c.evidence == ["3 manual grep commands in this session"]
    assert c.status.value == "open"
