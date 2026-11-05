"""Hybrid search (M6.1): keyword + cosine behind the same search interface.

The fake embedder is deterministic and separable: items about "deploying"
get vectors near e1, items about "testing" near e2, and queries carry a
hint term. This reproduces the acceptance case (natural-language query with
NO keyword overlap still finds the right item) without a live model.
"""

from __future__ import annotations

import pytest

from rework.library.embeddings import embeddable_text
from rework.library.model import ItemStatus, ItemType, LibraryItem
from rework.library.search import HybridSearch
from rework.library.store import GitLibrary

E_DEPLOY = [1.0, 0.0]
E_TEST = [0.0, 1.0]


class FakeEmbedder:
    """Two-topic vector space; 'ship/rollout' language points at deploy."""

    model = "fake-embed-2d"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            if any(w in t.lower() for w in ("ship", "rollout", "deploy", "release")):
                out.append(E_DEPLOY)
            else:
                out.append(E_TEST)
        return out


def _seed_items() -> list[LibraryItem]:
    # ACTIVE: search is a serving path; proposed items never surface
    return [
        LibraryItem(
            type=ItemType.SKILL,
            name="release checklist",
            description="how to roll a build out to users",
            tags=["ops"],
            status=ItemStatus.ACTIVE,
        ),
        LibraryItem(
            type=ItemType.SKILL,
            name="flaky test triage",
            description="how to debug intermittent failures",
            tags=["pytest"],
            status=ItemStatus.ACTIVE,
        ),
    ]


@pytest.fixture
def items() -> list[LibraryItem]:
    return _seed_items()


async def test_keyword_only_when_no_embedder(library_index, items) -> None:
    for item in items:
        await library_index.upsert_item(item)
    hs = HybridSearch(library_index, None)
    hits = await hs.search("release")
    assert hits[0].name == "release checklist"


async def test_semantic_recall_without_keyword_overlap(library_index, items) -> None:
    """THE acceptance case: a query sharing NO keywords with the item still
    finds it via the vector half (keyword-only search misses it)."""
    for item in items:
        (vector,) = await FakeEmbedder().embed(
            [embeddable_text(item.name, item.description, item.tags)]
        )
        item.vectors = {FakeEmbedder.model: vector}
        await library_index.upsert_item(item)

    hs = HybridSearch(library_index, FakeEmbedder())
    # 'ship it' shares no token with 'release checklist'/'roll a build out'
    keyword_only = await library_index.search("ship it to production")
    hybrid = await hs.search("ship it to production")
    assert not keyword_only or keyword_only[0].name != "release checklist"
    assert hybrid[0].name == "release checklist"


async def test_hybrid_blends_both_signals(library_index) -> None:
    from rework.library.curation import contribute

    await contribute(
        library_index,
        _fake_git(),
        type_=ItemType.SKILL,
        name="deploy runbook",
        description="ship to prod",
        body="b",
        tags=[],
    )
    hs = HybridSearch(library_index, FakeEmbedder())
    await hs.ensure_indexed()
    hits = await hs.search("deploy ship")
    assert hits[0].name == "deploy runbook"


async def test_ensure_indexed_backfills_missing(library_index) -> None:
    from rework.library.curation import contribute

    await contribute(
        library_index,
        _fake_git(),
        type_=ItemType.SKILL,
        name="release checklist",
        description="roll out",
        body="b",
        tags=[],
    )
    _ = items
    hs = HybridSearch(library_index, FakeEmbedder())
    count = await hs.ensure_indexed()
    assert count == 1
    again = await hs.ensure_indexed()
    assert again == 0  # idempotent


def _fake_git():
    import tempfile
    from pathlib import Path

    return GitLibrary(Path(tempfile.mkdtemp()) / "lib")
