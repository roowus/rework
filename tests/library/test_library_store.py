"""LibraryStore conformance — both backends must pass this suite unchanged."""

from __future__ import annotations

import asyncio

import pytest

from rework.library.curation import backfill_bodies, contribute
from rework.library.model import (
    ItemStatus,
    ItemType,
    LibraryItem,
    Origin,
    Provenance,
)
from rework.library.store import GitLibrary, open_library_store, read_item_body


def _item(name: str = "toyrepo fix recipe", **kw) -> LibraryItem:
    return LibraryItem(
        type=kw.get("type", ItemType.SKILL),
        name=name,
        description=kw.get("description", "how to fix a planted bug in the toyrepo"),
        tags=kw.get("tags", []),
        provenance=kw.get(
            "provenance", Provenance(origin=Origin.INTERNAL, mission_id="m-abc", role="coder")
        ),
        status=kw.get("status", ItemStatus.ACTIVE),
    )


async def test_roundtrip_and_update(library_index) -> None:
    item = _item(tags=["toyrepo", "pytest"])
    stored = await library_index.upsert_item(item)
    assert stored.id == item.id

    got = await library_index.get_item(item.id)
    assert got is not None
    assert got.name == "toyrepo fix recipe"
    assert got.provenance.mission_id == "m-abc"
    assert got.tags == ["toyrepo", "pytest"]
    assert got.status == ItemStatus.ACTIVE
    assert await library_index.get_item("l-missing") is None

    # update: bump status + version, keep identity
    item.status = ItemStatus.RETIRED
    item.touch()
    await library_index.upsert_item(item)
    got2 = await library_index.get_item(item.id)
    assert got2 is not None
    assert got2.status == ItemStatus.RETIRED
    assert got2.version == 2


async def test_list_and_filters(library_index) -> None:
    await library_index.upsert_item(_item("skill one"))
    await library_index.upsert_item(
        _item("tool one", type=ItemType.TOOL, status=ItemStatus.PROPOSED)
    )
    await library_index.upsert_item(_item("skill two"))

    active = await library_index.list_items(status=ItemStatus.ACTIVE)
    assert {i.name for i in active} == {"skill one", "skill two"}
    tools = await library_index.list_items(type_=ItemType.TOOL)
    assert [i.name for i in tools] == ["tool one"]
    everything = await library_index.list_items()
    assert len(everything) == 3


async def test_search_ranking(library_index) -> None:
    await library_index.upsert_item(_item("toyrepo fix recipe", tags=["toyrepo"]))
    await library_index.upsert_item(
        _item("drive fetcher", description="pull files from google drive")
    )
    await library_index.upsert_item(
        _item(
            "pytest patterns",
            description="collection of pytest idioms for small repos",
            tags=["pytest", "testing"],
        )
    )

    hits = await library_index.search("toyrepo")
    assert hits[0].name == "toyrepo fix recipe"
    # matches come from name, tag, or description; non-matching items stay out
    assert {h.name for h in await library_index.search("pytest")} == {"pytest patterns"}
    assert {h.name for h in await library_index.search("drive")} == {"drive fetcher"}
    assert {h.name for h in await library_index.search("toyrepo")} == {"toyrepo fix recipe"}
    assert await library_index.search("zzz-nothing") == []

    # uses counter
    await library_index.record_use(hits[0].id)
    got = await library_index.get_item(hits[0].id)
    assert got is not None and got.uses == 1


def test_git_library_commits(tmp_path) -> None:
    import subprocess

    lib = GitLibrary(tmp_path / "lib")
    item = _item("toyrepo fix recipe")
    rel = lib.write_body(item, "# recipe\nrun pytest\n", "library: add skill")
    assert rel == "skills/toyrepo-fix-recipe.md"
    item.body_path = rel  # callers persist the returned path on the item
    body = lib.read_body(item)
    assert body is not None and "pytest" in body
    log = subprocess.run(["git", "log", "--oneline"], cwd=lib.root, capture_output=True, text=True)
    assert "library: add skill" in log.stdout
    # a second write bumps the file in place (same body path)
    item.touch()
    lib.write_body(item, "# recipe v2\n", "library: update skill")
    assert "v2" in (lib.read_body(item) or "")
    assert lib.last_commit(item) is not None


