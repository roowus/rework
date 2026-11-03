"""Bounded reproduction of the stop/restart demo with logging.

Run: REWORK_DB=/tmp/rework-kr/state.sqlite3 uv run python tests/manual/repro_stop_restart.py
Not part of the test suite (no test_ prefix).
"""

import asyncio
import logging
import os
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
os.environ.setdefault("REWORK_DB", "/tmp/rework-kr/state.sqlite3")


async def main() -> None:
    from rework.config import Config, load_catalog, open_store_from_config
    from rework.kernel.events.bus import InProcessEventBus
    from rework.kernel.gates import GateManager
    from rework.kernel.state.records import Mission, new_id
    from rework.orchestrator.daemon import Daemon, sqlite_checkpointer_factory
    from rework.roles.registry import autodiscover

    config = Config.from_env()
    store = await open_store_from_config(config)

    def make_daemon() -> Daemon:
        return Daemon(
            store=store,
            bus=InProcessEventBus(store),
            registry=autodiscover(),
            catalog=load_catalog(),
            make_checkpointer=sqlite_checkpointer_factory(config.checkpoints_db),
            workroot=Path(config.db).parent / "work",
            config=config,
        )

    print("== daemon A starts ==", flush=True)
    daemon_a = make_daemon()
    task_a = asyncio.create_task(daemon_a.run_forever())
    await asyncio.sleep(1)

    mission = await store.create_mission(Mission(id=new_id("m"), objective="repro stop"))
    print(f"mission {mission.id} created", flush=True)

    deadline = time.time() + 15
    gate = None
    while time.time() < deadline:
        gates = await store.list_open_gates(mission.id)
        if gates:
            gate = gates[0]
            break
        await asyncio.sleep(0.2)
    assert gate is not None, "daemon A never opened a gate"
    print(f"gate {gate.id} open; stoping daemon A", flush=True)

    task_a.cancel()
    try:
        await asyncio.wait_for(task_a, timeout=5)
    except (TimeoutError, asyncio.CancelledError):
        pass
    await store.release_lock(daemon_a._owner)

    print("== daemon B starts (recovery) ==", flush=True)
    daemon_b = make_daemon()
    task_b = asyncio.create_task(daemon_b.run_forever())

    await asyncio.sleep(1)
    gm = GateManager(store, InProcessEventBus(store))
    decided = await gm.decide(gate.id, approved=True, note="repro")
    print(f"gate decided: {decided.status.value if decided else None}", flush=True)

    deadline = time.time() + 20
    final = None
    while time.time() < deadline:
        m = await store.get_mission(mission.id)
        if m and m.status.value in {"done", "failed"}:
            final = m.status.value
            break
        await asyncio.sleep(0.3)
    runs = await store.list_runs(mission.id)
    print(f"FINAL STATUS: {final} runs={[r.status.value for r in runs]}", flush=True)

    task_b.cancel()
    try:
        await asyncio.wait_for(task_b, timeout=5)
    except (TimeoutError, asyncio.CancelledError):
        pass
    await store.aclose()
    assert final == "done", f"expected done, got {final}"


asyncio.run(main())
