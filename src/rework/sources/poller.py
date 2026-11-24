"""The polling loop that turns ready work items into missions.

Separate from the Plane client so the source is swappable: `poll_once` is the
whole behaviour, and a future GitHub or Linear source implements the same two
methods on its own client.

Everything here is idempotent by construction. A mission id is derived from the
work item id, and a mission that already exists is skipped, so polling twice (or
restarting mid-poll) creates nothing twice. That is the same rule the run-id
scheme uses, for the same reason.
"""

from __future__ import annotations

import logging
from typing import Any

from rework.kernel.state.records import Mission
from rework.kernel.state.store import StateStore
from rework.sources.plane import PlaneClient, mission_id_for, objective_for

log = logging.getLogger("rework.sources")


async def poll_once(
    store: StateStore,
    client: Any,
    *,
    state: str | None = None,
    label: str | None = None,
    project_id: str | None = None,
    create: Any | None = None,
) -> list[str]:
    """Create one mission per ready item that does not have one yet.

    Returns the mission ids created. A failure to list is raised (the caller
    logs it and retries next tick); a failure on ONE item is logged and skipped,
    because a single malformed work item must not stop the queue."""
    items = await client.list_ready(state=state, label=label)
    created: list[str] = []
    for item in items:
        item_id = str(item.get("id") or "")
        if not item_id:
            continue
        mission_id = mission_id_for(item_id)
        try:
            existing = await store.get_mission(mission_id)
            if existing is not None:
                continue
            objective = objective_for(item)
            if create is not None:
                # the daemon's full create_mission (repo resolution, plan, budget)
                mid = await create(objective=objective, mission_id=mission_id)
                if mid:
                    created.append(str(mid))
                continue
            await store.create_mission(
                Mission(
                    id=mission_id,
                    objective=objective,
                    project_id=project_id,
                    title=str(item.get("name") or "")[:80] or None,
                    # where this mission came from, so the write-back knows
                    # which work item to comment on. `result` is free-form and
                    # `_synthesize` carries unknown keys over; a dedicated
                    # column would be a schema change for an optional bridge.
                    result={"source": {"kind": "plane", "item_id": item_id}},
                )
            )
            created.append(mission_id)
        except Exception as exc:  # one bad item must not stop the queue
            log.warning("plane: could not turn item %s into a mission: %s", item_id, exc)
    if created:
        log.info("plane: created %d mission(s): %s", len(created), ", ".join(created))
    return created


def write_back_text(mission: Any) -> str:
    """The one comment a finished mission leaves: what it did and where the
    result is. Deliberately NOT a status feed."""
    result = (mission.result if mission is not None else None) or {}
    bits = [f"rework mission `{mission.id}` finished: {result.get('outcome') or 'done'}."]
    if result.get("summary"):
        bits.append(str(result["summary"])[:1200])
    if result.get("pr_url"):
        bits.append(f"PR: {result['pr_url']}")
    if result.get("branch"):
        bits.append(f"branch: `{result['branch']}`")
    return "\n\n".join(bits)


async def write_back(
    client: Any, store: StateStore, mission_id: str, *, item_id: str | None = None
) -> bool:
    """Comment on the work item a mission came from. Never raises: a tracker
    outage must not fail a finished mission."""
    mission = await store.get_mission(mission_id)
    if mission is None:
        return False
    source = ((mission.result or {}).get("source") or {}) if mission else {}
    target = item_id or source.get("item_id")
    if not target:
        return False
    try:
        await client.comment(str(target), write_back_text(mission))
        return True
    except Exception as exc:
        log.warning("plane: write-back for %s failed: %s", mission_id, exc)
        return False


def plane_config() -> dict[str, Any]:
    """Everything the poller needs, from the environment. Absent token = the
    poller does not run at all, which is the default."""
    import os

    return {
        "url": os.environ.get("REWORK_PLANE_URL", "").strip(),
        "token": os.environ.get("REWORK_PLANE_TOKEN", "").strip(),
        "workspace": os.environ.get("REWORK_PLANE_WORKSPACE", "").strip(),
        "project": os.environ.get("REWORK_PLANE_PROJECT", "").strip(),
        "state": os.environ.get("REWORK_PLANE_STATE", "").strip() or None,
        "label": os.environ.get("REWORK_PLANE_LABEL", "").strip() or None,
        "interval_s": float(os.environ.get("REWORK_PLANE_POLL_S", "60") or 60),
    }


def enabled(config: dict[str, Any] | None = None) -> bool:
    cfg = config or plane_config()
    return bool(cfg["token"] and cfg["url"] and cfg["workspace"] and cfg["project"])


def build_client(config: dict[str, Any]) -> PlaneClient:
    import httpx

    return PlaneClient(
        config["url"],
        config["token"],
        config["workspace"],
        config["project"],
        http=httpx.AsyncClient(),
    )
