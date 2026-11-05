"""Eval-in-CI (M6.2): the toyrepo task through the REAL engine on a scripted
provider. Zero cost, zero network, runs in the normal `uv run pytest` pass:
a score regression (engine/role/grader drift) fails the build like any test.

This is the offline rung of the eval ladder; the real-model smoke (REWORK_
SMOKE=1) stays manual. The history-recording half is asserted too: an eval
run must leave a score event on the sentinel mission."""

from __future__ import annotations

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
from rework.evals.harness import builtin_tasks, run_task
from rework.kernel.state.records import EventType
from rework.kernel.state.sqlite import SqliteStore, open_sqlite
from rework.roles.registry import autodiscover


@pytest.fixture
async def eval_world(tmp_path: Path) -> AsyncIterator[dict[str, Any]]:
    store: SqliteStore = await open_sqlite(str(tmp_path / "eval.sqlite3"))
    provider = ScriptedProvider()
    provider.route("Investigate any brief", _investigator_script())
    provider.route("Fulfill the task", _coder_script("tests pass: 2/2 green, add fixed"))
    provider.route("Find issues", _auditor_script(AUDIT_CLEAN))
    try:
        yield {
            "store": store,
            "provider": provider,
            "ck": str(tmp_path / "ck.sqlite3"),
            "workroot": tmp_path / "work",
        }
    finally:
        await store.aclose()


def _checkpointer(world: dict[str, Any]) -> Any:
    from rework.orchestrator.daemon import sqlite_checkpointer_factory

    return sqlite_checkpointer_factory(world["ck"])


async def test_toyrepo_task_through_real_engine(eval_world: dict[str, Any]) -> None:
    """Score 1.0 on the scripted happy path: engine + grader + history wired."""
    task = builtin_tasks()[0]
    score = await run_task(
        task,
        eval_world["store"],
        autodiscover(),
        role_catalog(),
        _checkpointer(eval_world),
        eval_world["workroot"],
        provider=eval_world["provider"],
    )
    assert score.passed, score.grade
    assert score.grade["tests_pass"] is True
    assert score.grade["audit_verdict"] == "approve"
    assert score.cost_usd == 0.0  # scripted: CI evals never spend

    # history recorded on the sentinel mission
    from rework.evals.harness import score_history

    history = await score_history(eval_world["store"], task.id)
    assert history and history[-1]["passed"] is True


async def test_grader_fails_a_broken_coder(eval_world: dict[str, Any]) -> None:
    """A coder whose diff drops mul() must NOT pass the grader (the live eval
    lesson: printf-style whole-file writes once shipped this)."""
    from conftest import _coder_script_broken_writes_whole_file

    task = builtin_tasks()[0]
    provider = ScriptedProvider()
    provider.route("Investigate any brief", _investigator_script())
    provider.route("Fulfill the task", _coder_script_broken_writes_whole_file())
    provider.route("Find issues", _auditor_script(AUDIT_CLEAN))
    score = await run_task(
        task,
        eval_world["store"],
        autodiscover(),
        role_catalog(),
        _checkpointer(eval_world),
        eval_world["workroot"],
        provider=provider,
    )
    assert score.passed is False
    assert score.grade["tests_pass"] is False


async def test_history_events_are_queryable(eval_world: dict[str, Any]) -> None:
    store: SqliteStore = eval_world["store"]
    events = await store.events_since(0, limit=10)
    _ = events  # smoke: the store path works before any eval ran
    assert EventType.INFO.value == "info"
