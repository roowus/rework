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
            turn(call("finish", "a5", outcome="failed", summary="investigation only")),
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
            turn(call("finish", "b5", outcome="failed", summary="owner chose")),
        ],
    )
    provider.route("Investigate any brief", asking_investigator("old or new parser?"))
    task = asyncio.create_task(runner(world).run())
    gate = None
    for _ in range(300):
        gates = await store.list_open_gates("mo")
        if gates:
            gate = gates[0]
            break
        await asyncio.sleep(0.05)
    assert gate is not None and gate.kind.value == "choice"
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
            turn(call("finish", "r5", outcome="failed", summary="done")),
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
            turn(call("finish", "v5", outcome="failed", summary="two coders")),
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
            turn(call("finish", "t3", outcome="failed", summary="budgeted")),
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

    provider.route("Investigate any brief", [*reads[:3], ChatResult(text=INV_REPORT)])
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
                turn(call("finish", "s3", outcome="done", summary="fixed")),
            ],
        )
        provider.route("Fulfill the task", _coder_script("tests pass"))
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
