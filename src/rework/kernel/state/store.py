"""StateStore: the seam everything else in rework is written against.

Two implementations (sqlite: demo/test-grade; postgres: deployment-grade) must both
pass tests/conformance/test_store.py. The store is the single source of truth:
in-process queues and caches are latency optimizations only (CLAUDE.md rule 3).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from rework.kernel.state.records import (
    BudgetState,
    Charge,
    Chat,
    ChatMessage,
    Complaint,
    ComplaintKind,
    ComplaintStatus,
    EventRecord,
    EventType,
    Gate,
    GateStatus,
    Mission,
    MissionStatus,
    Project,
    Run,
    RunStatus,
)


@runtime_checkable
class StateStore(Protocol):
    """Async persistence for missions, runs, gates, events, charges, leader lock.
    runtime_checkable so pydantic can validate RoleBuildCtx.store by instance."""

    async def aclose(self) -> None: ...

    # -- missions ---------------------------------------------------------
    async def create_mission(self, mission: Mission) -> Mission: ...

    async def get_mission(self, mission_id: str) -> Mission | None: ...

    async def update_mission(
        self,
        mission_id: str,
        *,
        status: MissionStatus | None = None,
        plan: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        repo_path: str | None = None,
        project_id: str | None = None,
    ) -> Mission | None: ...

    async def list_missions(
        self,
        status: MissionStatus | None = None,
        limit: int = 50,
        chat_id: str | None = None,
    ) -> list[Mission]: ...

    # -- projects (ADR 0005) ------------------------------------------------
    async def create_project(self, project: Project) -> Project: ...

    async def get_project(self, project_id: str) -> Project | None: ...

    async def update_project(
        self, project_id: str, *, name: str | None = None, settings: dict[str, Any] | None = None
    ) -> Project | None: ...

    async def list_projects(self, limit: int = 100) -> list[Project]: ...

    # -- runs ---------------------------------------------------------------
    async def create_run(self, run: Run) -> Run:
        """Idempotent on run.id (deterministic ids make crash recovery safe)."""
        ...

    async def get_run(self, run_id: str) -> Run | None: ...

    async def update_run(
        self,
        run_id: str,
        *,
        status: RunStatus | None = None,
        model_ref: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
        artifact: dict[str, Any] | None = None,
        escalation: dict[str, Any] | None = None,
        redirect: str | None = None,
    ) -> Run | None: ...

    async def list_runs(self, mission_id: str) -> list[Run]: ...

    async def list_runs_by_status(self, status: RunStatus) -> list[Run]: ...

    # -- gates --------------------------------------------------------------
    async def create_gate(self, gate: Gate) -> Gate: ...

    async def get_gate(self, gate_id: str) -> Gate | None: ...

    async def decide_gate(
        self,
        gate_id: str,
        approved: bool,
        note: str | None = None,
        decision: dict[str, Any] | None = None,
    ) -> Gate | None:
        """Resolve an open gate. `note` is for humans; `decision` is the TYPED
        answer handed to the paused role as its interrupt() result (None =
        the role receives a plain bool, as before)."""
        ...

    async def list_open_gates(self, mission_id: str | None = None) -> list[Gate]: ...

    async def list_gates(self, mission_id: str | None = None) -> list[Gate]: ...

    # -- events ---------------------------------------------------------------
    async def append_event(self, event: EventRecord) -> EventRecord:
        """Assigns seq and persists; seq order is the attach/replay cursor."""
        ...

    async def events_since(
        self,
        seq: int = 0,
        mission_id: str | None = None,
        limit: int = 500,
        *,
        newest_first: bool = False,
    ) -> list[EventRecord]:
        """Events after `seq` (exclusive), oldest first.

        `newest_first` reads backwards instead: it returns the NEWEST `limit`
        matching events, still ordered oldest-first inside that window. A
        forward read stops at the oldest `limit` events, so on a mission with
        more events than one page it never reaches the end of the stream
        (finding #37: the finished card's tail was rebuilt from that stale
        window)."""
        ...

    # -- ledger -----------------------------------------------------------------
    async def charge(self, charge: Charge) -> None: ...

    async def mission_spend(self, mission_id: str) -> BudgetState: ...

    async def spend_by_model(self, mission_id: str | None = None) -> list[dict[str, Any]]:
        """Ledger grouped by model: tokens and cost, one row per model.

        The Cost tab's whole point is "what is this thing costing me", which the
        per-mission total cannot answer once more than one model is in play.
        Grouped in SQL rather than in the TUI so the tab never loads every
        charge row into the client."""
        ...

    async def run_charges(self, run_id: str) -> list[Charge]:
        """Per-run charge rows, oldest first: feeds the mechanical run_stats
        section stamped onto every report artifact."""
        ...

    async def spend_history(
        self,
        *,
        bucket: str = "day",
        since: float | None = None,
        until: float | None = None,
        mission_id: str | None = None,
        group_by: str = "none",
    ) -> list[dict[str, Any]]:
        """The ledger as a TIME SERIES — the one question a single total cannot
        answer: "am I spending faster this week than last?", or "which project
        or role is eating the budget?".

        bucket:   "day" | "hour" | "week" | "total"
        group_by: "none" | "project" | "role" | "model"
        Returns rows of {bucket_start (epoch float), bucket_label (str),
        group_key (str), calls, input_tokens, output_tokens, cost_usd},
        ordered oldest bucket first then group_key.

        `project` and `role` are not columns on `charges`; they are joined from
        missions (project_id) and runs (role) via charge.run_id. A charge with
        no run (the orchestrator chat, which is billed to a session before any
        mission exists) groups under "" — callers should label that "unattributed"
        rather than dropping it, or the totals silently fail to reconcile.
        """
        ...

    # -- chats (the orchestrator conversation; how missions get created) ------
    async def create_chat(self, chat: Chat) -> Chat:
        """Insert a chat. Returns the stored row."""
        ...

    async def get_chat(self, chat_id: str) -> Chat | None: ...

    async def update_chat(
        self,
        chat_id: str,
        *,
        title: str | None = None,
        project_id: str | None = None,
        model_id: str | None = None,
        effort: str | None = None,
        mission_id: str | None = None,
    ) -> Chat | None:
        """Patch a chat row; `updated_at` always moves."""
        ...

    async def list_chats(self, limit: int = 50, include_missions: bool = True) -> list[Chat]:
        """Chats newest-first. `include_missions=False` hides chats that already
        launched a mission (they are done as conversations); the default keeps
        everything so a chat can always be reopened."""
        ...

    async def delete_chat(self, chat_id: str) -> None:
        """Delete a chat and its messages."""
        ...

    async def append_chat_message(self, message: ChatMessage) -> ChatMessage: ...

    async def chat_messages(self, chat_id: str) -> list[ChatMessage]:
        """Full transcript, oldest first."""
        ...

    async def chat_messages_max_id(self, chat_id: str) -> int:
        """Highest message id in a chat (0 when empty). The table is append-only
        and indexed on (chat_id, id), so this is an O(log n) staleness check."""
        ...

    async def chat_messages_since(self, chat_id: str, after_id: int) -> list[ChatMessage]:
        """Transcript rows newer than `after_id`, oldest first."""
        ...

    # -- leader lock (one daemon per database; atomic-upsert hardened in M3) --
    async def acquire_lock(self, owner: str, stale_after_s: float = 60.0) -> bool: ...

    async def heartbeat_lock(self, owner: str) -> bool: ...

    async def release_lock(self, owner: str) -> None: ...

    # -- complaints inbox (ADR 0004; table + tool landed early, clustering at M5) --
    async def file_complaint(self, complaint: Complaint) -> Complaint: ...

    async def list_complaints(
        self,
        status: ComplaintStatus | None = None,
        kind: ComplaintKind | None = None,
        limit: int = 100,
    ) -> list[Complaint]: ...

    async def update_complaint(
        self, complaint_id: str, *, status: ComplaintStatus | None = None
    ) -> Complaint | None: ...


def gate_is_open(gate: Gate) -> bool:
    return gate.status == GateStatus.OPEN


def event(type_: EventType, **payload: Any) -> EventRecord:
    """Convenience constructor: `event(EventType.DONE, run_id=...)`."""
    return EventRecord(type=type_, payload=payload)
