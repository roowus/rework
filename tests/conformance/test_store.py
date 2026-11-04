"""StateStore conformance — every backend must pass this suite unchanged.

This is the contract that lets SQLite (demo/test) and Postgres (deployment)
swap freely behind the seam (docs/adr/0001).
"""

from __future__ import annotations

import asyncio

from rework.kernel.state.records import (
    BudgetState,
    Charge,
    Complaint,
    ComplaintKind,
    ComplaintStatus,
    EventRecord,
    EventType,
    Gate,
    GateKind,
    Mission,
    MissionStatus,
    Project,
    Run,
    RunStatus,
    new_id,
    run_id,
)
from rework.kernel.state.store import StateStore


async def test_mission_roundtrip(store: StateStore) -> None:
    m = Mission(id=new_id("m"), objective="fix the login bug", repo_path="/tmp/repo")
    created = await store.create_mission(m)
    assert created.id == m.id

    got = await store.get_mission(m.id)
    assert got is not None
    assert got.objective == "fix the login bug"
    assert got.status == MissionStatus.PENDING

    updated = await store.update_mission(
        m.id,
        status=MissionStatus.RUNNING,
        plan={"steps": [{"role": "investigator"}]},
        repo_path="/staged/on/daemon",
    )
    assert updated is not None
    assert updated.status == MissionStatus.RUNNING
    assert updated.plan == {"steps": [{"role": "investigator"}]}
    # the daemon records where IT staged/cloned the repo (ADR 0005)
    assert updated.repo_path == "/staged/on/daemon"

    assert await store.get_mission("missing") is None


async def test_list_missions_by_status(store: StateStore) -> None:
    a = await store.create_mission(Mission(id=new_id("m"), objective="a"))
    b = await store.create_mission(Mission(id=new_id("m"), objective="b"))
    await store.update_mission(b.id, status=MissionStatus.DONE)

    pending = await store.list_missions(status=MissionStatus.PENDING)
    assert [m.id for m in pending] == [a.id]
    everything = await store.list_missions()
    assert len(everything) == 2


async def test_run_create_is_idempotent(store: StateStore) -> None:
    m = await store.create_mission(Mission(id=new_id("m"), objective="o"))
    rid = run_id(m.id, "investigate", "investigator", 1)
    run = Run(id=rid, mission_id=m.id, role="investigator", step="investigate")

    first = await store.create_run(run)
    # crash-recovery re-dispatch: same deterministic id must not duplicate
    second = await store.create_run(run)
    assert first.id == second.id == rid
    runs = await store.list_runs(m.id)
    assert len(runs) == 1


async def test_run_updates(store: StateStore) -> None:
    m = await store.create_mission(Mission(id=new_id("m"), objective="o"))
    rid = run_id(m.id, "code", "coder", 1)
    await store.create_run(Run(id=rid, mission_id=m.id, role="coder", step="code"))

    updated = await store.update_run(
        rid,
        status=RunStatus.RUNNING,
        model_ref={"model": "test-model", "tier": 1},
        context={"branch": "fix/x"},
    )
    assert updated is not None
    assert updated.status == RunStatus.RUNNING
    assert updated.model_ref == {"model": "test-model", "tier": 1}

    done = await store.update_run(
        rid, status=RunStatus.COMPLETED, artifact={"kind": "diff", "path": "d.diff"}
    )
    assert done is not None
    assert done.artifact == {"kind": "diff", "path": "d.diff"}

    running = await store.list_runs_by_status(RunStatus.COMPLETED)
    assert [r.id for r in running] == [rid]


async def test_gate_lifecycle(store: StateStore) -> None:
    m = await store.create_mission(Mission(id=new_id("m"), objective="o"))
    gate = Gate(id=new_id("gate"), mission_id=m.id, kind=GateKind.SHIP, question="ship it?")
    await store.create_gate(gate)

    open_gates = await store.list_open_gates(m.id)
    assert [g.id for g in open_gates] == [gate.id]

    decided = await store.decide_gate(gate.id, approved=True, note="lgtm")
    assert decided is not None
    assert decided.decision_note == "lgtm"
    assert decided.decided_at is not None
    assert await store.list_open_gates(m.id) == []

    # deciding again is a no-op that must not resurrect the gate
    again = await store.decide_gate(gate.id, approved=False)
    assert again is not None
    assert again.decision_note == "lgtm"  # first decision stands


async def test_events_seq_cursor(store: StateStore) -> None:
    m = await store.create_mission(Mission(id=new_id("m"), objective="o"))
    other = await store.create_mission(Mission(id=new_id("m"), objective="p"))

    e1 = await store.append_event(EventRecord(mission_id=m.id, type=EventType.MISSION_CREATED))
    e2 = await store.append_event(EventRecord(mission_id=other.id, type=EventType.INFO))
    e3 = await store.append_event(EventRecord(mission_id=m.id, type=EventType.ROLE_SPAWNED))

    assert e1.seq is not None and e2.seq is not None and e3.seq is not None
    assert e1.seq < e2.seq < e3.seq

    mine = await store.events_since(0, mission_id=m.id)
    assert [e.type for e in mine] == [EventType.MISSION_CREATED, EventType.ROLE_SPAWNED]

    after_e1 = await store.events_since(e1.seq)  # e1.seq is e1's own seq; since is exclusive
    assert all(e.seq is not None and e.seq > e1.seq for e in after_e1)


