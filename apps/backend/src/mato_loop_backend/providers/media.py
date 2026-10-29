from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend.media.attachments import AttachmentError, image_mime_type, validate_image
from mato_loop_backend.models import Credential
from mato_loop_backend.privacy import PseudonymizationError, RequestPseudonymizer
from mato_loop_backend.providers import azure_speech, codex_images, lemonade
from mato_loop_backend.providers.base import MAX_PROVIDER_IMAGE_BYTES, ProviderError
from mato_loop_backend.providers.codex_auth import refresh_codex_credential
from mato_loop_backend.providers.codex_oauth import CODEX_IMAGE_MODEL_ID
from mato_loop_backend.security import load_credential, read_credential
from mato_loop_backend.settings import (
    AiSettings,
    load_effective_ai,
)
from mato_loop_backend.settings.models import (
    LlmProviderConfig,
    ModelAvailability,
    ModelCapability,
    ModelConfig,
    TrustLevel,
    effective_model_pseudonymization,
    effective_model_trust,
)

_SIZE_PATTERN = re.compile(r"^[1-9][0-9]{1,3}x[1-9][0-9]{1,3}$")
_LANGUAGE_PATTERN = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")


@dataclass(frozen=True, slots=True)
class MediaResult:
    data: bytes
    mime_type: str
    filename: str
    metadata: dict[str, Any] = field(default_factory=dict)
    trust_level: TrustLevel | None = None


@dataclass(frozen=True, slots=True)
class TranscriptionResult:
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ResolvedMediaModel:
    provider: LlmProviderConfig
    model: ModelConfig
    base_url: str
    api_key: str | None
    credential: Credential | None = None


@dataclass(frozen=True, slots=True)
class ImageGenerationRequest:
    prompt: str
    size: str | None
    steps: int | None
    cfg_scale: float | None
    seed: int | None
    timeout: float


@dataclass(frozen=True, slots=True)
class ImageEditRequest:
    prompt: str
    images: tuple[tuple[bytes, str], ...]
    timeout: float


_DEFAULT_FIELDS = {
    ModelCapability.IMAGE_GENERATION: "image_generation_model_id",
    ModelCapability.IMAGE_EDITING: "image_generation_model_id",
    ModelCapability.SPEECH_GENERATION: "speech_generation_model_id",
    ModelCapability.AUDIO_GENERATION: "audio_generation_model_id",
    ModelCapability.AUDIO_TRANSCRIPTION: "audio_transcription_model_id",
}

_ADAPTER_CAPABILITIES = {
    "azure-speech": frozenset({ModelCapability.AUDIO_TRANSCRIPTION}),
    "lemonade": frozenset(
        {
            ModelCapability.IMAGE_GENERATION,
            ModelCapability.SPEECH_GENERATION,
            ModelCapability.AUDIO_GENERATION,
            ModelCapability.AUDIO_TRANSCRIPTION,
        }
    ),
    "openai-codex": frozenset(
        {ModelCapability.IMAGE_GENERATION, ModelCapability.IMAGE_EDITING}
    ),
}


def _media_target(
    settings: AiSettings,
    capability: ModelCapability,
    model_id: UUID | None,
) -> tuple[LlmProviderConfig, ModelConfig] | None:
    model = next((item for item in settings.models if item.id == model_id), None)
    provider = (
        next((item for item in settings.providers if item.id == model.provider_id), None)
        if model is not None
        else None
    )
    if (
        model is None
        or provider is None
        or not model.enabled
        or not provider.enabled
        or model.availability != ModelAvailability.AVAILABLE
        or capability not in model.capabilities
        or capability not in _ADAPTER_CAPABILITIES.get(provider.adapter_type, ())
        or (
            provider.adapter_type == "azure-speech"
            and (
                provider.credential_id is None
                or model.model_id != azure_speech.MODEL_ID
            )
        )
        or (
            provider.adapter_type == "openai-codex"
            and (
                provider.credential_id is None
                or model.model_id != CODEX_IMAGE_MODEL_ID
            )
        )
    ):
        return None
    return provider, model


