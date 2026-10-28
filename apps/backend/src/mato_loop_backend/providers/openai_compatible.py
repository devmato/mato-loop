from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from mato_loop_backend.providers.base import (
    DeltaCallback,
    ProviderError,
    ProviderMedia,
    ProviderRequest,
    ProviderResult,
    PublicReasoning,
    ReasoningDeltaCallback,
    ToolCall,
)
from mato_loop_backend.providers.reasoning import (
    TaggedReasoningStream,
    split_tagged_reasoning,
)
from mato_loop_backend.settings.models import (
    ModelCapability,
    ModelInputModality,
    ReasoningEffort,
)

logger = logging.getLogger(__name__)

_PROVIDER_REQUEST_ID_HEADERS = (
    "x-request-id",
    "request-id",
    "x-correlation-id",
    "trace-id",
)
_DATA_URL = re.compile(r"data:[^\s,;]+(?:;[^\s,]+)*,[^\s]+", re.IGNORECASE)
_BEARER_TOKEN = re.compile(r"\bBearer\s+\S+", re.IGNORECASE)
_SENSITIVE_PROVIDER_TEXT = re.compile(
    r"\b(?:api[-_ ]?key|authorization|password|secret|access[-_ ]?token|refresh[-_ ]?token)\b",
    re.IGNORECASE,
)
_URL_QUERY = re.compile(r"(https?://[^\s?#]+)\?[^\s#]+", re.IGNORECASE)

