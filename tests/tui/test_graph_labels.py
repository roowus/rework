"""The Graph draws two different levels, and the labels must keep them apart.

Level 1 - inside ONE run: the role's own LangGraph nodes (coder:
prepare/loop/handoff, auditor: prepare/loop/report) are drawn as a strip that
must say it is about this run and use friendly TUI-only names (`inner_step_name`).

Level 2 - the mission flow: review rounds, where the edges carry the audit's
mechanical verdict ('request changes' -> 'approve') so the audit loop is
visible at mission level. The roles' node names and the stored payloads are
untouched: mapping is presentation only."""

from __future__ import annotations

from typing import Any

import pytest
from textual.app import App, ComposeResult

from rework.kernel.state.records import Mission, MissionStatus, Run, RunStatus
from rework.tui.widgets.graph import (
    GNode,
    GraphView,
    _Node,
    inner_step_name,
    mission_layers,
)


class NodeApp(App[None]):
    """A bare app mounting one graph node (no store, no full ReworkApp)."""

    def __init__(self, node: GNode) -> None:
        super().__init__()
        self._node = node

    def compose(self) -> ComposeResult:
        yield _Node(self._node)


class GraphApp(App[None]):
    def compose(self) -> ComposeResult:
        yield GraphView(id="m-graph")


async def _painted(g_node: GNode, node: GNode | None = None) -> str:
    widget = _Node(g_node)
    app = NodeApp(widget)
    async with app.run_test() as pilot:
        widget.paint(node or g_node, False)
        await pilot.pause()
        return str(widget.render())


@pytest.mark.parametrize(
    ("raw", "friendly"),
    [
        ("prepare", "set up"),
        ("loop", "working (tool calls)"),
        ("report", "report"),
        ("handoff", "hand off"),
    ],
)
async def test_inner_step_names_are_friendly_in_the_tui(raw: str, friendly: str) -> None:
    assert inner_step_name(raw) == friendly
    text = await _painted(GNode(key="r", title="coder #1", inner=[raw]))
    assert f"inside this run: {friendly}" in text, text
    if raw != friendly:
        # the role's own node name must not leak into the mission-level flow
        assert raw not in text, text


async def test_inner_strip_is_labelled_as_belonging_to_one_run() -> None:
    text = await _painted(
        GNode(
            key="r",
            title="coder #1",
            status="running",
            inner=["prepare", "loop", "handoff"],
            inner_at="loop",
        )
    )
    assert "inside this run:" in text, text
    # and the raw role node names must not leak as if they were mission steps
    stripped = text.replace("working (tool calls)", "")
    assert "loop" not in stripped and "prepare" not in stripped and "handoff" not in stripped


async def test_current_inner_step_keeps_the_highlight() -> None:
    text = await _painted(
        GNode(
            key="r",
            title="auditor #1",
            status="running",
            inner=["prepare", "loop", "report"],
            inner_at="report",
        )
    )
    assert "▶ report" in text, text
    # exactly one step is current; the marker is our cue to check which one
    marked = [ln for ln in text.splitlines() if "▶" in ln]
    assert marked == ["inside this run: set up ─ working (tool calls) ─ ▶ report"], text


@pytest.mark.parametrize("current", ["prepare", "loop", "report", "handoff"])
async def test_exactly_one_step_is_marked_current(current: str) -> None:
    text = await _painted(
        GNode(
            key="r", title="coder", inner=["prepare", "loop", "report", "handoff"], inner_at=current
        )
    )
    assert text.count("▶") == 1, text
    assert f"▶ {inner_step_name(current)}" in text, text


async def test_unknown_inner_node_name_falls_through_unchanged() -> None:
    text = await _painted(
        GNode(key="r", title="reviewer", inner=["prepare", "bogus_node"], inner_at="bogus_node")
    )
    assert "bogus_node" in text, text
    assert "▶ bogus_node" in text, text


async def test_run_without_inner_steps_has_no_label() -> None:
    text = await _painted(GNode(key="r", title="coder #1", status="completed"))
    assert "inside this run:" not in text, text


async def test_non_running_run_keeps_the_strip_but_no_marker() -> None:
    """Unchanged behaviour: the builders blank inner_at unless the run is
    running, so a finished run shows a flat, unmarked strip."""
    text = await _painted(
        GNode(key="r", title="coder #1", status="completed", inner=["prepare", "loop", "handoff"])
    )
    assert "inside this run:" in text, text
    assert "▶" not in text, text


def _run(run_id: str, role: str, attempt: int, artifact: dict[str, Any] | None = None) -> Run:
    return Run(
        id=run_id,
        mission_id="m-labels0001",
        role=role,
        step="code" if role == "coder" else "audit",
        attempt=attempt,
        status=RunStatus.COMPLETED,
        context={"strictness": "standard"} if role == "auditor" else {},
        artifact=artifact,
    )


def _mission() -> Mission:
    return Mission(
        id="m-labels0001",
        objective="two rounds of review",
        status=MissionStatus.RUNNING,
        plan={"orchestrator": {"model": "pro"}, "playbook": "standard", "gates": []},
    )


def _review_mission_runs() -> list[Run]:
    return [
        _run("m-labels0001:code:coder:1", "coder", 1, {"branch": "b"}),
        _run(
            "m-labels0001:audit:auditor:1",
            "auditor",
            1,
            {"findings": [{"title": "Bug", "severity": "critical", "detail": "", "evidence": []}]},
        ),
        _run("m-labels0001:code:coder:2", "coder", 2, {"branch": "b"}),
        _run("m-labels0001:audit:auditor:2", "auditor", 2, {"findings": []}),
    ]


def test_leaving_an_audit_is_labelled_with_its_verdict() -> None:
    runs = _review_mission_runs()
    layers = mission_layers(_mission(), runs, [], {}, {}, [])
    by_key = {node.key: (i, node) for i, layer in enumerate(layers) for node in layer.nodes}
    audit1_layer, _ = by_key["m-labels0001:audit:auditor:1"]
    assert layers[audit1_layer].edge_to_next is not None
    assert layers[audit1_layer].edge_to_next.label == "request changes"

    # the edge INTO the audit keeps its (more detailed) findings label
    coder1_layer, _ = by_key["m-labels0001:code:coder:1"]
    into_audit = layers[coder1_layer].edge_to_next
    assert into_audit is not None and "request changes" in into_audit.label, into_audit

    # an approving audit still labels where the mission goes next
    audit2_layer, _ = by_key["m-labels0001:audit:auditor:2"]
    leaving = layers[audit2_layer].edge_to_next
    assert leaving is not None and leaving.label == "approve", leaving


async def test_mission_flow_renders_the_review_verdicts() -> None:
    runs = _review_mission_runs()
    layers = mission_layers(_mission(), runs, [], {}, {}, [])
    app = GraphApp()
    async with app.run_test() as pilot:
        view = app.query_one("#m-graph", GraphView)
        view.set_layers(layers)
        await pilot.pause()
        edges = [str(e.render()) for e in app.query(".gedge")]
        text = "\n".join(edges)
        assert "request changes" in text, text
        assert "approve" in text, text
        # the review direction is visible at mission level: audit -> revision
        assert any(e.strip().splitlines()[-1].strip() == "request changes" for e in edges), text
