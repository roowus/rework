"""Scripted provider for tests and demos. No network, no cost, deterministic.

Feed it canned ChatResults (tool calls included); it hands them out in order and
records every request for assertions. Mind the house lesson: a fake built from the
same belief as the code under test proves nothing about model behavior; use it for
protocol plumbing, then confirm with a real smoke run (pytest -m smoke).
"""

from __future__ import annotations

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
