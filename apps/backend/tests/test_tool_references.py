import json
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

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


def test_model_tool_references_replace_only_caldav_tokens() -> None:
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
    ],
)
async def test_resolve_tool_references_restores_canonical_token(
    source_tool: str,
    target_tool: str,
    container: str,
    token_key: str,
    field: str,
    kind: str,
) -> None:
    item = {token_key: "encrypted-token"}
    source = _message(
        source_tool,
        {container: [item] if source_tool == "tasks_create_many" else item},
    )
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = source

    resolved = await resolve_tool_references(
        target_tool,
        {field: f"{kind}:{source.id}:0", "other": "value"},
        db,
        source.session_id,
    )

    assert resolved == {field: "encrypted-token", "other": "value"}


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
