"""Library curation (M5.2): tiered acceptance per the owner plan.

Internal items auto-accept EXCEPT tools: other agents execute tools blindly,
so a tool is a supply chain and always starts PROPOSED until the owner
activates it (`rework library activate <id>` is the human gate; retire is the
off switch). External ingest exists only in the model enum; its verification
pipeline is M6 and no code path creates external items yet.

A SKILL is also gated, but on generality rather than trust: a skill is a
general, reusable technique (e.g. "how to make a TUI look good", "how to
write CliRunner tests with a tmp sqlite db") an agent would apply again on a
*different* task -- not a recap of the one task it just did (e.g. "toyrepo
add() planted-bug: one-line fix flips both tests green"). One-off outcomes
belong in a report, single facts in a memory. See skill_generality_ok().
"""

from __future__ import annotations

import re
from typing import Any

from rework.library.model import (
    ItemStatus,
    ItemType,
    LibraryItem,
    Origin,
)

# Deterministic, no-LLM generality gate (see skill_generality_ok). Conservative
# on purpose: it only catches the clear, cheap-to-check cases. A false
# negative (a one-off slips through as a skill) is just noise; a false
# positive just means the caller resubmits with a justification or, for a
# human at the CLI/TUI, passes force=True -- never blocked.
_MISSION_OR_ITEM_ID = re.compile(r"\b[lm]-[0-9a-f]{6,}\b")
_TASK_SPECIFIC_PHRASES = (
    "planted-bug",
    "planted bug",
    "toyrepo",
    "this mission",
    "this task",
)
# A justification must look like a sentence AND carry a word that signals the
# claim is actually about generality, not just an assertion of it. This is
# what catches "yes" / "because it is reusable" / "reusable because I say so"
# -- all short, signal-free restatements of the word "reusable" itself, never
# a reason -- while leaving a real sentence like "applies to any future,
# different repo" alone.
_MIN_JUSTIFICATION_WORDS = 5
_MIN_JUSTIFICATION_CHARS = 20
_GENERALITY_SIGNALS = (
    "any",
    "future",
    "different",
    "other",
    "reuse",
    "applies",
    "whenever",
    "generally",
    "similar",
)


def skill_generality_ok(
    name: str, description: str, reusable_because: str | None
) -> tuple[bool, str]:
    """Does this read like a general, reusable technique rather than a recap
    of one task? Returns (ok, reason-if-not). Three checks, all deterministic:

    1. A justification is required: `reusable_because` must explain why this
       applies to a future, DIFFERENT task. Missing/blank -> not ok.
    2. The name/description/justification must not name a specific mission
       or library item id, or use stock one-off recap phrasing (planted-bug,
       toyrepo, "this mission"/"this task") -- those are the textual
       fingerprints of a task summary, not a generalized technique.
    3. The justification itself must be substantive: a short sentence (not
       just "yes" or a restatement of the word "reusable") that names an
       actual generality signal ("any", "future", "different", "other",
       "reuse", "applies", "whenever", "generally", "similar"). Asserting
       reusability is not the same as explaining it.
    """
    justification = (reusable_because or "").strip()
    if not justification:
        return False, (
            "no reusable_because given: a skill needs a one-sentence justification of"
            " why this applies to a future, DIFFERENT task"
        )
    haystack = f"{name} {description} {justification}".lower()
    if _MISSION_OR_ITEM_ID.search(haystack):
        return False, "name/description/justification names a specific mission or item id"
    for phrase in _TASK_SPECIFIC_PHRASES:
        if phrase in haystack:
            return False, f"name/description reads like a one-off task recap ({phrase!r})"
    words = justification.split()
    if len(words) < _MIN_JUSTIFICATION_WORDS or len(justification) < _MIN_JUSTIFICATION_CHARS:
        return False, (
            f"reusable_because is too thin ({len(words)} word(s)): explain in a full"
            " sentence why this applies to a future, DIFFERENT task, not just assert"
            " that it does"
        )
    if not any(signal in justification.lower() for signal in _GENERALITY_SIGNALS):
        return False, (
            "reusable_because does not say WHY it generalizes: use a word like "
            + ", ".join(repr(s) for s in _GENERALITY_SIGNALS)
            + " to explain what future, different task this also applies to"
        )
    return True, ""


# External ingest MVP (M6.2): the owner allowlists SITES (hosts) in
# REWORK_LIBRARY_ALLOWLIST (comma-separated); an external item needs (1) a
# source host on that list AND (2) an explicit per-item human approval
# (rework library approve-external). Both conditions are checked on ACTIVATE,
# so nothing an agent does can shortcut the owner.


