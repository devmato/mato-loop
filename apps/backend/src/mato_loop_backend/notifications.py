from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from mato_loop_backend.db.changes import record_change
from mato_loop_backend.db.session import AsyncSessionFactory
from mato_loop_backend.message_content import canonical_assistant_content
from mato_loop_backend.models import (
    AgentRun,
    ApprovalRequest,
    Automation,
    AutomationRun,
    ChatSession,
    Message,
    Notification,
    QuestionRequest,
    UserAccount,
)
from mato_loop_backend.notification_content import NotificationPriority, notification_title
from mato_loop_backend.sessions.presence import (
    presence_remaining,
    session_is_present,
    user_is_present,
)
from mato_loop_backend.sessions.state import deleted_session_clause
from mato_loop_backend.settings import SettingsStore, UserPreferences
from mato_loop_backend.web_push import (
    WebPushMemoryApprovalTarget,
    WebPushNotificationCenterTarget,
    WebPushPayload,
    WebPushService,
    WebPushSessionTarget,
    web_push_payload,
)

SESSION_REQUEST_KINDS = ("approval.requested", "question.requested")
READ_ONCE_KINDS = ("session.response", *SESSION_REQUEST_KINDS)
IMMEDIATE_PUSH_KINDS = (*SESSION_REQUEST_KINDS, "memory.approval")
PUSH_BATCH_SECONDS = 30.0
logger = logging.getLogger(__name__)


@dataclass
class _PushBatch:
    ready_at: float = 0.0
    notices: dict[UUID, bool] = field(default_factory=dict)
    block_changed: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None


_push_batches: dict[UUID, _PushBatch] = {}


def _notification_push_payload(item: Notification, *, silent: bool = False) -> WebPushPayload:
    return web_push_payload(
        title=notification_title(item.title, item.priority), body=item.body, tag=f"notification:{item.id}",
        notification_id=item.id, silent=silent,
        target=WebPushMemoryApprovalTarget(
            type="memoryApproval", approvalId=item.approval_id, sessionId=item.session_id,
        )
        if item.kind == "memory.approval" and item.approval_id is not None
        else WebPushSessionTarget(type="session", sessionId=item.session_id)
        if item.session_id else None,
    )


async def _wait_for_push(batch: _PushBatch, delay: float) -> None:
    try:
        await asyncio.wait_for(batch.block_changed.wait(), timeout=delay)
    except TimeoutError:
        pass


async def _send_pending_push(
    user_id: UUID, notices: dict[UUID, bool], *, batch: _PushBatch | None = None,
) -> float | None:
    """Return a deferral in seconds, or None if this set of notices is finished."""
    delivery = await WebPushService.prepare_delivery(user_id)
    pending = notices.copy()
    items = []
    pause = 0.0
    if delivery is not None:
        async with AsyncSessionFactory() as db:
            if batch is not None:
                preferences = await SettingsStore.load(db, UserPreferences, user_id=user_id)
                if preferences.push_paused_until is not None:
                    pause = (preferences.push_paused_until - datetime.now(UTC)).total_seconds()
            if pause <= 0:
                items = (await db.scalars(select(Notification).outerjoin(ChatSession).where(
                    Notification.user_id == user_id, Notification.id.in_(pending),
                    Notification.read_at.is_(None),
                    or_(Notification.session_id.is_(None), ~deleted_session_clause()),
                ))).all()

    if batch is not None and (batch.block_changed.is_set() or pending != notices):
        return 0.0
    delay = max(pause, presence_remaining(user_id)) if delivery is not None else 0.0
    if delay > 0:
        return delay

    # Retire the validated window at handoff, not after network I/O: new arrivals
    # during the send get their own window, including corrections to existing IDs.
    notices.clear()
    if delivery is not None and items:
        silent = all(pending[item.id] for item in items)
        payload = _notification_push_payload(items[0], silent=silent) if len(items) == 1 else web_push_payload(
            title=f"{len(items)} neue Session-Hinweise",
            body="Öffne die Benachrichtigungen, um die ungelesenen Hinweise zu sehen.",
            tag="session-responses", target=WebPushNotificationCenterTarget(type="actions"), silent=silent,
        )
        await WebPushService.send(delivery, payload)
    return None


