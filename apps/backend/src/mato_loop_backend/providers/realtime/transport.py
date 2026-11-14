from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urljoin, urlsplit

import aiohttp
import httpx
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, TypeAdapter

from mato_loop_backend.providers.base import ProviderError

logger = logging.getLogger(__name__)

RealtimeAdapter = Literal["azure-openai-realtime", "openai-realtime"]
MAX_MESSAGE_BYTES = 1024 * 1024
MAX_SDP_BYTES = 64 * 1024
_CALL_ID = re.compile(r"rtc_[A-Za-z0-9_-]{1,200}")


class RealtimeSessionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    model: str = Field(min_length=1, max_length=300)
    voice: str = Field(min_length=1, max_length=100)
    transcription_model: str = Field(min_length=1, max_length=300)
    instructions: str = Field(max_length=100_000)

    def bootstrap(self) -> dict[str, Any]:
        return {
            "type": "realtime",
            "model": self.model,
            "instructions": self.instructions,
            "output_modalities": ["text"],
            "audio": {
                "input": {
                    "transcription": {"model": self.transcription_model},
                    "turn_detection": None,
                },
                "output": {"voice": self.voice},
            },
            "tools": [],
        }


@dataclass(frozen=True, slots=True)
class RealtimeEndpoint:
    base_url: str
    adapter: RealtimeAdapter

    def __post_init__(self) -> None:
        try:
            endpoint = TypeAdapter(HttpUrl).validate_python(self.base_url)
        except ValueError as exc:
            raise ProviderError(
                "Invalid Realtime endpoint", code="provider.configurationInvalid"
            ) from exc
        path = (endpoint.path or "").rstrip("/")
        if (
            self.adapter not in {"azure-openai-realtime", "openai-realtime"}
            or endpoint.scheme != "https"
            or endpoint.username
            or endpoint.password
            or endpoint.query
            or endpoint.fragment
            or not path.endswith("/v1")
            or any(part in {".", ".."} for part in urlsplit(self.base_url).path.split("/"))
            or "%" in path
            or (self.adapter == "azure-openai-realtime" and path != "/openai/v1")
        ):
            raise ProviderError(
                "Realtime requires a GA HTTPS base URL",
                code="provider.configurationInvalid",
            )
        object.__setattr__(self, "base_url", str(endpoint).rstrip("/"))

    def headers(self, api_key: str) -> dict[str, str]:
        if not api_key or not api_key.strip() or "\r" in api_key or "\n" in api_key:
            raise ProviderError("Realtime API key is missing or invalid", code="provider.notConnected")
        if self.adapter == "azure-openai-realtime":
            return {"api-key": api_key.strip()}
        return {"Authorization": f"Bearer {api_key.strip()}"}

    def call_id(self, location: str) -> str:
        base = urlsplit(self.base_url)
        target = urlsplit(urljoin(f"{self.base_url}/", location))
        value = target.path.rsplit("/", 1)[-1]
        paths = {f"{base.path}/realtime/calls/{value}"}
        if self.adapter == "azure-openai-realtime":
            paths.add(f"/v1/realtime/calls/{value}")
        if (
            not location
            or target.scheme != base.scheme
            or target.netloc != base.netloc
            or target.query
            or target.fragment
            or target.path not in paths
            or _CALL_ID.fullmatch(value) is None
        ):
            raise ProviderError(
                "Invalid Realtime call location", code="provider.invalidResponse"
            )
        return value

    def controller_url(self, call_id: str) -> str:
        if _CALL_ID.fullmatch(call_id) is None:
            raise ProviderError("Invalid Realtime call identity", code="provider.invalidResponse")
        return f"wss://{self.base_url.removeprefix('https://')}/realtime?call_id={call_id}"


@dataclass(frozen=True, slots=True)
class RealtimeCall:
    call_id: str
    sdp_answer: str = field(repr=False)


async def _response_bytes(response: httpx.Response, limit: int) -> bytes:
    if not response.is_success:
        logger.warning("Realtime request failed: status=%s", response.status_code)
        raise ProviderError("Realtime request failed", status_code=response.status_code)
    result = bytearray()
    async for chunk in response.aiter_bytes():
        if len(result) + len(chunk) > limit:
            raise ProviderError("Realtime response is too large", code="provider.responseTooLarge")
        result.extend(chunk)
    return bytes(result)