def resolve_media_target(
    settings: AiSettings,
    capability: ModelCapability,
    *,
    active_model_id: UUID | None = None,
) -> tuple[LlmProviderConfig, ModelConfig] | None:
    active = next((item for item in settings.models if item.id == active_model_id), None)
    if target := _media_target(settings, capability, active_model_id):
        return target
    if active is not None and capability in {
        ModelCapability.IMAGE_GENERATION,
        ModelCapability.IMAGE_EDITING,
    }:
        native = next(
            (
                item
                for item in settings.models
                if item.provider_id == active.provider_id
                and item.discovery_metadata.get("native_for_chat_provider") is True
                and _media_target(settings, capability, item.id) is not None
            ),
            None,
        )
        if native is not None:
            return _media_target(settings, capability, native.id)
    field_name = _DEFAULT_FIELDS.get(capability)
    return (
        _media_target(settings, capability, getattr(settings, field_name))
        if field_name is not None
        else None
    )


def media_model_available(
    settings: AiSettings,
    capability: ModelCapability,
    model_id: UUID | None = None,
) -> bool:
    field_name = _DEFAULT_FIELDS.get(capability)
    candidate_id = model_id if model_id is not None else (
        getattr(settings, field_name) if field_name is not None else None
    )
    return _media_target(settings, capability, candidate_id) is not None


def available_media_capabilities(
    settings: AiSettings, *, active_model_id: UUID | None = None
) -> set[ModelCapability]:
    return {
        capability
        for capability in _DEFAULT_FIELDS
        if resolve_media_target(
            settings, capability, active_model_id=active_model_id
        )
    }


def _require_text(value: str, *, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise ProviderError("Invalid media request", code="provider.configurationInvalid")
    return value


def _optional_language(value: str | None) -> str | None:
    if value is not None and not _LANGUAGE_PATTERN.fullmatch(value):
        raise ProviderError("Invalid media request", code="provider.configurationInvalid")
    return value


def _optional_positive_number(
    value: float | int | None, *, maximum: float
) -> float | int | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
        or value > maximum
    ):
        raise ProviderError("Invalid media request", code="provider.configurationInvalid")
    return value


async def _protected_text(
    provider: LlmProviderConfig, model: ModelConfig, value: str
) -> str:
    if not effective_model_pseudonymization(provider, model):
        return value
    try:
        return await asyncio.to_thread(RequestPseudonymizer().protect_text, value)
    except PseudonymizationError as exc:
        raise ProviderError(
            "Media pseudonymization failed", code="privacy.pseudonymizationFailed"
        ) from exc


async def _generate_image_with_lemonade(
    _db: AsyncSession,
    resolved: ResolvedMediaModel,
    request: ImageGenerationRequest,
) -> tuple[bytes, dict[str, Any]]:
    return await lemonade.generate_image(
        base_url=resolved.base_url,
        api_key=resolved.api_key,
        model=resolved.model.model_id,
        prompt=request.prompt,
        size=request.size,
        steps=request.steps,
        cfg_scale=request.cfg_scale,
        seed=request.seed,
        timeout=request.timeout,
    )


async def _generate_image_with_codex(
    db: AsyncSession,
    resolved: ResolvedMediaModel,
    request: ImageGenerationRequest,
) -> tuple[bytes, dict[str, Any]]:
    if request.seed is not None:
        raise ProviderError(
            "Codex image generation does not support seeds",
            code="provider.configurationInvalid",
            params={"field": "seed"},
        )
    if resolved.credential is None:
        raise ProviderError(
            "ChatGPT Codex is not connected", code="provider.notConnected"
        )
    credential = await refresh_codex_credential(
        db, resolved.credential, owner_id=resolved.provider.owner_id
    )
    return await codex_images.generate_image(
        credential=credential,
        prompt=request.prompt,
        size=request.size,
        turn_id=str(uuid4()),
        timeout=request.timeout,
    )


async def _edit_image_with_codex(
    db: AsyncSession,
    resolved: ResolvedMediaModel,
    request: ImageEditRequest,
) -> tuple[bytes, dict[str, Any]]:
    if resolved.credential is None:
        raise ProviderError(
            "ChatGPT Codex is not connected", code="provider.notConnected"
        )
    credential = await refresh_codex_credential(
        db, resolved.credential, owner_id=resolved.provider.owner_id
    )
    return await codex_images.edit_image(
        credential=credential,
        images=request.images,
        prompt=request.prompt,
        turn_id=str(uuid4()),
        timeout=request.timeout,
    )


