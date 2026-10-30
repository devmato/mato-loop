import json
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend.agent.run_support import _safe_tool_arguments
from mato_loop_backend.models import Message
from mato_loop_backend.settings import TrustLevel
from mato_loop_backend.tools.references import (
    InvalidToolReference,
    model_tool_references,
    resolve_tool_references,
)


def _message(tool_name: str, content: dict) -> Message:
    return Message(
        id=uuid4(),
        session_id=uuid4(),
        role="tool",
        content=json.dumps(content),
        trust_level=TrustLevel.PUBLIC.value,
        provider_data={"_matoloop": {"toolName": tool_name}},
    )


def test_model_tool_references_replace_registered_tokens_only() -> None:
    message_id = uuid4()
    content = json.dumps(
        {
            "tasks": [
                {"title": "First", "taskToken": "encrypted-1"},
                {"title": "Second", "taskToken": "encrypted-2"},
            ]
        }
    )

    projected = json.loads(model_tool_references("tasks_query", content, message_id))

    assert [item["taskToken"] for item in projected["tasks"]] == [
        f"task:{message_id}:0",
        f"task:{message_id}:1",
    ]
    assert json.loads(content)["tasks"][0]["taskToken"] == "encrypted-1"
    assert model_tool_references("webfetch", content, message_id) == content


@pytest.mark.parametrize(
    ("source_tool", "target_tool", "container", "token_key", "field", "kind"),
    [
        (
            "calendar_create_event",
            "calendar_delete_event",
            "event",
            "eventToken",
            "event_token",
            "event",
        ),
        (
            "tasks_create_many",
            "tasks_update",
            "created",
            "taskToken",
            "task_token",
            "task",
        ),
        ("mail_search", "mail_get_message", "messages", "messageToken", "message_token", "mail"),
        ("mail_get_message", "mail_delete_message", "message", "messageToken", "message_token", "mail"),
        ("mail_create_draft", "mail_update_draft", None, "messageToken", "message_token", "mail"),
        ("mail_update_draft", "mail_send_draft", None, "messageToken", "message_token", "mail"),
        ("mail_create_draft", "mail_delete_draft", None, "messageToken", "message_token", "mail"),
    ],
)
async def test_resolve_tool_references_restores_canonical_token(
    source_tool: str,
    target_tool: str,
    container: str | None,
    token_key: str,
    field: str,
    kind: str,
) -> None:
    item = {token_key: "encrypted-token"}
    payload = [item] if source_tool in {"tasks_create_many", "mail_search"} else item
    source = _message(source_tool, {container: payload} if container else item)
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = source
    projected = json.loads(model_tool_references(source_tool, source.content, source.id))
    projected_item = projected[container] if container else projected
    if isinstance(projected_item, list):
        projected_item = projected_item[0]

    arguments = _safe_tool_arguments(
        target_tool, {field: projected_item[token_key], "other": "value"},
    )
    resolved = await resolve_tool_references(
        target_tool,
        arguments,
        db,
        source.session_id,
    )

    assert (arguments[field], resolved) == (
        f"{kind}:{source.id}:0", {field: "encrypted-token", "other": "value"},
    )


async def test_resolve_tool_references_preserves_full_tokens() -> None:
    db = AsyncMock(spec=AsyncSession)
    arguments = {"task_token": "encrypted-token"}

    assert await resolve_tool_references(
        "tasks_delete", arguments, db, uuid4()
    ) is arguments
    db.scalar.assert_not_awaited()


async def test_resolve_tool_references_rejects_missing_or_wrong_sources() -> None:
    source = _message("webfetch", {"tasks": [{"taskToken": "encrypted-token"}]})
    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = [None, source]
    reference = f"task:{source.id}:0"

    with pytest.raises(InvalidToolReference, match="task_token"):
        await resolve_tool_references(
            "tasks_delete",
            {"task_token": reference},
            db,
            source.session_id,
        )
    with pytest.raises(InvalidToolReference, match="task_token"):
        await resolve_tool_references(
            "tasks_delete",
            {"task_token": reference},
            db,
            source.session_id,
        )
