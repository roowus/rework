"""Unit tests for budget math and the gate manager + event bus (over SQLite)."""

from __future__ import annotations

from pathlib import Path

import pytest

from rework.kernel.budget.ledger import apply_charge, guard
from rework.kernel.events.bus import InProcessEventBus
from rework.kernel.gates import GateManager
from rework.kernel.state.records import (
    Budget,
    BudgetExceeded,
    BudgetState,
    Charge,
    EventRecord,
    EventType,
    GateKind,
    GateStatus,
    Mission,
    new_id,
)
from rework.kernel.state.sqlite import open_sqlite


@pytest.fixture
async def db(tmp_path: Path):
    store = await open_sqlite(str(tmp_path / "u.sqlite3"))
    yield store
    await store.aclose()


# -- budget ledger ------------------------------------------------------------


def test_apply_charge_folds() -> None:
    state = BudgetState()
    state = apply_charge(state, Charge(model="m", input_tokens=10, output_tokens=5, cost_usd=0.1))
    state = apply_charge(state, Charge(model="m", input_tokens=1, output_tokens=2, cost_usd=0.2))
    assert state.input_tokens == 11
    assert state.output_tokens == 7
    assert state.spent_usd == pytest.approx(0.3)


def test_guard_raises_past_usd_cap() -> None:
    state = BudgetState(spent_usd=1.5)
    with pytest.raises(BudgetExceeded):
        guard(state, Budget(max_usd=1.0))


def test_guard_raises_past_token_caps() -> None:
    with pytest.raises(BudgetExceeded):
        guard(BudgetState(input_tokens=2000), Budget(max_input_tokens=1000))
    with pytest.raises(BudgetExceeded):
        guard(BudgetState(output_tokens=2000), Budget(max_output_tokens=1000))


def test_guard_passes_under_caps() -> None:
    guard(BudgetState(spent_usd=0.5, input_tokens=10, output_tokens=10), Budget(max_usd=1.0))


# -- gates + bus ---------------------------------------------------------------


async def test_gate_request_and_decide(db) -> None:
    bus = InProcessEventBus(db)
    gates = GateManager(db, bus)
    m = await db.create_mission(Mission(id=new_id("m"), objective="o"))

    q = bus.subscribe()
    gate = await gates.request(m.id, GateKind.SHIP, "ship it?")
    assert gate.status == GateStatus.OPEN

    opened = await gates.open_gates(m.id)
    assert [g.id for g in opened] == [gate.id]

    got_event = q.get_nowait()
    assert got_event.type == EventType.GATE_OPENED
    assert got_event.payload["gate_id"] == gate.id

    decided = await gates.decide(gate.id, approved=True, note="lgtm")
    assert decided is not None and decided.status == GateStatus.APPROVED
    decided_event = q.get_nowait()
    assert decided_event.type == EventType.GATE_DECIDED

    # re-decide is a no-op: no further events
    await gates.decide(gate.id, approved=False)
    assert q.empty()
    # bus fan-out persisted everything — the store is the truth
    persisted = await db.events_since(0, mission_id=m.id)
    assert [e.type for e in persisted] == [
        EventType.GATE_OPENED,
        EventType.GATE_DECIDED,
    ]


async def test_bus_slow_consumer_does_not_stall(db) -> None:
    bus = InProcessEventBus(db)
    q = bus.subscribe()
    for i in range(1500):  # over the 1000 queue cap
        await bus.publish(EventRecord(payload={"i": i}))
    persisted = await db.events_since(0, limit=2000)  # replay API paginates at 500
    assert len(persisted) == 1500  # nothing lost where it counts
    assert not q.empty()  # consumer got what fit


async def test_bus_unsubscribe(db) -> None:
    bus = InProcessEventBus(db)
    q = bus.subscribe()
    bus.unsubscribe(q)
    await bus.publish(EventRecord(payload={"x": 1}))
    assert q.empty()