async def test_postgres_reads_leave_no_open_transaction(library_index) -> None:  # type: ignore[no-untyped-def]
    """Every Library read must end its transaction (found live: the TUI's
    library connection sat idle-in-transaction for hours on prod)."""
    from rework.library.store import PostgresLibraryStore

    if not isinstance(library_index, PostgresLibraryStore):
        pytest.skip("transaction hygiene is a Postgres concern")
    from psycopg.pq import TransactionStatus

    item = LibraryItem(type=ItemType.SKILL, name="hygiene", status=ItemStatus.ACTIVE)
    await library_index.upsert_item(item)
    for read in (
        lambda: library_index.get_item(item.id),
        lambda: library_index.list_items(),
        lambda: library_index.search("hygiene"),
    ):
        await read()
        assert library_index._conn.info.transaction_status == TransactionStatus.IDLE


# -- body in the DB row (cross-machine reads) -------------------------------


async def test_body_round_trips_through_the_row(library_index) -> None:
    """The body is a column, not just a file: it must survive a fresh read."""
    item = _item("row body skill")
    item.body = "# from the row\nstep one\n"
    await library_index.upsert_item(item)

    got = await library_index.get_item(item.id)
    assert got is not None
    assert got.body == "# from the row\nstep one\n"

    # an update rewrites the body (write-through, not write-once)
    item.body = "# edited\n"
    item.touch()
    await library_index.upsert_item(item)
    got2 = await library_index.get_item(item.id)
    assert got2 is not None and got2.body == "# edited\n"

    # a fresh store handle on the same DB sees it too (new connection)
    listed = await library_index.list_items()
    assert [i.body for i in listed if i.id == item.id] == ["# edited\n"]


async def test_row_body_wins_over_the_git_file(tmp_path) -> None:
    """The row is the source of truth: a stale local file must not shadow it."""

    db = tmp_path / "state.sqlite3"
    index = await open_library_store(str(db))
    try:
        item = _item("stale file skill")
        item.body_path = "skills/stale-file-skill.md"
        item.body = "# the row version\n"
        await index.upsert_item(item)

        git = GitLibrary(tmp_path / "lib")
        git.ensure_repo()
        dest = git.root / item.body_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("# the stale file version\n")

        got = await index.get_item(item.id)
        assert got is not None
        assert read_item_body(got, git) == "# the row version\n"
    finally:
        await index.aclose()


async def test_body_readable_on_a_machine_with_no_git_folder(tmp_path, library_index) -> None:
    """THE cross-machine case: written with git folder A, read with git folder
    B (a different, empty library dir -- i.e. a second machine sharing only
    the index). The body must still come back."""
    machine_a = GitLibrary(tmp_path / "machine-a" / "library")
    item, _note = await contribute(
        library_index,
        machine_a,
        type_=ItemType.SKILL,
        name="cross machine recipe",
        description="how to read a body on another machine",
        body="run the failing test, then fix it\n",
        tags=["cross-machine"],
        reusable_because="generalizes to any future cross-machine library read",
    )
    assert machine_a.read_body(item) is not None  # file exists on machine A

    machine_b = GitLibrary(tmp_path / "machine-b" / "library")
    assert not machine_b.root.exists()
    assert machine_b.read_body(item) is None  # no local file on machine B

    fresh = await library_index.get_item(item.id)
    assert fresh is not None
    body = read_item_body(fresh, machine_b)
    assert body is not None and "run the failing test" in body
    assert body == machine_a.read_body(item)

    # and deleting machine A's file does not matter either: the row has it
    for path in (machine_a.root / item.body_path).parent.glob("*.md"):
        path.unlink()
    assert machine_a.read_body(item) is None
    assert read_item_body(fresh, machine_a) == body


async def test_contribute_persists_the_git_body_text(tmp_path, library_index) -> None:
    """contribute() still writes the git file (local history) AND the row."""
    git = GitLibrary(tmp_path / "lib")
    item, _note = await contribute(
        library_index,
        git,
        type_=ItemType.REPORT,
        name="report with a body",
        description="d",
        body="# report\nfindings here\n",
        tags=[],
    )
    fresh = await library_index.get_item(item.id)
    assert fresh is not None
    assert "findings here" in fresh.body
    # the file and the row agree (the file is the mirror, the row the truth)
    assert (git.root / (item.body_path or "")).read_text() == fresh.body


