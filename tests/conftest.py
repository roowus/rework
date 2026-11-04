"""Shared fixtures. Store conformance parametrizes over available backends:
SQLite always; Postgres when REWORK_TEST_POSTGRES is set (CI sets it).

Add a backend: implement the store, then extend `BACKENDS` + `_make_store`.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from rework.kernel.llm.types import ChatResult, ToolCall, Usage
from rework.kernel.state.store import StateStore

if TYPE_CHECKING:
    from rework.kernel.llm.catalog import ModelCatalog
    from rework.kernel.llm.types import ChatRequest

import json

BACKENDS: list[str] = ["sqlite"]
if os.environ.get("REWORK_TEST_POSTGRES"):
    BACKENDS.append("postgres")

_TEST_PG_URL = os.environ.get("REWORK_TEST_POSTGRES", "")


def make_git_repo(tmp_path: Path) -> Path:
    """Tiny git repo with one commit on main, for role tests that need one."""
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    (repo / "README.md").write_text("# repo\n")
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=r@r", "-c", "user.name=r", "commit", "-m", "init"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return repo


def role_catalog() -> ModelCatalog:
    """Catalog covering every builtin role's default model id (compile canaries)."""
    from rework.kernel.llm.catalog import ModelCatalog, ModelEntry

    return ModelCatalog(
        [
            ModelEntry(id="flash", provider="fake", model="fake-flash"),
            ModelEntry(id="pro", provider="fake", model="fake-pro"),
            ModelEntry(id="fake", provider="fake", model="fake-1"),
        ]
    )


class ScriptedProvider:
    """Routes canned responses by substring of the system prompt (the GOAL
    line), so one provider plays every role in a multi-role mission test."""

    name = "scripted"

    def __init__(self) -> None:
        self._queues: dict[str, list[ChatResult]] = {}
        self.requests: list[ChatRequest] = []

    def route(self, key: str, responses: list[ChatResult]) -> None:
        self._queues.setdefault(key, []).extend(responses)

    async def complete(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        for key, queue in self._queues.items():
            if key in (request.system or ""):
                if not queue:
                    raise AssertionError(f"script exhausted for route {key!r}")
                return queue.pop(0)
        raise AssertionError("no script matched this system prompt")


async def _make_store(backend: str, tmp_path: Path) -> StateStore:
    if backend == "sqlite":
        from rework.kernel.state.sqlite import open_sqlite

        return await open_sqlite(str(tmp_path / "test.sqlite3"))
    if backend == "postgres":
        from rework.kernel.state.postgres import PostgresStore, open_postgres

        store = await open_postgres(_TEST_PG_URL)
        assert isinstance(store, PostgresStore)
        await store.reset_for_tests()  # shared dev database — isolate every test
        return store
    raise ValueError(f"unknown backend {backend}")


@pytest.fixture(params=BACKENDS)
async def store(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[StateStore]:
    s = await _make_store(request.param, tmp_path)
    yield s
    await s.aclose()


INV_REPORT = json.dumps(
    {
        "summary": "add() subtracts instead of adding: calc.py line 4 returns a - b.",
        "findings": [
            {
                "title": "add returns a - b",
                "detail": (
                    "The planted bug: add(2,3) returns -1. mul() compounds it but"
                    " its tests pass anyway by luck of the implementation."
                ),
                "evidence": ["calc.py:4"],
                "severity": "high",
                "verdict": "CONFIRMED",
                "confidence": 0.95,
            }
        ],
        "suggested_fix": "Change `return a - b` to `return a + b` in calc.py.",
        "proposed_tests": [
            {"name": "test_add", "exercises": "add(2, 3) == 5 already in test_calc.py"}
        ],
        "blockers": [],
    }
)

AUDIT_CLEAN = json.dumps(
    {
        "summary": "Fix is correct: add() now adds; mul() unaffected and still passing.",
        "findings": [],
        "standing_assessments": {
            "proportionality": "proportionate (no accessory)",
            "complexity": "trivial, appropriate",
            "priority": "appropriate",
            "coverage": "adequate: existing tests pin the change",
            "business_logic_risk": "none",
        },
        "blockers": [],
    }
)

AUDIT_VETO = json.dumps(
    {
        "summary": "Requested changes: the diff is right but no test run was shown.",
        "findings": [
            {
                "title": "No verification evidence",
                "detail": "The handoff summary does not show the test suite passing on the branch.",
                "evidence": [],
                "severity": "critical",
                "verdict": "PLAUSIBLE",
                "confidence": 0.4,
            }
        ],
        "standing_assessments": {
            "proportionality": "proportionate",
            "complexity": "trivial, appropriate",
            "priority": "appropriate",
            "coverage": "cannot confirm",
            "business_logic_risk": "none",
        },
        "blockers": [],
    }
)


def _coder_script(final_summary: str) -> list[ChatResult]:
    return [
        ChatResult(
            text="fixing",
            tool_calls=[
                ToolCall(
                    id="c1",
                    name="run_command",
                    arguments={
                        "command": "printf 'def add(a, b):\\n    return a + b\\n' > calc.py"
                    },
                )
            ],
            usage=Usage(input_tokens=10, output_tokens=5),
            stop_reason="tool_calls",
        ),
        ChatResult(
            text="verifying",
            tool_calls=[
                ToolCall(id="c2", name="run_command", arguments={"command": "python3 -m pytest -q"})
            ],
            usage=Usage(input_tokens=10, output_tokens=5),
            stop_reason="tool_calls",
        ),
        ChatResult(
            text=final_summary,
            usage=Usage(input_tokens=10, output_tokens=8),
            stop_reason="end_turn",
        ),
    ]


def _auditor_script(report_json: str) -> list[ChatResult]:
    return [
        ChatResult(
            text="reading the diff and code",
            tool_calls=[ToolCall(id="a1", name="read_file", arguments={"path": "calc.py"})],
            usage=Usage(input_tokens=20, output_tokens=10),
            stop_reason="tool_calls",
        ),
        ChatResult(
            text="analysis done",
            usage=Usage(input_tokens=15, output_tokens=8),
            stop_reason="end_turn",
        ),
        ChatResult(text=report_json, usage=Usage(input_tokens=40, output_tokens=50)),
    ]


def _investigator_script() -> list[ChatResult]:
    return [
        ChatResult(
            text="reading the file",
            tool_calls=[ToolCall(id="i1", name="read_file", arguments={"path": "calc.py"})],
            usage=Usage(input_tokens=20, output_tokens=10),
            stop_reason="tool_calls",
        ),
        ChatResult(
            text="add() subtracts; that is the bug.",
            usage=Usage(input_tokens=15, output_tokens=8),
            stop_reason="end_turn",
        ),
        ChatResult(text=INV_REPORT, usage=Usage(input_tokens=40, output_tokens=50)),
    ]
