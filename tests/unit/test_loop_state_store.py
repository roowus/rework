"""The agent loop's transcript lives in the store (ADR 0020).

It used to live in `<workdir>/loop.json` on the daemon's disk, so a run could
only resume on the machine that started it, and a lost workdir silently cost
every earlier turn. These pin: resume from the store with an EMPTY workdir (as
on another host), the legacy file fallback, and that a finished loop leaves
nothing behind.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rework.kernel.budget.ledger import Budget
from rework.kernel.llm.catalog import ModelCatalog, ModelEntry
from rework.kernel.llm.types import ChatResult, ToolCall, Usage
from rework.kernel.state.records import Mission, Run
from rework.kernel.state.sqlite import open_sqlite
from rework.nodes import toolloop


class _Exec:
    def specs(self, allowed: Any = None) -> list[Any]:
        return []

    async def execute(self, name: str, args: dict[str, Any]) -> str:
        return "ok"


class _Provider:
    def __init__(self, results: list[ChatResult]) -> None:
        self.results = list(results)
        self.seen: list[int] = []

    async def complete(self, request: Any) -> ChatResult:
        self.seen.append(len(request.messages))
        return self.results.pop(0)


def _catalog() -> ModelCatalog:
    return ModelCatalog([ModelEntry(id="m", provider="fake", model="fake-m", tier=1)])


def _node(store: Any, provider: Any, resume_file: Path | None) -> Any:
    return toolloop.make_toolloop_node(
        provider=provider,
        catalog=_catalog(),
        catalog_id="m",
        executor=_Exec(),  # type: ignore[arg-type]
        system_prompt="sys",
        store=store,
        mission_id="m-1",
        run_id="m-1:s:r:1",
        budget=Budget(),
        resume_file=resume_file,
    )


OPENING = [{"role": "user", "content": "brief"}]
SAVED = [
    *OPENING,
    {"role": "assistant", "content": "", "tool_calls": [{"id": "a", "name": "x", "arguments": {}}]},
    {"role": "tool", "tool_call_id": "a", "name": "x", "content": "ok"},
]


async def test_resume_comes_from_the_store_not_the_workdir(tmp_path: Path) -> None:
    store = await open_sqlite(str(tmp_path / "s.sqlite3"))
    try:
        await store.create_mission(Mission(id="m-1", objective="x"))
        await store.create_run(Run(id="m-1:s:r:1", mission_id="m-1", role="r", step="s"))
        await store.save_loop_state("m-1:s:r:1", 5, SAVED)
        # a DIFFERENT, empty workdir: what another host sees
        other_host = tmp_path / "elsewhere" / "loop.json"
        provider = _Provider(
            [ChatResult(text="done", usage=Usage(input_tokens=1, output_tokens=1))]
        )
        out = await _node(store, provider, other_host)({"messages": list(OPENING)})
        assert out["loop_turns"] == 6, "resumed at turn 5, not from scratch"
        assert provider.seen == [len(SAVED)], "the model saw the saved transcript"
        assert await store.load_loop_state("m-1:s:r:1") is None, "cleared when done"
    finally:
        await store.aclose()


async def test_each_turn_is_saved(tmp_path: Path) -> None:
    store = await open_sqlite(str(tmp_path / "s.sqlite3"))
    try:
        await store.create_mission(Mission(id="m-1", objective="x"))
        await store.create_run(Run(id="m-1:s:r:1", mission_id="m-1", role="r", step="s"))
        snapshots: list[Any] = []
        real = store.save_loop_state

        async def spy(run_id: str, turns: int, messages: list[dict[str, Any]]) -> None:
            snapshots.append((turns, len(messages)))
            await real(run_id, turns, messages)

        store.save_loop_state = spy  # type: ignore[method-assign]
        provider = _Provider(
            [
                ChatResult(
                    text="",
                    tool_calls=[ToolCall(id="t1", name="x", arguments={})],
                    usage=Usage(input_tokens=1, output_tokens=1),
                ),
                ChatResult(text="done", usage=Usage(input_tokens=1, output_tokens=1)),
            ]
        )
        await _node(store, provider, None)({"messages": list(OPENING)})
        assert snapshots == [(1, 3)], "one save after the tool turn"
    finally:
        await store.aclose()


async def test_a_legacy_loop_json_still_resumes(tmp_path: Path) -> None:
    """A run interrupted across the upgrade left its transcript on disk."""
    store = await open_sqlite(str(tmp_path / "s.sqlite3"))
    try:
        await store.create_mission(Mission(id="m-1", objective="x"))
        await store.create_run(Run(id="m-1:s:r:1", mission_id="m-1", role="r", step="s"))
        legacy = tmp_path / "loop.json"
        legacy.write_text(json.dumps({"turns": 3, "messages": SAVED}))
        provider = _Provider(
            [ChatResult(text="done", usage=Usage(input_tokens=1, output_tokens=1))]
        )
        out = await _node(store, provider, legacy)({"messages": list(OPENING)})
        assert out["loop_turns"] == 4
        assert not legacy.exists(), "the legacy file is cleared with the state"
    finally:
        await store.aclose()
