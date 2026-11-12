"""Merge a local SQLite rework database into a Postgres one (one instance).

    uv run python scripts/merge_local_into.py SQLITE_PATH            # dry run
    uv run python scripts/merge_local_into.py SQLITE_PATH --apply    # write

Target: REWORK_DB (Postgres). Only FINISHED missions move (done, failed,
cancelled); rerun later for the rest. Idempotent: rows whose id already
exists are skipped, and the auto-numbered rows (events, chat messages,
charges) are only copied for missions and chats inserted by this run.
Projects are matched by NAME (local ids differ) and created when missing.
Library item bodies are files: copy them separately (see --library-out).
Backup the target first (docs/ops.md).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sqlite3
from pathlib import Path

import psycopg

FINISHED = ("done", "failed", "cancelled")


def rows(db: sqlite3.Connection, sql: str, args: tuple = ()) -> list[dict]:
    db.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in db.execute(sql, args).fetchall()]
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):  # older databases predate chats etc.
            return []
        raise


async def existing(pg, table: str, ids: list[str]) -> set[str]:
    if not ids:
        return set()
    cur = await pg.execute(f"SELECT id FROM {table} WHERE id = ANY(%s)", (ids,))
    return {r[0] for r in await cur.fetchall()}


async def insert(pg, table: str, row: dict, apply: bool) -> None:
    if not apply:
        return
    cols = list(row)
    await pg.execute(
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})",
        tuple(row[c] for c in cols),
    )


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("sqlite")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--library-src", default=str(Path.home() / ".rework" / "library"))
    ap.add_argument("--library-out", help="copy library body files here (a local staging dir)")
    a = ap.parse_args()
    src = sqlite3.connect(a.sqlite)
    pg = await psycopg.AsyncConnection.connect(os.environ["REWORK_DB"], connect_timeout=15)
    counts: dict[str, int] = {}
    note = lambda k, n=1: counts.__setitem__(k, counts.get(k, 0) + n)  # noqa: E731

    # projects: by name
    cur = await pg.execute("SELECT id, name FROM projects")
    by_name = {name: pid for pid, name in await cur.fetchall()}
    project_map: dict[str, str] = {}
    for p in rows(src, "SELECT * FROM projects"):
        if p["name"] in by_name:
            project_map[p["id"]] = by_name[p["name"]]
        else:
            project_map[p["id"]] = p["id"]
            await insert(pg, "projects", p, a.apply)
            note("projects")

    # missions (finished only), with their runs, gates, events, charges
    missions = rows(src, f"SELECT * FROM missions WHERE status IN {FINISHED}")
    have = await existing(pg, "missions", [m["id"] for m in missions])
    new_missions = [m for m in missions if m["id"] not in have]
    counts["missions skipped (already there)"] = len(have)
    for m in new_missions:
        if m.get("project_id"):
            m["project_id"] = project_map.get(m["project_id"], m["project_id"])
        await insert(pg, "missions", m, a.apply)
        note("missions")
        for table in ("runs", "gates"):
            for r in rows(src, f"SELECT * FROM {table} WHERE mission_id = ?", (m["id"],)):
                if r["id"] in await existing(pg, table, [r["id"]]):
                    note(f"{table} COLLISION")
                    continue
                await insert(pg, table, r, a.apply)
                note(table)
        for e in rows(src, "SELECT * FROM events WHERE mission_id = ? ORDER BY seq", (m["id"],)):
            e.pop("seq")
            await insert(pg, "events", e, a.apply)
            note("events")
        for c in rows(src, "SELECT * FROM charges WHERE mission_id = ? ORDER BY id", (m["id"],)):
            c.pop("id")
            await insert(pg, "charges", c, a.apply)
            note("charges")

    # chats (any whose mission moved, plus chats with no mission), and messages
    moved = {m["id"] for m in new_missions}
    chats = [
        c
        for c in rows(src, "SELECT * FROM chats")
        if (c.get("mission_id") in moved)
        or not c.get("mission_id")
        or (c["id"].startswith("orch-") and c["id"][5:] in moved)
    ]
    have_chats = await existing(pg, "chats", [c["id"] for c in chats])
    for c in chats:
        if c["id"] in have_chats:
            note("chats skipped (already there)")
            continue
        if c.get("project_id"):
            c["project_id"] = project_map.get(c["project_id"], c["project_id"])
        await insert(pg, "chats", c, a.apply)
        note("chats")
        sql = "SELECT * FROM chat_messages WHERE chat_id = ? ORDER BY id"
        for msg in rows(src, sql, (c["id"],)):
            msg.pop("id")
            await insert(pg, "chat_messages", msg, a.apply)
            note("chat_messages")

    # complaints (text ids)
    comps = rows(src, "SELECT * FROM complaints")
    have_c = await existing(pg, "complaints", [c["id"] for c in comps])
    for c in comps:
        if c["id"] not in have_c:
            await insert(pg, "complaints", c, a.apply)
            note("complaints")

    # library index rows; bodies are files
    items = rows(src, "SELECT * FROM library_items")
    have_l = await existing(pg, "library_items", [i["id"] for i in items])
    lib_src = Path(a.library_src)
    for it in items:
        if it["id"] in have_l:
            note("library skipped (already there)")
            continue
        body = it.get("body_path")
        if body and not (lib_src / body).exists():
            note("library MISSING BODY")
        await insert(pg, "library_items", it, a.apply)
        note("library_items")
        if body and a.library_out and (lib_src / body).exists():
            dst = Path(a.library_out) / body
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(lib_src / body, dst)
            note("library files staged")

    if a.apply:
        await pg.commit()
    else:
        await pg.rollback()
    await pg.close()
    print(("APPLIED " if a.apply else "DRY RUN ") + json.dumps(counts, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
