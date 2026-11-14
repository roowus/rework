"""URL + IP guard for outbound fetches, and stdlib HTML->text extraction.

A role runs with the daemon's own network position (the executor jail is
filesystem-only), so a fetch tool must carry its own egress checks: an agent
reading the public web must not be able to reach loopback, the cloud metadata
service, the tailnet or any other private service. The checks here are plain
functions so they can be unit-tested without a socket.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from collections.abc import Awaitable, Callable
from html.parser import HTMLParser
from http import HTTPStatus
from typing import Any
from urllib.parse import urlsplit

ALLOWED_SCHEMES = ("http", "https")
DEFAULT_PORTS = {"http": 80, "https": 443}

# hostnames that never belong to the public web (checked before any DNS)
BLOCKED_HOST_SUFFIXES = (
    ".local",
    ".internal",
    ".localhost",
    ".localdomain",
    ".home.arpa",
    ".lan",
    ".private",
    ".intranet",
)
BLOCKED_HOSTS = frozenset(
    {
        "localhost",
        "metadata",
        "metadata.google.internal",
        "instance-data",
        "ip6-localhost",
        "ip6-loopback",
    }
)


class BlockedURL(ValueError):
    """The URL is not fetchable: bad scheme, no host, or a private destination."""


def _blocked_ip_reason(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """Why this resolved address is off limits, or None when it is public."""
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:  # ::ffff:127.0.0.1 reaches loopback
        return _blocked_ip_reason(mapped)
    if addr.is_loopback:
        return "loopback"
    if addr.is_link_local:
        return "link-local"
    if addr.is_multicast:
        return "multicast"
    if addr.is_unspecified:
        return "unspecified (0.0.0.0/::)"
    if addr.is_reserved and not addr.is_global:
        return "reserved"
    if addr.is_private:
        return "private"
    if isinstance(addr, ipaddress.IPv4Address) and addr in ipaddress.ip_network("100.64.0.0/10"):
        return "CGNAT/tailnet"
    if isinstance(addr, ipaddress.IPv6Address) and addr in ipaddress.ip_network("fc00::/7"):
        return "unique-local"
    return None


def check_host_text(host: str) -> str | None:
    """Blocked-host reason from the hostname alone (no DNS)."""
    name = (host or "").strip().strip("[]").rstrip(".").lower()
    if not name:
        return "missing host"
    if name in BLOCKED_HOSTS:
        return f"host {name!r} is internal"
    if any(name.endswith(suffix) for suffix in BLOCKED_HOST_SUFFIXES):
        return f"host {name!r} is internal"
    try:
        addr = ipaddress.ip_address(name)
    except ValueError:
        return None  # a name: resolved IPs are checked separately
    reason = _blocked_ip_reason(addr)
    return f"{name} is {reason}" if reason else None


def check_url(url: str, *, allow_private: bool = False) -> tuple[str, str, int]:
    """Validate scheme/host and return (scheme, host, port).

    Raises BlockedURL for anything that is not a plain public http(s) URL.
    Only textual checks here: resolution happens in `ensure_public`, which is
    what defends against DNS rebinding. `allow_private` is for the SEARCH
    backend, whose address is deployment config (a SearXNG on 127.0.0.1), not
    model input.
    """
    raw = (url or "").strip()
    if not raw:
        raise BlockedURL("empty url")
    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise BlockedURL(
            f"scheme {scheme or '(none)'!r} is not allowed; only http and https are fetchable"
        )
    host = parts.hostname or ""
    if not host:
        raise BlockedURL(f"url has no host: {raw!r}")
    if not allow_private:
        reason = check_host_text(host)
        if reason:
            raise BlockedURL(f"blocked: {reason}")
    try:
        port = parts.port or DEFAULT_PORTS[scheme]
    except ValueError as exc:
        raise BlockedURL(f"bad port in {raw!r}: {exc}") from exc
    return scheme, host, port


async def resolve_host(host: str, port: int) -> list[str]:
    """Every address the host resolves to (a name may point at several).

    getaddrinfo in a thread: DNS can block, and this runs inside an event loop.
    """
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        pass
    else:
        return [host.strip("[]")]
    infos = await asyncio.to_thread(
        socket.getaddrinfo, host, port, socket.AF_UNSPEC, socket.SOCK_STREAM
    )
    return sorted({str(info[4][0]) for info in infos})


async def ensure_public(
    url: str,
    *,
    allow_private: bool = False,
    resolver: Callable[[str, int], Awaitable[list[str]]] | None = None,
) -> None:
    """check_url + resolve + check every resolved IP (defeats DNS rebinding).

    `resolver` is injectable so tests never touch real DNS.
    """
    _scheme, host, port = check_url(url, allow_private=allow_private)
    if allow_private:
        return
    try:
        ips = await (resolver or resolve_host)(host, port)
    except OSError as exc:
        raise BlockedURL(f"cannot resolve {host!r}: {exc}") from exc
    if not ips:
        raise BlockedURL(f"cannot resolve {host!r}")
    for raw_ip in ips:
        try:
            addr = ipaddress.ip_address(raw_ip)
        except ValueError:
            raise BlockedURL(f"unparseable address for {host!r}: {raw_ip}") from None
        reason = _blocked_ip_reason(addr)
        if reason:
            raise BlockedURL(f"blocked: {host!r} resolves to {raw_ip} ({reason})")


# -- HTML -> readable text ------------------------------------------------------

_SKIP_TAGS = frozenset(
    {"script", "style", "head", "noscript", "template", "svg", "iframe", "object"}
)
_BLOCK_TAGS = frozenset(
    {
        "p",
        "div",
        "section",
        "article",
        "main",
        "header",
        "footer",
        "aside",
        "nav",
        "br",
        "hr",
        "li",
        "ul",
        "ol",
        "tr",
        "td",
        "th",
        "table",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "blockquote",
        "pre",
        "figure",
        "figcaption",
        "form",
    }
)
_WS = re.compile(r"[ \t\r\f\v]+")
_MANY_NEWLINES = re.compile(r"\n{3,}")


class _TextExtractor(HTMLParser):
    """Collect visible text; block tags become paragraph breaks.

    convert_charrefs (the default) decodes entities for us, so `&amp;` and
    `&#8212;` arrive as text. Anchors keep their TEXT (that is what a reader
    needs) and drop the href.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


def collapse_whitespace(text: str) -> str:
    lines = [_WS.sub(" ", line).strip() for line in text.splitlines()]
    joined = "\n".join(line for line in lines)
    return _MANY_NEWLINES.sub("\n\n", joined).strip()


def html_to_text(html: str) -> str:
    """Readable text from HTML, without a third-party parser."""
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # malformed markup: keep what was collected
        pass
    return collapse_whitespace(parser.text())


def status_text(code: int) -> str:
    try:
        return f"{code} {HTTPStatus(code).phrase}"
    except ValueError:
        return str(code)
