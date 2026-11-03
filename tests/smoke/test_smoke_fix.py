"""The smoke test: one real fix mission on toyrepo with a real cheap model.

Excluded from default runs (smoke marker); runs ONLY when REWORK_SMOKE=1 plus
a real catalog (REWORK_MODELS or ~/.rework/models.json) and provider keys are
present. The only test allowed to spend money; hard-capped by mission budget.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from langgraph.types import Command  # noqa: F401

from rework.cli import PLAN_FIX
from rework.config import Config, load_catalog, open_store_from_config
from rework.kernel.events.bus import InProcessEventBus
from rework.kernel.gates import GateManager
from rework.kernel.state.records import Mission, MissionStatus
from rework.kernel.state.sqlite import SqliteStore
from rework.orchestrator.daemon import sqlite_checkpointer_factory
from rework.orchestrator.engine import MissionRunner
from rework.roles.registry import autodiscover


def _real_catalog_ready() -> bool:
    if os.environ.get("REWORK_SMOKE") != "1":
        return False
    try:
        catalog = load_catalog()
    except Exception:
        return False
    return any(entry.provider != "fake" for entry in catalog.all())


pytestmark = [
    pytest.mark.smoke,
    pytest.mark.skipif(not _real_catalog_ready(), reason="REWORK_SMOKE=1 + real catalog required"),
]


def _prepare_toyrepo(tmp_path: Path) -> Path:
    """examples/toyrepo as a fresh git repo (sync helper: rule 2 stays clean)."""
    source = Path(__file__).parents[2] / "examples" / "toyrepo"
    dest = tmp_path / "mission" / "toyrepo"
    dest.mkdir(parents=True)
    for f in source.iterdir():
        if f.is_file():
            shutil.copy(f, dest / f.name)
    subprocess.run(["git", "init", "-b", "main"], cwd=dest, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=dest, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=r@r", "-c", "user.name=r", "commit", "-m", "init"],
        cwd=dest,
        check=True,
        capture_output=True,
    )
    return dest


@pytest.fixture
async def smoke_world(tmp_path: Path) -> AsyncIterator[dict[str, Any]]:  # type: ignore[no-untyped-def]
    dest = _prepare_toyrepo(tmp_path)
    store = await open_store_from_config(
        Config(db=str(tmp_path / "s.sqlite3"), checkpoints_db=str(tmp_path / "ck.sqlite3"))
    )
    try:
        objective = "make add(2, 3) == 5: calc.py's add() subtracts; fix it and run the tests"
        plan = json.loads(json.dumps(PLAN_FIX).replace("$OBJECTIVE", objective))
        mission = await store.create_mission(
            Mission(id="smoke", objective=objective, repo_path=str(dest), plan=plan)
        )
        yield {
            "store": store,
            "mission": mission,
            "workroot": tmp_path / "work",
            "catalog": load_catalog(),
        }
    finally:
        await store.aclose()


async def test_real_fix_mission_on_toyrepo(smoke_world: dict[str, Any]) -> None:  # type: ignore[no-untyped-def]
    store: SqliteStore = smoke_world["store"]
    runner = MissionRunner(
        smoke_world["mission"].id,
        store,
        InProcessEventBus(store),
        autodiscover(),
        smoke_world["catalog"],
        sqlite_checkpointer_factory(str(smoke_world["workroot"].parent / "ck.sqlite3")),
        smoke_world["workroot"],
        poll_interval_s=0.5,
    )
    task = asyncio.create_task(runner.run())

    # the ship gate needs a human; auto-approve it here (this IS the human, for the smoke)
    for _ in range(1200):
        gates = await store.list_open_gates(smoke_world["mission"].id)
        if gates:
            break
        await asyncio.sleep(1)
    gates = await store.list_open_gates(smoke_world["mission"].id)
    assert gates, "mission never reached the ship gate"
    await GateManager(store, InProcessEventBus(store)).decide(gates[0].id, approved=True)

    await asyncio.wait_for(task, timeout=900)
    mission = await store.get_mission(smoke_world["mission"].id)
    assert mission is not None and mission.status == MissionStatus.DONE
    runs = await store.list_runs(mission.id)
    coder = next(r for r in runs if r.step == "code")
    assert coder.artifact and "+    return a + b" in str(coder.artifact.get("diff", ""))
    spend = await store.mission_spend(mission.id)
    print(f"\nsmoke spend: ${spend.spent_usd:.4f}")
    assert spend.spent_usd < 1.0, "smoke mission blew its budget"
