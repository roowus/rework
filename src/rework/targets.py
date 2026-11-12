"""Named connection targets (~/.rework/targets.toml): pick a database and
daemon per launch with `rework --target NAME` or REWORK_TARGET, instead of
exporting REWORK_DB from the shell profile.

A target file is READ-ONLY to rework: it never writes it (stdlib tomllib only,
no TOML writer). Format:

    default = "rewvis"

    [targets.rewvis]
    db_env = "REWORK_REWVIS_DB"        # or db = "postgres://..."
    daemon_url = "http://100.107.125.118:8941"
    ssh = "ubuntu@100.107.125.118"     # optional, for sync-config
    library_dir = "/home/ubuntu/.rework/library"  # optional

Precedence (highest wins): (1) REWORK_DB / REWORK_DAEMON_URL /
REWORK_LIBRARY_DIR already in the environment (previews and the systemd unit
depend on that winning), (2) --target NAME, (3) the REWORK_TARGET env var,
(4) the file's `default`. Selection applies each key with setdefault
semantics and sets REWORK_TARGET_NAME (again only if unset) so the TUI's
status bar shows it. No targets file at all = today's behavior, unchanged.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from rework.config import DEFAULT_STATE_DIR

TARGETS_PATH = Path(os.environ.get("REWORK_TARGETS", str(DEFAULT_STATE_DIR / "targets.toml")))


@dataclass(frozen=True)
class Target:
    name: str
    db: str | None = None  # resolved (db_env looked up) db URL or sqlite path
    daemon_url: str | None = None
    ssh: str | None = None
    library_dir: str | None = None


@dataclass(frozen=True)
class TargetsFile:
    targets: dict[str, Target] = field(default_factory=dict)
    default: str | None = None

    def resolve_name(
        self,
        *,
        flag: str | None = None,
        env_var: str | None = None,
    ) -> str | None:
        """Which target applies: --target beats REWORK_TARGET beats `default`.
        An unknown name is an error only when someone asked for it (flag/env):
        a bad `default = "..."` line falls back to no target, like a missing
        file, so a typo cannot take the whole CLI down."""
        for name, source in ((flag, "--target"), (env_var, "REWORK_TARGET")):
            if name:
                if name not in self.targets:
                    raise KeyError(f"{source}: no target named {name!r} in {TARGETS_PATH}")
                return name
        if self.default and self.default in self.targets:
            return self.default
        return None


def load_targets_file(path: Path | None = None) -> TargetsFile:
    """Parse targets.toml; a missing or malformed file means NO targets
    (today's behavior), never an error. A target needs db/db_env and
    daemon_url; malformed entries are skipped, not fatal."""
    path = path if path is not None else Path(TARGETS_PATH)
    try:
        raw = tomllib.loads(path.expanduser().read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return TargetsFile()
    table = raw.get("targets")
    targets: dict[str, Target] = {}
    if isinstance(table, dict):
        for name, entry in table.items():
            if not isinstance(entry, dict):
                continue
            db_env = entry.get("db_env")
            db = entry.get("db")
            if db_env:
                db = os.environ.get(str(db_env), db)  # db_env wins; fall back to db
            if not db or not entry.get("daemon_url"):
                continue
            targets[str(name)] = Target(
                name=str(name),
                db=str(db),
                daemon_url=str(entry["daemon_url"]),
                ssh=entry.get("ssh"),
                library_dir=entry.get("library_dir"),
            )
    default = raw.get("default")
    return TargetsFile(targets=targets, default=str(default) if default else None)


def resolve_target(
    *,
    flag: str | None = None,
    env_var: str | None = None,
    path: Path | None = None,
    apply: bool = True,
) -> str | None:
    """Pick and APPLY a target to the process environment, setdefault-style:
    only fill in what is missing, so an explicitly exported REWORK_DB /
    REWORK_DAEMON_URL / REWORK_LIBRARY_DIR / REWORK_TARGET_NAME (previews, the
    rewvis unit, tests) always wins. Returns the target name, or None when
    nothing applied (no file, no flag, no default). With apply=False it only
    answers "which target would apply", touching nothing (used by `rework
    targets` and by main(), which reports unknown --target names)."""
    if env_var is None:
        env_var = os.environ.get("REWORK_TARGET")
    file = load_targets_file(path)
    selected = file.resolve_name(flag=flag, env_var=env_var)
    if selected is None or not apply:
        return selected
    target = file.targets[selected]
    _setdefault("REWORK_DB", target.db)
    _setdefault("REWORK_DAEMON_URL", target.daemon_url)
    if target.library_dir:
        _setdefault("REWORK_LIBRARY_DIR", target.library_dir)
    _setdefault("REWORK_TARGET_NAME", target.name)
    return selected


def _setdefault(name: str, value: str | None) -> None:
    if value and name not in os.environ:
        os.environ[name] = value
