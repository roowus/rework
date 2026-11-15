"""Structured logs: JSON lines per component, context tags that follow the
asyncio task doing the work, model calls logged by the provider wrapper, and
a reader that filters by mission / level / substring."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
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
    assert "cause" not in bad, "a failure with no cause logs no chain"


async def test_logged_provider_logs_the_cause_chain(log_env: Path) -> None:
    """Found live (m-b928ea6d51bc): the Anthropic SDK raises APITimeoutError
    `from err`, where err is the underlying httpx exception, and only the
    top-level type reached the log, so an idle timeout could not be told from a
    dropped connection or an overload. The chain gets its own `cause` field."""
    import httpx

    from rework.kernel.llm.adapters.logged import LoggedProvider
    from rework.kernel.llm.types import ChatRequest, ChatResult

    path = logs.setup_logging("test")

    class TimedOut:
        async def complete(self, request: ChatRequest) -> ChatResult:
            raise TimeoutError("Request timed out or interrupted") from httpx.ReadTimeout(
                "read timed out"
            )

    request = ChatRequest(model="haiku", messages=[{"role": "user", "content": "x"}])
    with pytest.raises(TimeoutError):
        await LoggedProvider(TimedOut()).complete(request)

    bad = next(
        json.loads(x)
        for x in path.read_text().splitlines()
        if "model call failed" in json.loads(x)["msg"]
    )
    assert "TimeoutError" in bad["msg"], "the top-level warning is unchanged"
    assert "ReadTimeout" in bad["cause"] and "read timed out" in bad["cause"]
    assert bad["cause"] not in bad["msg"], "the chain is a separate field, not the message"


def test_cause_chain_walks_causes_contexts_cycles_and_bad_str() -> None:
    """Pure logic of the chain walk: __cause__ first, then __context__, bounded
    per link and in depth, cycle-safe, and never raising on a hostile cause."""
    from rework.kernel.llm.adapters.logged import cause_chain

    top = RuntimeError("top")
    assert cause_chain(top) == "", "no cause, no chain"

    outer = RuntimeError("outer")
    outer.__cause__ = ValueError("boom")
    assert cause_chain(outer) == "ValueError: boom"

    # __cause__ wins when both are set, and __context__ is walked after it
    both = RuntimeError("both")
    inner = ValueError("inner")
    inner.__cause__ = LookupError("deep")
    both.__cause__ = inner
    both.__context__ = OSError("stale context")
    assert cause_chain(both) == "ValueError: inner <- LookupError: deep"

    loop = RuntimeError("a")
    loop.__cause__ = RuntimeError("b")
    loop.__cause__.__cause__ = loop
    assert cause_chain(loop) == "RuntimeError: b", "a cycle stops the walk"

    deep = RuntimeError("d0")
    node = deep
    for i in range(1, 8):
        node.__cause__ = RuntimeError(f"d{i}")
        node = node.__cause__
    assert cause_chain(deep).count("<-") == 3, "capped at 4 links"

    class Hostile(Exception):
        def __str__(self) -> str:
            raise RuntimeError("no str for you")

    class Blank(Exception):
        pass

    hostile = RuntimeError("x")
    hostile.__cause__ = Hostile()
    assert cause_chain(hostile) == "Hostile: <unprintable>"
    blank = RuntimeError("x")
    blank.__cause__ = Blank()
    assert cause_chain(blank) == "Blank", "an empty message still names the type"


# `~/.rework/work` holds the worktree of every mission rework has run (15 GB,
# ~1.9M files on the owner's machine) and the running test session itself writes
# into it; this test must not walk it. Everything else under the state dir --
# logs, library, run, the config files -- is what a test run could pollute.
_SKIP_STATE_DIRS = frozenset({"work"})


def _snapshot(root: Path) -> dict[str, int]:
    """The files under `root` a test run could write, with their mtime."""
    if not root.exists():
        return {}
    found: dict[str, int] = {}
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            found[str(entry)] = entry.stat().st_mtime_ns
        elif entry.name not in _SKIP_STATE_DIRS:
            for path in sorted(entry.rglob("*")):
                if path.is_file():
                    found[str(path)] = path.stat().st_mtime_ns
    return found


def test_the_session_never_logs_into_the_owner_s_real_state_dir(
    log_isolation: Path, reset_logs: Callable[[], None]
) -> None:
    """Follow-up: driving run_tui() in-process made the suite append to the
    owner's ~/.rework/logs/tui.log. The autouse isolation fixture must keep this
    whole session's logging in one tmp dir, and this test watches the real
    directory to prove it. The `_configured` trap is the point: a component
    configured earlier in the session keeps its stale handler and setup_logging()
    returns early, so configuring "tui" twice must both land in the tmp dir."""
    real = Path.home() / ".rework"
    before = _snapshot(real)
    real_state = str(real.resolve())

    assert str(logs.log_dir().resolve()).startswith(str(log_isolation.resolve())), (
        f"the session logs into {logs.log_dir()}, not into the isolated {log_isolation}"
    )

    # an earlier test of this session already configured "tui" (run_tui in-process)
    first = logs.setup_logging("tui")
    assert "tui" in logs._configured
    reset_logs()
    second = logs.setup_logging("tui")  # the early-return path, after the reset
    assert first == second == log_isolation / "tui.log", f"{second} is not the isolated log file"
    logging.getLogger("rework.tui").error("isolation probe")

    written = {str(p) for p in log_isolation.rglob("*.log")}
    assert str(second) in written, "the record went to the isolated log file"
    assert second.read_text().count("isolation probe") == 1
    assert not any(p.startswith(real_state) for p in written), f"a log landed under {real}"
    assert _snapshot(real) == before, f"the test run wrote under {real}"


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