async def test_backfill_bodies_from_the_local_files(tmp_path) -> None:
    """A pre-migration row (body column empty, file on disk) is filled by the
    backfill and stays readable after the file goes away."""
    db = tmp_path / "state.sqlite3"
    index = await open_library_store(str(db))
    git = GitLibrary(tmp_path / "lib")
    try:
        # simulate the old world: file written, row has only body_path
        legacy = _item("legacy skill")
        legacy.body_path = await asyncio.to_thread(
            git.write_body, legacy, "# legacy body\n", "library: add skill"
        )
        assert legacy.body == ""
        await index.upsert_item(legacy)
        assert (await index.get_item(legacy.id)).body == ""  # type: ignore[union-attr]

        # a row whose file is NOT on this machine is skipped, not failed
        orphan = _item("orphan skill")
        orphan.body_path = "skills/orphan-skill.md"
        await index.upsert_item(orphan)

        filled, skipped = await backfill_bodies(index, git)
        assert (filled, skipped) == (1, 1)
        got = await index.get_item(legacy.id)
        assert got is not None and got.body == "# legacy body\n"

        # idempotent: a second run is a no-op (and skips the orphan again)
        assert await backfill_bodies(index, git) == (0, 1)

        # the row alone is enough: remove the file, the body survives
        (git.root / legacy.body_path).unlink()
        again = await index.get_item(legacy.id)
        assert again is not None
        assert read_item_body(again, git) == "# legacy body\n"
    finally:
        await index.aclose()


async def test_sqlite_migration_adds_body_without_data_loss(tmp_path) -> None:
    """An existing sqlite DB created before the column existed must gain it
    (PRAGMA table_info + ALTER), keeping its rows."""
    import aiosqlite

    from rework.library.store import _SQLITE_DDL

    db = tmp_path / "old.sqlite3"
    # the DDL as it was before the column existed (drop just the body line)
    old_ddl = [d.replace("        body TEXT,\n", "") for d in _SQLITE_DDL]
    assert all("body TEXT" not in d for d in old_ddl)
    conn = await aiosqlite.connect(str(db))
    for ddl in old_ddl:
        await conn.execute(ddl)
    await conn.execute(
        "INSERT INTO library_items (id, type, name, description, body_path, tags,"
        " provenance, status, version, uses, created_at, updated_at, vectors)"
        " VALUES ('l-old', 'skill', 'old skill', 'd', 'skills/old-skill.md', '[]',"
        " '{}', 'active', 1, 0, 1.0, 1.0, '{}')"
    )
    await conn.commit()
    cur = await conn.execute("PRAGMA table_info(library_items)")
    cols = {row[1] for row in await cur.fetchall()}
    assert "body" not in cols
    await conn.close()

    index = await open_library_store(str(db))
    try:
        got = await index.get_item("l-old")
        assert got is not None
        assert got.name == "old skill"  # no data loss
        assert got.body == ""  # new column, empty for the pre-existing row
        got.body = "# filled in later\n"
        await index.upsert_item(got)
        assert (await index.get_item("l-old")).body == "# filled in later\n"  # type: ignore[union-attr]
    finally:
        await index.aclose()
    # re-opening is a no-op, not an error (ALTER only when missing)
    index2 = await open_library_store(str(db))
    try:
        assert (await index2.get_item("l-old")).body == "# filled in later\n"  # type: ignore[union-attr]
    finally:
        await index2.aclose()


# -- Postgres reconnect: a dead connection self-heals once, then errors clear --


class _FakeCursor:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeCursor:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def execute(self, sql: str, params: tuple = ()) -> None:
        await self._conn.execute(sql, params)
        self._conn.queries.append(sql)

    async def fetchone(self) -> object:
        return self._conn.row

    async def fetchall(self) -> list:
        return [self._conn.row] if self._conn.row else []


class _FakeTxInfo:
    from psycopg.pq import TransactionStatus

    transaction_status = TransactionStatus.IDLE


class _FakeConn:
    """A stand-in psycopg connection that raises OperationalError until told
    to recover (as a recreated DB container leaves the old socket)."""

    def __init__(self) -> None:
        import psycopg

        self.fail = True
        self.closed = False
        self.row: dict | None = None
        self.queries: list[str] = []
        self.commits = 0
        self.info = _FakeTxInfo()
        self._err = psycopg.OperationalError("the connection is closed")

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    async def execute(self, sql: str, params: tuple = ()) -> None:
        if self.fail:
            raise self._err
        self.queries.append(sql)

    async def commit(self) -> None:
        if self.fail:
            raise self._err
        self.commits += 1

    async def rollback(self) -> None:
        pass

    async def close(self) -> None:
        self.closed = True


