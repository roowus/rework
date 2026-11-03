"""Registry autodiscovery + the Echo pause/resume cycle (the pause primitive of
ADR 0002 exercised at the LangGraph level, before the engine exists)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from rework.kernel.budget.ledger import Budget
from rework.kernel.events.bus import InProcessEventBus
from rework.kernel.gates import GateManager
from rework.kernel.llm.adapters.fake import FakeProvider
from rework.kernel.llm.catalog import ModelCatalog, ModelEntry
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
    store: SqliteStore = await open_sqlite(str(tmp_path / "roles.sqlite3"))
    try:
        bus = InProcessEventBus(store)
        mission = await store.create_mission(Mission(id="m1", objective="say hello"))
        run = await store.create_run(
            Run(id=run_id("m1", "echo", "echo", 1), mission_id="m1", role="echo", step="echo")
        )
        catalog = ModelCatalog(
            [
                ModelEntry(id="fake", provider="fake", model="fake-1"),
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


async def test_echo_interrupts_then_resumes(echo_ctx: RoleBuildCtx) -> None:
    graph = autodiscover().get("echo").build_graph(echo_ctx)
    assert graph is not None
    graph.checkpointer = InMemorySaver()
    config: dict[str, Any] = {"configurable": {"thread_id": "t1"}}

    # first run reaches the gate and stops on the interrupt
    result = await graph.ainvoke({"objective": "say hello"}, config=config)
    assert "__interrupt__" in result
    interrupt_payload = result["__interrupt__"][0].value
    assert "Approve completion" in interrupt_payload["question"]

    # resume with the decision (what the engine will do from the gate row)
    final = await graph.ainvoke(Command(resume=True), config=config)
    assert final["artifact"] == {
        "kind": "echo",
        "objective": "say hello",
        "approved": True,
    }


async def test_echo_rejection_flows_through(echo_ctx: RoleBuildCtx) -> None:
    graph = autodiscover().get("echo").build_graph(echo_ctx)
    assert graph is not None
    graph.checkpointer = InMemorySaver()
    config: dict[str, Any] = {"configurable": {"thread_id": "t2"}}
    await graph.ainvoke({"objective": "try again"}, config=config)
    final = await graph.ainvoke(Command(resume=False), config=config)
    assert final["artifact"]["approved"] is False
