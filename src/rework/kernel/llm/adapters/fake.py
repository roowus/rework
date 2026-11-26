"""Scripted provider for tests and demos. No network, no cost, deterministic.

Feed it canned ChatResults (tool calls included); it hands them out in order and
records every request for assertions. Mind the house lesson: a fake built from the
same belief as the code under test proves nothing about model behavior; use it for
protocol plumbing, then confirm with a real smoke run (pytest -m smoke).
"""

from __future__ import annotations

from typing import ClassVar

from rework.kernel.llm.types import ChatRequest, ChatResult


class FakeProvider:
    name = "fake"

    def __init__(self, responses: list[ChatResult] | None = None) -> None:
        self._responses = list(responses or [])
        self.requests: list[ChatRequest] = []

    def push(self, response: ChatResult) -> None:
        self._responses.append(response)

    async def complete(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        if not self._responses:
            raise AssertionError("FakeProvider ran out of scripted responses")
        return self._responses.pop(0)


def scripted_from_env() -> FakeProvider:
    """A FakeProvider whose turns come from REWORK_FAKE_SCRIPT (a JSON file), for
    driving a REAL daemon process in end-to-end runs without spending money.

    The file is {"<route>": [<turn>, ...]}: a turn is {"text": str,
    "tool_calls": [{"name": str, "arguments": {}}]}. A request is routed to the
    first key found in its system prompt ("MISSION ORCHESTRATOR", a role's GOAL
    line, ...); turns are consumed in order per route, shared across every
    provider built in the process. Unset: today's empty FakeProvider."""
    import os

    path = os.environ.get("REWORK_FAKE_SCRIPT")
    if not path:
        return FakeProvider()
    return _RoutedScript.load(path)


class _RoutedScript(FakeProvider):
    # one script per file, shared by every provider the process builds
    _shared: ClassVar[dict[str, dict[str, list[ChatResult]]]] = {}

    def __init__(self, routes: dict[str, list[ChatResult]]) -> None:
        super().__init__()
        self._routes = routes

    @classmethod
    def load(cls, path: str) -> FakeProvider:
        import json

        from rework.kernel.llm.types import ToolCall, Usage

        if path not in cls._shared:
            raw = json.loads(open(path, encoding="utf-8").read())
            routes: dict[str, list[ChatResult]] = {}
            for key, turns in raw.items():
                routes[key] = [
                    ChatResult(
                        text=str(t.get("text") or ""),
                        tool_calls=[
                            ToolCall(
                                id=f"{key[:8]}-{i}-{j}",
                                name=str(c["name"]),
                                arguments=dict(c.get("arguments") or {}),
                            )
                            for j, c in enumerate(t.get("tool_calls") or [])
                        ],
                        usage=Usage(input_tokens=100, output_tokens=20),
                        stop_reason="tool_calls" if t.get("tool_calls") else "end_turn",
                    )
                    for i, t in enumerate(turns)
                ]
            cls._shared[path] = routes
        return cls(cls._shared[path])

    async def complete(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        system = request.system or ""
        for key, queue in self._routes.items():
            if key in system:
                if not queue:
                    raise AssertionError(f"REWORK_FAKE_SCRIPT route {key!r} is exhausted")
                return queue.pop(0)
        raise AssertionError("REWORK_FAKE_SCRIPT: no route matches this system prompt")