async def test_ledger_sums(store: StateStore) -> None:
    m = await store.create_mission(Mission(id=new_id("m"), objective="o"))
    await store.charge(
        Charge(mission_id=m.id, model="a", input_tokens=100, output_tokens=50, cost_usd=0.01)
    )
    await store.charge(
        Charge(mission_id=m.id, model="b", input_tokens=10, output_tokens=5, cost_usd=0.02)
    )

    spend = await store.mission_spend(m.id)
    assert spend == BudgetState(input_tokens=110, output_tokens=55, spent_usd=0.03)


async def test_leader_lock(store: StateStore) -> None:
    assert await store.acquire_lock("daemon-1") is True
    assert await store.acquire_lock("daemon-2") is False  # live foreign lock refused
    assert await store.heartbeat_lock("daemon-2") is False
    assert await store.heartbeat_lock("daemon-1") is True
    await store.release_lock("daemon-1")
    assert await store.acquire_lock("daemon-2") is True


async def test_leader_lock_stale_takeover(store: StateStore) -> None:
    """A SIGKILLed daemon cannot clean up; its lock must go stale quickly so a
    restart can take over (daemon passes a small stale_after_s, ADR 0002)."""
    assert await store.acquire_lock("dead-daemon", stale_after_s=0.05) is True
    await asyncio.sleep(0.15)  # heartbeat would have kept it alive; it didn't
    assert await store.acquire_lock("fresh-daemon", stale_after_s=0.05) is True
    assert await store.heartbeat_lock("dead-daemon") is False


async def test_leader_lock_concurrent_acquire(store: StateStore) -> None:
    """The old acquire was read-then-write: two daemons could both pass the
    stale check and both win. The atomic upsert admits exactly one."""
    results = await asyncio.gather(
        *[store.acquire_lock(f"daemon-{i}", stale_after_s=60.0) for i in range(8)]
    )
    assert sum(results) == 1
    # and the winner keeps it against further contenders
    assert await store.acquire_lock("latecomer", stale_after_s=60.0) is False


async def test_complaints_roundtrip(store: StateStore) -> None:
    """ADR 0004: complaints are typed, fire-and-forget feedback in the store."""
    c = Complaint(
        kind=ComplaintKind.NEEDED_INFO,
        source_mission_id="m1",
        source_run_id="m1:audit:auditor:1",
        source_role="auditor",
        summary="audit lacked the test output",
        detail="could not judge coverage without the pytest run",
        evidence=["handoff diff had no test log"],
        suggestion="attach run_stats + test output to the coder handoff",
    )
    filed = await store.file_complaint(c)
    assert filed.id == c.id

    got = await store.get_complaint(c.id)
    assert got is not None
    assert got.kind == ComplaintKind.NEEDED_INFO
    assert got.evidence == ["handoff diff had no test log"]
    assert got.status == ComplaintStatus.OPEN

    # filters
    assert len(await store.list_complaints(status=ComplaintStatus.OPEN)) == 1
    assert len(await store.list_complaints(kind=ComplaintKind.TOOL_MISSING)) == 0

    # lifecycle: open -> triaged -> resolved with the fixing commit noted
    await store.update_complaint(c.id, status=ComplaintStatus.TRIAGED)
    assert len(await store.list_complaints(status=ComplaintStatus.OPEN)) == 0
    resolved = await store.update_complaint(c.id, status=ComplaintStatus.RESOLVED)
    assert resolved is not None and resolved.status == ComplaintStatus.RESOLVED


async def test_project_roundtrip(store: StateStore) -> None:
    """ADR 0005: projects are labels with settings and repo references."""
    p = Project(
        name="capstone",
        settings={
            "transport": "git",
            "results": "push_branch",
            "repos": [{"name": "firmware", "kind": "github", "url": "github.com/x/y"}],
        },
    )
    created = await store.create_project(p)
    assert created.id == p.id

    got = await store.get_project(p.id)
    assert got is not None
    assert got.name == "capstone"
    assert got.settings["transport"] == "git"
    assert got.settings["repos"][0]["name"] == "firmware"

    updated = await store.update_project(p.id, settings={**p.settings, "transport": "upload"})
    assert updated is not None
    assert updated.settings["transport"] == "upload"
    assert updated.name == "capstone"  # untouched

    assert await store.get_project("p-missing") is None
    listed = await store.list_projects()
    assert [x.id for x in listed] == [p.id]


async def test_mission_carries_project_and_origin(store: StateStore) -> None:
    """ADR 0005: missions reference projects + git origins instead of only
    creating-machine paths."""
    p = await store.create_project(Project(name="capstone"))
    m = Mission(
        id=new_id("m"),
        objective="build the air gun adapter",
        project_id=p.id,
        repo_origin="https://github.com/roowus/rework.git",
    )
    await store.create_mission(m)
    got = await store.get_mission(m.id)
    assert got is not None
    assert got.project_id == p.id
    assert got.repo_origin == "https://github.com/roowus/rework.git"
    assert got.repo_path is None  # daemon resolves the path itself
