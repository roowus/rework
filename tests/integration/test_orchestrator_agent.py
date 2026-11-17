"""The LLM mission orchestrator (ADR 0007) through the REAL engine, scripted.

The orchestrator's model replies are scripted tool calls; the roles run their
real graphs on scripted model output against a real git repo. What is under
test is the engine side: spawning, automatic wiring, waiting, parallel runs,
tool errors fed back, fixed final gates, persistence and resume after a kill."""

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
    ScriptedProvider,
    _auditor_script,
    _coder_script,
    _investigator_script,
    role_catalog,
    wait_for_gate_park,
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
    # each role's own LangGraph is visible: topology on spawn, node enters live
    spawned = [e for e in events if e.type.value == "role_spawned"]
    coder_graph = next(e for e in spawned if e.run_id == "mo:code:coder:1").payload["graph"]
    assert {"prepare", "loop", "handoff"} <= set(coder_graph["nodes"])
    assert ["prepare", "loop", False] in coder_graph["edges"]
    entered = [
        e.payload["node"]
        for e in events
        if e.run_id == "mo:code:coder:1" and e.payload.get("phase") == "enter"
    ]
    assert entered == ["prepare", "loop", "handoff"]
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
            turn(
                call("spawn_role", "k3", role="auditor", step="audit", brief="review the fix"),
                call("wait", "k4"),
            ),
            turn(
                call(
                    "finish",
                    "k5",
                    outcome="done",
                    summary="fixed the reload bug so a restart keeps the shell alive",
                )
            ),
        ],
    )
    provider.route("Find issues", _auditor_script(AUDIT_CLEAN))
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
    assert [r.id for r in runs] == ["mo:code:coder:1", "mo:audit:auditor:1"], (
        "no duplicate spawn after resume"
    )
    mission = await store.get_mission("mo")
    assert mission is not None and mission.status == MissionStatus.DONE
    assert (mission.result or {}).get("summary") == (
        "fixed the reload bug so a restart keeps the shell alive"
    )


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


# -- steering (M7 P2) ----------------------------------------------------------


def asking_investigator(question: str) -> list[ChatResult]:
    """An investigator that asks the orchestrator first, then works normally."""
    ask = ChatResult(
        tool_calls=[ToolCall(id="q1", name="ask_orchestrator", arguments={"question": question})],
        usage=Usage(input_tokens=20, output_tokens=10),
    )
    return [ask, *_investigator_script()]


def tool_results_seen(provider: ScriptedProvider, marker: str) -> list[str]:
    out = []
    for req in provider.requests:
        if marker in (req.system or ""):
            out += [
                str(m.get("content")) for m in req.messages if m.get("role") in ("tool", "user")
            ]
    return out


async def test_a_role_question_is_answered_by_the_orchestrator(world: dict[str, Any]) -> None:
    provider: ScriptedProvider = world["provider"]
    store: SqliteStore = world["store"]
    provider.route(
        MARKER,
        [
            turn(
                call("spawn_role", "a1", role="investigator", step="look", brief="look"),
                call("wait", "a2"),
            ),
            turn(
                call(
                    "answer_escalation",
                    "a3",
                    run_id="mo:look:investigator:1",
                    answer="only calc.py matters",
                ),
                call("wait", "a4"),
            ),
            turn(call("finish", "a5", outcome="failed", summary="investigated only the parser")),
        ],
    )
    provider.route("Investigate any brief", asking_investigator("which file matters?"))
    await asyncio.wait_for(runner(world).run(), timeout=30)

    msgs = await store.chat_messages(orchestrator_chat_id("mo"))
    tool_out = [m.body.get("content", "") for m in msgs if m.role == "tool"]
    assert any("asks [question]: which file matters?" in t for t in tool_out)
    seen = tool_results_seen(provider, "Investigate any brief")
    assert "orchestrator: only calc.py matters" in seen, "the role got the answer"
    run = await store.get_run("mo:look:investigator:1")
    assert run is not None and run.status == RunStatus.COMPLETED


async def test_a_role_question_forwarded_to_the_owner_as_a_choice(world: dict[str, Any]) -> None:
    provider: ScriptedProvider = world["provider"]
    store: SqliteStore = world["store"]
    provider.route(
        MARKER,
        [
            turn(
                call("spawn_role", "b1", role="investigator", step="look", brief="look"),
                call("wait", "b2"),
            ),
            turn(
                call(
                    "ask_owner",
                    "b3",
                    question="Old parser or new parser?",
                    kind="choice",
                    options=["old", "new"],
                    run_id="mo:look:investigator:1",
                ),
                call("wait", "b4"),
            ),
            turn(call("finish", "b5", outcome="failed", summary="owner picked the new parser")),
        ],
    )
    provider.route("Investigate any brief", asking_investigator("old or new parser?"))
    task = asyncio.create_task(runner(world).run())
    # BARRIER: the orchestrator's ask_owner commits the gate row and then the
    # AWAITING_GATE status, two separate writes (agent._ask_owner). Wait for
    # BOTH facts, or the status assert below races the second write.
    await wait_for_gate_park(
        store, "mo", mission_status=MissionStatus.AWAITING_GATE, what="ask_owner gate"
    )
    gates = await store.list_open_gates("mo")
    gate = gates[0]
    assert gate.kind.value == "choice"
    assert (gate.spec or {}).get("options") == ["old", "new"]
    assert gate.for_run == "mo:look:investigator:1"
    mission = await store.get_mission("mo")
    assert mission is not None and mission.status == MissionStatus.AWAITING_GATE
    second = await open_sqlite(world["db"])
    try:
        await GateManager(second, InProcessEventBus(second)).decide(
            gate.id, approved=True, decision={"choice": "new"}
        )
    finally:
        await second.aclose()
    await asyncio.wait_for(task, timeout=30)
    seen = tool_results_seen(provider, "Investigate any brief")
    assert any("owner: approved: new" in s for s in seen), "the role got the owner's choice"


async def test_redirect_and_owner_message_reach_their_targets(world: dict[str, Any]) -> None:
    """The orchestrator redirects a running role; the owner messages the
    orchestrator mid-mission. Both arrive."""
    store: SqliteStore = world["store"]
    release = asyncio.Event()
    inv = _investigator_script()

    class Held(ScriptedProvider):
        async def complete(self, request):  # type: ignore[no-untyped-def]
            system = request.system or ""
            if "Investigate any brief" in system and MARKER not in system:
                self.requests.append(request)
                if len(self.requests_for_role) == 0:
                    self.requests_for_role.append(request)
                    await release.wait()
                else:
                    self.requests_for_role.append(request)
                return inv.pop(0)
            return await super().complete(request)

    provider = Held()
    provider.requests_for_role = []  # type: ignore[attr-defined]
    provider.route(
        MARKER,
        [
            turn(
                call("spawn_role", "r1", role="investigator", step="look", brief="look"),
                call("wait", "r2", minutes=10),
            ),
            # woken by the owner's message
            turn(
                call(
                    "redirect", "r3", run_id="mo:look:investigator:1", instruction="check mul too"
                ),
                call("wait", "r4"),
            ),
            turn(call("finish", "r5", outcome="failed", summary="redirected the investigator")),
        ],
    )
    world["provider"] = provider
    task = asyncio.create_task(runner(world).run())
    for _ in range(200):
        if provider.requests_for_role:  # type: ignore[attr-defined]
            break
        await asyncio.sleep(0.05)
    from rework.kernel.state.records import EventRecord, EventType

    await store.append_event(
        EventRecord(mission_id="mo", type=EventType.OWNER_MESSAGE, payload={"text": "add a test"})
    )
    for _ in range(200):
        run = await store.get_run("mo:look:investigator:1")
        if run is not None and run.redirect:
            break
        await asyncio.sleep(0.05)
    release.set()
    await asyncio.wait_for(task, timeout=30)

    orch_reqs = [r for r in provider.requests if MARKER in (r.system or "")]
    assert any(
        "Message from the owner: add a test" in str(m.get("content")) for m in orch_reqs[1].messages
    ), "the owner's message woke the orchestrator and is in its next turn"
    role_msgs = [m for r in provider.requests_for_role for m in r.messages]  # type: ignore[attr-defined]
    assert any("[Orchestrator redirect] check mul too" in str(m.get("content")) for m in role_msgs)