def allowed_host(source_url: str) -> str | None:
    """The source's host when it is on the allowlist, else None."""
    import os as _os
    from urllib.parse import urlparse

    raw = _os.environ.get("REWORK_LIBRARY_ALLOWLIST", "")
    allowlist = {h.strip().lower() for h in raw.split(",") if h.strip()}
    if not allowlist:
        return None
    host = (urlparse(source_url).hostname or "").lower()
    return host if host in allowlist else None


def accept_status(item_type: ItemType, origin: Origin = Origin.INTERNAL) -> ItemStatus:
    """The status an item gets when contributed. One rule table:
    internal skills/reports/memories -> ACTIVE; tools (any origin) -> PROPOSED."""
    if item_type is ItemType.TOOL or origin is Origin.EXTERNAL:
        return ItemStatus.PROPOSED
    return ItemStatus.ACTIVE


def external_gate_status(item: LibraryItem) -> tuple[bool, str]:
    """(ok_to_activate, reason). External items pass only when their host is
    allowlisted AND status was manually set by the owner (the approve-
    external command moves PROPOSED -> ACTIVE after a human reads it)."""
    if item.provenance.origin is not Origin.EXTERNAL:
        return True, "internal"
    host = allowed_host(item.provenance.source_url or "")
    if host is None:
        return (
            False,
            "source host is not on REWORK_LIBRARY_ALLOWLIST; external ingest denied",
        )
    return True, f"allowlisted source {host} + owner approval"


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
    reusable_because: str | None = None,
    force: bool = False,
) -> tuple[LibraryItem, str]:
    """The single write path for new items (role tool, MCP, CLI add, TUI
    form): writes the body, commits, indexes, applies curation. Returns
    (item, note); the note tells the caller what happened (gating included).

    `reusable_because` is the agent's one-sentence case for why a SKILL
    generalizes to a different task (see skill_generality_ok()). A skill
    that fails the check is stored as a REPORT instead -- never dropped,
    never silently kept as a skill. `force=True` is the human escape hatch
    (CLI `--force`, the TUI form): an explicit human choice is never
    second-guessed."""
    import asyncio as _asyncio

    from rework.library.model import Origin, Provenance

    effective_type = type_
    demotion_reason = ""
    body_to_write = body
    if type_ is ItemType.SKILL and not force:
        ok, reason = skill_generality_ok(name, description, reusable_because)
        if not ok:
            effective_type = ItemType.REPORT
            demotion_reason = reason
        # audit trail (medium finding): the justification and (if demoted) the
        # reason are otherwise discarded the moment the note is read -- there
        # is no model field for them and no migration is worth adding for
        # two short lines, so they ride along in the body itself, where the
        # owner can read them back on any item.
        provenance_lines = [
            f"- reusable_because: {(reusable_because or '').strip() or '(none given)'}"
        ]
        if demotion_reason:
            provenance_lines.append(f"- demoted to report: {demotion_reason}")
        else:
            provenance_lines.append("- generality gate: passed, kept as skill")
        body_to_write = f"{body}\n\n## Provenance\n" + "\n".join(provenance_lines)

    item = LibraryItem(
        type=effective_type,
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
        git.write_body, item, body_to_write, f"library: add {item.type.value} {name!r}"
    )
    # the git file is a local-history mirror; the row carries the body so
    # other machines (same index, different library dir) can read it
    item.body = body_to_write
    await index.upsert_item(item)
    note = f"contributed: {item.id} [{item.type.value}/{item.status.value}]"
    if item.status is ItemStatus.PROPOSED:
        note += " (tools are supply chain: the owner must run `rework library "
        "activate` before any role sees it)"
    if demotion_reason:
        note += (
            f"\nsaved as a report, not a skill: {demotion_reason}. A skill must be a"
            " general, reusable technique you'd apply on a DIFFERENT task, not a recap"
            " of this one. Resubmit with type=skill and a reusable_because sentence if"
            " this really generalizes."
        )
    return item, note


async def backfill_bodies(index: Any, git: Any) -> tuple[int, int]:
    """Copy body files from this machine's library git folder into the index
    row for items that have none. Rows written before `body` was a column (or
    by a machine that never had the file) have an empty body; the row is the
    source of truth now, so this is the one-time bridge.

    Idempotent: a row that already has a body is left untouched, and a row
    whose file is not on this machine is skipped, so re-running is free.
    Returns (backfilled, skipped)."""
    import asyncio as _asyncio

    backfilled = 0
    skipped = 0
    for item in await index.list_items(limit=100_000):
        if item.body:
            continue
        text = await _asyncio.to_thread(git.read_body, item)
        if not text:
            skipped += 1
            continue
        item.body = text
        await index.upsert_item(item)
        backfilled += 1
    return backfilled, skipped
