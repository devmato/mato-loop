from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import math
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass
from io import BytesIO
from types import SimpleNamespace
from typing import Any, Literal
from uuid import UUID

import httpx
from PIL import Image, ImageOps
from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend.media.attachments import (
    MAX_IMAGE_ATTACHMENT_BYTES,
    AttachmentError,
    AttachmentService,
    ValidatedImage,
    latest_image_references,
    validate_image,
)
from mato_loop_backend.privacy import (
    ProtectionCache,
    PseudonymizationError,
    RequestPseudonymizer,
    StreamingPseudonymRestorer,
)
from mato_loop_backend.providers import codex_oauth, lemonade, openai_compatible
from mato_loop_backend.providers.adapters import (
    PROVIDER_ADAPTERS,
    AdapterRuntime,
    ApiType,
    ProviderAdapter,
)
from mato_loop_backend.providers.base import (
    MAX_PROVIDER_AUDIO_BYTES,
    MAX_PROVIDER_IMAGE_BYTES,
    MAX_PROVIDER_IMAGES,
    MAX_PROVIDER_MEDIA_INPUTS,
    MAX_PROVIDER_VIDEO_BYTES,
    ProviderError,
    ProviderImageInput,
    ProviderMedia,
    ProviderMediaInput,
    ProviderRequest,
    ProviderResult,
    ReasoningDeltaCallback,
    select_provider_images,
)
from mato_loop_backend.providers.codex_auth import codex_headers, refresh_codex_credential
from mato_loop_backend.providers.openai_compatible import (
    DiscoveredModel,
    _chat_body,
    _responses_body,
    chat_completions,
    responses_api,
)
from mato_loop_backend.providers.output_budget import (
    ESTIMATION_POLICY,
    allocate_output_tokens,
    estimate_request_input_tokens,
    guarded_input_tokens,
    output_token_limit,
)
from mato_loop_backend.providers.reasoning import reasoning_profile
from mato_loop_backend.providers.token_counting import chat_token_count_payload, count_chat_tokens
from mato_loop_backend.security import load_credential, read_credential
from mato_loop_backend.settings.models import (
    AgentRuntimeSettings,
    LlmProviderConfig,
    ModelCapability,
    ModelConfig,
    ModelInputModality,
    ReasoningEffort,
    TrustLevel,
    effective_model_pseudonymization,
    effective_model_trust,
)
from mato_loop_backend.settings.store import SettingsStore

logger = logging.getLogger(__name__)

PSEUDONYMIZATION_WARNING_CODE = "privacy.placeholderRestorationIncomplete"
_UNKNOWN_MARKER_REPLACEMENT = "[...]"

_IMAGE_MIME_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
_RETRY_DELAYS = (2.0, 5.0)
_MODEL_IMAGE_MAX_EDGE = 2048
_MODEL_IMAGE_MAX_BYTES = 5 * 1024 * 1024
_AUDIO_FORMATS = {
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
}
_VIDEO_MIME_TYPES = {"video/mp4", "video/mpeg", "video/quicktime", "video/webm"}


def _adapter_runtime() -> AdapterRuntime:
    return AdapterRuntime(
        read_credential=read_credential,
        refresh_codex_credential=refresh_codex_credential,
        codex_headers=codex_headers,
        discover_openai=openai_compatible.discover_models,
        discover_codex=codex_oauth.discover_models,
        discover_lemonade=lemonade.discover_models,
        chat_completions=chat_completions,
        responses_api=responses_api,
    )


def _pseudonymization_provider_error(exc: PseudonymizationError) -> ProviderError:
    return ProviderError("Local pseudonymization failed", code=str(exc))


def _temperature_option(config: dict[str, Any]) -> float | None:
    value = config.get("temperature")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    return None


def _is_llamacpp_model(provider: LlmProviderConfig, model: ModelConfig) -> bool:
    metadata = model.discovery_metadata
    return (
        provider.adapter_type == "openai-compatible"
        and str(metadata.get("owned_by") or "").casefold() == "llamacpp"
    ) or (
        provider.adapter_type == "lemonade"
        and str(metadata.get("recipe") or "").casefold() == "llamacpp"
    )


