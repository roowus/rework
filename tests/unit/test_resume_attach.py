import pytest

from rework.kernel.budget.ledger import Budget
from rework.kernel.llm.catalog import ModelCatalog, ModelEntry
from rework.kernel.llm.types import ChatResult, ToolCall
from rework.kernel.state.records import Mission
from rework.nodes.toolloop import make_toolloop_node
from rework.nodes.tools import LazyExecutor


class _P:
    """First call asks for one tool run, second finishes."""

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, request):
        self.calls += 1
        if self.calls == 1:
            return ChatResult(
                text="",
                tool_calls=[ToolCall(id="t1", name="run_command", arguments={"command": "pwd"})],
            )
        return ChatResult(text="done")


@pytest.mark.asyncio
async def test_on_start_reestablishes_binding_on_resume(store):
    """The live bug: a mission resumed AFTER prepare keeps an unattached
    executor, so the first tool call dies with 'LazyExecutor used before
    attach()'. on_start must fix that on every entry."""
    mission = await store.create_mission(
        Mission(id="m-resume", objective="x", plan={"kind": "fix"})
    )
    executor = LazyExecutor()  # unattached, exactly like a fresh resume
    attached = []

    async def reattach():
        attached.append(1)
        from pathlib import Path

        executor.attach(Path("/tmp"))

    node = make_toolloop_node(
        provider=_P(),
        catalog=ModelCatalog([ModelEntry(id="flash", provider="fake", model="f")]),
        catalog_id="flash",
        executor=executor,
        system_prompt="sys",
        store=store,
        mission_id=mission.id,
        run_id="r",
        budget=Budget(),
        on_start=reattach,
    )
    out = await node({"messages": [{"role": "user", "content": "go"}]})
    assert out["final_text"] == "done"
    assert executor.attached is True
    assert attached == [1]


@pytest.mark.asyncio
async def test_without_hook_unattached_executor_still_fails_fast(store):
    mission = await store.create_mission(
        Mission(id="m-noattach", objective="x", plan={"kind": "fix"})
    )
    executor = LazyExecutor()
    node = make_toolloop_node(
        provider=_P(),
        catalog=ModelCatalog([ModelEntry(id="flash", provider="fake", model="f")]),
        catalog_id="flash",
        executor=executor,
        system_prompt="sys",
        store=store,
        mission_id=mission.id,
        run_id="r",
        budget=Budget(),
    )
    with pytest.raises(RuntimeError, match="before attach"):
        await node({"messages": [{"role": "user", "content": "go"}]})
