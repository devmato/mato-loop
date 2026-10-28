from __future__ import annotations

import asyncio
import gzip
import html
import ipaddress
import logging
import socket
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from io import BytesIO
from unittest.mock import AsyncMock, Mock

import pytest
from PIL import Image
from pypdf import PdfWriter
from yarl import URL

import mato_loop_backend.web.fetch as web_fetch
from mato_loop_backend.web.fetch import (
    WebFetchError,
    canonicalize_url,
    fetch_web,
    request_web_bytes,
)

Responder = Callable[[str, dict[str, str]], Awaitable[bytes]]


def _response(
    body: bytes = b"",
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
    content_length: bool = True,
) -> bytes:
    reasons = {200: "OK", 202: "Accepted", 302: "Found", 404: "Not Found"}
    response_headers = dict(headers or {})
    if content_length:
        response_headers.setdefault("Content-Length", str(len(body)))
    response_headers.setdefault("Connection", "close")
    lines = [f"HTTP/1.1 {status} {reasons.get(status, 'Status')}"]
    lines.extend(f"{name}: {value}" for name, value in response_headers.items())
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + body


@asynccontextmanager
async def _server(responder: Responder, *, host: str = "127.0.0.1") -> AsyncIterator[int]:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            request_lines = request.decode("latin-1").split("\r\n")
            target = request_lines[0].split(" ", 2)[1]
            headers = {
                name.strip().lower(): value.strip()
                for line in request_lines[1:]
                if ":" in line
                for name, value in [line.split(":", 1)]
            }
            writer.write(await responder(target, headers))
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, host, 0)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()
        await server.wait_closed()


def _origin(port: int, host: str = "127.0.0.1") -> str:
    return f"http://{host}:{port}"


@pytest.mark.parametrize(("format", "preferred"), [
    ("markdown", "text/markdown"), ("text", "text/plain"), ("html", "text/html"),
])
async def test_format_accept_header_selects_page_instead_of_interstitial(format, preferred):
    async def responder(_path, headers):
        body = b"Article" if headers["accept"].startswith(preferred + ";q=1.0") else b"Consent"
        return _response(body, headers={"Content-Type": "text/html"})

    async with _server(responder) as port:
        result = await fetch_web(_origin(port), format=format, public_addresses_only=False)
    assert result.content == "Article"


async def _error_code(awaitable: Awaitable[object]) -> str:
    with pytest.raises(WebFetchError) as exc_info:
        await awaitable
    assert str(exc_info.value) == exc_info.value.code
    return exc_info.value.code


def _blank_pdf() -> bytes:
    buffer = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=100)
    writer.write(buffer)
    return buffer.getvalue()


def _scan_pdf() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (200, 100), "white").save(buffer, format="PDF")
    return buffer.getvalue()


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("ftp://example.com/file", "webfetch.unsupportedScheme"),
        ("https://user:secret@example.com/", "webfetch.userinfoForbidden"),
        ("/relative", "webfetch.unsupportedScheme"),
        ("http://./", "webfetch.invalidUrl"),
        ("http://-pin.attacker.test/", "webfetch.invalidUrl"),
        ("https://[::1", "webfetch.invalidUrl"),
    ],
)
async def test_rejects_invalid_urls(url: str, code: str) -> None:
    assert await _error_code(fetch_web(url)) == code


def test_canonicalizes_urls_with_query_values() -> None:
    url = "HTTPS://ExAmPle.com/path?token=very-secret&empty=#fragment"

    assert canonicalize_url(url) == "https://example.com/path?token=very-secret&empty="


async def test_authorization_runs_after_normalization_and_resolution(monkeypatch) -> None:
    authorize = AsyncMock(side_effect=WebFetchError("permission.denied"))
    resolve = AsyncMock(return_value=(ipaddress.ip_address("8.8.8.8"),))
    monkeypatch.setattr(web_fetch, "_resolve_addresses", resolve)

    assert await _error_code(
        fetch_web("HTTPS://ExAmPle.com", authorize_url=authorize)
    ) == "permission.denied"
    authorize.assert_awaited_once_with("https://example.com/")
    resolve.assert_awaited_once()


