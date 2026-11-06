"""GraphView: a mission drawn as its LangGraph-style flow, top to bottom.

Each node is a step (or a gate / the end); its border carries the status
colour, the active node is accent. Clicking a node selects that step's run.
Inside a node, when the role published its own graph topology (ROLE_SPAWNED
payload["graph"], docs/tui/BACKEND-NEEDS.md §6) the role's inner nodes are
drawn as a strip with the current one highlighted."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from textual import events
from textual.containers import VerticalScroll
from textual.message import Message
from textual.widgets import Static

from rework.tui.theme import ACCENT, FAINT, MUTED, esc, glyph, status_value


@dataclass
class GNode:
    key: str  # step name, "gate:<kind>" or "end"
    title: str
    status: str = "pending"
    sub: str = ""
    run_id: str | None = None
    loop_note: str = ""
    inner: list[str] = field(default_factory=list)  # role graph nodes
    inner_at: str = ""  # current inner node


class _Node(Static):
    def __init__(self, node: GNode) -> None:
        super().__init__(classes="gnode")
        self.node = node

    def paint(self, node: GNode, selected: bool) -> None:
        self.node = node
        lines = [f"{glyph(node.status)} [b]{esc(node.title)}[/b]"]
        if node.sub:
            lines.append(f"[{MUTED}]{esc(node.sub)}[/]")
        if node.inner:
            strip = []
            for name in node.inner:
                if name == node.inner_at:
                    strip.append(f"[{ACCENT}][b]▶ {esc(name)}[/b][/]")
                else:
                    strip.append(f"[{FAINT}]{esc(name)}[/]")
            lines.append(f" [{FAINT}]─[/] ".join(strip))
        if node.loop_note:
            lines.append(f"[{FAINT}]↺ {esc(node.loop_note)}[/]")
        self.update("\n".join(lines))
        for cls in list(self.classes):
            if cls.startswith("-s-"):
                self.remove_class(cls)
        self.add_class(f"-s-{node.status}")
        self.set_class(selected, "-selected")

    def on_click(self, event: events.Click) -> None:
        event.stop()
        view = self.parent
        while view is not None and not isinstance(view, GraphView):
            view = view.parent
        if isinstance(view, GraphView):
            view.post_message(GraphView.NodeClicked(view, self.node))


class GraphView(VerticalScroll):
    class NodeClicked(Message):
        def __init__(self, view: GraphView, node: GNode) -> None:
            super().__init__()
            self.view = view
            self.node = node

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id, classes="graphview")
        self._keys: list[str] = []

    def set_nodes(self, nodes: list[GNode], selected: str | None = None) -> None:
        keys = [n.key for n in nodes]
        if keys != self._keys:
            self.remove_children()
            widgets: list[Static] = []
            for i, node in enumerate(nodes):
                if i:
                    widgets.append(Static("│\n▼", classes="gedge"))
                widgets.append(_Node(node))
            self.mount_all(widgets)
            self._keys = keys
        for widget in self.query(_Node):
            fresh = next(n for n in nodes if n.key == widget.node.key)
            widget.paint(fresh, fresh.run_id is not None and fresh.run_id == selected)


def mission_nodes(
    mission: Any,
    runs: list[Any],
    gates: list[Any],
    run_meta: dict[str, str],
    inner: dict[str, tuple[list[str], str]],
) -> list[GNode]:
    """Plan steps + gates + end, each with the status of its latest run.

    run_meta: run_id -> one-line subtitle (model · duration · cost)
    inner:    run_id -> (role graph nodes, current node)"""
    plan = mission.plan or {}
    steps = list(plan.get("steps") or [])
    by_step: dict[str, list[Any]] = {}
    for run in runs:
        by_step.setdefault(run.step, []).append(run)
    # runs for steps the plan does not list (echo plans, ad-hoc spawns)
    for step_name in by_step:
        if not any((s.get("step") or s.get("role")) == step_name for s in steps):
            steps.append({"step": step_name, "role": by_step[step_name][0].role})
    nodes: list[GNode] = []
    for step in steps:
        name = str(step.get("step") or step.get("role"))
        attempts = sorted(by_step.get(name, []), key=lambda r: r.attempt)
        latest = attempts[-1] if attempts else None
        status = status_value(latest.status) if latest else "pending"
        role = str(step.get("role") or name)
        sub = role
        if latest is not None:
            sub = run_meta.get(latest.id, role)
            if len(attempts) > 1:
                sub = f"{sub} · attempt {latest.attempt}"
        loop = ""
        if isinstance(step.get("on_request_changes"), dict):
            rc = step["on_request_changes"]
            loop = f"changes requested → back to {rc.get('revise')} (≤{rc.get('max_rounds', '?')})"
        inner_nodes, inner_at = inner.get(latest.id, ([], "")) if latest else ([], "")
        nodes.append(
            GNode(
                key=name,
                title=name,
                status=status,
                sub=sub,
                run_id=latest.id if latest else None,
                loop_note=loop,
                inner=inner_nodes,
                inner_at=inner_at if status == "running" else "",
            )
        )
    for kind in plan.get("gates") or []:
        matching = [g for g in gates if g.kind.value == kind]
        status = status_value(matching[-1].status) if matching else "pending"
        nodes.append(
            GNode(key=f"gate:{kind}", title=f"{kind} gate", status=status, sub="you decide")
        )
    end_status = {
        "done": "done",
        "failed": "failed",
        "cancelled": "cancelled",
    }.get(status_value(mission.status), "pending")
    nodes.append(GNode(key="end", title="end", status=end_status, sub=""))
    return nodes
