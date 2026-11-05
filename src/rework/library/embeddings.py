"""Embedding seam (M6.1): one async call, one vector out.

Deliberately tiny: rework's model catalog is for CHAT; embeddings ride a
separate OpenAI-compatible endpoint (owner config: REWORK_EMBEDDINGS_URL +
REWORK_EMBEDDINGS_KEY + REWORK_EMBEDDINGS_MODEL). When unset, the semantic
layer is OFF and search() falls back to keyword-only — sqlite deployments and
air-gapped installs lose nothing.
"""

from __future__ import annotations

import os
from typing import Protocol, runtime_checkable


class EmbeddingsOff(Exception):
    pass


@runtime_checkable
class Embedder(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]: ...


def embeddings_from_env() -> Embedder | None:
    """The configured embedder, or None when the semantic layer is off."""
    url = os.environ.get("REWORK_EMBEDDINGS_URL")
    model = os.environ.get("REWORK_EMBEDDINGS_MODEL")
    if not url or not model:
        return None
    return OpenAICompatEmbedder(url, model, os.environ.get("REWORK_EMBEDDINGS_KEY"))


class OpenAICompatEmbedder:
    """POST /embeddings {model, input} -> data[].embedding. httpx client per
    call batch; timeouts hard (a hung embedder must not hang search)."""

    def __init__(self, base_url: str, model: str, api_key: str | None) -> None:
        self._url = base_url.rstrip("/") + "/embeddings"
        self.model = model  # public: HybridSearch keys item vectors by it
        self._key = api_key

    async def embed(self, texts: list[str]) -> list[list[float]]:
        import httpx

        headers = {"Authorization": f"Bearer {self._key}"} if self._key else {}
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                self._url,
                json={"model": self.model, "input": texts},
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()["data"]
            return [item["embedding"] for item in data]


def cosine(a: list[float], b: list[float]) -> float:
    import math

    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def embeddable_text(name: str, description: str, tags: list[str]) -> str:
    """What an item's vector represents (kept in ONE place so indexing and
    query-time can never drift)."""
    return f"{name}\n{description}\n{' '.join(tags)}"
