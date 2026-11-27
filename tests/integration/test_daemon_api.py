"""The daemon's /v1 command API (ADR 0021), end to end through a real HTTP
server and the real client: two clients acting at once must not corrupt a
mission, refusals carry the daemon's reason, and the bearer token guards it."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestServer

from rework.api.client import ApiError, DaemonClient
from rework.kernel.events.bus import InProcessEventBus
from rework.kernel.gates import GateManager
from rework.kernel.state.records import EventType, GateKind, Mission, MissionStatus
from rework.kernel.state.sqlite import open_sqlite
from rework.orchestrator.workspace import build_workspace_app


@pytest.fixture
async def api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("REWORK_TOKEN", "secret")
    store = await open_sqlite(str(tmp_path / "s.sqlite3"))
    server = TestServer(build_workspace_app(store, tmp_path / "work"))
    await server.start_server()
    url = str(server.make_url("")).rstrip("/")
    try:
        yield {"store": store, "url": url}
    finally:
        await server.close()
        await store.aclose()


def _client(api: dict[str, Any], token: str = "secret") -> DaemonClient:
    return DaemonClient(api["url"], token=token)


async def test_pause_resume_cancel_round_trip(api: dict[str, Any]) -> None:
    store = api["store"]
    await store.create_mission(Mission(id="m-1", objective="x", status=MissionStatus.RUNNING))
    async with _client(api) as client:
        assert await client.version() == 1
        assert (await client.mission_verb("m-1", "pause"))["status"] == "on_hold"
        assert (await client.mission_verb("m-1", "resume"))["status"] == "running"
        assert (await client.mission_verb("m-1", "cancel"))["status"] == "cancelled"
        with pytest.raises(ApiError) as refused:
            await client.mission_verb("m-1", "resume")
        assert refused.value.status == 409 and "it is cancelled" in str(refused.value)
        with pytest.raises(ApiError) as missing:
            await client.mission_verb("m-nope", "pause")
        assert missing.value.status == 404


async def test_two_clients_deciding_one_gate_decide_it_once(api: dict[str, Any]) -> None:
    """Two TUIs press approve and reject at the same moment: exactly one
    decision lands, exactly one GATE_DECIDED event, and both are told."""
    store = api["store"]
    await store.create_mission(Mission(id="m-g", objective="x", status=MissionStatus.RUNNING))
    gate = await GateManager(store, InProcessEventBus(store)).request(
        "m-g", kind=GateKind.SHIP, question="ship?", park_mission=True
    )
    async with _client(api) as a, _client(api) as b:
        first, second = await asyncio.gather(
            a.decide_gate(gate.id, True), b.decide_gate(gate.id, False)
        )
    assert sorted([first["decided"], second["decided"]]) == [False, True]
    assert first["gate"]["status"] == second["gate"]["status"]
    events = await store.events_since(0, mission_id="m-g")
    assert [e.type for e in events].count(EventType.GATE_DECIDED) == 1


async def test_message_and_prioritize(api: dict[str, Any]) -> None:
    store = api["store"]
    await store.create_mission(Mission(id="m-2", objective="x"))
    async with _client(api) as client:
        seq = await client.message("m-2", "please also update the README")
        assert seq is not None
        assert (await client.prioritize("m-2", "high"))["priority"] == "high"
        with pytest.raises(ApiError) as bad:
            await client.prioritize("m-2", "urgent")
        assert bad.value.status == 400
        with pytest.raises(ApiError):
            await client.message("m-2", "   ")
    events = await store.events_since(0, mission_id="m-2")
    assert events[-1].type == EventType.OWNER_MESSAGE


async def test_the_token_guards_every_route(api: dict[str, Any]) -> None:
    store = api["store"]
    await store.create_mission(Mission(id="m-3", objective="x", status=MissionStatus.RUNNING))
    async with _client(api, token="wrong") as client:
        with pytest.raises(ApiError) as denied:
            await client.mission_verb("m-3", "cancel")
        assert denied.value.status == 401
    row = await store.get_mission("m-3")
    assert row is not None and row.status == MissionStatus.RUNNING


async def test_the_cli_sends_its_verbs_to_the_daemon(
    api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With REWORK_DAEMON_URL set, `rework pause/cancel/resume` are executed by
    the daemon: the CLI's own REWORK_DB points at an EMPTY database, so a
    command that worked must have gone over the API."""
    from typer.testing import CliRunner

    from rework.cli import app

    store = api["store"]
    await store.create_mission(Mission(id="m-cli", objective="x", status=MissionStatus.RUNNING))
    monkeypatch.setenv("REWORK_DAEMON_URL", api["url"])
    monkeypatch.setenv("REWORK_DB", str(tmp_path / "empty-client-side.sqlite3"))
    runner = CliRunner()

    def run(*argv: str) -> Any:
        return await_in_thread(lambda: runner.invoke(app, list(argv)))

    paused = await run("pause", "m-cli")
    assert paused.exit_code == 0 and "on_hold" in paused.output
    cancelled = await run("cancel", "m-cli")
    assert cancelled.exit_code == 0 and "cancelled" in cancelled.output
    refused = await run("resume", "m-cli")
    assert refused.exit_code == 1 and "it is cancelled" in refused.output
    row = await store.get_mission("m-cli")
    assert row is not None and row.status == MissionStatus.CANCELLED


