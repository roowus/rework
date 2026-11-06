"""Owner actions shared by every surface (chat cards, dashboard, gates tab,
menus). One implementation each, so a button in the Work tab and the same
button in the Dashboard can never disagree. All writes go through TuiData
(the store); the daemon owns the consequences."""

from __future__ import annotations

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