# -- cost controls ------------------------------------------------------------------


async def test_revision_coder_continues_the_previous_branch(world: dict[str, Any]) -> None:
    provider: ScriptedProvider = world["provider"]
    store: SqliteStore = world["store"]
    provider.route(
        MARKER,
        [
            turn(
                call("spawn_role", "v1", role="coder", step="code", brief="fix add()"),
                call("wait", "v2"),
            ),
            turn(
                call("spawn_role", "v3", role="coder", step="code", brief="also tidy it"),
                call("wait", "v4"),
            ),
            turn(call("finish", "v5", outcome="failed", summary="two coders ran the parser fix")),
        ],
    )
    provider.route("Fulfill the task", _coder_script("first") + _coder_script("second"))
    await asyncio.wait_for(runner(world).run(), timeout=30)
    first = await store.get_run("mo:code:coder:1")
    second = await store.get_run("mo:code:coder:2")
    assert first is not None and second is not None
    branch = (first.artifact or {}).get("branch")
    assert second.context.get("continue_branch") == branch, "revision builds on round one"
    assert second.context.get("base_sha") == (first.artifact or {}).get("base_sha")
    assert "first" in str(second.context.get("previous_attempt")), "round one's note travels"
    assert "+    return a + b" in (second.artifact or {}).get("diff", ""), "full diff kept"
    first_msg = next(
        m["content"]
        for r in provider.requests
        if "Fulfill the task" in (r.system or "") and MARKER not in (r.system or "")
        for m in r.messages[:1]
        if "continuing earlier work" in str(m.get("content"))
    )
    assert branch in first_msg


async def test_turn_budget_stops_a_role(world: dict[str, Any]) -> None:
    provider: ScriptedProvider = world["provider"]
    store: SqliteStore = world["store"]
    provider.route(
        MARKER,
        [
            turn(
                call(
                    "spawn_role", "t1", role="investigator", step="look", brief="look", max_turns=3
                ),
                call("wait", "t2"),
            ),
            turn(call("finish", "t3", outcome="failed", summary="budget spent on the parser fix")),
        ],
    )
    reads = [
        ChatResult(
            tool_calls=[ToolCall(id=f"r{i}", name="read_file", arguments={"path": "calc.py"})],
            usage=Usage(input_tokens=10, output_tokens=5),
        )
        for i in range(10)
    ]
    from conftest import INV_REPORT

    handoff = ChatResult(text="HANDOFF: calc.py:2 subtracts; run pytest -q", usage=Usage())
    provider.route("Investigate any brief", [*reads[:3], handoff, ChatResult(text=INV_REPORT)])
    await asyncio.wait_for(runner(world).run(), timeout=30)
    run = await store.get_run("mo:look:investigator:1")
    assert run is not None and run.status == RunStatus.COMPLETED
    assert run.context.get("max_turns") == 3
    role_reqs = [
        r
        for r in provider.requests
        if "Investigate any brief" in (r.system or "") and MARKER not in (r.system or "")
    ]
    loop_reqs = [r for r in role_reqs if r.tools]
    assert len(loop_reqs) == 3, "the loop stopped at its budget"
    assert "Turn budget: about 3" in str(loop_reqs[0].messages[0]["content"])
    assert any("wrap up" in str(m.get("content")) for m in loop_reqs[-1].messages)
    note_req = next(r for r in role_reqs if not r.tools and "handoff note" in str(r.messages[-1]))
    assert "out of turns" in str(note_req.messages[-1]["content"])
    writer_input = str(role_reqs[-1].messages)
    assert "HANDOFF: calc.py:2 subtracts" in writer_input, "the note reaches the report"


def _git(args: list[str], cwd: Path | None = None) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout


async def test_ship_pushes_the_branch_for_a_git_origin_mission(
    tmp_path: Path, toy_repo: Path
) -> None:
    """Found live: a shipped mission's commit stayed on the daemon's disk.
    Approving ship on a git-origin mission pushes the final coder branch."""
    bare = tmp_path / "origin.git"
    await asyncio.to_thread(_git, ["git", "clone", "--bare", str(toy_repo), str(bare)])
    clone = tmp_path / "clone"
    await asyncio.to_thread(_git, ["git", "clone", str(bare), str(clone)])
    db = str(tmp_path / "ship.sqlite3")
    store = await open_sqlite(db)
    try:
        await store.create_mission(
            Mission(
                id="ms",
                objective=OBJECTIVE,
                repo_path=str(clone),
                repo_origin=str(bare),
                plan=orchestrated_plan("fix", "pro"),
            )
        )
        provider = ScriptedProvider()
        provider.route(
            MARKER,
            [
                turn(
                    call("spawn_role", "s1", role="coder", step="code", brief="fix add()"),
                    call("wait", "s2"),
                ),
                turn(
                    call("spawn_role", "s3", role="auditor", step="audit", brief="review the fix"),
                    call("wait", "s4"),
                ),
                turn(call("finish", "s5", outcome="done", summary="pinned the add() fix")),
            ],
        )
        provider.route("Fulfill the task", _coder_script("tests pass"))
        provider.route("Find issues", _auditor_script(AUDIT_CLEAN))
        world = {
            "db": db,
            "store": store,
            "provider": provider,
            "mission": await store.get_mission("ms"),
            "ck": str(tmp_path / "ck.sqlite3"),
            "workroot": tmp_path / "work",
            "registry": autodiscover(),
            "catalog": role_catalog(),
        }
        task = asyncio.create_task(runner(world).run())
        await approve_next_gate(world)
        await asyncio.wait_for(task, timeout=30)
        mission = await store.get_mission("ms")
        assert mission is not None and mission.status == MissionStatus.DONE
        branch = (mission.result or {}).get("branch")
        assert branch == "rework/ms-code-coder-1"
        out = await asyncio.to_thread(_git, ["git", "branch", "--list", branch], bare)
        assert branch in out, "the branch reached origin"
    finally:
        await store.aclose()


async def test_restart_after_finish_goes_straight_to_the_gate(world: dict[str, Any]) -> None:
    """Found live: a restart while the mission waited at its final gate made
    the orchestrator think again. A recorded finish must be honored as-is."""
    provider: ScriptedProvider = world["provider"]
    store: SqliteStore = world["store"]
    provider.route(
        MARKER,
        [
            turn(
                call("spawn_role", "f1", role="auditor", step="audit", brief="review the branch"),
                call("wait", "f2"),
            ),
            turn(call("finish", "f3", outcome="done", summary="add() now adds correctly")),
        ],
    )
    provider.route("Find issues", _auditor_script(AUDIT_CLEAN))
    first = asyncio.create_task(runner(world).run())
    # BARRIER: wait for the park (gate row AND AWAITING_GATE), not the gate row
    await wait_for_gate_park(
        store, "mo", mission_status=MissionStatus.AWAITING_GATE, what="ship gate before the restart"
    )
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    calls_before = len(provider.requests)
    second = asyncio.create_task(runner(world).run())
    await approve_next_gate(world)
    await asyncio.wait_for(second, timeout=30)
    assert len(provider.requests) == calls_before, "no model call after a recorded finish"
    mission = await store.get_mission("mo")
    assert mission is not None and mission.status == MissionStatus.DONE
    assert (mission.result or {}).get("summary") == "add() now adds correctly"


