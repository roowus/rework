"""The open_pr gate: branch push to origin (a local bare remote stands in for
GitHub) + mechanical PR creation through a fake gh binary. This is the
self-hosting acceptance in test form."""

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
)
from rework.kernel.events.bus import InProcessEventBus
from rework.kernel.gates import GateManager
from rework.kernel.state.records import Mission, MissionStatus, Project
from rework.kernel.state.sqlite import SqliteStore, open_sqlite
from rework.missions import PLAN_FIX_PR
from rework.orchestrator.engine import MissionRunner
from rework.roles.registry import autodiscover


@pytest.fixture
def pr_env(tmp_path: Path) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    """Mission repo with origin = a bare remote; a fake gh that records calls."""
    source = Path(__file__).parents[2] / "examples" / "toyrepo"
    dest = tmp_path / "mission" / "toyrepo"
    dest.mkdir(parents=True)
    for f in source.iterdir():
        if f.is_file():
            shutil.copy(f, dest / f.name)
    bare = tmp_path / "origin.git"
    bare.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "--bare", "-b", "main"], cwd=bare, check=True, capture_output=True
    )
    subprocess.run(["git", "init", "-b", "main"], cwd=dest, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=dest, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=r@r", "-c", "user.name=r", "commit", "-m", "init"],
        cwd=dest,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", str(bare)], cwd=dest, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "push", "-u", "origin", "main"], cwd=dest, check=True, capture_output=True
    )

    fake_gh = tmp_path / "fake-gh"
    fake_gh.write_text(
        "#!/bin/sh\n"
        'echo "$@" >> ' + str(tmp_path / "gh-calls.log") + "\n"
        "echo https://github.com/roowus/toyrepo/pull/1\n"
    )
    fake_gh.chmod(0o755)
    return {
        "repo": dest,
        "bare": bare,
        "fake_gh": str(fake_gh),
        "gh_log": tmp_path / "gh-calls.log",
        "tmp": tmp_path,
    }


