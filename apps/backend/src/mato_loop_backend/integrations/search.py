from __future__ import annotations

import asyncio
import html
import json
import logging
from collections.abc import Iterable
from typing import Literal

import httpx
from bs4 import BeautifulSoup
from ddgs import DDGS
from ddgs.exceptions import DDGSException
from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend.integrations.registry import (
    effective_integration_contributions,
)
from mato_loop_backend.integrations.searxng.adapter import search_provider_key
from mato_loop_backend.settings.models import (
    IntegrationInstance,
    IntegrationSettings,
)
from mato_loop_backend.web.evidence import evidence_source_id
from mato_loop_backend.web.fetch import WebFetchError, canonicalize_url, normalize_source_title

logger = logging.getLogger(__name__)
MAX_SEARCH_RESPONSE_BYTES = 256 * 1024
MAX_SEARCH_SNIPPET_CHARACTERS = 2_000
SEARCH_DEADLINE_SECONDS = 35


class SearchIntegrationError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def search_available(_settings: IntegrationSettings) -> bool:
    return True


def search_provider_options(settings: IntegrationSettings) -> list[dict[str, str | None]]:
    options: list[dict[str, str | None]] = [
        {
            "value": "no_key",
            "name": "Websuche ohne API-Key",
            "adapterType": "ddgs",
            "integrationId": None,
        }
    ]
    options.extend(
        {
            "value": contribution.value,
            "name": contribution.label,
            "adapterType": contribution.value.split(":", 1)[0],
            "integrationId": contribution.integration_id,
        }
        for contribution in effective_integration_contributions(settings).tool_options
        if contribution.tool_name == "web_search" and contribution.option_name == "provider"
    )
    return options


def search_provider(
    settings: IntegrationSettings, provider_key: str
) -> IntegrationInstance | None:
    if provider_key == "no_key":
        return None
    return next(
        (
            instance
            for instance in settings.instances
            if instance.enabled
            and instance.integration_type == "search"
            and (
                instance.adapter_type == "searxng" and instance.config.get("base_url")
                or instance.adapter_type == "webiq_search"
                and instance.owner_id is None and instance.credential_id is not None
            )
            and settings.availability_for(instance.adapter_type) != "disabled"
            and search_provider_key(instance) == provider_key
        ),
        None,
    )


def _public_result_url(value: object) -> str | None:
    try:
        url = httpx.URL(str(value))
    except (TypeError, ValueError):
        return None
    if url.scheme not in {"http", "https"} or not url.host or url.userinfo:
        return None
    return str(url)


def _normalize_snippet(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = (
        BeautifulSoup(value, "html.parser").get_text(" ", strip=True)
        if "<" in value
        else value
    )
    normalized = " ".join(html.unescape(text).split())
    return normalized[:MAX_SEARCH_SNIPPET_CHARACTERS] or None


def _normalize_published_date(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    return normalized[:100] or None


def _compact_results(
    results: Iterable[object], *, search_type: Literal["web", "images"] = "web"
) -> str:
    compact = []
    seen_urls: set[str] = set()
    for item in results:
        if not isinstance(item, dict):
            continue
        url = _public_result_url(item.get("url") or item.get("href"))
        if url is None:
            continue
        try:
            url = canonicalize_url(url)
        except WebFetchError:
            continue
        image_url = None
        if search_type == "images":
            image_url = _public_result_url(item.get("image") or item.get("img_src"))
            if image_url is None:
                continue
            try:
                image_url = canonicalize_url(image_url)
            except WebFetchError:
                continue
        if url in seen_urls:
            continue
        seen_urls.add(url)
        compact.append(
            {
                "sourceId": evidence_source_id(url),
                "title": normalize_source_title(item.get("title")),
                "url": url,
                "snippet": _normalize_snippet(item.get("content") or item.get("body")),
                "publishedDate": _normalize_published_date(
                    item.get("publishedDate") or item.get("pubdate") or item.get("date")
                ),
                **({"imageUrl": image_url} if image_url is not None else {}),
            }
        )
        if len(compact) == 8:
            break
    return json.dumps(compact, ensure_ascii=False)


async def search_without_api_key(
    query: str, *, search_type: Literal["web", "images"] = "web", timeout: float = 35,
) -> str:
    def execute() -> list[dict[str, str]]:
        client = DDGS(timeout=min(30, timeout))
        search = client.images if search_type == "images" else client.text
        try:
            return search(
                query=query,
                max_results=8,
            )
        except DDGSException as exc:
            if str(exc) != "No results found.":
                raise
            return []

    try:
        async with asyncio.timeout(timeout):
            results = await asyncio.to_thread(execute)
    except Exception as exc:
        logger.warning("No-key web search failed: %s", type(exc).__name__)
        raise SearchIntegrationError("integration.unavailable") from exc
    return _compact_results(results, search_type=search_type)


async def search_with_key(
    settings: IntegrationSettings,
    provider_key: str,
    query: str,
    *,
    search_type: Literal["web", "images"] = "web",
    db: AsyncSession | None = None,
    timeout: float | None = None,
) -> str:
    if provider_key == "no_key":
        return await search_without_api_key(query, search_type=search_type, timeout=timeout or SEARCH_DEADLINE_SECONDS)
    provider = search_provider(settings, provider_key)
    if provider is None:
        raise SearchIntegrationError("integration.providerUnavailable")
    if provider.adapter_type == "webiq_search":
        from mato_loop_backend.integrations.service import IntegrationSettingsError
        from mato_loop_backend.integrations.webiq import client, service
        from mato_loop_backend.integrations.webiq.adapter import WebIqConfig

        if db is None:
            raise SearchIntegrationError("integration.invalidConfiguration")
        try:
            secrets = await service.credentials(db, provider)
        except IntegrationSettingsError as exc:
            raise SearchIntegrationError(exc.code) from exc
        results = await client.search(
            WebIqConfig.model_validate(provider.config), query,
            api_key=secrets.api_key, search_type=search_type,
        )
        return _compact_results(results, search_type=search_type)
    from mato_loop_backend.integrations.searxng.client import search_with_provider

    return await search_with_provider(provider, query, search_type=search_type)


async def search_web(
    settings: IntegrationSettings,
    query: str,
    *,
    search_type: Literal["web", "images"] = "web",
    provider: str = "no_key",
    db: AsyncSession | None = None,
    timeout: float | None = None,
) -> str:
    deadline = SEARCH_DEADLINE_SECONDS if timeout is None else min(timeout, SEARCH_DEADLINE_SECONDS)
    try:
        async with asyncio.timeout(deadline):
            return await search_with_key(
                settings, provider, query, search_type=search_type, db=db, timeout=deadline,
            )
    except TimeoutError as exc:
        raise SearchIntegrationError("integration.unavailable") from exc
