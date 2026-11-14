"""GitHub operations via the gh CLI (thin, mechanical, no gate by default).

After a clean audited `done` the engine pushes the branch and opens the PR with
no gate; the orchestrator may request one (`ask_before_pr` + reason) and a
project `settings.pr_gate` of `always`/`never` overrides. Merging is always the
owner's. REWORK_GH_BIN overrides the binary (tests use a fake that records the
call and prints a URL).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path


async def pr_create(
    repo_path: Path,
    head: str,
    title: str,
    body: str,
    base: str = "main",
    timeout_s: float = 60.0,
) -> str:
    """Create a PR from `head` into `base`; returns the PR URL."""
    gh_bin = os.environ.get("REWORK_GH_BIN", "gh")
    proc = await asyncio.create_subprocess_exec(
        gh_bin,
        "pr",
        "create",
        "--head",
        head,
        "--base",
        base,
        "--title",
        title,
        "--body",
        body,
        cwd=str(repo_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError:
        proc.terminate()
        raise RuntimeError("gh pr create timed out") from None
    text = out.decode(errors="replace").strip()
    if proc.returncode != 0:
        raise RuntimeError(f"gh pr create failed: {text}")
    for token in text.split():
        if token.startswith("http"):
            return token
    raise RuntimeError(f"gh pr create produced no URL: {text}")
