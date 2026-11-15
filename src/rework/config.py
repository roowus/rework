"""Per-instance config. Everything comes from env with local-file defaults, so a
deployment is one env file next to the systemd unit (M4) or plain defaults locally.
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rework.kernel.llm.catalog import ModelCatalog
    from rework.kernel.state.store import StateStore

DEFAULT_STATE_DIR = Path.home() / ".rework"

# The cheap, strong model every role runs on unless a plan/brief names another
# one. One env var (or one catalog edit) switches it deployment-wide: no code
# change, no literal id in a default path.
DEFAULT_MODEL_ENV = "REWORK_DEFAULT_MODEL"
DEFAULT_MODEL_ID = "flash"
# The one model a role is escalated TO (a run that failed on the WORK, or a
# strict audit of a high-risk change). NOT a ban on anything: the default and
# the escalation target are pure config, so moving either later is env-only.
ESCALATION_MODEL_ENV = "REWORK_ESCALATION_MODEL"
ESCALATION_MODEL_ID = "claude-sonnet-5-5"


# The URL schemes this loader treats as Postgres. ONE source of truth: the
# credential-recovery guard compares candidate URLs against this same set,
# because anything else is opened as a sqlite PATH, which would create a local
# file named after the URL (password included).
POSTGRES_SCHEMES = ("postgresql", "postgres")
POSTGRES_URL_PREFIXES = tuple(f"{scheme}://" for scheme in POSTGRES_SCHEMES)


def is_postgres_url(url: str) -> bool:
    """Whether a database value is a Postgres URL rather than a sqlite path."""
    return url.startswith(POSTGRES_URL_PREFIXES)


def default_env_file() -> Path:
    """The env file this process reads: REWORK_ENV_FILE, else
    ~/.rework/rework.env (the same file systemd loads on rewvis)."""
    return Path(os.environ.get("REWORK_ENV_FILE", str(DEFAULT_STATE_DIR / "rework.env")))


def parse_env_file(path: Path | None = None) -> dict[str, str]:
    """Parse KEY=VALUE lines from the env file and return them as a dict,
    WITHOUT touching the environment. A missing or unreadable file is an empty
    dict, like load_env_file's empty list. Values come back as written (quotes
    stripped), so a caller decides what to do with them."""
    path = path if path is not None else default_env_file()
    try:
        lines = path.expanduser().read_text().splitlines()
    except (OSError, UnicodeDecodeError, ValueError):
        return {}
    values: dict[str, str] = {}
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
        if name:
            # setdefault, not assignment: a repeated key keeps its FIRST value,
            # which is what the old line-by-line loader did ("if name not in
            # os.environ"). The owner hand-edits this file, and the default
            # path has to stay byte-for-byte compatible.
            values.setdefault(name, value)
    return values


def load_env_file(path: Path | None = None, *, override: bool = False) -> list[str]:
    """Read KEY=VALUE lines from ~/.rework/rework.env (REWORK_ENV_FILE) into
    the environment without overriding anything already set. The same file
    systemd loads on rewvis, so provider keys live in one place per machine
    instead of the owner's shell profile (where every program inherits them).
    Returns the names it set (never the values).

    `override=True` replaces names that are already set; that is the credential
    rotation path (a rotated password must beat the stale value in this
    process), and it is opt-in so the existing setdefault contract and its
    callers stay exactly as they were."""
    added: list[str] = []
    for name, value in parse_env_file(path).items():
        if override or name not in os.environ:
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


def projects_dir() -> Path:
    """Where per-project files areas live (upload transport, ADR 0005). This is
    daemon-side internal state, so it sits under the same state root as the
    database rather than in the visible deliverables directory."""
    override = os.environ.get("REWORK_PROJECTS_DIR")
    if override:
        return Path(override).expanduser()
    return DEFAULT_STATE_DIR / "projects"


_UNSAFE_ID_CHARS = re.compile(r"[^A-Za-z0-9_-]")


def project_files_dir(project_id: str) -> Path:
    """A repo-less project's persistent files area: <projects_dir>/<id>/files.

    The id is a path segment, so it is sanitised: a dot is not a legal id
    character in practice (`new_id` mints `p-<hex>`), but anything that could
    read as a traversal (".", "..", separators) must never escape the area."""
    safe = _UNSAFE_ID_CHARS.sub("_", project_id)
    if not safe.strip("_"):
        raise ValueError(f"unusable project id for a files area: {project_id!r}")
    return projects_dir() / safe / "files"


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
        return is_postgres_url(self.db)

    @classmethod
    def from_env(cls) -> Config:
        db = os.environ.get("REWORK_DB", _default_db())
        author_name = os.environ.get("REWORK_GIT_AUTHOR_NAME", "rework")
        author_email = os.environ.get("REWORK_GIT_AUTHOR_EMAIL", "rework@rework.local")
        if is_postgres_url(db):
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


def _configured_model(
    env_var: str,
    fallback_id: str,
    *,
    catalog: ModelCatalog | None = None,
    cheapest: bool,
) -> str:
    """Resolve one configured model id against the catalog.

    The requested id wins when it exists AND this process can call it. A
    missing id or one with no key here falls back to the cheapest (or
    strongest) USABLE entry, so a machine that does not hold a key still runs
    on something it can actually call. With no usable entry at all the
    requested id is returned unresolved (the engine reports it)."""
    wanted = (os.environ.get(env_var) or fallback_id).strip() or fallback_id
    cat = catalog if catalog is not None else load_catalog()
    entry = next((e for e in cat.all() if e.id == wanted), None)
    if entry is not None and entry.has_credentials():
        return entry.id
    pick = cat.cheapest_usable() if cheapest else cat.strongest_usable()
    return pick.id if pick is not None else wanted


def default_model_id(catalog: ModelCatalog | None = None) -> str:
    """THE default model: REWORK_DEFAULT_MODEL (default 'flash'), resolved
    against the catalog. Every role, plan step, the TUI knob and the
    orchestrator's own model use this one function, so switching the default
    is env-only."""
    return _configured_model(DEFAULT_MODEL_ENV, DEFAULT_MODEL_ID, catalog=catalog, cheapest=True)


def escalation_model_id(catalog: ModelCatalog | None = None) -> str:
    """THE escalation model: REWORK_ESCALATION_MODEL (default
    'claude-sonnet-5-5'), resolved against the catalog, falling back to the
    strongest usable entry. Used wherever a role is escalated; nothing is
    excluded, ever."""
    return _configured_model(
        ESCALATION_MODEL_ENV, ESCALATION_MODEL_ID, catalog=catalog, cheapest=False
    )


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
