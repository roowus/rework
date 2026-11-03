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
    EventRecord,
    EventType,
    Gate,
    GateStatus,
    Mission,
    MissionStatus,
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
    ) -> Mission | None: ...

    async def list_missions(
        self, status: MissionStatus | None = None, limit: int = 50
    ) -> list[Mission]: ...

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
        self, gate_id: str, approved: bool, note: str | None = None
    ) -> Gate | None: ...

    async def list_open_gates(self, mission_id: str | None = None) -> list[Gate]: ...

    # -- events ---------------------------------------------------------------
    async def append_event(self, event: EventRecord) -> EventRecord:
        """Assigns seq and persists; seq order is the attach/replay cursor."""
        ...

    async def events_since(
        self, seq: int = 0, mission_id: str | None = None, limit: int = 500
    ) -> list[EventRecord]: ...

    # -- ledger -----------------------------------------------------------------
    async def charge(self, charge: Charge) -> None: ...

    async def mission_spend(self, mission_id: str) -> BudgetState: ...

    # -- leader lock (one daemon per database; postgres gains an advisory lock in M3) --
    async def acquire_lock(self, owner: str, stale_after_s: float = 60.0) -> bool: ...

    async def heartbeat_lock(self, owner: str) -> bool: ...

    async def release_lock(self, owner: str) -> None: ...


def gate_is_open(gate: Gate) -> bool:
    return gate.status == GateStatus.OPEN


def event(type_: EventType, **payload: Any) -> EventRecord:
    """Convenience constructor: `event(EventType.DONE, run_id=...)`."""
    return EventRecord(type=type_, payload=payload)
