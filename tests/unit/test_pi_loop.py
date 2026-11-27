"""The pi loop (ADR 0022) against a FAKE `pi` that replays a recorded --mode
json stream: no real pi or model in CI. Pins the contract a role relies on:
the same output keys as the native loop, charges priced by rework's catalog,
trajectory events, the budget stop, and a clean failure."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from rework.kernel.budget.ledger import Budget
from rework.kernel.state.records import Mission
from rework.kernel.state.sqlite import open_sqlite
from rework.nodes import pi_loop


def _assistant(text: str, inp: int, out: int, stop: str = "stop", err: str = "") -> dict[str, Any]:
    return {
        "type": "message_end",
        "message": {
            "role": "assistant",
            "model": "glm/test",
            "content": [{"type": "text", "text": text}] if text else [],
            "usage": {
                "input": inp,
                "output": out,
                "cacheRead": 0,
                "totalTokens": inp + out,
                "cost": {"total": 0},
            },
            "stopReason": stop,
            **({"errorMessage": err} if err else {}),
        },
    }


def _fake_pi(tmp: Path, events: list[dict[str, Any]], sleep_s: float = 0.0) -> Path:
    stream = tmp / "stream.jsonl"
    stream.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    script = tmp / "pi"
    script.write_text(
        "#!/bin/sh\n"
        f'echo "$@" > {tmp}/argv.txt\n'
        f'while IFS= read -r line; do echo "$line"; sleep {sleep_s}; done < {stream}\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


async def _run(
    tmp: Path, events: list[dict[str, Any]], *, cap: float | None = None, sleep_s: float = 0.0
) -> tuple[dict[str, Any], Any]:
    os.environ["REWORK_PI_BIN"] = str(_fake_pi(tmp, events, sleep_s))
    store = await open_sqlite(str(tmp / "s.sqlite3"))
    await store.create_mission(Mission(id="m-pi", objective="x"))
    node = pi_loop.make_pi_loop_node(
        store=store,
        bus=None,
        mission_id="m-pi",
        run_id="m-pi:s:investigator:1",
        model_id="glm/test",
        system_prompt="SYS",
        workdir=tmp,
        budget=Budget(max_usd=cap),
        env=dict(os.environ),
        price=lambda i, o, c: (i + o) / 1000,
    )
    out = await node({"messages": [{"role": "user", "content": "brief"}]})
    return out, store


async def test_same_output_shape_as_the_native_loop(tmp_path: Path) -> None:
    events = [
        {"type": "tool_execution_start", "toolName": "read", "args": {"path": "a.py"}},
        _assistant("", 100, 10, stop="toolUse"),
        _assistant("the report", 200, 20),
    ]
    out, store = await _run(tmp_path, events)
    try:
        assert set(out) == {"messages", "loop_turns", "final_text"}
        assert out["loop_turns"] == 2 and out["final_text"] == "the report"
        assert out["messages"][-1] == {"role": "assistant", "content": "the report"}
        spend = await store.mission_spend("m-pi")
        assert round(spend.spent_usd, 3) == 0.33, "priced by the catalog, not pi's $0"
        argv = (tmp_path / "argv.txt").read_text()
        assert "--tools read,ask_orchestrator,library_search" in argv
        assert "--no-session" in argv and "--system-prompt SYS" in argv
    finally:
        await store.aclose()


async def test_the_budget_stops_the_loop(tmp_path: Path) -> None:
    events = [_assistant(f"turn {n}", 1000, 0) for n in range(10)]
    out, store = await _run(tmp_path, events, cap=2.5, sleep_s=0.05)
    try:
        assert out["final_text"].startswith("stopped: budget $2.50 reached")
        assert out["loop_turns"] == 3, "stopped at the first turn past the cap"
    finally:
        await store.aclose()


async def test_an_error_with_no_output_fails_the_run(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="Insufficient Balance"):
        await _run(tmp_path, [_assistant("", 0, 0, stop="error", err="402 Insufficient Balance")])
