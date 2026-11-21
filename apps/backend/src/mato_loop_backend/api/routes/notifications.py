from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend.api.errors import ApiError
from mato_loop_backend.db.session import get_db_session
from mato_loop_backend.memory.service import MemoryService
from mato_loop_backend.models import ChatSession, Notification, UserAccount
from mato_loop_backend.notification_content import (
    NotificationPriority,
    notification_body_preview,
    notification_title,
)
from mato_loop_backend.notifications import NotificationError, NotificationService
from mato_loop_backend.security import require_admin, require_user
from mato_loop_backend.sessions.presence import mark_user_present
from mato_loop_backend.settings import SettingsStore, WebPushSettings
from mato_loop_backend.web_push import (
    VapidKeyStore,
    WebPushConfigurationError,
    WebPushRegistrationError,
    WebPushService,
    WebPushSubscriptionInput,
    load_effective_web_push_settings,
    validate_web_push_subject,
)

router = APIRouter(prefix="/notifications", dependencies=[Depends(require_user)])


class SessionNotificationTargetResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["session"]
    session_id: UUID = Field(alias="sessionId")


class AutomationNotificationTargetResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["automationRun"]
    automation_id: UUID = Field(alias="automationId")
    run_id: UUID = Field(alias="runId")


class MemoryApprovalNotificationTargetResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["memoryApproval"]
    approval_id: UUID = Field(alias="approvalId")
    session_id: UUID | None = Field(default=None, alias="sessionId")


class NotificationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    kind: str
    priority: NotificationPriority = "normal"
    title: str
    body: str | None
    target: SessionNotificationTargetResponse | AutomationNotificationTargetResponse | MemoryApprovalNotificationTargetResponse | None
    read_at: datetime | None = Field(alias="readAt")
    created_at: datetime = Field(alias="createdAt")


class NotificationListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[NotificationResponse]
    next_cursor: str | None = Field(alias="nextCursor")
    unread_count: int = Field(alias="unreadCount")


class NotificationReadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    notification: NotificationResponse | None
    unread_count: int = Field(alias="unreadCount")


class NotificationsReadAllResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    updated_count: int = Field(alias="updatedCount")
    unread_count: int = Field(alias="unreadCount")


class WebPushConfigResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    configured: bool
    public_key: str | None = Field(alias="publicKey")


class WebPushSettingsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    configured: bool
    contact_email: str | None = Field(alias="contactEmail")


def _notification_payload(item: Notification) -> NotificationResponse:
    automation = item.automation_run.automation if item.automation_run is not None else None
    return NotificationResponse(
        id=item.id,
        kind=item.kind,
        priority=item.priority or "normal",
        title=notification_title(item.title, item.priority),
        body=notification_body_preview(item.body, limit=500 if item.kind == "session.response" else 4_000),
        target=(
            MemoryApprovalNotificationTargetResponse(
                type="memoryApproval", approvalId=item.approval_id, sessionId=item.session_id,
            )
            if item.kind == "memory.approval" and item.approval_id is not None
            else SessionNotificationTargetResponse(type="session", sessionId=item.session_id)
            if item.session_id is not None
            else AutomationNotificationTargetResponse(
                type="automationRun",
                automationId=automation.id,
                runId=item.automation_run_id,
            )
            if automation is not None and item.automation_run_id is not None
            else None
        ),
        readAt=item.read_at,
        createdAt=item.created_at,
    )


def _encode_cursor(user_id: UUID, created_at: datetime, notification_id: UUID) -> str:
    value = json.dumps(
        [str(user_id), created_at.isoformat(), str(notification_id)], separators=(",", ":")
    ).encode()
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _decode_cursor(value: str, user_id: UUID) -> tuple[datetime, UUID]:
    try:
        decoded = base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
        )
        payload = json.loads(decoded)
        if not (
            isinstance(payload, list)
            and len(payload) == 3
            and all(isinstance(item, str) for item in payload)
            and UUID(payload[0]) == user_id
        ):
            raise ValueError
        created_at = datetime.fromisoformat(payload[1])
        if created_at.tzinfo is None:
            raise ValueError
        return created_at, UUID(payload[2])
    except (binascii.Error, UnicodeDecodeError, ValueError, TypeError) as exc:
        raise ApiError(400, "validation.invalid", field="cursor") from exc


class NotificationPresenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: UUID = Field(alias="clientId")
    session_id: UUID | None = Field(default=None, alias="sessionId")
    active: bool = True


