"""Role integration: web_search + fetch_url as ordinary role tools.

Registered compositionally, exactly like the library tools: an executor
wrapper that answers its own tool names and appends its own specs. Exposure is
the WRAPPER's job and the only mechanism: there is no declarative list to
register a tool in (ADR 0017 removed the one that existed unread).
"""

from __future__ import annotations

from typing import Any

import httpx

from rework.kernel.llm.types import ToolSpec
from rework.web.fetch import fetch_url_text
from rework.web.search import search_text

WEB_SPECS: list[ToolSpec] = [
    ToolSpec(
        name="web_search",
        description=(
            "Search the public web (SearXNG). Returns numbered results with title,"
            " url and snippet; cite the urls you use. Args: {query, results?}"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for."},
                "results": {
                    "type": "integer",
                    "description": "How many results to show (default 6, max 10).",
                },
            },
            "required": ["query"],
        },
    ),
    ToolSpec(
        name="fetch_url",
        description=(
            "Fetch one public http(s) page and return it as readable text. Private,"
            " loopback and internal addresses are refused. Args: {url}"
        ),
        input_schema={
            "type": "object",
            "properties": {"url": {"type": "string", "description": "An http(s) URL."}},
            "required": ["url"],
        },
    ),
]


class WebAwareExecutor:
    """Wraps any executor; answers web_search / fetch_url, delegates the rest.

    Disjoint tool names, so ordering against the other wrappers does not matter.
    """

    def __init__(
        self,
        inner: Any,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        search_url: str | None = None,
    ) -> None:
        self._inner = inner
        self._transport = transport
        self._search_url = search_url

    async def execute(self, name: str, args: dict[str, Any]) -> str:
        if name == "web_search":
            raw = args.get("results")
            try:
                wanted = int(raw) if raw not in (None, "") else 6
            except (TypeError, ValueError):
                wanted = 6
            return await search_text(
                str(args.get("query") or ""),
                transport=self._transport,
                base_url=self._search_url,
                results=wanted,
            )
        if name == "fetch_url":
            return await fetch_url_text(str(args.get("url") or ""), transport=self._transport)
        return await self._inner.execute(name, args)

    def specs(self, allowed: set[str] | None = None) -> list[ToolSpec]:
        base = self._inner.specs(allowed)
        if allowed is not None and not any(s.name in allowed for s in WEB_SPECS):
            return base
        return [*base, *WEB_SPECS]

    def __getattr__(self, item: str) -> Any:
        return getattr(self._inner, item)


def web_executor(executor: Any) -> Any:
    """Wrap a role's executor with the web tools. Roles that should not reach
    the web (the auditor) simply do not call this."""
    return WebAwareExecutor(executor)
