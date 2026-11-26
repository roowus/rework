"""Client for the daemon's /v1 command API.

One method per route. A refusal (409 illegal, 404 not found) raises ApiError
carrying the daemon's own message, so a front end shows exactly what the daemon
decided ("cannot resume mission m-...: it is done").
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from rework.orchestrator.workspace import workspace_url_from_env

TIMEOUT_S = 30.0
# retry-pr pushes and opens a PR: allow the daemon's delivery lock wait
RETRY_PR_TIMEOUT_S = 300.0


class ApiError(RuntimeError):
    def __init__(self, status: int, code: str, message: str) -> None:
        self.status = status
        self.code = code
        super().__init__(message)


class DaemonClient:
    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = (base_url or workspace_url_from_env()).rstrip("/")
        self._token = token if token is not None else os.environ.get("REWORK_TOKEN")
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        self._http = httpx.AsyncClient(
            base_url=self.base_url, headers=headers, timeout=TIMEOUT_S, transport=transport
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> DaemonClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def _post(
        self, path: str, body: dict[str, Any] | None = None, *, wait_s: float | None = None
    ) -> dict[str, Any]:
        response = await self._http.post(path, json=body or {}, timeout=wait_s or TIMEOUT_S)
        data: dict[str, Any]
        try:
            data = response.json()
        except ValueError:
            data = {"error": response.text, "code": "bad_response"}
        if response.status_code >= 400:
            raise ApiError(
                response.status_code,
                str(data.get("code") or "error"),
                str(data.get("error") or response.text),
            )
        return data

    async def version(self) -> int:
        response = await self._http.get("/v1")
        response.raise_for_status()
        return int(response.json()["api"])

    async def mission_verb(self, mission_id: str, verb: str) -> dict[str, Any]:
        return (await self._post(f"/v1/missions/{mission_id}/{verb}"))["mission"]

    async def prioritize(self, mission_id: str, priority: str) -> dict[str, Any]:
        out = await self._post(f"/v1/missions/{mission_id}/prioritize", {"priority": priority})
        return out["mission"]

    async def message(self, mission_id: str, text: str) -> int | None:
        return (await self._post(f"/v1/missions/{mission_id}/message", {"text": text}))["seq"]

    async def decide_gate(
        self,
        gate_id: str,
        approved: bool,
        *,
        note: str | None = None,
        decision: dict[str, Any] | None = None,
        send_back: bool = False,
    ) -> dict[str, Any]:
        return await self._post(
            f"/v1/gates/{gate_id}/decide",
            {"approved": approved, "note": note, "decision": decision, "send_back": send_back},
        )

    async def retry_pr(self, mission_id: str) -> tuple[bool, str]:
        out = await self._post(f"/v1/missions/{mission_id}/retry-pr", wait_s=RETRY_PR_TIMEOUT_S)
        return bool(out["ok"]), str(out["detail"])