@router.put("/presence", status_code=status.HTTP_204_NO_CONTENT)
async def mark_notification_presence(
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    payload: NotificationPresenceRequest | None = None,
) -> Response:
    if payload is None:
        mark_user_present(user.id)
    else:
        if payload.session_id is not None and await db.scalar(select(ChatSession.id).where(
            ChatSession.id == payload.session_id, ChatSession.user_id == user.id,
        )) is None:
            raise ApiError(404, "session.notFound")
        mark_user_present(
            user.id, client_id=payload.client_id, session_id=payload.session_id,
            active=payload.active,
        )
    if payload is None or payload.active:
        NotificationService.cancel_response_pushes(user.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/push/config", response_model=WebPushConfigResponse)
async def get_web_push_config(
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> WebPushConfigResponse:
    store = VapidKeyStore()
    try:
        settings = await load_effective_web_push_settings(db)
        public_key = store.public_key()
        if settings.legacy and settings.enabled and public_key is None:
            public_key = store.ensure_key()
        configured = bool(
            settings.subject
            and settings.allowed_origins
            and public_key
            and validate_web_push_subject(settings.subject)
        )
    except (ValueError, WebPushConfigurationError) as exc:
        raise ApiError(503, "webPush.unavailable") from exc
    return WebPushConfigResponse(
        enabled=settings.enabled and configured,
        configured=configured,
        publicKey=public_key,
    )


async def _web_push_settings_payload(db: AsyncSession) -> WebPushSettingsResponse:
    stored = await SettingsStore.load_optional(db, WebPushSettings)
    effective = await load_effective_web_push_settings(db)
    try:
        public_key = VapidKeyStore().public_key()
        configured = bool(
            effective.subject
            and effective.allowed_origins
            and public_key
            and validate_web_push_subject(effective.subject)
        )
    except (ValueError, WebPushConfigurationError):
        configured = False
    contact_email = stored.contact_email if stored else None
    if stored is None and effective.subject and effective.subject.startswith("mailto:"):
        contact_email = effective.subject.removeprefix("mailto:")
    return WebPushSettingsResponse(
        enabled=stored.enabled if stored else effective.enabled,
        configured=configured,
        contactEmail=contact_email,
    )


@router.get("/push/settings", response_model=WebPushSettingsResponse)
async def get_web_push_settings(
    _admin: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> WebPushSettingsResponse:
    return await _web_push_settings_payload(db)


@router.put("/push/settings", response_model=WebPushSettingsResponse)
async def update_web_push_settings(
    payload: WebPushSettings,
    _admin: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> WebPushSettingsResponse:
    if payload.enabled and payload.contact_email is None:
        raise ApiError(422, "validation.required", field="contactEmail")
    if payload.contact_email is not None:
        try:
            VapidKeyStore().ensure_key()
        except WebPushConfigurationError as exc:
            raise ApiError(503, "webPush.unavailable") from exc
    await SettingsStore.save(db, payload)
    await db.commit()
    return await _web_push_settings_payload(db)


@router.put("/push/subscriptions/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
async def put_web_push_subscription(
    device_id: UUID,
    payload: WebPushSubscriptionInput,
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> Response:
    try:
        await WebPushService.register(
            db, user_id=user.id, device_id=device_id, subscription=payload
        )
        await db.commit()
    except WebPushConfigurationError as exc:
        await db.rollback()
        raise ApiError(503, "webPush.disabled") from exc
    except WebPushRegistrationError as exc:
        await db.rollback()
        raise ApiError(422, "validation.invalid", field="endpoint") from exc
    except IntegrityError as exc:
        await db.rollback()
        raise ApiError(409, "webPush.subscriptionConflict") from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/push/subscriptions/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_web_push_subscription(
    device_id: UUID,
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> Response:
    await WebPushService.delete(db, user_id=user.id, device_id=device_id)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("", response_model=NotificationListResponse)
async def list_notifications(
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    page_size: Annotated[int, Query(alias="pageSize", ge=1, le=100)] = 20,
    cursor: str | None = None,
) -> NotificationListResponse:
    decoded_cursor = _decode_cursor(cursor, user.id) if cursor else None
    if await MemoryService.reconcile_approvals(db, owner_id=user.id):
        await db.commit()
    rows = await NotificationService.list_page(
        db, user.id, page_size=page_size, cursor=decoded_cursor
    )
    page = rows[:page_size]
    return NotificationListResponse(
        items=[_notification_payload(item) for item in page],
        nextCursor=(
            _encode_cursor(user.id, page[-1].created_at, page[-1].id)
            if len(rows) > page_size
            else None
        ),
        unreadCount=await NotificationService.unread_count(db, user.id),
    )


@router.post("/read-all", response_model=NotificationsReadAllResponse)
async def mark_all_notifications_read(
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> NotificationsReadAllResponse:
    updated_count = await NotificationService.mark_all_read(db, user.id)
    unread_count = await NotificationService.unread_count(db, user.id)
    await db.commit()
    return NotificationsReadAllResponse(
        updatedCount=updated_count,
        unreadCount=unread_count,
    )


@router.post("/{notification_id}/read", response_model=NotificationReadResponse)
async def mark_notification_read(
    notification_id: UUID,
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> NotificationReadResponse:
    item = await NotificationService.mark_read(db, user.id, notification_id)
    unread_count = await NotificationService.unread_count(db, user.id)
    await db.commit()
    return NotificationReadResponse(
        notification=_notification_payload(item) if item is not None else None,
        unreadCount=unread_count,
    )


@router.delete("/{notification_id}")
async def delete_notification(
    notification_id: UUID,
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, int]:
    try:
        await NotificationService.delete(db, user.id, notification_id)
    except NotificationError as exc:
        raise ApiError(404, exc.code) from exc
    unread_count = await NotificationService.unread_count(db, user.id)
    await db.commit()
    return {"unreadCount": unread_count}
