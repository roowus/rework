"""The web role tools: `web_search` (SearXNG) and `fetch_url`.

No test here touches the network or DNS: every request goes through an injected
`httpx.MockTransport` and every name resolves through an injected `resolver`, so
a broken assertion fails loudly instead of quietly reaching 127.0.0.1:8888 or
the real DNS.
"""

from __future__ import annotations

import ast
from pathlib import Path

import httpx
import pytest

from rework.web.fetch import fetch_url_text
from rework.web.guard import (
    BlockedURL,
    check_url,
    ensure_public,
    html_to_text,
)
from rework.web.search import search_text, shape_results
from rework.web.tools import WEB_SPECS, WebAwareExecutor, web_executor

PUBLIC_IP = "93.184.216.34"  # what example.com "resolves" to in these tests


async def public_resolver(host: str, port: int) -> list[str]:
    return [PUBLIC_IP]


def _http_error(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


def _timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("too slow", request=request)


# -- html_to_text ---------------------------------------------------------------


def test_html_to_text_strips_script_style_and_decodes_entities() -> None:
    html = (
        "<html><head><title>ignored</title><style>body{color:red}</style></head>"
        "<body><h1>Caf&eacute; &amp; bar</h1><script>var x = '<p>no</p>';</script>"
        "<p>one   two</p><p>three&#8212;four</p></body></html>"
    )
    text = html_to_text(html)
    assert text.startswith("Café & bar")
    assert "body{color:red}" not in text, "style is not content"
    assert "var x" not in text, "script is not content"
    assert "ignored" not in text, "head/title is not body text"
    assert "one two" in text, "runs of spaces collapse"
    assert "three—four" in text, "numeric entities decode"
    # paragraph elements become separate paragraphs, not one run-on line
    assert "Café & bar\n\none two\n\nthree—four" == text


def test_html_to_text_survives_malformed_markup() -> None:
    assert "still here" in html_to_text("<p>still here<div><span>")


# -- the guard ------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/x",
        "gopher://example.com/",
        "http://127.0.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.1/",
        "http://192.168.1.1/",
        "http://100.64.0.1/",
        "http://[::1]/",
        "http://[fd00::1]/",
        "http://localhost/",
        "http://metadata.google.internal/",
        "http://printer.local/",
        "http://redis.internal:6379/",
        "http://0.0.0.0/",
        "",
    ],
)
async def test_check_url_refuses_non_public_targets(url: str) -> None:
    with pytest.raises(BlockedURL):
        check_url(url)


@pytest.mark.parametrize(
    ("url", "embedded"),
    [
        # NAT64 (RFC 6052): a 64:ff9b::/96 address carries an IPv4 in its low
        # 32 bits, and ipaddress reports the whole block as is_global, so the
        # block used to let 127.0.0.1 and the metadata service straight through
        ("http://[64:ff9b::7f00:1]/", "127.0.0.1"),
        ("http://[64:ff9b::a9fe:a9fe]/", "169.254.169.254"),
        ("http://[64:ff9b::a00:1]/", "10.0.0.1"),
    ],
)
async def test_nat64_addresses_embedding_a_blocked_ipv4_are_blocked(
    url: str, embedded: str
) -> None:
    with pytest.raises(BlockedURL, match="NAT64-embedded") as exc:
        check_url(url)
    assert embedded in str(exc.value), "the error must name the embedded address"


async def test_a_nat64_address_with_a_public_embedded_ipv4_is_allowed() -> None:
    """The negative control: the NAT64 rule unwraps, it does not blanket-ban."""
    check_url("http://[64:ff9b::5db8:d822]/")  # 93.184.216.34, example.com
    await ensure_public("http://[64:ff9b::5db8:d822]/", resolver=public_resolver)


async def test_site_local_ipv6_is_blocked() -> None:
    """fec0::/10 is deprecated but still routable on some networks."""
    with pytest.raises(BlockedURL, match="site-local"):
        check_url("http://[fec0::1]/")


async def test_ipv4_mapped_ipv6_is_still_blocked() -> None:
    """Regression guard for the pre-existing ::ffff: path."""
    with pytest.raises(BlockedURL, match="loopback"):
        check_url("http://[::ffff:127.0.0.1]/")


