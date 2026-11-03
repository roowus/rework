"""Postgres StateStore: the deployment backend.

Same contract as SqliteStore; both must pass tests/conformance/test_store.py.
Checkpointer tables (langgraph-checkpoint-postgres) live in the same database
but are owned by the checkpointer, not this store.
"""

from __future__ import annotations

import json
import time
from typing import Any

import psycopg
from psycopg.rows import DictRow

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

_DDL = [
    """
    CREATE TABLE IF NOT EXISTS missions (
        id TEXT PRIMARY KEY,
        objective TEXT NOT NULL,
        repo_path TEXT,
        status TEXT NOT NULL,
        plan TEXT,
        result TEXT,
        created_at DOUBLE PRECISION NOT NULL,
        updated_at DOUBLE PRECISION NOT NULL
    )
    """,
    """
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
        created_at DOUBLE PRECISION NOT NULL,
        updated_at DOUBLE PRECISION NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_runs_mission ON runs(mission_id)",
    "CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status)",
    """
    CREATE TABLE IF NOT EXISTS gates (
        id TEXT PRIMARY KEY,
        mission_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        question TEXT NOT NULL,
        status TEXT NOT NULL,
        decision_note TEXT,
        created_at DOUBLE PRECISION NOT NULL,
        decided_at DOUBLE PRECISION
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS events (
        seq BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        mission_id TEXT,
        run_id TEXT,
        type TEXT NOT NULL,
        payload TEXT,
        ts DOUBLE PRECISION NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_events_mission ON events(mission_id, seq)",
    """
    CREATE TABLE IF NOT EXISTS charges (
        id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        mission_id TEXT,
        run_id TEXT,
        model TEXT NOT NULL,
        input_tokens INTEGER NOT NULL,
        output_tokens INTEGER NOT NULL,
        cost_usd DOUBLE PRECISION NOT NULL,
        ts DOUBLE PRECISION NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_charges_mission ON charges(mission_id)",
    """
    CREATE TABLE IF NOT EXISTS daemon_lock (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        owner TEXT NOT NULL,
        heartbeat_at DOUBLE PRECISION NOT NULL
    )
    """,
]

_TABLES = ["events", "charges", "gates", "runs", "missions", "daemon_lock"]


def _j(value: dict[str, Any] | None) -> str | None:
    return json.dumps(value) if value is not None else None


def _uj(value: str | None) -> dict[str, Any]:
    return json.loads(value) if value else {}


class PostgresStore:
    """Async Postgres StateStore. Open with `open_postgres(url)`."""

    def __init__(self, conn: psycopg.AsyncConnection[DictRow]) -> None:
        self._conn = conn

    async def aclose(self) -> None:
        await self._conn.close()

    async def reset_for_tests(self) -> None:
        """Wipe domain tables (test isolation for the shared dev database)."""
        for table in _TABLES:
            async with self._conn.cursor() as cur:
                await cur.execute(f"TRUNCATE TABLE {table}")
        await self._conn.commit()

    # -- internal helpers ---------------------------------------------------
    async def _fetch_one(self, sql: str, params: tuple[Any, ...]) -> DictRow | None:
        async with self._conn.cursor() as cur:
            await cur.execute(sql, params)
            return await cur.fetchone()

    async def _fetch_all(self, sql: str, params: tuple[Any, ...]) -> list[DictRow]:
        async with self._conn.cursor() as cur:
            await cur.execute(sql, params)
            return list(await cur.fetchall())

    @staticmethod
    def _mission(row: DictRow) -> Mission:
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
    def _run(row: DictRow) -> Run:
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
    def _gate(row: DictRow) -> Gate:
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
        await self._conn.execute(
            "INSERT INTO missions (id, objective, repo_path, status, plan, result,"
            " created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
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
        await self._conn.commit()
        return mission

    async def get_mission(self, mission_id: str) -> Mission | None:
        row = await self._fetch_one("SELECT * FROM missions WHERE id = %s", (mission_id,))
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
            sets.append("status = %s")
            params.append(status.value)
        if plan is not None:
            sets.append("plan = %s")
            params.append(_j(plan))
        if result is not None:
            sets.append("result = %s")
            params.append(_j(result))
        if sets:
            sets.append("updated_at = %s")
            params.append(time.time())
            params.append(mission_id)
            await self._conn.execute(
                f"UPDATE missions SET {', '.join(sets)} WHERE id = %s", tuple(params)
            )
            await self._conn.commit()
        return await self.get_mission(mission_id)

    async def list_missions(
        self, status: MissionStatus | None = None, limit: int = 50
    ) -> list[Mission]:
        if status is not None:
            rows = await self._fetch_all(
                "SELECT * FROM missions WHERE status = %s ORDER BY created_at DESC LIMIT %s",
                (status.value, limit),
            )
        else:
            rows = await self._fetch_all(
                "SELECT * FROM missions ORDER BY created_at DESC LIMIT %s", (limit,)
            )
        return [self._mission(r) for r in rows]

    # -- runs -----------------------------------------------------------------
    async def create_run(self, run: Run) -> Run:
        """Idempotent on run.id: re-dispatch after a crash returns the stored row."""
        await self._conn.execute(
            "INSERT INTO runs (id, mission_id, role, step, attempt, status, model_ref,"
            " context, artifact, escalation, redirect, created_at, updated_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (id) DO NOTHING",
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
        await self._conn.commit()
        existing = await self.get_run(run.id)
        assert existing is not None
        return existing

    async def get_run(self, run_id: str) -> Run | None:
        row = await self._fetch_one("SELECT * FROM runs WHERE id = %s", (run_id,))
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
            sets.append("status = %s")
            params.append(status.value)
        if model_ref is not None:
            sets.append("model_ref = %s")
            params.append(_j(model_ref))
        if context is not None:
            sets.append("context = %s")
            params.append(_j(context))
        if artifact is not None:
            sets.append("artifact = %s")
            params.append(_j(artifact))
        if escalation is not None:
            sets.append("escalation = %s")
            params.append(_j(escalation))
        if redirect is not None:
            sets.append("redirect = %s")
            params.append(redirect)
        if sets:
            sets.append("updated_at = %s")
            params.append(time.time())
            params.append(run_id)
            await self._conn.execute(
                f"UPDATE runs SET {', '.join(sets)} WHERE id = %s", tuple(params)
            )
            await self._conn.commit()
        return await self.get_run(run_id)

    async def list_runs(self, mission_id: str) -> list[Run]:
        rows = await self._fetch_all(
            "SELECT * FROM runs WHERE mission_id = %s ORDER BY created_at", (mission_id,)
        )
        return [self._run(r) for r in rows]

    async def list_runs_by_status(self, status: RunStatus) -> list[Run]:
        rows = await self._fetch_all(
            "SELECT * FROM runs WHERE status = %s ORDER BY created_at", (status.value,)
        )
        return [self._run(r) for r in rows]

    # -- gates ------------------------------------------------------------------
    async def create_gate(self, gate: Gate) -> Gate:
        await self._conn.execute(
            "INSERT INTO gates (id, mission_id, kind, question, status, decision_note,"
            " created_at, decided_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
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
        await self._conn.commit()
        return gate

    async def get_gate(self, gate_id: str) -> Gate | None:
        row = await self._fetch_one("SELECT * FROM gates WHERE id = %s", (gate_id,))
        return self._gate(row) if row else None

    async def decide_gate(
        self, gate_id: str, approved: bool, note: str | None = None
    ) -> Gate | None:
        await self._conn.execute(
            "UPDATE gates SET status = %s, decision_note = %s, decided_at = %s"
            " WHERE id = %s AND status = %s",
            (
                GateStatus.APPROVED.value if approved else GateStatus.REJECTED.value,
                note,
                time.time(),
                gate_id,
                GateStatus.OPEN.value,
            ),
        )
        await self._conn.commit()
        return await self.get_gate(gate_id)

    async def list_open_gates(self, mission_id: str | None = None) -> list[Gate]:
        if mission_id is not None:
            rows = await self._fetch_all(
                "SELECT * FROM gates WHERE status = %s AND mission_id = %s ORDER BY created_at",
                (GateStatus.OPEN.value, mission_id),
            )
        else:
            rows = await self._fetch_all(
                "SELECT * FROM gates WHERE status = %s ORDER BY created_at",
                (GateStatus.OPEN.value,),
            )
        return [self._gate(r) for r in rows]

    # -- events ---------------------------------------------------------------------
    async def append_event(self, event: EventRecord) -> EventRecord:
        async with self._conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO events (mission_id, run_id, type, payload, ts)"
                " VALUES (%s, %s, %s, %s, %s) RETURNING seq",
                (
                    event.mission_id,
                    event.run_id,
                    event.type.value,
                    _j(event.payload) or "{}",
                    event.ts,
                ),
            )
            row = await cur.fetchone()
            assert row is not None
            event.seq = int(row["seq"])
        await self._conn.commit()
        return event

    async def events_since(
        self, seq: int = 0, mission_id: str | None = None, limit: int = 500
    ) -> list[EventRecord]:
        if mission_id is not None:
            rows = await self._fetch_all(
                "SELECT * FROM events WHERE seq > %s AND mission_id = %s ORDER BY seq LIMIT %s",
                (seq, mission_id, limit),
            )
        else:
            rows = await self._fetch_all(
                "SELECT * FROM events WHERE seq > %s ORDER BY seq LIMIT %s", (seq, limit)
            )
        return [
            EventRecord(
                seq=int(r["seq"]),
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
        await self._conn.execute(
            "INSERT INTO charges (mission_id, run_id, model, input_tokens, output_tokens,"
            " cost_usd, ts) VALUES (%s, %s, %s, %s, %s, %s, %s)",
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
        await self._conn.commit()

    async def mission_spend(self, mission_id: str) -> BudgetState:
        row = await self._fetch_one(
            "SELECT COALESCE(SUM(input_tokens), 0) AS i, COALESCE(SUM(output_tokens), 0) AS o,"
            " COALESCE(SUM(cost_usd), 0.0) AS c FROM charges WHERE mission_id = %s",
            (mission_id,),
        )
        assert row is not None
        return BudgetState(input_tokens=row["i"], output_tokens=row["o"], spent_usd=row["c"])

    # -- leader lock (row-based; advisory lock hardening lands M3) ------------------------
    async def acquire_lock(self, owner: str, stale_after_s: float = 60.0) -> bool:
        row = await self._fetch_one("SELECT owner, heartbeat_at FROM daemon_lock WHERE id = 1", ())
        if (
            row is not None
            and row["owner"] != owner
            and row["heartbeat_at"] > (time.time() - stale_after_s)
        ):
            return False  # live foreign lock
        await self._conn.execute(
            "INSERT INTO daemon_lock (id, owner, heartbeat_at) VALUES (1, %s, %s)"
            " ON CONFLICT (id) DO UPDATE SET owner = %s, heartbeat_at = %s",
            (owner, time.time(), owner, time.time()),
        )
        await self._conn.commit()
        return True

    async def heartbeat_lock(self, owner: str) -> bool:
        cur = await self._conn.execute(
            "UPDATE daemon_lock SET heartbeat_at = %s WHERE id = 1 AND owner = %s",
            (time.time(), owner),
        )
        await self._conn.commit()
        return cur.rowcount > 0

    async def release_lock(self, owner: str) -> None:
        await self._conn.execute("DELETE FROM daemon_lock WHERE id = 1 AND owner = %s", (owner,))
        await self._conn.commit()


async def open_postgres(url: str) -> PostgresStore:
    conn = await psycopg.AsyncConnection.connect(
        url, autocommit=False, row_factory=psycopg.rows.dict_row
    )
    for ddl in _DDL:
        await conn.execute(ddl)
    await conn.commit()
    return PostgresStore(conn)