@pytest.fixture
async def world(pr_env: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:  # type: ignore[no-untyped-def]
    store: SqliteStore = await open_sqlite(str(pr_env["tmp"] / "pr.sqlite3"))
    try:
        objective = "write docs: add a line to README saying hello from rework"
        plan = json.loads(json.dumps(PLAN_FIX_PR).replace("$OBJECTIVE", objective))
        mission = await store.create_mission(
            Mission(id="mpr", objective=objective, repo_path=str(pr_env["repo"]), plan=plan)
        )
        provider = ScriptedProvider()
        provider.route("Investigate any brief", _investigator_script())
        provider.route("Fulfill the task", _coder_script("docs line added, tests still green"))
        provider.route("Find issues", _auditor_script(AUDIT_CLEAN))
        yield {
            "store": store,
            "provider": provider,
            "mission": mission,
            "ck": str(pr_env["tmp"] / "ck.sqlite3"),
            "workroot": pr_env["tmp"] / "work",
            "registry": autodiscover(),
            "catalog": role_catalog(),
            "tmp": pr_env["tmp"],
            "fake_gh": pr_env["fake_gh"],
            "gh_log": pr_env["gh_log"],
            "bare": pr_env["bare"],
        }
    finally:
        await store.aclose()


async def test_fix_pr_pushes_and_creates_pr_without_a_gate(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fix-pr mission publishes its branch and PR with no human gate: the
    audit rail already guarantees a clean, reviewed result."""
    monkeypatch.setenv("REWORK_GH_BIN", str(world["fake_gh"]))
    store: SqliteStore = world["store"]
    runner = MissionRunner(
        world["mission"].id,
        store,
        InProcessEventBus(store),
        world["registry"],
        world["catalog"],
        _ck_factory(world["ck"]),
        world["workroot"],
        poll_interval_s=0.05,
        provider=world["provider"],
    )
    await asyncio.wait_for(asyncio.create_task(runner.run()), timeout=30)
    assert await store.list_gates(world["mission"].id) == []
    mission = await store.get_mission(world["mission"].id)
    assert mission is not None and mission.status == MissionStatus.DONE
    assert (mission.result or {}).get("pr_url") == "https://github.com/roowus/toyrepo/pull/1"

    # the branch really reached the bare remote (origin)
    heads_proc = await asyncio.create_subprocess_exec(
        "git",
        "branch",
        "--list",
        "*mpr*",
        cwd=str(world["bare"]),
        stdout=asyncio.subprocess.PIPE,
    )
    heads_out, _ = await heads_proc.communicate()
    assert b"mpr" in heads_out
    # the fake gh got the PR call with head + base
    gh_log = Path(str(world["gh_log"])).read_text()  # noqa: ASYNC240 (tiny test read)
    assert "pr create" in gh_log and "--head" in gh_log


def _ck_factory(ck_path: str):  # type: ignore[no-untyped-def]
    from rework.orchestrator.daemon import sqlite_checkpointer_factory

    return sqlite_checkpointer_factory(ck_path)


# ---------------------------------------------------------------------------
# FIX-PR-21: PR body must lead with the orchestrator's finish summary
# ---------------------------------------------------------------------------

ORCH_SUMMARY = "ORCHESTRATOR_FINISH_SUMMARY: add() now adds correctly"
CODER_HANDOFF = "CODER_HANDOFF_TEXT: changed a - b to a + b"


@pytest.fixture
def pr_env_orch(tmp_path: Path) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    """Like pr_env but fake-gh captures --body to a separate file (body-safe)."""
    source = Path(__file__).parents[2] / "examples" / "toyrepo"
    dest = tmp_path / "mission" / "toyrepo"
    dest.mkdir(parents=True)
    for f in source.iterdir():
        if f.is_file():
            shutil.copy(f, dest / f.name)
    bare = tmp_path / "origin.git"
    bare.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "--bare", "-b", "main"], cwd=bare, check=True, capture_output=True
    )
    subprocess.run(["git", "init", "-b", "main"], cwd=dest, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=dest, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=r@r", "-c", "user.name=r", "commit", "-m", "init"],
        cwd=dest,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", str(bare)], cwd=dest, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "push", "-u", "origin", "main"], cwd=dest, check=True, capture_output=True
    )

    body_file = tmp_path / "gh-body.txt"
    fake_gh = tmp_path / "fake-gh"
    # Python fake-gh: extracts --body arg reliably (body may contain newlines)
    fake_gh.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, pathlib\n"
        f"body_file = pathlib.Path({str(body_file)!r})\n"
        "args = sys.argv[1:]\n"
        "if '--body' in args:\n"
        "    idx = args.index('--body')\n"
        "    body_file.write_text(args[idx + 1])\n"
        "print('https://github.com/roowus/toyrepo/pull/1')\n"
    )
    fake_gh.chmod(0o755)
    return {
        "repo": dest,
        "bare": bare,
        "fake_gh": str(fake_gh),
        "gh_body": body_file,
        "tmp": tmp_path,
    }


@pytest.fixture
async def orch_world(pr_env_orch: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:  # type: ignore[no-untyped-def]
    from rework.missions import orchestrated_plan
    from rework.orchestrator.agent import MARKER

    store: SqliteStore = await open_sqlite(str(pr_env_orch["tmp"] / "opr.sqlite3"))
    try:
        objective = "make add(2, 3) == 5"
        plan = orchestrated_plan("fix-pr", "pro")
        mission = await store.create_mission(
            Mission(
                id="mopr",
                objective=objective,
                repo_path=str(pr_env_orch["repo"]),
                plan=plan,
            )
        )
        provider = ScriptedProvider()
        # The orchestrator's route
        provider.route(MARKER, _orch_pr_script())
        # The roles' routes
        provider.route("Investigate any brief", _investigator_script())
        provider.route("Fulfill the task", _coder_script(CODER_HANDOFF))
        provider.route("Find issues", _auditor_script(AUDIT_CLEAN))
        yield {
            "store": store,
            "provider": provider,
            "mission": mission,
            "ck": str(pr_env_orch["tmp"] / "ck.sqlite3"),
            "db": str(pr_env_orch["tmp"] / "opr.sqlite3"),
            "workroot": pr_env_orch["tmp"] / "work",
            "registry": autodiscover(),
            "catalog": role_catalog(),
            "tmp": pr_env_orch["tmp"],
            "fake_gh": pr_env_orch["fake_gh"],
            "gh_body": pr_env_orch["gh_body"],
            "orch_marker": MARKER,
        }
    finally:
        await store.aclose()


def _orch_pr_script() -> list[Any]:
    """Scripted orchestrator tool calls: investigate -> code -> audit -> finish."""
    from rework.kernel.llm.types import ChatResult, ToolCall, Usage

    def turn(*calls: ToolCall, text: str = "") -> ChatResult:  # type: ignore[no-untyped-def]
        return ChatResult(
            text=text, tool_calls=list(calls), usage=Usage(input_tokens=200, output_tokens=20)
        )

    def call(name: str, cid: str, **args: Any) -> ToolCall:  # type: ignore[no-untyped-def]
        return ToolCall(id=cid, name=name, arguments=args)

    return [
        turn(
            call(
                "spawn_role",
                "p1",
                role="investigator",
                step="investigate",
                brief="find add() bug",
                model="flash",
            ),
            call("wait", "p2"),
            text="Investigating.",
        ),
        turn(call("read_artifact", "p3", run_id="mopr:investigate:investigator:1")),
        turn(
            call("spawn_role", "p4", role="coder", step="code", brief="fix add()"),
            call("wait", "p5"),
        ),
        turn(
            call("spawn_role", "p6", role="auditor", step="audit", brief="review fix"),
            call("wait", "p7"),
        ),
        # finish with the distinctive orchestrator summary
        turn(call("finish", "p8", outcome="done", summary=ORCH_SUMMARY)),
    ]


async def test_pr_body_leads_with_orchestrator_summary(
    orch_world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """PR #21 fix: _create_pr must use the orchestrator's finish summary in the
    'What changed' section, not the last coder handoff text.  Before the fix,
    result['summary'] was empty when _pr_body ran because _synthesize (which
    persists it) was called after _final_gates."""
    monkeypatch.setenv("REWORK_GH_BIN", str(orch_world["fake_gh"]))
    store: SqliteStore = orch_world["store"]

    runner = MissionRunner(
        orch_world["mission"].id,
        store,
        InProcessEventBus(store),
        orch_world["registry"],
        orch_world["catalog"],
        _ck_factory(orch_world["ck"]),
        orch_world["workroot"],
        poll_interval_s=0.05,
        provider=orch_world["provider"],
    )
    await asyncio.wait_for(asyncio.create_task(runner.run()), timeout=60)

    mission = await store.get_mission(orch_world["mission"].id)
    assert mission is not None and mission.status == MissionStatus.DONE

    # The PR body must contain the orchestrator's finish summary in "What changed"
    assert orch_world["gh_body"].exists(), "fake gh was never called with --body"
    pr_body = orch_world["gh_body"].read_text()

    # Orchestrator's summary appears in the "What changed" section
    assert ORCH_SUMMARY in pr_body, (
        f"PR body 'What changed' should contain orchestrator summary.\n"
        f"Expected: {ORCH_SUMMARY!r}\nBody:\n{pr_body[:800]}"
    )
    # Coder handoff text should NOT appear in "What changed" (it goes to appendix)
    what_changed = pr_body.split("## What changed", 1)[1].split("##", 1)[0]
    assert CODER_HANDOFF not in what_changed, (
        f"PR body 'What changed' should not contain coder handoff.\nSection:\n{what_changed[:400]}"
    )


# ---------------------------------------------------------------------------
# The PR gate is OFF by default: after a clean audited `done` the engine pushes
# and opens the PR itself. The orchestrator can ask for the gate, and the
# project setting `pr_gate` can force it always / never.
# ---------------------------------------------------------------------------


def _with_reason(
    script: list[Any], *, reason: str, ask: bool = True, category: str = "security"
) -> list[Any]:
    """The same orchestrator script, but its finish call asks for a review. A
    gate ask is strict (issue #36): it needs a reason AND one of the four
    high-stakes categories, so both are patched in."""
    patched = list(script)
    last = patched[-1]
    finish = last.tool_calls[-1]
    args = dict(finish.arguments)
    args["ask_before_pr"] = ask
    if category:
        args["gate_category"] = category
    if reason:
        args["reason"] = reason
    patched[-1] = last.model_copy(
        update={"tool_calls": [finish.model_copy(update={"arguments": args})]}
    )
    return patched


def _runner(world: dict[str, Any], monkeypatch: pytest.MonkeyPatch, script: list[Any]) -> Any:
    monkeypatch.setenv("REWORK_GH_BIN", str(world["fake_gh"]))
    for route, responses in _route_specs().items():
        world["provider"].route(route, responses)
    world["provider"]._queues[world["orch_marker"]] = list(script)
    return MissionRunner(
        world["mission"].id,
        world["store"],
        InProcessEventBus(world["store"]),
        world["registry"],
        world["catalog"],
        _ck_factory(world["ck"]),
        world["workroot"],
        poll_interval_s=0.05,
        provider=world["provider"],
    )


def _route_specs() -> dict[str, list[Any]]:
    return {
        "Investigate any brief": [_investigator_script()],
        "Fulfill the task": [_coder_script(CODER_HANDOFF)],
        "Find issues": [_auditor_script(AUDIT_CLEAN)],
    }


async def _decide(store: SqliteStore, sqlite_path: str, mission_id: str, approved: bool) -> None:
    second: SqliteStore = await open_sqlite(sqlite_path)
    try:
        gates = await second.list_open_gates(mission_id)
        assert gates, "expected an open gate to decide"
        await GateManager(second, InProcessEventBus(second)).decide(gates[0].id, approved=approved)
    finally:
        await second.aclose()


async def test_done_opens_the_pr_without_a_gate(
    orch_world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """(a) no ask, auto setting: PR opened, no open_pr gate row at all."""
    runner = _runner(orch_world, monkeypatch, _orch_pr_script())
    await asyncio.wait_for(asyncio.create_task(runner.run()), timeout=60)
    mission = await orch_world["store"].get_mission(orch_world["mission"].id)
    assert mission is not None and mission.status == MissionStatus.DONE
    assert (mission.result or {}).get("pr_url") == "https://github.com/roowus/toyrepo/pull/1"
    gates = await orch_world["store"].list_gates(orch_world["mission"].id)
    assert [g for g in gates if g.kind.value == "open_pr"] == []
    assert orch_world["gh_body"].exists(), "the PR was never opened"


async def test_ask_before_pr_gates_the_pr_and_shows_the_reason(
    orch_world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """(b) ask_before_pr=True: a gate with the reason; PR only after approval."""
    reason = "touches the admission path, the owner should see it first"
    script = _with_reason(_orch_pr_script(), reason=reason)
    runner = _runner(orch_world, monkeypatch, script)
    task = asyncio.create_task(runner.run())
    for _ in range(200):
        if await orch_world["store"].list_open_gates(orch_world["mission"].id):
            break
        await asyncio.sleep(0.05)
    open_gates = await orch_world["store"].list_open_gates(orch_world["mission"].id)
    assert open_gates and open_gates[0].kind.value == "open_pr"
    question = open_gates[0].question
    assert reason in question
    # the strict gate shows WHICH high-stakes category justified it (issue #36)
    assert "Gate category: security" in question
    await _decide(orch_world["store"], orch_world["db"], orch_world["mission"].id, True)
    await asyncio.wait_for(task, timeout=60)
    mission = await orch_world["store"].get_mission(orch_world["mission"].id)
    gates = await orch_world["store"].list_gates(orch_world["mission"].id)
    pr_gates = [g for g in gates if g.kind.value == "open_pr"]
    assert len(pr_gates) == 1 and pr_gates[0].status.value == "approved"
    assert (mission.result or {}).get("pr_url") == "https://github.com/roowus/toyrepo/pull/1"


async def test_denied_ask_before_pr_gate_does_not_open_a_pr(
    orch_world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """(b') the same ask, denied: no PR at all."""
    script = _with_reason(_orch_pr_script(), reason="risky")
    runner = _runner(orch_world, monkeypatch, script)
    task = asyncio.create_task(runner.run())
    for _ in range(200):
        if await orch_world["store"].list_open_gates(orch_world["mission"].id):
            break
        await asyncio.sleep(0.05)
    await _decide(orch_world["store"], orch_world["db"], orch_world["mission"].id, False)
    await asyncio.wait_for(task, timeout=60)
    mission = await orch_world["store"].get_mission(orch_world["mission"].id)
    assert mission is not None and "pr_url" not in (mission.result or {})
    assert not orch_world["gh_body"].exists(), "gh ran even though the owner denied the gate"


async def _set_pr_gate(world: dict[str, Any], value: str) -> None:
    store: SqliteStore = world["store"]
    project = await store.create_project(Project(id=f"p-{value}", name=value))
    await store.update_project(project.id, settings={"pr_gate": value})
    await store.update_mission(world["mission"].id, project_id=project.id)
    world["mission"] = await store.get_mission(world["mission"].id)


async def test_pr_gate_always_forces_the_gate_without_an_ask(
    orch_world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """(c) project setting pr_gate=always: a gate even with no ask."""
    await _set_pr_gate(orch_world, "always")
    runner = _runner(orch_world, monkeypatch, _orch_pr_script())
    task = asyncio.create_task(runner.run())
    for _ in range(200):
        if await orch_world["store"].list_open_gates(orch_world["mission"].id):
            break
        await asyncio.sleep(0.05)
    forced = await orch_world["store"].list_open_gates(orch_world["mission"].id)
    # a category-less forced gate still renders sensibly (no empty category)
    assert forced and "pr_gate=always" in forced[0].question
    assert "Gate category:" not in forced[0].question
    await _decide(orch_world["store"], orch_world["db"], orch_world["mission"].id, True)
    await asyncio.wait_for(task, timeout=60)
    gates = await orch_world["store"].list_gates(orch_world["mission"].id)
    pr_gates = [g for g in gates if g.kind.value == "open_pr"]
    assert len(pr_gates) == 1 and pr_gates[0].status.value == "approved"
    mission = await orch_world["store"].get_mission(orch_world["mission"].id)
    assert (mission.result or {}).get("pr_url")


async def test_pr_gate_never_overrides_the_ask(
    orch_world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """(d) project setting pr_gate=never: no gate even when the orchestrator asks."""
    await _set_pr_gate(orch_world, "never")
    runner = _runner(orch_world, monkeypatch, _with_reason(_orch_pr_script(), reason="risky"))
    await asyncio.wait_for(asyncio.create_task(runner.run()), timeout=60)
    gates = await orch_world["store"].list_gates(orch_world["mission"].id)
    assert [g for g in gates if g.kind.value == "open_pr"] == []
    mission = await orch_world["store"].get_mission(orch_world["mission"].id)
    assert (mission.result or {}).get("pr_url")


async def test_an_ask_without_a_category_is_refused_then_the_pr_opens_ungated(
    orch_world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """(a) the strict rule end to end: an ask_before_pr with no valid category
    is refused as a tool error (no gate, mission not finished), and the retry
    the answer tells it to make -- finish with no ask -- opens the PR ungated."""
    from rework.kernel.llm.types import ChatResult, ToolCall, Usage

    script = _with_reason(_orch_pr_script(), reason="large diff, many files touched", category="")
    # what the refusal text tells the orchestrator to do: finish with no ask
    script = [
        *script,
        ChatResult(
            text="",
            tool_calls=[
                ToolCall(
                    id="r2", name="finish", arguments={"outcome": "done", "summary": ORCH_SUMMARY}
                )
            ],
            usage=Usage(input_tokens=200, output_tokens=20),
        ),
    ]
    runner = _runner(orch_world, monkeypatch, script)
    await asyncio.wait_for(asyncio.create_task(runner.run()), timeout=60)

    from rework.orchestrator.agent import orchestrator_chat_id

    msgs = await orch_world["store"].chat_messages(orchestrator_chat_id(orch_world["mission"].id))
    tool_out = [m.body.get("content", "") for m in msgs if m.role == "tool"]
    refusals = [t for t in tool_out if "gate_category" in t]
    assert refusals, f"the invalid ask was not refused; tool results: {tool_out}"
    assert "without a gate" in refusals[0], "the refusal must say how to proceed"
    gates = await orch_world["store"].list_gates(orch_world["mission"].id)
    assert [g for g in gates if g.kind.value == "open_pr"] == [], "a refused ask opened a gate"
    mission = await orch_world["store"].get_mission(orch_world["mission"].id)
    assert mission is not None and mission.status == MissionStatus.DONE
    assert (mission.result or {}).get("pr_url") == "https://github.com/roowus/toyrepo/pull/1"
    assert orch_world["gh_body"].exists(), "the retry never opened the PR"
