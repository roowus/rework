"""RoleSpec base: how every role in rework is defined.

A role is one module: metadata + knobs as typed fields, and build_graph() assembling
its LangGraph from the node library (docs/adr/0001). Exotic roles override
build_graph entirely; standard roles use roles/assembly.py (M2).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

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
    git_author: tuple[str, str] = ("rework", "rework@rework.local")  # commit identity
    # the Library (M5): None = library off (tests, deployments without one);
    # menu is the prebuilt ACTIVE-skills list for the system prompt
    library: Any | None = None
    library_menu: str = ""

    @property
    def model_ref(self) -> dict[str, Any]:
        return self.run.model_ref

    @property
    def read_root(self) -> Path:
        """Where a READ-ONLY role should look. The mission's staged repo when
        there is one, else the empty per-run workspace.

        Found live (mission m-c8b72e7734b7): the investigator was jailed to an
        empty per-run dir, found nothing, then escaped to
        /home/ubuntu/projects/rework and audited a STALE checkout (39ccc9e)
        while reporting it as the mission's code. A read-only role has no
        reason to see an empty box when the real tree is right there."""
        if self.mission.repo_path:
            return Path(self.mission.repo_path)
        return self.workdir


class RoleSpec(ABC):
    """Subclass me. One module per role; the registry auto-discovers them.

    Class-level defaults (tools/escalation/budget) are shared config: subclasses
    replace them wholesale, never mutate them in place.
    """

    name: str = ""
    description: str = ""
    goal: str = ""  # the line that anchors every prompt for this role (ADR 0003)
    default_model: str = ""  # catalog id
    tools: ToolPack = ToolPack()
    escalation: EscalationPolicy = EscalationPolicy()
    budget: Budget = Budget()
    # what this role can consume in its task context; the orchestrator picks
    # per mission which keys to actually hand over (ADR 0003 decision 5)
    context_manifest: ClassVar[set[str]] = set()

    @abstractmethod
    def build_graph(self, ctx: RoleBuildCtx) -> CompiledStateGraph:
        """Assemble and return the compiled graph. The engine supplies the
        checkpointer at run time; compile bare here."""

    def resolve_model(self, ctx: RoleBuildCtx) -> str:
        """The catalog id this run actually uses: the plan/brief's explicit
        override first, then the role's own default, then the CONFIGURED
        default (REWORK_DEFAULT_MODEL). One resolution path, so a role whose
        plan names no model gets the configured default and switching it is
        env-only."""
        from rework.config import default_model_id

        explicit = ctx.run.model_ref.get("id")
        return str(explicit or self.default_model or default_model_id(ctx.catalog))

    def initial_state(self, ctx: RoleBuildCtx) -> dict[str, Any]:
        return {"objective": ctx.mission.objective}
