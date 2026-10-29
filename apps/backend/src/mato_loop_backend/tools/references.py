from __future__ import annotations

import json
import re
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend.models import Message

_REFERENCE = re.compile(
    r"^(event|task):([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}):(\d{1,3})$"
)
_SOURCES = {
    "calendar_query_events": ("event", "events", "eventToken", True),
    "calendar_create_event": ("event", "event", "eventToken", False),
    "calendar_update_event": ("event", "event", "eventToken", False),
    "tasks_query": ("task", "tasks", "taskToken", True),
    "tasks_create": ("task", "task", "taskToken", False),
    "tasks_create_many": ("task", "created", "taskToken", True),
    "tasks_update": ("task", "task", "taskToken", False),
    "tasks_set_completed": ("task", "task", "taskToken", False),
}
_TARGETS = {
    "calendar_update_event": ("event", "event_token"),
    "calendar_delete_event": ("event", "event_token"),
    "tasks_update": ("task", "task_token"),
    "tasks_set_completed": ("task", "task_token"),
    "tasks_delete": ("task", "task_token"),
}


class InvalidToolReference(ValueError):
    def __init__(self, field: str) -> None:
        super().__init__(field)
        self.field = field


def model_secret_references(content: str, available_references: set[str]) -> str:
    """Expose only temporary references still available in the calling run or session."""
    try:
        payload = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return content
    if not isinstance(payload, dict) or not isinstance(payload.get("environment"), dict):
        return content
    environment = payload["environment"]
    return json.dumps({
        "environment": {name: ref for name, ref in environment.items() if isinstance(ref, str) and ref in available_references},
        "unavailableCredentialNames": [name for name, ref in environment.items() if not isinstance(ref, str) or ref not in available_references],
        "instruction": "Use available references unchanged. Call secret_input for unavailable credentials; reading history does not restore them.",
    }, ensure_ascii=False)


def model_tool_references(
    tool_name: str | None, content: str, message_id: UUID
) -> str:
    source = _SOURCES.get(tool_name or "")
    if source is None:
        return content
    kind, container_key, token_key, multiple = source
    try:
        payload = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return content
    if not isinstance(payload, dict):
        return content
    container = payload.get(container_key)
    if multiple and not isinstance(container, list):
        return content
    if not multiple and not isinstance(container, dict):
        return content
    items = container if multiple else [container]
    changed = False
    for index, item in enumerate(items):
        if isinstance(item, dict) and isinstance(item.get(token_key), str):
            item[token_key] = f"{kind}:{message_id}:{index}"
            changed = True
    return (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if changed
        else content
    )


async def resolve_tool_references(
    tool_name: str,
    arguments: dict[str, Any],
    db: AsyncSession,
    session_id: UUID,
) -> dict[str, Any]:
    target = _TARGETS.get(tool_name)
    if target is None:
        return arguments
    expected_kind, field = target
    value = arguments.get(field)
    match = _REFERENCE.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        return arguments
    kind, message_id, index_text = match.groups()
    if kind != expected_kind:
        raise InvalidToolReference(field)
    filters = [
        Message.id == UUID(message_id),
        Message.session_id == session_id,
        Message.role == "tool",
        Message.error_code.is_(None),
    ]
    source_message = await db.scalar(select(Message).where(*filters))
    if source_message is None:
        raise InvalidToolReference(field)
    metadata = (source_message.provider_data or {}).get("_matoloop", {})
    source = _SOURCES.get(metadata.get("toolName"))
    if source is None or source[0] != kind:
        raise InvalidToolReference(field)
    _kind, container_key, token_key, multiple = source
    try:
        payload = json.loads(source_message.content)
    except (TypeError, json.JSONDecodeError) as exc:
        raise InvalidToolReference(field) from exc
    container = payload.get(container_key) if isinstance(payload, dict) else None
    index = int(index_text)
    item = (
        container[index]
        if multiple and isinstance(container, list) and index < len(container)
        else container if not multiple and index == 0 else None
    )
    token = item.get(token_key) if isinstance(item, dict) else None
    if not isinstance(token, str):
        raise InvalidToolReference(field)
    return {**arguments, field: token}
