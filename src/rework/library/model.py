"""Library item model (ADR 0001, design.md#the-library, MVP = M5.1).

An item is a markdown body on disk in the library git repo plus an index row
(a LibraryStore). The git repo is provenance and history; the index is what
search reads. rework's API is the only git writer (owner plan decision).

MVP scope: internal items only (skills/memories/reports auto-accept, tools
human-gated in M5.2). External ingest (permissioned sites + verification
pipeline) is M6 and stays out of this model's happy path: `origin=external`
exists in the enum but no code path creates one yet.
"""

from __future__ import annotations

import time
import uuid
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


def new_item_id() -> str:
    return f"l-{uuid.uuid4().hex[:12]}"


class ItemType(StrEnum):
    SKILL = "skill"  # a procedure: how to do a thing
    TOOL = "tool"  # an executable: other agents will run it blindly
    REPORT = "report"  # a finished artifact worth keeping
    MEMORY = "memory"  # a fact worth remembering


class ItemStatus(StrEnum):
    PROPOSED = "proposed"  # indexed, not yet served (tools start here)
    ACTIVE = "active"  # served to roles and searches
    RETIRED = "retired"  # kept for history, never served


class Origin(StrEnum):
    INTERNAL = "internal"  # produced by a rework mission or the owner
    EXTERNAL = "external"  # from the web; requires verification (M6)


class Provenance(BaseModel):
    """Where an item came from and who vouches for it."""

    origin: Origin = Origin.INTERNAL
    mission_id: str | None = None
    role: str | None = None
    project: str | None = None
    source_url: str | None = None


class LibraryItem(BaseModel):
    id: str = Field(default_factory=new_item_id)
    type: ItemType
    name: str
    description: str = ""
    body_path: str | None = None  # relative to the library repo root
    tags: list[str] = Field(default_factory=list)
    provenance: Provenance = Field(default_factory=Provenance)
    status: ItemStatus = ItemStatus.PROPOSED
    version: int = 1  # bumped on every body/metadata edit
    uses: int = 0  # retrieval counter (usage signal for ranking later)
    # model id -> embedding vector (M6.1); empty when the semantic layer is off
    vectors: dict[str, list[float]] = Field(default_factory=dict)
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)

    def touch(self) -> None:
        self.version += 1
        self.updated_at = time.time()


def item_to_row(item: LibraryItem) -> dict[str, Any]:
    """Flat row for the index tables (json columns serialized by stores)."""
    return {
        "id": item.id,
        "type": item.type.value,
        "name": item.name,
        "description": item.description,
        "body_path": item.body_path,
        "tags": item.tags,
        "provenance": item.provenance.model_dump(mode="json"),
        "status": item.status.value,
        "version": item.version,
        "uses": item.uses,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }
