from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import selectinload

import mato_loop_backend.tools.access as tools_access
from mato_loop_backend.agent.projection import (
    SESSION_TOOL_RESULT_CHUNK_CHARACTERS,
    _model_file_tool_result,
    _sanitize_tool_result_reference_fields,
    _shorten_evidence_output,
    _target_tool_result,
    _tool_names_by_message_id,
    _tool_result_page,
    _tool_result_reference,
    command_message_content,
    tool_execution_binding,
)
from mato_loop_backend.agent.session_secrets import session_secrets
from mato_loop_backend.media.attachments import model_attachments
from mato_loop_backend.models import AgentRun, ChatSession, Message
from mato_loop_backend.permissions.policy import (
    session_browse_permission,
    session_permission,
    session_search_permission,
)
from mato_loop_backend.sessions.trust import session_visibility_filters
from mato_loop_backend.settings import TrustLevel
from mato_loop_backend.tools.actions import (
    _SESSION_READ,
    _SESSION_SEARCH,
    _SESSION_TOOL_RESULT_READ,
)
from mato_loop_backend.tools.arguments import (
    SessionBrowseArguments,
    SessionReadArguments,
    SessionSearchArguments,
    SessionToolResultReadArguments,
)
from mato_loop_backend.tools.context import ToolContext as ToolContext
from mato_loop_backend.tools.contracts import ToolDefinition as ToolDefinition
from mato_loop_backend.tools.contracts import ToolResult as ToolResult
from mato_loop_backend.tools.references import model_tool_references


def _available_secret_references(context: ToolContext) -> set[str]:
    return {
        *(f"run-secret:{secret_id}" for secret_id in context.one_time_secrets),
        *session_secrets.references(context.session.id).values(),
    }


def _attachment_items(message: Message) -> list[dict[str, Any]]:
    return [
        {
            "root": "files",
            "filename": attachment.filename,
            "kind": attachment.kind,
            "mimeType": attachment.mime_type,
            "category": attachment.category,
            "processingStatus": attachment.processing_status,
            "path": attachment.file_path,
            "fileId": str(attachment.file_id) if attachment.file_id else None,
            "fileRevision": attachment.file_revision,
        }
        for attachment in model_attachments(message, include_pending=False)
    ]


def _message_item(
    message: Message,
    remaining: int,
    tool_names: dict[UUID, str],
    target_trust_level: TrustLevel,
    *,
    reference_tool_results: bool,
    available_secret_references: set[str] | None = None,
) -> tuple[dict[str, Any], int]:
    content_limit = min(SESSION_TOOL_RESULT_CHUNK_CHARACTERS, max(0, remaining))
    content = command_message_content(message)
    if message.role == "tool":
        content = _target_tool_result(
            tool_names.get(message.id), message.content, target_trust_level,
            available_secret_references=available_secret_references or set(),
        )
        content = _model_file_tool_result(content)
    if message.role == "tool":
        content = _sanitize_tool_result_reference_fields(content)
        content = model_tool_references(tool_names.get(message.id), content, message.id)
    full_content = content
    if len(full_content) > content_limit and message.role == "tool":
        content = (_shorten_evidence_output(full_content, content_limit) or full_content)[:content_limit]
    else:
        content = full_content[:content_limit]
    item = {
        "id": str(message.id),
        "role": message.role,
        "content": content,
        "generationStatus": message.generation_status,
        "createdAt": message.created_at.isoformat() if message.created_at else None,
        **tool_execution_binding(message),
    }
    if len(full_content) > content_limit:
        item["truncated"] = True
        if message.role == "tool" and reference_tool_results:
            item["toolResultReference"] = _tool_result_reference(message.id, full_content)
    if message.tool_call_id:
        item["toolCallId"] = message.tool_call_id
        if tool_name := tool_names.get(message.id):
            item["toolName"] = tool_name
    if message.tool_calls:
        item["toolCalls"] = [
            {"id": call.get("id"), "name": call.get("name")} for call in message.tool_calls
        ]
    if attachments := _attachment_items(message):
        item["attachments"] = attachments
    return item, remaining - len(content)


