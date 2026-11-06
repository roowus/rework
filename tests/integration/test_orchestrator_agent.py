"""The LLM mission orchestrator (ADR 0007) through the REAL engine, scripted.

The orchestrator's model replies are scripted tool calls; the roles run their
real graphs on scripted model output against a real git repo. What is under
test is the engine side: spawning, automatic wiring, waiting, parallel runs,
tool errors fed back, fixed final gates, persistence and resume after a kill."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from collections.abc import AsyncIterator
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
from rework.kernel.gates import GateManager
from rework.kernel.llm.types import ChatResult, ToolCall, Usage
from rework.kernel.state.records import Mission, MissionStatus, RunStatus
from rework.kernel.state.sqlite import SqliteStore, open_sqlite
from rework.missions import orchestrated_plan
from rework.orchestrator.agent import MARKER, orchestrator_chat_id, orchestrator_run_id
from rework.orchestrator.engine import MissionRunner
from rework.roles.registry import autodiscover

OBJECTIVE = "make add(2, 3) == 5 in toyrepo"


def call(name: str, cid: str, **args: Any) -> ToolCall:
    return ToolCall(id=cid, name=name, arguments=args)


def turn(*calls: ToolCall, text: str = "") -> ChatResult:
    return ChatResult(
        text=text, tool_calls=list(calls), usage=Usage(input_tokens=500, output_tokens=40)
    )


@pytest.fixture
def toy_repo(tmp_path: Path) -> Path:
    source = Path(__file__).parents[2] / "examples" / "toyrepo"
    dest = tmp_path / "mission" / "toyrepo"
    dest.mkdir(parents=True)
    for f in source.iterdir():
        shutil.copy(f, dest / f.name)
    for args in (
        ["git", "init", "-b", "main"],
        ["git", "add", "-A"],
        ["git", "-c", "user.email=r@r", "-c", "user.name=r", "commit", "-m", "init"],
    ):
        subprocess.run(args, cwd=dest, check=True, capture_output=True)
    return dest


@pytest.fixture
async def world(tmp_path: Path, toy_repo: Path) -> AsyncIterator[dict[str, Any]]:
    db = str(tmp_path / "orch.sqlite3")
    store: SqliteStore = await open_sqlite(db)
    try:
        plan = orchestrated_plan("fix", "pro")
        mission = await store.create_mission(
            Mission(id="mo", objective=OBJECTIVE, repo_path=str(toy_repo), plan=plan)
        )
        yield {
            "db": db,
            "store": store,
            "provider": ScriptedProvider(),
            "mission": mission,
            "ck": str(tmp_path / "ck.sqlite3"),
            "workroot": tmp_path / "work",
            "registry": autodiscover(),
            "catalog": role_catalog(),
        }
    finally:
        await store.aclose()


def runner(world: dict[str, Any]) -> MissionRunner:
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


async def approve_next_gate(world: dict[str, Any], within_s: float = 30) -> Any:
    store = world["store"]
    for _ in range(int(within_s / 0.1)):
        gates = await store.list_open_gates(world["mission"].id)
        if gates:
            second = await open_sqlite(world["db"])
            try:
                await GateManager(second, InProcessEventBus(second)).decide(
                    gates[0].id, approved=True
                )
            finally:
                await second.aclose()
            return gates[0]
        await asyncio.sleep(0.1)
    raise AssertionError("no gate opened")


def script_roles(provider: ScriptedProvider) -> None:
    provider.route("Investigate any brief", _investigator_script())
    provider.route("Fulfill the task", _coder_script("tests pass: add fixed"))
    provider.route("Find issues", _auditor_script(AUDIT_CLEAN))


async def test_orchestrator_runs_the_fix_shape(world: dict[str, Any]) -> None:
    provider: ScriptedProvider = world["provider"]
    store: SqliteStore = world["store"]
    # the orchestrator route goes FIRST: its prompt lists the roles' goals,
    # which contain the role routes' substrings
    provider.route(
        MARKER,
        [
            turn(
                call(
                    "spawn_role",
                    "o1",
                    role="investigator",
                    step="investigate",
                    brief="find why add(2, 3) != 5",
                    model="flash",
                ),
                call("wait", "o2"),
                text="Investigating first.",
            ),
            turn(call("read_artifact", "o3", run_id="mo:investigate:investigator:1")),
            turn(
                call("spawn_role", "o4", role="coder", step="code", brief="fix add()"),
                call("wait", "o5"),
            ),
            turn(
                call("spawn_role", "o6", role="auditor", step="audit", brief="review the fix"),
                call("message_owner", "o7", text="coder done, auditing"),
                call("wait", "o8"),
            ),
            turn(call("finish", "o9", outcome="done", summary="add() fixed and audited")),
        ],
    )
    script_roles(provider)
    task = asyncio.create_task(runner(world).run())
    gate = await approve_next_gate(world)
    assert gate.kind.value == "ship", "the plan's fixed final gate, not the model's choice"
    await asyncio.wait_for(task, timeout=30)

    mission = await store.get_mission("mo")
    assert mission is not None and mission.status == MissionStatus.DONE
    assert (mission.result or {}).get("summary") == "add() fixed and audited"
    runs = {r.step: r for r in await store.list_runs("mo")}
    assert set(runs) == {"investigate", "code", "audit"}
    assert all(r.status == RunStatus.COMPLETED for r in runs.values())
    assert runs["investigate"].model_ref.get("id") == "flash", "orchestrator picks models"
    # automatic wiring: findings -> coder, diff + worktree -> auditor
    assert "planted bug" in str(runs["code"].context.get("investigation", ""))
    assert "return a + b" in str(runs["audit"].context.get("diff", ""))
    assert runs["code"].context.get("brief") == "fix add()"

    # the orchestrator is visible like a role, and it pays its own way
    events = await store.events_since(0, mission_id="mo", limit=1000)
    orch = [e for e in events if (e.payload or {}).get("actor") == "orchestrator"]
    assert any((e.payload or {}).get("trajectory") == "tool_call" for e in orch)
    assert any("auditing" in str((e.payload or {}).get("note")) for e in orch)
    charges = await store.run_charges(orchestrator_run_id("mo"))
    assert len(charges) == 5 and all(c.input_tokens == 500 for c in charges)
    # the conversation is persisted (that is what makes resume possible)
    msgs = await store.chat_messages(orchestrator_chat_id("mo"))
    assert [m.role for m in msgs].count("assistant") == 5
    # the report the orchestrator saw included the investigator's handoff
    first_tool_results = [m.body.get("content", "") for m in msgs if m.role == "tool"]
    assert any("completed" in c for c in first_tool_results)


async def test_parallel_spawns_and_tool_errors_are_fed_back(world: dict[str, Any]) -> None:
    provider: ScriptedProvider = world["provider"]
    store: SqliteStore = world["store"]
    provider.route(
        MARKER,
        [
            turn(
                call("spawn_role", "p1", role="wizard", brief="cast a spell"),
                call("spawn_role", "p2", role="investigator", step="look-a", brief="part a"),
                call("spawn_role", "p3", role="investigator", step="look-b", brief="part b"),
            ),
            turn(call("finish", "p4", outcome="done", summary="early")),  # rejected: live runs
            turn(call("wait", "p5")),
            turn(call("wait", "p6")),
            turn(call("finish", "p7", outcome="failed", summary="no coder needed, stopping")),
        ],
    )
    provider.route("Investigate any brief", _investigator_script() + _investigator_script())
    task = asyncio.create_task(runner(world).run())
    await asyncio.wait_for(task, timeout=30)

    runs = await store.list_runs("mo")
    assert sorted(r.step for r in runs) == ["look-a", "look-b"], "two roles ran in parallel"
    msgs = await store.chat_messages(orchestrator_chat_id("mo"))
    tool_out = [m.body.get("content", "") for m in msgs if m.role == "tool"]
    assert any("no role 'wizard'" in t for t in tool_out), "bad call answered, not crashed"
    assert any("still running" in t for t in tool_out), "finish refused while roles run"
    mission = await store.get_mission("mo")
    assert mission is not None and mission.status == MissionStatus.FAILED
    assert not await store.list_gates("mo"), "a failed mission skips the final gate"


async def test_orchestrator_resumes_after_a_kill(world: dict[str, Any]) -> None:
    """Kill the runner while the orchestrator is waiting on the coder; a fresh
    runner reloads the conversation, re-runs the interrupted wait, and carries
    on without spawning a duplicate."""
    provider: ScriptedProvider = world["provider"]
    store: SqliteStore = world["store"]
    provider.route(
        MARKER,
        [
            turn(
                call("spawn_role", "k1", role="coder", step="code", brief="fix add()"),
                call("wait", "k2"),
            ),
            turn(call("finish", "k3", outcome="done", summary="fixed after restart")),
        ],
    )
    # the coder hangs until we release it, so the kill lands mid-wait
    release = asyncio.Event()
    coder_turns = _coder_script("tests pass")

    class Gate(ScriptedProvider):
        async def complete(self, request):  # type: ignore[no-untyped-def]
            if "Fulfill the task" in (request.system or "") and MARKER not in (
                request.system or ""
            ):
                await release.wait()
            return await super().complete(request)

    gated = Gate()
    gated._queues = provider._queues
    gated.route("Fulfill the task", coder_turns)
    world["provider"] = gated

    first = asyncio.create_task(runner(world).run())
    for _ in range(100):
        if await store.get_run("mo:code:coder:1") is not None:
            break
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.3)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    release.set()
    second = asyncio.create_task(runner(world).run())
    await approve_next_gate(world)
    await asyncio.wait_for(second, timeout=30)

    runs = await store.list_runs("mo")
    assert [r.id for r in runs] == ["mo:code:coder:1"], "no duplicate spawn after resume"
    mission = await store.get_mission("mo")
    assert mission is not None and mission.status == MissionStatus.DONE
    assert (mission.result or {}).get("summary") == "fixed after restart"


async def test_owner_cancel_stops_the_orchestrator(world: dict[str, Any]) -> None:
    """The owner cancels while the orchestrator is mid-mission (here: during
    its second model call). The next owner check must end the loop with the
    mission CANCELLED, no final gate, no further model calls."""
    store: SqliteStore = world["store"]
    calls = 0

    class CancelOnSecond(ScriptedProvider):
        async def complete(self, request):  # type: ignore[no-untyped-def]
            nonlocal calls
            calls += 1
            if calls == 2:
                await store.update_mission("mo", status=MissionStatus.CANCELLED)
            return turn(call("wait", f"c{calls}", minutes=10))

    world["provider"] = CancelOnSecond()
    await asyncio.wait_for(runner(world).run(), timeout=20)
    mission = await store.get_mission("mo")
    assert mission is not None and mission.status == MissionStatus.CANCELLED
    assert calls == 2, "no model calls after the cancel"
    assert not await store.list_gates("mo")
