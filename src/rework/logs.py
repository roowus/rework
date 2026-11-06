"""Structured logs: one JSON object per line, one rotating file per component.

    ~/.rework/logs/daemon.log   the engine: runners, roles, model + tool calls
    ~/.rework/logs/tui.log      the TUI: swallowed errors, offline transitions
    ~/.rework/logs/cli.log      one-shot commands

Mission EVENTS (the events table) stay the product-level record the UI shows;
these logs are the operational record for debugging: every model call with its
latency and token counts, every tool call with its duration, every crash with a
traceback. `rework logs` reads them.

Fields beyond the standard ones ride on `extra=`: mission, run, model, tool,
ms, tokens_in, tokens_out, cost. Never log secrets or prompt bodies.
"""

from __future__ import annotations

import contextvars
import json
import logging
import logging.handlers
import os
import sys
import time
from pathlib import Path
from typing import Any

FIELDS = ("mission", "run", "role", "model", "tool", "ms", "tokens_in", "tokens_out", "cost")
_configured: set[str] = set()

# who is doing the work right now (set by role runs and the orchestrator); every
# record logged inside that task is tagged with it, provider calls included
_context: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "rework_log_context", default=None
)


def bind(**fields: str | None) -> contextvars.Token[dict[str, str] | None]:
    """Tag subsequent log records in this task (asyncio copies context per
    task, so a role's binding never leaks into its siblings)."""
    current = dict(_context.get() or {})
    current.update({k: v for k, v in fields.items() if v})
    return _context.set(current)


class _ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in (_context.get() or {}).items():
            if getattr(record, key, None) is None:
                setattr(record, key, value)
        return True


def log_dir() -> Path:
    from rework.config import DEFAULT_STATE_DIR

    return Path(os.environ.get("REWORK_LOG_DIR", str(DEFAULT_STATE_DIR / "logs"))).expanduser()


class JsonFormatter(logging.Formatter):
    def __init__(self, component: str) -> None:
        super().__init__()
        self.component = component

    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": round(record.created, 3),
            "level": record.levelname.lower(),
            "component": self.component,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in FIELDS:
            value = getattr(record, key, None)
            if value is not None:
                out[key] = value
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


def setup_logging(component: str, *, console: bool = False) -> Path:
    """Attach the JSON file handler for this process (idempotent). `console`
    also prints human lines to stderr (the daemon; never the TUI, which owns
    the terminal). Returns the log file path."""
    path = log_dir() / f"{component}.log"
    if component in _configured:
        return path
    _configured.add(component)
    level = getattr(logging, os.environ.get("REWORK_LOG_LEVEL", "INFO").upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(min(root.level or logging.WARNING, level))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        handler.setFormatter(JsonFormatter(component))
        handler.setLevel(level)
        handler.addFilter(_ContextFilter())
        root.addHandler(handler)
    except OSError as exc:  # an unwritable log dir must never stop the app
        print(f"rework: logging to file disabled ({exc})", file=sys.stderr)
    if console:
        stream = logging.StreamHandler()
        stream.setFormatter(logging.Formatter("%(asctime)s %(name)s %(message)s"))
        stream.setLevel(level)
        root.addHandler(stream)
    # third-party chatter: keep request lines at INFO only when asked
    for noisy in ("httpx", "httpcore", "aiohttp.access", "openai", "anthropic"):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))
    _install_excepthook()
    return path


def _install_excepthook() -> None:
    previous = sys.excepthook

    def hook(exc_type: Any, exc: BaseException, tb: Any) -> None:
        logging.getLogger("rework.crash").critical(
            "uncaught %s: %s", exc_type.__name__, exc, exc_info=(exc_type, exc, tb)
        )
        previous(exc_type, exc, tb)

    sys.excepthook = hook


def asyncio_exception_handler(loop: Any, context: dict[str, Any]) -> None:
    """For loop.set_exception_handler: background-task failures get logged
    with their traceback instead of a bare stderr line."""
    exc = context.get("exception")
    logging.getLogger("rework.crash").error(
        "asyncio: %s", context.get("message", "unhandled"), exc_info=exc
    )


class Timer:
    """`with Timer() as t: ...; t.ms` for latency fields."""

    def __enter__(self) -> Timer:
        self._start = time.perf_counter()
        self.ms = 0
        return self

    def __exit__(self, *exc: object) -> None:
        self.ms = int((time.perf_counter() - self._start) * 1000)


def read_logs(
    component: str | None = None,
    *,
    mission: str | None = None,
    level: str | None = None,
    grep: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Newest-last records from the component files (all components when
    None), filtered. Rotated files are included, oldest first."""
    order = {"debug": 0, "info": 1, "warning": 2, "error": 3, "critical": 4}
    floor = order.get((level or "debug").lower(), 0)
    files: list[Path] = []
    comps = [component] if component else ["daemon", "tui", "cli"]
    for comp in comps:
        base = log_dir() / f"{comp}.log"
        files += sorted(base.parent.glob(f"{comp}.log.*"), reverse=True)
        files.append(base)
    records: list[dict[str, Any]] = []
    for f in files:
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if mission and rec.get("mission") != mission and mission not in rec.get("msg", ""):
                continue
            if order.get(str(rec.get("level")), 0) < floor:
                continue
            if grep and grep.lower() not in line.lower():
                continue
            records.append(rec)
    records.sort(key=lambda r: r.get("ts", 0))
    return records[-limit:]


def format_record(rec: dict[str, Any]) -> str:
    when = time.strftime("%m-%d %H:%M:%S", time.localtime(float(rec.get("ts", 0))))
    bits = [f"{k}={rec[k]}" for k in FIELDS if k in rec]
    line = (
        f"{when} {rec.get('level', '?'):<7} {rec.get('component', '')[:6]:<6} {rec.get('msg', '')}"
    )
    if bits:
        line += "  [" + " ".join(bits) + "]"
    if rec.get("exc"):
        line += "\n" + str(rec["exc"])
    return line