def _item_row(item_id: str = "l-one") -> dict:
    return {
        "id": item_id,
        "type": "skill",
        "name": "one",
        "description": "d",
        "body_path": None,
        "body": None,
        "tags": "[]",
        "provenance": "{}",
        "status": "active",
        "version": 1,
        "uses": 0,
        "created_at": 1.0,
        "updated_at": 1.0,
        "vectors": "{}",
    }


def _postgres_with_fake(monkeypatch, first: _FakeConn, rest: list[_FakeConn]):
    from rework.library import store as store_mod

    made: list[_FakeConn] = []

    async def fake_connect(dsn: str) -> _FakeConn:
        conn = rest[len(made)]
        made.append(conn)
        return conn

    monkeypatch.setattr(store_mod, "_connect", fake_connect)
    return store_mod.PostgresLibraryStore(first, dsn="postgresql://fake"), made


async def test_dead_connection_reconnects_and_retries_once(monkeypatch) -> None:
    """First use hits a closed connection (recreated DB container); the store
    reconnects through the shared connect path and the read still answers."""
    first, second = _FakeConn(), _FakeConn()
    second.fail = False
    second.row = _item_row()
    store, made = _postgres_with_fake(monkeypatch, first, [second])

    got = await store.get_item("l-one")
    assert got is not None and got.id == "l-one"
    assert made == [second], "reconnect happened exactly once"
    assert first.closed, "the dead handle was dropped"
    assert store._conn is second and store._op_lock._conn is second

    # later reads just work (the fresh connection stays)
    assert (await store.get_item("l-one")).id == "l-one"  # type: ignore[union-attr]
    assert made == [second]


async def test_reconnect_failure_is_a_clear_error_not_an_infinite_retry(
    monkeypatch,
) -> None:
    """A second consecutive failure surfaces one clear error; no retry loop."""
    import psycopg

    first, second, third = _FakeConn(), _FakeConn(), _FakeConn()
    store, made = _postgres_with_fake(monkeypatch, first, [second, third])

    with pytest.raises(RuntimeError, match="library store unavailable after reconnect"):
        await store.get_item("l-one")
    assert made == [second], "retried exactly once, then gave up"
    assert not isinstance(psycopg.OperationalError("x"), RuntimeError)  # distinct type


async def test_record_use_write_path_recovers(monkeypatch) -> None:
    """The write path self-heals too: the dead conn is closed (discarding any
    half-applied write) before the retry runs on the fresh connection."""
    first, second = _FakeConn(), _FakeConn()
    second.fail = False
    store, made = _postgres_with_fake(monkeypatch, first, [second])

    await store.record_use("l-one")
    assert made == [second]
    assert any("UPDATE library_items SET uses" in q for q in second.queries)
    assert second.commits == 1
    assert first.closed


async def test_non_connection_errors_are_not_retried(monkeypatch) -> None:
    """A query error (not a dead connection) must propagate as-is, unretried."""

    class _BorkedConn(_FakeConn):
        async def execute(self, sql: str, params: tuple = ()) -> None:
            raise ValueError("bad query")

    from rework.library import store as store_mod

    store = store_mod.PostgresLibraryStore(_BorkedConn(), dsn="postgresql://fake")
    connect_calls = 0

    async def fake_connect(dsn: str) -> _FakeConn:
        nonlocal connect_calls
        connect_calls += 1
        return _FakeConn()

    monkeypatch.setattr(store_mod, "_connect", fake_connect)
    with pytest.raises(ValueError, match="bad query"):
        await store.get_item("l-one")
    assert connect_calls == 0


async def test_a_store_without_a_dsn_never_reconnects(monkeypatch) -> None:
    """The legacy construction (bare conn, no dsn) keeps the old behaviour:
    the error propagates, nothing is retried."""
    from rework.library import store as store_mod

    store = store_mod.PostgresLibraryStore(_FakeConn())
    monkeypatch.setattr(
        store_mod,
        "_connect",
        lambda dsn: pytest.fail("must not reconnect without a dsn"),
    )
    import psycopg

    with pytest.raises(psycopg.OperationalError):
        await store.get_item("l-one")


# -- rotating the Postgres password: the store re-reads the env file --


