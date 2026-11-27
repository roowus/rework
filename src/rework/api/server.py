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
            except LookupError as exc:  # a capability this daemon lacks
                return _error(409, "unavailable", str(exc))

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

    async def snapshot(request: web.Request) -> web.Response:
        """A front end's whole heartbeat in one read (services/snapshot.py):
        missions (light), open gates, projects, listed chats, spend since
        `?spend_since=<epoch>`, budget raises after `?events_after=<seq>` and
        the spinner's event stamps. The TUI never needs the database for it."""
        from rework.services import snapshot as snapshot_service

        def number(name: str, kind: Any) -> Any:
            raw = request.query.get(name)
            if raw in (None, ""):
                return None
            try:
                return kind(raw)
            except ValueError:
                raise ValueError(f"{name} must be a number") from None

        snap = await snapshot_service.build(
            store,
            spend_since=number("spend_since", float),
            events_after=number("events_after", int),
        )
        response = web.json_response(snapshot_service.to_json(snap))
        # ~130 KiB of repetitive JSON per poll on a busy store: compression (httpx
        # asks for it by default) cuts the tailnet cost several-fold
        response.enable_compression()
        return response

    async def read(request: web.Request) -> web.Response:
        """One StateStore READ, run on the daemon's store (ADR 0021 phase 3c):
        `{"method": "list_runs", "args": {"mission_id": "m-1"}}` ->
        `{"result": ...}`. Only the allowlisted read methods are served
        (api/remote_store.READS); a write is refused as 400."""
        from rework.api import remote_store

        body = await _body(request)
        method = str(body.get("method") or "")
        if method not in remote_store.READS:
            raise ValueError(f"{method or '(none)'} is not a remote read")
        args = body.get("args") or {}
        if not isinstance(args, dict):
            raise ValueError("args must be an object")
        from pydantic import ValidationError

        try:
            kwargs = remote_store.unbind(method, args)
        except ValidationError as exc:
            raise ValueError(f"bad arguments for {method}: {exc.errors()[0]['msg']}") from None
        result = await getattr(store, method)(**kwargs)
        response = web.json_response({"result": remote_store.encode(method, result)})
        response.enable_compression()
        return response

    # -- owner writes (ADR 0021 phase 3d): chats, missions, projects ---------

    def _opt_str(body: dict[str, Any], key: str) -> str | None:
        value = body.get(key)
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError(f"{key} must be a string")
        return value

    async def chat_create(request: web.Request) -> web.Response:
        from rework.kernel.state.records import Chat

        body = await _body(request)
        chat = await store.create_chat(
            Chat(
                title=_opt_str(body, "title") or "",
                project_id=_opt_str(body, "project_id"),
                model_id=_opt_str(body, "model_id"),
                effort=_opt_str(body, "effort"),
            )
        )
        return web.json_response({"chat": chat.model_dump(mode="json")})

    async def chat_update(request: web.Request) -> web.Response:
        """Patch title / project / model / effort / first mission. A field that
        is absent or null is left alone (StateStore.update_chat's contract)."""
        body = await _body(request)
        allowed = {"title", "project_id", "model_id", "effort", "mission_id"}
        unknown = set(body) - allowed
        if unknown:
            raise ValueError(f"cannot set {sorted(unknown)[0]} on a chat")
        chat_id = request.match_info["chat_id"]
        if await store.get_chat(chat_id) is None:
            raise KeyError(chat_id)
        fields = {k: _opt_str(body, k) for k in allowed if k in body}
        chat = await store.update_chat(chat_id, **fields)
        assert chat is not None
        return web.json_response({"chat": chat.model_dump(mode="json")})

    async def chat_delete(request: web.Request) -> web.Response:
        chat_id = request.match_info["chat_id"]
        if await store.get_chat(chat_id) is None:
            raise KeyError(chat_id)
        await store.delete_chat(chat_id)
        return web.json_response({"deleted": chat_id})

    async def chat_note(request: web.Request) -> web.Response:
        """A UI-only transcript row ('launched m-...'): body stays empty so it
        is never replayed into the model's history. The reply carries the row
        id so the writing TUI knows the row is its own (it painted it live)."""
        from rework.kernel.state.records import ChatMessage

        body = await _body(request)
        display = _opt_str(body, "display") or ""
        if not display.strip():
            raise ValueError("display is required")
        chat_id = request.match_info["chat_id"]
        if await store.get_chat(chat_id) is None:
            raise KeyError(chat_id)
        saved = await store.append_chat_message(
            ChatMessage(chat_id=chat_id, role="note", body={}, display=display)
        )
        return web.json_response({"id": saved.id})

    async def mission_create(request: web.Request) -> web.Response:
        """`rework new` / a launched proposal, created BY the daemon: the same
        `missions.create_mission` (budget, plan and repo rules included). A
        local `repo` path resolves on the daemon's machine, where the mission
        will run; a client folder travels by the workspace upload instead."""
        from rework.missions import create_mission

        body = await _body(request)
        objective = (_opt_str(body, "objective") or "").strip()
        if not objective:
            raise ValueError("objective is required")
        kwargs: dict[str, Any] = {}
        for key in (
            "repo",
            "project_id",
            "model",
            "effort",
            "chat_id",
            "orchestrator_model",
            "orchestrator_effort",
            "title",
        ):
            value = _opt_str(body, key)
            if value is not None:
                kwargs[key] = value
        plan = _opt_str(body, "plan")
        if plan is not None:
            kwargs["plan"] = plan
        priority = _opt_str(body, "priority")
        if priority is not None:
            kwargs["priority"] = priority
        for key in ("budget_usd", "budget_step_usd"):
            value = body.get(key)
            if value is not None:
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ValueError(f"{key} must be a number")
                kwargs[key] = float(value)
        mission = await create_mission(objective, store=store, **kwargs)
        return web.json_response({"mission": mission_json(mission)})

    async def project_create(request: web.Request) -> web.Response:
        from rework.services.projects import create_project

        body = await _body(request)
        name = (_opt_str(body, "name") or "").strip()
        if not name:
            raise ValueError("name is required")
        repos = body.get("repos") or []
        if not isinstance(repos, list) or not all(isinstance(r, dict) for r in repos):
            raise ValueError("repos must be a list of objects")
        project = await create_project(store, name, repos)
        return web.json_response({"project": project.model_dump(mode="json")})

    def _library() -> Any:
        if library is None:
            raise LookupError("this daemon has no Library configured")
        return library

    async def library_items(request: web.Request) -> web.Response:
        """The TUI's Library tab: `?q=` keyword (or hybrid with `mode=hybrid`)
        search, empty `q` lists every status. Full items (bodies included)."""
        lib = _library()
        query = str(request.query.get("q") or "").strip()
        if not query:
            items = await lib.index.list_items(status=None, limit=100)
            mode = ""
        elif request.query.get("mode") == "hybrid":
            items = await lib.search_items(query)
            mode = "hybrid" if lib.hybrid is not None else "keyword"
        else:
            items = await lib.index.search(query, limit=50)
            mode = "keyword"
        return web.json_response(
            {"items": [i.model_dump(mode="json") for i in items], "mode": mode}
        )

    async def library_item(request: web.Request) -> web.Response:
        lib = _library()
        item = await lib.index.get_item(request.match_info["item_id"])
        if item is None:
            raise KeyError(request.match_info["item_id"])
        return web.json_response({"item": item.model_dump(mode="json")})

    async def library_add(request: web.Request) -> web.Response:
        """The TUI's Add form, executed on the daemon: the same curation
        `rework library add --force` applies (owner-typed, so forced)."""
        from rework.library.curation import contribute
        from rework.library.model import ItemType

        lib = _library()
        body = await _body(request)
        name = (_opt_str(body, "name") or "").strip()
        text = _opt_str(body, "body") or ""
        if not name or not text.strip():
            raise ValueError("name and body are required")
        tags = body.get("tags") or []
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            raise ValueError("tags must be a list of strings")
        item, note = await contribute(
            lib.index,
            lib.git,
            type_=ItemType(_opt_str(body, "type") or "skill"),
            name=name,
            description=_opt_str(body, "description") or "",
            body=text,
            tags=tags,
            mission_id=None,
            project=_opt_str(body, "project"),
            force=True,
        )
        return web.json_response({"item": item.model_dump(mode="json"), "note": note})

    async def library_status(request: web.Request) -> web.Response:
        from rework.library.model import ItemStatus

        lib = _library()
        body = await _body(request)
        status = ItemStatus(str(body.get("status") or ""))
        item = await lib.index.set_status(request.match_info["item_id"], status)
        if item is None:
            raise KeyError(request.match_info["item_id"])
        return web.json_response({"item": item.model_dump(mode="json")})

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
    app.router.add_get("/v1/snapshot", guard(snapshot))
    app.router.add_post("/v1/read", guard(read))
    app.router.add_post("/v1/runs/{run_id}/ask", guard(run_ask))
    app.router.add_get("/v1/library/search", guard(library_search))
    app.router.add_get("/v1/library/items", guard(library_items))
    app.router.add_post("/v1/library/items", guard(library_add))
    app.router.add_get("/v1/library/items/{item_id}", guard(library_item))
    app.router.add_post("/v1/library/items/{item_id}/status", guard(library_status))
    app.router.add_post("/v1/chats/{chat_id}/messages", guard(chat_send))
    app.router.add_post("/v1/chats", guard(chat_create))
    app.router.add_patch("/v1/chats/{chat_id}", guard(chat_update))
    app.router.add_delete("/v1/chats/{chat_id}", guard(chat_delete))
    app.router.add_post("/v1/chats/{chat_id}/notes", guard(chat_note))
    app.router.add_post("/v1/missions", guard(mission_create))
    app.router.add_post("/v1/projects", guard(project_create))
    app.router.add_post("/v1/missions/{mission_id}/{verb:pause|resume|cancel}", guard(mission_verb))
    app.router.add_post("/v1/missions/{mission_id}/prioritize", guard(prioritize))
    app.router.add_post("/v1/missions/{mission_id}/message", guard(message))
    app.router.add_post("/v1/missions/{mission_id}/retry-pr", guard(retry))
    app.router.add_post("/v1/gates/{gate_id}/decide", guard(decide))
