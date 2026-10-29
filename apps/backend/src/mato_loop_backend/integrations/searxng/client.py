from __future__ import annotations

import json
from typing import Literal

import httpx

from mato_loop_backend.integrations.search import (
    MAX_SEARCH_RESPONSE_BYTES,
    SearchIntegrationError,
    _compact_results,
)
from mato_loop_backend.settings.models import IntegrationInstance

_RECOVERY_ENGINES = "bing,mojeek,presearch"


def _http_error_code(status_code: int) -> str:
    if status_code in {401, 403}:
        return "integration.authenticationFailed"
    if status_code == 404:
        return "integration.endpointNotFound"
    if status_code == 429:
        return "integration.rateLimited"
    if status_code == 408 or status_code >= 500:
        return "integration.unavailable"
    return "integration.requestFailed"


async def search_with_provider(
    provider: IntegrationInstance,
    query: str,
    *,
    search_type: Literal["web", "images"] = "web",
) -> str:
    if provider.integration_type != "search" or provider.adapter_type != "searxng":
        raise SearchIntegrationError("integration.providerUnsupported")
    base_url = str(provider.config.get("base_url") or "").rstrip("/")
    if not base_url:
        raise SearchIntegrationError("integration.configurationInvalid")

    async def request(
        client: httpx.AsyncClient, params: dict[str, str]
    ) -> tuple[list[object], list[object]]:
        async with client.stream(
            "GET",
            f"{base_url}/search",
            params=params,
            headers={"Accept-Encoding": "identity"},
        ) as response:
            if response.status_code >= 400:
                raise SearchIntegrationError(_http_error_code(response.status_code))
            if response.headers.get("Content-Encoding", "identity").lower() not in {
                "",
                "identity",
            }:
                raise SearchIntegrationError("integration.invalidResponse")
            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                try:
                    if int(content_length) > MAX_SEARCH_RESPONSE_BYTES:
                        raise SearchIntegrationError("integration.invalidResponse")
                except ValueError:
                    pass
            body = bytearray()
            async for chunk in response.aiter_raw():
                if len(body) + len(chunk) > MAX_SEARCH_RESPONSE_BYTES:
                    raise SearchIntegrationError("integration.invalidResponse")
                body.extend(chunk)
        try:
            payload = json.loads(body)
        except (TypeError, ValueError) as exc:
            raise SearchIntegrationError("integration.requestFailed") from exc
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            raise SearchIntegrationError("integration.requestFailed")
        unresponsive = payload.get("unresponsive_engines", [])
        if not isinstance(unresponsive, list):
            unresponsive = []
        return results, list(unresponsive)

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            params = {"q": query, "format": "json"}
            if search_type == "images":
                params["categories"] = "images"
            results, unresponsive_engines = await request(client, params)
            if not results and unresponsive_engines and search_type == "web":
                results, recovery_unresponsive = await request(
                    client,
                    {
                        "q": query,
                        "format": "json",
                        "engines": _RECOVERY_ENGINES,
                    },
                )
                unresponsive_engines.extend(recovery_unresponsive)
    except SearchIntegrationError:
        raise
    except httpx.HTTPError as exc:
        raise SearchIntegrationError("integration.unavailable") from exc
    if not results and unresponsive_engines:
        raise SearchIntegrationError("integration.unavailable")
    return _compact_results(results, search_type=search_type)