async def _deliver_push_batch(user_id: UUID, batch: _PushBatch) -> None:
    try:
        loop = asyncio.get_running_loop()
        while batch.notices:
            batch.block_changed.clear()
            delay = max(batch.ready_at - loop.time(), presence_remaining(user_id))
            if delay > 0:
                await _wait_for_push(batch, delay)
                continue
            delay = await _send_pending_push(user_id, batch.notices, batch=batch)
            if delay is not None and delay > 0:
                await _wait_for_push(batch, delay)
    except Exception:
        logger.exception("Deferred notification push delivery failed")
    finally:
        if _push_batches.get(user_id) is batch:
            _push_batches.pop(user_id)


class NotificationDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    user_id: UUID
    kind: Annotated[str, Field(min_length=3, max_length=80, pattern=r"^[a-z][A-Za-z]*(?:\.[a-z][A-Za-z]*)+$")]
    title: Annotated[str, Field(min_length=1, max_length=240)]
    body: Annotated[str | None, Field(default=None, max_length=4_000)]
    deduplication_key: Annotated[str, Field(min_length=1, max_length=255)]
    session_id: UUID | None = None
    automation_run_id: UUID | None = None
    approval_id: UUID | None = None
    priority: NotificationPriority = "normal"

    @field_validator("title", "deduplication_key")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be blank")
        return normalized

    @field_validator("body")
    @classmethod
    def normalize_body(cls, value: str | None) -> str | None:
        return value.rstrip() or None if value is not None else None


