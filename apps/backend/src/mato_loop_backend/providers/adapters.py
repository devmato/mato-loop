from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal, cast

from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend.providers import azure_speech, codex_oauth
from mato_loop_backend.providers.base import (
    DeltaCallback,
    ProviderError,
    ProviderRequest,
    ProviderResult,
    ReasoningDeltaCallback,
)
from mato_loop_backend.providers.openai_compatible import DiscoveredModel
from mato_loop_backend.settings.models import LlmProviderConfig

ApiType = Literal["chat_completions", "responses"]
ProviderOperation = Callable[[], Awaitable[ProviderResult]]


@dataclass(frozen=True, slots=True)
class AdapterRuntime:
    read_credential: Callable[[Any], dict[str, Any]]
    refresh_codex_credential: Callable[..., Awaitable[dict[str, Any]]]
    codex_headers: Callable[..., dict[str, str]]
    discover_openai: Callable[..., Awaitable[list[DiscoveredModel]]]
    discover_codex: Callable[..., Awaitable[list[DiscoveredModel]]]
    discover_lemonade: Callable[..., Awaitable[list[DiscoveredModel]]]
    chat_completions: Callable[..., Awaitable[ProviderResult]]
    responses_api: Callable[..., Awaitable[ProviderResult]]


Discovery = Callable[
    [AdapterRuntime, AsyncSession, LlmProviderConfig, Any | None, float],
    Awaitable[list[DiscoveredModel]],
]
OperationFactory = Callable[
    [
        AdapterRuntime,
        AsyncSession,
        LlmProviderConfig,
        Any | None,
        str | None,
        ApiType,
        ProviderRequest,
        DeltaCallback | None,
        ReasoningDeltaCallback | None,
    ],
    Awaitable[ProviderOperation],
]


@dataclass(frozen=True, slots=True)
class ProviderAdapter:
    adapter_type: str
    discover: Discovery
    operation: OperationFactory
    forced_api_type: ApiType | None = None
    native_media_api_types: frozenset[ApiType] = frozenset()
    codex_auth: bool = False
    model_prefix: str = ""
    omit_generation_options: bool = False
    force_reasoning_summary: bool = False
    supports_prompt_cache: bool = False
    include_stream_usage: bool = False
    reasoning_profile_for_responses: bool = False
    controls_thinking_without_profile: bool = False

    def api_type(self, provider: LlmProviderConfig) -> ApiType:
        if self.forced_api_type is not None:
            return self.forced_api_type
        return cast(
            ApiType, str(provider.config.get("api_type") or "chat_completions")
        )

    def endpoint(self, provider: LlmProviderConfig) -> str:
        if self.codex_auth:
            return codex_oauth.CODEX_RESPONSES_URL
        return str(provider.config.get("base_url") or "").rstrip("/")


def _base_url(provider: LlmProviderConfig) -> str:
    base_url = str(provider.config.get("base_url") or "").rstrip("/")
    if not base_url:
        raise ProviderError(
            "Provider base URL is missing", code="provider.configurationInvalid"
        )
    return base_url


async def _discover_openai(
    runtime: AdapterRuntime,
    _db: AsyncSession,
    provider: LlmProviderConfig,
    credential: Any | None,
    timeout: float,
) -> list[DiscoveredModel]:
    payload = runtime.read_credential(credential) if credential else {}
    return await runtime.discover_openai(
        base_url=_base_url(provider),
        api_key=payload.get("api_key"),
        timeout=timeout,
    )


async def _discover_lemonade(
    runtime: AdapterRuntime,
    _db: AsyncSession,
    provider: LlmProviderConfig,
    credential: Any | None,
    timeout: float,
) -> list[DiscoveredModel]:
    payload = runtime.read_credential(credential) if credential else {}
    return await runtime.discover_lemonade(
        base_url=_base_url(provider),
        api_key=payload.get("api_key"),
        timeout=timeout,
    )


async def _discover_codex(
    runtime: AdapterRuntime,
    db: AsyncSession,
    provider: LlmProviderConfig,
    credential: Any | None,
    timeout: float,
) -> list[DiscoveredModel]:
    if credential is None:
        raise ProviderError(
            "ChatGPT Codex is not connected", code="provider.notConnected"
        )
    refreshed = await runtime.refresh_codex_credential(
        db, credential, owner_id=provider.owner_id
    )
    return await runtime.discover_codex(
        access_token=refreshed["access"],
        account_id=refreshed.get("account_id"),
        timeout=timeout,
    )


