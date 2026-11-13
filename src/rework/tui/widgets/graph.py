"""GraphView: a mission drawn as its LangGraph-style flow, top to bottom.

Each node is a step (or a gate / the end); its border carries the status
colour, the active node is accent. Clicking a node selects that step's run.
Inside a node, when the role published its own graph topology (ROLE_SPAWNED
payload["graph"], M7 P3) the role's inner nodes are
drawn as a strip with the current one highlighted."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from textual import events
from textual.containers import Horizontal, VerticalScroll
from textual.message import Message
from textual.widgets import Static

from rework.tui.theme import ACCENT, FAINT, MUTED, esc, glyph, status_value


@dataclass
class GNode:
    key: str  # run id for orchestrated per-run nodes, step name otherwise
    title: str
    status: str = "pending"
    sub: str = ""
    run_id: str | None = None
    loop_note: str = ""
    inner: list[str] = field(default_factory=list)  # role graph nodes
    inner_at: str = ""  # current inner node
    findings: str = ""  # audit findings summary: "2 high · 4 medium"
    strictness: str = ""  # audit strictness level


@dataclass
class GEdge:
    """An edge between nodes or layers."""

    label: str = ""  # optional label, e.g. "changes requested: 2 high · 1 medium"


@dataclass
class GLayer:
    """A layer of one or more nodes (parallel runs go in the same layer)."""

    nodes: list[GNode] = field(default_factory=list)
    edge_to_next: GEdge | None = None


class _Node(Static):
    def __init__(self, node: GNode) -> None:
        super().__init__(classes="gnode")
        self.node = node

    def paint(self, node: GNode, selected: bool) -> None:
        self.node = node
        lines = [f"{glyph(node.status)} [b]{esc(node.title)}[/b]"]
        if node.sub:
            lines.append(f"[{MUTED}]{esc(node.sub)}[/]")
        if node.findings:
            lines.append(f"[{MUTED}]{esc(node.findings)}[/]")
        if node.strictness:
            lines.append(f"[{FAINT}]strictness: {esc(node.strictness)}[/]")
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
            widgets: list[Static | Horizontal] = []
            for i, node in enumerate(nodes):
                if i:
                    widgets.append(Static("│\n▼", classes="gedge"))
                widgets.append(_Node(node))
            self.mount_all(widgets)
            self._keys = keys
        for widget in self.query(_Node):
            fresh = next(n for n in nodes if n.key == widget.node.key)
            widget.paint(fresh, fresh.run_id is not None and fresh.run_id == selected)

    def set_layers(self, layers: list[GLayer], selected: str | None = None) -> None:
        """Set nodes organized into layers (for parallel runs and labeled edges)."""
        all_nodes = [n for layer in layers for n in layer.nodes]
        keys = [n.key for n in all_nodes]

        if keys != self._keys:
            self.remove_children()
            widgets: list[Static | Horizontal] = []
            # Track which Horizontal widget corresponds to which layer
            horiz_to_layer: dict[Horizontal, GLayer] = {}

            for i, layer in enumerate(layers):
                if i > 0:
                    # Add edge from previous layer
                    prev_edge = layers[i - 1].edge_to_next
                    if prev_edge and prev_edge.label:
                        # Labeled edge
                        widgets.append(
                            Static(f"│\n▼\n[{FAINT}]{esc(prev_edge.label)}[/]", classes="gedge")
                        )
                    else:
                        # Standard edge
                        widgets.append(Static("│\n▼", classes="gedge"))

                # Add nodes for this layer
                if len(layer.nodes) == 1:
                    # Single node - just add it directly
                    widgets.append(_Node(layer.nodes[0]))
                else:
                    # Multiple nodes - horizontal layout
                    horiz = Horizontal(classes="glayer")
                    horiz_to_layer[horiz] = layer
                    widgets.append(horiz)

            self.mount_all(widgets)

            # Now mount nodes inside horizontal containers using the tracked mapping
            for horiz, layer in horiz_to_layer.items():
                horiz.mount_all([_Node(n) for n in layer.nodes])

            self._keys = keys

        # Update all nodes
        for widget in self.query(_Node):
            fresh = next((n for n in all_nodes if n.key == widget.node.key), None)
            if fresh:
                widget.paint(fresh, fresh.run_id is not None and fresh.run_id == selected)


def _findings_summary(run: Any) -> str:
    """Format audit findings as '2 high · 4 medium'."""
    findings = (run.artifact or {}).get("findings", [])
    if not findings:
        return ""
    counts: dict[str, int] = {}
    for f in findings:
        sev = f.get("severity", "")
        if sev:
            counts[sev] = counts.get(sev, 0) + 1
    # Order by severity: critical, high, medium, low
    parts = []
    for sev in ["critical", "high", "medium", "low"]:
        if sev in counts:
            parts.append(f"{counts[sev]} {sev}")
    return " · ".join(parts)


def mission_nodes(
    mission: Any,
    runs: list[Any],
    gates: list[Any],
    run_meta: dict[str, str],
    inner: dict[str, tuple[list[str], str]],
    compact: bool = False,
) -> list[GNode]:
    """Plan steps + gates + end, each with the status of its latest run.

    For orchestrated missions and compact=False, returns one node per run.
    For fixed-plan missions or compact=True, returns step-grouped nodes.

    run_meta: run_id -> one-line subtitle (model · duration · cost)
    inner:    run_id -> (role graph nodes, current node)
    compact:  True for the flow strip (step-grouped), False for the graph view"""
    plan = mission.plan or {}
    is_orchestrated = bool(plan.get("orchestrator") or {})

    # For compact mode (flow strip) or fixed-plan missions, use step-grouped view
    if compact or not is_orchestrated:
        return _mission_nodes_step_grouped(mission, runs, gates, run_meta, inner)

    # Orchestrated mission: one node per run, chronologically
    return _mission_nodes_per_run(mission, runs, gates, run_meta, inner)


def _mission_nodes_step_grouped(
    mission: Any,
    runs: list[Any],
    gates: list[Any],
    run_meta: dict[str, str],
    inner: dict[str, tuple[list[str], str]],
) -> list[GNode]:
    """Original step-grouped view: one node per step name (latest attempt)."""
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
    orch = plan.get("orchestrator") or {}
    if orch:
        live = status_value(mission.status) in {"pending", "running", "awaiting_gate", "on_hold"}
        st = "running" if live else status_value(mission.status)
        nodes.append(
            GNode(
                key="orchestrator",
                title="orchestrator",
                status={"done": "completed"}.get(st, st),
                sub=f"{orch.get('model') or 'default'} · playbook {plan.get('playbook', '?')}",
                run_id=f"{mission.id}:orchestrate:orchestrator:1",
            )
        )
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


def _mission_nodes_per_run(
    mission: Any,
    runs: list[Any],
    gates: list[Any],
    run_meta: dict[str, str],
    inner: dict[str, tuple[list[str], str]],
) -> list[GNode]:
    """Orchestrated mission view: one node per run, chronologically.

    This is the simple linear view - use mission_layers for the full flow graph."""
    plan = mission.plan or {}
    nodes: list[GNode] = []

    # Add orchestrator node
    orch = plan.get("orchestrator") or {}
    live = status_value(mission.status) in {"pending", "running", "awaiting_gate", "on_hold"}
    st = "running" if live else status_value(mission.status)
    nodes.append(
        GNode(
            key="orchestrator",
            title="orchestrator",
            status={"done": "completed"}.get(st, st),
            sub=f"{orch.get('model') or 'default'} · playbook {plan.get('playbook', '?')}",
            run_id=f"{mission.id}:orchestrate:orchestrator:1",
        )
    )

    # Add one node per run, in chronological order (runs are already sorted by created_at)
    coder_runs: list[Any] = []
    for run in runs:
        status = status_value(run.status)
        role = run.role
        title = f"{role}"

        # Check if this is a revision (coder following an audit)
        if role == "coder" and coder_runs:
            title = f"{role} (revision)"

        if role == "coder":
            coder_runs.append(run)

        sub = run_meta.get(run.id, role)
        if run.attempt > 1:
            sub = f"{sub} · attempt {run.attempt}"

        inner_nodes, inner_at = inner.get(run.id, ([], ""))

        # Extract audit findings and strictness
        findings = ""
        strictness = ""
        if role == "auditor":
            findings = _findings_summary(run)
            strictness = (run.context or {}).get("strictness", "")

        nodes.append(
            GNode(
                key=run.id,  # Use run ID as key for per-run nodes
                title=title,
                status=status,
                sub=sub,
                run_id=run.id,
                inner=inner_nodes,
                inner_at=inner_at if status == "running" else "",
                findings=findings,
                strictness=strictness,
            )
        )

    # Add gates
    for kind in plan.get("gates") or []:
        matching = [g for g in gates if g.kind.value == kind]
        status = status_value(matching[-1].status) if matching else "pending"
        nodes.append(
            GNode(key=f"gate:{kind}", title=f"{kind} gate", status=status, sub="you decide")
        )

    # Add end node
    end_status = {
        "done": "done",
        "failed": "failed",
        "cancelled": "cancelled",
    }.get(status_value(mission.status), "pending")
    nodes.append(GNode(key="end", title="end", status=end_status, sub=""))

    return nodes


def mission_layers(
    mission: Any,
    runs: list[Any],
    gates: list[Any],
    run_meta: dict[str, str],
    inner: dict[str, tuple[list[str], str]],
    events: list[Any],
) -> list[GLayer]:
    """Build layered graph structure for orchestrated missions with parallel runs and labeled edges.

    Returns a list of layers where each layer contains one or more nodes (parallel runs).
    Edges between layers can carry labels (e.g. for revision loops).
    """
    plan = mission.plan or {}
    is_orchestrated = bool(plan.get("orchestrator") or {})

    if not is_orchestrated:
        # For non-orchestrated missions, just convert nodes to single-node layers
        nodes = _mission_nodes_step_grouped(mission, runs, gates, run_meta, inner)
        return [GLayer(nodes=[n], edge_to_next=GEdge()) for n in nodes]

    # Build layers for orchestrated mission
    layers: list[GLayer] = []

    # Orchestrator layer
    orch = plan.get("orchestrator") or {}
    live = status_value(mission.status) in {"pending", "running", "awaiting_gate", "on_hold"}
    st = "running" if live else status_value(mission.status)
    orch_node = GNode(
        key="orchestrator",
        title="orchestrator",
        status={"done": "completed"}.get(st, st),
        sub=f"{orch.get('model') or 'default'} · playbook {plan.get('playbook', '?')}",
        run_id=f"{mission.id}:orchestrate:orchestrator:1",
    )
    layers.append(GLayer(nodes=[orch_node], edge_to_next=GEdge()))

    # Group runs by spawn events to detect parallel execution
    run_groups = _group_parallel_runs(runs, events)

    # Track revision loops: when an audit's findings trigger another coder run
    prev_audit_run: Any = None

    # Collapse >4 rounds if needed (count total rounds, not run groups)
    total_runs = sum(len(group) for group in run_groups)
    if total_runs > 8:  # More than 6 runs (3 rounds of coder+audit pairs)
        # Collapse early rounds, keep last 3 round groups
        collapsed_count = len(run_groups) - 3
        collapsed_runs = [r for group in run_groups[:collapsed_count] for r in group]

        # Create a summary node
        summary_node = GNode(
            key="rounds-collapsed",
            title=f"rounds 1-{collapsed_count} (collapsed)",
            status="completed",  # Assume earlier rounds completed
            sub=f"{len(collapsed_runs)} runs",
            run_id=None,
        )
        layers.append(GLayer(nodes=[summary_node], edge_to_next=GEdge()))

        # Keep only last 3 round groups
        run_groups = run_groups[-3:]

    # Process each group of runs
    for group_idx, group in enumerate(run_groups):
        layer_nodes: list[GNode] = []

        for run in group:
            status = status_value(run.status)
            role = run.role
            title = f"{role}"

            # Check if this is a revision (coder following an audit)
            if role == "coder" and prev_audit_run and prev_audit_run.role == "auditor":
                title = f"{role} (revision)"

            sub = run_meta.get(run.id, role)
            if run.attempt > 1:
                sub = f"{sub} · attempt {run.attempt}"

            inner_nodes, inner_at = inner.get(run.id, ([], ""))

            # Extract audit findings and strictness
            findings = ""
            strictness = ""
            if role == "auditor":
                findings = _findings_summary(run)
                strictness = (run.context or {}).get("strictness", "")

            layer_nodes.append(
                GNode(
                    key=run.id,
                    title=title,
                    status=status,
                    sub=sub,
                    run_id=run.id,
                    inner=inner_nodes,
                    inner_at=inner_at if status == "running" else "",
                    findings=findings,
                    strictness=strictness,
                )
            )

            # Track audit runs for detecting revision loops
            if role == "auditor":
                prev_audit_run = run

        # Determine edge label for the NEXT edge (from this layer to the next)
        edge_label = ""
        if prev_audit_run and prev_audit_run.role == "auditor":
            # Check if there's a next group and if it starts with a coder (revision loop)
            if group_idx < len(run_groups) - 1:
                next_group = run_groups[group_idx + 1]
                if next_group and next_group[0].role == "coder":
                    # This is a revision loop - label the edge
                    findings = _findings_summary(prev_audit_run)
                    if findings:
                        edge_label = f"changes requested: {findings}"

        layers.append(GLayer(nodes=layer_nodes, edge_to_next=GEdge(label=edge_label)))

    # Add gates
    for kind in plan.get("gates") or []:
        matching = [g for g in gates if g.kind.value == kind]
        status = status_value(matching[-1].status) if matching else "pending"
        gate_node = GNode(key=f"gate:{kind}", title=f"{kind} gate", status=status, sub="you decide")
        layers.append(GLayer(nodes=[gate_node], edge_to_next=GEdge()))

    # Add end node
    end_status = {
        "done": "done",
        "failed": "failed",
        "cancelled": "cancelled",
    }.get(status_value(mission.status), "pending")
    end_node = GNode(key="end", title="end", status=end_status, sub="")
    layers.append(GLayer(nodes=[end_node], edge_to_next=None))

    return layers


def _group_parallel_runs(runs: list[Any], events: list[Any]) -> list[list[Any]]:
    """Group runs by when they were spawned to detect parallel execution.

    Returns a list of groups where each group contains runs spawned together.
    """
    if not runs:
        return []

    # Build a map of run_id -> event sequence when it was spawned
    spawn_seq: dict[str, int] = {}
    for ev in events:
        if str(getattr(ev.type, "value", ev.type)) == "role_spawned" and ev.run_id:
            spawn_seq[ev.run_id] = ev.seq or 0

    # If we have spawn events, use them to group runs
    if spawn_seq:
        # Sort runs by their spawn sequence
        runs_with_seq = [(run, spawn_seq.get(run.id, run.created_at)) for run in runs]
        runs_with_seq.sort(key=lambda x: x[1])

        # Group consecutive runs spawned close together (within 5 sequence numbers = same batch)
        groups: list[list[Any]] = []
        current_group: list[Any] = []
        prev_seq = -1000

        for run, seq in runs_with_seq:
            if not current_group or (seq - prev_seq) <= 5:
                # Same group
                current_group.append(run)
                prev_seq = seq
            else:
                # New group
                if current_group:
                    groups.append(current_group)
                current_group = [run]
                prev_seq = seq

        if current_group:
            groups.append(current_group)

        return groups

    # Fallback: group by overlapping timestamps
    # For simplicity, assume runs are in chronological order and group consecutive runs
    # that have overlapping time windows
    fallback_groups: list[list[Any]] = []
    fallback_current_group: list[Any] = []
    fallback_current_end = 0.0

    for run in runs:
        start = run.created_at
        end = run.updated_at if hasattr(run, "updated_at") else start + 1.0

        if not fallback_current_group or start < fallback_current_end:
            # Overlapping or first run - add to current group
            fallback_current_group.append(run)
            fallback_current_end = max(fallback_current_end, end)
        else:
            # No overlap - start new group
            if fallback_current_group:
                fallback_groups.append(fallback_current_group)
            fallback_current_group = [run]
            fallback_current_end = end

    if fallback_current_group:
        fallback_groups.append(fallback_current_group)

    return fallback_groups
