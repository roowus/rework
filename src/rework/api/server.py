"""`/v1` command routes, mounted on the daemon's existing aiohttp app.

Every route is a thin shell over code that already exists and is tested:
`rework.services.missions` (guarded pause/resume/cancel), `GateManager`
(atomic gate decisions), `missions.send_owner_message`, `delivery.retry_pr`.
Nothing here re-implements a rule.

Errors are JSON `{"error": ..., "code": ...}`:
  404 not_found        unknown mission or gate
  409 illegal          the verb does not apply to the mission's status
  400 bad_request      malformed input
  401 unauthorized     REWORK_TOKEN set and the bearer token is wrong
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from aiohttp import web

from rework.kernel.events.bus import EventBus
from rework.kernel.state.records import Gate, Mission
from rework.kernel.state.store import StateStore
from rework.kernel.state.transitions import IllegalTransition

log = logging.getLogger("rework.api")

API_VERSION = 1
# seconds between keepalive comments on an idle event stream
KEEPALIVE_S = 15.0
# how often the stream re-reads the events table when nothing woke it (writes
# from other processes do not wake the in-process bus)
TAIL_POLL_S = 1.0


def _error(status: int, code: str, message: str) -> web.Response:
    return web.json_response({"error": message, "code": code}, status=status)


def mission_json(m: Mission) -> dict[str, Any]:
    return m.model_dump(mode="json", exclude={"light"})


def gate_json(g: Gate) -> dict[str, Any]:
    return g.model_dump(mode="json")


async def _body(request: web.Request) -> dict[str, Any]:
    if not request.can_read_body:
        return {}
    try:
        data = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise web.HTTPBadRequest(
            text=json.dumps({"error": f"body is not JSON: {exc}", "code": "bad_request"}),
            content_type="application/json",
        ) from exc
    if not isinstance(data, dict):
        raise web.HTTPBadRequest(
            text=json.dumps({"error": "body must be a JSON object", "code": "bad_request"}),
            content_type="application/json",
        )
    return data


def add_routes(
    app: web.Application,
    store: StateStore,
    bus: EventBus,
    *,
    authorized: Any,
    git_author: tuple[str, str] | None = None,
    library: Any | None = None,
) -> None:
    """Mount the /v1 command routes. `authorized(request) -> bool` is the
    workspace API's own bearer check, so one token guards both."""
    from rework.services import missions as mission_service

    def guard(handler: Any) -> Any:
        async def wrapped(request: web.Request) -> web.StreamResponse:
            if not authorized(request):
                return _error(401, "unauthorized", "unauthorized")
            try:
                result: web.StreamResponse = await handler(request)
                return result
            except IllegalTransition as exc:
                return _error(409, "illegal", str(exc))
            except KeyError as exc:
                return _error(404, "not_found", f"not found: {exc.args[0]}")
            except ValueError as exc:
                return _error(400, "bad_request", str(exc))

        return wrapped

    async def version(_: web.Request) -> web.Response:
        return web.json_response({"api": API_VERSION})

    async def mission_verb(request: web.Request) -> web.Response:
        verb = request.match_info["verb"]
        action = mission_service.VERBS.get(verb)
        if action is None:
            return _error(404, "not_found", f"unknown verb {verb!r}")
        mission = await action(store, request.match_info["mission_id"])
        return web.json_response({"mission": mission_json(mission)})

    async def prioritize(request: web.Request) -> web.Response:
        from rework.kernel.state.records import check_priority

        body = await _body(request)
        level = check_priority(str(body.get("priority") or "high").strip().lower())
        mission_id = request.match_info["mission_id"]
        if await store.get_mission(mission_id) is None:
            raise KeyError(mission_id)
        updated = await store.update_mission(mission_id, priority=level)
        assert updated is not None
        return web.json_response({"mission": mission_json(updated)})

    async def message(request: web.Request) -> web.Response:
        from rework.missions import send_owner_message

        body = await _body(request)
        text = str(body.get("text") or "").strip()
        if not text:
            raise ValueError("text is required")
        mission_id = request.match_info["mission_id"]
        if await store.get_mission(mission_id) is None:
            raise KeyError(mission_id)
        event = await send_owner_message(mission_id, text, store=store)
        return web.json_response({"seq": event.seq})

    async def decide(request: web.Request) -> web.Response:
        from rework.kernel.gates import GateManager

        body = await _body(request)
        if "approved" not in body and not body.get("send_back"):
            raise ValueError("approved (true/false) or send_back is required")
        gate_id = request.match_info["gate_id"]
        before = await store.get_gate(gate_id)
        if before is None:
            raise KeyError(gate_id)
        decision = body.get("decision")
        if decision is not None and not isinstance(decision, dict):
            raise ValueError("decision must be an object")
        manager = GateManager(store, bus)
        gate = await manager.decide(
            gate_id,
            bool(body.get("approved")),
            body.get("note"),
            decision,
            bool(body.get("send_back")),
        )
        assert gate is not None
        # a gate already decided by someone else is reported, not re-decided;
        # "decided" is true only for the call whose write actually landed
        return web.json_response({"gate": gate_json(gate), "decided": manager.last_decided})

    async def retry(request: web.Request) -> web.Response:
        from rework.orchestrator.delivery import retry_pr

        mission_id = request.match_info["mission_id"]
        if await store.get_mission(mission_id) is None:
            raise KeyError(mission_id)
        kwargs = {"author": git_author} if git_author else {}
        ok, detail = await retry_pr(store, bus, mission_id, **kwargs)
        return web.json_response({"ok": ok, "detail": detail})

    async def chat_send(request: web.Request) -> web.Response:
        from rework.services import chat as chat_service

        body = await _body(request)
        text = str(body.get("text") or "").strip()
        if not text:
            raise ValueError("text is required")
        images = body.get("images") or None
        if images is not None and not isinstance(images, list):
            raise ValueError("images must be a list of paths")
        result = await chat_service.send(
            store, request.match_info["chat_id"], text, images=images, library=library
        )
        return web.json_response(result)

    async def events(request: web.Request) -> web.StreamResponse:
        """Server-sent events (ADR 0021 phase 3): every event the store records,
        in seq order, as it lands. The stream TAILS THE EVENTS TABLE rather than
        the in-process bus, so it also carries what other processes wrote (a
        CLI, a second store connection, a direct owner message): the store is
        the truth (CLAUDE.md rule 3). The bus only wakes the tail early.

        `?since=<seq>` resumes after a reconnect without a gap; default is "from
        now". A comment line every KEEPALIVE_S keeps proxies from closing an
        idle stream and lets a client notice a dead one."""
        response = web.StreamResponse(
            headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"}
        )
        await response.prepare(request)
        try:
            since = int(request.query.get("since") or 0)
        except ValueError:
            since = 0
        if since <= 0:
            newest = await store.events_since(0, limit=1, newest_first=True)
            since = int(newest[-1].seq or 0) if newest else 0
        wake = bus.subscribe()
        try:
            await response.write(b": connected\n\n")
            idle = 0.0
            while True:
                batch = await store.events_since(since, limit=200)
                for event in batch:
                    since = max(since, int(event.seq or 0))
                    data = json.dumps(
                        {
                            "seq": event.seq,
                            "type": event.type.value,
                            "mission_id": event.mission_id,
                            "run_id": event.run_id,
                        }
                    )
                    await response.write(
                        f"id: {event.seq}\nevent: {event.type.value}\ndata: {data}\n\n".encode()
                    )
                if batch:
                    idle = 0.0
                    continue
                try:
                    await asyncio.wait_for(wake.get(), TAIL_POLL_S)
                except TimeoutError:
                    idle += TAIL_POLL_S
                    if idle >= KEEPALIVE_S:
                        idle = 0.0
                        await response.write(b": keepalive\n\n")
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            bus.unsubscribe(wake)
        return response

    async def run_ask(request: web.Request) -> web.Response:
        """A role whose loop runs OUTSIDE the daemon (pi, ADR 0022) asks the
        orchestrator through the same run-row ask the native loop uses: the
        question lands on the run row, the orchestrator (or the owner) answers
        it, and this call returns the answer. Long-poll: it waits."""
        from rework.nodes.escalate import EscalationAwareExecutor

        body = await _body(request)
        question = str(body.get("question") or "").strip()
        if not question:
            raise ValueError("question is required")
        run = await store.get_run(request.match_info["run_id"])
        if run is None:
            raise KeyError(request.match_info["run_id"])
        mission = await store.get_mission(run.mission_id)
        if mission is None:
            raise KeyError(run.mission_id)

        class _Ctx:  # the slice of RoleBuildCtx the ask path reads
            pass

        ctx = _Ctx()
        ctx.store, ctx.bus, ctx.run, ctx.mission = store, bus, run, mission  # type: ignore[attr-defined]
        from rework.kernel.gates import GateManager

        ctx.gates = GateManager(store, bus)  # type: ignore[attr-defined]
        from contextlib import nullcontext

        ctx.parked = nullcontext  # type: ignore[attr-defined]
        answer = await EscalationAwareExecutor(None, ctx).execute(
            "ask_orchestrator", {"question": question, "kind": body.get("kind") or "question"}
        )
        return web.json_response({"answer": answer})

    async def library_search(request: web.Request) -> web.Response:
        if library is None:
            return web.json_response({"items": [], "note": "no library on this daemon"})
        query = str(request.query.get("q") or "").strip()
        if not query:
            raise ValueError("q is required")
        items = await library.search_items(query, request.query.get("type") or None)
        return web.json_response(
            {
                "items": [
                    {
                        "id": i.id,
                        "type": i.type.value,
                        "name": i.name,
                        "description": i.description,
                        "body": (i.body or "")[:4000],
                    }
                    for i in items[:20]
                ]
            }
        )

    app.router.add_get("/v1", guard(version))
    app.router.add_get("/v1/events", guard(events))
    app.router.add_post("/v1/runs/{run_id}/ask", guard(run_ask))
    app.router.add_get("/v1/library/search", guard(library_search))
    app.router.add_post("/v1/chats/{chat_id}/messages", guard(chat_send))
    app.router.add_post("/v1/missions/{mission_id}/{verb:pause|resume|cancel}", guard(mission_verb))
    app.router.add_post("/v1/missions/{mission_id}/prioritize", guard(prioritize))
    app.router.add_post("/v1/missions/{mission_id}/message", guard(message))
    app.router.add_post("/v1/missions/{mission_id}/retry-pr", guard(retry))
    app.router.add_post("/v1/gates/{gate_id}/decide", guard(decide))
