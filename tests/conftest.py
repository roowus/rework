"""Shared fixtures. Store conformance parametrizes over available backends:
SQLite always; Postgres when REWORK_TEST_POSTGRES is set (CI sets it).

Add a backend: implement the store, then extend `BACKENDS` + `_make_store`.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from rework.kernel.state.store import StateStore

if TYPE_CHECKING:
    from rework.kernel.llm.catalog import ModelCatalog

BACKENDS: list[str] = ["sqlite"]
if os.environ.get("REWORK_TEST_POSTGRES"):
    BACKENDS.append("postgres")

_TEST_PG_URL = os.environ.get("REWORK_TEST_POSTGRES", "")


def make_git_repo(tmp_path: Path) -> Path:
    """Tiny git repo with one commit on main, for role tests that need one."""
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    (repo / "README.md").write_text("# repo\n")
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=r@r", "-c", "user.name=r", "commit", "-m", "init"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return repo


def role_catalog() -> ModelCatalog:
    """Catalog covering every builtin role's default model id (compile canaries)."""
    from rework.kernel.llm.catalog import ModelCatalog, ModelEntry

    return ModelCatalog(
        [
            ModelEntry(id="flash", provider="fake", model="fake-flash"),
            ModelEntry(id="pro", provider="fake", model="fake-pro"),
            ModelEntry(id="fake", provider="fake", model="fake-1"),
        ]
    )


async def _make_store(backend: str, tmp_path: Path) -> StateStore:
    if backend == "sqlite":
        from rework.kernel.state.sqlite import open_sqlite

        return await open_sqlite(str(tmp_path / "test.sqlite3"))
    if backend == "postgres":
        from rework.kernel.state.postgres import PostgresStore, open_postgres

        store = await open_postgres(_TEST_PG_URL)
        assert isinstance(store, PostgresStore)
        await store.reset_for_tests()  # shared dev database — isolate every test
        return store
    raise ValueError(f"unknown backend {backend}")


@pytest.fixture(params=BACKENDS)
async def store(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[StateStore]:
    s = await _make_store(request.param, tmp_path)
    yield s
    await s.aclose()