async def test_spawn_refuses_a_model_whose_provider_keeps_failing(world: dict[str, Any]) -> None:
    """Found live: after two kimi-k3 runs died on timeouts/500s the orchestrator
    picked kimi-k3 again. Two provider failures on a model: spawn refuses it."""
    from rework.kernel.state.records import EventRecord, EventType, Run

    store: SqliteStore = world["store"]
    for i in (1, 2):
        rid = f"mo:code:coder:{i}"
        await store.create_run(
            Run(
                id=rid,
                mission_id="mo",
                role="coder",
                step="code",
                attempt=i,
                status=RunStatus.FAILED,
                model_ref={"id": "pro"},
            )
        )
        await store.append_event(
            EventRecord(
                mission_id="mo",
                run_id=rid,
                type=EventType.INFO,
                payload={"error": "InternalServerError: Error code: 500"},
            )
        )
    provider: ScriptedProvider = world["provider"]
    provider.route(
        MARKER,
        [
            turn(
                call("spawn_role", "x1", role="investigator", step="look", brief="b", model="pro")
            ),
            turn(
                call(
                    "finish",
                    "x2",
                    outcome="failed",
                    summary="stopped before spawning on the dead model",
                )
            ),
        ],
    )
    await asyncio.wait_for(runner(world).run(), timeout=30)
    msgs = await store.chat_messages(orchestrator_chat_id("mo"))
    out = [m.body.get("content", "") for m in msgs if m.role == "tool"]
    assert any("already failed 2 runs" in t and "choose another" in t for t in out)
    assert not [r for r in await store.list_runs("mo") if r.step == "look"]


async def test_the_catalog_shows_each_model_s_provider_and_endpoint(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Found live (m-b928ea6d51bc): after an Anthropic timeout the orchestrator
    promised a retry "on a different model" and picked claude-opus-4-5 -- the
    same api.anthropic.com that had just failed, and the priciest tier. The
    rendered catalog named model ids only, so it could not tell. It now shows
    the provider and the endpoint each entry's calls reach."""
    from rework.kernel.llm.catalog import ModelCatalog, ModelEntry
    from rework.orchestrator.agent import MissionOrchestrator

    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("ZAI_API_KEY", "k")
    world["catalog"] = ModelCatalog(
        [
            ModelEntry(id="haiku", provider="anthropic", model="claude-haiku-4-5", tier=1),
            ModelEntry(id="opus", provider="anthropic", model="claude-opus-4-5", tier=3),
            ModelEntry(
                id="glm",
                provider="openai-compatible",
                model="glm-4.6",
                tier=2,
                base_url="https://api.z.ai/api/v4",
                api_key_env="ZAI_API_KEY",
            ),
        ]
    )
    prompt = await MissionOrchestrator(runner(world), world["mission"])._system_prompt()
    catalog = prompt.split("CATALOG")[1].split("MISSION:")[0]
    # both Anthropic models name the SAME endpoint: that is the point
    assert "haiku: anthropic (https://api.anthropic.com)" in catalog
    assert "opus: anthropic (https://api.anthropic.com)" in catalog
    assert "glm: openai-compatible (https://api.z.ai/api/v4)" in catalog
    assert "tier 3" in catalog and "per Mtok" in catalog, "the rest of the line is intact"


async def test_the_roles_block_names_the_web_tools_without_the_draft_playbook(
    world: dict[str, Any],
) -> None:
    """A fix mission's planner must still learn that investigators and coders can
    reach the public web: before this, web_search/fetch_url were named only in
    PLAYBOOKS["draft"], so `fix`/`fix-pr` missions never heard of them."""
    from rework.orchestrator.agent import PLAYBOOKS, MissionOrchestrator

    world["mission"].plan = {**(world["mission"].plan or {}), "playbook": "fix"}
    prompt = await MissionOrchestrator(runner(world), world["mission"])._system_prompt()
    roles = prompt.split("ROLES:")[1].split("Automatic wiring:")[0]
    lines = {ln.split(":")[0].strip("- "): ln for ln in roles.splitlines() if ln.startswith("- ")}
    for role in ("investigator", "coder"):
        line = lines[role]
        assert "web_search" in line, f"the {role} entry does not mention web_search: {line}"
        assert "fetch_url" in line, f"the {role} entry does not mention fetch_url: {line}"
    assert "web_search" not in lines["auditor"], "the auditor must not gain web access"
    # and the proof it does not come from the playbook text
    playbook_text = prompt.split("PLAYBOOK (fix):")[1].split("ROLES:")[0]
    assert "web_search" not in playbook_text and "fetch_url" not in playbook_text
    assert "web_search" in PLAYBOOKS["draft"], "the draft playbook still names them too"


async def test_the_rules_say_to_retry_on_another_provider_and_not_a_pricier_tier(
    world: dict[str, Any],
) -> None:
    """The other half of the same finding: "respawn on a different model" was
    satisfied by another model on the failing endpoint, at a higher price."""
    from rework.orchestrator.agent import MissionOrchestrator

    prompt = await MissionOrchestrator(runner(world), world["mission"])._system_prompt()
    rules = prompt.split("ROLES:")[0].lower()
    assert "different provider" in rules
    assert "pricier" in rules and "tier" in rules, "do not escalate the tier to retry"


async def test_long_missions_still_see_their_newest_events(world: dict[str, Any]) -> None:
    """Found live on a 9000-event mission: events_since(0, limit=N) returns the
    OLDEST N events, so an owner message and a role's latest steps were
    invisible to the orchestrator. Its event reads page forward."""
    from rework.kernel.state.records import EventRecord, EventType, Run
    from rework.orchestrator.agent import MissionOrchestrator

    store: SqliteStore = world["store"]
    await store.create_run(
        Run(
            id="mo:code:coder:1",
            mission_id="mo",
            role="coder",
            step="code",
            status=RunStatus.RUNNING,
        )
    )
    for i in range(1200):
        await store.append_event(
            EventRecord(
                mission_id="mo",
                run_id="mo:code:coder:1",
                type=EventType.PROGRESS,
                payload={"note": f"filler {i}"},
            )
        )
    await store.append_event(
        EventRecord(
            mission_id="mo",
            run_id="mo:code:coder:1",
            type=EventType.PROGRESS,
            payload={"trajectory": "turn", "turn": 99, "text": "the newest step"},
        )
    )
    await store.append_event(
        EventRecord(mission_id="mo", type=EventType.OWNER_MESSAGE, payload={"text": "use flash"})
    )
    orch = MissionOrchestrator(runner(world), world["mission"])
    owner = await orch._owner_messages()
    assert [e.payload["text"] for e in owner] == ["use flash"]
    assert "the newest step" in await orch._check_in("mo:code:coder:1", 5)
    # incremental: a second read fetches only what is new
    await store.append_event(
        EventRecord(mission_id="mo", type=EventType.OWNER_MESSAGE, payload={"text": "and hurry"})
    )
    assert [e.payload["text"] for e in await orch._owner_messages()] == ["use flash", "and hurry"]


async def test_request_budget_raises_the_real_cap(world: dict[str, Any]) -> None:
    """Found live: the owner approved 'up to $5' on a permission gate and the
    engine's cap stayed $2. request_budget's approval moves the cap itself."""
    provider: ScriptedProvider = world["provider"]
    store: SqliteStore = world["store"]
    provider.route(
        MARKER,
        [
            turn(call("request_budget", "b1", max_usd=5, reason="one more coder")),
            turn(
                call(
                    "finish",
                    "b2",
                    outcome="failed",
                    summary="checked budget before the third coder",
                )
            ),
        ],
    )
    task = asyncio.create_task(runner(world).run())
    gate = await approve_next_gate(world)
    assert gate.kind.value == "permission" and "$2.00 to $5.00" in gate.question
    await asyncio.wait_for(task, timeout=30)
    mission = await store.get_mission("mo")
    assert mission is not None and (mission.plan or {})["budget"]["max_usd"] == 5.0
    msgs = await store.chat_messages(orchestrator_chat_id("mo"))
    assert any(
        "budget raised to $5.00" in m.body.get("content", "") for m in msgs if m.role == "tool"
    )


async def _decide_next_gate(world: dict[str, Any], approved: bool) -> Any:
    store = world["store"]
    for _ in range(300):
        gates = await store.list_open_gates(world["mission"].id)
        if gates:
            second = await open_sqlite(world["db"])
            try:
                await GateManager(second, InProcessEventBus(second)).decide(
                    gates[0].id, approved=approved
                )
            finally:
                await second.aclose()
            return gates[0]
        await asyncio.sleep(0.1)
    raise AssertionError("no gate opened")


@pytest.mark.parametrize("approved", [True, False])
async def test_the_turn_cap_asks_the_owner_instead_of_failing(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch, approved: bool
) -> None:
    """Found live: the 80-turn cap failed a large mission one step before its
    PR. At the cap the owner is asked; approval grants more turns."""
    from rework.orchestrator import agent

    monkeypatch.setattr(agent, "MAX_TURNS", 2)
    provider: ScriptedProvider = world["provider"]
    provider.route(
        MARKER,
        [
            turn(call("message_owner", "t1", text="one")),
            turn(call("message_owner", "t2", text="two")),
            turn(call("finish", "t3", outcome="failed", summary="finished after the extension")),
        ],
    )
    task = asyncio.create_task(runner(world).run())
    gate = await _decide_next_gate(world, approved)
    assert gate.kind.value == "permission" and "Allow 40 more" in gate.question
    await asyncio.wait_for(task, timeout=30)
    msgs = await world["store"].chat_messages(orchestrator_chat_id("mo"))
    finished = any(
        tc.get("name") == "finish" for m in msgs for tc in (m.body.get("tool_calls") or [])
    )
    assert finished is approved


async def test_a_long_orchestrator_history_is_compacted(world: dict[str, Any]) -> None:
    """Found live: 167 messages / 335k chars per orchestrator turn timed out on
    a cold prompt cache. Old tool results are elided like the roles' are; the
    stored chat keeps everything."""
    from rework.nodes.toolloop import COMPACT_AT, KEEP_RECENT
    from rework.orchestrator.agent import MissionOrchestrator

    orch = MissionOrchestrator(runner(world), world["mission"])
    big = "x" * 9000
    orch.history = []
    for i in range(40):
        orch.history.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": f"c{i}", "name": "wait", "arguments": {}}],
            }
        )
        orch.history.append(
            {"role": "tool", "tool_call_id": f"c{i}", "name": "wait", "content": big}
        )
    assert orch._compact_history() == 40 - KEEP_RECENT
    size = sum(len(str(m.get("content") or "")) for m in orch.history)
    assert size < COMPACT_AT
    assert orch.history[-1]["content"] == big  # recent results intact
    assert orch._compact_history() == 0  # stable until it grows again


