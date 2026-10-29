from __future__ import annotations

import base64
import binascii
import logging
import math
from typing import Annotated, Any

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StringConstraints,
    ValidationError,
)

from mato_loop_backend.providers.base import ProviderError
from mato_loop_backend.providers.openai_compatible import DiscoveredModel
from mato_loop_backend.settings.models import ModelCapability, ModelInputModality

logger = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_AUDIO_BYTES = 64 * 1024 * 1024
_MAX_JSON_BYTES = 15 * 1024 * 1024
_MAX_TRANSCRIPTION_BYTES = 1024 * 1024
_CHAT_RECIPES = {
    "llamacpp",
    "flm",
    "ryzenai-llm",
    "vllm",
    "cloud",
    "collection.router",
}
_AUDIO_RECIPES = {"acestep", "thinksound"}
_SafeString = Annotated[str, StringConstraints(max_length=500)]
_SafeScalar = _SafeString | int | float | bool | None
_ModelId = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=300)]
_Label = Annotated[str, StringConstraints(strict=True, max_length=100)]
_Recipe = Annotated[str, StringConstraints(strict=True, max_length=100)]


class _LemonadeModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: _ModelId
    labels: list[_Label] = Field(default_factory=list, max_length=64)
    recipe: _Recipe = ""
    downloaded: StrictBool | None = None
    context_length: StrictInt | None = Field(default=None, ge=1, le=10_000_000)
    max_context_window: StrictInt | None = Field(default=None, ge=1, le=10_000_000)
    recipe_options: dict[str, Any] = Field(default_factory=dict)
    image_defaults: dict[str, _SafeScalar] | None = None
    supports_tools: StrictBool | None = None
    supports_parallel_tool_calls: StrictBool | None = None


class _ModelListResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    data: list[_LemonadeModel] = Field(max_length=10_000)


class _ImageData(BaseModel):
    model_config = ConfigDict(extra="ignore")

    b64_json: str


class _ImageResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    data: list[_ImageData] = Field(min_length=1, max_length=1)
    created: int | None = None


class _TranscriptionResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    text: str = Field(max_length=2_000_000)


def _normalized(value: str) -> str:
    return "-".join(value.strip().casefold().replace("_", "-").split())


def _label_set(labels: list[str]) -> set[str]:
    return {_normalized(label) for label in labels if label.strip()}


def _capabilities(model: _LemonadeModel) -> set[ModelCapability]:
    labels = _label_set(model.labels)
    recipe = _normalized(model.recipe)
    capabilities: set[ModelCapability] = set()
    if "image" in labels:
        capabilities.add(ModelCapability.IMAGE_GENERATION)
    if "tts" in labels:
        capabilities.add(ModelCapability.SPEECH_GENERATION)
    if "transcription" in labels:
        capabilities.add(ModelCapability.AUDIO_TRANSCRIPTION)
    if recipe.replace("-", "") in _AUDIO_RECIPES:
        capabilities.add(ModelCapability.AUDIO_GENERATION)
    if recipe in _CHAT_RECIPES:
        capabilities.add(ModelCapability.CHAT)
    return capabilities


def _safe_image_defaults(value: dict[str, _SafeScalar] | None) -> dict[str, _SafeScalar] | None:
    if value is None:
        return None
    result: dict[str, _SafeScalar] = {}
    for key, item in list(value.items())[:32]:
        if not isinstance(key, str) or not key or len(key) > 100:
            continue
        if isinstance(item, float) and not math.isfinite(item):
            continue
        result[key] = item
    return result


