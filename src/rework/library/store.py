"""Library storage: index tables (this module) + body files in a git repo
(GitLibrary below). The index is the search path; the git repo is provenance.

The LibraryStore is its own seam (separate connections from StateStore) with
the same check-then-alter migration discipline (ADR 0005's lock-convoy lesson).
Both impls must pass tests/library/test_library_store.py.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from rework.library.model import (
    ItemStatus,
    ItemType,
    LibraryItem,
    Provenance,
)

_DDL = [
    """
    CREATE TABLE IF NOT EXISTS library_items (
        id TEXT PRIMARY KEY,
        type TEXT NOT NULL,
        name TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        body_path TEXT,
        tags TEXT NOT NULL DEFAULT '[]',
        provenance TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL,
        version INTEGER NOT NULL DEFAULT 1,
        uses INTEGER NOT NULL DEFAULT 0,
        created_at DOUBLE PRECISION NOT NULL,
        updated_at DOUBLE PRECISION NOT NULL,
        vectors TEXT NOT NULL DEFAULT '{}'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_library_type_status ON library_items(type, status)",
]

# sqlite equivalent of DOUBLE PRECISION: REAL (only the type words differ)
_SQLITE_DDL = [ddl.replace("DOUBLE PRECISION", "REAL") for ddl in _DDL]

# (table, column, DDL): check-then-alter (the ADR 0005 lock-convoy rule)
_MIGRATIONS: list[tuple[str, str, str]] = [
    (
        "library_items",
        "vectors",
        "ALTER TABLE library_items ADD COLUMN vectors TEXT NOT NULL DEFAULT '{}'",
    ),
]


def _j(value: Any) -> str:
    return json.dumps(value)


def _row_to_item(row: Any) -> LibraryItem:
    prov = row["provenance"] or "{}"
    try:
        raw_vectors = row["vectors"]
    except (KeyError, IndexError):
        raw_vectors = None
    vectors = json.loads(raw_vectors) if raw_vectors else {}
    item = LibraryItem(
        id=row["id"],
        type=ItemType(row["type"]),
        name=row["name"],
        description=row["description"],
        body_path=row["body_path"],
        tags=json.loads(row["tags"] or "[]"),
        provenance=Provenance(**json.loads(prov)),
        status=ItemStatus(row["status"]),
        version=int(row["version"]),
        uses=int(row["uses"]),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        vectors=vectors,
    )
    return item


@runtime_checkable
class LibraryStore(Protocol):
    async def upsert_item(self, item: LibraryItem) -> LibraryItem: ...

    async def get_item(self, item_id: str) -> LibraryItem | None: ...

    async def list_items(
        self,
        type_: ItemType | None = None,
        status: ItemStatus | None = None,
        limit: int = 100,
    ) -> list[LibraryItem]: ...

    async def search(
        self,
        query: str,
        type_: ItemType | None = None,
        limit: int = 20,
    ) -> list[LibraryItem]:
        """Keyword search over name/description/tags, best-first. MVP:
        substring matching with a simple score; pgvector hybrid ranking at M6
        behind this same interface."""

    async def record_use(self, item_id: str) -> None: ...

    async def aclose(self) -> None: ...


def _terms(query: str) -> list[str]:
    """Agents search in natural language ('toyrepo planted bug'), so a query
    is a bag of terms, not one substring (found live: the coder's multi-word
    searches matched nothing under whole-query substring rules)."""
    return [t for t in query.lower().split() if t]


def _score(item: LibraryItem, query: str) -> int:
    score = 0
    for q in _terms(query):
        if item.name.lower() == q:
            score += 100
        elif item.name.lower().startswith(q):
            score += 50
        elif q in item.name.lower():
            score += 25
        if q in item.description.lower():
            score += 10
        if any(q in t.lower() for t in item.tags):
            score += 15
    return score


def _matches(item: LibraryItem, query: str) -> bool:
    terms = _terms(query)
    if not terms:
        return False
    return any(
        q in item.name.lower()
        or q in item.description.lower()
        or any(q in t.lower() for t in item.tags)
        for q in terms
    )