async def test_the_orchestrator_switches_its_own_model_when_it_fails(
    world: dict[str, Any],
) -> None:
    """Found live: the orchestrator's own model (kimi-k3) timed out three times
    and the mission failed, one step from its PR, while flash was healthy."""
    scripted: ScriptedProvider = world["provider"]
    scripted.route(
        MARKER,
        [
            turn(
                call(
                    "finish",
                    "f1",
                    outcome="failed",
                    summary="finished on flash after pro timed out",
                )
            )
        ],
    )

    class PickyProvider:
        def __init__(self) -> None:
            self.models: list[str] = []

        async def complete(self, request: Any) -> Any:
            self.models.append(request.model)
            if request.model == "fake-pro":
                raise TimeoutError("pro is down")
            return await scripted.complete(request)

    picky = PickyProvider()
    world["provider"] = picky
    await asyncio.wait_for(runner(world).run(), timeout=30)
    assert picky.models[:2] == ["fake-pro", "fake-flash"]
    msgs = await world["store"].chat_messages(orchestrator_chat_id("mo"))
    assert any(tc.get("name") == "finish" for m in msgs for tc in (m.body.get("tool_calls") or []))


async def test_a_stalled_turn_uses_the_fallback_then_returns_to_the_chosen_model(
    world: dict[str, Any],
) -> None:
    """kimi-k3 stalls on ~6% of calls; one stall must not cost the owner their
    chosen model, but three in a row switch for good."""
    scripted: ScriptedProvider = world["provider"]
    scripted.route(
        MARKER,
        [
            turn(call("message_owner", "a", text="answered by flash")),
            turn(call("message_owner", "b", text="back on pro")),
            turn(call("finish", "c", outcome="failed", summary="messaged the owner twice")),
        ],
    )

    class FlakyPro:
        def __init__(self) -> None:
            self.models: list[str] = []

        async def complete(self, request: Any) -> Any:
            self.models.append(request.model)
            if request.model == "fake-pro" and self.models.count("fake-pro") == 1:
                raise TimeoutError("stalled")
            return await scripted.complete(request)

    flaky = FlakyPro()
    world["provider"] = flaky
    await asyncio.wait_for(runner(world).run(), timeout=30)
    assert flaky.models == ["fake-pro", "fake-flash", "fake-pro", "fake-pro"]


async def test_three_failed_turns_switch_the_orchestrator_for_good(world: dict[str, Any]) -> None:
    scripted: ScriptedProvider = world["provider"]
    scripted.route(
        MARKER,
        [turn(call("message_owner", f"m{i}", text=str(i))) for i in range(3)]
        + [turn(call("finish", "f", outcome="failed", summary="messaged the owner three times"))],
    )

    class DeadPro:
        def __init__(self) -> None:
            self.models: list[str] = []

        async def complete(self, request: Any) -> Any:
            self.models.append(request.model)
            if request.model == "fake-pro":
                raise TimeoutError("down")
            return await scripted.complete(request)

    dead = DeadPro()
    world["provider"] = dead
    await asyncio.wait_for(runner(world).run(), timeout=30)
    assert dead.models == ["fake-pro", "fake-flash"] * 3 + ["fake-flash"]


async def test_timeouts_are_not_retried_when_the_caller_switches_models() -> None:
    from rework.kernel.llm.adapters.retry import with_retries

    calls = 0

    class Stall:
        name = "stall"

        async def complete(self, request: Any) -> Any:
            nonlocal calls
            calls += 1
            raise TimeoutError("stalled")

    with pytest.raises(TimeoutError):
        await with_retries(Stall(), backoff_s=0, retry_timeouts=False).complete(None)  # type: ignore[arg-type]
    assert calls == 1
    with pytest.raises(TimeoutError):
        await with_retries(Stall(), backoff_s=0).complete(None)  # type: ignore[arg-type]
    assert calls == 4


