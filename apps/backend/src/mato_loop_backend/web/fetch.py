from __future__ import annotations

import asyncio
import codecs
import html
import ipaddress
import logging
import re
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from email.message import Message
from email.parser import BytesHeaderParser
from email.policy import HTTP
from pathlib import PurePosixPath
from typing import Literal
from urllib.parse import quote, unquote

from bs4 import BeautifulSoup
from bs4.element import Tag
from curl_cffi import AsyncSession, CurlECode, CurlOpt
from curl_cffi.curl import CURL_WRITEFUNC_ERROR
from curl_cffi.requests.exceptions import RequestException, SSLError
from curl_cffi.requests.exceptions import Timeout as CurlTimeout
from markdownify import markdownify
from yarl import URL

from mato_loop_backend.media.pdf import (
    PDF_VISION_BATCH_PAGES,
    PdfVisionHandler,
    render_pdf_page,
)
from mato_loop_backend.media.processing import (
    AttachmentProcessingError,
    extract_pdf_pages,
    format_pdf_pages,
)
from mato_loop_backend.settings.models import DEFAULT_WEB_ACCESS_USER_AGENT

logger = logging.getLogger("mato_loop_backend.integrations.webfetch")

MAX_RESPONSE_BYTES = 64 * 1024 * 1024
MAX_REQUEST_BYTES = 256 * 1024
MAX_REDIRECTS = 5
MAX_PDF_PAGE_IMAGES = 8
MAX_PDF_PAGE_IMAGE_BYTES = 20 * 1024 * 1024

_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_IMAGE_TYPES = {
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
_TEXT_TYPES = {"application/xhtml+xml", "text/html", "text/markdown", "text/x-markdown", "text/plain"}
_STRUCTURED_TEXT_TYPES = {
    "application/atom+xml",
    "application/json",
    "application/ndjson",
    "application/rss+xml",
    "application/x-ndjson",
    "application/xml",
    "text/csv",
    "text/xml",
}
_MEDIA_TYPE = re.compile(
    r"[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}"
)
_PDF_TYPE = "application/pdf"
_METADATA_ADDRESSES = {
    ipaddress.ip_address("100.100.100.200"),
    ipaddress.ip_address("169.254.169.254"),
    ipaddress.ip_address("169.254.170.2"),
    ipaddress.ip_address("fd00:ec2::254"),
}
_PRIVATE_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fec0::/10"),
)
_DROP_HTML_TAGS = {
    "base",
    "canvas",
    "embed",
    "iframe",
    "link",
    "math",
    "meta",
    "noscript",
    "object",
    "script",
    "style",
    "svg",
    "template",
}
_UNWRAP_HTML_TAGS = {"button", "fieldset", "form", "input", "option", "select", "textarea"}
_URL_ATTRIBUTES = {"action", "formaction", "href", "poster", "src", "xlink:href"}
_UNSAFE_URL_SCHEME = re.compile(r"^[\x00-\x20]*(?:data|javascript|vbscript):", re.IGNORECASE)
_CHARSET_PATTERN = re.compile(br"charset\s*=\s*['\"]?\s*([a-zA-Z0-9._:+-]+)", re.IGNORECASE)
_DEFAULT_ACCEPT = (
    "text/html, text/markdown, text/plain, application/pdf, "
    "image/png, image/jpeg, image/webp, image/gif, "
    + ", ".join(sorted(_STRUCTURED_TEXT_TYPES))
)
_PAGE_ACCEPT = {
    "markdown": "text/markdown;q=1.0, text/x-markdown;q=0.9, text/plain;q=0.8, text/html;q=0.7, */*;q=0.1",
    "text": "text/plain;q=1.0, text/markdown;q=0.9, text/html;q=0.8, */*;q=0.1",
    "html": "text/html;q=1.0, application/xhtml+xml;q=0.9, text/plain;q=0.8, text/markdown;q=0.7, */*;q=0.1",
}

