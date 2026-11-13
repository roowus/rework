"""GraphView: a mission drawn as its LangGraph-style flow, top to bottom.

Each node is a step (or a gate / the end); its border carries the status
colour, the active node is accent. Clicking a node selects that step's run.
Inside a node, when the role published its own graph topology (ROLE_SPAWNED
payload["graph"], M7 P3) the role's inner nodes are
drawn as a strip with the current one highlighted.

Orchestrated missions are drawn as ordered review ROUNDS: a round is every
coder run between two audits, plus the audit that reviewed them. A coder that
continues the previous coder (context["continue_branch"], e.g. after the
previous one exhausted its turns) belongs to the SAME round; a revision starts
a new one. Parallel spawns stay side by side inside their round. When a
mission has many rounds the older ones fold into one summary node that
expands on click (and folds again), keeping the latest two rounds expanded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from textual import events
from textual.containers import Horizontal, VerticalScroll
from textual.message import Message
from textual.widgets import Static

from rework.nodes.report_sections import mechanical_verdict
from rework.tui.theme import ACCENT, FAINT, MUTED, esc, glyph, status_value
from rework.tui.widgets.trajectory import role_of

# Collapse policy (bug (a)-(c): grouping was a 2s spawn window and the
# walk-back split mid-round; rounds are now first-class, so the collapse is
# "fold every round before the last COLLAPSE_KEEP_ROUNDS, only when the
# mission is long enough to need it").
COLLAPSE_ROUND_THRESHOLD = 5  # only fold when there are more rounds than this
COLLAPSE_KEEP_ROUNDS = 2  # the latest rounds stay expanded (the loop under review)
PARALLEL_SPAWN_WINDOW_SECONDS = 2.0  # spawns this close are a parallel fan-out


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
    expandable: bool = False  # a collapsed-round summary: click toggles it
    expanded: bool = False


@dataclass
class GEdge:
    """An edge between nodes or layers."""

    label: str = ""  # optional label, e.g. "changes requested: 2 high · 1 medium"


@dataclass
class GLayer:
    """A layer of one or more nodes (parallel runs go in the same layer)."""

    nodes: list[GNode] = field(default_factory=list)
    edge_to_next: GEdge | None = None


@dataclass
class ReviewRound:
    """One review round: the coder runs it is made of, then the audit(s) that
    reviewed them. Runs before the first coder (the investigator, the
    orchestrator's own spawns) form a round with no audit."""

    coders: list[Any] = field(default_factory=list)
    audits: list[Any] = field(default_factory=list)
    others: list[Any] = field(default_factory=list)  # e.g. the investigator

    def runs(self) -> list[Any]:
        return self.others + self.coders + self.audits

    @property
    def is_review(self) -> bool:
        """A round counts as a review round once an audit reviewed its coders."""
        return bool(self.audits)


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
        if node.expandable:
            hint = "collapse rounds" if node.expanded else "expand rounds"
            lines.append(f"[{ACCENT}]{hint}[/]")
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
        self.expanded_rounds = False  # the collapsed-round summary starts folded

    def toggle_collapsed_rounds(self) -> bool:
        """Expand / fold the collapsed-round summary node. Returns the new state."""
        self.expanded_rounds = not self.expanded_rounds
        return self.expanded_rounds

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
    prev_audit_run: Any = None
    for run in runs:
        status = status_value(run.status)
        title = role_of(run.id) or str(run.role)
        if run.role == "coder" and prev_audit_run is not None:
            title = f"{title} (revision)"
        if run.role == "auditor":
            prev_audit_run = run
        sub = run_meta.get(run.id, str(run.role))
        inner_nodes, inner_at = inner.get(run.id, ([], ""))
        findings = _findings_summary(run) if run.role == "auditor" else ""
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
                strictness=str((run.context or {}).get("strictness", ""))
                if run.role == "auditor"
                else "",
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


# -- review rounds ---------------------------------------------------------------------------


def _continues_previous(run: Any, prev: Any) -> bool:
    """True when `run` continues `prev`'s work (the same round, more legs):
    a fresh coder after one that exhausted its turns carries the previous
    branch in its context and is spawned before any audit ran."""
    if run.role != "coder" or prev is None or prev.role != "coder":
        return False
    return bool((run.context or {}).get("continue_branch"))


def _review_rounds(runs: list[Any]) -> list[ReviewRound]:
    """Split a mission's runs into ordered review rounds.

    A round is every coder run between two audits plus the audit that reviewed
    them; a coder continuing the previous coder (context["continue_branch"],
    e.g. spawned after it exhausted its turns) belongs to the SAME round, so
    'coders 5-6' is one round. Auditors fanned out in parallel all review the
    same round. Any role before the first coder (the investigator) is its own
    non-review round.
    """
    ordered = sorted(runs, key=lambda r: (r.created_at, r.id))
    rounds: list[ReviewRound] = []
    current = ReviewRound()

    def close_round() -> None:
        nonlocal current
        if current.others or current.coders or current.audits:
            rounds.append(current)
        current = ReviewRound()

    prev_coder: Any = None
    for run in ordered:
        role = str(run.role)
        if role == "coder":
            if current.audits and not _continues_previous(run, prev_coder):
                # the previous round was already reviewed: this is a revision
                close_round()
            current.coders.append(run)
            prev_coder = run
        elif role == "auditor":
            current.audits.append(run)  # reviews the round's coders
        else:
            # the investigator and other pre-code roles: one round each
            close_round()
            current.others.append(run)
            close_round()
    close_round()
    return rounds


def _review_edge_label(audits: list[Any]) -> str:
    """The review outcome of a round, from its audit findings/verdicts:
    'changes requested: 2 high', 'approve', ..."""
    if not audits:
        return ""
    verdicts = {mechanical_verdict(a.artifact) for a in audits}
    if len(verdicts) > 1:
        return " / ".join(sorted(verdicts))
    verdict = verdicts.pop()
    if verdict in ("request changes", "approve with fixes"):
        parts = []
        for sev in ("critical", "high", "medium", "low"):
            n = sum(
                1
                for a in audits
                for f in (a.artifact or {}).get("findings") or []
                if isinstance(f, dict) and str(f.get("severity", "")).lower() == sev
            )
            if n:
                parts.append(f"{n} {sev}")
        return f"{verdict}: " + " · ".join(parts) if parts else verdict
    return verdict


def _derive_collapsed_status(runs: list[Any]) -> str:
    """Derive a summary status for collapsed runs."""
    if not runs:
        return "completed"
    statuses = {status_value(r.status) for r in runs}
    if "failed" in statuses:
        return "failed"
    if "cancelled" in statuses:
        return "cancelled"
    if statuses - {"completed", "running", "pending", "intent"}:
        return "mixed"
    return "completed"


def _node_for(run: Any, run_meta: dict[str, str], inner: dict[str, tuple[list[str], str]]) -> GNode:
    """One run, one node - titled from its run id ('coder #2'), never 'coder 4'."""
    st = status_value(run.status)
    inner_nodes, inner_at = inner.get(run.id, ([], ""))
    return GNode(
        key=run.id,
        title=role_of(run.id) or str(run.role),
        status=st,
        sub=run_meta.get(run.id, str(run.role)),
        run_id=run.id,
        inner=inner_nodes,
        inner_at=inner_at if st == "running" else "",
        findings=_findings_summary(run) if run.role == "auditor" else "",
        strictness=str((run.context or {}).get("strictness", "")) if run.role == "auditor" else "",
    )


def _round_layers(
    round_: ReviewRound,
    index: int,
    run_meta: dict[str, str],
    inner: dict[str, tuple[list[str], str]],
    is_revision: bool,
) -> list[GLayer]:
    """The layers of one round: its leading roles, its coders, then its audits.

    Every run stays its own node (clicking it selects that run), titled from
    its run id: 'coder #5', never 'coder 4'. A coder that continues the
    previous coder's branch keeps the round's label ('coders 5-6') in its
    subtitle; a coder opening a round after an audit is a revision.
    """
    layers: list[GLayer] = []
    if round_.others:
        layers.append(GLayer(nodes=[_node_for(r, run_meta, inner) for r in round_.others]))
    coder_nodes: list[GNode] = []
    chain: list[Any] = []  # the continuation chain inside this round, if any
    for pos, run in enumerate(round_.coders):
        node = _node_for(run, run_meta, inner)
        title = role_of(run.id) or str(run.role)
        sub = node.sub
        if is_revision and pos == 0:
            title = f"{title} (revision)"
        if pos > 0 and _continues_previous(run, round_.coders[pos - 1]):
            chain.append(run)
        else:
            chain = [run]
        if len(chain) > 1:
            lo = _run_ordinal(chain[0])
            hi = _run_ordinal(run)
            sub = f"coders {lo}-{hi} · continues {role_of(chain[0].id) or chain[0].role}"
        node.title = title
        node.sub = sub
        coder_nodes.append(node)
    if coder_nodes:
        layers.append(GLayer(nodes=coder_nodes))
    audit_nodes = [_node_for(r, run_meta, inner) for r in round_.audits]
    if audit_nodes:
        layers.append(GLayer(nodes=audit_nodes))
    return layers


def _run_ordinal(run: Any) -> int:
    """'m-abc:code:coder:6' -> 6 (0 when the id is not the mission:step:role:n shape)."""
    try:
        return int(str(run.id).rsplit(":", 1)[-1])
    except ValueError:
        return 0


def mission_layers(
    mission: Any,
    runs: list[Any],
    gates: list[Any],
    run_meta: dict[str, str],
    inner: dict[str, tuple[list[str], str]],
    events: list[Any],
    expanded_rounds: bool = False,
) -> list[GLayer]:
    """Build layered graph structure for orchestrated missions.

    Layers follow the review rounds: coders of a round (side by side when they
    were spawned in parallel), then the audit(s) that reviewed them, with the
    review outcome on the edge between them. Rounds older than the last
    COLLAPSE_KEEP_ROUNDS fold into one expandable summary node - unless
    `expanded_rounds` is True, which lays every round out.
    """
    plan = mission.plan or {}
    is_orchestrated = bool(plan.get("orchestrator") or {})

    if not is_orchestrated:
        # For non-orchestrated missions, just convert nodes to single-node layers
        nodes = _mission_nodes_step_grouped(mission, runs, gates, run_meta, inner)
        return [GLayer(nodes=[n], edge_to_next=GEdge()) for n in nodes]

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

    rounds = _review_rounds(runs)
    review_rounds = [r for r in rounds if r.is_review]
    many_rounds = len(review_rounds) > COLLAPSE_ROUND_THRESHOLD
    head: list[ReviewRound] = []
    if many_rounds:
        # fold at a ROUND boundary, never mid-round: the latest rounds stay
        # visible so the loop currently under review is not hidden
        split = len(review_rounds) - COLLAPSE_KEEP_ROUNDS
        head = review_rounds[:split]
        head_runs = [r for rd in head for r in rd.runs()]
        layers.append(
            GLayer(
                nodes=[
                    GNode(
                        key="rounds-collapsed",
                        title=(
                            f"rounds 1-{split} (collapsed)"
                            if not expanded_rounds
                            else f"rounds 1-{split} (expanded)"
                        ),
                        status=_derive_collapsed_status(head_runs),
                        sub=(
                            f"{len(head_runs)} runs · click to expand"
                            if not expanded_rounds
                            else f"{len(head_runs)} runs · click to collapse"
                        ),
                        expandable=True,
                        expanded=expanded_rounds,
                    )
                ]
            )
        )
        if not expanded_rounds:
            rounds = [r for r in rounds if r not in head]

    seen_review = False
    for i, round_ in enumerate(rounds):
        index = (len(head) + i) if (many_rounds and expanded_rounds) else i
        round_layers = _round_layers(round_, index, run_meta, inner, seen_review)
        # label the edge into the audits with the round's review outcome
        if round_.audits and round_.coders and len(round_layers) >= 2:
            round_layers[-2].edge_to_next = GEdge(label=_review_edge_label(round_.audits))
        layers.extend(round_layers)
        if round_.is_review:
            seen_review = True

    # Gates + end
    for kind in plan.get("gates") or []:
        matching = [g for g in gates if g.kind.value == kind]
        status = status_value(matching[-1].status) if matching else "pending"
        layers.append(
            GLayer(
                nodes=[
                    GNode(
                        key=f"gate:{kind}",
                        title=f"{kind} gate",
                        status=status,
                        sub="you decide",
                    )
                ],
                edge_to_next=GEdge(),
            )
        )
    end_status = {
        "done": "done",
        "failed": "failed",
        "cancelled": "cancelled",
    }.get(status_value(mission.status), "pending")
    layers.append(GLayer(nodes=[GNode(key="end", title="end", status=end_status, sub="")]))
    return layers


def mission_flow_summary(mission: Any, runs: list[Any], gates: list[Any]) -> list[str]:
    """The header flow line above the tabs: one token per phase, derived from
    the same run-derived rounds as the graph so the two cannot drift.

    e.g. ['investigator', '3 rounds (6 coders, 3 audits)', 'PR gate', 'end']
    """
    plan = mission.plan or {}
    if not (plan.get("orchestrator") or {}):
        return [n.title for n in _mission_nodes_step_grouped(mission, runs, gates, {}, {})]
    rounds = _review_rounds(runs)
    review_rounds = [r for r in rounds if r.is_review]
    tokens: list[str] = []
    for round_ in rounds:
        if not round_.is_review:
            tokens.extend(role_of(r.id) or str(r.role) for r in round_.runs())
    if review_rounds:
        coders = sum(len(r.coders) for r in rounds)
        audits = sum(len(r.audits) for r in rounds)
        n = len(review_rounds)
        plural = "s" if n > 1 else ""
        tokens.append(f"{n} round{plural} ({coders} coders, {audits} audits)")
    for kind in plan.get("gates") or []:
        tokens.append(f"{kind} gate")
    tokens.append("end")
    return tokens