async def test_an_outage_waits_instead_of_failing_the_mission(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Found live: a network drop made every model (all on one provider) fail
    with a connection error, and the mission failed. A transient failure of
    every model now waits and retries from the owner's model."""
    from rework.orchestrator import agent

    monkeypatch.setattr(agent, "OUTAGE_WAITS_S", (0.01, 0.01, 0.01))
    scripted: ScriptedProvider = world["provider"]
    scripted.route(
        MARKER,
        [turn(call("finish", "f", outcome="failed", summary="waited out the outage"))],
    )

    class Outage:
        def __init__(self) -> None:
            self.models: list[str] = []

        async def complete(self, request: Any) -> Any:
            self.models.append(request.model)
            if len(self.models) <= 6:  # two full rounds of the 3-model catalog
                raise ConnectionError("Connection error.")
            return await scripted.complete(request)

    outage = Outage()
    world["provider"] = outage
    await asyncio.wait_for(runner(world).run(), timeout=30)
    assert outage.models[6] == "fake-pro", "after the wait it starts from the chosen model"
    msgs = await world["store"].chat_messages(orchestrator_chat_id("mo"))
    assert any(tc.get("name") == "finish" for m in msgs for tc in (m.body.get("tool_calls") or []))


async def test_a_long_outage_still_fails(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from rework.orchestrator import agent

    monkeypatch.setattr(agent, "OUTAGE_WAITS_S", (0.01,))

    class Down:
        async def complete(self, request: Any) -> Any:
            raise ConnectionError("Connection error.")

    world["provider"] = Down()
    await asyncio.wait_for(runner(world).run(), timeout=30)
    mission = await world["store"].get_mission("mo")
    assert mission is not None and (mission.result or {}).get("outcome") == "failed"


# -- audit rails: strictness, the fresh-audit rail, findings acceptance -------

AUDIT_MEDIUM = json.dumps(
    {
        "summary": "One real gap and one unproven risk: the zero-results path is untested.",
        "findings": [
            {
                "title": "No test for the zero-results path",
                "detail": "fetch_all() returns [] but nothing exercises it.",
                "evidence": ["calc.py:2"],
                "severity": "medium",
                "verdict": "CONFIRMED",
                "confidence": 0.8,
            },
            {
                "title": "Zero results may crash the caller",
                "detail": "The caller iterates the result without a None check.",
                "evidence": ["calc.py:3"],
                "severity": "high",
                "verdict": "PLAUSIBLE",
                "confidence": 0.5,
            },
        ],
        "standing_assessments": {
            "proportionality": "proportionate",
            "complexity": "trivial, appropriate",
            "priority": "appropriate",
            "coverage": "gap: zero-results path untested",
            "business_logic_risk": "caller may crash on empty input",
        },
        "warnings": [],
        "blockers": [],
    }
)


async def test_strictness_reaches_the_auditor_prompt(world: dict[str, Any]) -> None:
    """The orchestrator's per-round strictness is not a dead constant: it is
    interpolated into the auditor's rendered system prompt, and the levels are
    actually different text."""
    from rework.roles.builtin.auditor import SEVERITY_RUBRIC, STRICTNESS_PROMPTS

    assert len({STRICTNESS_PROMPTS[k] for k in ("light", "standard", "strict")}) == 3
    provider: ScriptedProvider = world["provider"]
    store: SqliteStore = world["store"]
    provider.route(
        MARKER,
        [
            turn(
                call(
                    "spawn_role",
                    "u1",
                    role="auditor",
                    step="audit",
                    brief="review the diff",
                    strictness="strict",
                ),
                call("wait", "u2"),
            ),
            turn(
                call(
                    "spawn_role",
                    "u3",
                    role="auditor",
                    step="audit-light",
                    brief="quick look",
                    strictness="light",
                ),
                call("wait", "u4"),
            ),
            turn(call("finish", "u5", outcome="failed", summary="audited in both modes only")),
        ],
    )
    provider.route("Find issues", _auditor_script(AUDIT_CLEAN) * 2)
    await asyncio.wait_for(runner(world).run(), timeout=30)

    strict_run = await store.get_run("mo:audit:auditor:1")
    light_run = await store.get_run("mo:audit-light:auditor:1")
    assert strict_run is not None and strict_run.context.get("strictness") == "strict"
    assert light_run is not None and light_run.context.get("strictness") == "light"
    systems = [
        r.system or ""
        for r in provider.requests
        if "REVIEW MODE (" in (r.system or "") and MARKER not in (r.system or "")
    ]
    assert any(
        "REVIEW MODE (strict)" in sys and STRICTNESS_PROMPTS["strict"] in sys for sys in systems
    ), "the strict mode text reached the prompt"
    assert any(
        "REVIEW MODE (light)" in sys and STRICTNESS_PROMPTS["light"] in sys for sys in systems
    ), "a light round renders its own text"
    assert all(SEVERITY_RUBRIC in sys for sys in systems), "the severity rubric is interpolated"
    assert all(
        STRICTNESS_PROMPTS["strict"] not in sys for sys in systems if "REVIEW MODE (light)" in sys
    ), "the modes do not bleed into each other"


async def test_finish_done_is_refused_without_a_fresh_audit(world: dict[str, Any]) -> None:
    """The one mechanical rail: what ships must have been audited. No audit at
    all, and a coder that ran after the last completed audit, both refuse."""
    provider: ScriptedProvider = world["provider"]
    store: SqliteStore = world["store"]
    provider.route(
        MARKER,
        [
            turn(
                call("spawn_role", "n1", role="coder", step="code", brief="fix add()"),
                call("wait", "n2"),
            ),
            turn(
                call("finish", "n3", outcome="done", summary="shipping the add fix")
            ),  # no audit yet
            turn(
                call("spawn_role", "n4", role="auditor", step="audit", brief="review"),
                call("wait", "n5"),
            ),
            turn(
                call("spawn_role", "n6", role="coder", step="code", brief="one more change"),
                call("wait", "n7"),
            ),
            turn(
                call("finish", "n8", outcome="done", summary="shipping the add fix again")
            ),  # unaudited
            turn(call("finish", "n9", outcome="failed", summary="stopped, nothing ships")),
        ],
    )
    provider.route("Fulfill the task", _coder_script("first") + _coder_script("second"))
    provider.route("Find issues", _auditor_script(AUDIT_CLEAN))
    await asyncio.wait_for(runner(world).run(), timeout=30)

    out = [
        m.body.get("content", "")
        for m in await store.chat_messages(orchestrator_chat_id("mo"))
        if m.role == "tool"
    ]
    assert any("no auditor has completed" in t for t in out)
    assert any("ran after the last completed audit" in t for t in out)
    mission = await store.get_mission("mo")
    assert mission is not None and mission.status == MissionStatus.FAILED, "it never shipped"


async def test_finish_done_needs_every_open_finding_accepted(world: dict[str, Any]) -> None:
    """A medium+ finding blocks done unless accepted_findings names it with a
    reason: no acceptance refused, partial acceptance refused, full accepted."""
    provider: ScriptedProvider = world["provider"]
    store: SqliteStore = world["store"]
    full = [
        {
            "finding": "No test for the zero-results path",
            "reason": "separate coverage issue filed; the branch itself does not regress",
        },
        {
            "finding": "Zero results may crash the caller",
            "reason": "caller is the CLI which guards empty input already; verified by hand",
        },
    ]
    provider.route(
        MARKER,
        [
            turn(
                call("spawn_role", "q1", role="coder", step="code", brief="fix add()"),
                call("wait", "q2"),
            ),
            turn(
                call("spawn_role", "q3", role="auditor", step="audit", brief="review"),
                call("wait", "q4"),
            ),
            turn(
                call("finish", "q5", outcome="done", summary="shipping the add fix")
            ),  # no acceptance
            turn(
                call(
                    "finish",
                    "q6",
                    outcome="done",
                    summary="shipping the add fix",
                    accepted_findings=[full[0]],
                )
            ),  # partial
            turn(
                call(
                    "finish",
                    "q7",
                    outcome="done",
                    summary="shipping with both findings accepted on purpose",
                    accepted_findings=full,
                )
            ),
        ],
    )
    provider.route("Fulfill the task", _coder_script("tests pass"))
    provider.route("Find issues", _auditor_script(AUDIT_MEDIUM))
    task = asyncio.create_task(runner(world).run())
    # the accepted finish proceeds to the mission's ship gate; approve it
    for _ in range(200):
        gates = await store.list_open_gates("mo")
        if gates:
            second = await open_sqlite(world["db"])
            try:
                await GateManager(second, InProcessEventBus(second)).decide(
                    gates[0].id, approved=True
                )
            finally:
                await second.aclose()
            break
        await asyncio.sleep(0.05)
    await asyncio.wait_for(task, timeout=30)

    out = [
        m.body.get("content", "")
        for m in await store.chat_messages(orchestrator_chat_id("mo"))
        if m.role == "tool"
    ]
    assert any(
        "accepted_findings does not cover" in t and "No test for the zero-results path" in t
        for t in out
    ), "unaccepted findings refuse finish(done)"
    assert any(
        "accepted_findings does not cover" in t and "Zero results may crash the caller" in t
        for t in out
    ), "a partially covered audit still refuses"
    assert any(t.strip() == "finished" for t in out), "full acceptance is allowed through"
    mission = await store.get_mission("mo")
    assert mission is not None
    assert (mission.result or {}).get("accepted_findings") == full, (
        "the reasons travel into the mission result (report/PR data)"
    )


async def finish_turn(orch: Any, cid: str, **args: Any) -> str:
    """One orchestrator turn that calls finish, recorded the way `_think` does
    (the assistant message with its tool_calls, then the tool reply), and the
    reply text back."""
    await orch._append(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": cid, "name": "finish", "arguments": args}],
        }
    )
    await orch._execute_calls([call("finish", cid, **args)])
    reply = orch.history[-1]
    assert reply.get("tool_call_id") == cid
    return str(reply.get("content") or "")


async def test_a_status_note_summary_is_refused_so_the_mission_stays_open(
    world: dict[str, Any],
) -> None:
    """Found live (PR #34): the orchestrator finished the mission with
    "FINISHED ALREADY.", and that status note became the whole PR body. With no
    earlier substantive summary to fall back on, the rail refuses it."""
    from rework.orchestrator.agent import MissionOrchestrator

    orch = MissionOrchestrator(runner(world), world["mission"])
    await orch._load()
    reply = await finish_turn(orch, "x1", outcome="failed", summary="FINISHED ALREADY.")

    assert reply.startswith("error:"), reply
    assert "summary must say what changed for the owner" in reply
    assert orch.finished is None, "a status note cannot end the mission"


async def test_a_blank_summary_is_refused(world: dict[str, Any]) -> None:
    from rework.orchestrator.agent import MissionOrchestrator

    orch = MissionOrchestrator(runner(world), world["mission"])
    await orch._load()
    reply = await finish_turn(orch, "b1", outcome="failed", summary="   ")

    assert reply.startswith("error:"), reply
    assert "summary must say what changed for the owner" in reply
    assert orch.finished is None


async def test_a_second_finish_keeps_the_summary_the_first_one_gave(
    world: dict[str, Any],
) -> None:
    """The PR #34 scenario: a restart re-ran the finish with a status note. The
    recorded summary is the earlier substantive one, not the status note."""
    from rework.orchestrator.agent import MissionOrchestrator

    first = "fixed the reload bug so re-exec keeps the shell alive"
    orch = MissionOrchestrator(runner(world), world["mission"])
    await orch._load()
    assert await finish_turn(orch, "d1", outcome="failed", summary=first) == "finished"
    assert orch.finished == ("failed", first)

    # the daemon restarted: the mission comes back up, reads the finish it had
    # already recorded out of the persisted history (_load does that), and only
    # then re-runs the finish with a status note (PR #34)
    restarted = MissionOrchestrator(runner(world), world["mission"])
    await restarted._load()
    assert restarted._resumed_finish_summary == first, "the pre-restart summary was captured"
    restarted.finished = restarted._recorded_finish()  # what run() does on a reloaded history
    assert restarted.finished == ("failed", first), "the restart resumes the recorded finish"
    restarted.finished = None  # ...and then re-finished the mission
    assert await finish_turn(restarted, "d2", outcome="failed", summary="FINISHED ALREADY.") == (
        "finished"
    )
    assert restarted.finished == ("failed", first), "the status note did not overwrite the story"


async def test_a_thin_finish_from_this_run_is_not_promoted_by_a_later_one(
    world: dict[str, Any],
) -> None:
    """The rail's reuse is bounded by the restart boundary. A finish accepted
    earlier in THIS run is an intermediate note, not a mission summary: when the
    orchestrator later finishes with "FINISHED ALREADY.", the thin middle note
    must not become the PR body -- the finish is refused instead."""
    from rework.orchestrator.agent import MissionOrchestrator

    thin = "shipping the add fix"
    orch = MissionOrchestrator(runner(world), world["mission"])
    await orch._load()
    assert orch._resumed_finish_summary is None, "nothing was recorded before this run"
    assert await finish_turn(orch, "i1", outcome="failed", summary=thin) == "finished"
    assert orch.finished == ("failed", thin)

    reply = await finish_turn(orch, "i2", outcome="failed", summary="FINISHED ALREADY.")
    assert reply.startswith("error:"), reply
    assert "summary must say what changed for the owner" in reply
    assert orch.finished == ("failed", thin), "the refused finish changed nothing"


async def test_a_short_but_descriptive_summary_finishes_the_mission(
    world: dict[str, Any],
) -> None:
    """The rail must not be a length rule: 24 characters that name the change
    are accepted all the way to the shipped mission result (and the 53-char
    ORCH_SUMMARY of tests/integration/test_open_pr.py stays well clear)."""
    provider: ScriptedProvider = world["provider"]
    store: SqliteStore = world["store"]
    summary = "add() now adds correctly"
    provider.route(
        MARKER,
        [
            turn(
                call("spawn_role", "w1", role="auditor", step="audit", brief="review the branch"),
                call("wait", "w2"),
            ),
            turn(call("finish", "w3", outcome="done", summary=summary)),
        ],
    )
    provider.route("Find issues", _auditor_script(AUDIT_CLEAN))
    task = asyncio.create_task(runner(world).run())
    await approve_next_gate(world)
    await asyncio.wait_for(task, timeout=30)

    mission = await store.get_mission("mo")
    assert mission is not None and mission.status == MissionStatus.DONE
    assert (mission.result or {}).get("summary") == summary


def test_the_summary_rail_tells_status_notes_from_real_changes() -> None:
    from rework.orchestrator.agent import _reads_like_status_note, _substantive_summary

    for note in ("FINISHED ALREADY.", "done!", "all good", "  "):
        assert not _substantive_summary(note), note
        assert _reads_like_status_note(note) or not note.strip(), note
    for real in (
        "add() now adds correctly",
        "ORCHESTRATOR_FINISH_SUMMARY: add() now adds correctly",
        "fixed the reload bug so re-exec keeps the shell alive",
    ):
        assert _substantive_summary(real), real
        assert not _reads_like_status_note(real), real


async def test_pr_body_describes_the_mission_not_the_last_coder_line(
    world: dict[str, Any],
) -> None:
    """Found live: PR #16 described a variable rename while the mission was much
    bigger. The body's primary description is the finish summary + the audit;
    the coder handoff is a supplementary appendix."""
    from rework.kernel.state.records import Run

    audit = Run(
        id="mo:audit:auditor:1",
        mission_id="mo",
        role="auditor",
        step="audit",
        artifact=json.loads(AUDIT_CLEAN),
    )
    body = runner(world)._pr_body(
        world["mission"],
        {"summary": "renamed a variable"},
        audit,
        finish_summary="Moved the audit gate into the orchestrator and added accepted_findings.",
        accepted_findings=[
            {"finding": "No test for the zero-results path", "reason": "follow-up issue filed"}
        ],
    )
    primary = body.split("<details>")[0]
    assert "Moved the audit gate into the orchestrator" in primary
    assert "renamed a variable" not in primary, "the trivial coder line is not the story"
    assert "Verdict: **approve**" in primary, "the audit is part of the primary description"
    assert "<details>" in body and "renamed a variable" in body, "kept as an appendix"
    assert "Accepted findings (shipped knowingly)" in body
    assert "follow-up issue filed" in body


async def test_accepted_findings_survive_a_restart_at_the_gate(world: dict[str, Any]) -> None:
    """A mission that shipped with findings accepted knowingly, and then
    restarted while it waited at its final gate, must come back with the SAME
    accepted reasons and reach the same decision without spending another model
    call. (The recorded finish is re-honored; the audit-fresh rail is not
    re-litigated into a different answer.)

    Flaked on CI (3.12 and 3.13) before the shared barrier existed. Reproduce
    the pre-fix failure mode under CPU load:

        for i in 1 2 3 4 5 6; do
            pytest tests/integration/test_orchestrator_agent.py -q &
        done; wait

    The old wait returned as soon as a gate row existed. Ship gate row and
    mission status AWAITING_GATE are two commits (`engine._final_gates`), so
    under load this test cancelled the runner inside that window and
    `before.status == AWAITING_GATE` below read `running`. Now the wait is
    conftest.wait_for_gate_park: gate open AND AWAITING_GATE in one bounded poll."""
    provider: ScriptedProvider = world["provider"]
    store: SqliteStore = world["store"]
    full = [
        {
            "finding": "No test for the zero-results path",
            "reason": "separate coverage issue filed; the branch itself does not regress",
        },
        {
            "finding": "Zero results may crash the caller",
            "reason": "caller is the CLI which guards empty input already; verified by hand",
        },
    ]
    provider.route(
        MARKER,
        [
            turn(
                call("spawn_role", "v1", role="coder", step="code", brief="fix add()"),
                call("wait", "v2"),
            ),
            turn(
                call("spawn_role", "v3", role="auditor", step="audit", brief="review the fix"),
                call("wait", "v4"),
            ),
            turn(
                call(
                    "finish",
                    "v5",
                    outcome="done",
                    summary="shipped with two findings accepted",
                    accepted_findings=full,
                )
            ),
        ],
    )
    provider.route("Fulfill the task", _coder_script("tests pass"))
    provider.route("Find issues", _auditor_script(AUDIT_MEDIUM))

    first = asyncio.create_task(runner(world).run())
    # the accepted finish is let through the rail and reaches the ship gate; the
    # kill lands while the mission waits there. BARRIER: the gate row and the
    # AWAITING_GATE status are two writes, so wait for both (see
    # conftest.wait_for_gate_park and the comment in engine._final_gates)
    await wait_for_gate_park(
        store, "mo", mission_status=MissionStatus.AWAITING_GATE, what="ship gate before the kill"
    )
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    before = await store.get_mission("mo")
    assert before is not None and before.status == MissionStatus.AWAITING_GATE
    # the recorded finish is in the conversation with its accepted reasons
    calls_before = len(provider.requests)
    second = asyncio.create_task(runner(world).run())
    await approve_next_gate(world)
    await asyncio.wait_for(second, timeout=30)

    mission = await store.get_mission("mo")
    assert mission is not None and mission.status == MissionStatus.DONE
    assert (mission.result or {}).get("accepted_findings") == full, (
        "the owner's recorded reasons survive a restart unchanged"
    )
    assert (mission.result or {}).get("summary") == "shipped with two findings accepted"
    assert len(provider.requests) == calls_before, "a recorded finish costs no model call"

    # the rail is still computed the same way on the reloaded state: a fresh
    # session over the same store re-derives the acceptance decision identically
    from rework.orchestrator.agent import MissionOrchestrator

    orch = MissionOrchestrator(runner(world), mission)
    await orch._load()
    assert orch._recorded_finish() == ("done", "shipped with two findings accepted")
    assert orch.accepted_findings == full
    await orch._enforce_audit_fresh()  # audited, and nothing ran after the audit
    await orch._enforce_findings_accepted(orch.accepted_findings)  # still fully covered


async def test_a_coder_that_finished_after_the_audit_started_is_unaudited(
    world: dict[str, Any],
) -> None:
    """A coder started BEFORE the last audit but finishing AFTER it began (the
    two ran in parallel) changed the branch after the auditor looked: what the
    rail compares is the coder's finish against the audit's start."""
    from rework.kernel.state.records import Run
    from rework.orchestrator.agent import MissionOrchestrator, ToolError

    store: SqliteStore = world["store"]
    t0 = 1_000_000.0
    await store.create_run(
        Run(
            id="mo:code:coder:1",
            mission_id="mo",
            role="coder",
            step="code",
            status=RunStatus.COMPLETED,
            created_at=t0,
            updated_at=t0 + 300,
        )
    )
    await store.create_run(
        Run(
            id="mo:audit:auditor:1",
            mission_id="mo",
            role="auditor",
            step="audit",
            status=RunStatus.COMPLETED,
            created_at=t0 + 100,
            updated_at=t0 + 200,
        )
    )
    orch = MissionOrchestrator(runner(world), world["mission"])
    with pytest.raises(ToolError, match="after the last completed audit"):
        await orch._enforce_audit_fresh()


async def _seed_runs(store: SqliteStore, *rows: tuple[str, str, RunStatus, float, float]) -> None:
    from rework.kernel.state.records import Run

    for role, n, status, created, updated in rows:
        await store.create_run(
            Run(
                id=f"mo:{role}:{role}:{n}",
                mission_id="mo",
                role=role,
                step=role,
                status=status,
                created_at=created,
                updated_at=updated,
            )
        )


async def test_audit_rail_still_refuses_a_completed_coder_with_no_audit(
    world: dict[str, Any],
) -> None:
    """(a) a completed coder and no completed audit is refused, and a surveyor
    in the same mission does not excuse it."""
    from rework.orchestrator.agent import MissionOrchestrator, ToolError

    store: SqliteStore = world["store"]
    t0 = 1_000_000.0
    await _seed_runs(
        store,
        ("coder", "1", RunStatus.COMPLETED, t0, t0 + 10),
        ("surveyor", "1", RunStatus.COMPLETED, t0 + 20, t0 + 30),
    )
    orch = MissionOrchestrator(runner(world), world["mission"])
    with pytest.raises(ToolError, match="no auditor has completed"):
        await orch._enforce_audit_fresh()


@pytest.mark.parametrize("status", [RunStatus.FAILED, RunStatus.ABORTED, RunStatus.RUNNING])
async def test_audit_rail_refuses_a_coder_that_did_not_complete_plus_a_surveyor(
    world: dict[str, Any], status: RunStatus
) -> None:
    """(b) a coder that ran but failed, timed out or is still running, plus a
    surveyor, in a PUBLISHING mission (the world plan is `fix`): still refused.
    The surveyor exemption exists only when no coder run exists at all."""
    from rework.orchestrator.agent import MissionOrchestrator, ToolError

    store: SqliteStore = world["store"]
    assert world["mission"].plan["gates"] == ["ship"], "this mission is a gated, shipping plan"
    t0 = 1_000_000.0
    await _seed_runs(
        store,
        ("coder", "1", status, t0, t0 + 10),
        ("surveyor", "1", RunStatus.COMPLETED, t0 + 20, t0 + 30),
    )
    orch = MissionOrchestrator(runner(world), world["mission"])
    with pytest.raises(ToolError, match="no auditor has completed"):
        await orch._enforce_audit_fresh()


async def test_audit_rail_refuses_a_failed_coder_that_ran_after_the_last_audit(
    world: dict[str, Any],
) -> None:
    """A coder that failed AFTER the last completed audit began may still have
    left edits behind: completion status does not matter."""
    from rework.orchestrator.agent import MissionOrchestrator, ToolError

    store: SqliteStore = world["store"]
    t0 = 1_000_000.0
    await _seed_runs(
        store,
        ("auditor", "1", RunStatus.COMPLETED, t0, t0 + 10),
        ("coder", "1", RunStatus.FAILED, t0 + 5, t0 + 50),
    )
    orch = MissionOrchestrator(runner(world), world["mission"])
    with pytest.raises(ToolError, match="after the last completed audit"):
        await orch._enforce_audit_fresh()


async def _use_plan(world: dict[str, Any], playbook: str) -> Any:
    """Re-plan the `mo` mission as `playbook` (what the rail reads from the store)."""
    from rework.missions import orchestrated_plan

    plan = orchestrated_plan(playbook, "pro")
    await world["store"].update_mission("mo", plan=plan)
    return await world["store"].get_mission("mo")


def test_plan_allows_unaudited_done_predicate() -> None:
    from rework.missions import PLAN_FIX, PLAN_FIX_PR, orchestrated_plan
    from rework.orchestrator.agent import _plan_allows_unaudited_done as ok

    assert ok(orchestrated_plan("audit", "pro"))
    # draft has no gates, no publish and no coder step but writes files: never exempt
    assert not ok(orchestrated_plan("draft", "pro"))
    assert not ok(orchestrated_plan("echo", "pro"))
    assert not ok({"steps": [], "gates": [], "publish": ""})  # no playbook stamp
    # the audit playbook is exempt only while it stays pure
    assert not ok({**orchestrated_plan("audit", "pro"), "gates": ["ship"]})
    assert not ok({**orchestrated_plan("audit", "pro"), "publish": "pr"})
    assert not ok({**orchestrated_plan("audit", "pro"), "steps": [{"role": "coder"}]})
    assert not ok(orchestrated_plan("fix", "pro"))  # gated
    assert not ok(orchestrated_plan("fix-pr", "pro"))  # publishes
    assert not ok(dict(PLAN_FIX)) and not ok(dict(PLAN_FIX_PR))  # coder step
    assert not ok({"steps": [{"role": "coder"}], "gates": [], "publish": ""})
    assert not ok({"gates": ["ship"]}) and not ok({"publish": "pr"})


async def test_audit_rail_allows_a_pure_audit_mission(world: dict[str, Any]) -> None:
    """(c) investigator + surveyor, no coder run at all, in a plan that cannot
    change or ship anything (the `audit` playbook): nothing to audit."""
    from rework.orchestrator.agent import MissionOrchestrator

    mission = await _use_plan(world, "audit")
    assert mission.plan["playbook"] == "audit"  # what the exemption keys on
    assert mission.plan["gates"] == [] and not mission.plan["publish"], (
        "the plan shape it relies on"
    )
    t0 = 1_000_000.0
    await _seed_runs(
        world["store"],
        ("investigator", "1", RunStatus.COMPLETED, t0, t0 + 10),
        ("surveyor", "1", RunStatus.COMPLETED, t0 + 20, t0 + 30),
    )
    orch = MissionOrchestrator(runner(world), mission)
    await orch._enforce_audit_fresh()  # allowed


@pytest.mark.parametrize("playbook", ["fix", "fix-pr", "draft"])
async def test_audit_rail_refuses_a_surveyor_only_run_on_a_gated_or_publishing_plan(
    world: dict[str, Any], playbook: str
) -> None:
    """A surveyor spawned instead of a coder must not let a `fix` (gated) or
    `fix-pr` (publishing) mission finish(done) with nothing audited, nor a
    `draft` one (no gates, no publish, but it writes files into the project)."""
    from rework.orchestrator.agent import MissionOrchestrator, ToolError

    mission = await _use_plan(world, playbook)
    assert mission.plan["playbook"] == playbook
    t0 = 1_000_000.0
    await _seed_runs(
        world["store"],
        ("investigator", "1", RunStatus.COMPLETED, t0, t0 + 10),
        ("surveyor", "1", RunStatus.COMPLETED, t0 + 20, t0 + 30),
    )
    orch = MissionOrchestrator(runner(world), mission)
    with pytest.raises(ToolError, match="no auditor has completed"):
        await orch._enforce_audit_fresh()


async def test_audit_rail_refuses_a_mission_with_neither_coder_nor_completed_survey(
    world: dict[str, Any],
) -> None:
    """The exemption needs a COMPLETED surveyor: an investigator alone, or a
    failed survey, does not let finish(done) through."""
    from rework.orchestrator.agent import MissionOrchestrator, ToolError

    mission = await _use_plan(world, "audit")  # the plan allows it: only the runs refuse
    store: SqliteStore = world["store"]
    t0 = 1_000_000.0
    await _seed_runs(
        store,
        ("investigator", "1", RunStatus.COMPLETED, t0, t0 + 10),
        ("surveyor", "1", RunStatus.FAILED, t0 + 20, t0 + 30),
    )
    orch = MissionOrchestrator(runner(world), mission)
    with pytest.raises(ToolError, match="no auditor has completed"):
        await orch._enforce_audit_fresh()


# -- a reply cut off at the output limit is not idleness ----------------------


SUMMARY = (
    "Retries a cut-off thinking reply with more room instead of counting it idle, "
    "so the mission reaches its tool call."
)


def cut_off() -> ChatResult:
    """A reply that spent its whole output budget thinking: no text, no tool call."""
    return ChatResult(
        text="",
        thinking_blocks=[{"type": "thinking", "thinking": "hmm" * 50, "signature": "s"}],
        stop_reason="max_tokens",
        usage=Usage(input_tokens=500, output_tokens=4096),
    )


async def set_orchestrator_effort(world: dict[str, Any], effort: str) -> None:
    plan = orchestrated_plan("fix", "pro", orchestrator_effort=effort)
    world["mission"] = await world["store"].update_mission("mo", plan=plan)


async def test_a_cut_off_thinking_reply_is_retried_not_counted_idle(
    world: dict[str, Any],
) -> None:
    """Found live: with an effort set, thinking ate the whole 4096-token cap three
    turns running, and the mission failed with 'orchestrator stopped calling tools'."""
    await set_orchestrator_effort(world, "high")
    provider: ScriptedProvider = world["provider"]
    provider.route(
        MARKER,
        [cut_off(), turn(call("finish", "f", outcome="failed", summary=SUMMARY))],
    )
    await asyncio.wait_for(runner(world).run(), timeout=30)
    mission = await world["store"].get_mission("mo")
    result = (mission.result if mission else None) or {}
    assert result.get("summary") == SUMMARY
    assert "stopped calling tools" not in str(result)
    first, second = provider.requests
    assert first.effort == "high" and second.effort == "high"
    assert first.max_tokens >= 16_384, "the turn budget is no longer capped at 4096"
    assert second.max_tokens > first.max_tokens, "the retry gets a larger budget"
    notes = [
        e.payload.get("note", "")
        for e in await world["store"].events_since(0, mission_id="mo")
        if "cut off" in str(e.payload.get("note", ""))
    ]
    assert len(notes) == 1 and "high" in notes[0] and str(second.max_tokens) in notes[0]


async def test_three_cut_offs_in_a_row_still_reach_the_tool_call(world: dict[str, Any]) -> None:
    await set_orchestrator_effort(world, "high")
    provider: ScriptedProvider = world["provider"]
    provider.route(
        MARKER,
        [
            cut_off(),
            cut_off(),
            cut_off(),
            turn(call("finish", "f", outcome="failed", summary=SUMMARY)),
        ],
    )
    await asyncio.wait_for(runner(world).run(), timeout=30)
    mission = await world["store"].get_mission("mo")
    result = (mission.result if mission else None) or {}
    assert result.get("summary") == SUMMARY
    efforts = [r.effort for r in provider.requests]
    # 1st try, retry with a bigger budget, retry at a lower effort, then the idle nudge
    assert efforts == ["high", "high", "medium", "medium"]
    budgets = [r.max_tokens for r in provider.requests]
    assert budgets[1] > budgets[0] and budgets[2] == budgets[1]


async def test_cut_off_overrides_do_not_leak_into_later_turns(world: dict[str, Any]) -> None:
    await set_orchestrator_effort(world, "high")
    provider: ScriptedProvider = world["provider"]
    provider.route(
        MARKER,
        [
            cut_off(),
            turn(call("message_owner", "m", text="hi")),
            turn(call("finish", "f", outcome="failed", summary=SUMMARY)),
        ],
    )
    await asyncio.wait_for(runner(world).run(), timeout=30)
    first, later = provider.requests[0], provider.requests[2]
    assert (later.effort, later.max_tokens) == (first.effort, first.max_tokens)


async def test_endless_cut_offs_terminate_as_a_failure(world: dict[str, Any]) -> None:
    await set_orchestrator_effort(world, "high")
    provider: ScriptedProvider = world["provider"]
    provider.route(MARKER, [cut_off() for _ in range(12)])
    await asyncio.wait_for(runner(world).run(), timeout=30)
    mission = await world["store"].get_mission("mo")
    result = (mission.result if mission else None) or {}
    assert result.get("outcome") == "failed"
    assert "stopped calling tools" in str(result)
    # 2 bounded retries, then the 3 idle strikes: bounded, not a loop
    assert len(provider.requests) == 5
