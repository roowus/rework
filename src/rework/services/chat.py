"""A chat turn, run by the DAEMON (ADR 0021 phase 2b).

The TUI used to run the orchestrator chat in its own process: each TUI built its
own engine, called the model with its own keys, and two TUIs on one chat raced
on the same transcript. The daemon is the control plane, so it runs the turn:
one engine per chat, one turn at a time per chat (a lock, because only one
daemon process serves turns), and every TUI just renders the rows it writes.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Any

from rework.kernel.state.store import StateStore

_locks: dict[str, asyncio.Lock] = {}
_engines: dict[str, tuple[tuple[str, str], Any]] = {}


def _lock(chat_id: str) -> asyncio.Lock:
    lock = _locks.get(chat_id)
    if lock is None:
        lock = _locks[chat_id] = asyncio.Lock()
    return lock


async def _context(store: StateStore, project_id: str | None) -> str:
    """The same facts the TUI's context note gives the orchestrator, built from
    the store instead of a TUI snapshot."""
    from rework.tui.conversation import context_note
    from rework.tui.hub import Snapshot

    snap = Snapshot(
        missions=await store.list_missions_light(limit=200),
        projects=await store.list_projects(),
    )
    return context_note(snap, project_id)


async def _engine(store: StateStore, chat: Any, library: Any | None) -> Any:
    from rework.config import load_catalog
    from rework.kernel.llm.adapters import build_provider
    from rework.orchestrator.chat import OrchestratorChat
    from rework.tui.conversation import default_model, items_from_messages

    catalog = load_catalog()
    model_id = chat.model_id or default_model(catalog)
    key = (model_id, chat.effort or "")
    cached = _engines.get(chat.id)
    context = await _context(store, chat.project_id)
    if cached is not None and cached[0] == key:
        engine = cached[1]
        engine._context = context
        return engine
    engine = OrchestratorChat(
        build_provider(catalog.resolve(model_id)),
        catalog,
        model_id,
        effort=chat.effort or None,
        context=context,
        store=store,
        chat_id=chat.id,
        library=library,
    )
    await engine.load()
    # proposals still pending in the stored transcript come back pending
    items = items_from_messages(await store.chat_messages(chat.id))
    engine.proposals = [i.proposal for i in items if i.kind == "proposal" and i.live]
    _engines[chat.id] = (key, engine)
    return engine


def result_json(result: Any) -> dict[str, Any]:
    return {
        "reply": result.reply,
        "proposals": [asdict(p) for p in result.proposals],
        "launched_all": result.launched_all,
        "scrapped_all": result.scrapped_all,
        "asked": result.asked,
        "turn_tokens": result.turn_tokens,
        "turn_cost_usd": result.turn_cost_usd,
    }


async def send(
    store: StateStore,
    chat_id: str,
    text: str,
    *,
    images: list[str] | None = None,
    library: Any | None = None,
) -> dict[str, Any]:
    """Run ONE owner turn on the chat and return its result. Turns on the same
    chat are serialized; turns on different chats run concurrently."""
    chat = await store.get_chat(chat_id)
    if chat is None:
        raise KeyError(chat_id)
    async with _lock(chat_id):
        engine = await _engine(store, chat, library)
        result = await engine.send(text, images=images)
        return result_json(result)
