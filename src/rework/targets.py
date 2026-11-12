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
    checkpoints_db = "/home/ubuntu/.rework/checkpoints.sqlite3"  # optional
    checkpoints_db_env = "REWORK_REWVIS_CHECKPOINTS_DB"          # optional

Precedence (highest wins): (1) REWORK_DB / REWORK_DAEMON_URL /
REWORK_LIBRARY_DIR / REWORK_CHECKPOINTS_DB already in the environment
(previews and the systemd unit depend on that winning), (2) --target NAME,
(3) the REWORK_TARGET env var, (4) the file's `default`. Selection applies
each key with setdefault semantics and sets REWORK_TARGET_NAME (again only
if unset) so the TUI's status bar shows it. No targets file at all = today's
behavior, unchanged.

Checkpoints: a sqlite target gets REWORK_CHECKPOINTS_DB setdefaulted to
`checkpoints.sqlite3` next to its db (or to the explicit `checkpoints_db` /
`checkpoints_db_env` value), so a sandbox target is self-contained the way
the old scripts/rework-local env exports were. Postgres targets share the
main database for checkpoints and touch no checkpoints variable.
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
    checkpoints_db: str | None = None  # resolved; None = derive from db (sqlite)


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
            checkpoints = entry.get("checkpoints_db")
            checkpoints_env = entry.get("checkpoints_db_env")
            if checkpoints_env:
                checkpoints = os.environ.get(str(checkpoints_env), checkpoints)
            targets[str(name)] = Target(
                name=str(name),
                db=str(db),
                daemon_url=str(entry["daemon_url"]),
                ssh=entry.get("ssh"),
                library_dir=entry.get("library_dir"),
                checkpoints_db=str(checkpoints) if checkpoints else None,
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
    if target.checkpoints_db:
        _setdefault("REWORK_CHECKPOINTS_DB", target.checkpoints_db)
    else:
        derived = _default_checkpoints_db(target.db)
        if derived:
            _setdefault("REWORK_CHECKPOINTS_DB", derived)
    return selected


def _default_checkpoints_db(db: str | None) -> str | None:
    """The sandbox-self-contained default: a plain-path sqlite target keeps its
    checkpoints next to the state db (the same default Config.from_env picks).
    Postgres shares the main db for checkpoints (no separate file to point at)
    and a `sqlite:///` URL has no plain directory to anchor to, so neither
    derives one."""
    if not db or "://" in db:
        return None
    return str(Path(db).expanduser().parent / "checkpoints.sqlite3")


def _setdefault(name: str, value: str | None) -> None:
    if value and name not in os.environ:
        os.environ[name] = value


def sqlite_db_path(db: str) -> Path | None:
    """The file behind a db string, or None when db is not a sqlite file
    reference (a postgres URL, say). Three forms reach a connection in this
    codebase: a bare path (Config.from_env's default), the `sqlite:////abs`
    URL docs/ops.md puts in targets.toml, and its `sqlite:///rel` sibling.
    `Path(db)` alone mangles the URL forms into bogus relative paths, which is
    exactly how `rework targets` reported healthy sqlite targets unreachable."""
    if db.startswith(("postgresql://", "postgres://")):
        return None
    if db.startswith("sqlite:"):
        rest = db[len("sqlite:") :]
        if not rest.startswith("//"):
            return Path(rest)  # scheme + relative path (sqlite:path)
        if not db.startswith("sqlite:///"):
            return None  # a named authority (sqlite://host/x.db): not a local file
        stripped = rest.lstrip("/")
        if not stripped:
            return None  # no path at all behind the scheme
        if db.startswith("sqlite:////"):
            return Path("/" + stripped)  # the absolute form: sqlite:////abs/x.db
        return Path(stripped)  # the relative form: sqlite:///rel/x.db
    return Path(db)


def db_reachable(db: str, timeout: float = 3.0) -> bool:
    """Can this db string be opened RIGHT NOW? Postgres gets a short connect
    timeout; sqlite gets a read-only SELECT 1 through sqlite_db_path (a
    missing file is unreachable; a fresh empty file is a valid database). One
    shared probe so every reachability check agrees instead of hand-parsing
    the URL again."""
    if db.startswith(("postgresql://", "postgres://")):
        try:
            import psycopg

            with psycopg.connect(db, connect_timeout=int(timeout), autocommit=True) as conn:
                conn.execute("SELECT 1")
            return True
        except Exception:
            return False
    path = sqlite_db_path(db)
    if path is None:
        return False
    path = path.expanduser()
    if not path.suffix and not path.exists():
        return False  # not a path that exists, not a URL scheme: unresolvable
    try:
        import sqlite3

        sconn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=timeout)
        sconn.execute("SELECT 1")
        sconn.close()
        return True
    except Exception:
        return False
