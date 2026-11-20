from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from mato_loop_backend.error_contract import safe_provider_error_code
from mato_loop_backend.settings.models import ReasoningEffort

DeltaCallback = Callable[[str], Awaitable[None]]
ReasoningDeltaCallback = Callable[[str, str], Awaitable[None]]

MAX_PROVIDER_IMAGES = 8
MAX_PROVIDER_IMAGE_BYTES = 20 * 1024 * 1024
MAX_PROVIDER_AUDIO_BYTES = 25 * 1024 * 1024
MAX_PROVIDER_VIDEO_BYTES = 50 * 1024 * 1024
MAX_PROVIDER_MEDIA_INPUTS = 4


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class PublicReasoning:
    text: str
    kind: str = "content"
    status: str = "completed"
    reasoning_tokens: int | None = None
    observed_duration_ms: int | None = None


@dataclass(slots=True)
class ProviderMedia:
    message_index: int
    mime_type: str
    data_url: str | None = field(default=None, repr=False)
    status: str = "unavailable"
    modality: Literal["image", "audio", "video"] = "image"
    data_base64: str | None = field(default=None, repr=False)
    format: str | None = None


@dataclass(slots=True)
class ProviderImageInput:
    message_index: int
    data: bytes = field(repr=False)
    trust_level: Literal["public", "private"]
    mime_type: str = "image/jpeg"
    filename: str | None = None


@dataclass(slots=True)
class ProviderMediaInput:
    message_index: int
    data: bytes = field(repr=False)
    mime_type: str
    trust_level: Literal["public", "private"]
    modality: Literal["audio", "video"]


@dataclass(slots=True)
class ProviderImageSelection:
    message_index: int
    attachment: dict[str, Any]
    allow_raw: bool


def _references_value(
    text: str, value: str, *, boundary_characters: str | None = None
) -> bool:
    if not value:
        return False
    if boundary_characters is None:
        return value in text
    return (
        re.search(
            rf"(?<![{boundary_characters}]){re.escape(value)}(?![{boundary_characters}])",
            text,
        )
        is not None
    )


def select_provider_images(
    messages: list[dict[str, Any]],
    *,
    target_trust_level: str,
    required_image_ids: tuple[str, ...] = (),
    allowed_categories: frozenset[str] | None = None,
    max_images: int = MAX_PROVIDER_IMAGES,
    max_bytes: int = MAX_PROVIDER_IMAGE_BYTES,
) -> list[ProviderImageSelection]:
    references: list[tuple[int, int, dict[str, Any]]] = []
    for message_index, message in enumerate(messages):
        for attachment_index, attachment in enumerate(message.get("attachments") or []):
            if (
                isinstance(attachment, dict)
                and attachment.get("kind") == "image"
                and (
                    allowed_categories is None
                    or attachment.get("category") in allowed_categories
                )
            ):
                references.append((message_index, attachment_index, attachment))
    if not references:
        return []

    latest_user_index = max(
        (index for index, message in enumerate(messages) if message.get("role") == "user"),
        default=-1,
    )
    reference_texts = [
        (index, str(message.get("content") or "")[message.get("image_reference_offset", 0):])
        for index, message in enumerate(messages)
        if index == latest_user_index
        or (index > latest_user_index and message.get("role") == "tool")
    ]
    filename_counts: dict[str, int] = {}
    for _message_index, _attachment_index, attachment in references:
        filename = attachment.get("filename")
        if isinstance(filename, str) and filename:
            filename_counts[filename] = filename_counts.get(filename, 0) + 1

    required_order = {
        attachment_id: order for order, attachment_id in enumerate(required_image_ids)
    }
    candidates: list[tuple[int, int, int, int, dict[str, Any]]] = []
    for message_index, attachment_index, attachment in references:
        source_role = str(attachment.get("source_role") or messages[message_index].get("role") or "")
        historical = bool(attachment.get("historical")) or message_index < latest_user_index
        source_order = attachment.get("source_order")
        if not isinstance(source_order, int) or isinstance(source_order, bool):
            source_order = message_index
        attachment_id = attachment.get("id")
        if isinstance(attachment_id, str) and attachment_id in required_order:
            candidates.append(
                (-1, required_order[attachment_id], message_index, source_order, attachment)
            )
            continue
        if not historical and source_role == "user" and message_index == latest_user_index:
            candidates.append((0, attachment_index, message_index, source_order, attachment))
            continue
        if not historical and source_role == "tool" and message_index > latest_user_index:
            candidates.append((1, -source_order, message_index, source_order, attachment))
            continue

        file_path = attachment.get("file_path")
        filename = attachment.get("filename")
        matching_indices = [
            index
            for index, text in reference_texts
            if (
                isinstance(attachment_id, str)
                and _references_value(text, attachment_id, boundary_characters=r"\w-")
            )
            or (
                isinstance(file_path, str)
                and _references_value(text, file_path, boundary_characters=r"\w./-")
            )
            or (
                isinstance(filename, str)
                and filename_counts.get(filename) == 1
                and _references_value(text, filename, boundary_characters=r"\w.-")
            )
        ]
        if matching_indices:
            candidates.append((2, -source_order, max(matching_indices), source_order, attachment))

    candidates.sort(key=lambda item: (item[0], item[1]))
    selected: list[tuple[int, int, int, ProviderImageSelection]] = []
    selected_bytes = 0
    for group, _priority, message_index, source_order, attachment in candidates:
        if len(selected) >= max_images:
            break
        size_bytes = attachment.get("size_bytes")
        if (
            not isinstance(size_bytes, int)
            or isinstance(size_bytes, bool)
            or size_bytes <= 0
            or selected_bytes + size_bytes > max_bytes
        ):
            continue
        selected_bytes += size_bytes
        selected.append(
            (
                group,
                message_index,
                source_order,
                ProviderImageSelection(message_index, attachment, True),
            )
        )

    selected.sort(key=lambda item: (item[0], item[1], item[2]))
    return [item[3] for item in selected]


@dataclass(slots=True, repr=False)
class ProviderTurnState:
    binding: tuple[str | None, ...] | None = None
    routing_token: str | None = None


@dataclass(slots=True)
class ProviderRequest:
    model: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] = field(default_factory=list)
    tool_choice: Literal["auto", "required", "none"] = "auto"
    stream: bool = True
    include_usage: bool = False
    supports_parallel_tool_calls: bool = False
    max_output_tokens: int | None = None
    temperature: float | None = None
    timeout: float = 300
    reasoning_summary: bool = False
    reasoning_effort: ReasoningEffort | None = None
    enable_thinking: bool | None = None
    prompt_cache_key: str | None = None
    service_tier: str | None = None
    continuation_scope: dict[str, str | None] = field(default_factory=dict)
    media: list[ProviderMedia] = field(default_factory=list)
    configuration_updates: dict[int, str] = field(default_factory=dict)
    reasoning_configuration: dict[str, Any] | None = None
    turn_state: ProviderTurnState | None = field(default=None, repr=False, compare=False)


@dataclass(slots=True)
class ProviderResult:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    provider_data: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, int] = field(default_factory=dict)
    finish_reason: str | None = None
    reasoning: PublicReasoning | None = None
    restore_script_arguments: Callable[[dict[str, Any]], dict[str, Any]] | None = field(
        default=None, repr=False, compare=False,
    )


class ProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
        retry_after: float | None = None,
        params: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.retry_after = retry_after
        self.params = params

    @property
    def public_code(self) -> str:
        return safe_provider_error_code(self.code, self.status_code)
