"""Shared fixtures. Store conformance parametrizes over available backends:
SQLite always; Postgres when REWORK_TEST_POSTGRES is set (CI sets it).

Add a backend: implement the store, then extend `BACKENDS` + `_make_store`.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from rework.kernel.state.store import StateStore

BACKENDS: list[str] = ["sqlite"]
if os.environ.get("REWORK_TEST_POSTGRES"):
    BACKENDS.append("postgres")

_TEST_PG_URL = os.environ.get("REWORK_TEST_POSTGRES", "")


async def _make_store(backend: str, tmp_path: Path) -> StateStore:
    if backend == "sqlite":
        from rework.kernel.state.sqlite import open_sqlite

        return await open_sqlite(str(tmp_path / "test.sqlite3"))
    if backend == "postgres":
        from rework.kernel.state.postgres import open_postgres

        return await open_postgres(_TEST_PG_URL)
    raise ValueError(f"unknown backend {backend}")


@pytest.fixture(params=BACKENDS)
async def store(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[StateStore]:
    s = await _make_store(request.param, tmp_path)
    yield s
    await s.aclose()