DOWNLOAD_TYPES = {
    "application/gzip": ".gz",
    "application/json": ".json",
    "application/msword": ".doc",
    "application/pdf": ".pdf",
    "application/rtf": ".rtf",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.oasis.opendocument.spreadsheet": ".ods",
    "application/vnd.oasis.opendocument.text": ".odt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/zip": ".zip",
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/svg+xml": ".svg",
    "image/webp": ".webp",
    "text/csv": ".csv",
    "text/markdown": ".md",
    "text/plain": ".txt",
}
_OPAQUE_DOWNLOAD_TYPE = "application/octet-stream"
_DOWNLOAD_ACCEPT = ", ".join([*sorted(DOWNLOAD_TYPES), _OPAQUE_DOWNLOAD_TYPE])
_EXTENSION_TYPES = {
    extension: content_type for content_type, extension in DOWNLOAD_TYPES.items()
} | {".jpeg": "image/jpeg", ".markdown": "text/markdown", ".text": "text/plain"}


@dataclass(frozen=True, slots=True)
class DownloadedFile:
    final_url: str
    content_type: str
    filename: str
    data: bytes


@dataclass(frozen=True, slots=True)
class WebFetchBytesResult:
    final_url: str
    content_type: str
    charset: str | None
    status_code: int
    data: bytes


class WebFetchError(RuntimeError):
    def __init__(self, code: str, *, params: dict[str, object] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.params = params


@dataclass(frozen=True, slots=True)
class WebFetchImage:
    data: bytes
    mime_type: str
    filename: str
    page_number: int | None = None


@dataclass(frozen=True, slots=True)
class WebFetchResult:
    final_url: str
    content_type: str
    content: str
    status_code: int
    output_format: str
    title: str | None = None
    images: tuple[WebFetchImage, ...] = ()
    page_count: int | None = None
    unavailable_pages: tuple[int, ...] = ()
    links: tuple[tuple[str, str], ...] = ()
    image_links: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class _Target:
    url: URL
    host: str
    port: int
    addresses: tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]
    browsing_url: URL | None = None


@dataclass(frozen=True, slots=True)
class _Response:
    status: int
    location: str | None
    content_type: str
    charset: str | None
    content_disposition: str | None
    body: bytes


def _error(code: str, **params: object) -> WebFetchError:
    return WebFetchError(f"webfetch.{code}", params=params or None)


def _unsupported_content_type(content_type: str) -> WebFetchError:
    params = {"contentType": content_type} if _MEDIA_TYPE.fullmatch(content_type) else {}
    return _error("contentTypeUnsupported", **params)


def _canonical_host(host: str) -> str:
    if not host or any(character.isspace() or ord(character) < 32 for character in host):
        raise _error("invalidUrl")
    if "%" in host:
        raise _error("invalidUrl")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            canonical = host.rstrip(".").encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise _error("invalidUrl") from exc
        # libcurl interprets leading +/- as DNS-cache commands.
        if not canonical or canonical.startswith(("+", "-")):
            raise _error("invalidUrl") from None
        return canonical
    return str(address.ipv4_mapped or address) if address.version == 6 else str(address)


def _parse_url(value: str, *, origin: bool = False) -> tuple[URL, str, int]:
    if not isinstance(value, str) or not value:
        raise _error("invalidOrigin" if origin else "invalidUrl")
    try:
        url = URL(value)
    except (TypeError, ValueError) as exc:
        raise _error("invalidOrigin" if origin else "invalidUrl") from exc
    if url.scheme.lower() not in {"http", "https"}:
        raise _error("invalidOrigin" if origin else "unsupportedScheme")
    if url.user is not None or url.password is not None:
        raise _error("invalidOrigin" if origin else "userinfoForbidden")
    try:
        host_value = url.host
        port = url.port
    except ValueError as exc:
        raise _error("invalidOrigin" if origin else "invalidUrl") from exc
    if host_value is None or port is None:
        raise _error("invalidOrigin" if origin else "invalidUrl")
    host = _canonical_host(host_value)
    if origin and (url.path not in {"", "/"} or url.query_string or url.fragment):
        raise _error("invalidOrigin")
    try:
        normalized = url.with_scheme(url.scheme.lower()).with_host(host).with_fragment(None)
    except ValueError as exc:
        raise _error("invalidOrigin" if origin else "invalidUrl") from exc
    return normalized, host, port


def canonicalize_url(value: str, *, preserve_fragment: bool = False) -> str:
    url, _, _ = _parse_url(value)
    if url.path == "/":
        url = url.with_path("/", keep_query=True)
    fragment = quote(value.partition("#")[2], safe="!$&'()*+,-./:;=?@_~%") if preserve_fragment else ""
    return str(url) + ("#" + fragment if fragment else "")