_ImageGenerator = Callable[
    [AsyncSession, ResolvedMediaModel, ImageGenerationRequest],
    Awaitable[tuple[bytes, dict[str, Any]]],
]
_IMAGE_GENERATORS: dict[str, _ImageGenerator] = {
    "lemonade": _generate_image_with_lemonade,
    "openai-codex": _generate_image_with_codex,
}


def _validate_image_size(size: str | None) -> None:
    if size is None:
        return
    if not _SIZE_PATTERN.fullmatch(size):
        raise ProviderError("Invalid image size", code="provider.configurationInvalid")
    width, height = (int(item) for item in size.split("x", 1))
    if width > 4096 or height > 4096:
        raise ProviderError("Invalid image size", code="provider.configurationInvalid")


def _image_result(
    data: bytes,
    metadata: dict[str, Any],
    resolved: ResolvedMediaModel,
    trust_level: TrustLevel,
) -> MediaResult:
    metadata.pop("authorizationSourceAttachmentIds", None)
    metadata.pop("sourceAttachmentIds", None)
    mime_type = image_mime_type(data)
    if mime_type is None:
        raise ProviderError(
            "Provider returned an invalid image", code="provider.invalidResponse"
        )
    try:
        validate_image(data, mime_type)
    except AttachmentError as exc:
        raise ProviderError(
            "Provider returned an invalid image",
            code=(
                "provider.responseTooLarge"
                if exc.code == "attachment.tooLarge"
                else "provider.invalidResponse"
            ),
        ) from exc
    metadata.update(
        {
            "providerAdapter": resolved.provider.adapter_type,
            "providerName": resolved.provider.name,
            "modelId": resolved.model.model_id,
            "modelName": resolved.model.name or resolved.model.model_id,
            "targetTrustLevel": trust_level.value,
        }
    )
    extension = {
        "image/png": "png",
        "image/jpeg": "jpg",
        "image/webp": "webp",
        "image/gif": "gif",
    }[mime_type]
    return MediaResult(
        data,
        mime_type,
        f"generated.{extension}",
        metadata,
        trust_level,
    )


