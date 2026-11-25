"""Plane as a mission source: poll for work items, create missions, comment back.

Pull, not push. On the deployment this was built against (`plane.rewis`, Plane
v1.5.0) a webhook could not work: the Plane API container could not reach the
host (`EHOSTUNREACH` from `plane-app_default` to the bridge gateway), and Plane's
own SSRF validator blocks `100.64.0.0/10`, the Tailscale range, so a webhook to
the daemon's tailnet address is rejected at creation time. A poller needs neither
a route nor an allowlist entry, and it matches rework's rule that every decision
re-derives from rows (CLAUDE.md rule 3).

The request surface is tiny on purpose: list ready items, comment, close. That is
everything a bridge needs, and every line of it can be tested against a fake
transport.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

# Plane allows 60 requests per minute per key; stay under it with room for a
# manual `rework` call sharing the same key.
MAX_REQUESTS_PER_MINUTE = 55

# The fields a mission needs, and nothing else: full work items carry a body,
# comments and activity that would triple the payload for no benefit.
ITEM_FIELDS = "id,name,description,state,labels,sequence_id,updated_at"


class PlaneError(RuntimeError):
    """A Plane call failed. Never raised for a single bad item: the poller logs
    it and moves on, because one unreadable work item must not stop the others."""


class PlaneClient:
    """A bounded, allowlisted client for one Plane workspace."""

    def __init__(
        self,
        base_url: str,
        token: str,
        workspace: str,
        project: str,
        *,
        http: Any | None = None,
        timeout_s: float = 20.0,
        requests_per_minute: int = MAX_REQUESTS_PER_MINUTE,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._token = token
        self.workspace = workspace
        self.project = project
        self._http = http
        self._timeout = timeout_s
        self._min_interval = 60.0 / max(1, requests_per_minute)
        self._last_call = 0.0

    # -- plumbing ----------------------------------------------------------
    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """One call, rate-limited and timeout-bounded. `http` is injectable so
        the tests need no server."""
        wait = self._min_interval - (time.monotonic() - self._last_call)
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_call = time.monotonic()
        if self._http is None:  # pragma: no cover - the daemon always injects one
            raise PlaneError("no HTTP client configured")
        try:
            response = await asyncio.wait_for(
                self._http.request(
                    method,
                    f"{self._base}{path}",
                    headers={"X-API-Key": self._token, "Accept": "application/json"},
                    **kwargs,
                ),
                timeout=self._timeout,
            )
        except TimeoutError as exc:
            raise PlaneError(f"{method} {path} timed out after {self._timeout:.0f}s") from exc
        if response.status_code >= 400:
            raise PlaneError(f"{method} {path} -> {response.status_code}: {response.text[:200]}")
        return response.json()

    def _project_path(self, suffix: str = "") -> str:
        return f"/api/v1/workspaces/{self.workspace}/projects/{self.project}/work-items/{suffix}"

    # -- the surface ------------------------------------------------------
    async def list_ready(self, *, state: str | None, label: str | None) -> list[dict[str, Any]]:
        """Work items in `state` carrying `label`, oldest update first.

        Both filters are configurable and both are OPTIONAL: a deployment that
        labels nothing still gets a stable order, and "what runs" stays a human
        decision made in the tracker either way."""
        params: dict[str, Any] = {"per_page": 100, "fields": ITEM_FIELDS}
        if state:
            params["state"] = state
        data = await self._request("GET", self._project_path(), params=params)
        items = data.get("results", data) if isinstance(data, dict) else data
        if not isinstance(items, list):
            raise PlaneError(f"unexpected work-item payload: {type(items).__name__}")
        if label:
            items = [i for i in items if _has_label(i, label)]
        items = [i for i in items if isinstance(i, dict) and i.get("id")]
        return sorted(items, key=lambda i: (str(i.get("updated_at") or ""), str(i["id"])))

    async def comment(self, item_id: str, text: str) -> None:
        await self._request(
            "POST",
            self._project_path(f"{item_id}/comments/"),
            json={"comment_html": text},
        )

    async def close(self) -> None:
        if self._http is not None and hasattr(self._http, "aclose"):
            await self._http.aclose()


def _has_label(item: dict[str, Any], label: str) -> bool:
    """A work item's labels come back as ids or as objects depending on the
    endpoint's expansion; match either, by id or by name."""
    for entry in item.get("labels") or []:
        if isinstance(entry, str) and entry == label:
            return True
        if isinstance(entry, dict) and label in (entry.get("id"), entry.get("name")):
            return True
    return False


def mission_id_for(item_id: str) -> str:
    """Deterministic, so polling twice creates one mission and a restart does
    not duplicate (the same rule as `create_run`'s deterministic ids)."""
    import hashlib

    return "m-plane-" + hashlib.sha256(item_id.encode()).hexdigest()[:12]


def objective_for(item: dict[str, Any]) -> str:
    """The name first (that is what a human wrote as the task), the body after
    it so the role has the detail."""
    name = str(item.get("name") or "").strip() or "untitled work item"
    body = str(item.get("description") or "").strip()
    seq = item.get("sequence_id")
    prefix = f"[plane #{seq}] " if seq is not None else "[plane] "
    return prefix + (f"{name}\n\n{body}" if body else name)