class DiscoveredModel(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str = Field(min_length=1)
    capabilities: set[ModelCapability] = Field(
        default_factory=lambda: {ModelCapability.CHAT}
    )
    input_modalities: set[ModelInputModality] | None = None
    default_reasoning_effort: ReasoningEffort | None = None
    reasoning_efforts: list[ReasoningEffort] | None = None

    @field_validator("capabilities", mode="before")
    @classmethod
    def normalize_capabilities(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                capability for capability in ModelCapability
                if value.get(capability) is True
            }
        return value

    @property
    def discovered_max_output_tokens(self) -> int | None:
        # OpenRouter /models documents this as an output cap, not a generation default.
        top_provider = (self.__pydantic_extra__ or {}).get("top_provider")
        value = top_provider.get("max_completion_tokens") if isinstance(top_provider, dict) else None
        return value if type(value) is int and value > 0 else None

    @model_validator(mode="after")
    def derive_legacy_image_capability(self) -> DiscoveredModel:
        if self.input_modalities is not None:
            self.__pydantic_extra__ = self.__pydantic_extra__ or {}
            self.__pydantic_extra__["supports_image_input"] = (
                ModelInputModality.IMAGE in self.input_modalities
            )
        return self

    @property
    def metadata(self) -> dict[str, Any]:
        allowed = {
            "name",
            "object",
            "created",
            "owned_by",
            "root",
            "parent",
            "context_length",
            "max_model_len",
            "max_context_window",
            "recipe",
            "supports_image_input",
            "input_modalities",
            "supports_tools",
            "supports_parallel_tool_calls",
            "capabilities_authoritative",
            "native_for_chat_provider",
            "default_reasoning_effort",
            "reasoning_efforts",
        }
        metadata: dict[str, Any] = {}
        for key, value in self.model_dump(
            exclude={"id", "capabilities"}, exclude_none=True
        ).items():
            if key == "input_modalities" and isinstance(value, set):
                metadata[key] = sorted(str(item) for item in value)
                continue
            if key == "reasoning_efforts" and isinstance(value, list):
                metadata[key] = [str(item) for item in value]
                continue
            if key not in allowed or not isinstance(value, (str, int, float, bool, type(None))):
                continue
            metadata[key] = value[:500] if isinstance(value, str) else value
        labels = self.__pydantic_extra__.get("labels") if self.__pydantic_extra__ else None
        if isinstance(labels, list):
            metadata["labels"] = [
                item[:100] for item in labels[:64] if isinstance(item, str)
            ]
        image_defaults = (
            self.__pydantic_extra__.get("image_defaults") if self.__pydantic_extra__ else None
        )
        if isinstance(image_defaults, dict):
            metadata["image_defaults"] = {
                key[:100]: value[:500] if isinstance(value, str) else value
                for key, value in list(image_defaults.items())[:32]
                if isinstance(key, str)
                and isinstance(value, (str, int, float, bool, type(None)))
            }
        if self.discovered_max_output_tokens is not None:
            metadata["top_provider"] = {
                "max_completion_tokens": self.discovered_max_output_tokens
            }
        return metadata


class _ModelListResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    data: list[DiscoveredModel]


async def _sse_events(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    async for line in response.aiter_lines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            return
        if not data:
            continue
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            if event.get("error") is not None or event.get("type") == "error":
                raise _failed_response_error(
                    event if "error" in event else {"error": event}
                )
            yield event


def _tools_payload(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"type": "function", "function": tool} for tool in tools]


def _media_by_message(media: list[ProviderMedia]) -> dict[int, list[ProviderMedia]]:
    grouped: dict[int, list[ProviderMedia]] = {}
    for item in media:
        grouped.setdefault(item.message_index, []).append(item)
    return grouped


def _media_marker(item: ProviderMedia) -> str:
    if item.status == "unsupported":
        return "[Image attachment not sent to this model]"
    return "[Image attachment unavailable]"


def _media_context_marker(source_role: str) -> str:
    if source_role == "tool":
        return (
            "[Tool-result media for internal inspection. It belongs to the preceding tool "
            "result and is not a new user message or upload. Inspect it and correct the result "
            "if needed before replying.]"
        )
    return "[Session image]"


def _chat_media_message(
    media: list[ProviderMedia], *, source_role: str
) -> dict[str, Any]:
    content: list[dict[str, Any]] = [
        {"type": "text", "text": _media_context_marker(source_role)}
    ]
    for item in media:
        if item.modality == "video" and item.data_url:
            content.append({"type": "video_url", "video_url": {"url": item.data_url}})
        elif item.modality == "audio" and item.data_base64 and item.format:
            content.append(
                {
                    "type": "input_audio",
                    "input_audio": {"data": item.data_base64, "format": item.format},
                }
            )
        elif item.data_url:
            content.append({"type": "image_url", "image_url": {"url": item.data_url}})
        else:
            content.append({"type": "text", "text": _media_marker(item)})
    return {"role": "user", "content": content}


def _chat_messages(
    messages: list[dict[str, Any]],
    continuation_scope: dict[str, str | None] | None = None,
    media: list[ProviderMedia] | None = None,
) -> list[dict[str, Any]]:
    converted = []
    leading_system = True
    grouped_media = _media_by_message(media or [])
    pending_tool_media: list[list[ProviderMedia]] = []
    for index, message in enumerate(messages):
        item = {"role": message["role"], "content": message.get("content") or ""}
        if message.get("tool_call_id"):
            item["tool_call_id"] = message["tool_call_id"]
        if message.get("tool_calls"):
            item["tool_calls"] = [
                {
                    "id": call["id"],
                    "type": "function",
                    "function": {
                        "name": call["name"],
                        "arguments": json.dumps(call.get("arguments") or {}, ensure_ascii=True),
                    },
                }
                for call in message["tool_calls"]
            ]
        provider_data = message.get("provider_data") or {}
        if (
            message["role"] == "assistant"
            and bool(continuation_scope)
            and provider_data.get("continuation_scope") == continuation_scope
            and isinstance(provider_data.get("chat_reasoning_details"), list)
        ):
            item["reasoning_details"] = provider_data["chat_reasoning_details"]
        if leading_system and item["role"] == "system" and converted:
            converted[0]["content"] += "\n\n" + item["content"]
        else:
            converted.append(item)
        leading_system = leading_system and item["role"] == "system"
        message_media = grouped_media.get(index)
        if message["role"] == "tool":
            if message_media:
                pending_tool_media.append(message_media)
            next_is_tool = index + 1 < len(messages) and messages[index + 1]["role"] == "tool"
            if not next_is_tool:
                converted.extend(
                    _chat_media_message(items, source_role="tool")
                    for items in pending_tool_media
                )
                pending_tool_media.clear()
        elif message_media:
            converted.append(
                _chat_media_message(message_media, source_role=message["role"])
            )
    return converted


def _direct_thinking_options(base_url: str) -> bool:
    hostname = (urlparse(base_url).hostname or "").casefold()
    return hostname.startswith("dashscope") and hostname.endswith(".aliyuncs.com")


def _chat_body(
    request: ProviderRequest, *, direct_thinking_options: bool = False
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": request.model,
        "messages": _chat_messages(request.messages, request.continuation_scope, request.media),
        "stream": request.stream,
    }
    if request.stream and request.include_usage:
        body["stream_options"] = {"include_usage": True}
    if request.max_output_tokens is not None:
        body["max_tokens"] = request.max_output_tokens
    if request.temperature is not None:
        body["temperature"] = request.temperature
    if request.reasoning_effort is not None:
        body["reasoning_effort"] = request.reasoning_effort.value
    if request.enable_thinking is not None:
        if direct_thinking_options:
            body["enable_thinking"] = request.enable_thinking
        else:
            body["chat_template_kwargs"] = {
                "enable_thinking": request.enable_thinking
            }
    if request.tools:
        body["tools"] = _tools_payload(request.tools)
        body["tool_choice"] = request.tool_choice
        if request.supports_parallel_tool_calls:
            body["parallel_tool_calls"] = True
    return body


def _responses_input(
    messages: list[dict[str, Any]],
    continuation_scope: dict[str, str | None] | None = None,
    media: list[ProviderMedia] | None = None,
    *,
    reasoning_token_estimates: dict[int, int] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    if any(item.modality != "image" for item in media or []):
        raise ProviderError(
            "Responses API does not support native audio or video input",
            code="provider.modelCapabilityUnavailable",
        )
    instructions: list[str] = []
    items: list[dict[str, Any]] = []
    if reasoning_token_estimates is not None:
        reasoning_token_estimates.clear()
    grouped_media = _media_by_message(media or [])
    pending_tool_media: list[list[ProviderMedia]] = []

    def append_media(values: list[ProviderMedia], *, source_role: str) -> None:
        content: list[dict[str, Any]] = [
            {"type": "input_text", "text": _media_context_marker(source_role)}
        ]
        for item in values:
            if item.data_url:
                content.append({"type": "input_image", "image_url": item.data_url})
            else:
                content.append({"type": "input_text", "text": _media_marker(item)})
        items.append({"role": "user", "content": content})

    for index, message in enumerate(messages):
        first_item = len(items)
        role = message["role"]
        content = message.get("content") or ""
        has_tool_calls = bool(message.get("tool_calls"))
        provider_data = message.get("provider_data") or {}
        if role == "system":
            instructions.append(content)
        elif role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": message.get("tool_call_id"),
                    "output": content,
                }
            )
        else:
            scope_matches = bool(continuation_scope) and provider_data.get(
                "continuation_scope"
            ) == continuation_scope
            replay_items = provider_data.get("responses_output_items") if scope_matches else None
            if isinstance(replay_items, list):
                replay_items = [
                    {
                        **item,
                        "summary": (
                            item["summary"] if isinstance(item.get("summary"), list) else []
                        ),
                    }
                    if isinstance(item, dict) and item.get("type") == "reasoning"
                    else item
                    for item in replay_items
                ]
            if (
                role == "assistant"
                and not has_tool_calls
                and isinstance(replay_items, list)
                and replay_items
            ):
                items.extend(item for item in replay_items if isinstance(item, dict))
            else:
                replayed_reasoning = [
                    item
                    for item in replay_items or []
                    if isinstance(item, dict) and item.get("type") == "reasoning"
                ]
                if role == "assistant" and has_tool_calls:
                    items.extend(replayed_reasoning)
                encrypted_reasoning = (
                    provider_data.get("reasoning_encrypted_content")
                    if scope_matches and not replayed_reasoning
                    else None
                )
                if role == "assistant" and encrypted_reasoning:
                    items.append(
                        {
                            "type": "reasoning",
                            "summary": [],
                            "encrypted_content": encrypted_reasoning,
                        }
                    )
                content_type = "output_text" if role == "assistant" else "input_text"
                if content:
                    items.append(
                        {"role": role, "content": [{"type": content_type, "text": content}]}
                    )
                for call in message.get("tool_calls") or []:
                    items.append(
                        {
                            "type": "function_call",
                            "call_id": call["id"],
                            "name": call["name"],
                            "arguments": json.dumps(call.get("arguments") or {}, ensure_ascii=True),
                        }
                    )
        if reasoning_token_estimates is not None and role == "assistant":
            usage = provider_data.get("usage")
            if isinstance(usage, dict):
                tokens = usage.get("reasoning_tokens")
                output_tokens = usage.get("output_tokens")
                if (
                    type(tokens) is int and type(output_tokens) is int
                    and 0 <= tokens <= output_tokens
                ):
                    opaque_indices = [
                        position for position in range(first_item, len(items))
                        if items[position].get("type") == "reasoning"
                        and items[position].get("encrypted_content")
                    ]
                    if opaque_indices:
                        # Usage belongs to the source response, not each replay item. Allow
                        # extra opaque framing; this remains an estimate, not a token bound.
                        reserve = (tokens + 256 + len(opaque_indices) - 1) // len(opaque_indices)
                        reasoning_token_estimates.update(
                            (position, reserve) for position in opaque_indices
                        )
        message_media = grouped_media.get(index)
        if role == "tool":
            if message_media:
                pending_tool_media.append(message_media)
            next_is_tool = index + 1 < len(messages) and messages[index + 1]["role"] == "tool"
            if not next_is_tool:
                for values in pending_tool_media:
                    append_media(values, source_role="tool")
                pending_tool_media.clear()
        elif message_media:
            append_media(message_media, source_role=role)
    return "\n\n".join(instructions), items


