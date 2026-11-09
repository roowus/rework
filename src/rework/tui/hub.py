"""The Hub: the TUI's one heartbeat.

It polls the cheap global state (missions, open gates, projects, chats, spend
today) on an interval, keeps the last good snapshot, and hands it to every view.
Views never run their own global polls; detail views (an open mission's events)
use `Hub.guard` so a failed query degrades to "offline", never to a crash.

Failure contract (docs/TUI-HANDOFF.md §7 trap #1/#2): nothing in here raises
into the event loop. A failed refresh marks the snapshot offline and keeps the
previous data."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from rework.tui.data import TuiData

log = logging.getLogger("rework.tui")


def midnight() -> float:
    lt = time.localtime()
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


@dataclass
class Snapshot:
    missions: list[Any] = field(default_factory=list)
    gates: list[Any] = field(default_factory=list)  # open gates, all missions
    projects: list[Any] = field(default_factory=list)
    chats: list[Any] = field(default_factory=list)
    spend_today: float = 0.0
    online: bool = True
    error: str = ""
    loaded: bool = False  # False until the first successful refresh
    at: float = 0.0

    def project_name(self, project_id: str | None) -> str:
        if not project_id:
            return ""
        for p in self.projects:
            if p.id == project_id:
                return str(p.name)
        return project_id

    def mission(self, mission_id: str | None) -> Any:
        for mission in self.missions:
            if mission.id == mission_id:
                return mission
        return None

    def gates_for(self, mission_id: str) -> list[Any]:
        return [g for g in self.gates if g.mission_id == mission_id]


class Hub:
    def __init__(self, data: TuiData | None = None) -> None:
        self.data = data or TuiData()
        self.snapshot = Snapshot()
        self._listeners: list[Callable[[Snapshot], None]] = []
        self._busy = False
        # offline backoff (found live: an unreachable host was retried and
        # logged twice every 2s, 2,400+ warnings in tui.log)
        self._failures = 0
        self._next_try = 0.0
        self._on_offline: Callable[[str], None] | None = None

    def subscribe(self, fn: Callable[[Snapshot], None]) -> None:
        self._listeners.append(fn)

    def unsubscribe(self, fn: Callable[[Snapshot], None]) -> None:
        if fn in self._listeners:
            self._listeners.remove(fn)

    async def refresh(self) -> Snapshot:
        """One poll. Single-flight: an overlapping call returns the current
        snapshot instead of stacking queries on a slow link."""
        if self._busy or time.monotonic() < self._next_try:
            return self.snapshot
        self._busy = True
        try:
            snap = Snapshot(
                missions=self.snapshot.missions,
                gates=self.snapshot.gates,
                projects=self.snapshot.projects,
                chats=self.snapshot.chats,
                spend_today=self.snapshot.spend_today,
                loaded=self.snapshot.loaded,
            )
            try:
                snap.missions = await self.data.missions(limit=200)
                snap.gates = await self.data.open_gates()
                snap.projects = await self.data.projects()
                snap.chats = await self._chats()
                snap.spend_today = await self._spend_today()
                snap.online = not self.data._reconnecting_hint
                snap.error = self.data._reconnecting_hint
                snap.loaded = True
                if self._failures:
                    log.info("back online after %d failed refreshes", self._failures)
                self._failures = 0
                self._next_try = 0.0
            except Exception as exc:  # stale data with an offline marker
                self._failures += 1
                if self._failures == 1:  # log the transition, not every poll
                    log.warning("offline: %s", exc)
                self._next_try = time.monotonic() + min(30.0, 2.0**self._failures)
                snap.online = False
                snap.error = f"{type(exc).__name__}: {exc}"[:200]
            snap.at = time.time()
            self.snapshot = snap
            self._broadcast()
            return snap
        finally:
            self._busy = False

    async def _chats(self) -> list[Any]:
        try:
            chats = await self.data.chats(limit=200)
        except AttributeError:  # store without chat support
            return []
        # a mission orchestrator's own conversation is machinery, not a chat
        return [c for c in chats if not str(c.id).startswith("orch-")]

    async def _spend_today(self) -> float:
        try:
            rows = await self.data.spend_history(bucket="total", since=midnight())
        except AttributeError:
            return 0.0
        return sum(float(r.get("cost_usd") or 0) for r in rows)

    def _broadcast(self) -> None:
        for fn in list(self._listeners):
            try:
                fn(self.snapshot)
            except Exception:  # a broken view must not starve the others
                log.exception("hub listener failed")

    async def guard(self, call: Callable[[], Awaitable[Any]], default: Any) -> Any:
        """Run a detail query; on failure mark offline and return `default`."""
        try:
            return await call()
        except Exception as exc:
            if self.snapshot.online:
                log.warning("query failed: %s", exc)
                self.snapshot.online = False
                self.snapshot.error = f"{type(exc).__name__}: {exc}"[:200]
                self._broadcast()
            return default

    async def aclose(self) -> None:
        try:
            await self.data.aclose()
        except Exception:
            log.debug("data close failed", exc_info=True)
