"""Coder role tests: worktree isolation, the scripted loop editing a file,
and the fully mechanical handoff (commit + diff vs the recorded base)."""

from __future__ import annotations

import asyncio
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
from rework.roles.base import RoleBuildCtx
from rework.roles.registry import autodiscover


def _init_repo(tmp_path: Path) -> Path:
    """A tiny git repo with a main branch and a planted bug."""
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n")  # the bug
    (repo / "test_calc.py").write_text(
        "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    )
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=r@rework", "-c", "user.name=r", "commit", "-m", "init"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return repo


@pytest.fixture
async def coder_ctx(tmp_path: Path) -> AsyncIterator[RoleBuildCtx]:
    repo = _init_repo(tmp_path / "mission")
    run_dir = tmp_path / "runs" / "run1"
    run_dir.mkdir(parents=True)
    store: SqliteStore = await open_sqlite(str(tmp_path / "coder.sqlite3"))
    try:
        bus = InProcessEventBus(store)
        mission = await store.create_mission(
            Mission(id="mc", objective="fix add()", repo_path=str(repo))
        )
        run = await store.create_run(
            Run(
                id=run_id("mc", "code", "coder", 1),
                mission_id="mc",
                role="coder",
                step="code",
                context={"brief": "add() subtracts; make it add"},
            )
        )
        catalog = ModelCatalog(
            [
                ModelEntry(id="flash", provider="fake", model="fake-flash"),
                ModelEntry(id="fake", provider="fake", model="fake-1"),
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


async def test_coder_edits_in_worktree_and_hands_off_diff(coder_ctx: RoleBuildCtx) -> None:
    fake = coder_ctx.provider
    assert isinstance(fake, FakeProvider)
    # turn 1: fix the bug (portable overwrite, not BSD-only sed); turn 2: tests; turn 3: done
    fake.push(
        ChatResult(
            text="fixing",
            tool_calls=[
                ToolCall(
                    id="t1",
                    name="run_command",
                    arguments={
                        "command": "printf 'def add(a, b):\\n    return a + b\\n' > calc.py"
                    },
                )
            ],
            usage=Usage(input_tokens=10, output_tokens=5),
            stop_reason="tool_calls",
        )
    )
    fake.push(
        ChatResult(
            text="verifying",
            tool_calls=[
                ToolCall(id="t2", name="run_command", arguments={"command": "python3 -m pytest -q"})
            ],
            usage=Usage(input_tokens=10, output_tokens=5),
            stop_reason="tool_calls",
        )
    )
    fake.push(
        ChatResult(
            text="test passed: 1/1 green. changed `a - b` to `a + b` in calc.py.",
            usage=Usage(input_tokens=10, output_tokens=8),
            stop_reason="end_turn",
        )
    )

    spec = autodiscover().get("coder")
    graph = spec.build_graph(coder_ctx)
    graph.checkpointer = InMemorySaver()
    config: dict[str, Any] = {"configurable": {"thread_id": "t-coder"}}
    final = await graph.ainvoke({"objective": "fix add()"}, config=config)

    artifact = final["artifact"]
    assert artifact["kind"] == "coder_handoff"
    assert artifact["branch"].startswith("rework/mc-")
    assert artifact["commit"] is not None
    assert artifact["summary"].startswith("test passed")
    assert "calc.py" in artifact["diff"]
    assert "calc.py" in artifact["changed_files"]  # taken from the full diff, not the cut copy
    assert "+    return a + b" in artifact["diff"]
    assert "-    return a - b" in artifact["diff"]

    # the fix landed on the branch, NOT on the mission repo's main
    repo = Path(str(coder_ctx.mission.repo_path))
    main_calc = (repo / "calc.py").read_text()
    assert "a - b" in main_calc  # untouched
    worktree_calc = (Path(final["worktree"]) / "calc.py").read_text()
    assert "a + b" in worktree_calc

    # base_sha was persisted onto the run context for resume-stable diffs
    run = await coder_ctx.store.get_run(coder_ctx.run.id)
    assert run is not None and run.context.get("base_sha")

    # test evidence really came from a run inside the worktree (pytest output
    # reached the tool transcript)
    tool_outputs = [m["content"] for m in final["messages"] if m.get("role") == "tool"]
    assert any("passed" in out for out in tool_outputs)

    # the mechanical commit carries the configured agent identity
    log_proc = await asyncio.create_subprocess_exec(
        "git",
        "log",
        "--format=%an <%ae>",
        "-1",
        cwd=str(final["worktree"]),
        stdout=asyncio.subprocess.PIPE,
    )
    out, _ = await log_proc.communicate()
    author = out.decode().strip()
    assert author == "rework <rework@rework.local>"


async def test_coder_requires_git_repo(tmp_path: Path) -> None:
    store = await open_sqlite(str(tmp_path / "x.sqlite3"))
    try:
        bus = InProcessEventBus(store)
        mission = await store.create_mission(Mission(id="m", objective="x"))  # no repo_path
        run = await store.create_run(
            Run(id=run_id("m", "code", "coder", 1), mission_id="m", role="coder", step="code")
        )
        ctx = RoleBuildCtx(
            run=run,
            mission=mission,
            catalog=ModelCatalog([ModelEntry(id="flash", provider="fake", model="f")]),
            provider=FakeProvider(),
            store=store,
            bus=bus,
            gates=GateManager(store, bus),
            budget=Budget(),
            workdir=tmp_path,
        )
        spec = autodiscover().get("coder")
        with pytest.raises(ValueError, match="repo_path"):
            spec.build_graph(ctx)
    finally:
        await store.aclose()


async def test_ensure_worktree_reuses_existing_branch(coder_ctx: RoleBuildCtx) -> None:
    """Crash-resume path: prepare runs again on an existing worktree; it must
    reuse (same branch) instead of failing or duplicating."""
    from rework.nodes.git_ops import current_branch, ensure_worktree

    repo = Path(str(coder_ctx.mission.repo_path))
    wt_path = coder_ctx.workdir / "worktree"
    branch = "rework/" + coder_ctx.run.id.replace(":", "-")
    wt1, _base = await ensure_worktree(repo, wt_path, branch)
    wt2, _sha = await ensure_worktree(repo, wt_path, branch)
    assert wt1 == wt2
    assert await current_branch(wt2) == branch


async def test_investigators_proposed_tests_reach_the_coder(coder_ctx: RoleBuildCtx) -> None:
    """Acceptance (c): the investigator's tests are in the coder's first message,
    ahead of the generic findings dump; and the red-green rule is in its prompt."""
    investigation = {
        "summary": "add subtracts",
        "tests": [
            {
                "name": "test_add_sums",
                "what_it_pins": "add(2, 3) == 5",
                "where": "test_calc.py",
                "should_fail_because": "add returns a - b",
                "command": "python3 -m pytest -q test_calc.py",
            }
        ],
        "no_tests_reason": "",
    }
    run = await coder_ctx.store.update_run(
        coder_ctx.run.id, context={"brief": "fix add()", "investigation": investigation}
    )
    ctx = RoleBuildCtx(**{**coder_ctx.__dict__, "run": run or coder_ctx.run})
    fake = coder_ctx.provider
    assert isinstance(fake, FakeProvider)
    fake.push(ChatResult(text="done", usage=Usage(input_tokens=1, output_tokens=1)))
    graph = autodiscover().get("coder").build_graph(ctx)
    graph.checkpointer = InMemorySaver()
    config: dict[str, Any] = {"configurable": {"thread_id": "t-c"}}
    await graph.ainvoke({"objective": "fix add()"}, config=config)
    first = str(fake.requests[0].messages[0]["content"])
    assert "Tests the investigator proposed" in first
    assert "test_add_sums" in first and "should_fail_because" in first
    assert "watch it FAIL for the RIGHT reason" in str(fake.requests[0].system)
