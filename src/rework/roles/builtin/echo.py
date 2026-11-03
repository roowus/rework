"""Echo: the M1 stub role.

Proves the whole pause/resume machinery with zero LLM calls: emit progress,
interrupt once at a gate (the engine materializes the gate row from the interrupt
payload, ADR 0002), consume the decision, complete with an artifact. The vertical
slice (M2) replaces this with real roles.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from rework.kernel.state.records import EventRecord, EventType
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
            # interrupt FIRST (ADR 0002): no side effects before this line. The
            # engine materializes the gate row from this payload; on resume this
            # returns the decision instead of raising.
            decision = interrupt(
                {
                    "kind": "ship",
                    "question": f"Echo says: '{state['objective']}'. Approve completion?",
                }
            )
            return {"decision": decision}

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
                    "approved": bool(state["decision"]),
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