def _metadata(model: _LemonadeModel) -> dict[str, Any]:
    labels = [label[:100] for label in model.labels]
    normalized_labels = _label_set(model.labels)
    configured_context = model.recipe_options.get("ctx_size")
    context_length = (
        model.context_length
        if model.context_length is not None
        else configured_context
        if isinstance(configured_context, int)
        and not isinstance(configured_context, bool)
        and configured_context > 1
        else None
    )
    metadata: dict[str, Any] = {
        "labels": labels,
        "recipe": _normalized(model.recipe),
        "context_length": context_length,
        "max_context_window": model.max_context_window,
        "image_defaults": _safe_image_defaults(model.image_defaults),
        "supports_image_input": "vision" in normalized_labels,
        "input_modalities": [
            ModelInputModality.TEXT,
            *(
                [ModelInputModality.IMAGE]
                if "vision" in normalized_labels
                else []
            ),
        ],
        "supports_tools": (
            model.supports_tools
            if model.supports_tools is not None
            else (
                "tool-calling" in normalized_labels
                or model.supports_parallel_tool_calls is True
            )
        ),
        "supports_parallel_tool_calls": model.supports_parallel_tool_calls or False,
    }
    return {key: value for key, value in metadata.items() if value is not None}


def _headers(api_key: str | None) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _raise_for_status(response: httpx.Response) -> None:
    if response.is_success:
        return
    logger.warning("Lemonade request failed with HTTP status %s", response.status_code)
    raise ProviderError(
        "Lemonade request failed",
        status_code=response.status_code,
    )


def _request_error(
    exc: httpx.RequestError, *, operation: str, timeout: float
) -> ProviderError:
    exception_name = type(exc).__name__
    if isinstance(exc, httpx.TimeoutException):
        logger.warning(
            "Lemonade request timed out: operation=%s timeout_seconds=%s exception=%s",
            operation,
            timeout,
            exception_name,
        )
        return ProviderError(
            "Lemonade request timed out",
            code="provider.timeout",
            params={"timeoutSeconds": timeout, "retryable": True},
        )
    logger.warning(
        "Lemonade network request failed: operation=%s exception=%s",
        operation,
        exception_name,
    )
    return ProviderError("Lemonade is unavailable", code="provider.unavailable")


async def discover_models(
    *, base_url: str, api_key: str | None, timeout: float = 30
) -> list[DiscoveredModel]:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "GET", f"{base_url.rstrip('/')}/models", headers=_headers(api_key)
            ) as response:
                _raise_for_status(response)
                content_length = response.headers.get("content-length")
                if content_length is not None:
                    try:
                        if int(content_length) > _MAX_JSON_BYTES:
                            raise ProviderError(
                                "Lemonade response is too large",
                                code="provider.invalidResponse",
                            )
                    except ValueError:
                        pass
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(content) + len(chunk) > _MAX_JSON_BYTES:
                        raise ProviderError(
                            "Lemonade response is too large",
                            code="provider.invalidResponse",
                        )
                    content.extend(chunk)
        payload = _ModelListResponse.model_validate_json(content)
    except ProviderError:
        raise
    except httpx.RequestError as exc:
        raise _request_error(exc, operation="model_discovery", timeout=timeout) from exc
    except (ValueError, ValidationError) as exc:
        raise ProviderError("Lemonade returned an invalid response", code="provider.invalidResponse") from exc
    return [
        DiscoveredModel(
            id=model.id,
            capabilities=_capabilities(model),
            **_metadata(model),
        )
        for model in payload.data
        if model.downloaded is not False
    ]


async def _post_json(
    *,
    url: str,
    api_key: str | None,
    payload: dict[str, Any],
    timeout: float,
    max_bytes: int,
    operation: str,
) -> httpx.Response:
    headers = _headers(api_key)
    headers["Content-Type"] = "application/json"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", url, headers=headers, json=payload) as response:
                _raise_for_status(response)
                content_length = response.headers.get("content-length")
                if content_length is not None:
                    try:
                        if int(content_length) > max_bytes:
                            raise ProviderError(
                                "Lemonade response is too large",
                                code="provider.responseTooLarge",
                            )
                    except ValueError:
                        pass
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(content) + len(chunk) > max_bytes:
                        raise ProviderError(
                            "Lemonade response is too large",
                            code="provider.responseTooLarge",
                        )
                    content.extend(chunk)
                result = httpx.Response(
                    response.status_code,
                    headers=response.headers,
                    content=bytes(content),
                )
    except ProviderError:
        raise
    except httpx.RequestError as exc:
        raise _request_error(exc, operation=operation, timeout=timeout) from exc
    return result


