"""A final gate's question must name the audit the mission actually finished on.

Found live (2026-11-2x): mission m-c97c1532f1c1's open_pr gate read "audit
verdict: request changes" and was approved while the final round-3 audit had
`findings: []`, and m-434b672f262d did the same with a clean round-4 audit. Two
things had to be true for that: the question string was only built when the gate
row was CREATED (so a reused row kept its frozen text), and the artifact handed
to `_final_gates` could be one round behind the mission's real last audit.

These tests pin the fix at the engine seam with no LLM in the loop: the store
alone decides the round, a reused OPEN gate is re-worded (with an INFO event), a
DECIDED gate is never touched, and an unchanged question writes nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rework.kernel.events.bus import InProcessEventBus
from rework.kernel.gates import GateManager
from rework.kernel.state.records import (
    EventType,
    Gate,
    GateKind,
    GateStatus,
    Mission,
    Project,
    Run,
    RunStatus,
    new_id,
)
from rework.kernel.state.sqlite import SqliteStore, open_sqlite
from rework.orchestrator.engine import MissionRunner

STALE_SHIP = "Ship the mission result? audit verdict: request changes (round 1 of 1)"
STALE_PR = "Open a pull request for this change? audit verdict: request changes (round 1 of 1)"
CLEAN = {"findings": []}
BRUISED = {"findings": [{"finding": "off by one", "severity": "high", "reason": "r"}]}
BROKEN = {"findings": [{"finding": "add() returns 0", "severity": "critical", "reason": "r"}]}


async def _world(tmp_path: Path, mission_id: str = "m1") -> tuple[SqliteStore, Any, Any]:
    store = await open_sqlite(str(tmp_path / "g.sqlite3"))
    bus = InProcessEventBus(store)
    return store, bus, _runner(store, bus, mission_id)


def _runner(store: SqliteStore, bus: InProcessEventBus, mission_id: str) -> Any:
    return MissionRunner(
        mission_id,
        store,
        bus,
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        Path("."),
        poll_interval_s=0.01,
    )


async def _seed_audit(
    store: SqliteStore, mission_id: str, attempt: int, artifact: dict[str, Any], *, created: float
) -> None:
    """One COMPLETED auditor run, i.e. one audit round the mission finished."""
    await store.create_run(
        Run(
            id=new_id("run"),
            mission_id=mission_id,
            role="auditor",
            step="audit",
            attempt=attempt,
            status=RunStatus.COMPLETED,
            artifact=artifact,
            created_at=created,
            updated_at=created,
        )
    )


def _no_waiting_decision(runner: Any, decision: bool | None) -> None:
    """Skip the store poll: these tests are about the question text, not about
    waiting for the owner. The gate row itself is left as the engine left it."""

    async def fake(mission_id: str, gate_id: str) -> bool | None:
        return decision

    runner._gate_decision = fake  # type: ignore[method-assign]


def _refresh_events(events: list[Any]) -> list[str]:
    return [
        str(e.payload.get("note") or "")
        for e in events
        if e.type == EventType.INFO and "question refreshed" in str(e.payload.get("note") or "")
    ]


async def test_an_open_gate_is_reworded_to_the_final_audit_round(tmp_path: Path) -> None:
    """(a) opened at round 1 with "request changes", re-entered after two more
    completed audit rounds that end clean: the question now names the verdict of
    the LAST round and its ordinal, the gate is still OPEN, and the refresh is
    on the event log."""
    store, _bus, runner = await _world(tmp_path)
    try:
        mission = await store.create_mission(
            Mission(id="m1", objective="x", plan={"steps": [], "gates": ["ship"]})
        )
        await _seed_audit(store, "m1", 1, BROKEN, created=1.0)
        gate = Gate(id=new_id("gate"), mission_id="m1", kind=GateKind.SHIP, question=STALE_SHIP)
        await store.create_gate(gate)
        await _seed_audit(store, "m1", 2, BRUISED, created=2.0)
        await _seed_audit(store, "m1", 3, CLEAN, created=3.0)

        _no_waiting_decision(runner, False)
        # the caller's artifact is the round-1 one: the store must win
        await runner._final_gates(mission, {}, BROKEN)

        stored = await store.get_gate(gate.id)
        assert stored is not None
        assert stored.question == "Ship the mission result? audit verdict: approve (round 3 of 3)"
        assert stored.status == GateStatus.OPEN, "a refresh must not decide the gate"
        notes = _refresh_events(await store.events_since(0, mission_id="m1"))
        assert len(notes) == 1, notes
        assert gate.id in notes[0] and "round 3 of 3" in notes[0]
    finally:
        await store.aclose()


async def test_a_decided_gate_is_never_rewritten(tmp_path: Path) -> None:
    """(b) approved and rejected rows keep the text the owner answered, even
    when the verdict and the round moved, and publish no refresh event."""
    store, bus, _ = await _world(tmp_path)
    try:
        for status, decision, question in (
            (GateStatus.APPROVED, True, STALE_SHIP),
            (GateStatus.REJECTED, False, STALE_SHIP),
        ):
            mission_id = new_id("m")
            runner = _runner(store, bus, mission_id)
            await store.create_mission(
                Mission(id=mission_id, objective="x", plan={"steps": [], "gates": ["ship"]})
            )
            await _seed_audit(store, mission_id, 1, BROKEN, created=1.0)
            await _seed_audit(store, mission_id, 2, CLEAN, created=2.0)
            gate = Gate(
                id=new_id("gate"),
                mission_id=mission_id,
                kind=GateKind.SHIP,
                question=question,
                status=status,
            )
            await store.create_gate(gate)
            _no_waiting_decision(runner, decision)
            plan_mission = await store.get_mission(mission_id)
            assert plan_mission is not None
            await runner._final_gates(plan_mission, {}, BROKEN)

            stored = await store.get_gate(gate.id)
            assert stored is not None
            assert stored.question == question, f"a {status.value} gate was rewritten"
            assert stored.status == status
            assert _refresh_events(await store.events_since(0, mission_id=mission_id)) == []
            # the reuse record stands: no duplicate gate for a decision already made
            gates = await store.list_gates(mission_id)
            assert [g.id for g in gates] == [gate.id]
    finally:
        await store.aclose()


async def test_an_unchanged_question_writes_nothing(tmp_path: Path) -> None:
    """(c) same verdict, same round: no store update, no event, row untouched."""
    store, _bus, runner = await _world(tmp_path)
    try:
        mission = await store.create_mission(
            Mission(id="m1", objective="x", plan={"steps": [], "gates": ["ship"]})
        )
        await _seed_audit(store, "m1", 1, CLEAN, created=1.0)
        gate = Gate(
            id=new_id("gate"),
            mission_id="m1",
            kind=GateKind.SHIP,
            question="Ship the mission result? audit verdict: approve (round 1 of 1)",
        )
        await store.create_gate(gate)
        writes: list[str] = []

        async def counting(gate_id: str, question: str) -> Gate | None:
            writes.append(question)
            return await SqliteStore.set_gate_question(store, gate_id, question)

        store.set_gate_question = counting  # type: ignore[method-assign]
        _no_waiting_decision(runner, False)
        await runner._final_gates(mission, {}, CLEAN)

        assert writes == [], "an unchanged question must not reach the store"
        stored = await store.get_gate(gate.id)
        assert stored is not None
        assert stored.question == gate.question
        assert stored.created_at == gate.created_at  # nothing re-created either
        assert _refresh_events(await store.events_since(0, mission_id="m1")) == []
    finally:
        await store.aclose()


async def test_the_open_pr_suffixes_follow_the_current_arguments(tmp_path: Path) -> None:
    """The strict-gate category clause and the pr_gate=always clause are rebuilt
    on every entry, so a reused gate never keeps a stale category."""
    store, _bus, runner = await _world(tmp_path)
    try:
        mission = await store.create_mission(
            Mission(id="m1", objective="x", plan={"steps": [], "publish": "pr"})
        )
        await _seed_audit(store, "m1", 1, CLEAN, created=1.0)
        gate = Gate(
            id=new_id("gate"),
            mission_id="m1",
            kind=GateKind.OPEN_PR,
            question=STALE_PR + ". Gate category: security — old reason",
        )
        await store.create_gate(gate)
        _no_waiting_decision(runner, False)

        await runner._final_gates(
            mission, {}, CLEAN, ask_before_pr=True, reason="new reason", gate_category="money"
        )
        stored = await store.get_gate(gate.id)
        assert stored is not None
        assert stored.question == (
            "Open a pull request for this change? audit verdict: approve (round 1 of 1)"
            ". Gate category: money — new reason"
        ), stored.question
    finally:
        await store.aclose()


async def test_the_forced_pr_gate_keeps_its_no_category_clause(tmp_path: Path) -> None:
    """pr_gate=always with no category: the forced clause survives the refresh."""
    store, _bus, runner = await _world(tmp_path)
    try:
        project = await store.create_project(
            Project(id="p1", name="proj", settings={"pr_gate": "always"})
        )
        mission = await store.create_mission(
            Mission(
                id="m1",
                objective="x",
                project_id=project.id,
                plan={"steps": [], "publish": "pr"},
            )
        )
        await _seed_audit(store, "m1", 1, CLEAN, created=1.0)
        gate = Gate(id=new_id("gate"), mission_id="m1", kind=GateKind.OPEN_PR, question=STALE_PR)
        await store.create_gate(gate)
        _no_waiting_decision(runner, False)

        await runner._final_gates(mission, {}, CLEAN)
        stored = await store.get_gate(gate.id)
        assert stored is not None
        assert stored.question.startswith(
            "Open a pull request for this change? audit verdict: approve (round 1 of 1)"
        )
        assert "pr_gate=always" in stored.question
        assert "Gate category:" not in stored.question
    finally:
        await store.aclose()


async def test_no_completed_audit_falls_back_without_a_round(tmp_path: Path) -> None:
    """No completed auditor run: the caller's artifact still decides the verdict
    and the round parenthetical is left out (never a crash, never "round 0")."""
    store, bus, runner = await _world(tmp_path)
    try:
        mission = await store.create_mission(
            Mission(id="m1", objective="x", plan={"steps": [], "gates": ["ship"]})
        )
        _no_waiting_decision(runner, False)
        await runner._final_gates(mission, {}, BROKEN)
        gates = await store.list_gates("m1")
        assert [g.question for g in gates] == [
            "Ship the mission result? audit verdict: request changes"
        ]
        assert await GateManager(store, bus).open_gates("m1")
    finally:
        await store.aclose()
