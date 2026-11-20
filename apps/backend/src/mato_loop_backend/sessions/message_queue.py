"""Persistent, owner-bound FIFO delivery through the ordinary agent runtime."""

import asyncio
import logging
from contextlib import contextmanager
from uuid import UUID

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import selectinload

from mato_loop_backend.db.session import AsyncSessionFactory
from mato_loop_backend.media.attachments import (
    ABSOLUTE_MAX_FILE_ATTACHMENT_BYTES,
    AttachmentError,
    AttachmentInput,
)
from mato_loop_backend.models import (
    AgentRun,
    ChatSession,
    QueuedAttachment,
    QueuedMessage,
    UserAccount,
)
from mato_loop_backend.sessions.state import deleted_session_clause, session_work_blocked

logger = logging.getLogger(__name__)
ACTIVE = ("queued", "running", "waiting_approval", "waiting_input")
_deliveries: dict[UUID, asyncio.Task[None]] = {}
_history_undos: set[UUID] = set()


@contextmanager
def hold_delivery_for_undo(session_id: UUID):
    if session_id in _history_undos:
        from mato_loop_backend.chat_history import ChatHistoryError

        raise ChatHistoryError("session.runInactive", 409)
    _history_undos.add(session_id)
    try:
        yield
    finally:
        _history_undos.discard(session_id)
        schedule_delivery(session_id)


def workspace_binding(session):
    return [str(session.preferred_execution_agent_id) if session.preferred_execution_agent_id else None, session.current_directory]


async def enqueue(db, session, request_id, payload, attachments=(), *, after_run_id=None):
    from mato_loop_backend.sessions.commands import parse_command, prepare_command
    from mato_loop_backend.sessions.service import SessionServiceError

    if blocked := await session_work_blocked(db, session):
        raise SessionServiceError(409, blocked)
    existing = await db.get(QueuedMessage, request_id)
    run = await db.get(AgentRun, request_id)
    if existing is not None or run is not None:
        if (existing or run).session_id != session.id:
            raise SessionServiceError(409, "http.conflict")
        return None
    if after_run_id is not None:
        previous = await db.get(QueuedMessage, after_run_id) or await db.get(AgentRun, after_run_id)
        if previous is None or previous.session_id != session.id or after_run_id == request_id:
            raise SessionServiceError(422, "validation.invalid", field="after_run_id")
        payload = {**payload, "_source": "followup"}
    command = parse_command(payload["content"])
    if command is not None:
        prepared = await prepare_command(
            db, session, command, payload["content"],
            agent_profile_id=UUID(payload["agent_profile_id"]) if payload.get("agent_profile_id") else session.agent_profile_id,
        )
        payload = {**payload, "_command_context": prepared["context"]}
    entry = QueuedMessage(id=request_id, session_id=session.id, after_run_id=after_run_id,
                          payload={**payload, "_workspace": workspace_binding(session)}, attachments=[])
    for position, attachment in enumerate(attachments or ()):
        data = attachment.data if isinstance(attachment.data, bytes) else await asyncio.to_thread(
            attachment.data.read, ABSOLUTE_MAX_FILE_ATTACHMENT_BYTES + 1,
        )
        if not data or len(data) > ABSOLUTE_MAX_FILE_ATTACHMENT_BYTES:
            raise AttachmentError("attachment.tooLarge" if data else "attachment.empty")
        entry.attachments.append(QueuedAttachment(position=position, data=data, details={
            "mime_type": attachment.mime_type, "filename": attachment.filename, "kind": attachment.kind,
            "metadata": attachment.metadata, "size": len(data),
        }))
    db.add(entry)
    await db.flush()
    return entry


def entry_payload(entry, session):
    return {
        "id": str(entry.id), "ownerUserId": str(session.user_id), "sessionId": str(session.id),
        "projectId": str(session.project_id) if session.project_id else None,
        "content": entry.payload["content"], "agentProfileId": entry.payload.get("agent_profile_id"),
        "activeFile": entry.payload.get("active_file"), "queuedAt": entry.created_at.isoformat(), "queuePosition": entry.position,
        "paused": entry.paused, "errorCode": entry.error_code, "serverQueued": True,
        "afterRunId": str(entry.after_run_id) if entry.after_run_id else None,
        "sourceType": entry.payload.get("_source"),
        "attachments": [{"id": f"{entry.id}:{item.position}", "name": item.details.get("filename") or "datei", "filename": item.details.get("filename"),
                         "kind": item.details["kind"], "mimeType": item.details["mime_type"], "size": item.details["size"],
                         "sizeBytes": item.details["size"], "voiceMessage": bool((item.details.get("metadata") or {}).get("voiceMessage")),
                         "url": f"/api/sessions/{session.id}/queue/{entry.id}/attachments/{item.position}"}
                        for item in entry.attachments],
    }


