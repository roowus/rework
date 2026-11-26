"""ONE list of additive column migrations, shared by both state backends.

Each backend used to keep its own hand-written check-then-alter list (SQLite as
a chain of `if "x" not in names`, Postgres as a tuple list). The two copies are
how drift happens: the Library's SQLite list silently lacked `vectors` for
weeks (audit 2026-11-25, 3.8). A column is added HERE, once, and the
conformance suite checks that both backends end with every column listed.

The DDL fragment must be valid on both SQLite and Postgres (TEXT / INTEGER /
constant defaults are). SCHEMA_VERSION is the length of the list: a database
records the version it was migrated to, so a client can tell that it is older
than the schema it is talking to.
"""

from __future__ import annotations

# (table, column, column definition): ALTER TABLE <table> ADD COLUMN <column> <definition>
COLUMN_MIGRATIONS: list[tuple[str, str, str]] = [
    ("charges", "cached_tokens", "INTEGER NOT NULL DEFAULT 0"),
    ("missions", "repo_origin", "TEXT"),
    ("missions", "project_id", "TEXT"),
    ("missions", "chat_id", "TEXT"),
    ("missions", "title", "TEXT"),
    ("missions", "priority", "TEXT NOT NULL DEFAULT 'normal'"),
    ("missions", "transient_crash_count", "INTEGER NOT NULL DEFAULT 0"),
    ("gates", "for_run", "TEXT"),
    ("gates", "decision", "TEXT"),
    ("gates", "spec", "TEXT"),
]

SCHEMA_VERSION = len(COLUMN_MIGRATIONS)


def alter_sql(table: str, column: str, definition: str) -> str:
    return f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
