"""The Plane bridge: poll for ready items, one mission each, comment back once.

No server: a fake transport answers the two calls the client makes. What is under
test is the contract that makes a poller safe to run on a loop: idempotence,
bounded calls, and a failure that is a log line rather than a dead queue.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from rework.kernel.state.records import Mission
from rework.kernel.state.sqlite import open_sqlite
from rework.sources.plane import PlaneClient, PlaneError, mission_id_for, objective_for
from rework.sources.poller import (
    enabled,
    plane_config,
    poll_once,
    write_back,
    write_back_text,
)


class _Response:
    def __init__(self, data: Any, status_code: int = 200) -> None:
        self._data = data
        self.status_code = status_code
        self.text = str(data)

    def json(self) -> Any:
        return self._data


class _FakeHTTP:
    def __init__(self, items: list[dict[str, Any]] | None = None) -> None:
        self.items = items or []
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.posts: list[dict[str, Any]] = []
        self.fail_list = False

    async def request(self, method: str, url: str, **kwargs: Any) -> _Response:
        self.calls.append((method, url, kwargs))
        if method == "GET":
            if self.fail_list:
                return _Response({"error": "down"}, status_code=503)
            return _Response({"results": self.items})
        self.posts.append(kwargs.get("json") or {})
        return _Response({"id": "c1"}, status_code=201)

    async def aclose(self) -> None:
        return None


def _item(item_id: str, name: str, labels: list[Any] | None = None) -> dict[str, Any]:
    return {
        "id": item_id,
        "name": name,
        "description": "details here",
        "sequence_id": 7,
        "labels": labels or [],
        "updated_at": "2026-11-01T00:00:00Z",
    }


def _client(http: _FakeHTTP) -> PlaneClient:
    return PlaneClient(
        "https://plane.test", "tok", "ws", "proj", http=http, requests_per_minute=600
    )


async def test_ready_items_create_one_mission_each() -> None:
    http = _FakeHTTP([_item("i1", "fix login"), _item("i2", "add export")])
    store = await open_sqlite(":memory:")
    try:
        created = await poll_once(store, _client(http))
        assert len(created) == 2
        mission = await store.get_mission(mission_id_for("i1"))
        assert mission is not None
        assert "fix login" in mission.objective
        assert mission.title == "fix login"
    finally:
        await store.aclose()


async def test_polling_twice_creates_nothing_new() -> None:
    http = _FakeHTTP([_item("i1", "fix login")])
    store = await open_sqlite(":memory:")
    try:
        first = await poll_once(store, _client(http))
        second = await poll_once(store, _client(http))
        assert len(first) == 1
        assert second == [], "a poller running on a loop must be idempotent"
    finally:
        await store.aclose()


async def test_the_mission_id_is_derived_from_the_item() -> None:
    assert mission_id_for("abc") == mission_id_for("abc")
    assert mission_id_for("abc") != mission_id_for("abd")
    assert mission_id_for("abc").startswith("m-plane-")


async def test_a_label_filter_matches_id_or_name() -> None:
    http = _FakeHTTP(
        [_item("i1", "a", ["lbl-1"]), _item("i2", "b", [{"name": "rework"}]), _item("i3", "c")]
    )
    client = _client(http)

    by_id = await client.list_ready(state=None, label="lbl-1")
    by_name = await client.list_ready(state=None, label="rework")

    assert [i["id"] for i in by_id] == ["i1"]
    assert [i["id"] for i in by_name] == ["i2"]


async def test_one_bad_item_does_not_stop_the_queue() -> None:
    http = _FakeHTTP([{"id": ""}, _item("i2", "good")])
    store = await open_sqlite(":memory:")
    try:
        created = await poll_once(store, _client(http))
        assert len(created) == 1, "an unusable item is skipped, the rest proceed"
    finally:
        await store.aclose()


async def test_a_listing_failure_is_raised_for_the_caller_to_retry() -> None:
    http = _FakeHTTP([_item("i1", "x")])
    http.fail_list = True
    store = await open_sqlite(":memory:")
    try:
        with pytest.raises(PlaneError):
            await poll_once(store, _client(http))
    finally:
        await store.aclose()


async def test_write_back_comments_once_with_the_pr_url() -> None:
    http = _FakeHTTP()
    store = await open_sqlite(":memory:")
    try:
        await store.create_mission(
            Mission(id="m-p", objective="x", result={"source": {"item_id": "i1"}})
        )
        await store.update_mission(
            "m-p",
            result={
                "source": {"item_id": "i1"},
                "outcome": "done",
                "summary": "made add() add",
                "pr_url": "https://github.com/x/y/pull/9",
                "branch": "rework/m-p",
            },
        )
        ok = await write_back(_client(http), store, "m-p")
        assert ok is True
        assert len(http.posts) == 1
        body = http.posts[0]["comment_html"]
        assert "https://github.com/x/y/pull/9" in body
        assert "made add() add" in body
    finally:
        await store.aclose()


async def test_write_back_failure_never_raises() -> None:
    class _Broken(_FakeHTTP):
        async def request(self, method, url, **kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("tracker down")

    store = await open_sqlite(":memory:")
    try:
        await store.create_mission(
            Mission(id="m-p", objective="x", result={"source": {"item_id": "i1"}})
        )
        assert await write_back(_client(_Broken()), store, "m-p") is False
    finally:
        await store.aclose()


def test_the_poller_is_disabled_without_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REWORK_PLANE_URL", "https://plane.test")
    monkeypatch.delenv("REWORK_PLANE_TOKEN", raising=False)
    assert enabled(plane_config()) is False


def test_the_poller_is_enabled_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in {
        "REWORK_PLANE_URL": "https://plane.test",
        "REWORK_PLANE_TOKEN": "tok",
        "REWORK_PLANE_WORKSPACE": "ws",
        "REWORK_PLANE_PROJECT": "proj",
    }.items():
        monkeypatch.setenv(key, value)
    assert enabled(plane_config()) is True


async def test_calls_are_rate_limited(monkeypatch: pytest.MonkeyPatch) -> None:
    """A burst of calls must be spaced out, not fired at the API."""
    import time

    http = _FakeHTTP([])
    client = PlaneClient(
        "https://plane.test", "tok", "ws", "proj", http=http, requests_per_minute=600
    )
    # 600/min = 0.1s apart; two calls should take at least that
    started = time.monotonic()
    await client.list_ready(state=None, label=None)
    await client.list_ready(state=None, label=None)
    elapsed = time.monotonic() - started
    assert elapsed >= 0.09, f"calls were not spaced ({elapsed:.3f}s)"


def test_objective_carries_the_sequence_and_body() -> None:
    text = objective_for({"id": "i", "name": "fix it", "sequence_id": 12, "description": "body"})
    assert text.startswith("[plane #12] fix it")
    assert "body" in text


def test_write_back_text_shapes_the_comment() -> None:
    mission = Mission(
        id="m-p",
        objective="x",
        result={"outcome": "done", "summary": "did it", "pr_url": "https://p/1"},
    )
    text = write_back_text(mission)
    assert "m-p" in text and "did it" in text and "https://p/1" in text


async def test_a_mission_survives_synthesis_with_its_source_pointer() -> None:
    """The write-back needs the pointer AFTER a mission finishes, and
    `_synthesize` rebuilds `result` from artifacts: the pointer must be carried,
    like the delivery keys are."""
    from rework.kernel.events.bus import InProcessEventBus
    from rework.kernel.llm.catalog import ModelCatalog
    from rework.orchestrator.engine import MissionRunner
    from rework.roles.registry import autodiscover

    store = await open_sqlite(":memory:")
    try:
        await store.create_mission(
            Mission(
                id="m-p",
                objective="x",
                result={"source": {"kind": "plane", "item_id": "i1"}},
            )
        )
        runner = MissionRunner(
            "m-p",
            store,
            InProcessEventBus(store),
            autodiscover(),
            ModelCatalog([]),
            None,  # type: ignore[arg-type]
            Path("."),
        )
        await runner._synthesize("m-p", True, outcome="done", summary="did the thing")
        mission = await store.get_mission("m-p")
        assert mission is not None
        assert (mission.result or {}).get("source") == {"kind": "plane", "item_id": "i1"}
    finally:
        await store.aclose()
