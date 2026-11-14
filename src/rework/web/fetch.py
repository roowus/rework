"""fetch_url: one guarded HTTP(S) GET, returned as readable text.

Design notes:
- Redirects are NOT followed automatically: each hop is re-validated, so a
  public URL cannot bounce the agent into the tailnet or the metadata service.
- The host is resolved and every resolved address is checked, which is what
  defends against DNS rebinding (a name that resolves to 127.0.0.1).
- httpx is constructed with trust_env=False: a proxy from the environment would
  resolve the destination itself and put the guard out of the loop.
- Errors are RETURNED as `error: ...` strings. The tool loop classifies failure
  by string heuristic, and a role must not see a crash.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from urllib.parse import urljoin

import httpx

from rework.web.guard import BlockedURL, check_url, ensure_public, html_to_text, status_text

USER_AGENT = "rework-fetch/0.1 (+https://github.com/roowus/rework)"
DEFAULT_TIMEOUT_S = 15.0
MAX_REDIRECTS = 5
MAX_BODY_BYTES = 2_000_000  # read cap: 2 MB, then stop downloading
MAX_OUTPUT_CHARS = 20_000  # what the model actually receives
REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
HTML_TYPES = ("text/html", "application/xhtml+xml")
TEXT_TYPES = (
    "text/",
    "application/json",
    "application/xml",
    "application/x-yaml",
    "application/javascript",
)

Resolver = Callable[[str, int], Awaitable[list[str]]]


def _charset_of(content_type: str) -> str:
    for part in content_type.split(";")[1:]:
        name, _, value = part.strip().partition("=")
        if name.lower() == "charset" and value:
            return value.strip("\"'")
    return "utf-8"


def _truncated(text: str, limit: int, what: str) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n({len(text) - limit} more chars of {what} truncated)"


def _decode(body: bytes, content_type: str) -> str:
    try:
        return body.decode(_charset_of(content_type), errors="replace")
    except LookupError:  # unknown charset name
        return body.decode("utf-8", errors="replace")


def render_body(body: bytes, content_type: str, url: str) -> str:
    """Readable text for a 2xx body, or a clear note when we cannot read it."""
    kind = (content_type or "").split(";")[0].strip().lower()
    if not kind:
        # no content-type: sniff, since plenty of plain servers omit it
        kind = "text/html" if body.lstrip()[:1] == b"<" else "text/plain"
    if kind in HTML_TYPES or kind.endswith("+html"):
        return html_to_text(_decode(body, content_type))
    if kind.startswith(TEXT_TYPES):
        return _decode(body, content_type).strip()
    return (
        f"note: {url} returned unsupported content type {kind!r}"
        f" ({len(body)} bytes); only HTML, text, JSON and XML are readable"
    )


async def _read_capped(response: httpx.Response, max_bytes: int) -> tuple[bytes, bool]:
    """Stream the body, stopping at max_bytes. (body, truncated?)"""
    chunks: list[bytes] = []
    total = 0
    truncated = False
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > max_bytes:
            chunks.append(chunk[: len(chunk) - (total - max_bytes)])
            truncated = True
            break
        chunks.append(chunk)
    return b"".join(chunks), truncated


async def fetch_url_text(
    url: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    resolver: Resolver | None = None,
    timeout_s: float | None = None,
    max_bytes: int = MAX_BODY_BYTES,
    max_chars: int = MAX_OUTPUT_CHARS,
) -> str:
    """Fetch one URL and return readable text (never raises)."""
    timeout = timeout_s or float(os.environ.get("REWORK_FETCH_TIMEOUT_S", DEFAULT_TIMEOUT_S))
    try:
        check_url(url)
    except BlockedURL as exc:
        return f"error: {exc}"

    target = url.strip()
    headers = {"user-agent": USER_AGENT, "accept": "text/html,text/plain;q=0.9,*/*;q=0.5"}
    # trust_env=False: HTTP(S)_PROXY would resolve the host for us and hand the
    # request to a destination the guard never checked
    async with httpx.AsyncClient(
        transport=transport, timeout=timeout, follow_redirects=False, trust_env=False
    ) as client:
        for _hop in range(MAX_REDIRECTS + 1):
            try:
                await ensure_public(target, resolver=resolver)
            except BlockedURL as exc:
                return f"error: {exc}"
            try:
                async with client.stream("GET", target, headers=headers) as response:
                    if response.status_code in REDIRECT_CODES:
                        location = response.headers.get("location", "")
                        if not location:
                            return (
                                f"error: {target} answered {status_text(response.status_code)}"
                                " with no location header"
                            )
                        target = urljoin(target, location)
                        continue
                    if not 200 <= response.status_code < 300:
                        return f"error: {target} returned HTTP {status_text(response.status_code)}"
                    content_type = response.headers.get("content-type", "")
                    body, cut = await _read_capped(response, max_bytes)
            except httpx.TimeoutException:
                return f"error: timed out after {timeout:.0f}s fetching {target}"
            except httpx.HTTPError as exc:
                return f"error: could not fetch {target}: {exc.__class__.__name__}: {exc}"
            text = render_body(body, content_type, target)
            if cut:
                text += f"\n\n(stop reading {target} at the {max_bytes} byte limit)"
            if len(text) > max_chars:
                text = _truncated(text, max_chars, "the page")
            return f"{target}\n\n{text}" if text.strip() else f"{target}\n\n(page has no text)"
        return f"error: too many redirects (>{MAX_REDIRECTS}) starting from {url.strip()}"
