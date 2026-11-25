"""RoleSpec base: how every role in rework is defined.

A role is one module: metadata + knobs as typed fields, and build_graph() assembling
its LangGraph from the node library (docs/adr/0001). Exotic roles override
build_graph entirely; standard roles use roles/assembly.py (M2).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, nullcontext
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


# Escalation and tool exposure are not declared here. Both used to be
# (`EscalationPolicy`, `ToolPack`) and nothing read either: exposure is decided by
# the executor WRAPPER a role builds (the escalate/read-only/library/web
# wrappers), and an escalation's kind travels on the run row. Two mechanisms for
# one job is how the declarative one drifted into a lie (ADR 0017).


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
    # Given back while a run is only WAITING (a parked escalation), and taken
    # again before it resumes: the role slot and the daemon mission slot, in
    # that order. None = no limiter (tests, evals), so parking costs nothing and
    # the context manager is a no-op.
    park: Callable[[], AbstractAsyncContextManager[None]] | None = None
    # How this run builds its ToolExecutor. One place decides local vs
    # container (ADR 0014); None = LocalExecutor, which is what tests and evals
    # get without saying anything.
    executor_factory: Callable[[Path], Any] | None = None
    # Teardown for things the run owns outside the graph (a container, a temp
    # dir). The SUPERVISOR calls these when the run ends, on every path
    # including cancellation; nothing else owns "end of run" (ADR 0014).
    closers: list[Callable[[], Any]] = Field(default_factory=list)

    def make_executor(self, workdir: Path) -> Any:
        """Build a run's executor through the injected factory, defaulting to
        the local jail. Imported here so roles/base stays free of node imports
        at module time."""
        if self.executor_factory is None:
            from rework.nodes.tools import LocalExecutor

            return LocalExecutor(workdir, mission_id=self.mission.id)
        return self.executor_factory(workdir)

    async def aclose(self) -> None:
        """Run every closer, ignoring failures: a broken teardown must never
        turn a finished run into a failed one."""
        closers, self.closers = self.closers, []
        for close in closers:
            try:
                result = close()
                if hasattr(result, "__await__"):
                    await result
            except Exception:
                pass

    def parked(self) -> AbstractAsyncContextManager[None]:
        """The context a role waits inside. Always usable: a ctx with no park
        hook (tests, evals) yields immediately."""
        if self.park is None:
            return nullcontext()
        return self.park()

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
