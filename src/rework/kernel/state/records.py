"""Domain records persisted by the StateStore.

Timestamps are float epoch seconds (recoverable wait deadlines are epoch-based by
design; see docs/adr/0001). Run ids are deterministic: `mission:step:role:attempt`
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
    """Deterministic run id: create_run is idempotent on this value."""
    return f"{mission_id}:{step}:{role}:{attempt}"


class MissionStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_GATE = "awaiting_gate"
    ON_HOLD = "on_hold"  # user pause or burn-gate (ADR 0003)
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
    # asked by a mission orchestrator (ADR 0007); Gate.spec says how to answer
    QUESTION = "question"  # free-text answer -> decision {"answer": str}
    CHOICE = "choice"  # pick one of spec.options -> {"choice": str}
    PERMISSION = "permission"  # allow / deny -> approved bool (+ optional note)
    TASK = "task"  # the owner does something by hand -> {"done": bool, "note": str}


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
    OWNER_MESSAGE = "owner_message"  # the owner talking to a running mission


class Mission(BaseModel):
    id: str
    objective: str
    repo_path: str | None = None  # a path the DAEMON can see (ADR 0005)
    repo_origin: str | None = None  # git URL the daemon clones at run start (ADR 0005)
    project_id: str | None = None  # label grouping; missions may be project-less
    chat_id: str | None = None  # the conversation that launched it (one chat, many missions)
    title: str | None = None  # a few words for lists (the orchestrator proposes it)
    status: MissionStatus = MissionStatus.PENDING
    # pickup order: "high" missions are picked before "normal" ones, oldest
    # first within a level (see StateStore.next_pending_mission)
    priority: str = "normal"
    plan: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    created_at: float = Field(default_factory=now)
    updated_at: float = Field(default_factory=now)


# Pickup priority levels: "high" missions are picked before "normal" ones.
# missions._priority is the source of truth; the stores validate on write and
# coerce on read so a hand-edited row can never break listing.
PRIORITIES = ("normal", "high")


def check_priority(value: str) -> str:
    """Validate a priority a store is about to write (caller bug otherwise)."""
    if value not in PRIORITIES:
        raise ValueError(f"unknown priority {value!r}; priorities: {', '.join(PRIORITIES)}")
    return value


def coerce_priority(value: str | None) -> str:
    """Decode a stored priority: unknown/empty/legacy values read as normal."""
    level = (value or "").strip().lower()
    return level if level in PRIORITIES else "normal"


class Project(BaseModel):
    """A label grouping missions, with per-project settings and zero-or-more
    repo references (ADR 0005). Repos are references, never synced by rework:
    code travels via its VCS; the upload transport is for non-git working files
    only."""

    id: str = Field(default_factory=lambda: new_id("p"))
    name: str
    settings: dict[str, Any] = Field(default_factory=dict)
    created_at: float = Field(default_factory=now)


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
    # A TYPED ANSWER that reaches the role, not just a yes/no. The note above is
    # for humans; this is what the graph receives as the result of its
    # interrupt(). Shape is the gate kind's business — the engine passes it
    # through untouched. Example: {"approved": false, "feedback": "don't touch
    # the parser", "changes": [...]}. None = plain approve/reject (the role gets
    # a bool, exactly as before).
    decision: dict[str, Any] | None = None
    # the run this gate unblocks (escalations only; mission-level gates leave
    # it None). DURABLE on the row, not a follow-up event: recovery needs to
    # answer "which run does this decided gate belong to?" and the event that
    # carried that answer was published AFTER the row was created, so a kill
    # in between stranded the gate forever (found live: chaos test timed out
    # on a 3.13 CI runner).
    for_run: str | None = None
    # how to answer (orchestrator gates): {"options": [...], "placeholder": str,
    # "detail": str}; None for the classic approve/reject gates
    spec: dict[str, Any] | None = None
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
    """One model call's cost: the ledger's atomic entry (accounting exact,
    enforcement approximate: see docs/design.md#kernel)."""

    mission_id: str | None = None
    run_id: str | None = None
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: float = 0.0
    ts: float = Field(default_factory=now)


class ComplaintKind(StrEnum):
    WORKFLOW_GRIPE = "workflow_gripe"  # the workflow itself made this harder
    NEEDED_INFO = "needed_info"  # a role lacked information it needed
    TOOL_MISSING = "tool_missing"  # a tool the role needed does not exist
    PROMPT_ISSUE = "prompt_issue"  # a prompt forced rework or confusion
    BUG_VERIFIED = "bug_verified"  # agent found + verified a bug in rework itself
    PROCESS_ISSUE = "process_issue"  # the division of labor misfired


class ComplaintStatus(StrEnum):
    OPEN = "open"
    TRIAGED = "triaged"
    ISSUE_OPEN = "issue_open"  # clustered into a GitHub issue
    RESOLVED = "resolved"  # fixed; fixing commit recorded
    WONTFIX = "wontfix"


class Complaint(BaseModel):
    """Agent feedback on the workflow itself: typed, evidenced, fire-and-forget
    (ADR 0004). Unlike an escalation, filing one never pauses a mission."""

    id: str = Field(default_factory=lambda: new_id("c"))
    kind: ComplaintKind
    source_mission_id: str | None = None
    source_run_id: str | None = None
    source_role: str | None = None
    summary: str
    detail: str = ""
    evidence: list[str] = Field(default_factory=list)
    suggestion: str = ""
    status: ComplaintStatus = ComplaintStatus.OPEN
    created_at: float = Field(default_factory=now)


class Chat(BaseModel):
    """A conversation with the orchestrator (how missions get created).

    Persisted so a chat can be closed and reopened, and so the Work surface can
    offer a list of conversations instead of one ephemeral in-memory thread.
    A chat is NOT a mission: it becomes one only when the owner confirms a
    proposal, and then `mission_id` records which mission it produced."""

    id: str = Field(default_factory=lambda: new_id("chat"))
    title: str = ""  # derived from the first user message when blank
    project_id: str | None = None  # the project the conversation is scoped to
    model_id: str | None = None  # catalog id the conversation was held on
    effort: str | None = None
    mission_id: str | None = None  # set once a proposal from this chat launched
    created_at: float = Field(default_factory=now)
    updated_at: float = Field(default_factory=now)


class ChatMessage(BaseModel):
    """One row of a chat transcript.

    `body` is the provider-neutral message dict (role, content, tool_calls,
    images, …) stored as JSON so a reloaded chat replays byte-identically into
    the model, including tool calls. `display` is the human-facing text for the
    UI (what the owner typed, what the orchestrator said), which is not always
    the same thing: an assistant turn may carry tool calls and no prose."""

    id: int | None = None  # assigned by the store
    chat_id: str
    role: str  # "user" | "assistant" | "tool"
    body: dict[str, Any] = Field(default_factory=dict)
    display: str = ""
    ts: float = Field(default_factory=now)


class Budget(BaseModel):
    max_usd: float | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None


class BudgetState(BaseModel):
    spent_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0


class BudgetExceeded(Exception):
    def __init__(self, state: BudgetState, budget: Budget) -> None:
        self.state = state
        self.budget = budget
        super().__init__(
            f"budget exceeded: spent ${state.spent_usd:.4f} / ${budget.max_usd}"
            if budget.max_usd is not None
            else "budget exceeded (token cap)"
        )


def bucket_label(bucket: int | float | None, kind: str) -> str:
    """Human label for a spend-history bucket, from the bucket NUMBER.

    Backends compute buckets with different SQL (SQLite divides the epoch
    column, Postgres uses date_trunc), so the label is derived once here from
    the bucket index and always reads in UTC. Every backend therefore returns
    the same labels for the same rows, which is what the conformance suite
    asserts."""
    import datetime as _dt

    if kind == "total":
        return "total"
    if bucket is None:
        return ""
    seconds = {"hour": 3600, "day": 86400, "week": 604800}.get(kind, 86400)
    start = _dt.datetime.fromtimestamp(float(bucket) * seconds, tz=_dt.UTC)
    if kind == "hour":
        return start.strftime("%Y-%m-%d %H:00")
    if kind == "week":
        return f"week of {start.strftime('%Y-%m-%d')}"
    return start.strftime("%Y-%m-%d")
