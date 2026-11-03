"""SQLite StateStore: demo/test-grade backend.

One aiosqlite connection, WAL on, mission concurrency 1 expected (docs/design.md).
Postgres is the deployment backend; both must pass tests/conformance/test_store.py.
"""

from __future__ import annotations

import json
import time
from typing import Any

import aiosqlite

from rework.kernel.state.records import (
    BudgetState,
    Charge,
    EventRecord,
    EventType,
    Gate,
    GateKind,
    GateStatus,
    Mission,
    MissionStatus,
    Run,
    RunStatus,
)

_DDL = """
CREATE TABLE IF NOT EXISTS missions (
    id TEXT PRIMARY KEY,
    objective TEXT NOT NULL,
    repo_path TEXT,
    status TEXT NOT NULL,
    plan TEXT,
    result TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL,
    role TEXT NOT NULL,
    step TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    status TEXT NOT NULL,
    model_ref TEXT,
    context TEXT,
    artifact TEXT,
    escalation TEXT,
    redirect TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_mission ON runs(mission_id);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
CREATE TABLE IF NOT EXISTS gates (
    id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    question TEXT NOT NULL,
    status TEXT NOT NULL,
    decision_note TEXT,
    created_at REAL NOT NULL,
    decided_at REAL
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_id TEXT,
    run_id TEXT,
    type TEXT NOT NULL,
    payload TEXT,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_mission ON events(mission_id, seq);
CREATE TABLE IF NOT EXISTS charges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_id TEXT,
    run_id TEXT,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd REAL NOT NULL,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_charges_mission ON charges(mission_id);
CREATE TABLE IF NOT EXISTS daemon_lock (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    owner TEXT NOT NULL,
    heartbeat_at REAL NOT NULL
);
"""


def _j(value: dict[str, Any] | None) -> str | None:
    return json.dumps(value) if value is not None else None


def _uj(value: str | None) -> dict[str, Any]:
    return json.loads(value) if value else {}