async def test_ensure_public_blocks_a_name_that_resolves_to_loopback() -> None:
    # DNS rebinding: the NAME is public, the resolution is not
    async def rebinding(host: str, port: int) -> list[str]:
        return ["127.0.0.1"]

    with pytest.raises(BlockedURL, match="loopback"):
        await ensure_public("http://evil.example.com/", resolver=rebinding)
    await ensure_public("http://example.com/", resolver=public_resolver)  # allowed


# -- web_search -----------------------------------------------------------------


def _searxng(payload: object, status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/search"
        assert request.url.params["q"] == "durable workflow engines"
        assert request.url.params["format"] == "json"
        return httpx.Response(status, json=payload)

    return httpx.MockTransport(handler)


def _status(code: int) -> httpx.MockTransport:
    return httpx.MockTransport(lambda r: httpx.Response(code))


def _body(content: bytes) -> httpx.MockTransport:
    return httpx.MockTransport(lambda r: httpx.Response(200, content=content))


def _json(payload: object) -> httpx.MockTransport:
    return httpx.MockTransport(lambda r: httpx.Response(200, json=payload))


SEARXNG_BODY = {
    "results": [
        {
            "title": "Temporal",
            "url": "https://temporal.io/",
            "content": "A durable execution platform.",
        },
        {
            "title": "Restate",
            "url": "https://restate.dev/",
            "content": "Durable handlers as code.",
        },
        {"title": "Inngest", "url": "https://inngest.com/", "content": "Event-driven."},
    ]
}


async def test_web_search_shapes_title_url_snippet() -> None:
    out = await search_text(
        "durable workflow engines",
        transport=_searxng(SEARXNG_BODY),
        base_url="http://127.0.0.1:8888",
    )
    assert out.startswith("web_search('durable workflow engines') -> 3 of 3")
    assert "1. Temporal\n   https://temporal.io/\n   A durable execution platform." in out
    assert "https://restate.dev/" in out
    assert not out.startswith("error")


async def test_web_search_caps_the_number_of_results() -> None:
    out = await search_text(
        "durable workflow engines",
        transport=_searxng(SEARXNG_BODY),
        base_url="http://127.0.0.1:8888",
        results=1,
    )
    assert "-> 1 of 3 results" in out
    assert "temporal.io" in out
    assert "restate.dev" not in out


async def test_web_search_caps_output_length() -> None:
    # ten results whose urls are long enough that the whole listing would
    # exceed MAX_OUTPUT_CHARS (4000): the shaping stops early and says so
    body = {
        "results": [
            {"title": f"r{i}", "url": "https://e.com/" + "p" * 400, "content": "x" * 320}
            for i in range(10)
        ]
    }
    out = await search_text(
        "durable workflow engines",
        transport=_searxng(body),
        base_url="http://127.0.0.1:8888",
        results=10,
    )
    assert len(out) < 4500, "the model must not receive an unbounded page of results"
    assert "output cap reached" in out


def test_shape_results_without_key_says_no_results() -> None:
    assert shape_results({"unexpected": []}, "q") == "web_search('q'): no results"


@pytest.mark.parametrize(
    ("transport", "expected"),
    [
        (httpx.MockTransport(_http_error), "backend unreachable"),
        (httpx.MockTransport(_timeout), "timed out"),
        (_status(500), "returned HTTP 500"),
        (_body(b"<html>not json</html>"), "did not return JSON"),
        (_json({"foo": 1}), "no 'results'"),
    ],
)
async def test_web_search_degraded_paths_are_error_strings(
    transport: httpx.AsyncBaseTransport, expected: str
) -> None:
    out = await search_text(
        "durable workflow engines",
        transport=transport,
        base_url="http://127.0.0.1:8888",
    )
    assert out.startswith("error: "), f"the loop classifies failure by this prefix: {out!r}"
    assert expected in out


async def test_web_search_empty_query_is_an_error_not_a_crash() -> None:
    out = await search_text("   ")
    assert out == "error: web_search needs a query"


# -- fetch_url ------------------------------------------------------------------


def _page(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "text/html; charset=utf-8"},
        text=(
            "<html><head><title>t</title><script>nav()</script></head><body>"
            "<h1>Report</h1><p>Body &amp; text</p></body></html>"
        ),
    )


