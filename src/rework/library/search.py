"""Hybrid search (M6.1): keyword scoring + embedding cosine, one interface.

Wraps any LibraryStore. The semantic half is best-effort end to end: when
there is no embedder, or the embedder fails, or an item has no vector, the
keyword half carries the result (never an error, never empty unless both
halves find nothing). Vectors live on the item row (model -> vector); items
indexed before a model was configured simply have none until reindexed.

Ranking: normalized keyword score + cosine, 60/40 (keyword is the precision
signal; cosine is recall for natural-language phrasing). If the embedder
matches nothing vector-wise this degrades to plain keyword order.
"""

from __future__ import annotations

from typing import Any

from rework.library.embeddings import cosine, embeddable_text
from rework.library.model import ItemStatus, ItemType, LibraryItem
from rework.library.store import LibraryStore, _score

KEYWORD_WEIGHT = 0.6
SEMANTIC_WEIGHT = 0.4


class HybridSearch:
    def __init__(self, index: LibraryStore, embedder: Any | None) -> None:
        self._index = index
        self._embedder = embedder

    async def search(
        self,
        query: str,
        type_: ItemType | None = None,
        limit: int = 20,
    ) -> list[LibraryItem]:
        items = await self._index.list_items(type_=type_, status=ItemStatus.ACTIVE, limit=1000)
        query_vector = await self._query_vector(query)
        scored: list[tuple[float, str, LibraryItem]] = []
        for item in items:
            kw = _score(item, query)
            sem = 0.0
            if query_vector is not None:
                vector = (item.vectors or {}).get(self._embedder_model, [])
                if vector:
                    sem = max(0.0, cosine(query_vector, vector))
            total = KEYWORD_WEIGHT * (kw / 100.0) + SEMANTIC_WEIGHT * sem
            if total > 0:
                scored.append((-total, item.name, item))
        scored.sort(key=lambda t: (t[0], t[1]))
        return [item for _, _, item in scored[:limit]]

    async def ensure_indexed(self, items: list[LibraryItem] | None = None) -> int:
        """Backfill embeddings for items missing the current model's vector.
        Returns how many were embedded (0 when the semantic layer is off)."""
        if self._embedder is None:
            return 0
        targets = items if items is not None else await self._index.list_items(limit=1000)
        missing = [i for i in targets if not (i.vectors or {}).get(self._embedder_model)]
        if not missing:
            return 0
        vectors = await self._embedder.embed(
            [embeddable_text(i.name, i.description, i.tags) for i in missing]
        )
        for item, vector in zip(missing, vectors, strict=True):
            item.vectors = {**(item.vectors or {}), self._embedder_model: vector}
            # ONE column: the rows above were read up to an embedding call ago,
            # and a whole-row upsert from them reverted any activate/retire and
            # lost `uses` counts written meanwhile
            await self._index.set_vector(item.id, self._embedder_model, vector)
        return len(missing)

    @property
    def _embedder_model(self) -> str:
        assert self._embedder is not None  # callers guard the None case
        return str(self._embedder.model)

    async def _query_vector(self, query: str) -> list[float] | None:
        if self._embedder is None:
            return None
        try:
            return (await self._embedder.embed([query]))[0]
        except Exception:
            return None  # semantic half down: keyword carries the search
