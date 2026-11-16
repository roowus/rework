"""Realistic seed data for TUI tests and screenshots (tmp SQLite only).

Never point this at a deployment database: it writes rows."""

from __future__ import annotations

import json
import random
import time
from pathlib import Path
from typing import Any

from rework.kernel.state.records import (
    Charge,
    Chat,
    ChatMessage,
    EventRecord,
    EventType,
    Gate,
    GateKind,
    Mission,
    MissionStatus,
    Project,
    Run,
    RunStatus,
)

DAY = 86400.0


def _traj(mid: str, run: str, kind: str, ts: float, **payload: Any) -> EventRecord:
    return EventRecord(
        mission_id=mid,
        run_id=run,
        type=EventType.PROGRESS,
        payload={"trajectory": kind, **payload},
        ts=ts,
    )


async def seed_library(db_path: str, root: str) -> list[str]:
    """A few library items (git body + index row), via the real write path."""
    from rework.library.curation import contribute
    from rework.library.model import ItemType
    from rework.library.store import GitLibrary, open_library_store

    index = await open_library_store(db_path)
    git = GitLibrary(Path(root), author="rework <rework@test.local>")
    ids = []
    try:
        for type_, name, desc, body, tags in [
            (
                ItemType.SKILL,
                "pytest-flaky-triage",
                "How to pin down a flaky pytest: reruns, seeds, float tolerances",
                "# Flaky test triage\n\n1. Run with `-p no:randomly` and `--count 20`\n"
                "2. Compare floats with `assert_allclose`\n",
                ["pytest", "flaky"],
            ),
            (
                ItemType.MEMORY,
                "polystats uses numpy 2",
                "polystats pins numpy>=2; window edges changed in 2.0",
                "polystats requires numpy 2.x.",
                ["polystats"],
            ),
            (
                ItemType.TOOL,
                "run-benchmarks",
                "Runs the polystats benchmark suite and reports regressions",
                "```bash\npytest benchmarks/ --benchmark-only\n```",
                ["bench"],
            ),
        ]:
            item, _note = await contribute(
                index, git, type_=type_, name=name, description=desc, body=body, tags=tags
            )
            ids.append(item.id)
    finally:
        await index.aclose()
    return ids