async def test_fetch_url_returns_page_text() -> None:
    out = await fetch_url_text(
        "http://example.com/report",
        transport=httpx.MockTransport(_page),
        resolver=public_resolver,
    )
    assert out.startswith("http://example.com/report\n\n")
    assert "Report\n\nBody & text" in out
    assert "nav()" not in out


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/pub",
        "http://127.0.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.1.2.3/",
        "http://100.64.0.1/",
        "http://[::1]/",
        "http://localhost/",
        "http://metadata.google.internal/",
    ],
)
async def test_fetch_url_refuses_private_and_non_http_targets(url: str) -> None:
    def boom(request: httpx.Request) -> httpx.Response:  # must never be reached
        raise AssertionError(f"fetch_url sent a request for a blocked url: {request.url}")

    out = await fetch_url_text(url, transport=httpx.MockTransport(boom), resolver=public_resolver)
    assert out.startswith("error: ")


async def test_fetch_url_refuses_a_name_resolving_to_a_private_address() -> None:
    async def rebinding(host: str, port: int) -> list[str]:
        return ["10.0.0.7"]

    def boom(request: httpx.Request) -> httpx.Response:
        raise AssertionError("a private resolution must not be fetched")

    out = await fetch_url_text(
        "http://rebind.example.com/", transport=httpx.MockTransport(boom), resolver=rebinding
    )
    assert out.startswith("error: ") and "private" in out


async def test_fetch_url_refuses_a_redirect_into_the_private_network() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "example.com":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/"})
        raise AssertionError(f"followed a redirect to a blocked host: {request.url}")

    out = await fetch_url_text(
        "http://example.com/", transport=httpx.MockTransport(handler), resolver=public_resolver
    )
    assert out.startswith("error: ")
    assert "169.254.169.254" in out


async def test_fetch_url_follows_a_public_redirect() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/old":
            return httpx.Response(301, headers={"location": "/new"})
        return httpx.Response(200, headers={"content-type": "text/plain"}, text="moved here")

    out = await fetch_url_text(
        "http://example.com/old", transport=httpx.MockTransport(handler), resolver=public_resolver
    )
    assert out.startswith("http://example.com/new\n\n")
    assert "moved here" in out


async def test_fetch_url_gives_up_on_a_redirect_loop() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "/again"})

    out = await fetch_url_text(
        "http://example.com/loop", transport=httpx.MockTransport(handler), resolver=public_resolver
    )
    assert out.startswith("error: ") and "too many redirects" in out


async def test_fetch_url_refuses_a_redirect_without_location() -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(302))
    out = await fetch_url_text("http://example.com/", transport=transport, resolver=public_resolver)
    assert out.startswith("error: ") and "no location" in out


async def test_fetch_url_body_size_cap_truncates_download() -> None:
    transport = httpx.MockTransport(
        lambda r: httpx.Response(200, headers={"content-type": "text/plain"}, content=b"y" * 5000)
    )
    out = await fetch_url_text(
        "http://example.com/big", transport=transport, resolver=public_resolver, max_bytes=100
    )
    assert "stop reading http://example.com/big at the 100 byte limit" in out
    assert len(out) < 400


async def test_fetch_url_output_char_cap_truncates_text() -> None:
    transport = httpx.MockTransport(
        lambda r: httpx.Response(200, headers={"content-type": "text/plain"}, text="z" * 900)
    )
    out = await fetch_url_text(
        "http://example.com/long", transport=transport, resolver=public_resolver, max_chars=120
    )
    assert "more chars of the page truncated" in out
    assert len(out) < 300


@pytest.mark.parametrize(
    ("transport", "expected"),
    [
        (httpx.MockTransport(_timeout), "timed out"),
        (httpx.MockTransport(_http_error), "could not fetch"),
        (_status(404), "HTTP 404 Not Found"),
        (_status(503), "HTTP 503 Service Unavailable"),
    ],
)
async def test_fetch_url_degraded_paths_are_error_strings(
    transport: httpx.AsyncBaseTransport, expected: str
) -> None:
    out = await fetch_url_text(
        "http://example.com/x", transport=transport, resolver=public_resolver
    )
    assert out.startswith("error: ")
    assert expected in out


