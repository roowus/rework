"""Domain records persisted by the StateStore.

Timestamps are float epoch seconds (recoverable wait deadlines are epoch-based by
design — docs/adr/0001). Run ids are deterministic: `mission:step:role:attempt`
(idempotent create_run is the crash-recovery backbone).
"""

from __future__ import annotations

import time
import uuid
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


def now() -> float:
    return time.time()


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def run_id(mission_id: str, step: str, role: str, attempt: int) -> str:
    """Deterministic run id — create_run is idempotent on this value."""
    return f"{mission_id}:{step}:{role}:{attempt}"


class MissionStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_GATE = "awaiting_gate"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunStatus(StrEnum):
    INTENT = "intent"  # dispatched on paper, engine has not started it yet
    RUNNING = "running"
    HALTED_ESCALATED = "halted_escalated"  # paused, waiting on orchestrator resolution
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"


class GateKind(StrEnum):
    SHIP = "ship"
    SPAWN = "spawn"
    OPEN_PR = "open_pr"
    GENERIC = "generic"


class GateStatus(StrEnum):
    OPEN = "open"
    APPROVED = "approved"
    REJECTED = "rejected"


class EventType(StrEnum):
    MISSION_CREATED = "mission_created"
    PLAN_CREATED = "plan_created"
    ROLE_SPAWNED = "role_spawned"
    PROGRESS = "progress"
    ESCALATED = "escalated"
    ROLE_HALTED = "role_halted"
    ROLE_COMPLETED = "role_completed"
    MODEL_PROMOTED = "model_promoted"
    GATE_OPENED = "gate_opened"
    GATE_DECIDED = "gate_decided"
    MISSION_DONE = "mission_done"
    MISSION_FAILED = "mission_failed"
    CANCELLED = "cancelled"
    INFO = "info"


class Mission(BaseModel):
    id: str
    objective: str
    repo_path: str | None = None
    status: MissionStatus = MissionStatus.PENDING
    plan: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    created_at: float = Field(default_factory=now)
    updated_at: float = Field(default_factory=now)


class Run(BaseModel):
    id: str
    mission_id: str
    role: str
    step: str
    attempt: int = 1
    status: RunStatus = RunStatus.INTENT
    model_ref: dict[str, Any] = Field(default_factory=dict)  # catalog id + tier + effort
    context: dict[str, Any] = Field(default_factory=dict)  # task context built by dispatch
    artifact: dict[str, Any] | None = None  # typed artifact returned on completion
    escalation: dict[str, Any] | None = None  # pending/historical escalation envelope
    redirect: str | None = None  # check-in redirect instruction for the engine to apply
    created_at: float = Field(default_factory=now)
    updated_at: float = Field(default_factory=now)


class Gate(BaseModel):
    id: str
    mission_id: str
    kind: GateKind
    question: str
    status: GateStatus = GateStatus.OPEN
    decision_note: str | None = None
    created_at: float = Field(default_factory=now)
    decided_at: float | None = None


class EventRecord(BaseModel):
    mission_id: str | None = None
    run_id: str | None = None
    type: EventType = EventType.INFO
    payload: dict[str, Any] = Field(default_factory=dict)
    ts: float = Field(default_factory=now)
    seq: int | None = None  # assigned by the store on append


class Charge(BaseModel):
    """One model call's cost — the ledger's atomic entry (accounting exact,
    enforcement approximate: see docs/design.md#kernel)."""

    mission_id: str | None = None
    run_id: str | None = None
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    ts: float = Field(default_factory=now)


class Budget(BaseModel):
    max_usd: float | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None


class BudgetState(BaseModel):
    spent_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0


class BudgetExceeded(Exception):
    def __init__(self, state: BudgetState, budget: Budget) -> None:
        self.state = state
        self.budget = budget
        super().__init__(
            f"budget exceeded: spent ${state.spent_usd:.4f} / ${budget.max_usd}"
            if budget.max_usd is not None
            else "budget exceeded (token cap)"
        )
