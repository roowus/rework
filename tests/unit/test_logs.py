"""Structured logs: JSON lines per component, context tags that follow the
asyncio task doing the work, model calls logged by the provider wrapper, and
a reader that filters by mission / level / substring."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import pytest

from rework import logs


@pytest.fixture
def log_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("REWORK_LOG_DIR", str(tmp_path))
    root = logging.getLogger()
    saved = list(root.handlers)
    logs._configured.discard("test")
    yield tmp_path
    for handler in list(root.handlers):
        if handler not in saved:
            root.removeHandler(handler)
            handler.close()
    logs._configured.discard("test")


async def test_records_are_json_and_carry_the_task_context(log_env: Path) -> None:
    path = logs.setup_logging("test")
    log = logging.getLogger("rework.test")

    async def role(run: str) -> None:
        logs.bind(mission="m-1", run=run, role="coder")
        await asyncio.sleep(0)
        log.info("working on %s", run, extra={"tool": "grep", "ms": 12})

    await asyncio.gather(role("r-a"), role("r-b"))
    log.info("outside any role")
    try:
        raise ValueError("boom")
    except ValueError:
        log.exception("it broke")

    lines = [json.loads(x) for x in path.read_text().splitlines()]
    tagged = {r["run"]: r for r in lines if r.get("run")}
    assert tagged["r-a"]["msg"] == "working on r-a", "each task keeps its own binding"
    assert tagged["r-b"]["mission"] == "m-1" and tagged["r-b"]["tool"] == "grep"
    untagged = next(r for r in lines if r["msg"] == "outside any role")
    assert "run" not in untagged, "a binding never leaks out of its task"
    crash = next(r for r in lines if r["msg"] == "it broke")
    assert crash["level"] == "error" and "ValueError: boom" in crash["exc"]


async def test_logged_provider_records_latency_tokens_and_failures(log_env: Path) -> None:
    from rework.kernel.llm.adapters.logged import LoggedProvider
    from rework.kernel.llm.types import ChatRequest, ChatResult, Usage

    path = logs.setup_logging("test")

    class Ok:
        async def complete(self, request: ChatRequest) -> ChatResult:
            return ChatResult(text="hi", usage=Usage(input_tokens=120, output_tokens=7))

    class Down:
        async def complete(self, request: ChatRequest) -> ChatResult:
            raise ConnectionError("upstream 503")

    request = ChatRequest(model="deepseek", messages=[{"role": "user", "content": "x"}])
    await LoggedProvider(Ok()).complete(request)
    with pytest.raises(ConnectionError):
        await LoggedProvider(Down()).complete(request)

    records = [json.loads(x) for x in path.read_text().splitlines()]
    ok = next(r for r in records if "model call ok" in r["msg"])
    assert ok["model"] == "deepseek" and ok["tokens_in"] == 120 and "ms" in ok
    bad = next(r for r in records if "model call failed" in r["msg"])
    assert bad["level"] == "warning" and "503" in bad["msg"]


def test_reader_filters_and_formats(log_env: Path) -> None:
    rows = [
        {"ts": 1.0, "level": "info", "component": "daemon", "msg": "a", "mission": "m-1"},
        {"ts": 2.0, "level": "error", "component": "daemon", "msg": "b", "mission": "m-2"},
        {"ts": 3.0, "level": "warning", "component": "tui", "msg": "offline: timeout"},
    ]
    (log_env / "daemon.log").write_text("\n".join(json.dumps(r) for r in rows[:2]) + "\nnot json\n")
    (log_env / "tui.log").write_text(json.dumps(rows[2]) + "\n")

    assert [r["msg"] for r in logs.read_logs()] == ["a", "b", "offline: timeout"]
    assert [r["msg"] for r in logs.read_logs(mission="m-2")] == ["b"]
    assert [r["msg"] for r in logs.read_logs(level="warning")] == ["b", "offline: timeout"]
    assert [r["msg"] for r in logs.read_logs("tui")] == ["offline: timeout"]
    assert [r["msg"] for r in logs.read_logs(grep="OFFLINE")] == ["offline: timeout"]
    line = logs.format_record(rows[1])
    assert "error" in line and "mission=m-2" in line


async def test_hub_backs_off_and_logs_only_transitions(caplog) -> None:  # type: ignore[no-untyped-def]
    """Found live: an unreachable database was retried and logged every 2s."""
    from rework.tui.hub import Hub

    class Down:
        _reconnecting_hint = ""
        calls = 0

        async def missions(self, limit: int = 60):  # type: ignore[no-untyped-def]
            Down.calls += 1
            raise OSError("connection timeout expired")

        async def aclose(self) -> None:
            pass

    hub = Hub(Down())  # type: ignore[arg-type]
    caplog.set_level(logging.INFO, logger="rework.tui")
    for _ in range(5):
        await hub.refresh()
    assert Down.calls == 1, "backed off: no retry within the backoff window"
    assert sum("offline" in r.message for r in caplog.records) == 1
    hub._next_try = 0  # backoff elapsed
    await hub.refresh()
    assert Down.calls == 2
    assert sum("offline" in r.message for r in caplog.records) == 1, "no repeat warning"