async def test_an_old_daemon_without_the_api_falls_back_in_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mixed versions during a rollout: a daemon without /v1 answers a plain
    404; the front end must act in-process instead of failing."""
    from aiohttp import web

    from rework.api import client as api_client

    old = web.Application()
    server = TestServer(old)
    await server.start_server()
    try:
        monkeypatch.setenv("REWORK_DAEMON_URL", str(server.make_url("")).rstrip("/"))
        with pytest.raises(api_client.NoApi):
            await api_client.call(lambda c: c.mission_verb("m-x", "pause"))
    finally:
        await server.close()


async def await_in_thread(fn: Any) -> Any:
    """CliRunner calls asyncio.run inside; keep it off this test's loop."""
    return await asyncio.to_thread(fn)


async def test_chat_turns_run_on_the_daemon_one_at_a_time(
    api: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR 0021 phase 2b: two clients sending to ONE chat at the same moment
    get two serialized turns on one engine, and the second turn's model call
    sees the first (no lost or interleaved history)."""
    import rework.kernel.llm.adapters as adapters
    from rework.kernel.llm.types import ChatResult as LlmResult
    from rework.kernel.llm.types import Usage
    from rework.kernel.state.records import Chat
    from rework.services import chat as chat_service

    seen: list[list[str]] = []
    in_flight = {"n": 0, "max": 0}

    class Slow:
        name = "slow"

        async def complete(self, request: Any) -> LlmResult:
            in_flight["n"] += 1
            in_flight["max"] = max(in_flight["max"], in_flight["n"])
            seen.append([str(m["content"]) for m in request.messages if m["role"] == "user"])
            await asyncio.sleep(0.2)
            in_flight["n"] -= 1
            return LlmResult(text="ok", usage=Usage(input_tokens=1, output_tokens=1))

    monkeypatch.setattr(adapters, "build_provider", lambda entry, extra=None: Slow())
    monkeypatch.setenv("REWORK_MODELS", '[{"id":"flash","provider":"fake","model":"f","tier":1}]')
    chat_service._engines.clear()
    store = api["store"]
    chat = await store.create_chat(Chat(title="shared"))
    async with _client(api) as a, _client(api) as b:
        await asyncio.gather(a.chat_send(chat.id, "from A"), b.chat_send(chat.id, "from B"))
        with pytest.raises(ApiError) as missing:
            await a.chat_send("chat-nope", "hello")
        assert missing.value.status == 404
    assert in_flight["max"] == 1, "two turns on one chat ran at the same time"
    assert len(seen) == 2 and len(seen[1]) == 2, seen
    users = [m.display for m in await store.chat_messages(chat.id) if m.role == "user"]
    assert sorted(users) == ["from A", "from B"]


async def test_the_event_stream_delivers_what_the_daemon_publishes(
    api: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR 0021 phase 3: /v1/events streams every event the daemon publishes, so
    a TUI refreshes the moment something happens instead of on its next poll."""
    from rework.api.client import stream_events

    monkeypatch.setenv("REWORK_DAEMON_URL", api["url"])
    store = api["store"]
    await store.create_mission(Mission(id="m-ev", objective="x", status=MissionStatus.RUNNING))
    got: list[dict[str, Any]] = []
    done = asyncio.Event()

    async def on_event(event: dict[str, Any]) -> None:
        got.append(event)
        done.set()

    follower = asyncio.create_task(stream_events(on_event, stop=done.is_set))
    await asyncio.sleep(0.3)  # let the stream connect
    async with _client(api) as client:
        # an owner message is written straight to the store, not the bus: the
        # stream must carry it anyway (it tails the events table)
        await client.message("m-ev", "ping")
    await asyncio.wait_for(done.wait(), 5)
    follower.cancel()
    assert any(e["type"] == "owner_message" and e["mission_id"] == "m-ev" for e in got), got


async def _seed_snapshot_world(store: Any) -> dict[str, Any]:
    """One of everything the heartbeat shows, plus the things it must hide."""
    import time as _time

    from rework.kernel.state.records import (
        ORCHESTRATOR_CHAT_PREFIX,
        Charge,
        Chat,
        ChatMessage,
        EventRecord,
        Project,
        Run,
        RunStatus,
    )

    project = await store.create_project(Project(name="toy"))
    await store.create_mission(
        Mission(
            id="m-run",
            objective="x" * 5000,
            status=MissionStatus.RUNNING,
            project_id=project.id,
            plan={"steps": [{"step": "do", "role": "echo"}]},
            result={"big": "y" * 1000},
        )
    )
    await store.create_mission(Mission(id="m-done", objective="old", status=MissionStatus.DONE))
    await store.create_run(
        Run(
            id="m-run:do:echo:1",
            mission_id="m-run",
            role="echo",
            step="do",
            status=RunStatus.RUNNING,
        )
    )
    await store.append_event(
        EventRecord(mission_id="m-run", run_id="m-run:do:echo:1", type=EventType.PROGRESS)
    )
    gate = await GateManager(store, InProcessEventBus(store)).request(
        "m-run", kind=GateKind.SHIP, question="ship?"
    )
    talk = await store.create_chat(Chat(title="a real conversation"))
    await store.append_chat_message(ChatMessage(chat_id=talk.id, role="user", body={"text": "hi"}))
    empty = await store.create_chat(Chat(title="empty"))
    orch = await store.create_chat(Chat(id=f"{ORCHESTRATOR_CHAT_PREFIX}m-run", title="machinery"))
    await store.append_chat_message(
        ChatMessage(chat_id=orch.id, role="assistant", body={"text": "internal"})
    )
    await store.charge(Charge(mission_id="m-run", model="fake", cost_usd=0.25))
    return {"gate": gate, "talk": talk, "empty": empty, "orch": orch, "now": _time.time()}


async def test_the_snapshot_is_the_whole_heartbeat_in_one_call(api: dict[str, Any]) -> None:
    """ADR 0021 phase 3: one GET gives a front end everything its heartbeat
    shows: light missions, open gates, projects, the LISTED chats (no empty
    chat, no orchestrator transcript), spend since the client's midnight, and
    the spinner's event stamps for the running mission and its run."""
    store = api["store"]
    seeded = await _seed_snapshot_world(store)
    async with _client(api) as client:
        snap = await client.snapshot(spend_since=seeded["now"] - 3600)
    by_id = {m.id: m for m in snap["missions"]}
    assert set(by_id) == {"m-run", "m-done"}
    running = by_id["m-run"]
    assert running.light and running.plan is None and running.result is None
    assert len(running.objective) < 5000, "the objective is clipped like list_missions_light"
    assert [g.id for g in snap["gates"]] == [seeded["gate"].id]
    assert [p.name for p in snap["projects"]] == ["toy"]
    assert [c.id for c in snap["chats"]] == [seeded["talk"].id]
    assert snap["chats"][0].last_message_at is not None
    assert snap["spend_today"] == pytest.approx(0.25)
    assert {"m-run", "m-run:do:echo:1", "*"} <= set(snap["last_event"])
    assert "m-done" not in snap["last_event"], "only running missions are stamped"
    assert snap["budget_raises"] == [] and snap["event_seq"] > 0


async def test_the_snapshot_cursor_carries_each_budget_raise_exactly_once(
    api: dict[str, Any],
) -> None:
    """The budget-raise toast rides the snapshot: the first call primes the
    cursor at "now" (yesterday's raises are not re-announced), a raise after
    it arrives in exactly one later snapshot, and the cursor moves past a
    burst of unrelated events too."""
    from rework.kernel.state.records import EventRecord

    store = api["store"]
    await store.create_mission(Mission(id="m-b", objective="x", status=MissionStatus.RUNNING))
    await store.append_event(EventRecord(mission_id="m-b", type=EventType.BUDGET_RAISED))
    async with _client(api) as client:
        first = await client.snapshot()
        assert first["budget_raises"] == [], "a fresh shell starts from now"
        await store.append_event(EventRecord(mission_id="m-b", type=EventType.INFO))
        await store.append_event(EventRecord(mission_id="m-b", type=EventType.BUDGET_RAISED))
        second = await client.snapshot(events_after=first["event_seq"])
        assert [e.type for e in second["budget_raises"]] == [EventType.BUDGET_RAISED]
        third = await client.snapshot(events_after=second["event_seq"])
        assert third["budget_raises"] == [] and third["event_seq"] == second["event_seq"]


async def test_the_snapshot_rejects_bad_numbers_and_wrong_tokens(api: dict[str, Any]) -> None:
    async with _client(api) as client:
        response = await client._http.get("/v1/snapshot", params={"events_after": "soon"})
        assert response.status_code == 400 and response.json()["code"] == "bad_request"
    async with _client(api, token="wrong") as client:
        with pytest.raises(ApiError) as denied:
            await client.snapshot()
        assert denied.value.status == 401


async def test_the_snapshot_equals_what_the_tui_reads_from_the_store(
    api: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The drift guard: a hub fed by the daemon and a hub reading the store
    directly must show the SAME snapshot (same rules, one module)."""
    from rework.tui.data import TuiData
    from rework.tui.hub import Hub

    store = api["store"]
    await _seed_snapshot_world(store)

    local = Hub(TuiData(store_factory=lambda: store))
    local_snap = await local.refresh()

    monkeypatch.setenv("REWORK_DAEMON_URL", api["url"])

    def no_database() -> Any:
        raise AssertionError("a daemon-backed hub must not open the database")

    remote = Hub(TuiData(store_factory=no_database))
    remote_snap = await remote.refresh()

    assert remote_snap.online and remote_snap.loaded, remote_snap.error
    assert [m.id for m in remote_snap.missions] == [m.id for m in local_snap.missions]
    assert [m.model_dump() for m in remote_snap.missions] == [
        m.model_dump() for m in local_snap.missions
    ]
    assert [g.id for g in remote_snap.gates] == [g.id for g in local_snap.gates]
    assert [p.id for p in remote_snap.projects] == [p.id for p in local_snap.projects]
    assert [c.model_dump() for c in remote_snap.chats] == [c.model_dump() for c in local_snap.chats]
    assert remote_snap.spend_today == pytest.approx(local_snap.spend_today)
    assert set(remote_snap.last_event) == set(local_snap.last_event)


async def test_an_unreachable_daemon_is_offline_not_a_database_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configured daemon that is down must show as OFFLINE with the last
    good data, never quietly go around it to the database (that would bring
    back the second-runtime TUI the API exists to remove)."""
    from rework.tui.data import TuiData
    from rework.tui.hub import Hub

    monkeypatch.setenv("REWORK_DAEMON_URL", "http://127.0.0.1:9")  # discard port

    def no_database() -> Any:
        raise AssertionError("must not fall back to the database")

    snap = await Hub(TuiData(store_factory=no_database)).refresh()
    assert not snap.online and snap.error


async def test_an_older_daemon_without_the_snapshot_route_falls_back_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mixed versions during a rollout: a daemon that answers aiohttp's plain
    404 for /v1/snapshot makes the hub read the store, and it is not asked
    again every poll."""
    from aiohttp import web

    from rework.tui.data import TuiData
    from rework.tui.hub import Hub

    hits: list[str] = []

    async def plain_404(request: web.Request) -> web.Response:
        hits.append(request.path)
        raise web.HTTPNotFound()

    old = web.Application()
    old.router.add_get("/v1/snapshot", plain_404)
    server = TestServer(old)
    await server.start_server()
    store = await open_sqlite(str(tmp_path / "local.sqlite3"))
    await store.create_mission(Mission(id="m-local", objective="x"))
    try:
        monkeypatch.setenv("REWORK_DAEMON_URL", str(server.make_url("")).rstrip("/"))
        hub = Hub(TuiData(store_factory=lambda: store))
        first = await hub.refresh()
        second = await hub.refresh()
        assert [m.id for m in first.missions] == ["m-local"]
        assert [m.id for m in second.missions] == ["m-local"]
        assert hits == ["/v1/snapshot"], "an old daemon is asked once, then remembered"
    finally:
        await server.close()
        await store.aclose()


async def test_the_snapshot_is_compressed_on_the_wire(api: dict[str, Any]) -> None:
    """An active TUI polls every second over the tailnet: the reply is compressed."""
    store = api["store"]
    for n in range(30):
        await store.create_mission(Mission(id=f"m-z{n}", objective="the same words " * 40))
    async with _client(api) as client:
        response = await client._http.get("/v1/snapshot")
    assert response.status_code == 200
    assert response.headers.get("content-encoding") in {"gzip", "deflate"}
    assert len(response.json()["missions"]) == 30


async def test_every_remote_read_equals_the_local_store(api: dict[str, Any]) -> None:
    """ADR 0021 phase 3c: each allowlisted StateStore read, called over
    /v1/read, returns exactly what the store returns locally: same types,
    same values. This is the contract the TUI's detail views rely on."""

    from rework.api.remote_store import READS, RemoteReadStore

    store = api["store"]
    seeded = await _seed_snapshot_world(store)
    calls: dict[str, tuple[tuple[Any, ...], dict[str, Any]]] = {
        "get_mission": (("m-run",), {}),
        "get_run": (("m-run:do:echo:1",), {}),
        "get_project": (((await store.list_projects())[0].id,), {}),
        "get_chat": ((seeded["talk"].id,), {}),
        "list_runs": (("m-run",), {}),
        "list_gates": (("m-run",), {}),
        "list_open_gates": ((), {}),
        "list_projects": ((), {}),
        "list_chats": ((), {"limit": 10}),
        "list_missions_light": ((), {"limit": 50}),
        "list_missions": ((), {"status": MissionStatus.DONE}),
        "events_since": ((0,), {"mission_id": "m-run", "newest_first": True}),
        "mission_spend": (("m-run",), {}),
        "run_charges": (("m-run:do:echo:1",), {}),
        "spend_by_model": ((), {}),
        "spend_history": ((), {"bucket": "total", "since": seeded["now"] - 3600}),
        "chat_messages": ((seeded["talk"].id,), {}),
        "chat_messages_max_id": ((seeded["talk"].id,), {}),
        "chat_messages_since": ((seeded["talk"].id, 0), {}),
        "chat_ids_with_conversation": (([seeded["talk"].id, seeded["empty"].id],), {}),
        "chat_ids_with_messages": (([seeded["talk"].id, seeded["empty"].id],), {}),
    }
    assert set(calls) == READS, "every served read is pinned here"
    remote = RemoteReadStore(_client(api))
    try:
        for method, (args, kwargs) in calls.items():
            local = await getattr(store, method)(*args, **kwargs)
            over_api = await getattr(remote, method)(*args, **kwargs)
            assert over_api == local, method
            if isinstance(local, list) and local:
                assert type(over_api[0]) is type(local[0]), method
    finally:
        await remote.aclose()


async def test_the_read_route_refuses_writes_and_bad_arguments(api: dict[str, Any]) -> None:
    """/v1/read serves READS only: a write method, an unknown method, or an
    argument the method does not take is a 400, and nothing is written."""
    from rework.api.remote_store import RemoteReadStore

    store = api["store"]
    async with _client(api) as client:
        for body in (
            {"method": "update_mission", "args": {"mission_id": "m-x"}},
            {"method": "create_mission", "args": {}},
            {"method": "__class__", "args": {}},
            {"method": "list_runs", "args": {"mission_id": "m", "drop": True}},
            {"method": "events_since", "args": {"seq": "not a number"}},
            {"method": "list_runs", "args": ["m"]},
        ):
            response = await client._http.post("/v1/read", json=body)
            assert response.status_code == 400, (body, response.text)
            assert response.json()["code"] == "bad_request"
    remote = RemoteReadStore(_client(api))
    try:
        with pytest.raises(AttributeError):
            remote.update_mission  # noqa: B018 - the attribute access is the test
    finally:
        await remote.aclose()
    assert await store.list_missions_light() == []


async def test_a_tui_against_a_daemon_reads_detail_views_without_a_database(
    api: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The point of phase 3c: with REWORK_DAEMON_URL set, the TUI's detail
    reads (runs, gates, events, spend, budget, report, transcript) all work
    while opening the database is an error."""
    from rework.tui.data import TuiData

    store = api["store"]
    seeded = await _seed_snapshot_world(store)
    monkeypatch.setenv("REWORK_DAEMON_URL", api["url"])

    def no_database() -> Any:
        raise AssertionError("a daemon-backed TUI must not open the database")

    data = TuiData(store_factory=no_database)
    try:
        assert [r.id for r in await data.mission_runs("m-run")] == ["m-run:do:echo:1"]
        assert [g.id for g in await data.gates("m-run")] == [seeded["gate"].id]
        assert (await data.get_mission("m-run")).plan is not None, "the FULL row"
        assert await data.mission_events("m-run")
        assert await data.mission_events_newest("m-run", limit=5)
        assert await data.mission_spend("m-run") == pytest.approx(0.25)
        assert (await data.mission_budget("m-run")).spent_usd == pytest.approx(0.25)
        assert (await data.spend_total())["usd"] == pytest.approx(0.25)
        assert await data.spend_history(bucket="day", since=seeded["now"] - 86400)
        assert "m-run" in await data.mission_report("m-run")
        assert [m.role for m in await data.chat_messages(seeded["talk"].id)] == ["user"]
        assert await data.chat_messages_max_id(seeded["talk"].id) > 0
        assert (await data.chat(seeded["talk"].id)).title == "a real conversation"
    finally:
        await data.aclose()


@pytest.fixture
async def api_with_library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    from rework.library.store import GitLibrary, open_library_store
    from rework.library.tools import LibraryHandle

    monkeypatch.setenv("REWORK_TOKEN", "secret")
    db = str(tmp_path / "s.sqlite3")
    store = await open_sqlite(db)
    index = await open_library_store(db)
    handle = LibraryHandle(index=index, git=GitLibrary(tmp_path / "lib"))
    server = TestServer(build_workspace_app(store, tmp_path / "work", library=handle))
    await server.start_server()
    url = str(server.make_url("")).rstrip("/")
    try:
        yield {"store": store, "url": url, "library": handle}
    finally:
        await server.close()
        await index.aclose()
        await store.aclose()


async def test_a_tui_against_a_daemon_writes_without_a_database(
    api_with_library: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR 0021 phase 3d: with REWORK_DAEMON_URL set, every owner write the TUI
    makes (chats, notes, missions, projects, owner messages, the Library) is
    executed by the daemon, while opening the database or the Library index
    from the TUI is an error. Each write is then checked on the daemon's store."""
    from rework.tui.data import TuiData

    api = api_with_library
    store = api["store"]
    monkeypatch.setenv("REWORK_DAEMON_URL", api["url"])

    def no_database() -> Any:
        raise AssertionError("a daemon-backed TUI must not open the database")

    data = TuiData(store_factory=no_database)

    async def no_library() -> Any:
        raise AssertionError("a daemon-backed TUI must not open the Library index")

    data.library = no_library  # type: ignore[method-assign]
    try:
        chat = await data.new_chat(title="plan a thing", model_id="fake")
        assert (await store.get_chat(chat.id)).title == "plan a thing"
        await data.rename_chat(chat.id, "renamed")
        await data.update_chat(chat.id, effort="high")
        row = await store.get_chat(chat.id)
        assert row.title == "renamed" and row.effort == "high"

        project = await data.create_project("toy", [])
        assert (await store.get_project(project.id)).settings["transport"] == "upload"

        mission = await data.create_mission(
            objective="say hi", plan="echo", chat_id=chat.id, title="Hi", budget_usd=1.5
        )
        stored = await store.get_mission(mission.id)
        assert stored.chat_id == chat.id and stored.title == "Hi"
        await data.attach_chat_to_mission(chat.id, mission.id)
        assert (await store.get_chat(chat.id)).mission_id == mission.id

        note = await data.append_chat_note(chat.id, f"launched {mission.id}")
        assert note.id in data.own_chat_ids, "the writer knows the note is its own"
        assert [m.role for m in await store.chat_messages(chat.id)] == ["note"]

        await data.send_owner_message(mission.id, "also say bye")
        events = await store.events_since(0, mission_id=mission.id)
        assert events[-1].type == EventType.OWNER_MESSAGE

        item, _note = await data.library_add(
            type_="skill", name="greet", description="say hi", body="Say hi.", tags=["x"]
        )
        assert (await api["library"].index.get_item(item.id)).name == "greet"
        assert [i.id for i in await data.library_search("")] == [item.id]
        assert await data.library_set_status(item.id, retire=True)
        assert (await data.library_item(item.id)).status.value == "retired"
        fetched, body = await data.library_body(item.id)
        assert fetched.id == item.id and body == "Say hi."
        assert await data.library_item("nope") is None

        await data.delete_chat(chat.id)
        assert await store.get_chat(chat.id) is None
    finally:
        await data.aclose()


async def test_write_routes_refuse_bad_input_with_the_daemons_reason(
    api_with_library: dict[str, Any],
) -> None:
    api = api_with_library
    async with _client(api) as client:
        for verb, path, body, status in (
            ("POST", "/v1/missions", {"objective": "  "}, 400),
            ("POST", "/v1/missions", {"objective": "x", "budget_usd": "lots"}, 400),
            ("POST", "/v1/missions", {"objective": "x", "budget_usd": float("-1")}, 400),
            ("POST", "/v1/missions", {"objective": "x", "plan": "nope"}, 400),
            ("POST", "/v1/projects", {"name": ""}, 400),
            ("POST", "/v1/projects", {"name": "p", "repos": "github.com/x"}, 400),
            ("PATCH", "/v1/chats/chat-missing", {"title": "t"}, 404),
            ("DELETE", "/v1/chats/chat-missing", None, 404),
            ("POST", "/v1/chats/chat-missing/notes", {"display": "x"}, 404),
            ("POST", "/v1/library/items", {"name": "n", "body": " "}, 400),
            ("POST", "/v1/library/items/nope/status", {"status": "retired"}, 404),
            ("POST", "/v1/library/items/nope/status", {"status": "sideways"}, 400),
        ):
            response = await client._http.request(verb, path, json=body)
            assert response.status_code == status, (verb, path, body, response.text)
        chat = await client.create_chat(title="t")
        response = await client._http.patch(f"/v1/chats/{chat.id}", json={"id": "evil"})
        assert response.status_code == 400, "only the patchable fields"
    assert await api["store"].list_missions_light() == []


async def test_the_library_routes_say_when_the_daemon_has_no_library(
    api: dict[str, Any],
) -> None:
    async with _client(api) as client:
        response = await client._http.get("/v1/library/items")
        assert response.status_code == 409 and response.json()["code"] == "unavailable"