async def _session_tool_result_read(
    arguments: SessionToolResultReadArguments, context: ToolContext
) -> ToolResult:
    filters = [
        Message.id == arguments.message_id,
        Message.session_id == context.session.id,
        Message.role == "tool",
        Message.error_code.is_(None),
    ]
    message = await context.db.scalar(
        select(Message).options(selectinload(Message.attachments)).where(*filters)
    )
    if message is None:
        return ToolResult.error("session.messageNotFound")

    records = list(
        (
            await context.db.scalars(
                select(Message)
                .where(Message.session_id == context.session.id)
                .order_by(Message.created_at, Message.id)
            )
        ).all()
    )
    tool_name = _tool_names_by_message_id(records).get(message.id)
    if tool_name is None:
        return ToolResult.error("session.messageNotFound")
    payload = _tool_result_page(
        message, tool_name, arguments.offset, context.memory_trust_level,
        available_secret_references=_available_secret_references(context),
    )
    if attachments := _attachment_items(message):
        payload["attachments"] = attachments
    return ToolResult.json(payload)


async def _session_read(arguments: SessionReadArguments, context: ToolContext) -> ToolResult:
    target_id = arguments.session_id or context.session.id
    target = None
    permission = session_permission(target_id)
    if context.model_trust_level == TrustLevel.PUBLIC and target_id != context.session.id:
        target, permission = await session_read_permission(context, target_id)
        if target is None:
            return ToolResult.error("session.notFound")
    if not await tools_access.allow(context, permission):
        return ToolResult.error("permission.denied")
    if (
        arguments.after_message_id is not None
        and target_id in context.session_read_cursors
        and arguments.after_message_id != context.session_read_cursors[target_id]
    ):
        retry_arguments = arguments.model_dump(mode="json", exclude_none=True)
        expected = context.session_read_cursors[target_id]
        if expected is None:
            retry_arguments.pop("after_message_id")
        else:
            retry_arguments["after_message_id"] = str(expected)
        return ToolResult.error(
            "tool.cursorInvalid",
            field="after_message_id",
            params={"retryArguments": retry_arguments},
        )
    if target is None:
        target, _permission = await session_read_permission(context, target_id)
        if target is None:
            return ToolResult.error("session.notFound")
    records = list(
        (
            await context.db.scalars(
                select(Message)
                .options(selectinload(Message.attachments))
                .where(Message.session_id == target_id, Message.error_code.is_(None))
                .order_by(Message.created_at, Message.id)
            )
        ).all()
    )
    runs = (await context.db.scalars(select(AgentRun).where(
        AgentRun.session_id == target_id,
        AgentRun.parent_run_id.is_(None),
    ))).all() if records else []
    by_run = {str(run.id): run for run in runs}
    by_input = {run.input_message_id: run for run in runs if run.input_message_id is not None}
    agent_by_message: dict[UUID, UUID | None] = {}
    active_agents: dict[str, UUID | None] = {}
    for message in records:
        host = (message.provider_data or {}).get("_matoloop")
        host = host if isinstance(host, dict) else {}
        run = by_input.get(message.id) or by_run.get(host.get("runId"))
        if run is None:
            continue
        key = str(run.id)
        if key not in active_agents:
            initial = host.get("executionAgentId") if message.id == run.input_message_id else None
            try:
                active_agents[key] = UUID(initial) if initial else run.execution_agent_id
            except (TypeError, ValueError):
                active_agents[key] = run.execution_agent_id
        if host.get("toolName") == "select_execution_agent" and message.role == "tool":
            try:
                changed = json.loads(message.content)
                if isinstance(changed, dict) and changed.get("selected") is True:
                    active_agents[key] = UUID(changed["id"])
            except (ValueError, KeyError, TypeError):
                pass
        agent_by_message[message.id] = active_agents[key]
    start = 0
    if arguments.around_message_id is not None:
        anchor = next(
            (
                index
                for index, item in enumerate(records)
                if item.id == arguments.around_message_id
            ),
            -1,
        )
        if anchor < 0:
            retry_arguments = arguments.model_dump(mode="json", exclude_none=True)
            retry_arguments.pop("around_message_id")
            return ToolResult.error(
                "tool.cursorInvalid",
                field="around_message_id",
                params={"retryArguments": retry_arguments},
            )
        start = min(
            max(0, anchor - arguments.limit // 2),
            max(0, len(records) - arguments.limit),
        )
    elif arguments.after_message_id is not None:
        start = next(
            (index + 1 for index, item in enumerate(records) if item.id == arguments.after_message_id),
            -1,
        )
        if start < 0:
            retry_arguments = arguments.model_dump(mode="json", exclude_none=True)
            retry_arguments.pop("after_message_id")
            return ToolResult.error(
                "tool.cursorInvalid",
                field="after_message_id",
                params={"retryArguments": retry_arguments},
            )
    page = records[start : start + arguments.limit]
    tool_names = _tool_names_by_message_id(records)
    remaining = 50_000
    messages: list[dict[str, Any]] = []
    for message in page:
        item, remaining = _message_item(
            message,
            remaining,
            tool_names,
            context.memory_trust_level,
            reference_tool_results=target_id == context.session.id,
            available_secret_references=(
                _available_secret_references(context) if target_id == context.session.id else set()
            ),
        )
        if "executionAgentId" not in item and message.id in agent_by_message:
            agent_id = agent_by_message[message.id]
            item["executionAgentId"] = str(agent_id) if agent_id is not None else None
        messages.append(item)
        if remaining <= 0:
            break
    last_emitted_index = start + len(messages) - 1
    has_more = last_emitted_index + 1 < len(records)
    next_after_message_id = records[last_emitted_index].id if messages and has_more else None
    context.session_read_cursors[target_id] = next_after_message_id
    return ToolResult.json(
        {
            "session": {
                "id": str(target.id),
                "title": target.title,
                "selectedExecutionAgentId": (
                    str(target.preferred_execution_agent_id)
                    if target.preferred_execution_agent_id else None
                ),
            },
            "messages": messages,
            "hasEarlier": start > 0,
            "nextAfterMessageId": (
                str(next_after_message_id) if next_after_message_id is not None else None
            ),
            "contentIsUntrusted": True,
        },
        metadata={"targetSessionId": str(target.id), "messageCount": len(messages)},
    )


async def session_read_permission(context: ToolContext, target_id: UUID):
    target = await context.db.scalar(select(ChatSession).where(
        ChatSession.id == target_id,
        ChatSession.user_id == context.session.user_id,
        or_(ChatSession.session_type == "chat", ChatSession.id == context.session.id),
        or_(ChatSession.parent_session_id.is_(None), ChatSession.parent_session_id == context.session.id,
            ChatSession.id == context.session.id),
    ).execution_options(populate_existing=True))
    return target, session_permission(target_id, private_access=(
        target is not None and target_id != context.session.id
        and context.model_trust_level == TrustLevel.PUBLIC and target.trust_level != TrustLevel.PUBLIC.value
    ))


def _ranked_visible_session_messages(*, pattern: str | None = None):
    filters = [
        Message.role.in_(("user", "assistant")),
        Message.content != "",
        Message.error_code.is_(None),
        Message.generation_status == "completed",
    ]
    if pattern is not None:
        filters.append(Message.content.ilike(pattern, escape="\\"))
    return (
        select(
            Message.id.label("message_id"),
            Message.session_id.label("session_id"),
            Message.role.label("role"),
            Message.content.label("content"),
            Message.created_at.label("created_at"),
            func.row_number()
            .over(
                partition_by=Message.session_id,
                order_by=(Message.created_at.desc(), Message.id.desc()),
            )
            .label("position"),
        )
        .where(*filters)
        .subquery()
    )


def _session_discovery_item(
    session: ChatSession,
    *,
    message_id: UUID | None,
    role: str | None,
    content: str | None,
    message_at: datetime | None,
) -> dict[str, Any]:
    read_arguments: dict[str, str] = {"session_id": str(session.id)}
    if message_id is not None:
        read_arguments["around_message_id"] = str(message_id)
    return {
        "id": str(session.id),
        "title": session.title,
        "anchorMessage": (
            {
                "id": str(message_id),
                "role": role,
                "createdAt": message_at.isoformat() if message_at else None,
                "preview": (content or "")[:500],
            }
            if message_id is not None
            else None
        ),
        "sessionReadArguments": read_arguments,
    }


async def _session_browse(
    arguments: SessionBrowseArguments, context: ToolContext
) -> ToolResult:
    if not await tools_access.allow(context, session_browse_permission(
        include_private=arguments.include_private, trust_level=context.model_trust_level,
    )):
        return ToolResult.error("permission.denied")
    messages = _ranked_visible_session_messages()
    rows = (
        await context.db.execute(
            select(
                ChatSession,
                messages.c.message_id,
                messages.c.role,
                messages.c.content,
                messages.c.created_at,
            )
            .join(
                messages,
                and_(
                    messages.c.session_id == ChatSession.id,
                    messages.c.position == 1,
                ),
            )
            .where(
                ChatSession.user_id == context.session.user_id,
                ChatSession.id != context.session.id,
                ChatSession.session_type == "chat",
                or_(ChatSession.parent_session_id.is_(None), ChatSession.parent_session_id == context.session.id),
                *session_visibility_filters(TrustLevel.PRIVATE if arguments.include_private else context.model_trust_level),
            )
            .order_by(messages.c.created_at.desc(), ChatSession.id.desc())
            .limit(arguments.limit)
        )
    ).all()
    return ToolResult.json(
        {
            "sessions": [
                _session_discovery_item(
                    session,
                    message_id=message_id,
                    role=role,
                    content=content,
                    message_at=message_at,
                )
                for session, message_id, role, content, message_at in rows
            ],
            "contentIsUntrusted": True,
        }
    )


async def _session_search(
    arguments: SessionSearchArguments, context: ToolContext
) -> ToolResult:
    if not await tools_access.allow(context, session_search_permission(
        include_private=arguments.include_private, trust_level=context.model_trust_level,
    )):
        return ToolResult.error("permission.denied")
    escaped = (
        arguments.query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )
    pattern = f"%{escaped}%"
    messages = _ranked_visible_session_messages(pattern=pattern)
    match_conditions = [messages.c.message_id.is_not(None)]
    match_conditions.append(ChatSession.title.ilike(pattern, escape="\\"))
    rows = (
        await context.db.execute(
            select(
                ChatSession,
                messages.c.message_id,
                messages.c.role,
                messages.c.content,
                messages.c.created_at,
            )
            .outerjoin(
                messages,
                and_(
                    messages.c.session_id == ChatSession.id,
                    messages.c.position == 1,
                ),
            )
            .where(
                ChatSession.user_id == context.session.user_id,
                ChatSession.id != context.session.id,
                ChatSession.session_type == "chat",
                or_(ChatSession.parent_session_id.is_(None), ChatSession.parent_session_id == context.session.id),
                *session_visibility_filters(TrustLevel.PRIVATE if arguments.include_private else context.model_trust_level),
                or_(*match_conditions),
            )
            .order_by(
                func.coalesce(messages.c.created_at, ChatSession.updated_at).desc(),
                ChatSession.id.desc(),
            )
            .limit(arguments.limit)
        )
    ).all()
    return ToolResult.json(
        {
            "results": [
                _session_discovery_item(
                    session,
                    message_id=message_id,
                    role=role,
                    content=content,
                    message_at=message_at,
                )
                for session, message_id, role, content, message_at in rows
            ],
            "contentIsUntrusted": True,
        }
    )


SESSION_READ = ToolDefinition(
            "session_read",
            "Read session messages. Omit cursors to start at the earliest message.",
            SessionReadArguments,
            _session_read,
            group="sessions",
            label="Session lesen",
            icon="forum",
            actions=(_SESSION_READ,),
        )

SESSION_TOOL_RESULT_READ = ToolDefinition(
            "session_tool_result_read",
            "Read a bounded chunk of a persisted tool result without executing the original tool.",
            SessionToolResultReadArguments,
            _session_tool_result_read,
            group="sessions",
            label="Tool-Ergebnis lesen",
            icon="history",
            actions=(_SESSION_TOOL_RESULT_READ,),
        )

SESSION_BROWSE = ToolDefinition(
            "session_browse",
            "List recent chat sessions.",
            SessionBrowseArguments,
            _session_browse,
            group="sessions",
            label="Letzte Sessions anzeigen",
            icon="view_list",
            actions=(_SESSION_SEARCH,),
        )

SESSION_SEARCH = ToolDefinition(
            "session_search",
            "Search prior chat sessions for a concrete literal term.",
            SessionSearchArguments,
            _session_search,
            group="sessions",
            label="Sessions durchsuchen",
            icon="manage_search",
            actions=(_SESSION_SEARCH,),
        )
