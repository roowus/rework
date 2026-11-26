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
