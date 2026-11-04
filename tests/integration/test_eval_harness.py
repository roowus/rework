"""Eval harness v1: run_task on the scripted provider, graded mechanically,
score history recorded and readable. No daemon, no real models."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from conftest import (
    AUDIT_CLEAN,
    ScriptedProvider,
    _auditor_script,
    _coder_script,
    _investigator_script,
    role_catalog,
)
from rework.evals.harness import (
    builtin_tasks,
    run_task,
    score_history,
)
from rework.kernel.state.sqlite import SqliteStore, open_sqlite
from rework.orchestrator.daemon import sqlite_checkpointer_factory
from rework.roles.registry import autodiscover


@pytest.fixture
async def eval_world(tmp_path: Path) -> AsyncIterator[dict[str, Any]]:  # type: ignore[no-untyped-def]
    store: SqliteStore = await open_sqlite(str(tmp_path / "eval.sqlite3"))
    try:
        provider = ScriptedProvider()
        yield {
            "store": store,
            "provider": provider,
            "catalog": role_catalog(),
            "ck": str(tmp_path / "ck.sqlite3"),
            "workroot": tmp_path / "eval-work",
            "tmp": tmp_path,
        }
    finally:
        await store.aclose()


async def test_run_task_passes_and_records_history(eval_world: dict[str, Any]) -> None:
    store: SqliteStore = eval_world["store"]
    provider: ScriptedProvider = eval_world["provider"]
    provider.route("Investigate any brief", _investigator_script())
    provider.route("Fulfill the task", _coder_script("tests pass: 2/1 green, add fixed"))
    provider.route("Find issues", _auditor_script(AUDIT_CLEAN))

    task = next(t for t in builtin_tasks() if t.id == "toyrepo-planted-bug")
    score = await run_task(
        task,
        store,
        autodiscover(),
        eval_world["catalog"],
        sqlite_checkpointer_factory(eval_world["ck"]),
        eval_world["workroot"],
        provider=provider,
    )

    assert score.passed, f"grader said no: {score.grade}"
    assert score.grade["tests_pass"] is True
    assert score.grade["diff_present"] is True
    assert score.grade["mission_done"] is True
    assert score.cost_usd == 0.0  # scripted provider charges nothing

    # history: this run is readable, oldest first, keyed by task
    history = await score_history(store, "toyrepo-planted-bug")
    assert len(history) == 1
    assert history[0]["mission"] == score.mission_id
    assert history[0]["passed"] is True
    # everything: sentinel exists, other-task queries empty
    assert await score_history(store, "no-such-task") == []


async def test_score_history_accumulates(eval_world: dict[str, Any]) -> None:
    store: SqliteStore = eval_world["store"]
    provider: ScriptedProvider = eval_world["provider"]
    for _ in range(2):  # two runs of the same task: the hill-climb baseline
        provider.route("Investigate any brief", _investigator_script())
        provider.route("Fulfill the task", _coder_script("tests pass: 2/1 green"))
        provider.route("Find issues", _auditor_script(AUDIT_CLEAN))
        task = next(t for t in builtin_tasks() if t.id == "toyrepo-planted-bug")
        await run_task(
            task,
            store,
            autodiscover(),
            eval_world["catalog"],
            sqlite_checkpointer_factory(eval_world["ck"]),
            eval_world["workroot"],
            provider=provider,
        )
        await asyncio.sleep(0.01)  # distinct stamps so history order is stable

    history = await score_history(store, None)
    assert len(history) == 2
    missions = [h["mission"] for h in history]
    assert len(set(missions)) == 2  # separate eval missions, one row each