class MediaService:
    @staticmethod
    async def resolve_model(
        db: AsyncSession,
        capability: ModelCapability,
        *,
        active_model_id: UUID | None = None,
        user_id: UUID,
    ) -> ResolvedMediaModel:
        field_name = _DEFAULT_FIELDS.get(capability)
        if field_name is None:
            raise ProviderError(
                "Unsupported media capability", code="provider.modelCapabilityUnavailable"
            )
        settings = await load_effective_ai(db, user_id)
        target = resolve_media_target(
            settings, capability, active_model_id=active_model_id
        )
        if target is None:
            raise ProviderError(
                "No available default model for capability",
                code="provider.modelCapabilityUnavailable",
            )
        provider, model = target
        base_url = str(provider.config.get("base_url") or "").rstrip("/")
        if provider.adapter_type in {"lemonade", "azure-speech"} and not base_url:
            raise ProviderError(
                "Provider base URL is missing", code="provider.configurationInvalid"
            )
        api_key = None
        credential = None
        if provider.credential_id:
            credential = await load_credential(
                db,
                provider.credential_id,
                owner_id=provider.owner_id,
                populate_existing=True,
            )
            if credential is None:
                raise ProviderError(
                    "Provider credential is missing", code="provider.configurationInvalid"
                )
            if provider.adapter_type != "openai-codex":
                try:
                    payload = read_credential(credential)
                except Exception as exc:
                    raise ProviderError(
                        "Provider credential is invalid", code="provider.configurationInvalid"
                    ) from exc
                value = payload.get("api_key")
                api_key = value if isinstance(value, str) and value else None
        return ResolvedMediaModel(
            provider,
            model,
            base_url,
            api_key,
            credential,
        )

    @classmethod
    async def generate_image(
        cls,
        db: AsyncSession,
        *,
        prompt: str,
        size: str | None = None,
        steps: int | None = None,
        cfg_scale: float | None = None,
        seed: int | None = None,
        timeout: float = 300,
        active_model_id: UUID | None = None,
        expected_model_id: UUID | None = None,
        expected_trust_level: TrustLevel | None = None,
        user_id: UUID,
    ) -> MediaResult:
        prompt = _require_text(prompt, max_length=20_000)
        _validate_image_size(size)
        _optional_positive_number(steps, maximum=1_000)
        _optional_positive_number(cfg_scale, maximum=100)
        if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int) or seed < 0):
            raise ProviderError("Invalid image seed", code="provider.configurationInvalid")
        resolved = await cls.resolve_model(
            db,
            ModelCapability.IMAGE_GENERATION,
            active_model_id=active_model_id,
            user_id=user_id,
        )
        if expected_model_id is not None and resolved.model.id != expected_model_id:
            raise ProviderError(
                "Image generation target changed",
                code="provider.modelCapabilityUnavailable",
            )
        trust_level = effective_model_trust(resolved.provider, resolved.model)
        if expected_trust_level is not None and trust_level != expected_trust_level:
            raise ProviderError(
                "Image generation target trust changed",
                code="provider.modelCapabilityUnavailable",
            )
        protected = await _protected_text(resolved.provider, resolved.model, prompt)
        request = ImageGenerationRequest(
            protected, size, steps, cfg_scale, seed, timeout
        )
        generator = _IMAGE_GENERATORS.get(resolved.provider.adapter_type)
        if generator is None:
            raise ProviderError(
                "Image generation adapter is unavailable",
                code="provider.modelCapabilityUnavailable",
            )
        data, metadata = await generator(db, resolved, request)
        return _image_result(data, metadata, resolved, trust_level)

    @classmethod
    async def edit_image(
        cls,
        db: AsyncSession,
        *,
        prompt: str,
        images: tuple[tuple[bytes, str], ...],
        source_attachment_ids: tuple[UUID, ...] = (),
        timeout: float = 300,
        active_model_id: UUID | None = None,
        expected_model_id: UUID | None = None,
        expected_trust_level: TrustLevel | None = None,
        user_id: UUID,
    ) -> MediaResult:
        prompt = _require_text(prompt, max_length=20_000)
        if not 1 <= len(images) <= 5 or len(source_attachment_ids) > len(images):
            raise ProviderError(
                "Invalid image edit inputs", code="provider.configurationInvalid"
            )
        from mato_loop_backend.providers.service import normalize_provider_image

        total_bytes = normalized_bytes = 0
        normalized_images = []
        for data, mime_type in images:
            try:
                validated = validate_image(data, mime_type)
                normalized_data, normalized_mime_type = normalize_provider_image(
                    data, validated
                )
            except AttachmentError as exc:
                raise ProviderError(
                    "Invalid image edit input", code="provider.invalidResponse"
                ) from exc
            total_bytes += validated.size_bytes
            normalized_bytes += len(normalized_data)
            normalized_images.append((normalized_data, normalized_mime_type))
        if max(total_bytes, normalized_bytes) > MAX_PROVIDER_IMAGE_BYTES:
            raise ProviderError(
                "Image edit inputs are too large", code="provider.responseTooLarge"
            )
        resolved = await cls.resolve_model(
            db,
            ModelCapability.IMAGE_EDITING,
            active_model_id=active_model_id,
            user_id=user_id,
        )
        if expected_model_id is not None and resolved.model.id != expected_model_id:
            raise ProviderError(
                "Image editing target changed",
                code="provider.modelCapabilityUnavailable",
            )
        trust_level = effective_model_trust(resolved.provider, resolved.model)
        if expected_trust_level is not None and trust_level != expected_trust_level:
            raise ProviderError(
                "Image editing target trust changed",
                code="provider.modelCapabilityUnavailable",
            )
        protected = await _protected_text(resolved.provider, resolved.model, prompt)
        if resolved.provider.adapter_type != "openai-codex":
            raise ProviderError(
                "Image editing adapter is unavailable",
                code="provider.modelCapabilityUnavailable",
            )
        data, metadata = await _edit_image_with_codex(
            db,
            resolved,
            ImageEditRequest(protected, tuple(normalized_images), timeout),
        )
        result = _image_result(data, metadata, resolved, trust_level)
        result.metadata["sourceAttachmentIds"] = [
            str(attachment_id) for attachment_id in source_attachment_ids
        ]
        return result

    @classmethod
    async def generate_speech(
        cls,
        db: AsyncSession,
        *,
        text: str,
        voice: str = "shimmer",
        speed: float = 1.0,
        timeout: float = 300,
        user_id: UUID,
    ) -> MediaResult:
        text = _require_text(text, max_length=100_000)
        voice = _require_text(voice, max_length=500)
        _optional_positive_number(speed, maximum=4)
        resolved = await cls.resolve_model(
            db,
            ModelCapability.SPEECH_GENERATION,
            user_id=user_id,
        )
        protected = await _protected_text(resolved.provider, resolved.model, text)
        data = await lemonade.generate_speech(
            base_url=resolved.base_url,
            api_key=resolved.api_key,
            model=resolved.model.model_id,
            text=protected,
            voice=voice,
            speed=speed,
            timeout=timeout,
        )
        return MediaResult(data, "audio/wav", "speech.wav")

    @classmethod
    async def generate_audio(
        cls,
        db: AsyncSession,
        *,
        prompt: str,
        lyrics: str | None = None,
        vocal_language: str | None = None,
        duration: float | None = None,
        steps: int | None = None,
        cfg: float | None = None,
        seed: int | None = None,
        timeout: float = 300,
        user_id: UUID,
    ) -> MediaResult:
        prompt = _require_text(prompt, max_length=20_000)
        if lyrics is not None and len(lyrics) > 100_000:
            raise ProviderError("Invalid lyrics", code="provider.configurationInvalid")
        vocal_language = _optional_language(vocal_language)
        _optional_positive_number(duration, maximum=3_600)
        _optional_positive_number(steps, maximum=1_000)
        _optional_positive_number(cfg, maximum=100)
        if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int) or seed < 0):
            raise ProviderError("Invalid audio seed", code="provider.configurationInvalid")
        resolved = await cls.resolve_model(
            db,
            ModelCapability.AUDIO_GENERATION,
            user_id=user_id,
        )
        protected_prompt = await _protected_text(resolved.provider, resolved.model, prompt)
        protected_lyrics = (
            await _protected_text(resolved.provider, resolved.model, lyrics)
            if lyrics
            else lyrics
        )
        data = await lemonade.generate_audio(
            base_url=resolved.base_url,
            api_key=resolved.api_key,
            model=resolved.model.model_id,
            prompt=protected_prompt,
            lyrics=protected_lyrics,
            vocal_language=vocal_language,
            duration=duration,
            steps=steps,
            cfg=cfg,
            seed=seed,
            timeout=timeout,
        )
        return MediaResult(data, "audio/wav", "generated.wav")

    @classmethod
    async def transcribe_audio(
        cls,
        db: AsyncSession,
        *,
        audio: bytes,
        language: str | None = None,
        timeout: float = 300,
        active_model_id: UUID | None = None,
        expected_model_id: UUID | None = None,
        expected_trust_level: TrustLevel | None = None,
        user_id: UUID,
    ) -> TranscriptionResult:
        if not isinstance(audio, bytes) or not audio or len(audio) > lemonade.MAX_AUDIO_BYTES:
            raise ProviderError("Invalid audio input", code="provider.configurationInvalid")
        language = _optional_language(language)
        if language is None:
            from mato_loop_backend.settings.language import preferred_language

            language = await preferred_language(db, user_id)
        resolved = await cls.resolve_model(
            db,
            ModelCapability.AUDIO_TRANSCRIPTION,
            active_model_id=active_model_id,
            user_id=user_id,
        )
        if (
            (expected_model_id is not None and resolved.model.id != expected_model_id)
            or (expected_trust_level is not None
                and effective_model_trust(resolved.provider, resolved.model) != expected_trust_level)
        ):
            raise ProviderError(
                "Audio transcription target changed", code="provider.modelCapabilityUnavailable",
            )
        await db.commit()
        transcribe = (
            azure_speech.transcribe_audio
            if resolved.provider.adapter_type == "azure-speech"
            else lemonade.transcribe_audio
        )
        text = await transcribe(
            base_url=resolved.base_url,
            api_key=resolved.api_key,
            model=resolved.model.model_id,
            audio=audio,
            language=language,
            timeout=timeout,
        )
        return TranscriptionResult(text)
