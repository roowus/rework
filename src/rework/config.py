"""Per-instance config. Everything comes from env with local-file defaults, so a
deployment is one env file next to the systemd unit (M4) or plain defaults locally.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rework.kernel.llm.catalog import ModelCatalog
    from rework.kernel.state.store import StateStore

DEFAULT_STATE_DIR = Path.home() / ".rework"


def load_env_file(path: Path | None = None) -> list[str]:
    """Read KEY=VALUE lines from ~/.rework/rework.env (REWORK_ENV_FILE) into
    the environment without overriding anything already set. The same file
    systemd loads on rewvis, so provider keys live in one place per machine
    instead of the owner's shell profile (where every program inherits them).
    Returns the names it set (never the values)."""
    import os

    path = path or Path(os.environ.get("REWORK_ENV_FILE", str(DEFAULT_STATE_DIR / "rework.env")))
    try:
        lines = path.expanduser().read_text().splitlines()
    except OSError:
        return []
    added: list[str] = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        name, _, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if name and name not in os.environ:
            os.environ[name] = value
            added.append(name)
    return added


# User-facing artifacts live in a VISIBLE directory on purpose: fetched results
# and reports are deliverables the owner asked for, not machinery. ~/.rework
# stays hidden and keeps only internal state (env, checkpoints, caches).
DEFAULT_USER_DIR = Path.home() / "rework"


def default_results_dir() -> Path:
    return Path(
        os.environ.get("REWORK_RESULTS_DIR", str(DEFAULT_USER_DIR / "results"))
    ).expanduser()


def default_reports_dir() -> Path:
    return Path(
        os.environ.get("REWORK_REPORTS_DIR", str(DEFAULT_USER_DIR / "reports"))
    ).expanduser()


def _default_db() -> str:
    return str(DEFAULT_STATE_DIR / "state.sqlite3")


@dataclass(frozen=True)
class Config:
    db: str  # sqlite path or postgresql:// URL (auto-detected by scheme)
    checkpoints_db: str  # sqlite path for role checkpoints (postgres shares db in M4)
    poll_interval_s: float = 0.5
    concurrency: int = 1  # sqlite deployments stay at 1 (docs/design.md)
    # daemon-wide cap on concurrently RUNNING role runs, across all missions
    # (REWORK_MAX_ROLES; 0/unset = os.cpu_count(), min 1). Runs beyond the cap
    # are queued, not dropped: see orchestrator/limits.py
    max_roles: int = 0
    git_author_name: str = "rework"  # agent commit identity; per-deployment via env
    git_author_email: str = "rework@rework.local"

    @property
    def git_author(self) -> tuple[str, str]:
        return (self.git_author_name, self.git_author_email)

    @property
    def is_postgres(self) -> bool:
        return self.db.startswith("postgresql://") or self.db.startswith("postgres://")

    @classmethod
    def from_env(cls) -> Config:
        db = os.environ.get("REWORK_DB", _default_db())
        author_name = os.environ.get("REWORK_GIT_AUTHOR_NAME", "rework")
        author_email = os.environ.get("REWORK_GIT_AUTHOR_EMAIL", "rework@rework.local")
        if db.startswith(("postgresql://", "postgres://")):
            # single source of truth: state and checkpoints share the database
            return cls(
                db=db,
                checkpoints_db=db,
                concurrency=_concurrency(),
                max_roles=_max_roles(),
                git_author_name=author_name,
                git_author_email=author_email,
            )
        state_dir = Path(db).expanduser().parent
        checkpoints = os.environ.get(
            "REWORK_CHECKPOINTS_DB", str(state_dir / "checkpoints.sqlite3")
        )
        return cls(
            db=db,
            checkpoints_db=checkpoints,
            concurrency=_concurrency(sqlite_default=1),
            max_roles=_max_roles(),
            git_author_name=author_name,
            git_author_email=author_email,
        )


def _concurrency(sqlite_default: int | None = None) -> int:
    """REWORK_CONCURRENCY, else a host-aware default: min(cpu_count, 4), min 1.
    A sqlite deployment keeps its historical 1 unless the env says otherwise."""
    from rework.nodes import hostlimits

    raw = os.environ.get("REWORK_CONCURRENCY")
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    if sqlite_default is not None:
        return sqlite_default
    return hostlimits.default_concurrency()


def max_roles(config: Config) -> int:
    """The daemon-wide role cap for this config (REWORK_MAX_ROLES, else CPUs)."""
    return config.max_roles if config.max_roles > 0 else _max_roles()


def _max_roles() -> int:
    """REWORK_MAX_ROLES, else one role per CPU (min 1)."""
    from rework.nodes import hostlimits

    raw = os.environ.get("REWORK_MAX_ROLES")
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return hostlimits.default_max_roles()


def _prepare_sqlite_dir(db: str) -> str:
    path = Path(db).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


async def open_store_from_config(config: Config) -> StateStore:
    """Open the configured StateStore (and create parent dirs for sqlite)."""
    if config.is_postgres:
        from rework.kernel.state.postgres import open_postgres

        return await open_postgres(config.db)
    from rework.kernel.state.sqlite import open_sqlite

    return await open_sqlite(await asyncio.to_thread(_prepare_sqlite_dir, config.db))


def load_catalog() -> ModelCatalog:
    """Catalog from REWORK_MODELS (path or inline JSON) or ~/.rework/models.json;
    falls back to the built-in demo catalog (fake provider only, zero cost)."""
    import json

    from rework.kernel.llm.catalog import ModelCatalog, ModelEntry

    source: str | None = os.environ.get("REWORK_MODELS")
    path = Path(source) if source else DEFAULT_STATE_DIR / "models.json"
    data: list[dict] | None = None
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            data = None
    if data is None and source and source.strip().startswith("["):
        data = json.loads(source)
    if data:
        return ModelCatalog.from_config(data)
    return ModelCatalog([ModelEntry(id="fake", provider="fake", model="fake-1", tier=0)])
