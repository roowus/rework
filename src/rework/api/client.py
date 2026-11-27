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
# a chat turn is one or more model calls (tool rounds included)
CHAT_TURN_TIMEOUT_S = 600.0


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
            # not our JSON: an OLDER daemon without the /v1 routes answers with
            # aiohttp's plain 404, which callers treat as "act in-process"
            code = "no_api" if response.status_code == 404 else "bad_response"
            data = {"error": response.text, "code": code}
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

    async def chat_send(
        self, chat_id: str, text: str, images: list[str] | None = None
    ) -> dict[str, Any]:
        """One owner turn, run by the daemon. A model turn can take minutes."""
        return await self._post(
            f"/v1/chats/{chat_id}/messages",
            {"text": text, "images": images},
            wait_s=CHAT_TURN_TIMEOUT_S,
        )

    async def retry_pr(self, mission_id: str) -> tuple[bool, str]:
        out = await self._post(f"/v1/missions/{mission_id}/retry-pr", wait_s=RETRY_PR_TIMEOUT_S)
        return bool(out["ok"]), str(out["detail"])


def configured() -> DaemonClient | None:
    """A client when this front end is pointed at a daemon (REWORK_DAEMON_URL,
    set by a --target or the env file), else None: the front end then acts
    in-process, which is what a single-machine setup and the tests do. A
    remote TUI or CLI always sets the URL, so its commands run on the daemon."""
    if not os.environ.get("REWORK_DAEMON_URL"):
        return None
    return DaemonClient()


class NoApi(Exception):
    """The configured daemon predates the command API (or is unreachable):
    the caller falls back to acting in-process."""


async def call(action: Any) -> Any:
    """Run `action(client)` against the configured daemon. Raises NoApi when
    there is no daemon URL, the daemon has no /v1 routes yet (mixed versions
    during a rollout), or it cannot be reached; an ApiError refusal (409, 404
    from the API itself, 401) propagates, because the daemon DID decide."""
    remote = configured()
    if remote is None:
        raise NoApi("no daemon configured")
    try:
        async with remote:
            return await action(remote)
    except ApiError as exc:
        if exc.code == "no_api":
            raise NoApi(str(exc)) from exc
        raise
    except httpx.TransportError as exc:
        raise NoApi(f"daemon unreachable: {exc}") from exc


async def stream_events(on_event: Any, *, stop: Any = None) -> None:
    """Follow the configured daemon's /v1/events and call `on_event(dict)` for
    each one, reconnecting with backoff, until `stop()` is true. Returns at once
    when no daemon is configured. Wake-ups only: the caller re-reads state."""
    import asyncio
    import json as _json

    if not os.environ.get("REWORK_DAEMON_URL"):
        return
    backoff = 1.0
    while not (stop and stop()):
        client = DaemonClient()
        try:
            async with client._http.stream("GET", "/v1/events", timeout=None) as response:
                if response.status_code != 200:
                    return  # an older daemon without the stream: polling covers it
                backoff = 1.0
                async for line in response.aiter_lines():
                    if stop and stop():
                        return
                    if line.startswith("data: "):
                        try:
                            await on_event(_json.loads(line[6:]))
                        except Exception:
                            pass  # one bad event never ends the stream
        except (httpx.TransportError, httpx.HTTPError):
            pass
        finally:
            await client.aclose()
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30.0)
