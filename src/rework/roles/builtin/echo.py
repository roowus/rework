"""Echo: the M1 stub role.

Proves the pause machinery with zero LLM calls: emit progress, ask the owner once
through a gate (the one pause primitive every role uses, ADR 0023), consume the
decision, complete with an artifact.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from rework.kernel.state.records import EventRecord, EventType, GateKind
from rework.nodes.escalate import ask_owner_gate
from rework.roles.base import RoleBuildCtx, RoleSpec

if TYPE_CHECKING:
    from langgraph.graph.state import CompiledStateGraph


class EchoState(TypedDict, total=False):
    objective: str
    decision: Any
    artifact: dict[str, Any] | None


class EchoSpec(RoleSpec):
    name = "echo"
    description = (
        "Stub role for M1: emits progress, pauses at one approval gate, "
        "returns the objective as its artifact. No model calls."
    )
    default_model = "fake"

    def build_graph(self, ctx: RoleBuildCtx) -> CompiledStateGraph:
        async def speak(state: EchoState) -> dict[str, Any]:
            await ctx.bus.publish(
                EventRecord(
                    mission_id=ctx.mission.id,
                    run_id=ctx.run.id,
                    type=EventType.PROGRESS,
                    payload={"note": f"echo received: {state['objective']}"},
                )
            )
            return {}

        async def gate(state: EchoState) -> dict[str, Any]:
            # The ONE pause primitive (ADR 0023): ask on the run row. On a
            # legacy mission that is a QUESTION-free ship gate for the owner;
            # on an orchestrated one, the orchestrator answers. The role blocks
            # in this call, gives its slots back while it waits, and a restart
            # re-asks with the same deterministic id, so an answer written while
            # the daemon was down is consumed, never re-asked.
            question = f"Echo says: '{state['objective']}'. Approve completion?"
            answer = await ask_owner_gate(ctx, question, kind=GateKind.SHIP)
            return {"decision": answer}

        async def finish(state: EchoState) -> dict[str, Any]:
            await ctx.bus.publish(
                EventRecord(
                    mission_id=ctx.mission.id,
                    run_id=ctx.run.id,
                    type=EventType.INFO,
                    payload={"note": f"echo finishing with decision {state['decision']}"},
                )
            )
            return {
                "artifact": {
                    "kind": "echo",
                    "objective": state["objective"],
                    "approved": _approved(state["decision"]),
                }
            }

        graph = StateGraph(EchoState)
        graph.add_node("speak", speak)
        graph.add_node("gate", gate)
        graph.add_node("finish", finish)
        graph.add_edge(START, "speak")
        graph.add_edge("speak", "gate")
        graph.add_edge("gate", "finish")
        graph.add_edge("finish", END)
        return graph.compile()


def _approved(decision: Any) -> bool:
    """The owner gate returns a bool, or a typed answer dict carrying `approved`."""
    if isinstance(decision, dict):
        return bool(decision.get("approved"))
    return bool(decision)
