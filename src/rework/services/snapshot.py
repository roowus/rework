"""The front ends' global view in ONE read (ADR 0021 phase 3).

A TUI's heartbeat used to be eight-plus store queries per poll, issued by the
TUI itself over the tailnet: missions, open gates, projects, chats, a chat
probe, spend today, the event tail, and one stamp query per running mission.
With the daemon as the control plane those reads run HERE, next to the
database, and a client gets the result in one HTTP call (`GET /v1/snapshot`).

The rules live in this module so the daemon and the TUI's local fallback (no
daemon configured: a single-machine setup, the tests) cannot drift apart:
which chats are listed and in what order, how the spinner's event stamps are
read, where the budget-raise cursor starts.
"""

from __future__ import annotations

from typing import Any

from rework.kernel.state.records import ORCHESTRATOR_CHAT_PREFIX, EventType
from rework.kernel.state.store import StateStore

# the spinner stamp sweep: at most this many running missions per poll, each
# one small query of this many newest events
STAMP_MISSIONS = 24
EVENT_WINDOW = 40
DEEP_WINDOW = 400
# the Work list shows at most CHAT_LIMIT chats; the store is asked for more so
# the orchestrator transcripts filtered out of the window cannot starve it
CHAT_LIMIT = 200
CHAT_FETCH = 600
MISSION_LIMIT = 200


def is_orchestrator_transcript(chat: Any) -> bool:
    """A mission orchestrator's own conversation is machinery, not a chat: one
    per mission, the owner never writes in it. It stays reachable per mission;
    it is hidden from the chat list, not deleted."""
    return str(chat.id).startswith(ORCHESTRATOR_CHAT_PREFIX)


def chat_candidates(chats: list[Any]) -> list[Any]:
    """Drop orchestrator transcripts BEFORE the cap, so the busiest of them
    cannot crowd real conversations out of the window."""
    return [c for c in chats if not is_orchestrator_transcript(c)]


def listed_chats(candidates: list[Any], with_conversation: set[str]) -> list[Any]:
    """The Work list: a chat is listed iff it holds a user or assistant message
    (`with_conversation`, from one store probe). Newest MESSAGE first;
    updated_at only breaks ties, and a chat with no message time sorts last."""
    listed = [c for c in candidates if str(c.id) in with_conversation]
    listed.sort(
        key=lambda c: (
            getattr(c, "last_message_at", None) is None,
            -float(getattr(c, "last_message_at", None) or 0),
            -float(getattr(c, "updated_at", 0) or 0),
        )
    )
    return listed[:CHAT_LIMIT]


async def event_stamps(
    store: Any, mission_id: str, limit: int = EVENT_WINDOW, deep_limit: int = DEEP_WINDOW
) -> dict[str, float]:
    """Newest event epoch for a mission and for each run seen in its newest
    `limit` events: {mission_id: ts, run_id: ts, ...}. Empty = nothing seen.

    ONE store query, then a scan. A busy interleaved mission can push a quiet
    run's newest event out of that window; only a SATURATED window can hide a
    run, so only then: one `list_runs`, and if a running run has no key, one
    wider read. A run still unseen has NO key, which callers treat as unknown
    (it animates), never a borrowed mission stamp."""

    def fold(rows: list[Any], out: dict[str, float]) -> None:
        for row in rows:  # max per key: never rely on the window's order
            if not row.ts:
                continue
            ts = float(row.ts)
            for key in (mission_id, str(row.run_id) if row.run_id else None):
                if key is not None and ts > out.get(key, 0.0):
                    out[key] = ts

    rows = await store.events_since(0, mission_id=mission_id, limit=limit, newest_first=True)
    out: dict[str, float] = {}
    fold(rows, out)
    if len(rows) >= limit:
        runs = await store.list_runs(mission_id)
        live = [r.id for r in runs if str(getattr(r.status, "value", r.status)) == "running"]
        if any(rid not in out for rid in live):
            fold(
                await store.events_since(
                    0, mission_id=mission_id, limit=deep_limit, newest_first=True
                ),
                out,
            )
    return out


