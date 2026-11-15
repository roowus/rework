"""Self-reload: the state the TUI saves before it re-execs, and the exec itself.

Why this exists (findings 15 and 28): a merge landing in the checkout, or an
update the owner accepted from the Help menu, used to leave the running TUI on
the OLD modules with a note asking for a manual restart. Now the shell arms a
reload, waits until the owner is idle, saves what they were looking at, exits,
and `run_tui` re-execs the process: same argv (`--target` still applies), same
environment, new code.

Three pieces live here so they can be tested without a terminal:

- `is_idle`: the pure predicate (no keypress for `IDLE_SECONDS`, nothing modal,
  no chat turn in flight, no update in flight);
- `ReloadState` + `save_state`/`load_state`: one small JSON file next to
  `tui.json`, written atomically, read tolerantly;
- `reexec_argv`/`reexec`: what to exec, behind a module-level `_exec` so tests
  never really replace the test process.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from rework.runtime import short_commit
from rework.tui.settings import settings_path

log = logging.getLogger("rework.tui")

# How long the owner must have been quiet before a reload may happen. A reload
# while they type would lose the half-written message; 30s is long enough that
# the keypress that starts a new sentence always wins.
IDLE_SECONDS = 30.0

# How often the shell asks "may I reload now?" (cheap: four booleans)
RELOAD_CHECK_SECONDS = 2.0

# the transient note for "a reload is going to happen"
RELOADING = "reloading…"


def state_path() -> Path:
    """The reload file lives next to `tui.json`, so the same state dir
    (`~/.rework`, or whatever `REWORK_TUI_SETTINGS` points at) holds both, and
    a test that isolates the settings isolates this too. `REWORK_TUI_RELOAD_STATE`
    overrides, like `REWORK_TUI_SETTINGS` does."""
    override = os.environ.get("REWORK_TUI_RELOAD_STATE")
    if override:
        return Path(override)
    return settings_path().parent / "reload.json"


def is_idle(
    *,
    now: float,
    last_input: float,
    modal_open: bool,
    chat_busy: bool,
    updating: bool = False,
    idle_seconds: float | None = None,
) -> bool:
    """Is the owner away from the keyboard with nothing in flight?

    Pure on purpose: every fact is passed in, `now` included, so this is
    testable without sleeping and the caller (the App) stays the only place
    that knows where those facts come from. `idle_seconds` defaults to the
    module constant at CALL time, so a test can shrink the threshold by
    patching `IDLE_SECONDS`.
    """
    threshold = IDLE_SECONDS if idle_seconds is None else idle_seconds
    if modal_open or chat_busy or updating:
        return False
    return (now - last_input) >= threshold


@dataclass
class ReloadState:
    """What survives the exec. Deliberately small: only things that are CHEAP
    and SAFE to put back. NOT saved (and why): the chat transcript (it is in
    the store; re-reading it is the normal path), the composer's pending image
    attachments (a path list would silently re-attach on the next send), which
    of the app's many scroll offsets are mid-animation, and any per-widget
    filter text other than the selected gate (the rest is re-derived from the
    store, which is the source of truth)."""

    tab: str = "work"
    chat_id: str = ""
    mission_id: str = ""
    gate_id: str = ""
    draft: str = ""
    scroll: dict[str, float] = field(default_factory=dict)
    pending_toast: str = ""
    commit: str = ""
    at: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "tab": self.tab,
            "chat_id": self.chat_id,
            "mission_id": self.mission_id,
            "gate_id": self.gate_id,
            "draft": self.draft,
            "scroll": dict(self.scroll),
            "pending_toast": self.pending_toast,
            "commit": self.commit,
            "at": self.at,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> ReloadState:
        """Tolerant read: unknown keys are ignored, a wrong TYPE falls back to
        the default, and nothing here can raise (a half-written or hand-edited
        file must not break the next start)."""
        state = cls()
        if not isinstance(raw, dict):
            return state
        for f in fields(cls):
            if f.name not in raw:
                continue
            value = raw[f.name]
            if f.name == "scroll":
                if isinstance(value, dict):
                    state.scroll = {
                        str(k): float(v)
                        for k, v in value.items()
                        if isinstance(v, int | float) and not isinstance(v, bool)
                    }
            elif f.name == "at":
                if isinstance(value, int | float) and not isinstance(value, bool):
                    state.at = float(value)
            elif isinstance(value, str):
                setattr(state, f.name, value)
        return state


def save_state(state: ReloadState) -> bool:
    """Write atomically (tmp + rename in the same directory): a crash or a
    kill mid-write must never leave a truncated file that the next start reads
    as "restore the wrong thing". Returns False instead of raising — a failed
    save must never block the exec it was preparing."""
    path = state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(state.as_dict(), indent=2))
        os.replace(tmp, path)
        return True
    except Exception:
        log.warning("could not write %s", path, exc_info=True)
        return False


def load_state(path: Path | None = None) -> ReloadState | None:
    """The saved state, or None when there is nothing usable. Never raises."""
    target = path or state_path()
    try:
        raw = json.loads(target.read_text())
    except FileNotFoundError:
        return None
    except Exception:
        log.warning("unreadable %s; ignoring it", target)
        return None
    return ReloadState.from_dict(raw)


def clear_state(path: Path | None = None) -> None:
    """Apply once: the file goes away as soon as it has been read."""
    try:
        (path or state_path()).unlink(missing_ok=True)
    except OSError:
        log.debug("could not remove the reload state", exc_info=True)


def toast_for(commit: str, *, reason: str = "") -> str:
    """The toast the NEW process shows: "updated to <commit>", and when the
    commit is unknown (a pip install, a tree without git) it degrades to a
    plain sentence rather than "updated to "."""
    if commit:
        return f"updated to {commit}"
    return f"reloaded ({reason})" if reason else "reloaded"


def current_commit() -> str:
    """The short sha of the code now on disk, or "" when it cannot be known."""
    try:
        return short_commit()
    except Exception:  # a stat/read failure is "unknown", never a crash
        return ""


def reexec_argv() -> list[str]:
    """How to become a fresh `rework`, replaying the original argv.

    `sys.orig_argv` is the truth when it names a real interpreter and a real
    rework script: that is what the launcher did (`uv run rework …`, which
    execs the venv python with the console script), so the exec keeps the same
    environment and the same `--target` / subcommand line. Anything else — a
    `python -c` stub, an argv built by a shell wrapper — falls back to
    `[sys.executable, "-m", "rework", *sys.argv[1:]]`, which is why
    `src/rework/__main__.py` exists.
    """
    argv = [sys.executable, "-m", "rework", *sys.argv[1:]]
    orig = [str(a) for a in (getattr(sys, "orig_argv", None) or [])]
    try:
        if len(orig) >= 3 and orig[1] == "-m" and orig[2] == "rework" and Path(orig[0]).exists():
            return orig
        if len(orig) >= 2 and "rework" in Path(orig[1]).name and Path(orig[0]).is_file():
            if Path(orig[1]).is_file():
                return orig
    except OSError:
        pass
    return argv


def _exec(path: str, argv: list[str]) -> None:  # pragma: no cover - replaced in tests
    """The real syscall, kept in ONE module-level function: no test may
    replace the test process, so every test patches this instead."""
    os.execv(path, argv)


def reexec() -> bool:
    """Replace this process with a fresh `rework`. Returns only on failure
    (a missing interpreter), so the caller can fall back to a normal exit."""
    argv = reexec_argv()
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:  # a closed/broken tty must not stop the exec
        pass
    path = argv[0]
    log.info("reexec: %s", " ".join(argv))
    try:
        _exec(path, argv)
    except OSError:
        log.exception("reexec failed; exiting instead")
        return False
    return True  # pragma: no cover - os.execv does not return


__all__ = [
    "IDLE_SECONDS",
    "RELOADING",
    "RELOAD_CHECK_SECONDS",
    "ReloadState",
    "clear_state",
    "current_commit",
    "is_idle",
    "load_state",
    "reexec",
    "reexec_argv",
    "save_state",
    "state_path",
    "toast_for",
]