async def test_fetch_url_unreadable_content_type_is_a_note_not_an_error() -> None:
    transport = httpx.MockTransport(
        lambda r: httpx.Response(
            200, headers={"content-type": "application/pdf"}, content=b"%PDF-1.7"
        )
    )
    out = await fetch_url_text(
        "http://example.com/a.pdf", transport=transport, resolver=public_resolver
    )
    assert not out.startswith("error"), "a PDF is a readable answer, just not readable text"
    assert "unsupported content type" in out


# -- the executor wrapper and the role wiring -----------------------------------


def test_web_specs_are_exactly_the_two_tools() -> None:
    assert [s.name for s in WEB_SPECS] == ["web_search", "fetch_url"]
    by_name = {s.name: s for s in WEB_SPECS}
    assert "query" in by_name["web_search"].input_schema["properties"]
    assert by_name["web_search"].input_schema["required"] == ["query"]
    assert by_name["fetch_url"].input_schema["required"] == ["url"]


async def test_executor_answers_web_tools_and_delegates_the_rest() -> None:
    class Inner:
        async def execute(self, name: str, args: dict) -> str:
            return f"inner:{name}"

        def specs(self, allowed: set[str] | None = None) -> list:
            return []

    inner = Inner()
    ex = WebAwareExecutor(
        inner, transport=_searxng(SEARXNG_BODY), search_url="http://127.0.0.1:8888"
    )
    names = [s.name for s in ex.specs()]
    assert names == ["web_search", "fetch_url"]

    out = await ex.execute("web_search", {"query": "durable workflow engines", "results": "2"})
    assert "temporal.io" in out
    assert await ex.execute("read_file", {"path": "x"}) == "inner:read_file"


async def test_executor_never_raises_on_bad_input() -> None:
    class Inner:
        async def execute(self, name: str, args: dict) -> str:
            return "inner"

        def specs(self, allowed: set[str] | None = None) -> list:
            return []

    ex = WebAwareExecutor(Inner())
    assert (await ex.execute("fetch_url", {})).startswith("error: ")
    assert (await ex.execute("fetch_url", {"url": "file:///etc/passwd"})).startswith("error: ")
    assert (await ex.execute("web_search", {"query": ""})).startswith("error: ")


def test_web_executor_factory_wraps_the_inner_executor() -> None:
    class Inner:
        async def execute(self, name: str, args: dict) -> str:
            return "inner"

        def specs(self, allowed: set[str] | None = None) -> list:
            return []

    inner = Inner()
    wrapped = web_executor(inner)
    assert isinstance(wrapped, WebAwareExecutor)
    assert {s.name for s in wrapped.specs()} == {"web_search", "fetch_url"}
    assert wrapped._inner is inner, "the wrapper must not drop the role's own tools"


def test_investigator_and_coder_are_wired_but_the_auditor_is_not() -> None:
    """The brief names investigators and coders only: the auditor reads the diff
    and must not gain outbound network access.

    Checked on `auditor.py`'s own source, not on its namespace: `hasattr` only
    proves the module does not import the factory under that name, while any of
    the aliasing routes below would still put the tools in front of the model.
    The behavioural half (the auditor's assembled executor offers no web tool)
    is asserted against a real graph in
    tests/unit/test_auditor.py::test_auditor_reads_context_and_reports.
    """
    from rework.roles.builtin import auditor, coder, investigator

    assert investigator.web_executor is web_executor
    assert coder.web_executor is web_executor

    tree = ast.parse(Path(auditor.__file__).read_text(encoding="utf-8"))
    constructed = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    } | {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert constructed.isdisjoint({"web_executor", "WebAwareExecutor"}), (
        f"auditor.py constructs a web executor: {constructed}"
    )
    assert "rework.web" not in Path(auditor.__file__).read_text(encoding="utf-8")
