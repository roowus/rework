"""The open_pr gate: branch push to origin (a local bare remote stands in for
GitHub) + mechanical PR creation through a fake gh binary. This is the
self-hosting acceptance in test form."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from test_vertical_slice import AUDIT_CLEAN, _auditor_script, _coder_script, _investigator_script

from conftest import ScriptedProvider, role_catalog
from rework.missions import PLAN_FIX_PR
from rework.kernel.events.bus import InProcessEventBus
from rework.kernel.gates import GateManager
from rework.kernel.state.records import Mission, MissionStatus
from rework.kernel.state.sqlite import SqliteStore, open_sqlite
from rework.orchestrator.engine import MissionRunner
from rework.roles.registry import autodiscover


@pytest.fixture
def pr_env(tmp_path: Path) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    """Mission repo with origin = a bare remote; a fake gh that records calls."""
    source = Path(__file__).parents[2] / "examples" / "toyrepo"
    dest = tmp_path / "mission" / "toyrepo"
    dest.mkdir(parents=True)
    for f in source.iterdir():
        if f.is_file():
            shutil.copy(f, dest / f.name)
    bare = tmp_path / "origin.git"
    bare.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "--bare", "-b", "main"], cwd=bare, check=True, capture_output=True
    )
    subprocess.run(["git", "init", "-b", "main"], cwd=dest, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=dest, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=r@r", "-c", "user.name=r", "commit", "-m", "init"],
        cwd=dest,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", str(bare)], cwd=dest, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "push", "-u", "origin", "main"], cwd=dest, check=True, capture_output=True
    )

    fake_gh = tmp_path / "fake-gh"
    fake_gh.write_text(
        "#!/bin/sh\n"
        'echo "$@" >> ' + str(tmp_path / "gh-calls.log") + "\n"
        "echo https://github.com/roowus/toyrepo/pull/1\n"
    )
    fake_gh.chmod(0o755)
    return {
        "repo": dest,
        "bare": bare,
        "fake_gh": str(fake_gh),
        "gh_log": tmp_path / "gh-calls.log",
        "tmp": tmp_path,
    }


@pytest.fixture
async def world(pr_env: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:  # type: ignore[no-untyped-def]
    store: SqliteStore = await open_sqlite(str(pr_env["tmp"] / "pr.sqlite3"))
    try:
        objective = "write docs: add a line to README saying hello from rework"
        plan = json.loads(json.dumps(PLAN_FIX_PR).replace("$OBJECTIVE", objective))
        mission = await store.create_mission(
            Mission(id="mpr", objective=objective, repo_path=str(pr_env["repo"]), plan=plan)
        )
        provider = ScriptedProvider()
        provider.route("Investigate any brief", _investigator_script())
        provider.route("Fulfill the task", _coder_script("docs line added, tests still green"))
        provider.route("Find issues", _auditor_script(AUDIT_CLEAN))
        yield {
            "store": store,
            "provider": provider,
            "mission": mission,
            "ck": str(pr_env["tmp"] / "ck.sqlite3"),
            "workroot": pr_env["tmp"] / "work",
            "registry": autodiscover(),
            "catalog": role_catalog(),
            "tmp": pr_env["tmp"],
            "fake_gh": pr_env["fake_gh"],
            "gh_log": pr_env["gh_log"],
            "bare": pr_env["bare"],
        }
    finally:
        await store.aclose()


async def test_open_pr_gate_pushes_and_creates_pr(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REWORK_GH_BIN", str(world["fake_gh"]))
    store: SqliteStore = world["store"]
    runner = MissionRunner(
        world["mission"].id,
        store,
        InProcessEventBus(store),
        world["registry"],
        world["catalog"],
        _ck_factory(world["ck"]),
        world["workroot"],
        poll_interval_s=0.05,
        provider=world["provider"],
    )
    task = asyncio.create_task(runner.run())

    for _ in range(200):
        gates = await store.list_open_gates(world["mission"].id)
        if gates:
            break
        await asyncio.sleep(0.05)
    gates = await store.list_open_gates(world["mission"].id)
    assert gates and gates[0].kind.value == "open_pr"

    second: SqliteStore = await open_sqlite(str(world["tmp"] / "pr.sqlite3"))
    try:
        await GateManager(second, InProcessEventBus(second)).decide(gates[0].id, approved=True)
    finally:
        await second.aclose()

    await asyncio.wait_for(task, timeout=30)
    mission = await store.get_mission(world["mission"].id)
    assert mission is not None and mission.status == MissionStatus.DONE
    assert (mission.result or {}).get("pr_url") == "https://github.com/roowus/toyrepo/pull/1"

    # the branch really reached the bare remote (origin)
    heads_proc = await asyncio.create_subprocess_exec(
        "git",
        "branch",
        "--list",
        "*mpr*",
        cwd=str(world["bare"]),
        stdout=asyncio.subprocess.PIPE,
    )
    heads_out, _ = await heads_proc.communicate()
    assert b"mpr" in heads_out
    # the fake gh got the PR call with head + base
    gh_log = Path(str(world["gh_log"])).read_text()  # noqa: ASYNC240 (tiny test read)
    assert "pr create" in gh_log and "--head" in gh_log


def _ck_factory(ck_path: str):  # type: ignore[no-untyped-def]
    from rework.orchestrator.daemon import sqlite_checkpointer_factory

    return sqlite_checkpointer_factory(ck_path)
