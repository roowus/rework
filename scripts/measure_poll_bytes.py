#!/usr/bin/env python
"""Bytes the TUI pulls per poll: the old full mission list vs the slim one.

    uv run python scripts/measure_poll_bytes.py [missions]

Seeds a tmp SQLite store with a realistic spread of missions (plan a few KB,
result a few KB, a handful of fat ones, one with a 200 KB result), then sizes
what one poll transfers, as the JSON of the records, which tracks what a
Postgres row costs on the wire (text columns dominate). Prints the numbers and
extrapolates MB/hour at the poll interval, with and without the idle backoff.
Reads and writes only its own tmp directory."""

from __future__ import annotations

import asyncio
import random
import sys
import tempfile
from pathlib import Path

from rework.kernel.state.records import Gate, GateKind, Mission, MissionStatus, new_id
from rework.kernel.state.sqlite import open_sqlite
from rework.tui.app import IDLE_POLL_SECONDS, POLL_SECONDS


def _size(rows: list) -> int:  # type: ignore[type-arg]
    return sum(len(r.model_dump_json()) for r in rows)


async def main(count: int) -> None:
    rng = random.Random(7)
    with tempfile.TemporaryDirectory() as tmp:
        store = await open_sqlite(str(Path(tmp) / "measure.sqlite3"))
        try:
            statuses = [MissionStatus.DONE] * 8 + [MissionStatus.FAILED, MissionStatus.RUNNING]
            first = ""
            for i in range(count):
                m = await store.create_mission(
                    Mission(
                        id=new_id("m"),
                        objective=f"mission {i}: "
                        + "fix the thing and add tests " * rng.randint(1, 20),
                        repo_path="/srv/repos/rework",
                        project_id=f"proj-{i % 4}",
                        title=f"title {i}",
                        created_at=1_700_000_000.0 + i,
                    )
                )
                first = first or m.id
                plan = {
                    "kind": "fix",
                    "orchestrator": {"model": "pro"},
                    "steps": [
                        {"step": f"s{k}", "role": "coder", "spec": "x" * 150} for k in range(12)
                    ],
                }
                result = {"summary": "s" * rng.randint(500, 4000), "pr_url": "https://x/pull/1"}
                if i == count // 2:
                    result = {"summary": "x" * 200_000}  # the one that hurts
                await store.update_mission(
                    m.id, status=statuses[i % len(statuses)], plan=plan, result=result
                )
            await store.create_gate(
                Gate(
                    id=new_id("g"),
                    mission_id=first,
                    kind=GateKind.SHIP,
                    question="Ship it?",
                    spec={"options": ["ship", "hold"]},
                )
            )
            full = _size(await store.list_missions(limit=200))
            light = _size(await store.list_missions_light(limit=200))
            rest = _size(await store.list_open_gates()) + _size(await store.list_chats(limit=200))
            rest += 40  # spend_today: one aggregate row
        finally:
            await store.aclose()

    before, after = full + rest, light + rest
    print(f"missions in store: {count} (poll limit 200)")
    print(f"mission list, full rows  : {full:>10,} B")
    print(f"mission list, light rows : {light:>10,} B")
    print(f"gates+chats+spend (same) : {rest:>10,} B")
    print(f"bytes per poll BEFORE    : {before:>10,} B")
    print(f"bytes per poll AFTER     : {after:>10,} B")
    print(f"reduction                : {100 * (1 - after / before):.2f}%  ({before / after:.0f}x)")
    for label, secs in (
        (f"active owner, {POLL_SECONDS:g}s poll", POLL_SECONDS),
        (f"idle owner, {IDLE_POLL_SECONDS:g}s poll", IDLE_POLL_SECONDS),
    ):
        mb_before = before * 3600 / secs / 1e6
        mb_after = after * 3600 / secs / 1e6
        print(f"MB/hour, {label:<26}: before {mb_before:>9,.1f}  after {mb_after:>8,.2f}")
    print("(an unchanged poll also skips the repaint; the idle rate applies after 30s untouched)")


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 200))
