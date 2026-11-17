"""Shared fixtures. Store conformance parametrizes over available backends:
SQLite always; Postgres when REWORK_TEST_POSTGRES is set (CI sets it).

Add a backend: implement the store, then extend `BACKENDS` + `_make_store`.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from rework.kernel.llm.types import ChatResult, ToolCall, Usage
from rework.kernel.state.records import MissionStatus, RunStatus
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


# The one bound every gate-park wait in the suite uses. It exists to turn a
# genuine hang (a park that never lands) into a loud TimeoutError; it is not a
# budget for a slow write. On a starved host (20 pytest processes on 2 cores) a
# park that takes ~1.6s serially has been observed to take tens of seconds, so a
# bound sized for the serial case fires on state that is merely late. CI runs
# pytest serially, where a real park is a couple of seconds and this is never
# approached.
GATE_PARK_TIMEOUT_S = 60.0


async def _wait_for(
    predicate: Callable[[], Awaitable[bool]],
    timeout_s: float = GATE_PARK_TIMEOUT_S,
    what: str = "condition",
) -> None:
    """Poll `predicate` on a fresh read until it holds, or raise TimeoutError.

    Every call polls: nothing here sleeps blindly and nothing assumes a write
    made by another coroutine is already visible."""
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.05)
    raise TimeoutError(f"timed out waiting for {what}")


async def wait_for_gate_park(
    store: Any,
    mission_id: str,
    *,
    mission_status: MissionStatus | None = None,
    run_id: str | None = None,
    timeout_s: float = GATE_PARK_TIMEOUT_S,
    what: str = "",
) -> None:
    """Wait until a mission is PARKED at its gate, not merely "a gate exists".

    The engine commits the gate row in one autocommit statement and the fact a
    test then relies on in a LATER one: the mission status AWAITING_GATE
    (`_final_gates` for ship/open_pr, `_handle_halted` for an escalation), or
    the gated run's HALTED_ESCALATED row (written in `_consume_result`, before
    `_handle_halted` opens the gate for that run). A reader therefore CAN see an
    open gate beside a `running` mission, or beside a run that is not parked
    yet. Waiting on the gate row alone is a race: under CPU load the poll wins,
    the test cancels or restarts early, and the next assertion reads the
    pre-park state (or the restart opens a duplicate gate).

    So: ONE bounded wait, ONE combined predicate, and every iteration re-reads
    through `store` (never a value captured before the wait):

    - an open gate exists for the mission, AND
    - `mission_status`, when given, is the mission's status, AND
    - `run_id`, when given, is a run whose status is HALTED_ESCALATED.

    `what` names the wait in the TimeoutError and defaults to a description of
    the facts asked for."""
    label = what or (
        f"gate open for {mission_id}"
        + (f" and mission {mission_status.value}" if mission_status is not None else "")
        + (f" and run {run_id} halted_escalated" if run_id is not None else "")
    )

    async def parked() -> bool:
        if not await store.list_open_gates(mission_id):
            return False
        if mission_status is not None:
            mission = await store.get_mission(mission_id)
            if mission is None or mission.status != mission_status:
                return False
        if run_id is not None:
            run = await store.get_run(run_id)
            if run is None or run.status != RunStatus.HALTED_ESCALATED:
                return False
        return True

    await _wait_for(parked, timeout_s=timeout_s, what=label)


def role_catalog() -> ModelCatalog:
    """Catalog covering every builtin role's default model id (compile canaries).
    Tiers matter: promotion (needs_model policy) walks them."""
    from rework.kernel.llm.catalog import ModelCatalog, ModelEntry

    return ModelCatalog(
        [
            ModelEntry(id="flash", provider="fake", model="fake-flash", tier=1),
            ModelEntry(id="pro", provider="fake", model="fake-pro", tier=2),
            ModelEntry(id="fake", provider="fake", model="fake-1", tier=0),
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
        "tests": [
            {
                "name": "test_add",
                "what_it_pins": "add(2, 3) == 5",
                "where": "test_calc.py",
                "should_fail_because": "add returns a - b, so it gives -1",
                "command": "python3 -m pytest -q test_calc.py",
            }
        ],
        "no_tests_reason": "",
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
        "coverage": [],
        "warnings": [],
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
        "coverage": [],
        "warnings": [],
        "blockers": [],
    }
)


CALC_FIXED = (
    "def add(a, b):\n"
    "    return a + b\n"
    "\n"
    "def mul(a, b):\n"
    "    result = 0\n"
    "    for _ in range(abs(int(b))):\n"
    "        result = add(result, a)\n"
    "    return result if b >= 0 else -result\n"
)


CODER_TESTS_BLOCK = (
    "\n\n```json\n"
    + json.dumps(
        {
            "tests": {
                "added": [],
                "changed": [],
                "red_green_verified": True,
                "commands": ["python3 -m pytest -q"],
                "result": "red then green",
                "not_testable_reason": "",
            }
        }
    )
    + "\n```"
)


def _coder_script(final_summary: str, tests_block: str = CODER_TESTS_BLOCK) -> list[ChatResult]:
    # the command writes the WHOLE file: the eval grader re-runs pytest on the
    # worktree mechanically, and a fix that drops mul() must fail it (it did)
    heredoc_cmd = "cat > calc.py << 'REWORK_EOF'\n" + CALC_FIXED + "REWORK_EOF"
    return [
        ChatResult(
            text="fixing",
            tool_calls=[
                ToolCall(
                    id="c1",
                    name="run_command",
                    arguments={"command": heredoc_cmd},
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
            text=final_summary + tests_block,
            usage=Usage(input_tokens=10, output_tokens=8),
            stop_reason="end_turn",
        ),
    ]


def _coder_script_broken_writes_whole_file() -> list[ChatResult]:
    """The historical failure mode: a 'fix' that rewrites calc.py WITHOUT mul().
    The grader's mechanical pytest re-run must fail it."""
    broken = (
        '"""Tiny calculator."""\n\n\n'
        "def add(a, b):\n"
        "    return a + b\n"  # mul GONE
    )
    heredoc_cmd = "cat > calc.py << 'REWORK_EOF'\n" + broken + "REWORK_EOF"
    return [
        ChatResult(
            text="fixing",
            tool_calls=[ToolCall(id="c1", name="run_command", arguments={"command": heredoc_cmd})],
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
            text="done (mul dropped by mistake)",
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


# -- Library index fixture (M5): sqlite always, postgres when REWORK_TEST_POSTGRES
# is set (pointed at the dedicated rework_test DB, never the deployment database)

BACKENDS_LIB = ["sqlite"]
_TEST_PG_LIB = os.environ.get("REWORK_TEST_POSTGRES")
if _TEST_PG_LIB:
    BACKENDS_LIB.append("postgres")


@pytest.fixture(params=BACKENDS_LIB)
async def library_index(request: pytest.FixtureRequest) -> AsyncIterator[object]:
    if request.param == "postgres":
        import psycopg
        from psycopg.rows import dict_row

        from rework.library.store import (
            _DDL,
            PostgresLibraryStore,
            apply_library_migrations,
        )

        assert _TEST_PG_LIB
        conn = await psycopg.AsyncConnection.connect(
            _TEST_PG_LIB, autocommit=False, row_factory=dict_row
        )
        for ddl in _DDL:
            await conn.execute(ddl)
        await apply_library_migrations(conn)
        await conn.execute("TRUNCATE TABLE library_items")
        await conn.commit()
        store = PostgresLibraryStore(conn)
        yield store
        await store.aclose()
    else:
        import asyncio as _asyncio
        import tempfile

        import aiosqlite

        from rework.library.store import _SQLITE_DDL, SqliteLibraryStore

        tmp = Path(tempfile.mkdtemp()) / "lib.sqlite3"

        def _prep() -> str:
            tmp.parent.mkdir(parents=True, exist_ok=True)
            return str(tmp)

        conn = await aiosqlite.connect(await _asyncio.to_thread(_prep))
        conn.row_factory = aiosqlite.Row
        for ddl in _SQLITE_DDL:
            await conn.execute(ddl)
        await conn.commit()
        store = SqliteLibraryStore(conn)
        yield store
        await store.aclose()


@pytest.fixture(autouse=True)
def _isolated_command_registry(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run_command records process groups under REWORK_RUN_DIR; never ~/.rework."""
    monkeypatch.setenv("REWORK_RUN_DIR", str(tmp_path_factory.mktemp("run")))


@pytest.fixture(scope="session")
def log_isolation(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The one log directory this test session may write into."""
    return tmp_path_factory.mktemp("logs")


@pytest.fixture(scope="session")
def _log_baseline() -> list[logging.Handler]:
    """The root handlers pytest itself installed, before any setup_logging()."""
    return list(logging.getLogger().handlers)


def reset_logging(baseline: list[logging.Handler]) -> None:
    """Detach every handler setup_logging() attached and forget the components it
    configured. `_configured` is the trap: setup_logging() returns EARLY for an
    already-configured component and attaches no new handler, so a stale handler
    keeps writing to the directory of the test that installed it."""
    from rework import logs

    root = logging.getLogger()
    for handler in list(root.handlers):
        if handler not in baseline:
            root.removeHandler(handler)
            handler.close()
    logs._configured.clear()


@pytest.fixture
def reset_logs(_log_baseline: list[logging.Handler]) -> Callable[[], None]:
    """reset_logging for tests that must exercise setup_logging() more than once."""
    return lambda: reset_logging(_log_baseline)


@pytest.fixture(autouse=True)
def _isolated_logs(
    monkeypatch: pytest.MonkeyPatch,
    log_isolation: Path,
    _log_baseline: list[logging.Handler],
) -> Iterator[None]:
    """No test may write into the owner's real ~/.rework/logs. An entrypoint
    driven in-process (run_tui() in tests/tui/test_reload.py) calls
    setup_logging(), which falls back to DEFAULT_STATE_DIR/logs when
    REWORK_LOG_DIR is unset and appends to the real tui.log."""
    monkeypatch.setenv("REWORK_LOG_DIR", str(log_isolation))
    reset_logging(_log_baseline)
    yield
    reset_logging(_log_baseline)
