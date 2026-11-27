"""Project creation, shared by the daemon's /v1/projects and the no-daemon TUI."""

from __future__ import annotations

from typing import Any

from rework.kernel.state.records import Project
from rework.kernel.state.store import StateStore


async def create_project(
    store: StateStore, name: str, repos: list[dict[str, Any]] | None = None
) -> Project:
    """Settings that are always self-consistent: the transport and the results
    handling are DERIVED from the repos, never hardcoded. No repo means an
    upload project (rework keeps the files, results are fetched); a repo
    means git/push_branch."""
    from rework.missions import derive_transport

    repo_list = [dict(r) for r in repos or []]
    transport, results = derive_transport({"repos": repo_list})
    return await store.create_project(
        Project(
            name=name,
            settings={"transport": transport, "results": results, "repos": repo_list},
        )
    )
