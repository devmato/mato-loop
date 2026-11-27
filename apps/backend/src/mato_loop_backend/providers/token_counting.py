"""Optional chat-template-aware counting on OpenAI-compatible inference servers."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx

from mato_loop_backend.providers.base import ProviderRequest
from mato_loop_backend.providers.openai_compatible import _chat_body
from mato_loop_backend.settings.models import LlmProviderConfig

_unavailable_until: dict[tuple[UUID, str, UUID | None, str], float] = {}


def chat_token_count_payload(request: ProviderRequest) -> dict[str, Any]:
    body = _chat_body(request)
    return {name: body[name] for name in ("model", "messages", "tools", "chat_template_kwargs") if name in body}


@dataclass(frozen=True, slots=True)
class ChatUsageSample:
    """Conservative calibration for an append-only text request within a turn."""

    message_count: int
    fingerprint: bytes
    estimate: int
    actual: int

    @staticmethod
    def _fingerprint(request: ProviderRequest, prefix_length: int | None = None) -> tuple[int, bytes]:
        payload = chat_token_count_payload(request)
        if prefix_length is not None:
            payload["messages"] = payload["messages"][:prefix_length]
        binding = [request.continuation_scope, request.reasoning_effort, request.tool_choice, payload]
        serialized = json.dumps(binding, ensure_ascii=False, separators=(",", ":")).encode()
        return len(payload["messages"]), hashlib.sha256(serialized).digest()

    @classmethod
    def record(cls, request: ProviderRequest, estimate: int, actual: int | None) -> ChatUsageSample | None:
        if request.media or type(actual) is not int or actual <= estimate or estimate <= 0:
            return None
        count, fingerprint = cls._fingerprint(request)
        return cls(count, fingerprint, estimate, actual)

    def calibrated_tokens(self, request: ProviderRequest, estimate: int) -> int:
        if request.media or self._fingerprint(request, self.message_count) != (self.message_count, self.fingerprint):
            return estimate
        return self.actual + max(0, estimate - self.estimate)


async def count_chat_tokens(
    provider: LlmProviderConfig, request: ProviderRequest, *, api_key: str | None,
) -> int | None:
    base_url = str(provider.config.get("base_url") or "").rstrip("/")
    if not base_url or request.media:
        return None
    key = (provider.id, base_url, provider.credential_id, request.model)
    if _unavailable_until.get(key, 0) > time.monotonic():
        return None
    payload = chat_token_count_payload(request)
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    retry_after = 60
    try:
        async with asyncio.timeout(min(request.timeout, 3)), httpx.AsyncClient(timeout=3, follow_redirects=False) as client:
            response = await client.post(
                f"{base_url.removesuffix('/v1')}/tokenize", json=payload, headers=headers,
            )
        if response.status_code in {404, 405, 501}:
            retry_after = 3_600
        response.raise_for_status()
        data = response.json()
        count = data.get("count") if isinstance(data, dict) else None
        if type(count) is int and count > 0:
            _unavailable_until.pop(key, None)
            return count
    except (TimeoutError, httpx.HTTPError, httpx.InvalidURL, ValueError):
        pass
    if len(_unavailable_until) >= 256:
        _unavailable_until.pop(next(iter(_unavailable_until)))
    _unavailable_until[key] = time.monotonic() + retry_after
    return None
