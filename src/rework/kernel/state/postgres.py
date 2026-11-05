"""Postgres StateStore: the deployment backend.

Same contract as SqliteStore; both must pass tests/conformance/test_store.py.
Checkpointer tables (langgraph-checkpoint-postgres) live in the same database
but are owned by the checkpointer, not this store.
"""

from __future__ import annotations

import asyncio
import functools
import json
import time
from typing import Any

import psycopg
from psycopg.rows import DictRow

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
    GateStatus,
    Mission,
    MissionStatus,
    Project,
    Run,
    RunStatus,
)

_DDL = [
    """
    CREATE TABLE IF NOT EXISTS missions (
        id TEXT PRIMARY KEY,
        objective TEXT NOT NULL,
        repo_path TEXT,
        repo_origin TEXT,
        project_id TEXT,
        status TEXT NOT NULL,
        plan TEXT,
        result TEXT,
        created_at DOUBLE PRECISION NOT NULL,
        updated_at DOUBLE PRECISION NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS projects (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        settings TEXT NOT NULL DEFAULT '{}',
        created_at DOUBLE PRECISION NOT NULL
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
        for_run TEXT,
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
        cached_tokens INTEGER NOT NULL DEFAULT 0,
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
    """
    CREATE TABLE IF NOT EXISTS complaints (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        source_mission_id TEXT,
        source_run_id TEXT,
        source_role TEXT,
        summary TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT '',
        evidence TEXT NOT NULL DEFAULT '[]',
        suggestion TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL,
        created_at DOUBLE PRECISION NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_complaints_status ON complaints(status)",
]

_TABLES = [
    "events",
    "charges",
    "gates",
    "runs",
    "projects",
    "missions",
    "daemon_lock",
    "complaints",
]


def _j(value: dict[str, Any] | None) -> str | None:
    return json.dumps(value) if value is not None else None


def _uj(value: str | None) -> dict[str, Any]:
    return json.loads(value) if value else {}


class _TaskLock:
    """asyncio.Lock, reentrant within one task. PostgresStore shares ONE
    connection (found live: the engine's concurrent pump loop + role tasks
    interleaved queries -> 'another command is already in progress'; aiosqlite
    never showed this because its thread serializes). Public methods hold this
    lock for their whole body; nested public calls (update_run -> get_run)
    stay in the same task, so reentrancy is safe here."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task[None] | None = None

    async def __aenter__(self) -> None:
        me = asyncio.current_task()
        if self._owner is me:
            return
        await self._lock.acquire()
        self._owner = me

    async def __aexit__(self, *exc: object) -> None:
        if self._owner is asyncio.current_task():
            self._owner = None
            self._lock.release()


def _serialized(method: Any) -> Any:
    """Serialize a store method against the shared connection, and always
    leave it in a committed state.

    FOUND LIVE (prod rewvis): a connection whose last statement was a plain
    SELECT sits "idle in transaction" until someone commits or rolls back. Every
    read method here is a SELECT with no commit, so the daemon held a
    transaction open indefinitely (observed: 1h51m) and its locks blocked
    ALTER TABLE on deploy ("migration could not lock gates within 5s"). Closing
    the transaction after each call bounds the exposure to one operation.
    Writers already commit inside; this is a no-op for them (psycopg returns a
    fresh transaction only when the current one is not idle)."""

    @functools.wraps(method)
    async def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        async with self._op_lock:
            try:
                return await method(self, *args, **kwargs)
            finally:
                await self._end_idle_transaction()

    return wrapper


class PostgresStore:
    """Async Postgres StateStore. Open with `open_postgres(url)`."""

    def __init__(self, conn: psycopg.AsyncConnection[DictRow]) -> None:
        self._conn = conn
        self._op_lock = _TaskLock()

    async def _end_idle_transaction(self) -> None:
        """Commit (or discard) whatever the last statement opened.

        psycopg's connection exposes the transaction status; only touch the
        connection when a transaction is genuinely open, so a failed method
        does not turn its own error into a second one."""
        info = getattr(self._conn, "info", None)
        if info is None:
            return
        txn = getattr(info, "transaction_status", None)
        from psycopg.pq import TransactionStatus

        try:
            if txn == TransactionStatus.INTRANS:
                await self._conn.commit()
            elif txn == TransactionStatus.INERROR:
                await self._conn.rollback()
        except Exception:  # never let hygiene mask the caller's result
            pass

    async def aclose(self) -> None:
        await self._conn.close()

    @_serialized
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
            repo_origin=row["repo_origin"],
            project_id=row["project_id"],
            status=MissionStatus(row["status"]),
            plan=_uj(row["plan"]) or None,
            result=_uj(row["result"]) or None,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _project(row: DictRow) -> Project:
        return Project(
            id=row["id"],
            name=row["name"],
            settings=_uj(row["settings"]),
            created_at=row["created_at"],
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
            for_run=row["for_run"],
            created_at=row["created_at"],
            decided_at=row["decided_at"],
        )

    # -- missions -----------------------------------------------------------
    @_serialized
    async def create_mission(self, mission: Mission) -> Mission:
        await self._conn.execute(
            "INSERT INTO missions (id, objective, repo_path, repo_origin, project_id,"
            " status, plan, result, created_at, updated_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                mission.id,
                mission.objective,
                mission.repo_path,
                mission.repo_origin,
                mission.project_id,
                mission.status.value,
                _j(mission.plan),
                _j(mission.result),
                mission.created_at,
                mission.updated_at,
            ),
        )
        await self._conn.commit()
        return mission

    @_serialized
    async def get_mission(self, mission_id: str) -> Mission | None:
        row = await self._fetch_one("SELECT * FROM missions WHERE id = %s", (mission_id,))
        return self._mission(row) if row else None

    @_serialized
    async def update_mission(
        self,
        mission_id: str,
        *,
        status: MissionStatus | None = None,
        plan: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        repo_path: str | None = None,
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
        if repo_path is not None:
            sets.append("repo_path = %s")
            params.append(repo_path)
        if sets:
            sets.append("updated_at = %s")
            params.append(time.time())
            params.append(mission_id)
            await self._conn.execute(
                f"UPDATE missions SET {', '.join(sets)} WHERE id = %s", tuple(params)
            )
            await self._conn.commit()
        return await self.get_mission(mission_id)

    @_serialized
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

    # -- projects (ADR 0005) --------------------------------------------------
    @_serialized
    async def create_project(self, project: Project) -> Project:
        await self._conn.execute(
            "INSERT INTO projects (id, name, settings, created_at) VALUES (%s, %s, %s, %s)",
            (project.id, project.name, _j(project.settings) or "{}", project.created_at),
        )
        await self._conn.commit()
        return project

    @_serialized
    async def get_project(self, project_id: str) -> Project | None:
        row = await self._fetch_one("SELECT * FROM projects WHERE id = %s", (project_id,))
        return self._project(row) if row else None

    @_serialized
    async def update_project(
        self, project_id: str, *, name: str | None = None, settings: dict[str, Any] | None = None
    ) -> Project | None:
        sets: list[str] = []
        params: list[Any] = []
        if name is not None:
            sets.append("name = %s")
            params.append(name)
        if settings is not None:
            sets.append("settings = %s")
            params.append(_j(settings) or "{}")
        if sets:
            params.append(project_id)
            await self._conn.execute(
                f"UPDATE projects SET {', '.join(sets)} WHERE id = %s", tuple(params)
            )
            await self._conn.commit()
        return await self.get_project(project_id)

    @_serialized
    async def list_projects(self, limit: int = 100) -> list[Project]:
        rows = await self._fetch_all(
            "SELECT * FROM projects ORDER BY created_at DESC LIMIT %s", (limit,)
        )
        return [self._project(r) for r in rows]

    # -- runs -----------------------------------------------------------------
    @_serialized
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

    @_serialized
    async def get_run(self, run_id: str) -> Run | None:
        row = await self._fetch_one("SELECT * FROM runs WHERE id = %s", (run_id,))
        return self._run(row) if row else None

    @_serialized
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

    @_serialized
    async def list_runs(self, mission_id: str) -> list[Run]:
        rows = await self._fetch_all(
            "SELECT * FROM runs WHERE mission_id = %s ORDER BY created_at", (mission_id,)
        )
        return [self._run(r) for r in rows]

    @_serialized
    async def list_runs_by_status(self, status: RunStatus) -> list[Run]:
        rows = await self._fetch_all(
            "SELECT * FROM runs WHERE status = %s ORDER BY created_at", (status.value,)
        )
        return [self._run(r) for r in rows]

    # -- gates ------------------------------------------------------------------
    @_serialized
    async def create_gate(self, gate: Gate) -> Gate:
        await self._conn.execute(
            "INSERT INTO gates (id, mission_id, kind, question, status, decision_note,"
            " for_run, created_at, decided_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                gate.id,
                gate.mission_id,
                gate.kind.value,
                gate.question,
                gate.status.value,
                gate.decision_note,
                gate.for_run,
                gate.created_at,
                gate.decided_at,
            ),
        )
        await self._conn.commit()
        return gate

    @_serialized
    async def get_gate(self, gate_id: str) -> Gate | None:
        row = await self._fetch_one("SELECT * FROM gates WHERE id = %s", (gate_id,))
        return self._gate(row) if row else None

    @_serialized
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

    @_serialized
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

    @_serialized
    async def list_gates(self, mission_id: str | None = None) -> list[Gate]:
        """Every gate for the mission, any status, oldest first."""
        if mission_id is not None:
            rows = await self._fetch_all(
                "SELECT * FROM gates WHERE mission_id = %s ORDER BY created_at",
                (mission_id,),
            )
        else:
            rows = await self._fetch_all("SELECT * FROM gates ORDER BY created_at", ())
        return [self._gate(r) for r in rows]

    # -- events ---------------------------------------------------------------------
    @_serialized
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

    @_serialized
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
    @_serialized
    async def charge(self, charge: Charge) -> None:
        await self._conn.execute(
            "INSERT INTO charges (mission_id, run_id, model, input_tokens, output_tokens,"
            " cached_tokens, cost_usd, ts) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                charge.mission_id,
                charge.run_id,
                charge.model,
                charge.input_tokens,
                charge.output_tokens,
                charge.cached_tokens,
                charge.cost_usd,
                charge.ts,
            ),
        )
        await self._conn.commit()

    @_serialized
    async def run_charges(self, run_id: str) -> list[Charge]:
        rows = await self._fetch_all(
            "SELECT * FROM charges WHERE run_id = %s ORDER BY ts", (run_id,)
        )
        return [
            Charge(
                mission_id=r["mission_id"],
                run_id=r["run_id"],
                model=r["model"],
                input_tokens=r["input_tokens"],
                output_tokens=r["output_tokens"],
                cached_tokens=r["cached_tokens"],
                cost_usd=r["cost_usd"],
                ts=r["ts"],
            )
            for r in rows
        ]

    @_serialized
    async def mission_spend(self, mission_id: str) -> BudgetState:
        row = await self._fetch_one(
            "SELECT COALESCE(SUM(input_tokens), 0) AS i, COALESCE(SUM(output_tokens), 0) AS o,"
            " COALESCE(SUM(cached_tokens), 0) AS ch, COALESCE(SUM(cost_usd), 0.0) AS c"
            " FROM charges WHERE mission_id = %s",
            (mission_id,),
        )
        assert row is not None
        return BudgetState(
            input_tokens=row["i"],
            output_tokens=row["o"],
            cached_tokens=row["ch"],
            spent_usd=row["c"],
        )

    # -- leader lock (atomic upsert: the read-then-write version let two daemons
    # both pass the stale check and both win; M3 hardening) ------------------------------
    @_serialized
    async def acquire_lock(self, owner: str, stale_after_s: float = 60.0) -> bool:
        now = time.time()
        cur = await self._conn.execute(
            "INSERT INTO daemon_lock (id, owner, heartbeat_at) VALUES (1, %s, %s)"
            " ON CONFLICT (id) DO UPDATE SET"
            " owner = EXCLUDED.owner, heartbeat_at = EXCLUDED.heartbeat_at"
            " WHERE daemon_lock.owner = EXCLUDED.owner"
            "    OR daemon_lock.heartbeat_at <= %s",
            (owner, now, now - stale_after_s),
        )
        await self._conn.commit()
        return cur.rowcount > 0

    @_serialized
    async def heartbeat_lock(self, owner: str) -> bool:
        cur = await self._conn.execute(
            "UPDATE daemon_lock SET heartbeat_at = %s WHERE id = 1 AND owner = %s",
            (time.time(), owner),
        )
        await self._conn.commit()
        return cur.rowcount > 0

    @_serialized
    async def release_lock(self, owner: str) -> None:
        await self._conn.execute("DELETE FROM daemon_lock WHERE id = 1 AND owner = %s", (owner,))
        await self._conn.commit()

    # -- complaints inbox (ADR 0004) ------------------------------------------------
    @_serialized
    async def file_complaint(self, complaint: Complaint) -> Complaint:
        await self._conn.execute(
            "INSERT INTO complaints"
            " (id, kind, source_mission_id, source_run_id, source_role, summary, detail,"
            "  evidence, suggestion, status, created_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (id) DO UPDATE SET status = EXCLUDED.status",
            (
                complaint.id,
                complaint.kind.value,
                complaint.source_mission_id,
                complaint.source_run_id,
                complaint.source_role,
                complaint.summary,
                complaint.detail,
                json.dumps(complaint.evidence),
                complaint.suggestion,
                complaint.status.value,
                complaint.created_at,
            ),
        )
        await self._conn.commit()
        return complaint

    @_serialized
    async def list_complaints(
        self,
        status: ComplaintStatus | None = None,
        kind: ComplaintKind | None = None,
        limit: int = 100,
    ) -> list[Complaint]:
        sql = "SELECT * FROM complaints"
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            clauses.append("status = %s")
            params.append(status.value)
        if kind is not None:
            clauses.append("kind = %s")
            params.append(kind.value)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT %s"
        params.append(limit)
        return [self._complaint(row) for row in await self._fetch_all(sql, tuple(params))]

    @_serialized
    async def update_complaint(
        self, complaint_id: str, *, status: ComplaintStatus | None = None
    ) -> Complaint | None:
        if status is None:
            return await self.get_complaint(complaint_id)
        await self._conn.execute(
            "UPDATE complaints SET status = %s WHERE id = %s", (status.value, complaint_id)
        )
        await self._conn.commit()
        return await self.get_complaint(complaint_id)

    @_serialized
    async def get_complaint(self, complaint_id: str) -> Complaint | None:
        row = await self._fetch_one("SELECT * FROM complaints WHERE id = %s", (complaint_id,))
        return self._complaint(row) if row is not None else None

    @staticmethod
    def _complaint(row: DictRow) -> Complaint:
        return Complaint(
            id=row["id"],
            kind=ComplaintKind(row["kind"]),
            source_mission_id=row["source_mission_id"],
            source_run_id=row["source_run_id"],
            source_role=row["source_role"],
            summary=row["summary"],
            detail=row["detail"],
            evidence=json.loads(row["evidence"]),
            suggestion=row["suggestion"],
            status=ComplaintStatus(row["status"]),
            created_at=row["created_at"],
        )


_MIGRATIONS: list[tuple[str, str, str]] = [
    # (table, column, DDL): check-then-alter. ALTER TABLE takes ACCESS
    # EXCLUSIVE even as a no-op IF NOT EXISTS, so issuing one on every open
    # convoyed every CLI/daemon connection behind any long-lived reader
    # (found live: an attach process idle-in-transaction froze all store
    # opens for minutes). The catalog read is cheap and takes no table lock;
    # the ALTER runs only when a column is genuinely missing, under a short
    # lock_timeout so a deploy colliding with a live reader fails loudly
    # instead of wedging the queue.
    (
        "charges",
        "cached_tokens",
        "ALTER TABLE charges ADD COLUMN cached_tokens INTEGER NOT NULL DEFAULT 0",
    ),
    ("missions", "repo_origin", "ALTER TABLE missions ADD COLUMN repo_origin TEXT"),
    ("missions", "project_id", "ALTER TABLE missions ADD COLUMN project_id TEXT"),
    ("gates", "for_run", "ALTER TABLE gates ADD COLUMN for_run TEXT"),
]


async def open_postgres(url: str) -> PostgresStore:
    conn = await psycopg.AsyncConnection.connect(
        url, autocommit=False, row_factory=psycopg.rows.dict_row
    )
    try:
        for ddl in _DDL:
            await conn.execute(ddl)
        for table, column, alter in _MIGRATIONS:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT 1 FROM information_schema.columns"
                    " WHERE table_name = %s AND column_name = %s",
                    (table, column),
                )
                missing = await cur.fetchone() is None
            if not missing:
                continue
            # each migration is its own transaction: a LockNotAvailable aborts
            # the current one, so continuing in it would fail on every later
            # statement (and did: the cleanup `SET lock_timeout = 0` inside the
            # aborted txn raised InFailedSqlTransaction and masked the real
            # error). Commit each successful ALTER, roll back each failure.
            await conn.execute("SET LOCAL lock_timeout = '5s'")
            try:
                await conn.execute(alter)
                await conn.commit()
            except psycopg.errors.LockNotAvailable:
                await conn.rollback()
                blockers = await _lock_blockers(conn, table)
                raise RuntimeError(
                    f"migration could not lock {table} within 5s; something holds"
                    f" a lock on it: {blockers}. Stop the daemon (systemctl stop"
                    " rework) and retry, or kill the listed pids."
                ) from None
            except Exception:
                await conn.rollback()
                raise
        await conn.commit()
    except BaseException:
        await conn.close()  # never leak a half-migrated connection
        raise
    return PostgresStore(conn)


async def _lock_blockers(conn: psycopg.AsyncConnection[Any], table: str) -> str:
    """A short, actionable description of who is holding locks on `table`."""
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT pid, state, coalesce(client_addr::text, 'local') AS client,"
                " left(query, 60) AS q FROM pg_stat_activity"
                " WHERE datname = current_database() AND pid <> pg_backend_pid()"
                " AND state <> 'idle' ORDER BY xact_start NULLS LAST LIMIT 5"
            )
            rows = await cur.fetchall()
        await conn.rollback()
    except Exception:
        return "(could not inspect pg_stat_activity)"
    if not rows:
        return "no active sessions found; try again"
    return "; ".join(
        f"pid {r['pid']} {r['state']} from {r['client']}: {r['q']}"
        if isinstance(r, dict)
        else str(tuple(r))
        for r in rows
    )
