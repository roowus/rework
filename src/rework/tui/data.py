"""TUI data layer: one live store connection the app shares (the TUI is a
CLIENT, same as the CLI commands: it talks to whatever REWORK_DB points at,
local sqlite or the deployment Postgres over the tailnet)."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from rework.config import (
    DEFAULT_STATE_DIR,
    Config,
    default_results_dir,
    open_store_from_config,
)
from rework.credentials import CREDENTIALS_RELOADED, is_auth_error, recover_db_credentials
from rework.kernel.state.records import Mission, Project, Run
from rework.kernel.state.store import StateStore
from rework.library.store import (
    GitLibrary,
    LibraryStore,
    open_library_store,
    read_item_body,
)


def library_root() -> Path:
    import os

    return Path(
        os.environ.get("REWORK_LIBRARY_DIR", str(DEFAULT_STATE_DIR / "library"))
    ).expanduser()


log = logging.getLogger("rework.tui")

# A read that has not answered in this long is treated as a dead connection
# (found live: an unreachable host made the first query hang for minutes).
QUERY_TIMEOUT_S = 15.0


def _is_connection_error(exc: BaseException) -> bool:
    """Whether this looks like the CONNECTION died rather than the query being
    wrong. Covers psycopg (the deployment reads Postgres over the tailnet),
    aiosqlite (local file) and any OSError wrapping a socket failure."""
    import sqlite3

    try:
        import psycopg
    except ImportError:  # pragma: no cover - psycopg is a hard dep
        psycopg = None  # type: ignore[assignment]
    connection_types: tuple[type[BaseException], ...] = (OSError, TimeoutError, ConnectionError)
    if psycopg is not None:
        connection_types = (
            *connection_types,
            psycopg.OperationalError,
            psycopg.InterfaceError,
        )
    if isinstance(exc, connection_types):
        return True
    if isinstance(exc, sqlite3.OperationalError):
        text = str(exc).lower()
        return "locked" in text or "disk i/o" in text or "unable to open" in text
    return False


async def _maybe_await(value: Any) -> Any:
    """Await it if it is awaitable.

    The query callables here are lambdas returning coroutines, but a caller
    (or a test) may hand `_resilient` a plain call that raises synchronously.
    Without this the synchronous exception escapes `await`'s try block
    uncaught and the reconnect/fallback never runs — and awaiting a non-
    awaitable raises TypeError, which is not a connection error."""
    if hasattr(value, "__await__"):
        return await value
    return value


class TuiData:
    """Lazy single connections + thin query methods. All async; the Textual
    loop is the only caller (single-connection stores are safe here)."""

    def __init__(self, store_factory: Any | None = None) -> None:
        # How the store is opened. The default is today's path exactly
        # (Config.from_env, then the shared factory); a test injects a fake.
        self._store_factory = store_factory or (lambda: open_store_from_config(Config.from_env()))
        self._store: StateStore | None = None
        # The DSN the OPEN connection really used ('' until one is opened). The
        # recovery compares the env FILE against this value, never against the
        # environment: another writer may move os.environ on without this
        # connection having adopted it, and that mismatch is what made the
        # multi-writer case flap between two passwords.
        self._dsn: str = ""
        # the URL the CURRENT open attempt used, captured right BEFORE the
        # factory is called. Only consulted when no connection was ever
        # established (an open that fails on stale credentials has no other
        # record of what it was holding); recovery never re-reads it.
        self._attempted_dsn: str = ""
        # the last rotated DSN this process adopted; kept across drop_store()
        self._recovered_dsn: str = ""
        # DSNs this process has already moved away from: a file that comes
        # back to one of them must not drag us back (no oscillation)
        self._retired_dsns: set[str] = set()
        self.own_chat_ids: set[int] = set()
        # Bumped every time the connection is thrown away or reopened. A view
        # caches chat_messages ids (a cursor, a rendered high-water mark) and
        # those are only comparable inside one connection: a reopened store can
        # hand out the same ids again, so the owner of such a cache has to
        # re-read when this changes instead of trusting the numbers.
        self.store_epoch: int = 0
        self._index: LibraryStore | None = None
        self._git: GitLibrary | None = None
        # the last successful result per query, used while the daemon is
        # unreachable (see `_resilient`)
        self._last_good: dict[str, Any] = {}
        # set when a reconnect failed; the shell shows it as an offline note
        self._reconnecting_hint: str = ""

    async def store(self) -> StateStore:
        if self._store is None:
            import os

            self._attempted_dsn = os.environ.get("REWORK_DB", "")
            store = await _maybe_await(self._store_factory())
            self._track_own_chat_writes(store)
            self._dsn = self._opened_dsn(store)
            self._store = store
            self.store_epoch += 1
        return self._store

    @staticmethod
    def _opened_dsn(store: StateStore) -> str:
        """The database URL THIS connection was opened with, captured once at
        open time. Postgres stores keep it for their own reconnects; a store
        that keeps none (sqlite, a test fake) was opened from the URL this
        process had then, so that value is the one in use. Never re-read at
        recovery time: another writer may have moved the environment on."""
        import os

        opened = getattr(store, "_dsn", "")
        if isinstance(opened, str) and opened:
            return opened
        return os.environ.get("REWORK_DB", "")

    def _track_own_chat_writes(self, store: StateStore) -> None:
        """Remember the ids of chat rows THIS process persists (the orchestrator
        writes through this same handle), so the transcript poller can tell them
        from other instances' rows, which are the only ones it must render."""
        original = store.append_chat_message

        async def tracked(message: Any) -> Any:
            saved = await original(message)
            if saved.id is not None:
                self.own_chat_ids.add(int(saved.id))
            return saved

        setattr(store, "append_chat_message", tracked)  # noqa: B010

    async def drop_store(self) -> None:
        """Throw the connection away so the next call reopens it. A store held
        across a network drop is dead: every call fails, and holding it only
        delays the recovery."""
        if self._store is not None:
            import contextlib

            with contextlib.suppress(Exception):
                await self._store.aclose()
            self._store = None
            self._dsn = ""
            self.store_epoch += 1

    def _current_dsn(self) -> str:
        """The DSN this process is actually HOLDING: the last one it adopted,
        else the open connection's, else (nothing was ever opened) the URL the
        last open attempt used. That is the value the recovery compares the env
        FILE against, so a file another writer has not updated yet can never
        pull this process back to credentials it has already left. Never
        os.environ at recovery time: that is what made it flap."""
        return self._recovered_dsn or self._dsn or self._attempted_dsn

    async def _reload_credentials(self) -> bool:
        """Apply a rotated database URL from the env file. True when the file
        holds a different URL and it was applied (so the reopen uses it). The
        URL is never logged: it carries the password."""
        try:
            # `avoid`: a file that momentarily points back at credentials this
            # process has already left must not drag it back (the two-writer
            # flap). The filter lives in the decision, so nothing is written to
            # the environment before the veto.
            fresh = await asyncio.to_thread(
                recover_db_credentials,
                current_url=self._current_dsn(),
                avoid=self._retired_dsns,
            )
        except Exception:  # pragma: no cover - recovery must never mask the error
            return False
        if fresh is not None:
            held = self._current_dsn()
            if held:
                self._retired_dsns.add(held)
            self._recovered_dsn = fresh
            # the library index (library-mcp holds it for the whole session)
            # reads a URL out of Config.from_env too, so drop the cached handle
            # and let `library()` reopen on the fresh credentials
            await self._drop_library()
        return fresh is not None

    async def _drop_library(self) -> None:
        index = self._index
        self._index = None
        self._git = None
        if index is not None:
            import contextlib

            with contextlib.suppress(Exception):
                await index.aclose()

    async def _call(self, fn: Any) -> Any:
        """One query against the (possibly reopened) store."""
        store = await self.store()
        return await fn(store)

    async def _resilient(self, key: str, call: Any, *, stale_ok: bool = True) -> Any:
        """Run one query; if the connection died, reconnect once and retry.

        The TUI reads a Postgres over the tailnet. A laptop sleeping, a
        tailnet hiccup or a daemon restart all surface as
        'consuming input failed' / 'server closed the connection', and an
        unhandled one used to take the WHOLE app down with a traceback screen
        (owner's screenshot). Stale data with a visible 'offline' note is the
        right failure mode for a dashboard, and the last good result keeps the
        table populated instead of blanking it.

        `stale_ok=False` opts a query out of the last-good fallback: it raises
        instead, for a value where an old answer would be a false claim (the
        spinner's event stamps: a stale stamp reads as "silent for 7m")."""
        try:
            result = await asyncio.wait_for(_maybe_await(call()), QUERY_TIMEOUT_S)
        except Exception as first:
            if not _is_connection_error(first):
                raise
            # Credentials rotated on disk (Postgres password change): the dead
            # connection is not the whole problem, the URL in this process is
            # stale too, so re-read the env file BEFORE reopening. An unchanged
            # URL returns None and the reconnect below runs exactly as before.
            reloaded = is_auth_error(first) and await self._reload_credentials()
            await self.drop_store()
            try:
                result = await asyncio.wait_for(_maybe_await(call()), QUERY_TIMEOUT_S)
            except Exception as second:
                # The reopen is the other place a rotated password can surface:
                # found live, the socket dies first (idle timeout, DB restart)
                # and only the RECONNECT meets the new password. Same rule as
                # above, and still bounded: at most 2 reconnects in total, and
                # the second one only when the URL actually CHANGED.
                if not (is_auth_error(second) and await self._reload_credentials()):
                    self._reconnecting_hint = f"{type(first).__name__}: {first}"[:160]
                    if stale_ok and key in self._last_good:
                        return self._last_good[key]
                    raise
                await self.drop_store()
                try:
                    result = await asyncio.wait_for(_maybe_await(call()), QUERY_TIMEOUT_S)
                except Exception:
                    # Bounded at 3 opens, so this failure ends the ladder the
                    # same way every other one does: keep the last good payload
                    # behind the offline note instead of escaping the caller.
                    self._reconnecting_hint = f"{type(first).__name__}: {first}"[:160]
                    if stale_ok and key in self._last_good:
                        return self._last_good[key]
                    raise
                reloaded = True
            if reloaded:
                log.info(CREDENTIALS_RELOADED)
        self._reconnecting_hint = ""
        if stale_ok:
            self._last_good[key] = result
        return result

    async def library(self) -> tuple[LibraryStore, GitLibrary]:
        if self._index is None or self._git is None:
            config = Config.from_env()
            self._index = await open_library_store(config.db)
            self._git = GitLibrary(library_root(), author=config.git_author)
        return self._index, self._git

    async def aclose(self) -> None:
        if self._store is not None:
            await self._store.aclose()
            self._store = None
        if self._index is not None:
            await self._index.aclose()
            self._index = None

    # -- queries ----------------------------------------------------------

    async def projects(self) -> list[Project]:
        return await self._resilient("projects", lambda: self._call(lambda s: s.list_projects()))

    async def missions(self, limit: int = 60) -> list[Mission]:
        """The slim mission list the poll carries: no plan, no result, objective
        clipped (`Mission.light` is True). A view that needs them calls
        get_mission for the full row."""
        return await self._resilient(
            f"missions:{limit}", lambda: self._call(lambda s: s.list_missions_light(limit=limit))
        )

    async def open_gates(self, mission_id: str | None = None) -> list[Any]:
        return await self._resilient(
            f"open_gates:{mission_id}",
            lambda: self._call(lambda s: s.list_open_gates(mission_id)),
        )

    async def events_tail(self, mission_id: str, since: int, limit: int = 500) -> list[Any]:
        return await self._resilient(
            f"events:{mission_id}:{since}",
            lambda: self._call(lambda s: s.events_since(since, mission_id=mission_id, limit=limit)),
        )

    async def events_after(self, seq: int, limit: int = 200) -> list[Any]:
        """Events after `seq` across all missions: the shell tails this to toast
        budget raises (a raise nobody notices is a raise the policy wasted)."""
        return await self._resilient(
            f"events_after:{seq}",
            lambda: self._call(lambda s: s.events_since(seq, limit=limit)),
        )

    async def newest_event_seq(self) -> int:
        """The newest event seq, to start a tail from "now" (a fresh shell must
        not toast the raises of missions that finished yesterday)."""
        events = await self._resilient(
            "newest_event_seq",
            lambda: self._call(lambda s: s.events_since(0, limit=1, newest_first=True)),
        )
        return int(events[-1].seq or 0) if events else 0

    async def recent_event_stamps(
        self, mission_id: str, limit: int = 40, deep_limit: int = 400
    ) -> dict[str, float]:
        """Newest event epoch for a mission and for each run seen in its newest
        `limit` events: {mission_id: ts, run_id: ts, ...}. Empty = nothing seen.

        ONE store query (the newest `limit` events), then a scan in Python.

        A busy interleaved mission can push a quiet run's newest event out of
        that window. Only a SATURATED window (it returned `limit` rows) can hide
        a run, so only then: one `list_runs`, and if a running run has no key,
        one wider read of `deep_limit` events to find it. Worst case is 3
        queries for a mission, 1 for most. A run still unseen after that has NO
        key, which the caller treats as unknown and animates (never a borrowed
        mission stamp).

        One wrapper with its own key and NO last-good fallback: on a store
        failure this raises (the caller treats that as "unknown", which
        animates) instead of replaying an old stamp that would claim the run
        has been silent."""

        def _fold(rows: list[Any], out: dict[str, float]) -> None:
            # the store returns a window oldest-first, so take the max per key
            # instead of relying on order
            for row in rows:
                if not row.ts:
                    continue
                ts = float(row.ts)
                for key in (mission_id, str(row.run_id) if row.run_id else None):
                    if key is not None and ts > out.get(key, 0.0):
                        out[key] = ts

        async def _window(n: int) -> list[Any]:
            return await self._call(
                lambda s: s.events_since(0, mission_id=mission_id, limit=n, newest_first=True)
            )

        async def _q() -> dict[str, float]:
            rows = await _window(limit)
            out: dict[str, float] = {}
            _fold(rows, out)
            if len(rows) >= limit:
                runs = await self._call(lambda s: s.list_runs(mission_id))
                live = [
                    r.id for r in runs if str(getattr(r.status, "value", r.status)) == "running"
                ]
                if any(rid not in out for rid in live):
                    _fold(await _window(deep_limit), out)
            return out

        return await self._resilient(f"stamps:{mission_id}", _q, stale_ok=False)

    async def mission_runs(self, mission_id: str) -> list[Run]:
        return await self._resilient(
            f"runs:{mission_id}", lambda: self._call(lambda s: s.list_runs(mission_id))
        )

    async def mission_spend(self, mission_id: str) -> float:
        async def _q() -> float:
            store = await self.store()
            spend = await store.mission_spend(mission_id)
            return spend.spent_usd

        return await self._resilient(f"spend:{mission_id}", _q)

    async def spend_by_model(self, mission_id: str | None = None) -> list[dict[str, Any]]:
        return await self._resilient(
            f"spend_by_model:{mission_id}",
            lambda: self._call(lambda s: s.spend_by_model(mission_id)),
        )

    async def spend_total(self) -> dict[str, float]:
        """Every model's row summed: the number the owner actually wants."""
        rows = await self.spend_by_model()
        return {
            "usd": sum(float(r.get("cost_usd") or 0) for r in rows),
            "in": sum(int(r.get("input_tokens") or 0) for r in rows),
            "out": sum(int(r.get("output_tokens") or 0) for r in rows),
            "calls": sum(int(r.get("calls") or 0) for r in rows),
        }

    async def get_mission(self, mission_id: str) -> Any:
        return await self._resilient(
            f"mission:{mission_id}", lambda: self._call(lambda s: s.get_mission(mission_id))
        )

    async def library_handle(self) -> Any:
        """A LibraryHandle for the chat's tool loop (search from a conversation).
        None when the deployment has no library configured."""
        try:
            from rework.library.tools import LibraryHandle

            index, git = await self.library()
            return LibraryHandle(index=index, git=git)
        except Exception:
            return None

    async def library_set_status(self, item_id: str, retire: bool) -> bool:
        """a / d on the library split: activate or retire the selected item."""
        index, _git = await self.library()
        item = await index.get_item(item_id)
        if item is None:
            return False
        from rework.library.model import ItemStatus

        item.status = ItemStatus.RETIRED if retire else ItemStatus.ACTIVE
        item.touch()
        await index.upsert_item(item)
        return True

    async def mission_result_path(self, mission_id: str) -> str | None:
        """Where the daemon says this mission's finished worktree lives (None =
        nothing to fetch). The repo_path column is daemon-local until fetch
        pulls it; the CLI fetch result lands in ~/rework/results/<id>/."""
        store = await self.store()
        mission = await store.get_mission(mission_id)
        if mission is None or not mission.repo_path:
            return None
        return str(default_results_dir() / mission_id)

    async def fetch_result(self, mission_id: str) -> tuple[str, int]:
        """Pull the final worktree over the workspace API (same call the CLI
        makes). Returns (destination, file count). Raises on failure."""
        import io
        import os
        import tarfile

        import httpx

        from rework.orchestrator.workspace import workspace_url_from_env

        token = os.environ.get("REWORK_TOKEN")
        url = f"{workspace_url_from_env().rstrip('/')}/workspaces/results/{mission_id}"
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        resp = await httpx.AsyncClient(timeout=300.0).get(url, headers=headers)
        if resp.status_code == 404:
            raise FileNotFoundError(f"nothing to fetch for {mission_id}")
        resp.raise_for_status()
        dest = default_results_dir() / mission_id
        dest.mkdir(parents=True, exist_ok=True)
        count = 0
        with tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz") as tar:
            for member in tar.getmembers():
                tar.extract(member, dest, filter="data")
                count += 1
        return str(dest), count

    async def decide_gate(
        self,
        gate_id: str,
        approved: bool,
        *,
        note: str | None = None,
        decision: dict[str, Any] | None = None,
        send_back: bool = False,
    ) -> bool:
        """Answer a gate. `note` is human-facing; `decision` is a TYPED answer
        the paused role receives as its interrupt() result (e.g. free-text
        feedback, or a choice among options the gate offered)."""
        store = await self.store()
        gate = await store.decide_gate(gate_id, approved, note, decision, send_back)
        if gate is None:
            return False
        if send_back:  # the third verdict: the mission resumes with `note` as feedback
            return gate.status.value == "sent_back"
        return gate.status.value == ("approved" if approved else "rejected")

    async def set_mission_status(self, mission_id: str, status: str) -> Any:
        """Owner controls: cancel / pause / resume. The daemon reads the row, so
        this works from any machine — no daemon RPC needed."""
        from rework.kernel.state.records import MissionStatus

        store = await self.store()
        return await store.update_mission(mission_id, status=MissionStatus(status))

    async def cancel_mission(self, mission_id: str) -> Any:
        return await self.set_mission_status(mission_id, "cancelled")

    async def pause_mission(self, mission_id: str) -> Any:
        return await self.set_mission_status(mission_id, "on_hold")

    async def resume_mission(self, mission_id: str) -> Any:
        return await self.set_mission_status(mission_id, "running")

    async def retry_pr(self, mission_id: str) -> tuple[bool, str]:
        """Retry push+PR for a finished mission whose delivery failed. Shares
        `rework.orchestrator.delivery.retry_pr` with `rework mission retry-pr`,
        so the button and the command can never disagree."""
        from rework.kernel.events.bus import InProcessEventBus
        from rework.orchestrator.delivery import retry_pr

        store = await self.store()
        return await retry_pr(store, InProcessEventBus(store), mission_id)

    async def prioritize_mission(self, mission_id: str, priority: str = "high") -> Any:
        """Pickup priority (normal | high): the daemon reads the row, so this
        works from any machine and never touches a running mission."""
        store = await self.store()
        return await store.update_mission(mission_id, priority=priority)

    async def spend_history(self, **kwargs: Any) -> list[dict[str, Any]]:
        return await self._resilient(
            f"spend_history:{sorted(kwargs.items())}",
            lambda: self._call(lambda s: s.spend_history(**kwargs)),
        )

    # -- chats -------------------------------------------------------------

    async def chats(self, limit: int = 50, include_missions: bool = True) -> list[Any]:
        return await self._resilient(
            f"chats:{limit}:{include_missions}",
            lambda: self._call(
                lambda s: s.list_chats(limit=limit, include_missions=include_missions)
            ),
        )

    async def chat(self, chat_id: str) -> Any:
        return await self._resilient(
            f"chat:{chat_id}", lambda: self._call(lambda s: s.get_chat(chat_id))
        )

    async def new_chat(
        self,
        *,
        title: str = "",
        project_id: str | None = None,
        model_id: str | None = None,
        effort: str | None = None,
    ) -> Any:
        from rework.kernel.state.records import Chat

        store = await self.store()
        return await store.create_chat(
            Chat(title=title, project_id=project_id, model_id=model_id, effort=effort)
        )

    async def rename_chat(self, chat_id: str, title: str) -> Any:
        store = await self.store()
        return await store.update_chat(chat_id, title=title)

    async def attach_chat_to_mission(self, chat_id: str, mission_id: str) -> Any:
        store = await self.store()
        return await store.update_chat(chat_id, mission_id=mission_id)

    async def delete_chat(self, chat_id: str) -> None:
        store = await self.store()
        await store.delete_chat(chat_id)

    async def chat_messages(self, chat_id: str) -> list[Any]:
        return await self._resilient(
            f"chat_messages:{chat_id}", lambda: self._call(lambda s: s.chat_messages(chat_id))
        )

    async def chat_messages_max_id(self, chat_id: str) -> int:
        """Cheap append-only staleness check for an open transcript."""
        return await self._resilient(
            f"chat_messages_max_id:{chat_id}",
            lambda: self._call(lambda s: s.chat_messages_max_id(chat_id)),
        )

    async def chat_messages_since(self, chat_id: str, after_id: int) -> list[Any]:
        return await self._resilient(
            f"chat_messages_since:{chat_id}",
            lambda: self._call(lambda s: s.chat_messages_since(chat_id, after_id)),
        )

    async def update_chat(self, chat_id: str, **fields: Any) -> Any:
        store = await self.store()
        return await store.update_chat(chat_id, **fields)

    async def append_chat_note(self, chat_id: str, display: str) -> Any:
        """A UI-only transcript row (e.g. 'launched m-…'). `body` stays empty so
        the row is never replayed into the model's history."""
        from rework.kernel.state.records import ChatMessage

        store = await self.store()
        return await store.append_chat_message(
            ChatMessage(chat_id=chat_id, role="note", body={}, display=display)
        )

    async def gates(self, mission_id: str | None = None) -> list[Any]:
        """Every gate (open and decided), for history views."""
        return await self._resilient(
            f"gates:{mission_id}", lambda: self._call(lambda s: s.list_gates(mission_id))
        )

    async def mission_events(self, mission_id: str, since: int = 0, limit: int = 2000) -> list[Any]:
        """Events after `since` for one mission, paging until caught up."""
        out: list[Any] = []
        cursor = since
        while True:
            batch = await self.events_tail(mission_id, cursor, limit=500)
            out.extend(batch)
            if len(batch) < 500 or len(out) >= limit:
                return out
            cursor = max(int(e.seq or 0) for e in batch)

    async def mission_events_newest(self, mission_id: str, limit: int = 5000) -> list[Any]:
        """The mission's NEWEST `limit` events, oldest-first inside the window.

        `mission_events` pages FORWARD from seq 0, so on a mission with more
        events than `limit` it answers with the OLDEST ones — rebuilding a
        finished card's tail from that window freezes stale lines again
        (finding #37). This asks the store to read backwards instead."""
        return await self._resilient(
            f"events_newest:{mission_id}:{limit}",
            lambda: self._call(
                lambda s: s.events_since(0, mission_id=mission_id, limit=limit, newest_first=True)
            ),
        )

    async def run_spend(self, run_id: str) -> float:
        """Exact cost of one run (sum of its charges)."""

        async def _q() -> float:
            store = await self.store()
            return sum(float(c.cost_usd or 0) for c in await store.run_charges(run_id))

        return await self._resilient(f"run_spend:{run_id}", _q)

    async def send_owner_message(self, mission_id: str, text: str) -> Any:
        """Talk to a running mission's orchestrator (ADR 0007): it is woken and
        sees the message before its next turn."""
        from rework.missions import send_owner_message

        return await send_owner_message(mission_id, text, store=await self.store())

    async def mission_budget(self, mission_id: str) -> Any:
        """BudgetState (spent_usd, input/output/cached tokens)."""
        return await self._resilient(
            f"budget:{mission_id}", lambda: self._call(lambda s: s.mission_spend(mission_id))
        )

    async def mission_report(self, mission_id: str) -> str:
        """The same markdown `rework report --md` writes."""
        from rework.cli import _render_md
        from rework.nodes.run_stats import attach_run_stats

        store = await self.store()
        mission = await store.get_mission(mission_id)
        if mission is None:
            return f"no such mission: {mission_id}"
        prepared: list[tuple[Any, dict[str, Any]]] = []
        for run in await store.list_runs(mission_id):
            artifact = dict(run.artifact or {})
            if artifact and "run_stats" not in artifact:
                artifact = await attach_run_stats(store, run.id, artifact)
            prepared.append((run, artifact))
        spend = await store.mission_spend(mission_id)
        return _render_md(prepared, spend, mission)

    async def library_add(
        self,
        *,
        type_: str,
        name: str,
        description: str,
        body: str,
        tags: list[str],
        project: str | None = None,
    ) -> tuple[Any, str]:
        """Same path as `rework library add --force`: git body + index row,
        curation rules applied (tools start proposed). force=True because this
        is a human typing into the TUI form, choosing the type explicitly --
        the owner-trusted path (same reasoning as the CLI's --force), not an
        agent asserting its own output is a skill."""
        from rework.library.curation import contribute
        from rework.library.model import ItemType

        index, git = await self.library()
        return await contribute(
            index,
            git,
            type_=ItemType(type_),
            name=name,
            description=description,
            body=body,
            tags=tags,
            mission_id=None,
            project=project,
            force=True,
        )

    # -- library ----------------------------------------------------------

    async def library_search(self, query: str) -> list[Any]:
        index, _ = await self.library()
        if not query.strip():
            return await index.list_items(status=None, limit=100)
        return await index.search(query, limit=50)

    async def library_hybrid_search(self, query: str) -> list[Any]:
        """Hybrid (embeddings + keyword) when REWORK_EMBEDDINGS_* is set;
        silently keyword-only otherwise. The TUI stays alive if the embedder
        errors: HybridSearch already degrades internally."""
        index, _git = await self.library()
        from rework.library.embeddings import embeddings_from_env
        from rework.library.search import HybridSearch

        embedder = embeddings_from_env()
        hybrid = HybridSearch(index, embedder)
        if embedder is not None:
            await hybrid.ensure_indexed()
        return await hybrid.search(query, limit=50)

    async def library_body(self, item_id: str) -> tuple[Any, str | None]:
        index, git = await self.library()
        item = await index.get_item(item_id)
        if item is None:
            return None, None
        # DB row first: the body comes with the index, so the reader works on
        # a machine that has no copy of the library git folder
        return item, await asyncio.to_thread(read_item_body, item, git)

    # -- writes ------------------------------------------------------------

    async def create_project(self, name: str, repos: list[dict[str, Any]] | None = None) -> Project:
        """Store settings that are always self-consistent: the transport and
        the results handling are DERIVED from the repos, never hardcoded. No
        repo means an upload project (rework keeps the files, results are
        fetched); a repo means git/push_branch."""
        from rework.kernel.state.records import Project as ProjectRecord
        from rework.missions import derive_transport

        repo_list = [dict(r) for r in repos or []]
        transport, results = derive_transport({"repos": repo_list})
        store = await self.store()
        project = ProjectRecord(
            name=name,
            settings={"transport": transport, "results": results, "repos": repo_list},
        )
        return await store.create_project(project)

    async def add_project_files(self, project_id: str, paths: list[str]) -> list[str]:
        """Push local paths into a repo-less project's files area. The HTTP
        helper is sync (shared with the CLI), so it runs off the event loop."""
        import asyncio

        from rework.project_files import add_project_files as _add

        return await asyncio.to_thread(_add, project_id, paths)

    async def create_mission(self, **kwargs: Any) -> Mission:
        from rework.missions import create_mission as _create

        return await _create(store=await self.store(), **kwargs)


def human_age(epoch: float) -> str:
    delta = max(0.0, time.time() - epoch)
    if delta < 60:
        return f"{delta:.0f}s"
    if delta < 3600:
        return f"{delta / 60:.0f}m"
    if delta < 86400:
        return f"{delta / 3600:.0f}h"
    return f"{delta / 86400:.0f}d"


def status_rich(status: str) -> str:
    """Status text with a color tag for DataTable cells (rich markup)."""
    color = {
        "running": "cyan",
        "awaiting_gate": "bold yellow",
        "on_hold": "yellow",
        "done": "green",
        "failed": "red",
        "cancelled": "dim",
        "pending": "white",
    }.get(status, "white")
    label = status.replace("_", " ")
    return f"[{color}]{label}[/{color}]"