def _responses_replay_items(raw_output: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_output, list):
        return []
    replay: list[dict[str, Any]] = []
    for item in raw_output:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "reasoning" and item.get("encrypted_content"):
            value: dict[str, Any] = {
                "type": "reasoning",
                "summary": item["summary"] if isinstance(item.get("summary"), list) else [],
                "encrypted_content": item["encrypted_content"],
            }
            if item.get("id"):
                value["id"] = item["id"]
            replay.append(value)
        elif item_type == "function_call":
            replay.append(
                {
                    "type": "function_call",
                    "call_id": item.get("call_id") or item.get("id"),
                    "name": item.get("name") or "",
                    "arguments": item.get("arguments") or "{}",
                }
            )
        elif item_type == "message":
            content = [
                {"type": "output_text", "text": part.get("text") or ""}
                for part in item.get("content") or []
                if isinstance(part, dict) and part.get("type") == "output_text"
            ]
            if content:
                replay.append({"role": "assistant", "content": content})
    return replay


def _responses_body(
    request: ProviderRequest, extra_body: dict[str, Any] | None = None,
    *,
    reasoning_token_estimates: dict[int, int] | None = None,
) -> dict[str, Any]:
    instructions, input_items = _responses_input(
        request.messages, request.continuation_scope, request.media,
        reasoning_token_estimates=reasoning_token_estimates,
    )
    body: dict[str, Any] = {
        "model": request.model,
        "input": input_items,
        "instructions": instructions,
        "stream": request.stream,
        "store": False,
    }
    if request.max_output_tokens is not None:
        body["max_output_tokens"] = request.max_output_tokens
    if request.temperature is not None:
        body["temperature"] = request.temperature
    if request.tools:
        body["tools"] = [
            {"type": "function", **tool, "strict": False} for tool in request.tools
        ]
        body["tool_choice"] = request.tool_choice
        if request.supports_parallel_tool_calls:
            body["parallel_tool_calls"] = True
    reasoning = {}
    if request.reasoning_summary:
        reasoning["summary"] = "auto"
    if request.reasoning_effort is not None:
        reasoning["effort"] = request.reasoning_effort.value
    if reasoning:
        body["reasoning"] = reasoning
    if request.prompt_cache_key:
        body["prompt_cache_key"] = request.prompt_cache_key
    body["include"] = ["reasoning.encrypted_content"]
    if extra_body:
        body.update(extra_body)
    return body


