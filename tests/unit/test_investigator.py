"""Investigator role tests, scripted end to end: prepare builds the map,
the loop runs a tool, the report-writer emits the validated artifact, the
ledger is charged. Also: the compile canary now covers every builtin role."""

from __future__ import annotations

import json
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


@pytest.fixture
async def inv_ctx(tmp_path: Path) -> AsyncIterator[RoleBuildCtx]:
    """Workspace with a planted bug; requested_sections set on the run."""
    (tmp_path / "app.py").write_text("def greet(name):\n    return f'hello {name.upper()}'\n")
    store: SqliteStore = await open_sqlite(str(tmp_path / "inv.sqlite3"))
    try:
        bus = InProcessEventBus(store)
        mission = await store.create_mission(Mission(id="mi", objective="find the crash"))
        run = await store.create_run(
            Run(
                id=run_id("mi", "investigate", "investigator", 1),
                mission_id="mi",
                role="investigator",
                step="investigate",
                context={
                    "brief": "greet('bob') crashes",
                    "depth": "quick",
                    "requested_sections": ["summary", "findings", "blockers"],
                },
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
            workdir=tmp_path,
        )
    finally:
        await store.aclose()


async def test_investigator_end_to_end_scripted(inv_ctx: RoleBuildCtx) -> None:
    artifact_json = json.dumps(
        {
            "summary": "greet crashes on uppercase: name.upper() raises when name is not a str.",
            "findings": [
                {
                    "title": "name.upper() assumes str",
                    "detail": (
                        "app.py line 2 calls name.upper(); non-str input raises AttributeError."
                    ),
                    "evidence": ["app.py:2"],
                    "severity": "high",
                    "verdict": "CONFIRMED",
                    "confidence": 0.9,
                }
            ],
            "blockers": [],
        }
    )
    fake = inv_ctx.provider
    assert isinstance(fake, FakeProvider)
    fake.push(
        ChatResult(
            text="checking the file",
            tool_calls=[ToolCall(id="t1", name="read_file", arguments={"path": "app.py"})],
            usage=Usage(input_tokens=20, output_tokens=10),
            stop_reason="tool_calls",
        )
    )
    fake.push(
        ChatResult(
            text="line 2 calls .upper() on the input; that is the crash.",
            usage=Usage(input_tokens=15, output_tokens=8),
            stop_reason="end_turn",
        )
    )
    fake.push(ChatResult(text=artifact_json, usage=Usage(input_tokens=50, output_tokens=60)))

    spec = autodiscover().get("investigator")
    graph = spec.build_graph(inv_ctx)
    graph.checkpointer = InMemorySaver()
    config: dict[str, Any] = {"configurable": {"thread_id": "t-inv"}}
    final = await graph.ainvoke({"objective": "find the crash"}, config=config)

    assert final["artifact"] is not None
    assert final["artifact"]["summary"].startswith("greet crashes")
    assert final["artifact"]["findings"][0]["evidence"] == ["app.py:2"]

    # the loop actually read the planted file and the system prompt carried the goal
    first_request = fake.requests[0]
    assert "GOAL:" in first_request.system
    tool_messages = [m for m in final["messages"] if m.get("role") == "tool"]
    assert any("name.upper()" in m["content"] for m in tool_messages)

    # the ledger charged all three model calls
    spend = await inv_ctx.store.mission_spend("mi")
    assert spend.output_tokens == 78  # 10 + 8 + 60

    # second call carried the tool result back into the transcript
    second = fake.requests[1]
    roles = [m["role"] for m in second.messages]
    assert roles == ["user", "assistant", "tool"]


async def test_report_writer_retry_on_bad_json(inv_ctx: RoleBuildCtx) -> None:
    fake = inv_ctx.provider
    assert isinstance(fake, FakeProvider)
    fake.push(ChatResult(text="not json at all", usage=Usage(input_tokens=5, output_tokens=5)))
    good = json.dumps({"summary": "ok", "findings": [], "blockers": []})
    fake.push(ChatResult(text=good, usage=Usage(input_tokens=5, output_tokens=5)))

    spec = autodiscover().get("investigator")
    graph = spec.build_graph(inv_ctx)
    graph.checkpointer = InMemorySaver()
    config: dict[str, Any] = {"configurable": {"thread_id": "t-retry"}}
    final = await graph.ainvoke({"objective": "retry path"}, config=config)
    assert final["artifact"] == {"summary": "ok", "findings": [], "blockers": []}
    assert len(fake.requests) == 2  # one retry after the rejection


async def test_compile_canary_covers_all_builtin_roles(inv_ctx: RoleBuildCtx) -> None:
    registry = autodiscover()
    assert {"echo", "investigator"} <= set(registry.names())
    for name in registry.names():
        assert registry.get(name).build_graph(inv_ctx) is not None