def _rotated_env(monkeypatch: pytest.MonkeyPatch, tmp_path, url: str):
    """Point REWORK_ENV_FILE at a tmp env file, as a real rotation edits it."""
    env_file = tmp_path / "rework.env"
    env_file.write_text(f"REWORK_DB={url}\n")
    monkeypatch.setenv("REWORK_ENV_FILE", str(env_file))
    return env_file


def _rotated_password_error():
    import psycopg

    return psycopg.OperationalError('FATAL:  password authentication failed for user "postgres"')


async def test_a_rotated_password_reconnects_with_the_new_dsn(
    monkeypatch: pytest.MonkeyPatch, tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    """Found live: after the Postgres password was rotated, every reconnect
    reused the URL this process started with and failed with 28P01 forever.
    An AUTH failure must re-read the env file and reconnect with the NEW dsn,
    exactly once, without ever logging the URL (it carries the password)."""
    import logging

    from rework.credentials import CREDENTIALS_RELOADED
    from rework.library import store as store_mod

    stale = "postgresql://postgres:STALEPASS@host:5433/rework_library"
    fresh = "postgresql://postgres:LIVEHOSTPASS@host:5433/rework_library"
    _rotated_env(monkeypatch, tmp_path, fresh)
    monkeypatch.setenv("REWORK_DB", stale)
    caplog.set_level(logging.DEBUG, logger="rework.library")

    class _AuthFailedConn(_FakeConn):
        def __init__(self) -> None:
            super().__init__()
            self._err = _rotated_password_error()

    first = _AuthFailedConn()
    second = _FakeConn()
    second.fail = False
    second.row = _item_row()
    made: list[_FakeConn] = []
    dsn_seen: list[str] = []

    async def fake_connect(dsn: str) -> _FakeConn:
        dsn_seen.append(dsn)
        conn = [second][len(made)]
        made.append(conn)
        return conn

    monkeypatch.setattr(store_mod, "_connect", fake_connect)
    store = store_mod.PostgresLibraryStore(first, dsn=stale)

    got = await store.get_item("l-one")
    assert got is not None and got.id == "l-one", "real data after the rotation"
    assert store._dsn == fresh, "the store now holds the rotated dsn"
    assert dsn_seen == [fresh], "the reconnect used the NEW dsn, not the stale one"
    assert store._conn is second and store._op_lock._conn is second

    messages = [r.getMessage() for r in caplog.records if r.name == "rework.library"]
    assert CREDENTIALS_RELOADED in messages
    blob = " ".join(messages) + " ".join(str(r.args) for r in caplog.records)
    for secret in ("STALEPASS", "LIVEHOSTPASS", stale, fresh):
        assert secret not in blob, "no password or URL ever reaches the log"


async def test_an_auth_failure_with_unchanged_credentials_reconnects_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    """The env file still holds the URL we already use: nothing to recover, so
    the old behaviour stands (warn, one reconnect, no second retry, no loop)
    and the reload line is NOT logged."""
    import logging

    from rework.credentials import CREDENTIALS_RELOADED
    from rework.library import store as store_mod

    dsn = "postgresql://postgres:STALEPASS@host:5433/rework_library"
    _rotated_env(monkeypatch, tmp_path, dsn)
    caplog.set_level(logging.DEBUG, logger="rework.library")

    class _AuthFailedConn(_FakeConn):
        def __init__(self) -> None:
            super().__init__()
            self._err = _rotated_password_error()

    first = _AuthFailedConn()
    second = _FakeConn()
    second.fail = False
    second.row = _item_row()

    async def connecting(_dsn: str) -> _FakeConn:
        # unchanged credentials -> the SAME dsn is retried (today's behaviour)
        return second

    monkeypatch.setattr(store_mod, "_connect", connecting)
    store = store_mod.PostgresLibraryStore(first, dsn=dsn)
    got = await store.get_item("l-one")
    assert got is not None and got.id == "l-one"
    assert store._dsn == dsn, "the dsn is untouched when the file holds the same URL"
    messages = [r.getMessage() for r in caplog.records if r.name == "rework.library"]
    assert CREDENTIALS_RELOADED not in messages, "nothing was reloaded"


async def test_a_non_auth_error_never_re_reads_the_library_env_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A dead socket must keep exactly today's behaviour: no env file read."""
    from rework.library import store as store_mod

    _rotated_env(monkeypatch, tmp_path, "postgresql://postgres:new@host:5433/l")

    def must_not_run(**_kwargs: object) -> None:
        pytest.fail("the env file must not be re-read for a non-auth failure")

    monkeypatch.setattr(store_mod, "recover_db_credentials", must_not_run)
    first = _FakeConn()  # raises OperationalError("the connection is closed")
    second = _FakeConn()
    second.fail = False
    second.row = _item_row()
    store, made = _postgres_with_fake(monkeypatch, first, [second])

    got = await store.get_item("l-one")
    assert got is not None and got.id == "l-one"
    assert made == [second]
    assert store._dsn == "postgresql://fake", "the dsn is never rewritten"


# -- the production sequence: the socket dies FIRST, auth fails on reconnect --


def _dead_socket_error():
    import psycopg

    return psycopg.OperationalError("the connection is closed")


async def test_a_dead_socket_then_an_auth_failure_on_reconnect_still_recovers(
    monkeypatch: pytest.MonkeyPatch, tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    """Found in audit, the REAL sequence: an idle timeout or a DB restart kills
    the socket first, so the operation fails with 'the connection is closed'
    and only the RECONNECT meets the rotated password. Classifying only the
    first error left the long-lived `rework library-mcp` retrying the stale dsn
    forever (reproduced: three calls, three connects, all stale)."""
    import logging

    from rework.credentials import CREDENTIALS_RELOADED
    from rework.library import store as store_mod

    stale = "postgresql://postgres:STALEPASS@host:5433/rework_library"
    fresh = "postgresql://postgres:LIVEHOSTPASS@host:5433/rework_library"
    _rotated_env(monkeypatch, tmp_path, fresh)
    monkeypatch.setenv("REWORK_DB", stale)
    caplog.set_level(logging.DEBUG, logger="rework.library")

    first = _FakeConn()  # dead socket: the non-auth error reaches _run first
    working = _FakeConn()
    working.fail = False
    working.row = _item_row()
    dsn_seen: list[str] = []

    async def fake_connect(dsn: str) -> _FakeConn:
        dsn_seen.append(dsn)
        if len(dsn_seen) == 1:
            raise _rotated_password_error()  # the reconnect meets the new password
        return working

    monkeypatch.setattr(store_mod, "_connect", fake_connect)
    store = store_mod.PostgresLibraryStore(first, dsn=stale)

    got = await store.get_item("l-one")
    assert got is not None and got.id == "l-one", "recovered instead of offline forever"
    assert dsn_seen == [stale, fresh], "one attempt on the stale dsn, then exactly one on the new"
    assert store._dsn == fresh, "the store now holds the rotated dsn"
    assert store._conn is working and store._op_lock._conn is working

    messages = [r.getMessage() for r in caplog.records if r.name == "rework.library"]
    assert messages.count(CREDENTIALS_RELOADED) == 1, "exactly one reload line"
    blob = " ".join(messages) + " ".join(str(r.args) for r in caplog.records)
    for secret in ("STALEPASS", "LIVEHOSTPASS", stale, fresh):
        assert secret not in blob, "no password or URL ever reaches the log"


async def test_a_dead_socket_then_an_auth_failure_with_unchanged_credentials_is_bounded(
    monkeypatch: pytest.MonkeyPatch, tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    """Same sequence, but the env file holds the URL we already use: nothing to
    recover, so it gives up after ONE reconnect (no loop) with a clear error."""
    import logging

    from rework.credentials import CREDENTIALS_RELOADED
    from rework.library import store as store_mod

    stale = "postgresql://postgres:STALEPASS@host:5433/rework_library"
    _rotated_env(monkeypatch, tmp_path, stale)
    caplog.set_level(logging.DEBUG, logger="rework.library")
    dsn_seen: list[str] = []

    async def fake_connect(dsn: str) -> _FakeConn:
        dsn_seen.append(dsn)
        raise _rotated_password_error()

    monkeypatch.setattr(store_mod, "_connect", fake_connect)
    store = store_mod.PostgresLibraryStore(_FakeConn(), dsn=stale)

    with pytest.raises(RuntimeError, match="library store unavailable after reconnect"):
        await store.get_item("l-one")
    assert dsn_seen == [stale], "exactly one connect attempt, never a second"
    assert store._dsn == stale
    messages = [r.getMessage() for r in caplog.records if r.name == "rework.library"]
    assert CREDENTIALS_RELOADED not in messages, "nothing was reloaded"
