"""Store resilience: ride out a dropped link without losing or doubling a write.

`ResilientStore` wraps any StateStore (both backends). A call that fails with a
transient error (a dropped Postgres link, a locked SQLite file) is retried with
capped exponential backoff, reconnecting first when the backend can (Postgres).

Only calls that are safe to repeat are retried:

- reads;
- writes that set absolute values (update_*, heartbeat/release_lock) or are keyed
  by a deterministic id (create_run is ON CONFLICT DO NOTHING);
- append_event and charge, WHEN they carry a dedupe_key (a repeat is a no-op).

Everything else (create_mission, create_gate, decide_gate, acquire_lock, un-keyed
append_event, ...) is attempted once. The connection is still rebuilt, so the next
call works, but the error reaches the caller: a blind repeat could apply the write
twice or re-decide a gate.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import random
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import psycopg

from rework.kernel.state.store import StateStore

log = logging.getLogger("rework.store")

TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (
    psycopg.OperationalError,
    psycopg.InterfaceError,
    sqlite3.OperationalError,
)

_READS = frozenset(
    {
        "get_mission",
        "list_missions",
        "list_missions_light",
        "list_missions_for_pickup",
        "next_pending_missions",
        "get_project",
        "list_projects",
        "get_run",
        "list_runs",
        "list_runs_by_status",
        "get_gate",
        "list_open_gates",
        "list_gates",
        "events_since",
        "mission_spend",
        "spend_by_model",
        "run_charges",
        "spend_history",
        "get_chat",
        "list_chats",
        "chat_messages",
        "chat_messages_max_id",
        "chat_messages_since",
        "list_complaints",
    }
)
_REPEATABLE_WRITES = frozenset(
    {
        "update_mission",
        "update_run",
        "update_project",
        "update_chat",
        "update_complaint",
        "create_run",
        "heartbeat_lock",
        "release_lock",
    }
)
_KEYED_WRITES = frozenset({"append_event", "charge"})


def is_transient(exc: BaseException) -> bool:
    """True for errors a retry (after a reconnect) can cure: a link drop, a lock."""
    return isinstance(exc, TRANSIENT_ERRORS)


def is_repeatable(method: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> bool:
    if method in _READS or method in _REPEATABLE_WRITES:
        return True
    if method in _KEYED_WRITES:
        record = args[0] if args else next(iter(kwargs.values()), None)
        return getattr(record, "dedupe_key", None) is not None
    return False


def _subject(method: str, args: tuple[Any, ...]) -> str:
    """The run (or mission) a store call is about, for the outage log line."""
    if method.endswith("_run") and args and isinstance(args[0], str):
        return args[0]
    for arg in args:
        for attr in ("run_id", "mission_id"):
            value = getattr(arg, attr, None)
            if isinstance(value, str) and value:
                return value
    return "-"


@dataclass(frozen=True)
class RetryPolicy:
    """Backoff for one outage episode. `max_elapsed_s=None` retries for as long as
    the caller waits; the default gives up after five minutes."""

    max_elapsed_s: float | None = 300.0
    base_delay_s: float = 0.25
    max_delay_s: float = 5.0
    jitter: float = 0.2

    def delay(self, attempt: int) -> float:
        exponent = min(max(attempt - 1, 0), 30)
        capped = min(self.max_delay_s, self.base_delay_s * (2**exponent))
        return capped * (1 + random.uniform(0, self.jitter))


Reopen = Callable[[], Awaitable[StateStore]]


class ResilientStore:
    """A StateStore proxy: transient failures are retried, the link is rebuilt when
    `reopen` is given (Postgres), and the caller sees one warning per outage and one
    info on recovery, not a line per attempt."""

    def __init__(
        self,
        inner: StateStore,
        *,
        reopen: Reopen | None = None,
        policy: RetryPolicy | None = None,
    ) -> None:
        self._inner: StateStore = inner
        self._reopen = reopen
        self._policy = policy or RetryPolicy()
        self._generation = 0
        self._reconnect_lock = asyncio.Lock()

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        attr = getattr(self._inner, name)
        if not inspect.iscoroutinefunction(attr):
            return attr

        async def call(*args: Any, **kwargs: Any) -> Any:
            return await self._call(name, args, kwargs)

        return call

    async def aclose(self) -> None:
        await self._inner.aclose()

    async def _call(self, method: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        loop = asyncio.get_running_loop()
        started = loop.time()
        attempts = 0
        generation = self._generation
        outage = False
        while True:
            try:
                result = await getattr(self._inner, method)(*args, **kwargs)
            except Exception as exc:
                if not is_transient(exc):
                    raise
                attempts += 1
                retry = is_repeatable(method, args, kwargs)
                budget = self._policy.max_elapsed_s
                give_up = not retry or (budget is not None and loop.time() - started >= budget)
                subject = _subject(method, args)
                if not outage:
                    outage = True
                    action = (
                        "retrying (repeat-safe)"
                        if retry
                        else "not retried: a repeat could double-apply it, so the error surfaces"
                    )
                    log.warning(
                        "store outage on %s (%s, attempt %d): %s; %s",
                        method,
                        subject,
                        attempts,
                        exc,
                        action,
                    )
                generation = await self._reconnect(generation)
                if give_up:
                    log.error(
                        "store outage on %s (%s) did not clear after %d attempts: %s",
                        method,
                        subject,
                        attempts,
                        exc,
                    )
                    raise exc
                log.debug("store retry %s attempt %d in backoff", method, attempts)
                await asyncio.sleep(self._policy.delay(attempts))
                continue
            if outage:
                log.info("store recovered on %s after %d attempts", method, attempts)
            return result

    async def _reconnect(self, seen: int) -> int:
        """Rebuild the link once per outage: a concurrent caller that already did it
        is not repeated. Without `reopen` (SQLite) this is a no-op."""
        if self._reopen is None:
            return self._generation
        async with self._reconnect_lock:
            if self._generation != seen:
                return self._generation
            try:
                fresh = await self._reopen()
            except Exception as exc:
                log.debug("store reconnect failed, will retry: %s", exc)
                return self._generation
            old, self._inner = self._inner, fresh
            self._generation += 1
            with contextlib.suppress(Exception):
                await old.aclose()
            return self._generation


def _retrying(name: str) -> Callable[..., Awaitable[Any]]:
    async def method(self: ResilientStore, *args: Any, **kwargs: Any) -> Any:
        return await self._call(name, args, kwargs)

    method.__name__ = name
    return method


# Real class attributes, one per StateStore method. The runtime protocol check
# (isinstance, which pydantic runs on RoleBuildCtx.store) reads attributes with
# getattr_static, which does not see __getattr__, so delegation must be on the class.
for _name in [
    n
    for n in dir(StateStore)
    if not n.startswith("_") and inspect.iscoroutinefunction(getattr(StateStore, n, None))
]:
    if _name not in ResilientStore.__dict__:
        setattr(ResilientStore, _name, _retrying(_name))