async def _discover_azure_speech(
    runtime: AdapterRuntime,
    _db: AsyncSession,
    provider: LlmProviderConfig,
    credential: Any | None,
    timeout: float,
) -> list[DiscoveredModel]:
    payload = runtime.read_credential(credential) if credential else {}
    return await azure_speech.discover_models(
        base_url=_base_url(provider),
        api_key=payload.get("api_key"),
        timeout=timeout,
    )


async def _transcription_only_operation(
    _runtime: AdapterRuntime,
    _db: AsyncSession,
    _provider: LlmProviderConfig,
    _credential: Any | None,
    _api_key: str | None,
    _api_type: ApiType,
    _request: ProviderRequest,
    _on_delta: DeltaCallback | None,
    _on_reasoning: ReasoningDeltaCallback | None,
) -> ProviderOperation:
    raise ProviderError(
        "Provider supports transcription only", code="provider.modelCapabilityUnavailable"
    )


async def _openai_operation(
    runtime: AdapterRuntime,
    _db: AsyncSession,
    provider: LlmProviderConfig,
    _credential: Any | None,
    api_key: str | None,
    api_type: ApiType,
    request: ProviderRequest,
    on_delta: DeltaCallback | None,
    on_reasoning: ReasoningDeltaCallback | None,
) -> ProviderOperation:
    base_url = _base_url(provider)
    if api_type == "responses":
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        async def execute() -> ProviderResult:
            return await runtime.responses_api(
                url=f"{base_url}/responses",
                headers=headers,
                request=request,
                on_delta=on_delta,
                on_reasoning=on_reasoning,
            )

        return execute

    async def execute() -> ProviderResult:
        return await runtime.chat_completions(
            base_url=base_url,
            api_key=api_key,
            request=request,
            on_delta=on_delta,
            on_reasoning=on_reasoning,
        )

    return execute


async def _codex_operation(
    runtime: AdapterRuntime,
    db: AsyncSession,
    provider: LlmProviderConfig,
    credential: Any | None,
    _api_key: str | None,
    _api_type: ApiType,
    request: ProviderRequest,
    on_delta: DeltaCallback | None,
    on_reasoning: ReasoningDeltaCallback | None,
) -> ProviderOperation:
    if credential is None:
        raise ProviderError(
            "ChatGPT Codex is not connected", code="provider.notConnected"
        )
    refreshed = await runtime.refresh_codex_credential(
        db, credential, owner_id=provider.owner_id
    )
    headers = runtime.codex_headers(
        refreshed,
        accept="text/event-stream" if request.stream else "application/json",
    )
    headers["OpenAI-Beta"] = "responses=experimental"
    if request.prompt_cache_key:
        headers["session-id"] = request.prompt_cache_key

    async def execute() -> ProviderResult:
        return await runtime.responses_api(
            url=codex_oauth.CODEX_RESPONSES_URL,
            headers=headers,
            request=request,
            on_delta=on_delta,
            on_reasoning=on_reasoning,
        )

    return execute


_OPENAI_COMPATIBLE = ProviderAdapter(
    adapter_type="openai-compatible",
    discover=_discover_openai,
    operation=_openai_operation,
    native_media_api_types=frozenset({"chat_completions"}),
    include_stream_usage=True,
)
_CODEX = ProviderAdapter(
    adapter_type="openai-codex",
    discover=_discover_codex,
    operation=_codex_operation,
    forced_api_type="responses",
    codex_auth=True,
    model_prefix="openai-codex/",
    omit_generation_options=True,
    force_reasoning_summary=True,
    supports_prompt_cache=True,
    reasoning_profile_for_responses=True,
)
_LEMONADE = ProviderAdapter(
    adapter_type="lemonade",
    discover=_discover_lemonade,
    operation=_openai_operation,
    forced_api_type="chat_completions",
    include_stream_usage=True,
    controls_thinking_without_profile=True,
)
_AZURE_SPEECH = ProviderAdapter(
    adapter_type="azure-speech",
    discover=_discover_azure_speech,
    operation=_transcription_only_operation,
)

PROVIDER_ADAPTERS: Mapping[str, ProviderAdapter] = MappingProxyType(
    {
        adapter.adapter_type: adapter
        for adapter in (_OPENAI_COMPATIBLE, _CODEX, _LEMONADE, _AZURE_SPEECH)
    }
)