def _normalize_usage(raw_usage: Any) -> dict[str, int]:
    if not isinstance(raw_usage, dict):
        return {}
    aliases = {
        "input_tokens": ("input_tokens", "prompt_tokens"),
        "output_tokens": ("output_tokens", "completion_tokens"),
        "total_tokens": ("total_tokens",),
    }
    usage: dict[str, int] = {}
    for normalized, keys in aliases.items():
        value = next((raw_usage.get(key) for key in keys if isinstance(raw_usage.get(key), int)), None)
        if value is not None:
            usage[normalized] = value
    output_details = raw_usage.get("output_tokens_details") or raw_usage.get(
        "completion_tokens_details"
    )
    if isinstance(output_details, dict) and isinstance(output_details.get("reasoning_tokens"), int):
        usage["reasoning_tokens"] = output_details["reasoning_tokens"]
    input_details = raw_usage.get("input_tokens_details") or raw_usage.get(
        "prompt_tokens_details"
    )
    if isinstance(input_details, dict) and isinstance(input_details.get("cached_tokens"), int):
        usage["cached_input_tokens"] = input_details["cached_tokens"]
    if isinstance(input_details, dict) and isinstance(
        input_details.get("cache_write_tokens"), int
    ):
        usage["cache_write_input_tokens"] = input_details["cache_write_tokens"]
    return usage