class SqliteStore:
    """Async SQLite StateStore. Open with `open_sqlite(path)`."""

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def aclose(self) -> None:
        await self._db.close()

    # -- internal helpers ---------------------------------------------------
    async def _fetch_one(self, sql: str, params: tuple[Any, ...]) -> aiosqlite.Row | None:
        async with self._db.execute(sql, params) as cur:
            return await cur.fetchone()

    async def _fetch_all(self, sql: str, params: tuple[Any, ...]) -> list[aiosqlite.Row]:
        async with self._db.execute(sql, params) as cur:
            return list(await cur.fetchall())

    @staticmethod
    def _mission(row: aiosqlite.Row) -> Mission:
        return Mission(
            id=row["id"],
            objective=row["objective"],
            repo_path=row["repo_path"],
            status=MissionStatus(row["status"]),
            plan=_uj(row["plan"]) or None,
            result=_uj(row["result"]) or None,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _run(row: aiosqlite.Row) -> Run:
        return Run(
            id=row["id"],
            mission_id=row["mission_id"],
            role=row["role"],
            step=row["step"],
            attempt=row["attempt"],
            status=RunStatus(row["status"]),
            model_ref=_uj(row["model_ref"]),
            context=_uj(row["context"]),
            artifact=_uj(row["artifact"]) or None,
            escalation=_uj(row["escalation"]) or None,
            redirect=row["redirect"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _gate(row: aiosqlite.Row) -> Gate:
        return Gate(
            id=row["id"],
            mission_id=row["mission_id"],
            kind=GateKind(row["kind"]),
            question=row["question"],
            status=GateStatus(row["status"]),
            decision_note=row["decision_note"],
            created_at=row["created_at"],
            decided_at=row["decided_at"],
        )

    # -- missions -----------------------------------------------------------
    async def create_mission(self, mission: Mission) -> Mission:
        await self._db.execute(
            "INSERT INTO missions (id, objective, repo_path, status, plan, result,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                mission.id,
                mission.objective,
                mission.repo_path,
                mission.status.value,
                _j(mission.plan),
                _j(mission.result),
                mission.created_at,
                mission.updated_at,
            ),
        )
        await self._db.commit()
        return mission

    async def get_mission(self, mission_id: str) -> Mission | None:
        row = await self._fetch_one("SELECT * FROM missions WHERE id = ?", (mission_id,))
        return self._mission(row) if row else None

    async def update_mission(
        self,
        mission_id: str,
        *,
        status: MissionStatus | None = None,
        plan: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
    ) -> Mission | None:
        sets: list[str] = []
        params: list[Any] = []
        if status is not None:
            sets.append("status = ?")
            params.append(status.value)
        if plan is not None:
            sets.append("plan = ?")
            params.append(_j(plan))
        if result is not None:
            sets.append("result = ?")
            params.append(_j(result))
        if not sets:
            return await self.get_mission(mission_id)
        sets.append("updated_at = ?")
        params.append(time.time())
        params.append(mission_id)
        await self._db.execute(f"UPDATE missions SET {', '.join(sets)} WHERE id = ?", tuple(params))
        await self._db.commit()
        return await self.get_mission(mission_id)

    async def list_missions(
        self, status: MissionStatus | None = None, limit: int = 50
    ) -> list[Mission]:
        if status is not None:
            rows = await self._fetch_all(
                "SELECT * FROM missions WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status.value, limit),
            )
        else:
            rows = await self._fetch_all(
                "SELECT * FROM missions ORDER BY created_at DESC LIMIT ?", (limit,)
            )
        return [self._mission(r) for r in rows]

    # -- runs -----------------------------------------------------------------
    async def create_run(self, run: Run) -> Run:
        """Idempotent on run.id: re-dispatch after a crash returns the stored row."""
        await self._db.execute(
            "INSERT OR IGNORE INTO runs (id, mission_id, role, step, attempt, status,"
            " model_ref, context, artifact, escalation, redirect, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run.id,
                run.mission_id,
                run.role,
                run.step,
                run.attempt,
                run.status.value,
                _j(run.model_ref),
                _j(run.context),
                _j(run.artifact),
                _j(run.escalation),
                run.redirect,
                run.created_at,
                run.updated_at,
            ),
        )
        await self._db.commit()
        existing = await self.get_run(run.id)
        assert existing is not None
        return existing

    async def get_run(self, run_id: str) -> Run | None:
        row = await self._fetch_one("SELECT * FROM runs WHERE id = ?", (run_id,))
        return self._run(row) if row else None

    async def update_run(
        self,
        run_id: str,
        *,
        status: RunStatus | None = None,
        model_ref: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
        artifact: dict[str, Any] | None = None,
        escalation: dict[str, Any] | None = None,
        redirect: str | None = None,
    ) -> Run | None:
        sets: list[str] = []
        params: list[Any] = []
        if status is not None:
            sets.append("status = ?")
            params.append(status.value)
        if model_ref is not None:
            sets.append("model_ref = ?")
            params.append(_j(model_ref))
        if context is not None:
            sets.append("context = ?")
            params.append(_j(context))
        if artifact is not None:
            sets.append("artifact = ?")
            params.append(_j(artifact))
        if escalation is not None:
            sets.append("escalation = ?")
            params.append(_j(escalation))
        if redirect is not None:
            sets.append("redirect = ?")
            params.append(redirect)
        if not sets:
            return await self.get_run(run_id)
        sets.append("updated_at = ?")
        params.append(time.time())
        params.append(run_id)
        await self._db.execute(f"UPDATE runs SET {', '.join(sets)} WHERE id = ?", tuple(params))
        await self._db.commit()
        return await self.get_run(run_id)

    async def list_runs(self, mission_id: str) -> list[Run]:
        rows = await self._fetch_all(
            "SELECT * FROM runs WHERE mission_id = ? ORDER BY created_at", (mission_id,)
        )
        return [self._run(r) for r in rows]

    async def list_runs_by_status(self, status: RunStatus) -> list[Run]:
        rows = await self._fetch_all(
            "SELECT * FROM runs WHERE status = ? ORDER BY created_at", (status.value,)
        )
        return [self._run(r) for r in rows]

    # -- gates ------------------------------------------------------------------
    async def create_gate(self, gate: Gate) -> Gate:
        await self._db.execute(
            "INSERT INTO gates (id, mission_id, kind, question, status, decision_note,"
            " created_at, decided_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                gate.id,
                gate.mission_id,
                gate.kind.value,
                gate.question,
                gate.status.value,
                gate.decision_note,
                gate.created_at,
                gate.decided_at,
            ),
        )
        await self._db.commit()
        return gate

    async def get_gate(self, gate_id: str) -> Gate | None:
        row = await self._fetch_one("SELECT * FROM gates WHERE id = ?", (gate_id,))
        return self._gate(row) if row else None

    async def decide_gate(
        self, gate_id: str, approved: bool, note: str | None = None
    ) -> Gate | None:
        await self._db.execute(
            "UPDATE gates SET status = ?, decision_note = ?, decided_at = ? WHERE id = ?"
            " AND status = ?",
            (
                GateStatus.APPROVED.value if approved else GateStatus.REJECTED.value,
                note,
                time.time(),
                gate_id,
                GateStatus.OPEN.value,
            ),
        )
        await self._db.commit()
        return await self.get_gate(gate_id)

    async def list_open_gates(self, mission_id: str | None = None) -> list[Gate]:
        if mission_id is not None:
            rows = await self._fetch_all(
                "SELECT * FROM gates WHERE status = ? AND mission_id = ? ORDER BY created_at",
                (GateStatus.OPEN.value, mission_id),
            )
        else:
            rows = await self._fetch_all(
                "SELECT * FROM gates WHERE status = ? ORDER BY created_at",
                (GateStatus.OPEN.value,),
            )
        return [self._gate(r) for r in rows]

    # -- events ---------------------------------------------------------------------
    async def append_event(self, event: EventRecord) -> EventRecord:
        cur = await self._db.execute(
            "INSERT INTO events (mission_id, run_id, type, payload, ts) VALUES (?, ?, ?, ?, ?)",
            (
                event.mission_id,
                event.run_id,
                event.type.value,
                _j(event.payload) or "{}",
                event.ts,
            ),
        )
        await self._db.commit()
        event.seq = cur.lastrowid
        return event

    async def events_since(
        self, seq: int = 0, mission_id: str | None = None, limit: int = 500
    ) -> list[EventRecord]:
        if mission_id is not None:
            rows = await self._fetch_all(
                "SELECT * FROM events WHERE seq > ? AND mission_id = ? ORDER BY seq LIMIT ?",
                (seq, mission_id, limit),
            )
        else:
            rows = await self._fetch_all(
                "SELECT * FROM events WHERE seq > ? ORDER BY seq LIMIT ?", (seq, limit)
            )
        return [
            EventRecord(
                seq=r["seq"],
                mission_id=r["mission_id"],
                run_id=r["run_id"],
                type=EventType(r["type"]),
                payload=_uj(r["payload"]),
                ts=r["ts"],
            )
            for r in rows
        ]

    # -- ledger ------------------------------------------------------------------------
    async def charge(self, charge: Charge) -> None:
        await self._db.execute(
            "INSERT INTO charges (mission_id, run_id, model, input_tokens, output_tokens,"
            " cost_usd, ts) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                charge.mission_id,
                charge.run_id,
                charge.model,
                charge.input_tokens,
                charge.output_tokens,
                charge.cost_usd,
                charge.ts,
            ),
        )
        await self._db.commit()

    async def mission_spend(self, mission_id: str) -> BudgetState:
        row = await self._fetch_one(
            "SELECT COALESCE(SUM(input_tokens), 0) AS i, COALESCE(SUM(output_tokens), 0) AS o,"
            " COALESCE(SUM(cost_usd), 0.0) AS c FROM charges WHERE mission_id = ?",
            (mission_id,),
        )
        assert row is not None
        return BudgetState(input_tokens=row["i"], output_tokens=row["o"], spent_usd=row["c"])

    # -- leader lock ----------------------------------------------------------------------
    async def acquire_lock(self, owner: str, stale_after_s: float = 60.0) -> bool:
        row = await self._fetch_one("SELECT owner, heartbeat_at FROM daemon_lock WHERE id = 1", ())
        if (
            row is not None
            and row["owner"] != owner
            and row["heartbeat_at"] > (time.time() - stale_after_s)
        ):
            return False  # live foreign lock
        await self._db.execute(
            "INSERT INTO daemon_lock (id, owner, heartbeat_at) VALUES (1, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET owner = ?, heartbeat_at = ?",
            (owner, time.time(), owner, time.time()),
        )
        await self._db.commit()
        return True

    async def heartbeat_lock(self, owner: str) -> bool:
        cur = await self._db.execute(
            "UPDATE daemon_lock SET heartbeat_at = ? WHERE id = 1 AND owner = ?",
            (time.time(), owner),
        )
        await self._db.commit()
        return cur.rowcount > 0

    async def release_lock(self, owner: str) -> None:
        await self._db.execute("DELETE FROM daemon_lock WHERE id = 1 AND owner = ?", (owner,))
        await self._db.commit()


async def open_sqlite(path: str) -> SqliteStore:
    db = await aiosqlite.connect(path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode = WAL")
    await db.execute("PRAGMA foreign_keys = ON")
    await db.executescript(_DDL)
    await db.commit()
    return SqliteStore(db)
