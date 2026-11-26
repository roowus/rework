"""Both state backends migrate from ONE list (kernel/state/migrations.py).

Two hand-kept copies are how a column went missing on one backend (the Library's
SQLite list lacked `vectors`; audit 2026-11-25, 3.8). This builds a database from
the OLDEST schema shape (none of the migrated columns), opens it, and checks
every listed column now exists and the schema version is recorded.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from rework.kernel.state.migrations import COLUMN_MIGRATIONS, SCHEMA_VERSION
from rework.kernel.state.sqlite import open_sqlite

OLD = {
    "missions": "id TEXT PRIMARY KEY, objective TEXT NOT NULL, repo_path TEXT, status TEXT"
    " NOT NULL, plan TEXT, result TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL",
    "charges": "mission_id TEXT, run_id TEXT, model TEXT NOT NULL, input_tokens INTEGER NOT"
    " NULL, output_tokens INTEGER NOT NULL, cost_usd REAL NOT NULL, ts REAL NOT NULL",
    "gates": "id TEXT PRIMARY KEY, mission_id TEXT NOT NULL, kind TEXT NOT NULL, question TEXT"
    " NOT NULL, status TEXT NOT NULL, decision_note TEXT, created_at REAL NOT NULL,"
    " decided_at REAL",
}


async def test_an_old_sqlite_database_gets_every_listed_column(tmp_path: Path) -> None:
    db = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(db)
    for table, cols in OLD.items():
        conn.execute(f"CREATE TABLE {table} ({cols})")
    conn.commit()
    conn.close()

    store = await open_sqlite(str(db))
    await store.aclose()

    conn = sqlite3.connect(db)
    try:
        for table, column, _ in COLUMN_MIGRATIONS:
            names = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            assert column in names, f"{table}.{column} was not migrated"
        (version,) = conn.execute("SELECT version FROM schema_version").fetchone()
        assert version == SCHEMA_VERSION
    finally:
        conn.close()


def test_every_migration_targets_a_table_the_ddl_creates() -> None:
    from rework.kernel.state.sqlite import _DDL

    for table, _, _ in COLUMN_MIGRATIONS:
        assert f"CREATE TABLE IF NOT EXISTS {table} (" in _DDL, table
