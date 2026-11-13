"""Owner actions shared by every surface (chat cards, dashboard, gates tab,
menus). One implementation each, so a button in the Work tab and the same
button in the Dashboard can never disagree. All writes go through TuiData
(the store); the daemon owns the consequences."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rework.tui.theme import clip

if TYPE_CHECKING:
    from rework.tui.app import ReworkApp

VERBS = {
    "pause": ("pause_mission", "Paused", "Pausing stops the roles; everything stays resumable."),
    "resume": ("resume_mission", "Resumed", ""),
    "cancel": ("cancel_mission", "Cancelled", ""),
}

ACTIVE = {"pending", "running", "awaiting_gate", "on_hold"}


def can(verb: str, status: str) -> bool:
    """Which controls make sense for a mission status."""
    if verb == "pause":
        return status in {"pending", "running", "awaiting_gate"}
    if verb == "resume":
        return status == "on_hold"
    if verb == "cancel":
        return status in ACTIVE
    if verb == "fetch":
        return status == "done"
    return False


async def control(app: ReworkApp, mission_id: str, verb: str) -> bool:
    if verb == "fetch":
        return await fetch(app, mission_id)
    if verb not in VERBS:
        app.notify(f"Unknown action {verb}", severity="error")
        return False
    method, done, _ = VERBS[verb]
    try:
        result = await getattr(app.hub.data, method)(mission_id)
    except Exception as exc:
        app.notify(f"{verb} failed: {exc}", severity="error", timeout=6)
        return False
    if result is None:
        app.notify(f"No mission {mission_id}", severity="error")
        return False
    app.notify(f"{done} {mission_id}", timeout=3)
    await app.hub.refresh()
    return True


async def fetch(app: ReworkApp, mission_id: str) -> bool:
    app.notify(f"Fetching {mission_id}…", timeout=3)
    try:
        dest, count = await app.hub.data.fetch_result(mission_id)
    except FileNotFoundError:
        app.notify("Nothing to fetch for this mission", severity="warning")
        return False
    except Exception as exc:
        app.notify(f"Fetch failed: {clip(str(exc), 160)}", severity="error", timeout=8)
        return False
    app.copy_to_clipboard(dest)
    app.notify(f"{count} files → {dest} (path copied)", title="Fetched", timeout=8)
    return True


async def decide(
    app: ReworkApp,
    gate_id: str,
    approved: bool,
    *,
    note: str | None = None,
    decision: dict[str, Any] | None = None,
) -> bool:
    """Answer a gate. Verified by re-reading: a gate that silently stays open is
    the worst failure this UI has had, so a False result is shown loudly."""
    try:
        ok = await app.hub.data.decide_gate(gate_id, approved, note=note, decision=decision)
    except Exception as exc:
        app.notify(f"Could not answer gate: {exc}", severity="error", timeout=10)
        return False
    if not ok:
        app.notify(
            "The gate did not take the answer (already decided elsewhere?)",
            severity="error",
            timeout=8,
        )
        await app.hub.refresh()
        return False
    app.notify("Approved" if approved else "Rejected", timeout=2)
    await app.hub.refresh()
    return True


def is_git_repo(repo_dir: str | Path) -> bool:
    """`rework preview --repo-dir` fetches into a real git repo; anything else
    blows up deep inside the CLI, so refuse up front with something readable."""
    try:
        top = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
        )
    except OSError:
        return False
    return top.returncode == 0


async def run_preview(app: ReworkApp, pr_number: str, repo_dir: str) -> bool:
    """Suspend this TUI, hand the terminal to `rework preview <n> --repo-dir`,
    come back when it exits. The ONE place a preview is launched from the TUI:
    the mission card's Preview button and the Mission menu both come here."""
    repo_dir = str(repo_dir or "").strip()
    if not repo_dir:
        app.notify("Give the path of a local git repo to preview", severity="error", timeout=6)
        return False
    if not is_git_repo(repo_dir):
        app.notify(f"Not a git repo: {repo_dir}", severity="error", timeout=8)
        return False
    cmd = ["rework", "preview", str(pr_number), "--repo-dir", repo_dir]
    try:
        app.notify(f"Starting preview of PR {pr_number}…", timeout=3)
        with app.suspend():
            # blocking on purpose: suspend() handed the terminal to the child
            subprocess.run(cmd)  # preview suspend: terminal handed to child: ASYNC221
    except Exception as exc:
        app.notify(str(exc), severity="error", timeout=8)
        return False
    await app.hub.refresh()
    return True


async def preview(app: ReworkApp, mission_id: str) -> bool:
    """Run the mission's PR as a preview (ADR 0008): read the PR number and the
    repo off the mission, then hand over to the shared runner."""
    try:
        mission = await app.hub.data.get_mission(mission_id)
    except Exception as exc:
        app.notify(f"Could not load {mission_id}: {exc}", severity="error", timeout=6)
        return False
    if mission is None:
        app.notify(f"No mission {mission_id}", severity="error")
        return False
    pr_url = (getattr(mission, "result", None) or {}).get("pr_url")
    if not pr_url:
        app.notify("This mission has no pull request yet", severity="warning", timeout=4)
        return False
    match = re.search(r"/pull/(\d+)", str(pr_url))
    if match is None:
        app.notify(f"cannot parse PR number from {pr_url}", severity="error", timeout=6)
        return False
    # the repo must come from the MISSION, never from this process's cwd (which
    # is what --repo-dir defaults to): fetching the wrong repo is worse than
    # refusing (ADR 0008, section 1)
    repo_path = getattr(mission, "repo_path", None)
    if not repo_path:
        app.notify(
            f"No repo recorded on {mission_id}; cannot preview PR {match.group(1)}",
            severity="error",
            timeout=8,
        )
        return False
    return await run_preview(app, match.group(1), str(repo_path))
