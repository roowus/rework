"""Library curation (M5.2): tiered acceptance per the owner plan.

Internal items auto-accept EXCEPT tools: other agents execute tools blindly,
so a tool is a supply chain and always starts PROPOSED until the owner
activates it (`rework library activate <id>` is the human gate; retire is the
off switch). External ingest exists only in the model enum; its verification
pipeline is M6 and no code path creates external items yet.
"""

from __future__ import annotations

from typing import Any

from rework.library.model import (
    ItemStatus,
    ItemType,
    LibraryItem,
    Origin,
)


def accept_status(item_type: ItemType, origin: Origin = Origin.INTERNAL) -> ItemStatus:
    """The status an item gets when contributed. One rule table:
    internal skills/reports/memories -> ACTIVE; tools (any origin) -> PROPOSED."""
    if item_type is ItemType.TOOL or origin is Origin.EXTERNAL:
        return ItemStatus.PROPOSED
    return ItemStatus.ACTIVE


async def contribute(
    index: Any,
    git: Any,
    *,
    type_: ItemType,
    name: str,
    description: str,
    body: str,
    tags: list[str],
    mission_id: str | None = None,
    role: str | None = None,
    project: str | None = None,
    requested_status: ItemStatus = ItemStatus.ACTIVE,
) -> tuple[LibraryItem, str]:
    """The single write path for new items (role tool AND CLI add): writes the
    body, commits, indexes, applies curation. Returns (item, note); the note
    tells a contributing agent what happened (gating included)."""
    import asyncio as _asyncio

    from rework.library.model import Origin, Provenance

    item = LibraryItem(
        type=type_,
        name=name,
        description=description,
        tags=tags,
        provenance=Provenance(
            origin=Origin.INTERNAL, mission_id=mission_id, role=role, project=project
        ),
        status=requested_status,
    )
    item.status = accept_status(item.type, item.provenance.origin)
    item.body_path = await _asyncio.to_thread(
        git.write_body, item, body, f"library: add {item.type.value} {name!r}"
    )
    await index.upsert_item(item)
    note = f"contributed: {item.id} [{item.type.value}/{item.status.value}]"
    if item.status is ItemStatus.PROPOSED:
        note += " (tools are supply chain: the owner must run `rework library "
        "activate` before any role sees it)"
    return item, note