async def list_entries(db, user_id):
    rows = (await db.execute(select(QueuedMessage, ChatSession).join(ChatSession).where(
        ChatSession.user_id == user_id, ~deleted_session_clause(),
    ).options(selectinload(QueuedMessage.attachments).defer(QueuedAttachment.data)).order_by(QueuedMessage.position))).all()
    return [entry_payload(entry, session) for entry, session in rows]


async def remove_entry(db, session_id, request_id):
    await db.execute(delete(QueuedMessage).where(QueuedMessage.session_id == session_id, or_(
        QueuedMessage.id == request_id, QueuedMessage.after_run_id == request_id,
    )))


async def deliver_next(session_id):
    from mato_loop_backend.agent.run_contracts import AgentRunError
    from mato_loop_backend.sessions.service import SessionServiceError, start_message

    async with AsyncSessionFactory() as db:
        session = await db.get(ChatSession, session_id)
        if session is None:
            return
        owner = await db.scalar(select(UserAccount.id).where(UserAccount.id == session.user_id).with_for_update(key_share=True, skip_locked=True))
        if owner is None:
            return
        session = await db.scalar(select(ChatSession).where(ChatSession.id == session_id).with_for_update(skip_locked=True)
                                  .execution_options(populate_existing=True))
        if session is None or session_id in _history_undos or await db.scalar(select(AgentRun.id).where(
            AgentRun.session_id == session_id, AgentRun.parent_run_id.is_(None), AgentRun.status.in_(ACTIVE),
        )):
            return
        entry = await db.scalar(select(QueuedMessage).where(QueuedMessage.session_id == session_id)
            .order_by(QueuedMessage.position).limit(1))
        if entry is None or entry.paused:
            return
        entry_id = entry.id
        try:
            if blocked := await session_work_blocked(db, session):
                raise SessionServiceError(409, blocked)
            if entry.after_run_id:
                previous = await db.get(AgentRun, entry.after_run_id)
                if previous is None or previous.session_id != session_id or previous.status != "completed":
                    raise AgentRunError("session.queuePredecessorFailed")
            payload = entry.payload
            if payload["_workspace"] != workspace_binding(session):
                raise AgentRunError("session.queueContextChanged")
            uploads = await db.scalars(select(QueuedAttachment).where(QueuedAttachment.message_id == entry_id).order_by(QueuedAttachment.position))
            attachments = [AttachmentInput(data=item.data, **{key: value for key, value in item.details.items() if key != "size"}) for item in uploads]
            await db.delete(entry)
            profile_id = UUID(payload["agent_profile_id"]) if payload.get("agent_profile_id") else session.agent_profile_id
            await start_message(db, session, payload["content"], profile_id, run_id=entry_id, attachments=attachments,
                agent_profile_id_override=profile_id,
                active_file=payload.get("active_file"), message_source=payload.get("_source"),
                command_context_snapshot=payload.get("_command_context"))
        except Exception as exc:
            code = exc.code if isinstance(exc, (AgentRunError, SessionServiceError, AttachmentError)) else "agent.failed"
            if code == "agent.failed":
                logger.error("Queued message %s could not be admitted (%s)", entry_id, type(exc).__name__)
            await db.rollback()
            entry = await db.scalar(select(QueuedMessage).where(QueuedMessage.id == entry_id).with_for_update())
            if entry is not None:
                if code != "session.runAlreadyActive":
                    entry.paused, entry.error_code = True, code
                await db.commit()


def _delivery_finished(session_id, task):
    _deliveries.pop(session_id, None)
    if not task.cancelled() and (error := task.exception()) is not None:
        logger.error("Message queue admission failed (%s)", type(error).__name__)


def schedule_delivery(session_id: UUID) -> None:
    if session_id in _deliveries or session_id in _history_undos:
        return
    task = asyncio.create_task(deliver_next(session_id))
    _deliveries[session_id] = task
    task.add_done_callback(
        lambda completed, session_id=session_id: _delivery_finished(session_id, completed)
    )


async def process_pending() -> None:
    async with AsyncSessionFactory() as db:
        sessions = list(await db.scalars(select(QueuedMessage.session_id).distinct()))
    for session_id in sessions:
        schedule_delivery(session_id)
