"""web_search: a SearXNG JSON query, shaped for a model to read.

SearXNG is self-hosted and normally on loopback, so this backend IS allowed to
reach a private address (the address is deployment config from
REWORK_SEARCH_URL, never model input). fetch_url is the one that must not.
"""

from __future__ import annotations

import os
from urllib.parse import parse_qs, urlsplit

import httpx

from rework.web.guard import BlockedURL, check_url

DEFAULT_SEARCH_URL = "http://127.0.0.1:8888"
DEFAULT_TIMEOUT_S = 15.0
DEFAULT_RESULTS = 6
MAX_RESULTS = 10
MAX_SNIPPET_CHARS = 320
MAX_OUTPUT_CHARS = 4000


def search_base_url() -> str:
    return (os.environ.get("REWORK_SEARCH_URL") or DEFAULT_SEARCH_URL).strip()


def search_endpoint(base: str | None = None) -> str:
    """The JSON endpoint for a base URL; a full /search URL is accepted too."""
    base = (base or search_base_url()).rstrip("/")
    return f"{base}/search"


def shape_results(payload: object, query: str, limit: int = DEFAULT_RESULTS) -> str:
    """SearXNG JSON -> compact `n. title / url / snippet` text (capped)."""
    results: list[object] = []
    if isinstance(payload, dict):
        raw = payload.get("results")
        if isinstance(raw, list):
            results = raw
    if not results:
        return f"web_search({query!r}): no results"
    lines = [f"web_search({query!r}) -> {min(len(results), limit)} of {len(results)} results"]
    shown = 0
    for item in results[:limit]:
        if not isinstance(item, dict):
            continue
        title = " ".join(str(item.get("title") or "").split()) or "(untitled)"
        url = str(item.get("url") or "").strip()
        snippet = " ".join(str(item.get("content") or item.get("snippet") or "").split())
        if len(snippet) > MAX_SNIPPET_CHARS:
            snippet = snippet[:MAX_SNIPPET_CHARS].rstrip() + "..."
        shown += 1
        entry = f"{shown}. {title}\n   {url}"
        if snippet:
            entry += f"\n   {snippet}"
        if len("\n".join([*lines, entry])) > MAX_OUTPUT_CHARS:
            lines.append("(output cap reached; fewer results shown)")
            break
        lines.append(entry)
    if shown == 0:
        return f"web_search({query!r}): no results"
    return "\n".join(lines)


async def search_text(
    query: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    base_url: str | None = None,
    results: int = DEFAULT_RESULTS,
    timeout_s: float | None = None,
) -> str:
    """Run one SearXNG query and return readable results (never raises)."""
    cleaned = " ".join((query or "").split())
    if not cleaned:
        return "error: web_search needs a query"
    limit = max(1, min(results, MAX_RESULTS))
    base = (base_url or search_base_url()).rstrip("/")
    endpoint = f"{base}/search"
    timeout = timeout_s or float(os.environ.get("REWORK_SEARCH_TIMEOUT_S", DEFAULT_TIMEOUT_S))
    try:
        # allow_private: a self-hosted SearXNG on 127.0.0.1 is the normal case
        check_url(endpoint, allow_private=True)
        if urlsplit(endpoint).scheme not in ("http", "https"):
            raise BlockedURL("search backend must be http(s)")
    except BlockedURL as exc:
        return f"error: REWORK_SEARCH_URL is unusable ({exc})"
    params = {"q": cleaned, "format": "json"}
    try:
        async with httpx.AsyncClient(
            transport=transport, timeout=timeout, follow_redirects=False, trust_env=False
        ) as client:
            response = await client.get(endpoint, params=params, headers=_HEADERS)
    except httpx.TimeoutException:
        return f"error: web_search timed out after {timeout:.0f}s (SEARXNG unreachable at {base}?)"
    except httpx.HTTPError as exc:
        return (
            f"error: web_search backend unreachable at {base}"
            f" ({exc.__class__.__name__}: {exc}); set REWORK_SEARCH_URL"
        )
    if response.status_code != 200:
        return (
            f"error: web_search backend {base} returned HTTP {response.status_code};"
            " is it a SearXNG instance with the JSON format enabled?"
        )
    try:
        payload = response.json()
    except ValueError:
        return (
            f"error: web_search backend {base} did not return JSON"
            " (SearXNG needs `format: json` enabled in settings.yml)"
        )
    if not isinstance(payload, dict) or "results" not in payload:
        return f"error: web_search backend {base} returned an unexpected shape (no 'results')"
    return shape_results(payload, cleaned, limit)


_HEADERS = {"user-agent": "rework-search/0.1", "accept": "application/json"}


def query_of(url: str) -> str:
    """The q= of a search URL (used by tests to prove the request was formed)."""
    return (parse_qs(urlsplit(url).query).get("q") or [""])[0]