def _tool_call(id_: Any, name: Any, arguments: Any) -> ToolCall:
    if isinstance(arguments, dict):
        parsed_arguments = arguments
    else:
        try:
            parsed_arguments = json.loads(arguments or "{}")
        except (json.JSONDecodeError, TypeError):
            parsed_arguments = {"_raw": str(arguments or "")}
    return ToolCall(id=str(id_ or ""), name=str(name or ""), arguments=parsed_arguments)


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
            return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def _safe_provider_text(value: Any, *, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    cleaned = _DATA_URL.sub("[data URL redacted]", cleaned)
    cleaned = _BEARER_TOKEN.sub("Bearer [redacted]", cleaned)
    cleaned = _URL_QUERY.sub(r"\1?[query redacted]", cleaned)
    if _SENSITIVE_PROVIDER_TEXT.search(cleaned):
        return "[sensitive provider message redacted]"
    return cleaned[:limit] or None


def _provider_error_code(error: Any) -> str | None:
    if not isinstance(error, dict):
        return None
    if any(
        error.get(key) in (
            "context_length_exceeded", "context_window_exceeded", "exceed_context_size_error"
        )
        for key in ("code", "type")
    ):
        return "agent.contextWindowExceeded"
    if error.get("code") == "rate_limit_exceeded":
        return "provider.rateLimited"
    return None


async def _ensure_success(response: httpx.Response) -> None:
    if response.status_code < 400:
        return
    await response.aread()
    error: Any = None
    non_json_message: str | None = None
    try:
        payload = response.json()
        if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
            error = payload["error"]
    except (json.JSONDecodeError, UnicodeDecodeError):
        non_json_message = response.text
    error_type = error.get("type") if isinstance(error, dict) else None
    error_code = error.get("code") if isinstance(error, dict) else None
    error_param = error.get("param") if isinstance(error, dict) else None
    error_message = error.get("message") if isinstance(error, dict) else non_json_message
    try:
        endpoint = response.request.url.path
    except RuntimeError:
        endpoint = None
    request_id = next(
        (
            value
            for header in _PROVIDER_REQUEST_ID_HEADERS
            if (value := _safe_provider_text(response.headers.get(header), limit=200))
        ),
        None,
    )
    logger.warning(
        "Provider request failed (status=%s, endpoint=%r, type=%r, code=%r, "
        "param=%r, request_id=%r, message=%r)",
        response.status_code,
        endpoint,
        _safe_provider_text(error_type, limit=80),
        _safe_provider_text(error_code, limit=80),
        _safe_provider_text(error_param, limit=80),
        request_id,
        _safe_provider_text(error_message, limit=500),
    )
    raise ProviderError(
        f"Provider request failed with HTTP {response.status_code}",
        status_code=response.status_code,
        code=_provider_error_code(error),
        retry_after=_retry_after(response),
    )


async def discover_models(
    *,
    base_url: str,
    api_key: str | None,
    timeout: float = 30,
) -> list[DiscoveredModel]:
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(f"{base_url.rstrip('/')}/models", headers=headers)
            await _ensure_success(response)
    except httpx.RequestError as exc:
        raise ProviderError(
            "Provider model discovery network request failed", code="provider.unavailable"
        ) from exc
    try:
        parsed = _ModelListResponse.model_validate(response.json())
    except (ValidationError, ValueError, TypeError, UnicodeDecodeError) as exc:
        raise ProviderError(
            "Provider returned an invalid model list", code="provider.invalidResponse"
        ) from exc
    return parsed.data


def _response_json(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ProviderError("Provider returned invalid JSON", code="provider.invalidResponse") from exc
    if not isinstance(data, dict):
        raise ProviderError("Provider returned invalid JSON", code="provider.invalidResponse")
    return data


def _reasoning_summary(item: dict[str, Any]) -> str:
    parts = item.get("summary") or []
    if not isinstance(parts, list):
        return ""
    return "\n\n".join(
        str(part.get("text") or "").strip()
        for part in parts
        if isinstance(part, dict)
        and part.get("type") in {"summary_text", "text"}
        and str(part.get("text") or "").strip()
    )


def _public_reasoning(
    text: str,
    *,
    kind: str,
    usage: dict[str, int],
    observed_duration_ms: int | None = None,
) -> PublicReasoning | None:
    value = text.strip()
    if not value:
        return None
    return PublicReasoning(
        text=value,
        kind=kind,
        reasoning_tokens=usage.get("reasoning_tokens"),
        observed_duration_ms=observed_duration_ms,
    )


def _normalize_finish_reason(value: Any) -> str | None:
    if value is None:
        return None
    reason = str(value)
    if reason in {"length", "max_tokens", "max_output_tokens"}:
        return "length"
    return reason


def _chat_result(data: dict[str, Any]) -> ProviderResult:
    choices = data.get("choices") or []
    choice = choices[0] if choices and isinstance(choices[0], dict) else {}
    message = choice.get("message") or {}
    raw_content = message.get("content") or ""
    content, tagged_reasoning = split_tagged_reasoning(
        raw_content if isinstance(raw_content, str) else "", final=True
    )
    explicit_reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
    reasoning_text = "\n\n".join(
        item for item in (explicit_reasoning, tagged_reasoning) if isinstance(item, str) and item.strip()
    )
    usage = _normalize_usage(data.get("usage"))
    tool_calls = []
    for item in message.get("tool_calls") or []:
        function = item.get("function") or {}
        tool_calls.append(_tool_call(item.get("id"), function.get("name"), function.get("arguments")))
    provider_data = {}
    if isinstance(message.get("reasoning_details"), list):
        provider_data["chat_reasoning_details"] = message["reasoning_details"]
    return ProviderResult(
        content=content,
        tool_calls=tool_calls,
        provider_data=provider_data,
        usage=usage,
        finish_reason=_normalize_finish_reason(choice.get("finish_reason")),
        reasoning=_public_reasoning(reasoning_text, kind="content", usage=usage),
    )


def _responses_finish_reason(data: dict[str, Any]) -> str | None:
    incomplete_reason = (data.get("incomplete_details") or {}).get("reason")
    if incomplete_reason == "max_output_tokens":
        return "length"
    if incomplete_reason:
        return str(incomplete_reason)
    status = data.get("status")
    if status == "completed":
        return "stop"
    return str(status) if status else None


def _failed_response_error(data: dict[str, Any]) -> ProviderError:
    error = data.get("error")
    details = error if isinstance(error, dict) else {"message": error}
    code = _provider_error_code(error)
    raw_code = details.get("code")
    status_code = raw_code if type(raw_code) is int and 400 <= raw_code <= 599 else None
    logger.warning(
        "Provider response failed (type=%r, code=%r, param=%r, message=%r)",
        _safe_provider_text(details.get("type"), limit=80),
        status_code or _safe_provider_text(raw_code, limit=80),
        _safe_provider_text(details.get("param"), limit=80),
        _safe_provider_text(details.get("message"), limit=500),
    )
    return ProviderError(
        "Provider response failed",
        status_code=(
            400 if code == "agent.contextWindowExceeded"
            else 429 if code == "provider.rateLimited"
            else status_code or 503
        ),
        code=code or (None if status_code else "provider.requestFailed"),
    )


def _responses_result(data: dict[str, Any]) -> ProviderResult:
    status = data.get("status")
    if status == "failed":
        raise _failed_response_error(data)
    incomplete_reason = (data.get("incomplete_details") or {}).get("reason")
    if status == "incomplete" and incomplete_reason not in {None, "max_output_tokens"}:
        raise ProviderError("Provider response incomplete", code="provider.invalidResponse")
    content_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    provider_data: dict[str, Any] = {}
    reasoning_summaries: list[str] = []
    tagged_reasoning_parts: list[str] = []
    replay_items = _responses_replay_items(data.get("output"))
    if replay_items:
        provider_data["responses_output_items"] = replay_items
    for item in data.get("output") or []:
        item_type = item.get("type")
        if item_type == "message":
            for part in item.get("content") or []:
                if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                    content, tagged_reasoning = split_tagged_reasoning(part["text"], final=True)
                    content_parts.append(content)
                    if tagged_reasoning:
                        tagged_reasoning_parts.append(tagged_reasoning)
        elif item_type == "function_call":
            tool_calls.append(
                _tool_call(item.get("call_id") or item.get("id"), item.get("name"), item.get("arguments"))
            )
        elif item_type == "reasoning" and item.get("encrypted_content"):
            provider_data["reasoning_encrypted_content"] = item["encrypted_content"]
            summary = _reasoning_summary(item)
            if summary:
                reasoning_summaries.append(summary)
        elif item_type == "reasoning":
            summary = _reasoning_summary(item)
            if summary:
                reasoning_summaries.append(summary)
    usage = _normalize_usage(data.get("usage"))
    return ProviderResult(
        content="".join(content_parts),
        tool_calls=tool_calls,
        provider_data=provider_data,
        usage=usage,
        finish_reason=_responses_finish_reason(data),
        reasoning=_public_reasoning(
            "\n\n".join([*reasoning_summaries, *tagged_reasoning_parts]),
            kind="content" if tagged_reasoning_parts else "summary",
            usage=usage,
        ),
    )


async def chat_completions(
    *,
    base_url: str,
    api_key: str | None,
    request: ProviderRequest,
    on_delta: DeltaCallback | None,
    on_reasoning: ReasoningDeltaCallback | None = None,
) -> ProviderResult:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    body = _chat_body(
        request, direct_thinking_options=_direct_thinking_options(base_url)
    )
    url = f"{base_url.rstrip('/')}/chat/completions"
    async with httpx.AsyncClient(timeout=request.timeout) as client:
        if not request.stream:
            response = await client.post(url, headers=headers, json=body)
            await _ensure_success(response)
            result = _chat_result(_response_json(response))
            if on_reasoning and result.reasoning:
                await on_reasoning(result.reasoning.text, result.reasoning.kind)
            if on_delta and result.content:
                await on_delta(result.content)
            return result

        content_parts: list[str] = []
        explicit_reasoning_parts: list[str] = []
        reasoning_details: list[dict[str, Any]] = []
        splitter = TaggedReasoningStream()
        calls: dict[int, dict[str, str]] = {}
        usage: dict[str, int] = {}
        finish_reason: str | None = None
        reasoning_started_at: float | None = None
        reasoning_finished_at: float | None = None
        async with client.stream("POST", url, headers=headers, json=body) as response:
            await _ensure_success(response)
            async for event in _sse_events(response):
                if event.get("usage"):
                    usage = _normalize_usage(event["usage"])
                choices = event.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                if choice.get("finish_reason"):
                    finish_reason = _normalize_finish_reason(choice["finish_reason"])
                delta = choice.get("delta") or {}
                reasoning_delta = delta.get("reasoning_content") or delta.get("reasoning") or ""
                if isinstance(reasoning_delta, str) and reasoning_delta:
                    if reasoning_started_at is None:
                        reasoning_started_at = time.monotonic()
                    explicit_reasoning_parts.append(reasoning_delta)
                    if on_reasoning:
                        await on_reasoning(reasoning_delta, "content")
                details = delta.get("reasoning_details")
                if isinstance(details, list):
                    reasoning_details.extend(item for item in details if isinstance(item, dict))
                text = delta.get("content")
                if text:
                    answer_delta, tagged_reasoning_delta = splitter.add(str(text))
                    if tagged_reasoning_delta:
                        if reasoning_started_at is None:
                            reasoning_started_at = time.monotonic()
                        if on_reasoning:
                            await on_reasoning(tagged_reasoning_delta, "content")
                    if answer_delta:
                        if reasoning_started_at is not None and reasoning_finished_at is None:
                            reasoning_finished_at = time.monotonic()
                        content_parts.append(answer_delta)
                        if on_delta:
                            await on_delta(answer_delta)
                for fragment in delta.get("tool_calls") or []:
                    index = int(fragment.get("index", 0))
                    call = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                    call["id"] += fragment.get("id") or ""
                    function = fragment.get("function") or {}
                    call["name"] += function.get("name") or ""
                    call["arguments"] += function.get("arguments") or ""

    final_content, tagged_reasoning = splitter.finish()
    if final_content != "".join(content_parts):
        content_parts = [final_content]
    tool_calls = [
        _tool_call(call["id"], call["name"], call["arguments"]) for call in calls.values()
    ]
    reasoning_text = "\n\n".join(
        item for item in ("".join(explicit_reasoning_parts), tagged_reasoning) if item.strip()
    )
    duration_ms = None
    if reasoning_started_at is not None:
        reasoning_finished_at = reasoning_finished_at or time.monotonic()
        duration_ms = round((reasoning_finished_at - reasoning_started_at) * 1000)
    provider_data = {"chat_reasoning_details": reasoning_details} if reasoning_details else {}
    return ProviderResult(
        content="".join(content_parts),
        tool_calls=tool_calls,
        provider_data=provider_data,
        usage=usage,
        finish_reason=finish_reason,
        reasoning=_public_reasoning(
            reasoning_text,
            kind="content",
            usage=usage,
            observed_duration_ms=duration_ms,
        ),
    )


async def responses_api(
    *,
    url: str,
    headers: dict[str, str],
    request: ProviderRequest,
    on_delta: DeltaCallback | None,
    on_reasoning: ReasoningDeltaCallback | None = None,
    extra_body: dict[str, Any] | None = None,
) -> ProviderResult:
    body = _responses_body(request, extra_body)
    async with httpx.AsyncClient(timeout=request.timeout) as client:
        if not request.stream:
            response = await client.post(url, headers=headers, json=body)
            await _ensure_success(response)
            result = _responses_result(_response_json(response))
            if on_reasoning and result.reasoning:
                await on_reasoning(result.reasoning.text, result.reasoning.kind)
            if on_delta and result.content:
                await on_delta(result.content)
            return result

        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        splitter = TaggedReasoningStream()
        calls: dict[str, dict[str, str]] = {}
        provider_data: dict[str, Any] = {}
        usage: dict[str, int] = {}
        finish_reason: str | None = None
        replay_items: list[dict[str, Any]] = []
        reasoning_started_at: float | None = None
        reasoning_finished_at: float | None = None
        async with client.stream("POST", url, headers=headers, json=body) as response:
            await _ensure_success(response)
            async for event in _sse_events(response):
                event_type = event.get("type")
                if event_type == "response.output_text.delta":
                    text = event.get("delta") or ""
                    answer_delta, tagged_reasoning_delta = splitter.add(str(text))
                    if tagged_reasoning_delta:
                        if reasoning_started_at is None:
                            reasoning_started_at = time.monotonic()
                        if on_reasoning:
                            await on_reasoning(tagged_reasoning_delta, "content")
                    if answer_delta:
                        if reasoning_started_at is not None and reasoning_finished_at is None:
                            reasoning_finished_at = time.monotonic()
                        content_parts.append(answer_delta)
                        if on_delta:
                            await on_delta(answer_delta)
                elif event_type == "response.reasoning_summary_part.added":
                    if reasoning_parts:
                        reasoning_parts.append("\n\n")
                        if on_reasoning:
                            await on_reasoning("\n\n", "summary")
                elif event_type == "response.reasoning_summary_text.delta":
                    text = event.get("delta") or ""
                    if isinstance(text, str) and text:
                        if reasoning_started_at is None:
                            reasoning_started_at = time.monotonic()
                        reasoning_parts.append(text)
                        if on_reasoning:
                            await on_reasoning(text, "summary")
                elif event_type == "response.output_item.added":
                    item = event.get("item") or {}
                    if item.get("type") == "function_call":
                        key = item.get("id") or item.get("call_id")
                        calls[str(key)] = {
                            "id": item.get("call_id") or str(key),
                            "name": item.get("name") or "",
                            "arguments": item.get("arguments") or "",
                        }
                    elif item.get("type") == "reasoning" and item.get("encrypted_content"):
                        provider_data["reasoning_encrypted_content"] = item["encrypted_content"]
                elif event_type == "response.function_call_arguments.delta":
                    key = str(event.get("item_id") or event.get("call_id"))
                    call = calls.setdefault(
                        key,
                        {
                            "id": event.get("call_id") or key,
                            "name": event.get("name") or "",
                            "arguments": "",
                        },
                    )
                    call["arguments"] += event.get("delta") or ""
                elif event_type == "response.output_item.done":
                    item = event.get("item") or {}
                    replay_items.extend(_responses_replay_items([item]))
                    if item.get("type") == "function_call":
                        key = str(item.get("id") or item.get("call_id"))
                        calls[key] = {
                            "id": item.get("call_id") or key,
                            "name": item.get("name") or "",
                            "arguments": item.get("arguments")
                            or calls.get(key, {}).get("arguments", ""),
                        }
                    elif item.get("type") == "reasoning" and item.get("encrypted_content"):
                        provider_data["reasoning_encrypted_content"] = item["encrypted_content"]
                elif event_type in {
                    "response.completed",
                    "response.incomplete",
                    "response.failed",
                }:
                    response_data = event.get("response") or {}
                    if event_type == "response.failed":
                        raise _failed_response_error(response_data)
                    incomplete_reason = (response_data.get("incomplete_details") or {}).get(
                        "reason"
                    )
                    if (
                        event_type == "response.incomplete"
                        and incomplete_reason not in {None, "max_output_tokens"}
                    ):
                        raise ProviderError(
                            "Provider response incomplete", code="provider.invalidResponse"
                        )
                    usage = _normalize_usage(response_data.get("usage"))
                    finish_reason = _responses_finish_reason(response_data)
                    completed_replay = _responses_replay_items(response_data.get("output"))
                    if completed_replay:
                        replay_items = completed_replay
                    completed_summaries = [
                        _reasoning_summary(item)
                        for item in response_data.get("output") or []
                        if isinstance(item, dict) and item.get("type") == "reasoning"
                    ]
                    if any(completed_summaries):
                        reasoning_parts = ["\n\n".join(item for item in completed_summaries if item)]
                    break

    final_content, tagged_reasoning = splitter.finish()
    if final_content != "".join(content_parts):
        content_parts = [final_content]
    tool_calls = [
        _tool_call(call["id"], call["name"], call["arguments"]) for call in calls.values()
    ]
    if replay_items:
        provider_data["responses_output_items"] = replay_items
    reasoning_text = "\n\n".join(
        item for item in ("".join(reasoning_parts), tagged_reasoning) if item.strip()
    )
    duration_ms = None
    if reasoning_started_at is not None:
        reasoning_finished_at = reasoning_finished_at or time.monotonic()
        duration_ms = round((reasoning_finished_at - reasoning_started_at) * 1000)
    return ProviderResult(
        content="".join(content_parts),
        tool_calls=tool_calls,
        provider_data=provider_data,
        usage=usage,
        finish_reason=finish_reason,
        reasoning=_public_reasoning(
            reasoning_text,
            kind="summary" if reasoning_parts else "content",
            usage=usage,
            observed_duration_ms=duration_ms,
        ),
    )
