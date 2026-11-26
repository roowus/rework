"""GitHub Issues as a mission source (ADR 0016's second source).

The same contract as Plane: list ready items, comment once when the mission
ends. It talks to GitHub through the `gh` CLI, already installed and logged in
wherever rework delivers PRs, so there is no new token to store. Ready = open
issues carrying the configured label (default `rework`). The mission targets the
issue's own repo (`repo_origin`), so the fix lands where the issue lives.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from typing import Any

from rework.nodes.gh import GHError

KIND = "github"


def config() -> dict[str, Any]:
    """REWORK_GITHUB_REPO=owner/name turns the source on (absent = off)."""
    return {
        "repo": os.environ.get("REWORK_GITHUB_REPO", "").strip(),
        "label": os.environ.get("REWORK_GITHUB_LABEL", "rework").strip() or "rework",
        "interval_s": float(os.environ.get("REWORK_GITHUB_POLL_S", "120") or 120),
    }


def enabled(cfg: dict[str, Any] | None = None) -> bool:
    return bool((cfg or config())["repo"])


def mission_id_for(repo: str, number: int | str) -> str:
    """Deterministic per issue: polling twice, or a restart, creates nothing twice."""
    digest = hashlib.sha256(f"{repo}#{number}".encode()).hexdigest()[:12]
    return f"m-gh-{digest}"


def objective_for(repo: str, item: dict[str, Any]) -> str:
    title = str(item.get("title") or "").strip() or "untitled issue"
    body = str(item.get("body") or "").strip()
    head = f"[{repo}#{item.get('number')}] {title}"
    return f"{head}\n\n{body}" if body else head


async def _gh(*args: str, timeout_s: float = 60.0) -> str:
    proc = await asyncio.create_subprocess_exec(
        "gh",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout_s)
    except TimeoutError:
        proc.kill()
        raise GHError(f"gh {' '.join(args[:2])} timed out") from None
    if proc.returncode:
        raise GHError(err.decode("utf-8", "replace").strip()[:500] or f"gh exit {proc.returncode}")
    return out.decode("utf-8", "replace")


class GitHubIssues:
    """One repository's labelled open issues."""

    kind = KIND

    def __init__(self, repo: str, label: str, runner: Any = None) -> None:
        self.repo = repo
        self.label = label
        self._gh = runner or _gh

    async def list_ready(self, **_: Any) -> list[dict[str, Any]]:
        raw = await self._gh(
            "issue",
            "list",
            "--repo",
            self.repo,
            "--state",
            "open",
            "--label",
            self.label,
            "--json",
            "number,title,body,url",
            "--limit",
            "50",
        )
        items = json.loads(raw or "[]")
        # oldest first, so the queue is worked in the order it was filed
        return sorted(items, key=lambda i: int(i.get("number") or 0))

    async def comment(self, item_id: str, text: str) -> None:
        await self._gh("issue", "comment", str(item_id), "--repo", self.repo, "--body", text)

    async def close(self) -> None:
        return None


async def poll_once(store: Any, client: GitHubIssues) -> list[str]:
    """One mission per ready issue that has none yet; a bad item is skipped."""
    import logging

    from rework.kernel.state.records import Mission

    log = logging.getLogger("rework.sources")
    created: list[str] = []
    for item in await client.list_ready():
        number = item.get("number")
        if number is None:
            continue
        mission_id = mission_id_for(client.repo, number)
        try:
            if await store.get_mission(mission_id) is not None:
                continue
            await store.create_mission(
                Mission(
                    id=mission_id,
                    objective=objective_for(client.repo, item),
                    title=str(item.get("title") or "")[:80] or None,
                    repo_origin=f"https://github.com/{client.repo}.git",
                    result={
                        "source": {
                            "kind": KIND,
                            "item_id": str(number),
                            "repo": client.repo,
                            "url": item.get("url"),
                        }
                    },
                )
            )
            created.append(mission_id)
        except Exception as exc:
            log.warning("github: could not turn #%s into a mission: %s", number, exc)
    return created