def normalize_source_title(value: object, *, max_length: int = 300) -> str | None:
    if not isinstance(value, str):
        return None
    title = " ".join(value.split())
    if not title:
        return None
    return title[:max_length]


def _normalized_address(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    address = ipaddress.ip_address(value.split("%", 1)[0])
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def _address_is_public(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return not (
        address in _METADATA_ADDRESSES
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
        or address.is_loopback
        or any(
            address in network
            for network in _PRIVATE_NETWORKS
            if address.version == network.version
        )
        or address.is_reserved
        or not address.is_global
    )


def _target_is_private(target: _Target) -> bool:
    return any(not _address_is_public(address) for address in target.addresses)


async def _resolve_addresses(
    host: str, port: int
) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
    try:
        literal = _normalized_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        return (literal,)
    try:
        records = await asyncio.get_running_loop().getaddrinfo(
            host,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
        addresses = tuple(dict.fromkeys(_normalized_address(record[4][0]) for record in records))
    except (OSError, ValueError) as exc:
        raise _error("dnsFailed") from exc
    if not addresses:
        raise _error("dnsFailed")
    return addresses


async def _validate_target(
    value: str,
    public_addresses_only: bool,
) -> _Target:
    url, host, port = _parse_url(value)
    if url.path == "/":
        url = url.with_path("/", keep_query=True)
    addresses = await _resolve_addresses(host, port)
    if public_addresses_only and any(
        not _address_is_public(address) for address in addresses
    ):
        raise _error("addressBlocked")
    return _Target(url=url, host=host, port=port, addresses=addresses)


async def _request_once(
    target: _Target,
    timeout: float,
    max_response_bytes: int,
    user_agent: str,
    accept: str = _DEFAULT_ACCEPT,
    *,
    method: Literal["GET", "POST"] = "GET",
    body: bytes | None = None,
    request_content_type: str | None = None,
) -> _Response:
    status = 0
    header_buffer = bytearray()
    response_body = bytearray()
    too_large = False

    def receive_header(line: bytes) -> int:
        nonlocal status, too_large
        if line.startswith(b"HTTP/"):
            status = int(line.split()[1])
            header_buffer.clear()
        else:
            header_buffer.extend(line)
        if line.lower().startswith(b"content-length:") and 200 <= status < 300 and status != 202:
            try:
                too_large = int(line.partition(b":")[2]) > max_response_bytes
            except ValueError:
                return CURL_WRITEFUNC_ERROR
            if too_large:
                return CURL_WRITEFUNC_ERROR
        # Redirects and errors need headers only, never an external response body.
        if line == b"\r\n" and (status in {101, 202} or status >= 300):
            return CURL_WRITEFUNC_ERROR
        return len(line)

    def receive_body(chunk: bytes) -> int:
        nonlocal too_large
        if len(response_body) + len(chunk) > max_response_bytes:
            too_large = True
            return CURL_WRITEFUNC_ERROR
        response_body.extend(chunk)
        return len(chunk)

    host = f"[{target.host}]" if ":" in target.host else target.host
    addresses = ",".join(
        f"[{address}]" if address.version == 6 else str(address)
        for address in target.addresses
    )
    headers = {
        "Accept": accept,
        "Accept-Encoding": "gzip, deflate",
        "Connection": "close",
        "User-Agent": user_agent,
    }
    if request_content_type is not None:
        headers["Content-Type"] = request_content_type
    try:
        async with AsyncSession(
            impersonate="chrome",
            allow_redirects=False,
            discard_cookies=True,
            timeout=timeout,
            trust_env=False,
            curl_options={
                CurlOpt.RESOLVE: [f"{host}:{target.port}:{addresses}"],
                CurlOpt.HEADERFUNCTION: receive_header,
            },
        ) as session:
            await session.request(
                method, str(target.url), headers=headers, content=body,
                content_callback=receive_body, accept_encoding="gzip, deflate", quote=False,
            )
    except (TimeoutError, CurlTimeout) as exc:
        raise _error("timeout") from exc
    except SSLError as exc:
        logger.warning("Web fetch TLS verification failed for host %s", target.host)
        raise _error("tlsFailed") from exc
    except RequestException as exc:
        if exc.code == CurlECode.WRITE_ERROR and (status in {101, 202} or status >= 300):
            pass
        elif too_large:
            raise _error("responseTooLarge") from exc
        else:
            raise _error("connectionFailed") from exc
    except OSError as exc:
        raise _error("connectionFailed") from exc
    response_headers = BytesHeaderParser(policy=HTTP).parsebytes(bytes(header_buffer))
    return _Response(
        status=status,
        location=response_headers.get("Location"),
        content_type=response_headers.get("Content-Type", "").split(";", 1)[0].strip().lower(),
        charset=response_headers.get_content_charset(),
        content_disposition=response_headers.get("Content-Disposition"),
        body=bytes(response_body),
    )


def _decode_text(body: bytes, charset: str | None, *, html_content: bool) -> str:
    encoding = charset
    if not encoding and html_content:
        match = _CHARSET_PATTERN.search(body[:4096])
        encoding = match.group(1).decode("ascii") if match else None
    encoding = encoding or "utf-8"
    try:
        codecs.lookup(encoding)
        return body.decode(encoding, errors="replace")
    except (LookupError, UnicodeError) as exc:
        raise _error("decodeFailed") from exc


def _sanitize_html(value: str) -> BeautifulSoup:
    soup = BeautifulSoup(value, "html.parser")
    for tag in list(soup.find_all(_DROP_HTML_TAGS)):
        tag.decompose()
    for tag in list(soup.find_all(_UNWRAP_HTML_TAGS)):
        tag.unwrap()
    for tag in soup.find_all(True):
        for attribute in list(tag.attrs):
            lowered = attribute.lower()
            if lowered.startswith("on") or lowered in {"srcdoc", "style"}:
                del tag.attrs[attribute]
                continue
            if lowered in _URL_ATTRIBUTES:
                raw_value = tag.attrs[attribute]
                text_value = " ".join(raw_value) if isinstance(raw_value, list) else str(raw_value)
                if _UNSAFE_URL_SCHEME.match(html.unescape(text_value)):
                    del tag.attrs[attribute]
    return soup


def _absolutize_urls(soup: BeautifulSoup, base_url: URL) -> None:
    for tag in soup.find_all(True):
        for attribute in _URL_ATTRIBUTES.intersection(tag.attrs):
            raw_value = tag.attrs[attribute]
            if isinstance(raw_value, list):
                continue
            value = html.unescape(str(raw_value)).strip()
            if not value or value.startswith("#"):
                continue
            try:
                parsed = URL(value)
                if not parsed.scheme:
                    tag.attrs[attribute] = str(base_url.join(parsed))
            except (TypeError, ValueError):
                continue


def _content_root(soup: BeautifulSoup) -> Tag | BeautifulSoup:
    root = (
        soup.find("main")
        or soup.find(attrs={"role": "main"})
        or soup.find("article")
        or soup.body
        or soup
    )
    if root is soup.body or root is soup:
        for tag in list(root.find_all(["nav", "footer"])):
            tag.decompose()
        for tag in list(root.find_all(attrs={"role": "navigation"})):
            tag.decompose()
    return root


def _normalize_lines(value: str) -> str:
    lines = [line.rstrip() for line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    result: list[str] = []
    blank = False
    for line in lines:
        if line.strip():
            result.append(line)
            blank = False
        elif result and not blank:
            result.append("")
            blank = True
    return "\n".join(result).strip()


def _convert_text(
    body: bytes,
    content_type: str,
    format: str,
    charset: str | None,
    base_url: URL,
    *,
    preserve_text: bool = False,
) -> str:
    is_html = content_type in {"application/xhtml+xml", "text/html"}
    value = _decode_text(body, charset, html_content=is_html)
    if is_html:
        soup = _sanitize_html(value)
        _absolutize_urls(soup, base_url)
        if format == "html":
            return str(soup)
        root = _content_root(soup)
        if format == "text":
            plain_text = root.get_text(" ", strip=True)
            return re.sub(r"\s+([,.;:!?])", r"\1", plain_text)
        return _normalize_lines(markdownify(str(root), heading_style="ATX"))
    if format == "html":
        return f"<pre>{html.escape(value)}</pre>"
    if preserve_text:
        return value
    if content_type in {"text/markdown", "text/x-markdown"}:
        soup = _sanitize_html(value)
        if format == "text":
            return _normalize_lines(soup.get_text())
        return _normalize_lines(str(soup))
    if format == "markdown":
        return _normalize_lines(html.escape(value, quote=False))
    return _normalize_lines(value)


def _html_title(body: bytes, content_type: str, charset: str | None) -> str | None:
    if content_type not in {"application/xhtml+xml", "text/html"}:
        return None
    value = _decode_text(body, charset, html_content=True)
    soup = BeautifulSoup(value, "html.parser")
    title_tag = soup.head.find("title") if soup.head is not None else None
    if title_tag is None:
        return None
    return normalize_source_title(title_tag.get_text(" ", strip=True))


def _text_content_and_title(
    response: _Response, format: str, base_url: URL
) -> tuple[str, str | None]:
    return (
        _convert_text(
            response.body,
            response.content_type,
            format,
            response.charset,
            base_url,
        ),
        _html_title(response.body, response.content_type, response.charset),
    )


def _valid_image_signature(content_type: str, body: bytes) -> bool:
    if content_type == "image/png":
        return body.startswith(b"\x89PNG\r\n\x1a\n")
    if content_type == "image/jpeg":
        return body.startswith(b"\xff\xd8\xff")
    if content_type == "image/gif":
        return body.startswith((b"GIF87a", b"GIF89a"))
    if content_type == "image/webp":
        return len(body) >= 12 and body.startswith(b"RIFF") and body[8:12] == b"WEBP"
    return False


def _render_pdf_page(data: bytes, page_number: int) -> bytes:
    try:
        return render_pdf_page(data, page_number)
    except AttachmentProcessingError as exc:
        raise _error("pdfRenderFailed") from exc


async def _convert_pdf(
    body: bytes,
    vision_handler: PdfVisionHandler | None,
    *,
    private_content: bool,
    requested_pages: tuple[int, ...],
) -> tuple[str, int, tuple[tuple[int, bytes], ...], tuple[int, ...]]:
    if b"%PDF-" not in body[:1_024]:
        raise _error("contentTypeMismatch")
    try:
        pages, metadata = await asyncio.to_thread(extract_pdf_pages, body)
    except AttachmentProcessingError as exc:
        raise _error("pdfInvalid") from exc
    missing = [
        number
        for number in metadata.get("imagePages", [])
        if isinstance(number, int) and 1 <= number <= len(pages)
    ]
    rendered_pages: dict[int, bytes] = {}

    async def render(number: int) -> bytes:
        image = rendered_pages.get(number)
        if image is None:
            image = await asyncio.to_thread(_render_pdf_page, body, number)
            rendered_pages[number] = image
        return image

    if missing and vision_handler is None:
        raise _error("pdfVisionUnavailable")
    for offset in range(0, len(missing), PDF_VISION_BATCH_PAGES):
        batch_numbers = missing[offset : offset + PDF_VISION_BATCH_PAGES]
        rendered = [(number, await render(number)) for number in batch_numbers]
        assert vision_handler is not None
        extracted = await vision_handler(rendered, private_content)
        if set(extracted) != set(batch_numbers) or any(
            not isinstance(text, str) for text in extracted.values()
        ):
            raise _error("pdfVisionFailed")
        for number, text in extracted.items():
            pages[number - 1] = text
        try:
            format_pdf_pages(pages)
        except AttachmentProcessingError as exc:
            raise _error("pdfInvalid") from exc
    available_pages = [number for number in requested_pages if number <= len(pages)]
    unavailable_pages = [number for number in requested_pages if number > len(pages)]
    selected: list[tuple[int, bytes]] = []
    selected_bytes = 0
    for number in available_pages:
        image = await render(number)
        if selected_bytes + len(image) > MAX_PDF_PAGE_IMAGE_BYTES:
            unavailable_pages.append(number)
            continue
        selected.append((number, image))
        selected_bytes += len(image)
    try:
        return (
            format_pdf_pages(pages),
            len(pages),
            tuple(selected),
            tuple(sorted(unavailable_pages)),
        )
    except AttachmentProcessingError as exc:
        raise _error("pdfInvalid") from exc


def _image_filename(url: URL, content_disposition: str | None, content_type: str) -> str:
    filename: str | None = None
    if content_disposition:
        message = Message()
        message["Content-Disposition"] = content_disposition
        filename = message.get_filename()
    if not filename:
        filename = unquote(PurePosixPath(url.path).name)
    filename = re.sub(r"[\x00-\x1f\x7f]", "", (filename or "").replace("\\", "/"))
    filename = PurePosixPath(filename).name.strip(" .")[:240]
    extension = _IMAGE_TYPES[content_type]
    allowed_extensions = {extension, ".jpeg"} if content_type == "image/jpeg" else {extension}
    suffix = PurePosixPath(filename).suffix.lower()
    if not filename:
        filename = f"image{extension}"
    elif suffix not in allowed_extensions:
        filename = f"{PurePosixPath(filename).stem or 'image'}{extension}"
    return filename


def _pdf_page_filename(url: URL, page_number: int) -> str:
    stem = PurePosixPath(unquote(url.path)).stem
    stem = re.sub(r"[\x00-\x1f\x7f]", "", stem.replace("\\", "/"))
    stem = PurePosixPath(stem).name.strip(" .")[:220] or "document"
    return f"{stem}-page-{page_number}.jpg"


async def _build_result(
    target: _Target,
    response: _Response,
    format: str,
    vision_handler: PdfVisionHandler | None,
    pdf_pages: tuple[int, ...],
) -> WebFetchResult:
    if response.content_type in _IMAGE_TYPES:
        if not _valid_image_signature(response.content_type, response.body):
            raise _error("contentTypeMismatch")
        return WebFetchResult(
            final_url=str(target.url),
            content_type=response.content_type,
            content="",
            status_code=response.status,
            output_format="image",
            images=(
                WebFetchImage(
                    response.body,
                    response.content_type,
                    _image_filename(
                        target.url, response.content_disposition, response.content_type
                    ),
                ),
            ),
        )
    if response.content_type == _PDF_TYPE:
        content, page_count, rendered, unavailable_pages = await _convert_pdf(
            response.body,
            vision_handler,
            private_content=_target_is_private(target),
            requested_pages=pdf_pages,
        )
        return WebFetchResult(
            final_url=str(target.url),
            content_type=response.content_type,
            content=content,
            status_code=response.status,
            output_format="text",
            images=tuple(
                WebFetchImage(
                    image,
                    "image/jpeg",
                    _pdf_page_filename(target.url, page_number),
                    page_number,
                )
                for page_number, image in rendered
            ),
            page_count=page_count,
            unavailable_pages=unavailable_pages,
        )
    if response.content_type in _STRUCTURED_TEXT_TYPES or (
        response.content_type.startswith("application/")
        and response.content_type.endswith("+json")
        and _MEDIA_TYPE.fullmatch(response.content_type)
    ):
        # Preserve structured data as text; parsing or prose conversion can change values.
        format = "html" if format == "html" else "text"
        content = await asyncio.to_thread(
            _convert_text, response.body, response.content_type, format,
            response.charset, target.url, preserve_text=True,
        )
        return WebFetchResult(
            final_url=str(target.url),
            content_type=response.content_type,
            content=content,
            status_code=response.status,
            output_format=format,
        )
    if response.content_type not in _TEXT_TYPES:
        raise _unsupported_content_type(response.content_type)
    content, title = await asyncio.to_thread(
        _text_content_and_title, response, format, target.url
    )
    return WebFetchResult(
        final_url=str(target.browsing_url or target.url) if response.content_type in {"text/html", "application/xhtml+xml"} else str(target.url),
        content_type=response.content_type,
        content=content,
        status_code=response.status,
        output_format=format,
        title=title,
    )


async def _follow_redirects(
    url: str,
    timeout: float,
    public_addresses_only: bool,
    authorize_url: Callable[[str], Awaitable[None]] | None,
    max_response_bytes: int,
    user_agent: str,
    accept: str,
    *,
    method: Literal["GET", "POST"] = "GET",
    body: bytes | None = None,
    request_content_type: str | None = None,
) -> tuple[_Target, _Response, float]:
    loop = asyncio.get_running_loop()
    remaining = float(timeout)
    current_url = url
    for redirect_count in range(MAX_REDIRECTS + 1):
        canonical_url = canonicalize_url(current_url)
        fragment = current_url.partition("#")[2] if "#" in current_url else None
        browsing_url = URL(canonical_url + ("#" + fragment if fragment is not None else ""), encoded=True)
        current_url = canonical_url
        started = loop.time()
        async with asyncio.timeout(max(remaining, 0.001)):
            target = await _validate_target(current_url, public_addresses_only)
        remaining -= loop.time() - started
        if authorize_url is not None:
            await authorize_url(current_url)
            started = loop.time()
            async with asyncio.timeout(max(remaining, 0.001)):
                target = await _validate_target(current_url, public_addresses_only)
            remaining -= loop.time() - started
        started = loop.time()
        async with asyncio.timeout(max(remaining, 0.001)):
            response = await _request_once(
                target,
                remaining,
                max_response_bytes,
                user_agent,
                accept,
                method=method,
                body=body,
                request_content_type=request_content_type,
            )
        remaining -= loop.time() - started
        if response.status in _REDIRECT_STATUSES:
            if method == "POST":
                raise _error("requestFailed", statusCode=response.status)
            if not response.location:
                raise _error("redirectInvalid")
            if redirect_count == MAX_REDIRECTS:
                raise _error("tooManyRedirects")
            try:
                current_url = str(target.url.join(URL(response.location)))
                if "#" not in response.location and fragment is not None:
                    current_url += "#" + fragment
            except (TypeError, ValueError) as exc:
                raise _error("redirectInvalid") from exc
            continue
        if response.status == 202 or not 200 <= response.status < 300:
            raise _error("requestFailed", statusCode=response.status)
        return replace(target, browsing_url=browsing_url), response, remaining
    raise _error("tooManyRedirects")


def _validate_fetch_limits(timeout: float, max_response_bytes: int) -> None:
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 120:
        raise _error("timeoutInvalid")
    if (
        isinstance(max_response_bytes, bool)
        or not isinstance(max_response_bytes, int)
        or max_response_bytes < 1
    ):
        raise _error("responseLimitInvalid")


def _validate_header_value(value: str) -> None:
    if not value or any(not 32 <= ord(character) <= 126 for character in value):
        raise _error("invalidUrl")


async def request_web_bytes(
    url: str,
    *,
    method: Literal["GET", "POST"] = "GET",
    body: bytes | None = None,
    request_content_type: str | None = None,
    accept: str = "*/*",
    timeout: float = 30,
    public_addresses_only: bool = True,
    authorize_url: Callable[[str], Awaitable[None]] | None = None,
    max_response_bytes: int = MAX_RESPONSE_BYTES,
    user_agent: str = DEFAULT_WEB_ACCESS_USER_AGENT,
) -> WebFetchBytesResult:
    """Return bounded raw bytes through the webfetch security pipeline."""
    _validate_fetch_limits(timeout, max_response_bytes)
    if method not in {"GET", "POST"}:
        raise _error("requestFailed")
    if body is not None and not isinstance(body, bytes):
        raise _error("requestFailed")
    if method == "GET" and (body is not None or request_content_type is not None):
        raise _error("requestFailed")
    if method == "POST" and (body is None or request_content_type is None):
        raise _error("requestFailed")
    if body is not None and len(body) > MAX_REQUEST_BYTES:
        raise _error("requestTooLarge")
    _validate_header_value(accept)
    if request_content_type is not None:
        _validate_header_value(request_content_type)
    try:
        target, response, _remaining = await _follow_redirects(
            url,
            timeout,
            public_addresses_only,
            authorize_url,
            max_response_bytes,
            user_agent,
            accept,
            method=method,
            body=body,
            request_content_type=request_content_type,
        )
    except WebFetchError:
        raise
    except TimeoutError as exc:
        raise _error("timeout") from exc
    return WebFetchBytesResult(
        final_url=str(target.url),
        content_type=response.content_type,
        charset=response.charset,
        status_code=response.status,
        data=response.body,
    )


def _raw_download_name(url: URL, content_disposition: str | None) -> str:
    filename: str | None = None
    if content_disposition:
        message = Message()
        message["Content-Disposition"] = content_disposition
        filename = message.get_filename()
    if not filename:
        filename = unquote(PurePosixPath(url.path).name)
    filename = re.sub(r"[\x00-\x1f\x7f]", "", (filename or "").replace("\\", "/"))
    return PurePosixPath(filename).name.strip(" .")[:240]


def _download_filename(
    url: URL, content_disposition: str | None, content_type: str
) -> str:
    filename = _raw_download_name(url, content_disposition)
    extension = DOWNLOAD_TYPES[content_type]
    if not filename:
        return f"download{extension}"
    if PurePosixPath(filename).suffix:
        return filename
    return f"{filename}{extension}"


def _resolve_download_type(
    content_type: str, url: URL, content_disposition: str | None
) -> str:
    """Resolve the effective type of a download.

    Servers routinely label documents as application/octet-stream. The declared
    type alone is then useless, so the extension decides, and it must still name
    a supported type. That keeps the allow list intact instead of turning the
    opaque type into a way to fetch anything.
    """
    if content_type in DOWNLOAD_TYPES:
        return content_type
    if content_type != _OPAQUE_DOWNLOAD_TYPE:
        raise _unsupported_content_type(content_type)
    suffix = PurePosixPath(_raw_download_name(url, content_disposition)).suffix.lower()
    resolved = _EXTENSION_TYPES.get(suffix)
    if resolved is None:
        raise _unsupported_content_type(content_type)
    return resolved


async def download_file(
    url: str,
    timeout: float = 30,
    public_addresses_only: bool = True,
    authorize_url: Callable[[str], Awaitable[None]] | None = None,
    max_response_bytes: int = MAX_RESPONSE_BYTES,
    user_agent: str = DEFAULT_WEB_ACCESS_USER_AGENT,
) -> DownloadedFile:
    """Fetch a document by URL and return its raw bytes.

    Applies the same SSRF, redirect, and size protections as fetch_web, but
    keeps the payload untouched so it can be handed to another system.
    """
    _validate_fetch_limits(timeout, max_response_bytes)
    try:
        target, response, _remaining = await _follow_redirects(
            url,
            timeout,
            public_addresses_only,
            authorize_url,
            max_response_bytes,
            user_agent,
            _DOWNLOAD_ACCEPT,
        )
    except WebFetchError:
        raise
    except TimeoutError as exc:
        raise _error("timeout") from exc
    content_type = _resolve_download_type(
        response.content_type, target.url, response.content_disposition
    )
    if content_type in _IMAGE_TYPES and not _valid_image_signature(
        content_type, response.body
    ):
        raise _error("contentTypeMismatch")
    if not response.body:
        raise _error("contentTypeMismatch")
    return DownloadedFile(
        final_url=str(target.url),
        content_type=content_type,
        filename=_download_filename(
            target.url, response.content_disposition, content_type
        ),
        data=response.body,
    )


async def fetch_web(
    url: str,
    format: str = "markdown",
    timeout: float = 30,
    public_addresses_only: bool = True,
    authorize_url: Callable[[str], Awaitable[None]] | None = None,
    max_response_bytes: int = MAX_RESPONSE_BYTES,
    pdf_vision_handler: PdfVisionHandler | None = None,
    pdf_pages: tuple[int, ...] | list[int] = (),
    user_agent: str = DEFAULT_WEB_ACCESS_USER_AGENT,
) -> WebFetchResult:
    if format not in {"html", "markdown", "text"}:
        raise _error("formatUnsupported")
    if (
        len(pdf_pages) > MAX_PDF_PAGE_IMAGES
        or any(
            isinstance(number, bool) or not isinstance(number, int) or number < 1
            for number in pdf_pages
        )
    ):
        raise ValueError("pdf_pages must contain at most eight positive integers")
    normalized_pdf_pages = tuple(sorted(set(pdf_pages)))
    _validate_fetch_limits(timeout, max_response_bytes)
    try:
        target, response, remaining = await _follow_redirects(
            url,
            timeout,
            public_addresses_only,
            authorize_url,
            max_response_bytes,
            user_agent,
            _PAGE_ACCEPT[format],
        )
        try:
            async with asyncio.timeout(max(remaining, 0.001)):
                result = await _build_result(
                    target,
                    response,
                    format,
                    pdf_vision_handler,
                    normalized_pdf_pages,
                )
            return result
        except WebFetchError as exc:
            exc.params = {"statusCode": response.status, **(exc.params or {})}
            raise
    except WebFetchError:
        raise
    except TimeoutError as exc:
        raise _error("timeout") from exc
