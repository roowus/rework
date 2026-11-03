"""run_stats stamping (mechanical, ledger-derived) + the report command."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rework.cli import app
from rework.kernel.state.records import (
    Charge,
    Mission,
    Run,
    RunStatus,
    new_id,
    run_id,
)
from rework.kernel.state.sqlite import open_sqlite
from rework.nodes.run_stats import attach_run_stats


async def test_attach_run_stats_from_ledger(tmp_path: Path) -> None:
    store = await open_sqlite(str(tmp_path / "rs.sqlite3"))
    try:
        await store.create_mission(Mission(id="mrs", objective="x"))
        rid = run_id("mrs", "audit", "auditor", 1)
        await store.create_run(
            Run(id=rid, mission_id="mrs", role="auditor", step="audit", status=RunStatus.COMPLETED)
        )
        await store.charge(
            Charge(
                mission_id="mrs",
                run_id=rid,
                model="glm/pro",
                input_tokens=1000,
                output_tokens=200,
                cached_tokens=700,
                cost_usd=0.01,
            )
        )
        await store.charge(
            Charge(
                mission_id="mrs",
                run_id=rid,
                model="glm/pro",
                input_tokens=500,
                output_tokens=100,
                cached_tokens=0,
                cost_usd=0.005,
            )
        )

        artifact = await attach_run_stats(store, rid, {"summary": "s", "findings": []})
        stats = artifact["run_stats"]
        assert stats["model_calls"] == 2
        assert stats["input_tokens"] == 1500
        assert stats["output_tokens"] == 300
        assert stats["cached_tokens"] == 700
        assert stats["cost_usd"] == pytest.approx(0.015)
        assert stats["elapsed_seconds"] >= 0
        assert "glm/pro" in stats["models"]
    finally:
        await store.aclose()


async def test_report_command_renders_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "cli.sqlite3"
    store = await open_sqlite(str(db))
    try:
        mission = await store.create_mission(Mission(id=new_id("m"), objective="demo"))
        rid = run_id(mission.id, "investigate", "investigator", 1)
        await store.create_run(
            Run(
                id=rid,
                mission_id=mission.id,
                role="investigator",
                step="investigate",
                status=RunStatus.COMPLETED,
                artifact={
                    "summary": "found it",
                    "findings": [
                        {
                            "title": "the bug",
                            "detail": "add subtracts",
                            "evidence": ["calc.py:5"],
                            "severity": "high",
                            "verdict": "CONFIRMED",
                            "confidence": 0.9,
                        }
                    ],
                    "blockers": [],
                },
            )
        )
        await store.charge(
            Charge(
                mission_id=mission.id,
                run_id=rid,
                model="glm/flash",
                input_tokens=100,
                output_tokens=20,
                cached_tokens=50,
                cost_usd=0.001,
            )
        )
    finally:
        await store.aclose()

    monkeypatch.setenv("REWORK_DB", str(db))
    # the real CLI runs asyncio.run() in a bare interpreter; this test is
    # already inside pytest-asyncio's loop, so invoke from a worker thread
    result = await asyncio.to_thread(CliRunner().invoke, app, ["report", mission.id])
    assert result.exit_code == 0
    assert "found it" in result.output
    assert "the bug" in result.output
    assert "CONFIRMED" in result.output
    assert "run stats" in result.output  # live-computed for old artifacts
    assert "cached: 50" in result.output