async def newest_event_seq(store: Any) -> int:
    rows = await store.events_since(0, limit=1, newest_first=True)
    return int(rows[-1].seq or 0) if rows else 0


async def build(
    store: StateStore,
    *,
    spend_since: float | None = None,
    events_after: int | None = None,
    mission_limit: int = MISSION_LIMIT,
) -> dict[str, Any]:
    """Everything a front end's heartbeat shows, as records.

    `spend_since` is the client's local midnight (the server does not know the
    owner's timezone). `events_after` is the budget-raise cursor: None means
    "start from now" (a fresh shell must not toast yesterday's raises), and
    the reply's `event_seq` is the cursor for the next call. A stamp query that
    fails leaves that mission unknown rather than failing the whole snapshot."""
    missions = await store.list_missions_light(limit=mission_limit)
    gates = await store.list_open_gates()
    projects = await store.list_projects()

    candidates = chat_candidates(await store.list_chats(limit=CHAT_FETCH, include_missions=True))
    with_conversation = (
        await store.chat_ids_with_conversation([str(c.id) for c in candidates])
        if candidates
        else set()
    )
    chats = listed_chats(candidates, with_conversation)

    spend_today = 0.0
    if spend_since is not None:
        rows = await store.spend_history(bucket="total", since=spend_since)
        spend_today = sum(float(r.get("cost_usd") or 0) for r in rows)

    budget_raises: list[Any] = []
    if events_after is None:
        event_seq = await newest_event_seq(store)
    else:
        events = await store.events_since(events_after, limit=200)
        # advance over the WHOLE batch: a burst of other events must not pin it
        event_seq = max([events_after, *(int(e.seq or 0) for e in events)])
        budget_raises = [e for e in events if e.type == EventType.BUDGET_RAISED]

    stamps: dict[str, float] = {}
    running = [m for m in missions if str(getattr(m.status, "value", m.status)) == "running"]
    for mission in running[:STAMP_MISSIONS]:
        try:
            stamps.update(await event_stamps(store, mission.id))
        except Exception:  # no stamp = unknown (animates), never a stale one
            continue
    if stamps:
        stamps["*"] = max(stamps.values())

    return {
        "missions": missions,
        "gates": gates,
        "projects": projects,
        "chats": chats,
        "spend_today": spend_today,
        "budget_raises": budget_raises,
        "event_seq": event_seq,
        "last_event": stamps,
    }


def to_json(snap: dict[str, Any]) -> dict[str, Any]:
    def dump(rows: list[Any]) -> list[dict[str, Any]]:
        return [r.model_dump(mode="json") for r in rows]

    return {
        "missions": dump(snap["missions"]),
        "gates": dump(snap["gates"]),
        "projects": dump(snap["projects"]),
        "chats": dump(snap["chats"]),
        "spend_today": snap["spend_today"],
        "budget_raises": dump(snap["budget_raises"]),
        "event_seq": snap["event_seq"],
        "last_event": snap["last_event"],
    }


def from_json(data: dict[str, Any]) -> dict[str, Any]:
    """The client side: typed records again, missions marked light (plan and
    result were never sent, exactly like list_missions_light)."""
    from rework.kernel.state.records import Chat, EventRecord, Gate, Mission, Project

    missions = []
    for row in data.get("missions") or []:
        mission = Mission.model_validate(row)
        mission.light = True
        missions.append(mission)
    return {
        "missions": missions,
        "gates": [Gate.model_validate(r) for r in data.get("gates") or []],
        "projects": [Project.model_validate(r) for r in data.get("projects") or []],
        "chats": [Chat.model_validate(r) for r in data.get("chats") or []],
        "spend_today": float(data.get("spend_today") or 0.0),
        "budget_raises": [EventRecord.model_validate(r) for r in data.get("budget_raises") or []],
        "event_seq": int(data.get("event_seq") or 0),
        "last_event": {str(k): float(v) for k, v in (data.get("last_event") or {}).items()},
    }