async def test_authorization_runs_once_before_post_authorization_resolution(
    monkeypatch,
) -> None:
    authorize = AsyncMock()
    resolve = AsyncMock(
        side_effect=[
            (ipaddress.ip_address("8.8.8.8"),),
            (ipaddress.ip_address("192.0.2.1"),),
        ]
    )
    request = AsyncMock(
        return_value=web_fetch._Response(
            status=200,
            location=None,
            content_type="text/plain",
            charset=None,
            content_disposition=None,
            body=b"ok",
        )
    )
    monkeypatch.setattr(web_fetch, "_resolve_addresses", resolve)
    monkeypatch.setattr(web_fetch, "_request_once", request)

    result = await fetch_web(
        "https://example.com/",
        public_addresses_only=False,
        authorize_url=authorize,
    )

    assert result.content == "ok"
    authorize.assert_awaited_once_with("https://example.com/")
    assert resolve.await_count == 2
    assert request.await_args.args[0].addresses == (
        ipaddress.ip_address("192.0.2.1"),
    )


@pytest.mark.parametrize("timeout", [0, 121, True, "30"])
async def test_rejects_invalid_timeouts(timeout: object) -> None:
    assert await _error_code(fetch_web("https://example.com", timeout=timeout)) == (
        "webfetch.timeoutInvalid"
    )


async def test_rejects_invalid_format() -> None:
    assert await _error_code(fetch_web("https://example.com", format="json")) == (
        "webfetch.formatUnsupported"
    )


@pytest.mark.parametrize(
    "address",
    [
        "169.254.169.254",
        "169.254.1.1",
        "100.100.100.200",
        "fd00:ec2::254",
        "fec0::1",
        "192.0.2.1",
        "100.64.0.1",
        "224.0.0.1",
        "0.0.0.0",
    ],
)
async def test_default_rejects_every_non_public_address_class(address: str) -> None:
    rendered = f"[{address}]" if ":" in address else address
    url = f"http://{rendered}"
    assert await _error_code(fetch_web(url)) == "webfetch.addressBlocked"


@pytest.mark.parametrize(
    "address",
    [
        "169.254.169.254",
        "169.254.1.1",
        "100.100.100.200",
        "fd00:ec2::254",
        "fec0::1",
        "192.0.2.1",
        "100.64.0.1",
        "224.0.0.1",
        "0.0.0.0",
    ],
)
async def test_unrestricted_mode_accepts_non_public_address_classes(
    monkeypatch, address: str
) -> None:
    request = AsyncMock(
        return_value=web_fetch._Response(
            status=200,
            location=None,
            content_type="text/plain",
            charset=None,
            content_disposition=None,
            body=b"ok",
        )
    )
    monkeypatch.setattr(web_fetch, "_request_once", request)
    url = f"http://{address}"
    if ":" in address:
        url = f"http://[{address}]"

    result = await fetch_web(url, public_addresses_only=False)

    assert result.content == "ok"
    request.assert_awaited_once()


async def test_unrestricted_mode_accepts_reachable_loopback_target() -> None:
    async def responder(target: str, headers: dict[str, str]) -> bytes:
        return _response(b"ok", headers={"Content-Type": "text/plain"})

    async with _server(responder) as port:
        url = f"http://127.0.0.1:{port}/"
        assert await _error_code(fetch_web(url)) == "webfetch.addressBlocked"
        result = await fetch_web(url, public_addresses_only=False)

    assert result.content == "ok"


async def test_dns_answers_are_validated_as_a_set(monkeypatch) -> None:
    async def resolve(host: str, port: int):
        return (ipaddress.ip_address("8.8.8.8"), ipaddress.ip_address("127.0.0.1"))

    monkeypatch.setattr(web_fetch, "_resolve_addresses", resolve)
    assert await _error_code(fetch_web("http://mixed.example")) == "webfetch.addressBlocked"


