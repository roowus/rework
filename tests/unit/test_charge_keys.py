"""Charge dedupe keys: two genuine calls must not share a key (a dropped charge is
silent under-counted spend), and a retried or rebuilt call must share one (a repeat
is a no-op, not a second charge). Real sqlite store, the FakeProvider, no network."""

from __future__ import annotations

from pathlib import Path

import pytest

from rework.kernel.budget.ledger import Budget
from rework.kernel.llm.adapters.fake import FakeProvider
from rework.kernel.llm.catalog import ModelCatalog, ModelEntry
from rework.kernel.llm.types import ChatResult, Usage
from rework.kernel.state.sqlite import open_sqlite
from rework.nodes.report_writer import make_report_writer_node
from rework.nodes.toolloop import make_toolloop_node, next_charge_key
from rework.nodes.tools import LocalExecutor

RUN = "r-keys"


def _catalog() -> ModelCatalog:
    return ModelCatalog([ModelEntry(id="fake", provider="fake", model="fake-1", tier=0)])


def _answer(text: str) -> ChatResult:
    return ChatResult(
        text=text, usage=Usage(input_tokens=8, output_tokens=4), stop_reason="end_turn"
    )


@pytest.fixture
async def store(tmp_path: Path):  # type: ignore[no-untyped-def]
    db = await open_sqlite(str(tmp_path / "keys.sqlite3"))
    try:
        yield db
    finally:
        await db.aclose()


async def _keys(store, run_id: str = RUN) -> list[str]:  # type: ignore[no-untyped-def]
    return sorted(c.dedupe_key or "" for c in await store.run_charges(run_id))


async def test_next_charge_key_is_stable_until_a_charge_lands(store) -> None:  # type: ignore[no-untyped-def]
    first = await next_charge_key(store, RUN, "loop")
    assert await next_charge_key(store, RUN, "loop") == first  # a retry reuses the slot
    assert first == f"{RUN}:loop:1"
    assert await next_charge_key(store, RUN, "fix") == f"{RUN}:fix:1"  # scopes are apart


@pytest.mark.parametrize(("first_scope", "second_scope"), [("implement", "fix"), ("loop", "loop")])
async def test_two_toolloops_in_one_run_both_keep_their_charge(
    store, tmp_path: Path, first_scope: str, second_scope: str
) -> None:  # type: ignore[no-untyped-def]
    """Two loop nodes in one run (coder implement + fix, or two default-scoped loops):
    before the fix the second turn-0 charge was discarded, so the run showed one call."""
    for scope in (first_scope, second_scope):
        node = make_toolloop_node(
            FakeProvider([_answer("done")]),
            _catalog(),
            "fake",
            LocalExecutor(tmp_path),
            "be brief",
            store,
            "m-keys",
            RUN,
            Budget(),
            charge_scope=scope,
        )
        await node({"messages": [{"role": "user", "content": "go"}]})
    charges = await store.run_charges(RUN)
    assert len(charges) == 2, [c.dedupe_key for c in charges]
    assert len({c.dedupe_key for c in charges}) == 2


async def test_rebuilt_report_writer_charges_its_genuine_call_once(store) -> None:  # type: ignore[no-untyped-def]
    """A report writer rebuilt for a resumed run used to restart its closure counter
    and reuse ':report:1', so its new genuine model call was discarded (zero charges).
    The key now comes from what has landed: the first build charges once, and a rebuilt
    writer charges its own call once more: two charges in total, not one, not three."""

    def build(provider: FakeProvider):  # type: ignore[no-untyped-def]
        return make_report_writer_node(
            provider, _catalog(), "fake", store, "m-keys", RUN, sections=[]
        )

    await build(FakeProvider([_answer("{}")]))({"messages": []})
    assert await _keys(store) == [f"{RUN}:report:1"]

    await build(FakeProvider([_answer("{}")]))({"messages": []})  # the rebuilt writer
    assert await _keys(store) == [f"{RUN}:report:1", f"{RUN}:report:2"]


async def test_report_writer_retry_within_one_run_is_two_calls(store) -> None:  # type: ignore[no-untyped-def]
    """An invalid first artifact is re-asked: that is a second genuine call, charged."""
    provider = FakeProvider([_answer("not json"), _answer("{}")])
    node = make_report_writer_node(provider, _catalog(), "fake", store, "m-keys", RUN, sections=[])
    await node({"messages": []})
    assert await _keys(store) == [f"{RUN}:report:1", f"{RUN}:report:2"]