async def generate_image(
    *,
    base_url: str,
    api_key: str | None,
    model: str,
    prompt: str,
    size: str | None = None,
    steps: int | None = None,
    cfg_scale: float | None = None,
    seed: int | None = None,
    timeout: float = 300,
) -> tuple[bytes, dict[str, Any]]:
    payload: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "n": 1,
        "response_format": "b64_json",
    }
    if size is not None:
        payload["size"] = size
    for key, value in (("steps", steps), ("cfg_scale", cfg_scale), ("seed", seed)):
        if value is not None:
            payload[key] = value
    response = await _post_json(
        url=f"{base_url.rstrip('/')}/images/generations",
        api_key=api_key,
        payload=payload,
        timeout=timeout,
        max_bytes=_MAX_JSON_BYTES,
        operation="image_generation",
    )
    try:
        parsed = _ImageResponse.model_validate(response.json())
        data = base64.b64decode(parsed.data[0].b64_json, validate=True)
    except (ValueError, ValidationError, binascii.Error) as exc:
        raise ProviderError("Lemonade returned an invalid image", code="provider.invalidResponse") from exc
    if len(data) > MAX_IMAGE_BYTES:
        raise ProviderError("Lemonade image is too large", code="provider.responseTooLarge")
    metadata = {"created": parsed.created} if parsed.created is not None else {}
    return data, metadata


async def generate_speech(
    *,
    base_url: str,
    api_key: str | None,
    model: str,
    text: str,
    voice: str = "shimmer",
    speed: float = 1.0,
    timeout: float = 300,
) -> bytes:
    response = await _post_json(
        url=f"{base_url.rstrip('/')}/audio/speech",
        api_key=api_key,
        payload={
            "model": model,
            "input": text,
            "voice": voice,
            "speed": speed,
            "response_format": "wav",
        },
        timeout=timeout,
        max_bytes=MAX_AUDIO_BYTES,
        operation="speech_generation",
    )
    if not response.content:
        raise ProviderError("Lemonade returned empty audio", code="provider.invalidResponse")
    return response.content


async def generate_audio(
    *,
    base_url: str,
    api_key: str | None,
    model: str,
    prompt: str,
    lyrics: str | None = None,
    vocal_language: str | None = None,
    duration: float | None = None,
    steps: int | None = None,
    cfg: float | None = None,
    seed: int | None = None,
    timeout: float = 300,
) -> bytes:
    payload: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "response_format": "wav",
    }
    for key, value in (
        ("lyrics", lyrics),
        ("vocal_language", vocal_language),
        ("duration", duration),
        ("steps", steps),
        ("cfg", cfg),
        ("seed", seed),
    ):
        if value is not None:
            payload[key] = value
    response = await _post_json(
        url=f"{base_url.rstrip('/')}/audio/generations",
        api_key=api_key,
        payload=payload,
        timeout=timeout,
        max_bytes=MAX_AUDIO_BYTES,
        operation="audio_generation",
    )
    if not response.content:
        raise ProviderError("Lemonade returned empty audio", code="provider.invalidResponse")
    return response.content


async def transcribe_audio(
    *,
    base_url: str,
    api_key: str | None,
    model: str,
    audio: bytes,
    language: str | None = None,
    timeout: float = 300,
) -> str:
    data = {"model": model, "response_format": "json"}
    if language is not None:
        data["language"] = language.partition("-")[0].lower()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST",
                f"{base_url.rstrip('/')}/audio/transcriptions",
                headers=_headers(api_key),
                data=data,
                files={"file": ("audio.wav", audio, "audio/wav")},
            ) as response:
                _raise_for_status(response)
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(content) + len(chunk) > _MAX_TRANSCRIPTION_BYTES:
                        raise ProviderError(
                            "Lemonade response is too large",
                            code="provider.responseTooLarge",
                        )
                    content.extend(chunk)
    except ProviderError:
        raise
    except httpx.RequestError as exc:
        raise _request_error(exc, operation="audio_transcription", timeout=timeout) from exc
    try:
        return _TranscriptionResponse.model_validate_json(content).text
    except (ValueError, ValidationError) as exc:
        raise ProviderError(
            "Lemonade returned an invalid transcription", code="provider.invalidResponse"
        ) from exc