class NotificationError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class NotificationService:
    @staticmethod
    async def create(
        db: AsyncSession, draft: NotificationDraft, *, created_at: datetime | None = None,
    ) -> UUID | None:
        if draft.approval_id is not None and await db.scalar(select(ApprovalRequest.id).where(
            ApprovalRequest.id == draft.approval_id, ApprovalRequest.user_id == draft.user_id,
        )) is None:
            raise NotificationError("notification.targetNotFound")
        if draft.session_id is not None:
            owned_session = await db.scalar(
                select(ChatSession.id)
                .where(
                    ChatSession.id == draft.session_id,
                    ChatSession.user_id == draft.user_id,
                )
                # Match foreign-key locks held by concurrent requests for this session.
                .with_for_update(read=True, key_share=True)
            )
            if owned_session is None:
                raise NotificationError("notification.targetNotFound")
            if draft.kind in SESSION_REQUEST_KINDS and session_is_present(draft.user_id, draft.session_id):
                return None
        if draft.automation_run_id is not None:
            owned_run = await db.scalar(
                select(AutomationRun.id)
                .join(Automation, Automation.id == AutomationRun.automation_id)
                .where(
                    AutomationRun.id == draft.automation_run_id,
                    Automation.user_id == draft.user_id,
                )
                .with_for_update()
            )
            if owned_run is None:
                raise NotificationError("notification.targetNotFound")
        notification_id = uuid4()
        statement = (
            insert(Notification)
            .values(
                id=notification_id, **draft.model_dump(),
                **({"created_at": created_at} if created_at is not None else {}),
            )
            .on_conflict_do_nothing(constraint="uq_notifications_user_deduplication_key")
            .returning(Notification.id)
        )
        notification_id = await db.scalar(statement)
        if notification_id is not None:
            record_change(db, draft.user_id, "notifications")
        return notification_id

    @staticmethod
    async def upsert_agent_notice(
        db: AsyncSession, draft: NotificationDraft, *, run_id: UUID,
    ) -> tuple[UUID | None, bool]:
        run = await db.scalar(select(AgentRun).join(ChatSession, ChatSession.id == AgentRun.session_id).where(
            AgentRun.id == run_id, ChatSession.user_id == draft.user_id,
        ).execution_options(populate_existing=True).with_for_update(of=AgentRun))
        if run is None:
            raise NotificationError("notification.targetNotFound")
        if run.notice_id is None:
            notification_id = await NotificationService.create(db, draft)
            if notification_id is not None:
                run.notice_id = notification_id
                await db.flush()
                return notification_id, True
        notification_id = await db.scalar(update(Notification).where(
            Notification.id == run.notice_id,
            Notification.user_id == draft.user_id,
            Notification.session_id == draft.session_id,
            Notification.kind == "agent.notice",
            Notification.deduplication_key == draft.deduplication_key,
        ).values(title=draft.title, body=draft.body, priority=draft.priority).returning(Notification.id))
        if notification_id is None:
            return None, False
        record_change(db, draft.user_id, "notifications")
        return notification_id, False

    @staticmethod
    async def create_for_approval(db: AsyncSession, approval: ApprovalRequest) -> UUID | None:
        return await NotificationService.create(db, NotificationDraft(
            user_id=approval.user_id,
            session_id=approval.session_id,
            approval_id=approval.id,
            kind="memory.approval" if approval.request_type == "memory_change" else "approval.requested",
            title=approval.title[:240], body=approval.summary[:4_000],
            deduplication_key=f"approval:{approval.id}",
        ), created_at=approval.created_at)

    @staticmethod
    async def create_for_question(db: AsyncSession, request: QuestionRequest) -> UUID | None:
        owner_id = await db.scalar(select(ChatSession.user_id).where(ChatSession.id == request.session_id))
        if owner_id is None:
            raise NotificationError("notification.targetNotFound")
        return await NotificationService.create(db, NotificationDraft(
            user_id=owner_id, session_id=request.session_id, kind="question.requested",
            title="Rückfrage vom Agenten", body=request.questions[0]["question"][:4_000],
            deduplication_key=f"question:{request.id}",
        ), created_at=request.created_at)

    @staticmethod
    async def create_for_message(db: AsyncSession, message: Message) -> UUID | None:
        session = await db.scalar(
            select(ChatSession).where(ChatSession.id == message.session_id)
            .execution_options(populate_existing=True).with_for_update()
        )
        if (
            session is None or session.parent_session_id is not None
            or session.session_type != "chat"
            or (session.last_read_at is not None and session.last_read_at >= message.created_at)
            or session_is_present(session.user_id, session.id)
        ):
            return None
        runtime_data = (message.provider_data or {}).get("_matoloop", {})
        run_id = runtime_data.get("runId") if isinstance(runtime_data, dict) else None
        if not message.error_code and run_id is not None and await db.scalar(
            select(Message.id).where(
                Message.session_id == session.id,
                Message.role == "tool",
                Message.provider_data["_matoloop"]["runId"].as_string() == run_id,
                Message.provider_data["_matoloop"]["toolName"].as_string().in_(("notify", "silence")),
                Message.provider_data["_matoloop"]["suppressResponseNotification"].as_boolean().is_(True),
            ).limit(1)
        ) is not None:
            return None
        return await NotificationService.create(
            db,
            NotificationDraft(
                user_id=session.user_id, session_id=session.id, kind="session.response",
                title=f"{'Antwort fehlgeschlagen' if message.error_code else 'Neue Antwort'}: {session.title}"[:240],
                body=(
                    "Der Agent-Auftrag konnte nicht abgeschlossen werden. Details stehen in der Session."
                    # ponytail: snapshots retain at most 4,000 source characters; read source messages if more is needed.
                    if message.error_code else canonical_assistant_content(message.content).rstrip()[:4_000]
                    or "Die Antwort ist in der Session verfügbar."
                ),
                deduplication_key=f"message:{message.id}",
            ),
            # Use the source timestamp so the existing through-message read boundary stays exact.
            created_at=message.created_at,
        )

    @staticmethod
    async def deliver_after_commit(notification_id: UUID, *, silent: bool = False) -> None:
        async with AsyncSessionFactory() as db:
            item = await db.get(Notification, notification_id)
        if item is not None:
            await NotificationService._dispatch_push(item, silent=silent)

    @staticmethod
    async def deliver_message_after_commit(message_id: UUID | None) -> None:
        if message_id is None:
            return
        async with AsyncSessionFactory() as db:
            item = await db.scalar(select(Notification).where(
                Notification.deduplication_key == f"message:{message_id}",
                Notification.kind == "session.response", Notification.read_at.is_(None),
            ))
        if item is not None:
            await NotificationService._dispatch_push(item)

    @staticmethod
    async def _dispatch_push(item: Notification, *, silent: bool = False) -> None:
        if item.read_at is not None:
            return
        user_id = item.user_id
        if item.kind in IMMEDIATE_PUSH_KINDS:
            if not user_is_present(user_id):
                await _send_pending_push(user_id, {item.id: silent})
            return
        batch = _push_batches.get(user_id)
        if batch is None:
            batch = _push_batches[user_id] = _PushBatch()
        if not batch.notices:
            batch.ready_at = asyncio.get_running_loop().time() + PUSH_BATCH_SECONDS
        batch.notices[item.id] = batch.notices.get(item.id, True) and silent
        if batch.task is None:
            batch.task = asyncio.create_task(_deliver_push_batch(user_id, batch))

    @staticmethod
    def push_block_changed(user_id: UUID) -> None:
        if batch := _push_batches.get(user_id):
            batch.block_changed.set()

    @staticmethod
    async def stop_pushes() -> None:
        tasks = [batch.task for batch in _push_batches.values() if batch.task is not None]
        _push_batches.clear()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    async def mark_session_read(
        db: AsyncSession, user_id: UUID, session_id: UUID, through: datetime | None,
    ) -> None:
        if through is not None:
            await db.execute(delete(Notification).where(
                Notification.user_id == user_id, Notification.session_id == session_id,
                Notification.kind == "session.response", Notification.created_at <= through,
            ))
        seen_at = datetime.now(UTC)
        await db.execute(delete(Notification).where(
            Notification.user_id == user_id, Notification.session_id == session_id,
            Notification.kind.in_(SESSION_REQUEST_KINDS), Notification.created_at <= seen_at,
        ))
        await db.execute(update(Notification).where(
            Notification.user_id == user_id, Notification.session_id == session_id,
            Notification.kind.not_in(READ_ONCE_KINDS), Notification.read_at.is_(None),
            Notification.created_at <= seen_at,
        ).values(read_at=seen_at))

    @staticmethod
    async def delete(db: AsyncSession, user_id: UUID, notification_id: UUID) -> None:
        deleted = await db.scalar(delete(Notification).where(
            Notification.id == notification_id, Notification.user_id == user_id,
        ).returning(Notification.id))
        if deleted is None:
            raise NotificationError("notification.notFound")

    @staticmethod
    async def list_page(
        db: AsyncSession,
        user_id: UUID,
        *,
        page_size: int,
        cursor: tuple[datetime, UUID] | None = None,
    ) -> list[Notification]:
        statement = (
            select(Notification)
            .options(
                selectinload(Notification.automation_run).selectinload(
                    AutomationRun.automation
                )
            )
            .where(Notification.user_id == user_id)
        )
        if cursor is not None:
            created_at, notification_id = cursor
            statement = statement.where(
                or_(
                    Notification.created_at < created_at,
                    and_(
                        Notification.created_at == created_at,
                        Notification.id < notification_id,
                    ),
                )
            )
        return list(
            (
                await db.scalars(
                    statement.order_by(
                        Notification.created_at.desc(), Notification.id.desc()
                    ).limit(page_size + 1)
                )
            ).all()
        )

    @staticmethod
    async def unread_count(db: AsyncSession, user_id: UUID) -> int:
        return int(
            await db.scalar(
                select(func.count())
                .select_from(Notification)
                .where(Notification.user_id == user_id, Notification.read_at.is_(None))
            )
            or 0
        )

    @staticmethod
    async def mark_read(
        db: AsyncSession, user_id: UUID, notification_id: UUID
    ) -> Notification | None:
        await db.scalar(
            select(UserAccount.id).where(UserAccount.id == user_id).with_for_update()
        )
        item = await db.scalar(
            select(Notification)
            .options(
                selectinload(Notification.automation_run).selectinload(
                    AutomationRun.automation
                )
            )
            .where(Notification.id == notification_id, Notification.user_id == user_id)
            .with_for_update()
        )
        if item is None:
            return None
        if item.kind in READ_ONCE_KINDS:
            await db.delete(item)
            await db.flush()
            return None
        if item.read_at is None:
            item.read_at = datetime.now(UTC)
        return item

    @staticmethod
    async def mark_all_read(db: AsyncSession, user_id: UUID) -> int:
        await db.scalar(
            select(UserAccount.id).where(UserAccount.id == user_id).with_for_update()
        )
        result = await db.execute(
            update(Notification)
            .where(Notification.user_id == user_id, Notification.read_at.is_(None))
            .values(read_at=func.now())
        )
        await db.execute(delete(Notification).where(
            Notification.user_id == user_id,
            Notification.kind.in_(READ_ONCE_KINDS), Notification.read_at.is_not(None),
        ))
        return int(result.rowcount or 0)