async def negotiate(
    endpoint: RealtimeEndpoint,
    *,
    api_key: str,
    session: RealtimeSessionConfig,
    sdp_offer: str,
    timeout: float = 30,
) -> RealtimeCall:
    if not sdp_offer.startswith("v=0") or len(sdp_offer.encode()) > MAX_SDP_BYTES:
        raise ProviderError("Invalid SDP offer", code="provider.configurationInvalid")
    headers = endpoint.headers(api_key)
    try:
        async with asyncio.timeout(timeout), httpx.AsyncClient(
            timeout=timeout, follow_redirects=False
        ) as client:
            async with client.stream(
                "POST", f"{endpoint.base_url}/realtime/client_secrets",
                headers=headers, json={"session": session.bootstrap()},
            ) as response:
                body = await _response_bytes(response, MAX_MESSAGE_BYTES)
            try:
                payload = json.loads(body)
            except (ValueError, UnicodeError) as exc:
                raise ProviderError("Invalid Realtime token response", code="provider.invalidResponse") from exc
            token = payload.get("value") if isinstance(payload, dict) else None
            if (
                not isinstance(token, str) or not token.strip() or len(token) > 4096
                or "\r" in token or "\n" in token
            ):
                raise ProviderError("Missing Realtime token", code="provider.invalidResponse")
            async with client.stream(
                "POST", f"{endpoint.base_url}/realtime/calls",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/sdp"},
                content=sdp_offer.encode(),
            ) as response:
                answer = await _response_bytes(response, MAX_SDP_BYTES)
                call_id = endpoint.call_id(response.headers.get("Location", ""))
            try:
                sdp_answer = answer.decode("utf-8")
            except UnicodeError as exc:
                raise ProviderError("Invalid SDP answer", code="provider.invalidResponse") from exc
            if not sdp_answer.startswith("v=0"):
                raise ProviderError("Missing SDP answer", code="provider.invalidResponse")
            return RealtimeCall(call_id=call_id, sdp_answer=sdp_answer)
    except (httpx.RequestError, TimeoutError) as exc:
        logger.warning("Realtime negotiation failed: exception=%s", type(exc).__name__)
        raise ProviderError(
            "Realtime negotiation failed",
            code="provider.timeout" if isinstance(exc, (httpx.TimeoutException, TimeoutError)) else "provider.requestFailed",
        ) from exc


@asynccontextmanager
async def connect_controller(
    endpoint: RealtimeEndpoint, *, api_key: str, call_id: str,
) -> AsyncIterator[aiohttp.ClientWebSocketResponse]:
    url = endpoint.controller_url(call_id)
    headers = endpoint.headers(api_key)

    async def reject_redirect(_session, _context, _params) -> None:
        logger.warning("Realtime controller redirect rejected")
        raise ProviderError("Realtime controller redirect rejected", code="provider.requestFailed")

    trace = aiohttp.TraceConfig()
    trace.on_request_redirect.append(reject_redirect)
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=None, connect=15),
            trace_configs=[trace],
        ) as client:
            async with client.ws_connect(
                url, headers=headers, heartbeat=15, max_msg_size=MAX_MESSAGE_BYTES,
                timeout=aiohttp.ClientWSTimeout(ws_close=5),
            ) as websocket:
                yield websocket
    except (aiohttp.ClientError, TimeoutError) as exc:
        logger.warning("Realtime controller failed: exception=%s", type(exc).__name__)
        raise ProviderError(
            "Realtime controller failed",
            status_code=exc.status if isinstance(exc, aiohttp.WSServerHandshakeError) else None,
            code="provider.timeout" if isinstance(exc, TimeoutError) else None,
        ) from exc


async def hangup(endpoint: RealtimeEndpoint, *, api_key: str, call_id: str) -> None:
    endpoint.controller_url(call_id)
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            async with client.stream(
                "POST", f"{endpoint.base_url}/realtime/calls/{call_id}/hangup",
                headers=endpoint.headers(api_key),
            ) as response:
                if response.status_code != 404:
                    await _response_bytes(response, MAX_MESSAGE_BYTES)
    except httpx.RequestError as exc:
        logger.warning("Realtime hangup failed: exception=%s", type(exc).__name__)
        raise ProviderError("Realtime hangup failed", code="provider.requestFailed") from exc
