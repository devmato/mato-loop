from __future__ import annotations

import asyncio
import logging
import re

import httpx
from pydantic import BaseModel, Field, ValidationError
from yarl import URL

from mato_loop_backend.integrations.jina.adapter import JinaConfig, JinaCookie
from mato_loop_backend.settings.models import DEFAULT_WEB_ACCESS_USER_AGENT
from mato_loop_backend.web.fetch import (
    MAX_RESPONSE_BYTES,
    WebFetchError,
    WebFetchResult,
    _convert_text,
    canonicalize_url,
    normalize_source_title,
)

logger = logging.getLogger(__name__)


class _Page(BaseModel):
    url: str
    title: str | None = None
    content: str = ""
    html: str | None = None
    text: str | None = None
    page_count: int | None = Field(default=None, ge=1, alias="numPages")
    http_status: int | None = Field(default=None, ge=100, le=599, alias="httpStatus")
    warning: str | None = None
    links: dict[str, str] = Field(default_factory=dict)
    images: dict[str, str] = Field(default_factory=dict)


class _Response(BaseModel):
    code: int = Field(ge=100, le=599)
    data: _Page


def _links(values: dict[str, str]) -> tuple[tuple[str, str], ...]:
    result = []
    for label, url in values.items():
        try:
            result.append((label, canonicalize_url(url, preserve_fragment=True)))
        except WebFetchError:
            continue
    return tuple(result)


async def fetch(
    config: JinaConfig, url: str, *, api_key: str | None = None,
    format: str = "markdown", timeout: float | None = None,
    max_response_bytes: int = MAX_RESPONSE_BYTES, user_agent: str = DEFAULT_WEB_ACCESS_USER_AGENT,
    locale: str | None = None, cookies: list[JinaCookie] | None = None,
    engine: str = "auto", remove_overlay: bool | None = None, no_cache: bool = False,
    target_selector: str | None = None, wait_for_selector: str | None = None,
    remove_selector: str | None = None,
    with_links_summary: bool = False, with_images_summary: bool = False,
) -> WebFetchResult:
    url = canonicalize_url(url, preserve_fragment=True)
    timeout = timeout if timeout is not None else config.timeout_seconds
    headers = {
        "Accept": "application/json", "Accept-Encoding": "identity", "DNT": "1",
        "X-Respond-With": format, "X-Timeout": str(max(1, timeout - 10)),
        "X-User-Agent": user_agent, "X-Engine": engine,
        "X-Remove-Overlay": str(config.remove_overlay if remove_overlay is None else remove_overlay).lower(),
    }
    if api_key is not None:
        headers["Authorization"] = f"Bearer {api_key}"
    for name, enabled in (("X-No-Cache", no_cache), ("X-With-Links-Summary", with_links_summary), ("X-With-Images-Summary", with_images_summary)):
        if enabled:
            headers[name] = "true"
    if locale is not None:
        headers["X-Locale"] = locale
    for name, value in (("X-Target-Selector", target_selector), ("X-Wait-For-Selector", wait_for_selector), ("X-Remove-Selector", remove_selector)):
        if value is not None:
            # CSS escapes preserve non-ASCII selectors in HTTP's ASCII header representation.
            headers[name] = "".join(char if ord(char) < 128 else f"\\{ord(char):x} " for char in value)
    # Send host-only cookies; Reader's browser scopes them to the requested HTTPS host.
    if URL(url).scheme == "https":
        selected = [cookie for cookie in cookies or [] if cookie.domain == URL(url).host]
        if selected:
            if any(not cookie.reader_value_supported for cookie in selected):
                raise WebFetchError("integration.invalidConfiguration")
            if engine == "curl":
                raise WebFetchError("tool.invalidArguments")
            headers["X-Engine"] = "browser"
            headers["X-No-Cache"] = "true"
            headers["X-Set-Cookie"] = ", ".join(f"{cookie.name}={cookie.value}; Path=/; Secure" for cookie in selected)
    try:
        async with asyncio.timeout(timeout):
            async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
                async with client.stream(
                    "POST", str(config.base_url).rstrip("/") + "/",
                    headers=headers,
                    json={"url": url},
                ) as response:
                    if response.status_code != 200:
                        code = {
                            401: "integration.authenticationFailed",
                            403: "integration.authenticationFailed",
                            429: "integration.rateLimited",
                            413: "webfetch.responseTooLarge",
                        }.get(response.status_code, "integration.requestFailed")
                        raise WebFetchError(code, params={"statusCode": response.status_code})
                    if response.headers.get("Content-Encoding", "identity").lower() not in {"", "identity"}:
                        raise WebFetchError("integration.invalidResponse")
                    length = response.headers.get("Content-Length")
                    if length is not None and int(length) > max_response_bytes:
                        raise WebFetchError("webfetch.responseTooLarge")
                    body = bytearray()
                    async for chunk in response.aiter_raw():
                        if len(body) + len(chunk) > max_response_bytes:
                            raise WebFetchError("webfetch.responseTooLarge")
                        body.extend(chunk)
            payload = _Response.model_validate_json(body)
            if payload.code != 200:
                raise WebFetchError("integration.requestFailed")
            warning_status = re.search(
                r"^Target URL returned error ([1-5][0-9]{2}):", payload.data.warning or "", re.MULTILINE,
            )
            status = payload.data.http_status or (int(warning_status[1]) if warning_status else payload.code)
            if status == 202 or not 200 <= status < 300:
                raise WebFetchError("webfetch.requestFailed", params={"statusCode": status})
            page = payload.data
            final_url = canonicalize_url(page.url, preserve_fragment=True)
            content_type = {"markdown": "text/markdown", "html": "text/html", "text": "text/plain"}[format]
            content = page.html if format == "html" else page.text if format == "text" else None
            content = await asyncio.to_thread(
                _convert_text, (content if content is not None else page.content).encode(),
                content_type, format, "utf-8", URL(final_url),
            )
            return WebFetchResult(
                final_url=final_url, content_type=content_type, content=content,
                status_code=status, output_format=format,
                title=normalize_source_title(page.title), page_count=page.page_count,
                links=_links(page.links) if with_links_summary else (),
                image_links=_links(page.images) if with_images_summary else (),
            )
    except WebFetchError:
        raise
    except (TimeoutError, httpx.TimeoutException) as exc:
        raise WebFetchError("webfetch.timeout") from exc
    except (httpx.HTTPError, ValidationError, ValueError) as exc:
        logger.warning("Jina Reader failed with %s", type(exc).__name__)
        raise WebFetchError("integration.invalidResponse" if isinstance(exc, ValueError) else "integration.unavailable") from exc
