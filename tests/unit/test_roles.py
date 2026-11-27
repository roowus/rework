"""Registry autodiscovery + the Echo pause/resume cycle (the pause primitive of
ADR 0002 exercised at the LangGraph level, before the engine exists)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from rework.kernel.budget.ledger import Budget
from rework.kernel.events.bus import InProcessEventBus
from rework.kernel.gates import GateManager
from rework.kernel.llm.adapters.fake import FakeProvider
from rework.kernel.state.records import Mission, Run, run_id
from rework.kernel.state.sqlite import SqliteStore, open_sqlite
from rework.roles.base import RoleBuildCtx, RoleSpec
from rework.roles.registry import autodiscover


def test_autodiscover_finds_echo() -> None:
    registry = autodiscover()
    assert "echo" in registry.names()
    spec = registry.get("echo")
    assert isinstance(spec, RoleSpec)
    assert spec.default_model == "fake"


def test_unknown_role_names_known() -> None:
    registry = autodiscover()
    with pytest.raises(KeyError, match="echo"):
        registry.get("nope")


@pytest.fixture
async def echo_ctx(tmp_path: Path) -> AsyncIterator[RoleBuildCtx]:
    """Build ctx with an owned store; the aiosqlite thread is non-daemon, so a
    leaked connection hangs interpreter exit (found the hard way)."""
    from conftest import make_git_repo, role_catalog

    repo = make_git_repo(tmp_path)
    store: SqliteStore = await open_sqlite(str(tmp_path / "roles.sqlite3"))
    try:
        bus = InProcessEventBus(store)
        mission = await store.create_mission(
            Mission(id="m1", objective="say hello", repo_path=str(repo))
        )
        run = await store.create_run(
            Run(id=run_id("m1", "echo", "echo", 1), mission_id="m1", role="echo", step="echo")
        )
        catalog = role_catalog()
        yield RoleBuildCtx(
            run=run,
            mission=mission,
            catalog=catalog,
            provider=FakeProvider(),
            store=store,
            bus=bus,
            gates=GateManager(store, bus),
            budget=Budget(),
            workdir=tmp_path,
        )
    finally:
        await store.aclose()


async def test_echo_compiles(echo_ctx: RoleBuildCtx) -> None:
    """Graph-compile canary: every builtin role must build. Version drift that
    breaks compilation fails here, not in production (docs/design.md)."""
    registry = autodiscover()
    for name in registry.names():
        assert registry.get(name).build_graph(echo_ctx) is not None


async def _decide_when_open(ctx: RoleBuildCtx, approved: bool) -> str:
    """Answer echo's gate from the side, like `rework approve` does."""
    import asyncio

    for _ in range(200):
        gates = await ctx.store.list_open_gates(ctx.mission.id)
        if gates:
            await ctx.gates.decide(gates[0].id, approved)
            return gates[0].question
        await asyncio.sleep(0.02)
    raise AssertionError("echo's gate never opened")


async def test_echo_asks_the_owner_then_finishes(echo_ctx: RoleBuildCtx) -> None:
    """One pause primitive (ADR 0023): echo blocks in a run-scoped owner gate
    and finishes with the owner's answer; no interrupt, no resume."""
    import asyncio

    graph = autodiscover().get("echo").build_graph(echo_ctx)
    graph.checkpointer = InMemorySaver()
    config: dict[str, Any] = {"configurable": {"thread_id": "t1"}}
    run = asyncio.create_task(graph.ainvoke({"objective": "say hello"}, config=config))
    question = await _decide_when_open(echo_ctx, True)
    final = await asyncio.wait_for(run, 10)
    assert "Approve completion" in question
    assert "__interrupt__" not in final
    assert final["artifact"] == {"kind": "echo", "objective": "say hello", "approved": True}
    gate = (await echo_ctx.store.list_gates("m1"))[0]
    assert gate.for_run == echo_ctx.run.id, "the gate names the run it unblocks"


async def test_echo_rejection_flows_through(echo_ctx: RoleBuildCtx) -> None:
    import asyncio

    graph = autodiscover().get("echo").build_graph(echo_ctx)
    graph.checkpointer = InMemorySaver()
    config: dict[str, Any] = {"configurable": {"thread_id": "t2"}}
    run = asyncio.create_task(graph.ainvoke({"objective": "try again"}, config=config))
    await _decide_when_open(echo_ctx, False)
    final = await asyncio.wait_for(run, 10)
    assert final["artifact"]["approved"] is False
