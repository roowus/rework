"""Where missions come from, other than a human typing one.

A source turns work items in another system into missions. It is deliberately
one-way and small: the tracker decides WHAT to work on (a list someone can read),
rework decides how, and one comment goes back when the mission finishes. The
tracker is never the source of truth for mission state; the store is (ADR 0016).
"""

from typing import Any


def configured() -> list[dict[str, Any]]:
    """Every source this daemon should poll: [{kind, interval_s, client, poll}].
    Plane and GitHub Issues today; each is off unless its env is set."""
    out: list[dict[str, Any]] = []
    from rework.sources import github, poller

    plane = poller.plane_config()
    if poller.enabled(plane):
        client = poller.build_client(plane)

        async def plane_poll(store: Any, client: Any = client, cfg: Any = plane) -> list[str]:
            return await poller.poll_once(store, client, state=cfg["state"], label=cfg["label"])

        out.append(
            {
                "kind": "plane",
                "interval_s": plane["interval_s"],
                "client": client,
                "poll": plane_poll,
                "where": plane["url"],
            }
        )
    gh_cfg = github.config()
    if github.enabled(gh_cfg):
        gh_client = github.GitHubIssues(gh_cfg["repo"], gh_cfg["label"])

        async def gh_poll(store: Any, client: Any = gh_client) -> list[str]:
            return await github.poll_once(store, client)

        out.append(
            {
                "kind": "github",
                "interval_s": gh_cfg["interval_s"],
                "client": gh_client,
                "poll": gh_poll,
                "where": f"github:{gh_cfg['repo']} label {gh_cfg['label']}",
            }
        )
    return out


def client_for(kind: str, source: dict[str, Any]) -> Any:
    """The write-back client for a mission's recorded source, or None."""
    from rework.sources import github, poller

    if kind == "plane":
        cfg = poller.plane_config()
        return poller.build_client(cfg) if poller.enabled(cfg) else None
    if kind == github.KIND:
        repo = str(source.get("repo") or github.config()["repo"])
        return github.GitHubIssues(repo, github.config()["label"]) if repo else None
    return None


# Mission status -> the phase a tracker shows. Coarse on purpose: a tracker
# reader wants "is it being worked, does it need me, is it over".
PHASES = {
    "running": "working",
    "pending": "working",
    "awaiting_gate": "needs_you",
    "on_hold": "needs_you",
    "done": "done",
    "failed": "failed",
    "cancelled": "failed",
}


async def sync_status(source: dict[str, Any], status: str) -> None:
    """Show a mission's phase on the work item it came from. Best-effort and
    bounded: a tracker outage is a log line, never a mission failure."""
    import asyncio
    import logging

    phase = PHASES.get(status)
    kind = str(source.get("kind") or "")
    item_id = source.get("item_id")
    if phase is None or not item_id:
        return
    client = client_for(kind, source)
    if client is None or not hasattr(client, "set_status"):
        return
    try:
        await asyncio.wait_for(client.set_status(str(item_id), phase), 20)
    except Exception as exc:
        logging.getLogger("rework.sources").warning(
            "%s status sync (%s) failed: %s", kind, phase, exc
        )
    finally:
        try:
            await client.close()
        except Exception:
            pass