class SqliteLibraryStore:
    """MVP/demo-grade library index on the state sqlite file."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def aclose(self) -> None:
        await self._conn.close()

    async def upsert_item(self, item: LibraryItem) -> LibraryItem:
        from rework.library.model import item_to_row

        r = item_to_row(item)
        await self._conn.execute(
            "INSERT INTO library_items (id, type, name, description, body_path, tags,"
            " provenance, status, version, uses, created_at, updated_at, vectors)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET"
            " type=excluded.type, name=excluded.name, description=excluded.description,"
            " body_path=excluded.body_path, tags=excluded.tags, provenance=excluded.provenance,"
            " status=excluded.status, version=excluded.version, uses=excluded.uses,"
            " updated_at=excluded.updated_at, vectors=excluded.vectors",
            (
                r["id"],
                r["type"],
                r["name"],
                r["description"],
                r["body_path"],
                _j(r["tags"]),
                _j(r["provenance"]),
                r["status"],
                r["version"],
                r["uses"],
                r["created_at"],
                r["updated_at"],
                _j(item.vectors) or "{}",
            ),
        )
        await self._conn.commit()
        return item

    async def get_item(self, item_id: str) -> LibraryItem | None:
        async with self._conn.execute(
            "SELECT * FROM library_items WHERE id = ?", (item_id,)
        ) as cur:
            row = await cur.fetchone()
        return _row_to_item(row) if row else None

    async def list_items(
        self,
        type_: ItemType | None = None,
        status: ItemStatus | None = None,
        limit: int = 100,
    ) -> list[LibraryItem]:
        sql = "SELECT * FROM library_items"
        clauses: list[str] = []
        params: list[Any] = []
        if type_ is not None:
            clauses.append("type = ?")
            params.append(type_.value)
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        async with self._conn.execute(sql, tuple(params)) as cur:
            rows = list(await cur.fetchall())
        return [_row_to_item(r) for r in rows]

    async def search(
        self, query: str, type_: ItemType | None = None, limit: int = 20
    ) -> list[LibraryItem]:
        # MVP: fetch candidates (LIKE narrows little that matters at this
        # scale), score in Python, sort best-first. ACTIVE only: search is a
        # serving path; proposed/retired items never surface (curation).
        items = [
            i for i in await self.list_items(type_=type_, status=ItemStatus.ACTIVE, limit=1000)
        ]
        hits = [(_score(i, query), i) for i in items if _matches(i, query)]
        hits.sort(key=lambda pair: (-pair[0], pair[1].name))
        return [i for _, i in hits[:limit]]

    async def record_use(self, item_id: str) -> None:
        await self._conn.execute(
            "UPDATE library_items SET uses = uses + 1 WHERE id = ?", (item_id,)
        )
        await self._conn.commit()


class PostgresLibraryStore:
    """Deployment-grade library index; same contract as the sqlite impl."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self._op_lock = _LibLock(conn)

    async def aclose(self) -> None:
        await self._conn.close()

    async def upsert_item(self, item: LibraryItem) -> LibraryItem:
        async with self._op_lock:
            from rework.library.model import item_to_row

            r = item_to_row(item)
            await self._conn.execute(
                "INSERT INTO library_items (id, type, name, description, body_path,"
                " tags, provenance, status, version, uses, created_at, updated_at, vectors)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (id) DO UPDATE SET"
                " type=EXCLUDED.type, name=EXCLUDED.name, description=EXCLUDED.description,"
                " body_path=EXCLUDED.body_path, tags=EXCLUDED.tags,"
                " provenance=EXCLUDED.provenance, status=EXCLUDED.status,"
                " version=EXCLUDED.version, uses=EXCLUDED.uses,"
                " updated_at=EXCLUDED.updated_at, vectors=EXCLUDED.vectors",
                (
                    r["id"],
                    r["type"],
                    r["name"],
                    r["description"],
                    r["body_path"],
                    _j(r["tags"]),
                    _j(r["provenance"]),
                    r["status"],
                    r["version"],
                    r["uses"],
                    r["created_at"],
                    r["updated_at"],
                    _j(item.vectors) or "{}",
                ),
            )
            await self._conn.commit()
        return item

    async def get_item(self, item_id: str) -> LibraryItem | None:
        async with self._op_lock:
            async with self._conn.cursor() as cur:
                await cur.execute("SELECT * FROM library_items WHERE id = %s", (item_id,))
                row = await cur.fetchone()
        return _row_to_item(row) if row else None

    async def list_items(
        self,
        type_: ItemType | None = None,
        status: ItemStatus | None = None,
        limit: int = 100,
    ) -> list[LibraryItem]:
        async with self._op_lock:
            sql = "SELECT * FROM library_items"
            clauses: list[str] = []
            params: list[Any] = []
            if type_ is not None:
                clauses.append("type = %s")
                params.append(type_.value)
            if status is not None:
                clauses.append("status = %s")
                params.append(status.value)
            if clauses:
                sql += " WHERE " + " AND ".join(clauses)
            sql += " ORDER BY created_at DESC LIMIT %s"
            params.append(limit)
            async with self._conn.cursor() as cur:
                await cur.execute(sql, tuple(params))
                rows = list(await cur.fetchall())
        return [_row_to_item(r) for r in rows]

    async def search(
        self, query: str, type_: ItemType | None = None, limit: int = 20
    ) -> list[LibraryItem]:
        # serving path: ACTIVE only (see the sqlite impl's note)
        items = [
            i for i in await self.list_items(type_=type_, status=ItemStatus.ACTIVE, limit=1000)
        ]
        hits = [(_score(i, query), i) for i in items if _matches(i, query)]
        hits.sort(key=lambda pair: (-pair[0], pair[1].name))
        return [i for _, i in hits[:limit]]

    async def record_use(self, item_id: str) -> None:
        async with self._op_lock:
            await self._conn.execute(
                "UPDATE library_items SET uses = uses + 1 WHERE id = %s", (item_id,)
            )
            await self._conn.commit()


