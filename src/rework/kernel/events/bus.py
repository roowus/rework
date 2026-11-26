"""EventBus: typed events, in-process fan-out, write-through to the store.

The bus is a latency optimization, never the source of truth (CLAUDE.md rule 3):
every publish is persisted by the store, and consumers can always re-derive from
`store.events_since(seq)`. A real transport (Redis/NATS) can replace this behind
the same protocol at M7+.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Protocol, runtime_checkable

from rework.kernel.state.records import EventRecord
from rework.kernel.state.store import StateStore


@runtime_checkable
class EventBus(Protocol):
    async def publish(self, event: EventRecord) -> EventRecord: ...

    def subscribe(self) -> asyncio.Queue[EventRecord]:
        """New subscriber queue; receives events published after subscribing.
        Replay history via the store (`events_since`), not the bus."""

    def unsubscribe(self, queue: asyncio.Queue[EventRecord]) -> None: ...


class InProcessEventBus:
    """Fan-out to per-subscriber queues; every publish persists first."""

    def __init__(self, store: StateStore) -> None:
        self._store = store
        self._subscribers: set[asyncio.Queue[EventRecord]] = set()

    async def publish(self, event: EventRecord) -> EventRecord:
        persisted = await self._store.append_event(event)
        for q in self._subscribers:
            # non-blocking: a slow consumer never stalls the publisher. A dropped
            # event is recoverable; the store is the source of truth (events_since).
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(persisted)
        return persisted

    def deliver(self, persisted: EventRecord) -> None:
        """Fan out an event the STORE already wrote (in the same transaction
        as the row it describes, e.g. a gate decision). Never persists again."""
        for q in self._subscribers:
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(persisted)

    def subscribe(self) -> asyncio.Queue[EventRecord]:
        q: asyncio.Queue[EventRecord] = asyncio.Queue(maxsize=1000)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, queue: asyncio.Queue[EventRecord]) -> None:
        self._subscribers.discard(queue)
