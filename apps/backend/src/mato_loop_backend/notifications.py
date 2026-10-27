from __future__ import annotations

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
    ApprovalRequest,
    Automation,
    AutomationRun,
    ChatSession,
    Message,
    Notification,
    QuestionRequest,
    UserAccount,
)
from mato_loop_backend.sessions.presence import session_is_present
from mato_loop_backend.web_push import (
    WebPushMemoryApprovalTarget,
    WebPushService,
    WebPushSessionTarget,
    web_push_payload,
)

SESSION_REQUEST_KINDS = ("approval.requested", "question.requested")
READ_ONCE_KINDS = ("session.response", *SESSION_REQUEST_KINDS)


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
    async def deliver_after_commit(notification_id: UUID) -> None:
        async with AsyncSessionFactory() as db:
            item = await db.get(Notification, notification_id)
            if item is None or item.read_at is not None:
                return
            user_id = item.user_id
            payload = web_push_payload(
                title=item.title, body=item.body, tag=f"notification:{item.id}",
                notification_id=item.id,
                target=WebPushMemoryApprovalTarget(type="memoryApproval", approvalId=item.approval_id)
                if item.kind == "memory.approval" and item.approval_id is not None
                else WebPushSessionTarget(type="session", sessionId=item.session_id)
                if item.session_id else None,
            )
        await WebPushService.deliver_after_commit(user_id, payload)

    @staticmethod
    async def deliver_message_after_commit(message_id: UUID | None) -> None:
        if message_id is None:
            return
        async with AsyncSessionFactory() as db:
            notification_id = await db.scalar(select(Notification.id).where(
                Notification.deduplication_key == f"message:{message_id}",
                Notification.kind == "session.response", Notification.read_at.is_(None),
            ))
        if notification_id is not None:
            await NotificationService.deliver_after_commit(notification_id)

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
