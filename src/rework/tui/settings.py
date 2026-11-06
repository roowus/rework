"""TUI preferences: ~/.rework/tui.json (machinery, so the hidden dir).

Env wins over the file so a deployment can pin behaviour:
REWORK_NOTIFY = comma list of toast,bell,os  |  none."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from rework.config import DEFAULT_STATE_DIR

log = logging.getLogger("rework.tui")

NOTIFY_CHANNELS = ("toast", "bell", "os")


def settings_path() -> Path:
    return Path(os.environ.get("REWORK_TUI_SETTINGS", str(DEFAULT_STATE_DIR / "tui.json")))


def parse_notify(value: str) -> list[str]:
    value = value.strip().lower()
    if value in ("", "none", "off"):
        return []
    if value == "both":  # the previous build's vocabulary
        return ["toast", "bell"]
    return [v for v in (p.strip() for p in value.split(",")) if v in NOTIFY_CHANNELS]


@dataclass
class Settings:
    notify: list[str] = field(default_factory=lambda: ["toast", "bell"])
    chat_model: str = ""  # last orchestrator model picked
    chat_effort: str = ""
    poll_seconds: float = 2.0

    @classmethod
    def load(cls) -> Settings:
        s = cls()
        try:
            raw = json.loads(settings_path().read_text())
            for key, value in raw.items():
                if hasattr(s, key):
                    setattr(s, key, value)
        except FileNotFoundError:
            pass
        except Exception:
            log.warning("unreadable %s; using defaults", settings_path())
        env = os.environ.get("REWORK_NOTIFY")
        if env is not None:
            s.notify = parse_notify(env)
        return s

    def save(self) -> None:
        try:
            path = settings_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(asdict(self), indent=2))
        except Exception:
            log.warning("could not save %s", settings_path(), exc_info=True)