def _project_provider_tools(
    provider: LlmProviderConfig,
    model: ModelConfig,
    tools: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not _is_llamacpp_model(provider, model):
        return tools

    # llama.cpp expands finite bounds into grammar repetitions with a hard complexity limit.
    def project(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: project(item)
                for key, item in value.items()
                if key not in {"maxLength", "maxItems"}
            }
        if isinstance(value, list):
            return [project(item) for item in value]
        return value

    return [project(tool) for tool in tools]


def normalize_provider_image(data: bytes, validated: ValidatedImage) -> tuple[bytes, str]:
    if (
        validated.size_bytes <= _MODEL_IMAGE_MAX_BYTES
        and validated.width <= _MODEL_IMAGE_MAX_EDGE
        and validated.height <= _MODEL_IMAGE_MAX_EDGE
    ):
        return data, validated.mime_type
    try:
        with Image.open(BytesIO(data)) as source:
            resized = ImageOps.exif_transpose(source)
            resized.thumbnail(
                (_MODEL_IMAGE_MAX_EDGE, _MODEL_IMAGE_MAX_EDGE), Image.Resampling.LANCZOS
            )
            rgba = resized.convert("RGBA")
            image = Image.new("RGB", rgba.size, "white")
            image.paste(rgba, mask=rgba.getchannel("A"))
        while True:
            buffer = BytesIO()
            image.save(buffer, format="JPEG", quality=85, optimize=True)
            normalized = buffer.getvalue()
            if len(normalized) <= _MODEL_IMAGE_MAX_BYTES:
                return normalized, "image/jpeg"
            next_size = tuple(max(1, dimension * 3 // 4) for dimension in image.size)
            if next_size == image.size:
                raise AttachmentError("attachment.tooLarge")
            image = image.resize(next_size, Image.Resampling.LANCZOS)
    except (OSError, SyntaxError, ValueError) as exc:
        raise AttachmentError("attachment.invalidImage") from exc


def _read_image_data_url(attachment: dict[str, Any]) -> str:
    session_id = UUID(attachment["session_id"])
    source = SimpleNamespace(
        **{
            **attachment,
            "session_id": session_id,
            "file_root_id": UUID(attachment["file_root_id"]),
            "file_id": UUID(attachment["file_id"]),
            "run_id": UUID(attachment["run_id"]) if attachment.get("run_id") else None,
        }
    )
    data = AttachmentService().read_source(source, session_id, current_only=True)
    if len(data) > MAX_IMAGE_ATTACHMENT_BYTES or len(data) != attachment["size_bytes"]:
        raise AttachmentError("attachment.invalidImage")
    validated = validate_image(data, attachment["mime_type"])
    if validated.sha256 != attachment["sha256"]:
        raise AttachmentError("attachment.invalidImage")
    normalized, mime_type = normalize_provider_image(data, validated)
    encoded = base64.b64encode(normalized).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


async def _provider_projection(
    messages: list[dict[str, Any]],
    *,
    supports_image_input: bool,
    target_trust_level: TrustLevel,
    image_inputs: list[ProviderImageInput] | None = None,
    required_image_ids: tuple[str, ...] = (),
) -> tuple[list[dict[str, Any]], list[ProviderMedia]]:
    projected_messages: list[dict[str, Any]] = []
    for message in messages:
        projected = dict(message)
        projected.pop("attachments", None)
        projected.pop("source_message_id", None)
        recorded_computer = projected.pop("recorded_computer", None)
        if projected.get("role") == "tool":
            content = projected.get("content") or ""
            try:
                payload = json.loads(content)
            except (TypeError, ValueError):
                payload = content
            # Add host provenance after result shortening/browser projection, before estimation
            # and privacy protection. Tool data cannot supply this reserved top-level field.
            supplied_context = isinstance(payload, dict) and "hostSelectedComputer" in payload
            if recorded_computer or supplied_context:
                payload = payload if isinstance(payload, dict) else {"result": payload}
                payload.pop("hostSelectedComputer", None)
                if recorded_computer:
                    payload["hostSelectedComputer"] = recorded_computer
                projected["content"] = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        projected_messages.append(projected)

    selected = select_provider_images(
        latest_image_references(messages),
        target_trust_level=target_trust_level.value,
        required_image_ids=required_image_ids,
    )
    if not supports_image_input:
        unavailable_indices = {item.message_index for item in selected} | {
            image.message_index for image in image_inputs or []
        }
        for index, message in enumerate(projected_messages):
            if index in unavailable_indices:
                message["content"] = (message.get("content") or "") + (
                    "\n\n[Image content unavailable to this model. Only image metadata is available.]"
                )
        return projected_messages, []

    direct_inputs = image_inputs or []
    if len(selected) + len(direct_inputs) > MAX_PROVIDER_IMAGES:
        raise ProviderError(
            "Too many combined image inputs",
            code="provider.imageLimitExceeded",
            params={"maxImages": MAX_PROVIDER_IMAGES},
        )
    media = [
        ProviderMedia(
            message_index=item.message_index,
            mime_type=str(item.attachment.get("mime_type") or "image"),
            status="unavailable",
        )
        for item in selected
    ]

    async def materialize(position: int, attachment: dict[str, Any]) -> None:
        mime_type = attachment.get("mime_type")
        size_bytes = attachment.get("size_bytes")
        sha256 = attachment.get("sha256")
        session_id = attachment.get("session_id")
        file_root_id = attachment.get("file_root_id")
        file_id = attachment.get("file_id")
        file_revision = attachment.get("file_revision")
        valid_source = (
            isinstance(file_root_id, str)
            and isinstance(file_id, str)
            and (
                (
                    isinstance(file_revision, int)
                    and not isinstance(file_revision, bool)
                    and file_revision > 0
                )
                or (file_revision is None and isinstance(attachment.get("run_id"), str))
            )
        )
        if (
            mime_type not in _IMAGE_MIME_TYPES
            or not isinstance(size_bytes, int)
            or isinstance(size_bytes, bool)
            or not 0 < size_bytes <= MAX_IMAGE_ATTACHMENT_BYTES
            or not isinstance(sha256, str)
            or len(sha256) != 64
            or not isinstance(session_id, str)
            or not valid_source
        ):
            return
        try:
            data_url = await asyncio.to_thread(_read_image_data_url, attachment)
        except (AttachmentError, OSError, ValueError):
            return
        media[position].data_url = data_url
        media[position].status = "ready"

    for position, item in enumerate(selected):
        await materialize(position, item.attachment)
    unavailable_count = sum(item.status != "ready" for item in media)
    if unavailable_count:
        logger.warning(
            "%s attachment file(s) could not be sent to the provider", unavailable_count
        )
    if direct_inputs:
        total_bytes = sum(item.attachment["size_bytes"] for item in selected)
        for image in direct_inputs:
            if image.trust_level not in {"public", "private"}:
                raise ProviderError(
                    "Invalid direct image trust level", code="provider.configurationInvalid"
                )
            if not 0 <= image.message_index < len(projected_messages):
                raise ProviderError(
                    "Invalid direct image message index", code="provider.configurationInvalid"
                )
            try:
                validated = validate_image(image.data, image.mime_type)
                normalized, mime_type = normalize_provider_image(image.data, validated)
            except AttachmentError as exc:
                raise ProviderError(
                    "Invalid direct image input", code="provider.invalidResponse"
                ) from exc
            total_bytes += len(normalized)
            if total_bytes > MAX_PROVIDER_IMAGE_BYTES:
                raise ProviderError(
                    "Combined image inputs are too large", code="provider.responseTooLarge"
                )
            encoded = base64.b64encode(normalized).decode("ascii")
            media.append(
                ProviderMedia(
                    message_index=image.message_index,
                    mime_type=mime_type,
                    data_url=f"data:{mime_type};base64,{encoded}",
                    status="ready",
                )
            )
    return projected_messages, media


def _direct_provider_media(
    inputs: list[ProviderMediaInput],
    *,
    message_count: int,
    input_modalities: set[ModelInputModality],
) -> list[ProviderMedia]:
    if len(inputs) > MAX_PROVIDER_MEDIA_INPUTS:
        raise ProviderError(
            "Too many direct media inputs", code="provider.modelCapabilityUnavailable"
        )
    media: list[ProviderMedia] = []
    for item in inputs:
        if item.modality not in {"audio", "video"}:
            raise ProviderError(
                "Invalid direct media modality", code="provider.configurationInvalid"
            )
        if ModelInputModality(item.modality) not in input_modalities:
            raise ProviderError(
                "Model does not support the direct media modality",
                code="provider.modelCapabilityUnavailable",
            )
        if item.trust_level not in {"public", "private"}:
            raise ProviderError(
                "Invalid direct media trust level", code="provider.configurationInvalid"
            )
        if not 0 <= item.message_index < message_count:
            raise ProviderError(
                "Invalid direct media message index", code="provider.configurationInvalid"
            )
        if not isinstance(item.data, bytes) or not item.data:
            raise ProviderError(
                "Invalid direct media data", code="provider.configurationInvalid"
            )
        if item.modality == "audio":
            format_ = _AUDIO_FORMATS.get(item.mime_type)
            if format_ is None:
                raise ProviderError(
                    "Unsupported direct audio MIME type",
                    code="provider.configurationInvalid",
                )
            if len(item.data) > MAX_PROVIDER_AUDIO_BYTES:
                raise ProviderError(
                    "Direct audio input is too large", code="provider.responseTooLarge"
                )
            media.append(
                ProviderMedia(
                    message_index=item.message_index,
                    mime_type=item.mime_type,
                    status="ready",
                    modality="audio",
                    data_base64=base64.b64encode(item.data).decode("ascii"),
                    format=format_,
                )
            )
        else:
            if item.mime_type not in _VIDEO_MIME_TYPES:
                raise ProviderError(
                    "Unsupported direct video MIME type",
                    code="provider.configurationInvalid",
                )
            if len(item.data) > MAX_PROVIDER_VIDEO_BYTES:
                raise ProviderError(
                    "Direct video input is too large", code="provider.responseTooLarge"
                )
            encoded = base64.b64encode(item.data).decode("ascii")
            media.append(
                ProviderMedia(
                    message_index=item.message_index,
                    mime_type=item.mime_type,
                    data_url=f"data:{item.mime_type};base64,{encoded}",
                    status="ready",
                    modality="video",
                )
            )
    return media


async def _with_retries(
    operation: Callable[[], Awaitable[ProviderResult]],
    can_retry: Callable[[], bool] | None = None,
    on_retry: Callable[[int, int, int | None, str], Awaitable[None]] | None = None,
) -> ProviderResult:
    total_attempts = len(_RETRY_DELAYS) + 1
    for attempt in range(total_attempts):
        try:
            return await operation()
        except ProviderError as exc:
            status_code = exc.status_code
            error_code = exc.public_code
            transient = exc.status_code == 429 or (
                exc.status_code is not None and exc.status_code >= 500
            )
            if (
                not transient
                or attempt == len(_RETRY_DELAYS)
                or (can_retry and not can_retry())
            ):
                raise
            delay = (
                exc.retry_after
                if exc.retry_after is not None
                else _RETRY_DELAYS[attempt]
            )
        except httpx.RequestError as exc:
            status_code = None
            error_code = "provider.unavailable"
            if attempt == len(_RETRY_DELAYS) or (can_retry and not can_retry()):
                raise ProviderError(
                    "Provider network request failed", code="provider.unavailable"
                ) from exc
            delay = _RETRY_DELAYS[attempt]
        next_attempt = attempt + 2
        delay = min(delay, 30.0)
        logger.info(
            "Retrying transient provider request (attempt %s/%s in %.1f seconds)",
            next_attempt,
            total_attempts,
            delay,
        )
        await asyncio.sleep(delay)
        if on_retry:
            await on_retry(next_attempt, total_attempts, status_code, error_code)
    raise ProviderError("Provider request failed", code="provider.requestFailed")


@dataclass(slots=True, repr=False)
class PreparedRequest:
    request: ProviderRequest
    adapter: ProviderAdapter
    api_type: ApiType
    pseudonymizer: RequestPseudonymizer | None
    estimated_input_tokens: int
    inputs: tuple[Any, ...]

    @property
    def input_token_fingerprint(self) -> str | None:
        if self.adapter.adapter_type != "openai-compatible" or self.api_type != "chat_completions" or self.request.media:
            return None
        payload = [self.request.continuation_scope, chat_token_count_payload(self.request)]
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


class ProviderService:
    @staticmethod
    async def count_input_tokens(
        db: AsyncSession, *, provider: LlmProviderConfig, prepared: PreparedRequest,
    ) -> int | None:
        if (
            provider.adapter_type != "openai-compatible"
            or prepared.api_type != "chat_completions"
            or not provider.config.get("base_url")
            or prepared.request.media
        ):
            return None
        credential = (
            await load_credential(db, provider.credential_id, owner_id=provider.owner_id, populate_existing=True)
            if provider.credential_id else None
        )
        payload = read_credential(credential) if credential else {}
        return await count_chat_tokens(provider, prepared.request, api_key=payload.get("api_key"))

    @staticmethod
    async def discover_models(
        db: AsyncSession,
        *,
        provider: LlmProviderConfig,
        timeout: float = 30,
    ) -> list[DiscoveredModel]:
        credential = (
            await load_credential(
                db,
                provider.credential_id,
                owner_id=provider.owner_id,
                populate_existing=True,
            )
            if provider.credential_id
            else None
        )
        adapter = PROVIDER_ADAPTERS.get(provider.adapter_type)
        if adapter is None:
            raise ProviderError(
                "Provider does not support model discovery",
                code="provider.discoveryUnsupported",
            )
        return await adapter.discover(
            _adapter_runtime(), db, provider, credential, timeout
        )

    @staticmethod
    async def prepare_request(
        *,
        provider: LlmProviderConfig,
        model: ModelConfig,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        tool_choice: Literal["auto", "required", "none"] = "auto",
        timeout: float = 300,
        target_trust_level: TrustLevel | None = None,
        pseudonymization_enabled: bool | None = None,
        pseudonymization_key: bytes | None = None,
        pseudonymization_cache: ProtectionCache | None = None,
        image_inputs: list[ProviderImageInput] | None = None,
        media_inputs: list[ProviderMediaInput] | None = None,
        prompt_cache_key: str | None = None,
        required_image_ids: tuple[str, ...] = (),
        reasoning_effort: ReasoningEffort | str | None = None,
        prepared_request: PreparedRequest | None = None,
    ) -> PreparedRequest:
        """Prepare original candidate messages locally, without settings, auth, or transport."""
        inputs = (
            provider, model, messages, tools, tool_choice, timeout, target_trust_level,
            pseudonymization_enabled, pseudonymization_key, image_inputs, media_inputs,
            prompt_cache_key, required_image_ids, reasoning_effort,
        )
        if prepared_request is not None and prepared_request.inputs == inputs:
            return prepared_request
        if model.provider_id != provider.id:
            raise ProviderError(
                "Model and provider do not belong together",
                code="provider.configurationInvalid",
            )
        if ModelCapability.CHAT not in model.capabilities:
            raise ProviderError(
                "Model does not support chat",
                code="provider.modelCapabilityUnavailable",
            )
        adapter = PROVIDER_ADAPTERS.get(provider.adapter_type)
        if adapter is None:
            raise ProviderError(
                "Unsupported provider adapter", code="provider.configurationInvalid"
            )
        model_trust_level = target_trust_level or effective_model_trust(provider, model)
        api_type = adapter.api_type(provider)
        if media_inputs and api_type not in adapter.native_media_api_types:
            raise ProviderError(
                "Native audio and video require OpenAI-compatible Chat Completions",
                code="provider.modelCapabilityUnavailable",
            )
        direct_media = _direct_provider_media(
            media_inputs or [],
            message_count=len(messages),
            input_modalities=model.input_modalities,
        )
        use_pseudonymization = (
            pseudonymization_enabled
            if pseudonymization_enabled is not None
            else effective_model_pseudonymization(provider, model)
        )
        profile = (
            reasoning_profile(model.model_id, model.discovery_metadata)
            if api_type == "chat_completions" or adapter.reasoning_profile_for_responses
            else None
        )
        effective_reasoning_effort = (
            ReasoningEffort(reasoning_effort)
            if reasoning_effort is not None
            else profile.default_effort if profile else model.reasoning_effort
        )
        if profile and effective_reasoning_effort not in profile.options:
            raise ProviderError(
                "Model does not support the selected reasoning effort",
                code="provider.modelCapabilityUnavailable",
            )
        endpoint = adapter.endpoint(provider)
        continuation_scope = {
            "adapterType": provider.adapter_type,
            "providerId": str(provider.id),
            "modelConfigId": str(model.id),
            "credentialId": str(provider.credential_id) if provider.credential_id else None,
            "endpoint": endpoint,
            "trustLevel": model_trust_level.value,
            "pseudonymization": "enabled" if use_pseudonymization else "disabled",
        }
        provider_messages, media = await _provider_projection(
            messages,
            supports_image_input=model.supports_image_input,
            target_trust_level=model_trust_level,
            image_inputs=image_inputs,
            required_image_ids=required_image_ids,
        )
        media.extend(direct_media)
        pseudonymizer = (
            RequestPseudonymizer(
                key=pseudonymization_key, protection_cache=pseudonymization_cache
            ) if use_pseudonymization else None
        )
        if pseudonymizer is not None:
            try:
                provider_messages = await asyncio.to_thread(
                    pseudonymizer.protect_messages, provider_messages
                )
            except PseudonymizationError as exc:
                raise _pseudonymization_provider_error(exc) from exc
        request = ProviderRequest(
            model=(
                model.model_id.removeprefix(adapter.model_prefix)
                if adapter.model_prefix
                else model.model_id
            ),
            messages=provider_messages,
            tools=_project_provider_tools(provider, model, tools),
            tool_choice=tool_choice,
            stream=model.supports_streaming,
            include_usage=model.supports_streaming and adapter.include_stream_usage,
            supports_parallel_tool_calls=(
                model.supports_tools and model.supports_parallel_tool_calls
            ),
            temperature=(
                None
                if adapter.omit_generation_options
                else _temperature_option(model.config)
            ),
            timeout=timeout,
            reasoning_summary=adapter.force_reasoning_summary or model.reasoning_summary,
            reasoning_effort=(
                None
                if profile
                and profile.supports_disable
                and effective_reasoning_effort == ReasoningEffort.NONE
                else effective_reasoning_effort
            ),
            enable_thinking=(
                effective_reasoning_effort != ReasoningEffort.NONE
                if effective_reasoning_effort is not None
                and (
                    profile is not None
                    and profile.supports_disable
                    or adapter.controls_thinking_without_profile
                )
                else None
            ),
            prompt_cache_key=prompt_cache_key if adapter.supports_prompt_cache else None,
            continuation_scope=continuation_scope,
            media=media,
        )
        reasoning_token_estimates: dict[int, int] = {}
        body = (
            _responses_body(request, reasoning_token_estimates=reasoning_token_estimates)
            if api_type == "responses" else _chat_body(request)
        )
        if pseudonymizer is not None:
            pseudonymizer.retain_transmitted(body)
        return PreparedRequest(
            request, adapter, api_type, pseudonymizer,
            estimate_request_input_tokens(
                body, model.characters_per_token,
                reasoning_token_estimates=reasoning_token_estimates,
            ),
            deepcopy(inputs),
        )

    @staticmethod
    async def complete(
        db: AsyncSession,
        *,
        provider: LlmProviderConfig,
        model: ModelConfig,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        tool_choice: Literal["auto", "required", "none"] = "auto",
        on_delta: Callable[[str], Awaitable[None]] | None = None,
        on_reasoning: ReasoningDeltaCallback | None = None,
        on_warning: Callable[[str], Awaitable[None]] | None = None,
        on_retry: Callable[[int, int, int | None, str], Awaitable[None]] | None = None,
        timeout: float = 300,
        max_output_tokens: int | None = None,
        estimated_input_tokens: int | None = None,
        counted_input_fingerprint: str | None = None,
        reserved_output_tokens: int | None = None,
        safety_reserve_tokens: int | None = None,
        target_trust_level: TrustLevel | None = None,
        pseudonymization_enabled: bool | None = None,
        pseudonymization_key: bytes | None = None,
        pseudonymization_cache: ProtectionCache | None = None,
        image_inputs: list[ProviderImageInput] | None = None,
        media_inputs: list[ProviderMediaInput] | None = None,
        prompt_cache_key: str | None = None,
        required_image_ids: tuple[str, ...] = (),
        reasoning_effort: ReasoningEffort | str | None = None,
        prepared_request: PreparedRequest | None = None,
    ) -> ProviderResult:
        """Complete with an adaptive budget checked after all input transformations.

        estimated_input_tokens is an optional raw projection estimate, without
        proportional slack or fixed safety. Diagnostics are returned separately
        from provider usage in provider_data["output_budget"].
        A matching counted_input_fingerprint uses the server count instead of the heuristic.
        An omitted safety reserve uses the stored global runtime setting; an
        explicit value preserves the caller's snapshot without another lookup.
        """
        prepared = await ProviderService.prepare_request(
            provider=provider,
            model=model,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            timeout=timeout,
            target_trust_level=target_trust_level,
            pseudonymization_enabled=pseudonymization_enabled,
            pseudonymization_key=pseudonymization_key,
            pseudonymization_cache=pseudonymization_cache,
            image_inputs=image_inputs,
            media_inputs=media_inputs,
            prompt_cache_key=prompt_cache_key,
            required_image_ids=required_image_ids,
            reasoning_effort=reasoning_effort,
            prepared_request=prepared_request,
        )
        request = prepared.request
        adapter = prepared.adapter
        api_type = prepared.api_type
        pseudonymizer = prepared.pseudonymizer
        final_input_tokens = prepared.estimated_input_tokens
        if estimated_input_tokens is not None:
            # Validate before max(), which could otherwise conceal invalid negative inputs.
            guarded_input_tokens(estimated_input_tokens)
        input_tokens = max(final_input_tokens, estimated_input_tokens or 0)
        if (estimated_input_tokens is not None and counted_input_fingerprint is not None
                and counted_input_fingerprint == prepared.input_token_fingerprint):
            input_tokens = final_input_tokens = estimated_input_tokens
        unknown_media = any(item.modality in {"audio", "video"} for item in request.media)
        limit = output_token_limit(model, max_output_tokens)
        reserve = (
            min(2_048, limit) if limit is not None else 2_048
        ) if reserved_output_tokens is None else reserved_output_tokens
        if safety_reserve_tokens is None:
            runtime_settings = await SettingsStore.load(db, AgentRuntimeSettings)
            safety_reserve_tokens = runtime_settings.safety_reserve_tokens
        allocated = allocate_output_tokens(
            model,
            input_tokens,
            requested=max_output_tokens,
            reserved_output_tokens=reserve,
            safety_reserve_tokens=safety_reserve_tokens,
        )
        request.max_output_tokens = (
            allocated
            if not adapter.omit_generation_options and limit is not None
            else None
        )
        output_budget = {
            "estimatedInputTokens": input_tokens,
            "finalEstimatedInputTokens": final_input_tokens,
            "guardedInputTokens": guarded_input_tokens(input_tokens),
            "reservedOutputTokens": reserve,
            "allocatedOutputTokens": allocated,
            "transmittedMaxOutputTokens": request.max_output_tokens,
            "safetyReserveTokens": safety_reserve_tokens,
            "manualLimit": model.max_output_tokens,
            "providerLimit": model.discovered_max_output_tokens,
            "helperLimit": max_output_tokens,
            "mode": (
                "helper" if max_output_tokens is not None
                else "manual" if model.max_output_tokens is not None else "auto"
            ),
            "estimationPolicy": ESTIMATION_POLICY,
            "mediaInputTokensUnknown": unknown_media,
        }
        stream_started = False
        warning_pending = False
        warning_emitted = False

        def note_unknown_marker() -> None:
            nonlocal warning_pending
            warning_pending = True

        async def publish_warning() -> None:
            nonlocal warning_pending, warning_emitted
            if not warning_pending or warning_emitted:
                return
            warning_pending = False
            warning_emitted = True
            if on_warning is not None:
                await on_warning(PSEUDONYMIZATION_WARNING_CODE)

        delta_restorer = (
            StreamingPseudonymRestorer(
                pseudonymizer,
                unknown_marker_replacement=_UNKNOWN_MARKER_REPLACEMENT,
                on_unknown_marker=note_unknown_marker,
            )
            if pseudonymizer is not None and on_delta is not None
            else None
        )
        reasoning_restorers: dict[str, StreamingPseudonymRestorer] = {}

        async def tracked_delta(text: str) -> None:
            nonlocal stream_started
            stream_started = True
            if on_delta:
                try:
                    restored = delta_restorer.feed(text) if delta_restorer else text
                except PseudonymizationError as exc:
                    raise _pseudonymization_provider_error(exc) from exc
                await publish_warning()
                if restored:
                    await on_delta(restored)

        async def tracked_reasoning(text: str, kind: str) -> None:
            nonlocal stream_started
            stream_started = True
            if on_reasoning:
                restorer = reasoning_restorers.get(kind)
                if pseudonymizer is not None and restorer is None:
                    restorer = StreamingPseudonymRestorer(
                        pseudonymizer,
                        unknown_marker_replacement=_UNKNOWN_MARKER_REPLACEMENT,
                        on_unknown_marker=note_unknown_marker,
                    )
                    reasoning_restorers[kind] = restorer
                try:
                    restored = restorer.feed(text) if restorer else text
                except PseudonymizationError as exc:
                    raise _pseudonymization_provider_error(exc) from exc
                await publish_warning()
                if restored:
                    await on_reasoning(restored, kind)

        delta_callback = tracked_delta if on_delta else None
        reasoning_callback = tracked_reasoning if on_reasoning else None

        async def finalize(result: ProviderResult) -> ProviderResult:
            result.provider_data["output_budget"] = output_budget
            try:
                if delta_restorer is not None and on_delta is not None:
                    if remaining := delta_restorer.flush():
                        await on_delta(remaining)
                    await publish_warning()
                if on_reasoning is not None:
                    for kind, restorer in reasoning_restorers.items():
                        if remaining := restorer.flush():
                            await on_reasoning(remaining, kind)
                        await publish_warning()
                if pseudonymizer is not None:
                    result.content = pseudonymizer.restore_text(
                        result.content,
                        unknown_marker_replacement=_UNKNOWN_MARKER_REPLACEMENT,
                        on_unknown_marker=note_unknown_marker,
                    )
                    for call in result.tool_calls:
                        call.arguments = pseudonymizer.restore_value(call.arguments)
                    if result.reasoning is not None:
                        result.reasoning.text = pseudonymizer.restore_text(
                            result.reasoning.text,
                            unknown_marker_replacement=_UNKNOWN_MARKER_REPLACEMENT,
                            on_unknown_marker=note_unknown_marker,
                        )
                    await publish_warning()
                    for key in (
                        "responses_output_items",
                        "reasoning_encrypted_content",
                        "chat_reasoning_details",
                        "chat_reasoning_content",
                    ):
                        result.provider_data.pop(key, None)
                    return result
            except PseudonymizationError as exc:
                raise _pseudonymization_provider_error(exc) from exc
            if (
                result.provider_data.get("responses_output_items")
                or result.provider_data.get("reasoning_encrypted_content")
                or result.provider_data.get("chat_reasoning_details")
                or result.provider_data.get("chat_reasoning_content")
            ):
                result.provider_data["continuation_scope"] = request.continuation_scope
            return result

        def can_retry() -> bool:
            return not stream_started

        credential = (
            await load_credential(
                db,
                provider.credential_id,
                owner_id=provider.owner_id,
                populate_existing=True,
            )
            if provider.credential_id
            else None
        )
        payload = read_credential(credential) if credential and not adapter.codex_auth else {}
        operation = await adapter.operation(
            _adapter_runtime(),
            db,
            provider,
            credential,
            payload.get("api_key"),
            api_type,
            request,
            delta_callback,
            reasoning_callback,
        )
        return await finalize(await _with_retries(operation, can_retry, on_retry))