async def test_dns_is_pinned_and_request_headers_are_safe(monkeypatch) -> None:
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("all_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("no_proxy", "")
    seen_headers: dict[str, str] = {}

    async def responder(target: str, headers: dict[str, str]) -> bytes:
        seen_headers.update(headers)
        return _response(b"pinned", headers={"Content-Type": "text/plain"})

    async with _server(responder) as port:
        async def resolve(host: str, resolved_port: int):
            assert host == "pinned.test"
            assert resolved_port == port
            return (ipaddress.ip_address("127.0.0.1"),)

        monkeypatch.setattr(web_fetch, "_resolve_addresses", resolve)
        result = await fetch_web(
            f"http://pinned.test:{port}/resource",
            public_addresses_only=False,
        )

    assert result.content == "pinned"
    assert seen_headers["host"] == f"pinned.test:{port}"
    assert seen_headers["user-agent"] == "MatoLoop/1.0"
    assert seen_headers["accept-encoding"] == "gzip, deflate"
    assert seen_headers["accept"] == "text/markdown;q=1.0, text/x-markdown;q=0.9, text/plain;q=0.8, text/html;q=0.7, */*;q=0.1"
    assert {"authorization", "cookie", "referer"}.isdisjoint(seen_headers)


async def test_tls_failure_has_a_specific_safe_error(monkeypatch, caplog) -> None:
    async def resolve(host: str, port: int):
        return (ipaddress.ip_address("8.8.8.8"),)

    class FailingSession:
        def __init__(self, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback) -> None:
            pass

        async def request(self, *args, **kwargs):
            raise web_fetch.SSLError("certificate mismatch", web_fetch.CurlECode.PEER_FAILED_VERIFICATION)

    monkeypatch.setattr(web_fetch, "_resolve_addresses", resolve)
    monkeypatch.setattr(web_fetch, "AsyncSession", FailingSession)

    with caplog.at_level(logging.WARNING, logger=web_fetch.__name__):
        code = await _error_code(fetch_web("https://tls.example/private?token=secret"))

    assert code == "webfetch.tlsFailed"
    assert "tls.example" in caplog.text
    assert "token" not in caplog.text
    assert "secret" not in caplog.text


async def test_redirects_are_resolved_and_each_origin_is_validated() -> None:
    authorized_urls: list[str] = []

    async def authorize(url: str) -> None:
        authorized_urls.append(url)

    async def destination(target: str, headers: dict[str, str]) -> bytes:
        return _response(
            b'<main><a href="/next">destination</a></main>',
            headers={"Content-Type": "text/html"},
        )

    async with _server(destination) as destination_port:
        async def source(target: str, headers: dict[str, str]) -> bytes:
            return _response(
                status=302,
                headers={"Location": f"http://127.0.0.1:{destination_port}/target"},
            )

        async with _server(source) as source_port:
            source_url = f"http://127.0.0.1:{source_port}/start"
            result = await fetch_web(
                source_url,
                public_addresses_only=False,
                authorize_url=authorize,
            )

    assert result.final_url == f"http://127.0.0.1:{destination_port}/target"
    assert result.status_code == 200
    assert result.content == f"[destination](http://127.0.0.1:{destination_port}/next)"
    assert authorized_urls == [
        f"http://127.0.0.1:{source_port}/start",
        f"http://127.0.0.1:{destination_port}/target",
    ]


async def test_approval_wait_does_not_consume_network_timeout() -> None:
    async def authorize(_url: str) -> None:
        await asyncio.sleep(1.05)

    async def responder(target: str, headers: dict[str, str]) -> bytes:
        return _response(b"ok", headers={"Content-Type": "text/plain"})

    async with _server(responder) as port:
        result = await fetch_web(
            f"http://127.0.0.1:{port}/",
            timeout=1,
            public_addresses_only=False,
            authorize_url=authorize,
        )

    assert result.content == "ok"


async def test_redirect_limit_and_invalid_redirect() -> None:
    async def responder(target: str, headers: dict[str, str]) -> bytes:
        if target == "/missing":
            return _response(status=302)
        number = int(target.removeprefix("/"))
        return _response(status=302, headers={"Location": f"/{number + 1}"})

    async with _server(responder) as port:
        assert await _error_code(
            fetch_web(f"{_origin(port)}/0", public_addresses_only=False)
        ) == "webfetch.tooManyRedirects"
        assert await _error_code(
            fetch_web(f"{_origin(port)}/missing", public_addresses_only=False)
        ) == "webfetch.redirectInvalid"


async def test_raw_byte_transport_preserves_get_and_posts_without_redirect_replay() -> None:
    seen_headers: list[dict[str, str]] = []

    async def responder(target: str, headers: dict[str, str]) -> bytes:
        seen_headers.append(headers)
        if target == "/redirect":
            return _response(status=302, headers={"Location": "/target"})
        return _response(b"<transcript />", headers={"Content-Type": "text/xml; charset=utf-8"})

    async with _server(responder) as port:
        origin = _origin(port)
        fetched = await request_web_bytes(
            f"{origin}/captions",
            public_addresses_only=False,
            max_response_bytes=100,
        )
        posted = await request_web_bytes(
            f"{origin}/player",
            method="POST",
            body=b'{"videoId":"abcdefghijk"}',
            request_content_type="application/json",
            public_addresses_only=False,
            max_response_bytes=100,
        )
        redirect_code = await _error_code(
            request_web_bytes(
                f"{origin}/redirect",
                method="POST",
                body=b"{}",
                request_content_type="application/json",
                public_addresses_only=False,
                max_response_bytes=100,
            )
        )

    assert fetched.data == posted.data == b"<transcript />"
    assert fetched.content_type == posted.content_type == "text/xml"
    assert seen_headers[1]["content-type"] == "application/json"
    assert {"authorization", "cookie", "referer"}.isdisjoint(seen_headers[1])
    assert redirect_code == "webfetch.requestFailed"


async def test_html_conversion_removes_active_content() -> None:
    document = b"""<!doctype html><html><head><meta charset=utf-8>
        <title> MatoLoop\nDocs </title><style>.x{}</style>
        <script>secret()</script><link href=/track></head><body onload=evil()>
        <nav>Navigation</nav><main><h1>Titel</h1><p>Hallo
        <a href="/docs">Dokumentation</a> und
        <a href="javascript:evil()">Welt</a>.</p></main><footer>Footer</footer></body></html>"""

    async def responder(target: str, headers: dict[str, str]) -> bytes:
        return _response(document, headers={"Content-Type": "text/html; charset=utf-8"})

    async with _server(responder) as port:
        url = f"{_origin(port)}/page"
        markdown_result = await fetch_web(url, public_addresses_only=False)
        text_result = await fetch_web(url, format="text", public_addresses_only=False)
        html_result = await fetch_web(url, format="html", public_addresses_only=False)

    assert "# Titel" in markdown_result.content
    assert f"({_origin(port)}/docs)" in markdown_result.content
    assert "Navigation" not in markdown_result.content
    assert "Footer" not in markdown_result.content
    assert "secret" not in markdown_result.content
    assert markdown_result.title == "MatoLoop Docs"
    assert markdown_result.status_code == 200
    assert markdown_result.output_format == "markdown"
    assert text_result.title == "MatoLoop Docs"
    assert html_result.title == "MatoLoop Docs"
    assert text_result.content == "Titel Hallo Dokumentation und Welt."
    assert "<script" not in html_result.content
    assert "<style" not in html_result.content
    assert "onload" not in html_result.content
    assert "javascript:" not in html_result.content


async def test_plain_text_is_escaped_for_html() -> None:
    async def responder(target: str, headers: dict[str, str]) -> bytes:
        return _response(b"<script>alert(1)</script>", headers={"Content-Type": "text/plain"})

    async with _server(responder) as port:
        result = await fetch_web(
            f"{_origin(port)}/text", format="html", public_addresses_only=False
        )

    assert result.content == "<pre>&lt;script&gt;alert(1)&lt;/script&gt;</pre>"


@pytest.mark.parametrize("format", [None, "text", "html"])
@pytest.mark.parametrize(
    ("content_type", "document"),
    [
        ("Application/JSON; charset=utf-8", ' \r\n{"name":"A & B <script>\\u003c</script>",'
         '"nested":[null,true,{"value":1.2300}],"name":"M\u00fcnchen"}\r\n '),
        ("application/ld+json", '[{"@id":"https://example.com/?a=1&b=2"}]\n'),
        ("application/problem+json", '{"status":200,"detail":"<b>data</b>"}'),
        ("application/vnd.example.v1+json", '"scalar & <value>"'),
        ("text/csv; charset=utf-8", 'name,value\r\n"A & B","first\r\nsecond"\r\n'),
        ("application/xml", '<!DOCTYPE data [<!ENTITY secret SYSTEM "file:///etc/passwd">]>'
         '<data>&secret;<value>A &amp; B</value></data>\n'),
        ("text/xml", '<data><![CDATA[<script>literal</script>]]></data>\n'),
        ("application/rss+xml", '<rss><channel><title>A &amp; B</title></channel></rss>\n'),
        ("application/atom+xml", '<feed xmlns="http://www.w3.org/2005/Atom"><title>A</title></feed>'),
        ("application/x-ndjson", '{"line":1}\n{"line":2}\n'),
        ("application/ndjson", '{"line":1}\r\n{"line":2}\r\n'),
        ("application/json", '{"incomplete":'),
    ],
)
async def test_structured_text_preserves_source_without_parsing(
    content_type: str, document: str, format: str | None,
) -> None:
    async def responder(target: str, headers: dict[str, str]) -> bytes:
        assert target == "/data"
        return _response(document.encode(), headers={"Content-Type": content_type})

    async with _server(responder) as port:
        result = await fetch_web(
            f"{_origin(port)}/data", public_addresses_only=False,
            **({"format": format} if format is not None else {}),
        )

    assert result.content == (
        f"<pre>{html.escape(document)}</pre>" if format == "html" else document
    )
    assert result.content_type == content_type.partition(";")[0].lower()
    assert result.output_format == ("html" if format == "html" else "text")
    assert result.status_code == 200
    assert result.title is None
    assert result.images == ()


@pytest.mark.parametrize("format", ["markdown", "html"])
async def test_structured_text_conversion_obeys_remaining_timeout(monkeypatch, format) -> None:
    target = web_fetch._Target(URL("https://example.com/data"), "example.com", 443, ())
    response = web_fetch._Response(200, None, "application/json", None, None, b"{}")
    monkeypatch.setattr(
        web_fetch, "_follow_redirects", AsyncMock(return_value=(target, response, 0.01)),
    )

    def slow_decode(*args, **kwargs):
        time.sleep(0.05)
        return "{}"

    monkeypatch.setattr(web_fetch, "_decode_text", slow_decode)

    assert await _error_code(fetch_web(str(target.url), format=format)) == "webfetch.timeout"


async def test_html_title_ignores_title_elements_outside_the_head() -> None:
    document = b"<html><body><svg><title>Diagram title</title></svg><p>Text</p></body></html>"

    async def responder(target: str, headers: dict[str, str]) -> bytes:
        return _response(document, headers={"Content-Type": "text/html"})

    async with _server(responder) as port:
        result = await fetch_web(
            f"{_origin(port)}/page", public_addresses_only=False
        )

    assert result.title is None


@pytest.mark.parametrize("markdown_type", ["text/markdown", "text/x-markdown"])
async def test_active_markup_is_removed_or_escaped_from_markdown(markdown_type) -> None:
    async def responder(target: str, headers: dict[str, str]) -> bytes:
        content_type = markdown_type if target == "/markdown" else "text/plain"
        return _response(
            b"before<script>alert(1)</script>after",
            headers={"Content-Type": content_type},
        )

    async with _server(responder) as port:
        markdown_result = await fetch_web(
            f"{_origin(port)}/markdown", public_addresses_only=False
        )
        plain_result = await fetch_web(f"{_origin(port)}/plain", public_addresses_only=False)

    assert markdown_result.content == "beforeafter"
    assert plain_result.content == "before&lt;script&gt;alert(1)&lt;/script&gt;after"


async def test_supported_image_returns_bytes_and_safe_filename() -> None:
    image = b"\x89PNG\r\n\x1a\ncontent"

    async def responder(target: str, headers: dict[str, str]) -> bytes:
        return _response(
            image,
            headers={
                "Content-Type": "image/png",
                "Content-Disposition": 'attachment; filename="../../payload.svg"',
            },
        )

    async with _server(responder) as port:
        result = await fetch_web(
            f"{_origin(port)}/fallback.png", public_addresses_only=False,
            user_agent="Mozilla/5.0",
        )

    assert result.content_type == "image/png"
    assert result.content == ""
    assert len(result.images) == 1
    assert result.images[0].data == image
    assert result.images[0].mime_type == "image/png"
    assert result.images[0].filename == "payload.png"


async def test_text_pdf_is_extracted_without_vision(monkeypatch) -> None:
    vision = AsyncMock(side_effect=AssertionError("Vision must not be used"))
    monkeypatch.setattr(
        web_fetch,
        "extract_pdf_pages",
        lambda _data: (["Manual text"], {"pageCount": 1, "imagePages": []}),
    )

    async def responder(target: str, headers: dict[str, str]) -> bytes:
        return _response(b"%PDF-1.7 fake", headers={"Content-Type": "application/pdf"})

    async with _server(responder) as port:
        result = await fetch_web(
            f"{_origin(port)}/manual.pdf",
            public_addresses_only=False,
            pdf_vision_handler=vision,
        )

    assert result.content_type == "application/pdf"
    assert result.output_format == "text"
    assert result.content == "--- Page 1 ---\nManual text"
    assert result.page_count == 1
    assert result.images == ()
    vision.assert_not_awaited()


async def test_text_pdf_can_return_selected_page_images(monkeypatch) -> None:
    image = b"\xff\xd8\xffpage"
    render = Mock(return_value=image)
    monkeypatch.setattr(
        web_fetch,
        "extract_pdf_pages",
        lambda _data: (["Diagram labels", "Notes"], {"pageCount": 2, "imagePages": []}),
    )
    monkeypatch.setattr(web_fetch, "_render_pdf_page", render)

    async def responder(target: str, headers: dict[str, str]) -> bytes:
        return _response(b"%PDF-1.7 fake", headers={"Content-Type": "application/pdf"})

    async with _server(responder) as port:
        result = await fetch_web(
            f"{_origin(port)}/drawing.pdf",
            public_addresses_only=False,
            pdf_pages=(3, 2, 1, 2),
        )

    assert result.page_count == 2
    assert [item.page_number for item in result.images] == [1, 2]
    assert [item.filename for item in result.images] == [
        "drawing-page-1.jpg",
        "drawing-page-2.jpg",
    ]
    assert result.unavailable_pages == (3,)
    assert render.call_count == 2


async def test_scan_pdf_requires_vision_and_uses_rendered_pages(monkeypatch) -> None:
    document = _scan_pdf()
    seen: list[tuple[int, bytes]] = []
    render = Mock(wraps=web_fetch._render_pdf_page)
    monkeypatch.setattr(web_fetch, "_render_pdf_page", render)

    async def responder(target: str, headers: dict[str, str]) -> bytes:
        return _response(document, headers={"Content-Type": "application/pdf"})

    async def vision(
        pages: list[tuple[int, bytes]], private_content: bool
    ) -> dict[int, str]:
        assert private_content is True
        seen.extend(pages)
        return {1: "Recognized scan text"}

    async with _server(responder) as port:
        url = f"{_origin(port)}/scan.pdf"
        assert await _error_code(
            fetch_web(url, public_addresses_only=False)
        ) == "webfetch.pdfVisionUnavailable"
        result = await fetch_web(
            url,
            public_addresses_only=False,
            pdf_vision_handler=vision,
            pdf_pages=(1,),
        )

    assert len(seen) == 1
    assert seen[0][0] == 1
    assert seen[0][1].startswith(b"\xff\xd8\xff")
    assert result.content == "--- Page 1 ---\nRecognized scan text"
    assert result.images[0].data == seen[0][1]
    assert render.call_count == 1


async def test_blank_pdf_does_not_require_vision() -> None:
    document = _blank_pdf()

    async def responder(target: str, headers: dict[str, str]) -> bytes:
        return _response(document, headers={"Content-Type": "application/pdf"})

    async with _server(responder) as port:
        result = await fetch_web(
            f"{_origin(port)}/blank.pdf",
            public_addresses_only=False,
        )

    assert result.content == "--- Page 1 ---"


@pytest.mark.parametrize(
    ("content_type", "body", "code"),
    [
        ("image/svg+xml", b"<svg/>", "webfetch.contentTypeUnsupported"),
        ("application/octet-stream", b"binary", "webfetch.contentTypeUnsupported"),
        ("application/octet-stream", b'{"value":1}', "webfetch.contentTypeUnsupported"),
        ("text/vnd.example+json", b"{}", "webfetch.contentTypeUnsupported"),
        ("application/*+json", b"{}", "webfetch.contentTypeUnsupported"),
        ("application/+json", b"{}", "webfetch.contentTypeUnsupported"),
        ("image/png", b"not a png", "webfetch.contentTypeMismatch"),
    ],
)
async def test_rejects_unsupported_or_spoofed_binary_content(
    content_type: str, body: bytes, code: str
) -> None:
    async def responder(target: str, headers: dict[str, str]) -> bytes:
        return _response(body, headers={"Content-Type": content_type})

    async with _server(responder) as port:
        assert await _error_code(
            fetch_web(f"{_origin(port)}/file.json", public_addresses_only=False)
        ) == code


@pytest.mark.parametrize(
    ("header", "expected_type"),
    [
        ('Application/Zip; filename="private-value"', "application/zip"),
        ("application/" + "x" * 127, "application/" + "x" * 127),
        ("application/" + "x" * 128, None),
        ("text/plain <script>private-value</script>", None),
        ("", None),
    ],
)
async def test_unsupported_mime_diagnostics_are_bounded_and_do_not_expose_parameters(
    header: str, expected_type: str | None,
) -> None:
    async def responder(target: str, headers: dict[str, str]) -> bytes:
        return _response(b"private response", headers={"Content-Type": header})

    async with _server(responder) as port:
        with pytest.raises(WebFetchError) as exc_info:
            await fetch_web(f"{_origin(port)}/data", public_addresses_only=False)

    assert exc_info.value.code == "webfetch.contentTypeUnsupported"
    assert exc_info.value.params == {
        "statusCode": 200,
        **({"contentType": expected_type} if expected_type is not None else {}),
    }


@pytest.mark.parametrize("content_type", ["text/plain", "application/json"])
async def test_content_length_is_rejected_before_body(content_type: str) -> None:
    limit = 1_024

    async def responder(target: str, headers: dict[str, str]) -> bytes:
        return _response(
            headers={
                "Content-Type": content_type,
                "Content-Length": str(limit + 1),
            },
            content_length=False,
        )

    async with _server(responder) as port:
        assert await _error_code(
            fetch_web(
                f"{_origin(port)}/large",
                public_addresses_only=False,
                max_response_bytes=limit,
            )
        ) == "webfetch.responseTooLarge"


@pytest.mark.parametrize("compressed", [False, True])
@pytest.mark.parametrize("content_type", ["text/plain", "application/json"])
async def test_streamed_and_decompressed_bodies_are_limited(
    compressed: bool, content_type: str,
) -> None:
    limit = 1_024
    body = b"x" * (limit + 1)
    wire_body = gzip.compress(body) if compressed else body

    async def responder(target: str, headers: dict[str, str]) -> bytes:
        response_headers = {"Content-Type": content_type}
        if compressed:
            response_headers["Content-Encoding"] = "gzip"
        return _response(
            wire_body,
            headers=response_headers,
            content_length=compressed,
        )

    async with _server(responder) as port:
        assert await _error_code(
            fetch_web(
                f"{_origin(port)}/large",
                public_addresses_only=False,
                max_response_bytes=limit,
            )
        ) == "webfetch.responseTooLarge"


@pytest.mark.parametrize("content_type", ["text/plain", "application/problem+json"])
async def test_timeout_and_http_errors_have_stable_codes(content_type: str) -> None:
    async def responder(target: str, headers: dict[str, str]) -> bytes:
        if target == "/slow":
            await asyncio.sleep(2)
            return _response(b"late", headers={"Content-Type": "text/plain"})
        return _response(
            b'{"detail":"upstream details"}', status=404,
            headers={"Content-Type": content_type},
        )

    async with _server(responder) as port:
        assert await _error_code(
            fetch_web(f"{_origin(port)}/slow", timeout=1, public_addresses_only=False)
        ) == "webfetch.timeout"
        with pytest.raises(WebFetchError) as exc_info:
            await fetch_web(f"{_origin(port)}/missing", public_addresses_only=False)

    assert exc_info.value.code == "webfetch.requestFailed"
    assert exc_info.value.params == {"statusCode": 404}


async def test_http_error_status_is_returned_without_reading_large_body() -> None:
    async def responder(target: str, headers: dict[str, str]) -> bytes:
        return _response(
            b"x",
            status=404,
            headers={
                "Content-Type": "text/plain",
                "Content-Length": str(web_fetch.MAX_RESPONSE_BYTES + 1),
            },
            content_length=False,
        )

    async with _server(responder) as port:
        with pytest.raises(WebFetchError) as exc_info:
            await fetch_web(
                f"{_origin(port)}/missing",
                public_addresses_only=False,
            )

    assert exc_info.value.code == "webfetch.requestFailed"
    assert exc_info.value.params == {"statusCode": 404}


async def test_accepted_response_is_not_treated_as_fetched_evidence() -> None:
    async def responder(target: str, headers: dict[str, str]) -> bytes:
        return _response(
            b"<h1>Making sure you're not a bot!</h1>",
            status=202,
            headers={"Content-Type": "text/html"},
        )

    async with _server(responder) as port:
        with pytest.raises(WebFetchError) as exc_info:
            await fetch_web(
                f"{_origin(port)}/challenge",
                public_addresses_only=False,
            )

    assert exc_info.value.code == "webfetch.requestFailed"
    assert exc_info.value.params == {"statusCode": 202}


async def test_dns_failures_have_a_stable_code(monkeypatch) -> None:
    real_loop = asyncio.get_running_loop()

    class FailingLoop:
        time = real_loop.time

        async def getaddrinfo(self, *args, **kwargs):
            raise socket.gaierror()

    monkeypatch.setattr(web_fetch.asyncio, "get_running_loop", FailingLoop)
    assert await _error_code(fetch_web("https://does-not-resolve.invalid")) == "webfetch.dnsFailed"


async def test_download_file_returns_raw_bytes_and_derives_the_filename() -> None:
    payload = _blank_pdf()

    async def responder(target: str, _headers: dict[str, str]) -> bytes:
        if target == "/redirect":
            return _response(status=302, headers={"Location": "/files/datasheet"})
        return _response(
            payload,
            headers={
                "Content-Type": "application/pdf",
                "Content-Disposition": 'attachment; filename="sheet.pdf"',
            },
        )

    async with _server(responder) as port:
        result = await web_fetch.download_file(
            f"{_origin(port)}/redirect",
            public_addresses_only=False,
        )

    assert result.data == payload
    assert result.content_type == "application/pdf"
    assert result.filename == "sheet.pdf"
    assert result.final_url.endswith("/files/datasheet")


async def test_download_file_names_files_from_the_url_when_no_header_is_sent() -> None:
    async def responder(_target: str, _headers: dict[str, str]) -> bytes:
        return _response(
            _blank_pdf(),
            headers={"Content-Type": "application/pdf"},
        )

    async with _server(responder) as port:
        named = await web_fetch.download_file(
            f"{_origin(port)}/docs/manual%20v2.pdf",
            public_addresses_only=False,
        )
        unnamed = await web_fetch.download_file(
            f"{_origin(port)}/",
            public_addresses_only=False,
        )

    assert named.filename == "manual v2.pdf"
    assert unnamed.filename == "download.pdf"


async def test_download_file_rejects_unsupported_types_and_oversized_bodies() -> None:
    async def responder(target: str, _headers: dict[str, str]) -> bytes:
        if target == "/page":
            return _response(b"<html></html>", headers={"Content-Type": "text/html"})
        return _response(_blank_pdf(), headers={"Content-Type": "application/pdf"})

    async with _server(responder) as port:
        unsupported = await _error_code(
            web_fetch.download_file(f"{_origin(port)}/page", public_addresses_only=False)
        )
        oversized = await _error_code(
            web_fetch.download_file(
                f"{_origin(port)}/sheet.pdf",
                public_addresses_only=False,
                max_response_bytes=16,
            )
        )

    assert unsupported == "webfetch.contentTypeUnsupported"
    assert oversized == "webfetch.responseTooLarge"


async def test_download_file_applies_the_same_ssrf_protection_as_fetch() -> None:
    async def responder(_target: str, _headers: dict[str, str]) -> bytes:
        return _response(_blank_pdf(), headers={"Content-Type": "application/pdf"})

    async with _server(responder) as port:
        blocked = await _error_code(web_fetch.download_file(f"{_origin(port)}/sheet.pdf"))

    assert blocked == "webfetch.addressBlocked"

async def test_download_file_resolves_opaque_types_by_extension() -> None:
    async def responder(target: str, _headers: dict[str, str]) -> bytes:
        if target.endswith(".pdf"):
            return _response(
                _blank_pdf(), headers={"Content-Type": "application/octet-stream"}
            )
        if target.endswith(".exe"):
            return _response(
                b"MZ binary", headers={"Content-Type": "application/octet-stream"}
            )
        if target == "/blob":
            return _response(
                b"payload", headers={"Content-Type": "application/octet-stream"}
            )
        return _response(
            b"payload",
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Disposition": 'attachment; filename="report.csv"',
            },
        )

    async with _server(responder) as port:
        from_path = await web_fetch.download_file(
            f"{_origin(port)}/files/sheet.pdf", public_addresses_only=False
        )
        from_header = await web_fetch.download_file(
            f"{_origin(port)}/download", public_addresses_only=False
        )
        rejected = await _error_code(
            web_fetch.download_file(
                f"{_origin(port)}/tool.exe", public_addresses_only=False
            )
        )
        unnamed = await _error_code(
            web_fetch.download_file(
                f"{_origin(port)}/blob", public_addresses_only=False
            )
        )

    assert from_path.content_type == "application/pdf"
    assert from_path.filename == "sheet.pdf"
    assert from_header.content_type == "text/csv"
    assert from_header.filename == "report.csv"
    assert rejected == "webfetch.contentTypeUnsupported"
    assert unnamed == "webfetch.contentTypeUnsupported"


async def test_opaque_downloads_still_verify_image_signatures() -> None:
    async def responder(_target: str, _headers: dict[str, str]) -> bytes:
        return _response(
            b"not really a png",
            headers={"Content-Type": "application/octet-stream"},
        )

    async with _server(responder) as port:
        code = await _error_code(
            web_fetch.download_file(
                f"{_origin(port)}/logo.png", public_addresses_only=False
            )
        )

    assert code == "webfetch.contentTypeMismatch"
