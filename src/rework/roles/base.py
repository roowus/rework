"""RoleSpec base: how every role in rework is defined.

A role is one module: metadata + knobs as typed fields, and build_graph() assembling
its LangGraph from the node library (docs/adr/0001). Exotic roles override
build_graph entirely; standard roles use roles/assembly.py (M2).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from rework.kernel.budget.ledger import Budget
from rework.kernel.events.bus import EventBus
from rework.kernel.gates import GateManager
from rework.kernel.llm.catalog import ModelCatalog, ModelProvider
from rework.kernel.state.records import Mission, Run
from rework.kernel.state.store import StateStore

if TYPE_CHECKING:
    from langgraph.graph.state import CompiledStateGraph


class EscalationKind(StrEnum):
    BLOCKED = "blocked"
    NEEDS_MODEL = "needs_model"
    NEEDS_TOOL = "needs_tool"
    NEEDS_CONTEXT = "needs_context"
    QUESTION = "question"


class Escalation(BaseModel):
    """The typed envelope roles send to the orchestrator (ADR 0001 decision 4)."""

    kind: EscalationKind
    detail: str
    run_id: str
    payload: dict[str, Any] = Field(default_factory=dict)


class EscalationPolicy(BaseModel):
    kinds: set[EscalationKind] = Field(default_factory=lambda: set(EscalationKind))
    max_escalations: int = 3


class ToolPack(BaseModel):
    """Tools/skills the role may use. Executed by the ToolExecutor the engine hands
    the graph (LocalExecutor now, DockerExecutor at the sandboxing milestone)."""

    allowed: set[str] = Field(default_factory=set)
    mcp_servers: list[str] = Field(default_factory=list)


class RoleBuildCtx(BaseModel):
    """Everything build_graph() may close over. Constructed by the engine at spawn."""

    model_config = {"arbitrary_types_allowed": True}

    run: Run
    mission: Mission
    catalog: ModelCatalog
    provider: ModelProvider
    store: StateStore
    bus: EventBus
    gates: GateManager
    budget: Budget
    workdir: Path  # per-run workspace (path-jailing is the v1 isolation line)

    @property
    def model_ref(self) -> dict[str, Any]:
        return self.run.model_ref


class RoleSpec(ABC):
    """Subclass me. One module per role; the registry auto-discovers them.

    Class-level defaults (tools/escalation/budget) are shared config: subclasses
    replace them wholesale, never mutate them in place.
    """

    name: str = ""
    description: str = ""
    default_model: str = ""  # catalog id
    tools: ToolPack = ToolPack()
    escalation: EscalationPolicy = EscalationPolicy()
    budget: Budget = Budget()

    @abstractmethod
    def build_graph(self, ctx: RoleBuildCtx) -> CompiledStateGraph:
        """Assemble and return the compiled graph. The engine supplies the
        checkpointer at run time; compile bare here."""

    def initial_state(self, ctx: RoleBuildCtx) -> dict[str, Any]:
        return {"objective": ctx.mission.objective}