async def seed(db_path: str, *, history_days: int = 120) -> dict[str, str]:
    from rework.kernel.state.sqlite import open_sqlite

    rng = random.Random(7)
    now = time.time()
    store = await open_sqlite(db_path)
    ids: dict[str, str] = {}
    try:
        poly = await store.create_project(
            Project(
                name="polystats",
                settings={
                    "transport": "git",
                    "repos": [
                        {
                            "name": "polystats",
                            "kind": "git",
                            "url": "https://github.com/x/polystats",
                        }
                    ],
                },
            )
        )
        rw = await store.create_project(Project(name="rework", settings={"repos": []}))
        ids["upload_project"] = rw.id
        ids["project"] = poly.id

        # -- a running mission with a live coder -------------------------------
        running = await store.create_mission(
            Mission(
                id="m-run0000000a",
                objective="fix the flaky test in test_rolling_mean (tolerance is too tight)",
                project_id=poly.id,
                status=MissionStatus.RUNNING,
                plan={
                    "kind": "fix",
                    "steps": [
                        {"step": "investigate", "role": "investigator"},
                        {"step": "code", "role": "coder"},
                        {"step": "audit", "role": "auditor"},
                    ],
                    "gates": ["ship"],
                },
                created_at=now - 600,
            )
        )
        ids["running"] = running.id
        inv = f"{running.id}:investigate:investigator:1"
        cod = f"{running.id}:code:coder:1"
        await store.create_run(
            Run(
                id=inv,
                mission_id=running.id,
                role="investigator",
                step="investigate",
                status=RunStatus.COMPLETED,
                model_ref={"id": "flash"},
                artifact={
                    "summary": "The rolling-mean test compares floats with ==; window edge "
                    "handling differs between numpy versions.",
                    "findings": [
                        {
                            "title": "Exact float comparison",
                            "detail": "assert result == expected on a float array",
                            "severity": "high",
                            "evidence": ["tests/test_stats.py:41"],
                            "confidence": 0.9,
                        },
                        {
                            "title": "Edge window off by one",
                            "detail": "min_periods default changed",
                            "severity": "medium",
                            "evidence": ["polystats/rolling.py:88"],
                            "confidence": 0.6,
                        },
                    ],
                    "suggested_fix": "use numpy.testing.assert_allclose with rtol=1e-9",
                    "run_stats": {"model_calls": 6, "wall_s": 74.2},
                },
            )
        )
        await store.create_run(
            Run(
                id=cod,
                mission_id=running.id,
                role="coder",
                step="code",
                status=RunStatus.RUNNING,
                model_ref={"id": "flash", "effort": "medium"},
            )
        )
        t = now - 590
        evs = [
            EventRecord(
                mission_id=running.id,
                type=EventType.MISSION_CREATED,
                payload={"objective": running.objective, "plan": "fix"},
                ts=t,
            ),
            EventRecord(
                mission_id=running.id,
                run_id=inv,
                type=EventType.ROLE_SPAWNED,
                payload={"role": "investigator"},
                ts=t + 1,
            ),
            _traj(
                running.id,
                inv,
                "turn",
                t + 5,
                turn=1,
                text="Let me find the flaky test and see how it compares values.",
                tools=["grep"],
                tokens=2100,
            ),
            _traj(
                running.id, inv, "tool_call", t + 6, name="grep", arguments="pattern=rolling_mean"
            ),
            _traj(
                running.id,
                inv,
                "tool_result",
                t + 7,
                name="grep",
                result="tests/test_stats.py:41: assert result == expected\npolystats/rolling.py:88",
                error="",
            ),
            EventRecord(
                mission_id=running.id,
                run_id=inv,
                type=EventType.ROLE_COMPLETED,
                payload={"role": "investigator"},
                ts=t + 80,
            ),
            EventRecord(
                mission_id=running.id,
                run_id=cod,
                type=EventType.ROLE_SPAWNED,
                payload={
                    "role": "coder",
                    "graph": {
                        "nodes": ["__start__", "prepare", "loop", "handoff", "__end__"],
                        "edges": [
                            ["__start__", "prepare", False],
                            ["prepare", "loop", False],
                            ["loop", "handoff", False],
                            ["handoff", "__end__", False],
                        ],
                    },
                },
                ts=t + 82,
            ),
            EventRecord(
                mission_id=running.id,
                run_id=cod,
                type=EventType.PROGRESS,
                payload={"node": "prepare", "phase": "enter"},
                ts=t + 82.5,
            ),
            EventRecord(
                mission_id=running.id,
                run_id=cod,
                type=EventType.PROGRESS,
                payload={"node": "loop", "phase": "enter"},
                ts=t + 85,
            ),
            EventRecord(
                mission_id=running.id,
                run_id=cod,
                type=EventType.PROGRESS,
                payload={"note": "worktree ready on rework/m-run0000000a"},
                ts=t + 84,
            ),
            _traj(
                running.id,
                cod,
                "turn",
                t + 90,
                turn=1,
                text="I'll read the test first, then switch to assert_allclose.",
                tools=["read_file"],
                tokens=3800,
            ),
            _traj(
                running.id,
                cod,
                "tool_call",
                t + 91,
                name="read_file",
                arguments="path=tests/test_stats.py",
            ),
            _traj(
                running.id,
                cod,
                "tool_result",
                t + 92,
                name="read_file",
                result="import numpy as np\nfrom polystats import rolling_mean\n\n"
                "def test_rolling_mean():\n    result = rolling_mean([1, 2, 3], 2)\n"
                "    assert result == expected",
                error="",
            ),
            _traj(
                running.id,
                cod,
                "turn",
                t + 120,
                turn=2,
                text="Replacing the exact comparison and running the suite.",
                tools=["edit_file", "run_command"],
                tokens=5200,
            ),
            _traj(
                running.id,
                cod,
                "tool_call",
                t + 121,
                name="run_command",
                arguments="command=pytest -q tests/test_stats.py",
            ),
            _traj(
                running.id,
                cod,
                "tool_result",
                t + 140,
                name="run_command",
                result="(exit 1)\nFAILED tests/test_stats.py::test_window_edge",
                error="exit 1",
            ),
        ]
        for ev in evs:
            await store.append_event(ev)

        # -- a mission waiting on a ship gate ----------------------------------
        waiting = await store.create_mission(
            Mission(
                id="m-gate000000b",
                objective="add dark mode to the settings page",
                project_id=poly.id,
                status=MissionStatus.AWAITING_GATE,
                plan={"kind": "fix", "steps": [{"step": "code", "role": "coder"}]},
                created_at=now - 3600 * 3,
            )
        )
        ids["waiting"] = waiting.id
        wcode = f"{waiting.id}:code:coder:1"
        await store.create_run(
            Run(
                id=wcode,
                mission_id=waiting.id,
                role="coder",
                step="code",
                status=RunStatus.COMPLETED,
                artifact={
                    "kind": "coder_handoff",
                    "branch": "rework/m-gate000000b",
                    "commit": "a1b2c3d4e5",
                    "summary": "Added a theme toggle and CSS variables.",
                    "diff": "diff --git a/app/settings.py b/app/settings.py\n"
                    "--- a/app/settings.py\n+++ b/app/settings.py\n@@ -1,3 +1,5 @@\n"
                    " class Settings:\n-    theme = 'light'\n+    theme = 'system'\n"
                    "+    dark_mode = True\n",
                },
            )
        )
        gate = await store.create_gate(
            Gate(
                id="gate-ship00000b",
                mission_id=waiting.id,
                kind=GateKind.SHIP,
                question="Ship the mission result? audit verdict: approve",
                for_run=wcode,
                created_at=now - 1200,
            )
        )
        ids["gate"] = gate.id
        await store.append_event(
            EventRecord(
                mission_id=waiting.id,
                type=EventType.GATE_OPENED,
                payload={"gate_id": gate.id, "kind": "ship", "question": gate.question},
                ts=now - 1200,
            )
        )
        q = await store.create_gate(
            Gate(
                id="gate-quest0000c",
                mission_id=running.id,
                kind=GateKind.GENERIC,
                question="Two tests use the old tolerance. Should I loosen both, or only "
                "the flaky one?",
                for_run=cod,
                created_at=now - 300,
            )
        )
        ids["question_gate"] = q.id

        # -- history ------------------------------------------------------------
        for i, (status, obj) in enumerate(
            [
                (MissionStatus.DONE, "make add(2, 3) == 5: calc.py's add() subtracts"),
                (MissionStatus.FAILED, "migrate the CI to uv"),
                (MissionStatus.ON_HOLD, "audit the auth module for token leaks"),
                (MissionStatus.DONE, "document the workspace API"),
                (MissionStatus.CANCELLED, "rewrite the parser in rust"),
            ]
        ):
            await store.create_mission(
                Mission(
                    id=f"m-hist{i:08d}",
                    objective=obj,
                    project_id=[poly.id, rw.id, None][i % 3],
                    status=status,
                    plan={"kind": "fix"},
                    created_at=now - DAY * (i + 1),
                )
            )

        models = [
            "accounts/fireworks/models/deepseek-v4p1-flash",
            "accounts/fireworks/models/kimi-k3",
        ]
        for d in range(history_days):
            if d > 0 and rng.random() < 0.35:
                continue
            n = rng.randint(1, 8)
            for _ in range(n):
                model = rng.choice(models)
                tin = rng.randint(2000, 60000)
                tout = rng.randint(200, 4000)
                await store.charge(
                    Charge(
                        mission_id=running.id if d < 1 else f"m-hist{d % 5:08d}",
                        run_id=cod if d < 1 else None,
                        model=model,
                        input_tokens=tin,
                        output_tokens=tout,
                        cost_usd=tin * 3e-7 + tout * 1.2e-6,
                        ts=now - d * DAY - rng.randint(0, 3600 * 8),
                    )
                )

        # -- a saved chat that launched the running mission ---------------------
        chat = await store.create_chat(
            Chat(
                title="flaky rolling mean test",
                project_id=poly.id,
                model_id="pro",
                mission_id=running.id,
            )
        )
        ids["chat"] = chat.id
        msgs = [
            ChatMessage(
                chat_id=chat.id,
                role="user",
                body={"role": "user", "content": "the rolling mean test is flaky again, fix it"},
                display="the rolling mean test is flaky again, fix it",
            ),
            ChatMessage(
                chat_id=chat.id,
                role="tool",
                body={
                    "role": "tool",
                    "name": "list_missions",
                    "content": "m-hist00000000 [done] make add(2, 3) == 5",
                },
            ),
            ChatMessage(
                chat_id=chat.id,
                role="assistant",
                body={
                    "role": "assistant",
                    "content": "No mission touched it yet. Here's what I'd run.",
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {
                                "name": "propose_mission",
                                "arguments": json.dumps(
                                    {
                                        "objective": running.objective,
                                        "plan": "fix",
                                        "project": "polystats",
                                    }
                                ),
                            },
                        }
                    ],
                },
                display="No mission touched it yet. Here's what I'd run.",
            ),
            ChatMessage(
                chat_id=chat.id,
                role="user",
                body={"role": "user", "content": "yes"},
                display="yes",
            ),
            ChatMessage(chat_id=chat.id, role="note", body={}, display=f"launched {running.id}"),
        ]
        for msg in msgs:
            await store.append_chat_message(msg)
        # a plain human chat with one message: the Work list HIDES chats that
        # never got a message (an auto-created shell looks like a broken chat),
        # so a fixture that must show up in the list needs a row of its own
        audit = await store.create_chat(
            Chat(title="what did the auth audit find?", model_id="flash")
        )
        await store.append_chat_message(
            ChatMessage(
                chat_id=audit.id,
                role="assistant",
                body={"role": "assistant", "content": "the audit found nothing alarming"},
                display="the audit found nothing alarming",
            )
        )
    finally:
        await store.aclose()
    return ids