class _LibLock:
    """Single-connection serialization (same rationale as StateStore's
    _TaskLock for postgres; sqlite's thread serializes on its own), and
    transaction hygiene on the way out.

    FOUND LIVE (prod rewvis): every Library read was a bare SELECT with no
    commit, so the TUI's library connection sat "idle in transaction" for
    hours (1h38m observed), the lock pattern that once blocked deploy
    migrations. Leaving every operation committed bounds that to one call."""

    def __init__(self, conn: Any = None) -> None:
        import asyncio

        self._lock = asyncio.Lock()
        self._conn = conn

    async def __aenter__(self) -> None:
        await self._lock.acquire()

    async def __aexit__(self, *exc: object) -> None:
        try:
            if self._conn is not None:
                from rework.kernel.state.postgres import end_idle_transaction

                await end_idle_transaction(self._conn)
        finally:
            self._lock.release()


async def apply_library_migrations(conn: Any) -> None:
    """Check-then-alter for databases created before a column existed (the
    ADR 0005 lock-convoy rule: cheap catalog read, ALTER only when missing)."""
    for table, column, alter in _MIGRATIONS:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT 1 FROM information_schema.columns"
                " WHERE table_name = %s AND column_name = %s",
                (table, column),
            )
            missing = await cur.fetchone() is None
        if missing:
            await conn.execute("SET lock_timeout = '5s'")
            try:
                await conn.execute(alter)
            finally:
                await conn.execute("SET lock_timeout = 0")


async def open_library_store(db: str) -> LibraryStore:
    """Open the library index on the same database substrate as the state
    store (sqlite path or postgresql:// URL)."""
    if db.startswith(("postgresql://", "postgres://")):
        import psycopg
        from psycopg.rows import dict_row

        conn = await psycopg.AsyncConnection.connect(db, autocommit=False, row_factory=dict_row)
        for ddl in _DDL:
            await conn.execute(ddl)
        await apply_library_migrations(conn)
        await conn.commit()
        return PostgresLibraryStore(conn)
    import aiosqlite

    def _prepare() -> str:
        path = Path(db).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        return str(path)

    sqlite_conn = await aiosqlite.connect(await asyncio.to_thread(_prepare))
    sqlite_conn.row_factory = aiosqlite.Row
    for ddl in _SQLITE_DDL:
        await sqlite_conn.execute(ddl)
    await sqlite_conn.commit()
    return SqliteLibraryStore(sqlite_conn)


class GitLibrary:
    """The body-file side: markdown files under <root>/<type>s/, committed to
    a local git repo. rework's API is the only git writer (owner decision):
    every mutation goes through write_body/update_body, which commit."""

    def __init__(
        self, root: Path, author: tuple[str, str] = ("rework", "rework@rework.local")
    ) -> None:
        self.root = root
        self._author = author

    def _git(self, *args: str) -> None:
        import subprocess

        subprocess.run(
            ["git", *args],
            cwd=self.root,
            check=True,
            capture_output=True,
        )

    def ensure_repo(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if not (self.root / ".git").exists():
            self._git("init", "-b", "main")

    def _slug(self, name: str) -> str:
        keep = "".join(c if c.isalnum() or c in "-_" else "-" for c in name.lower())
        return keep.strip("-") or "item"

    def body_path_for(self, item: LibraryItem) -> str:
        return f"{item.type.value}s/{self._slug(item.name)}.md"

    def write_body(self, item: LibraryItem, body: str, message: str) -> str:
        """Write the body file, stage + commit, return the repo-relative path.
        Sync by design; callers wrap in to_thread."""
        self.ensure_repo()
        rel = self.body_path_for(item)
        dest = self.root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(body)
        self._git("add", rel)
        self._git(
            "-c",
            f"user.name={self._author[0]}",
            "-c",
            f"user.email={self._author[1]}",
            "commit",
            "-m",
            message,
            "--allow-empty",
        )
        return rel

    def read_body(self, item: LibraryItem) -> str | None:
        if not item.body_path:
            return None
        path = self.root / item.body_path
        if not path.is_file():
            return None
        return path.read_text()

    def last_commit(self, item: LibraryItem) -> str | None:
        if not item.body_path:
            return None
        import subprocess

        proc = subprocess.run(
            ["git", "log", "-1", "--format=%h %s", "--", item.body_path],
            cwd=self.root,
            capture_output=True,
            text=True,
        )
        return proc.stdout.strip() or None
