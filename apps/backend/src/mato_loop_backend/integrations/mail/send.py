from __future__ import annotations

import hashlib
import json
from email import policy
from email.parser import BytesParser
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import delete, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from mato_loop_backend.db.session import AsyncSessionFactory
from mato_loop_backend.integrations.mail.client import MailClient, MailError
from mato_loop_backend.models import MailSendOperation


class MailSendResult(BaseModel):
    sent: Literal[True] = True
    sent_copy_confirmed: bool | None = Field(serialization_alias="sentCopyConfirmed")


def _digest(value: str | bytes) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def _message_digest(raw: bytes) -> str:
    message = BytesParser(policy=policy.default).parsebytes(raw)
    parts = []
    for part in message.walk():
        if part.is_multipart():
            continue
        parts.append(
            {
                "contentType": part.get_content_type(),
                "disposition": part.get_content_disposition(),
                "filename": part.get_filename(),
                "payload": _digest(part.get_payload(decode=True) or b""),
            }
        )
    return _digest(
        json.dumps(
            {
                "headers": {
                    name: str(message.get(name, ""))
                    for name in ("From", "To", "Cc", "Bcc", "Subject")
                },
                "parts": parts,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    )


async def send_status(
    user_id: UUID,
    integration_id: UUID,
    idempotency_key: str,
    *,
    draft_reference: str,
) -> MailSendResult | None:
    async with AsyncSessionFactory() as db:
        operation = await db.scalar(
            select(MailSendOperation).where(
                MailSendOperation.user_id == user_id,
                MailSendOperation.integration_id == integration_id,
                MailSendOperation.key_hash == _digest(idempotency_key),
            )
        )
    if operation is not None and operation.draft_hash != _digest(draft_reference):
        raise MailError("mail.staleReference")
    if operation is not None and operation.status == "sent":
        return MailSendResult(sent_copy_confirmed=operation.sent_copy_confirmed)
    return None


async def send_once(
    *,
    user_id: UUID,
    integration_id: UUID,
    idempotency_key: str,
    raw: bytes,
    client: MailClient,
    draft_reference: str | None = None,
) -> MailSendResult:
    key_hash = _digest(idempotency_key)
    request_hash = _message_digest(raw)
    draft_hash = _digest(draft_reference) if draft_reference is not None else None

    async with AsyncSessionFactory() as db:
        created = await db.scalar(
            insert(MailSendOperation)
            .values(
                user_id=user_id,
                integration_id=integration_id,
                key_hash=key_hash,
                draft_hash=draft_hash,
                request_hash=request_hash,
                status="started",
            )
            .on_conflict_do_nothing()
            .returning(MailSendOperation.id)
        )
        await db.commit()
        if created is None:
            existing = await db.scalar(
                select(MailSendOperation).where(
                    MailSendOperation.user_id == user_id,
                    MailSendOperation.integration_id == integration_id,
                    or_(
                        MailSendOperation.key_hash == key_hash,
                        MailSendOperation.draft_hash == draft_hash
                        if draft_hash is not None
                        else False,
                    ),
                )
            )
            if existing is None or existing.request_hash != request_hash:
                raise MailError("mail.staleReference")
            if existing.status == "sent":
                return MailSendResult(sent_copy_confirmed=existing.sent_copy_confirmed)
            raise MailError("mail.outcomeUnknown")

    try:
        sent_copy_confirmed = await client.send(raw)
    except MailError as exc:
        async with AsyncSessionFactory() as db:
            if exc.code == "mail.outcomeUnknown":
                await db.execute(
                    update(MailSendOperation)
                    .where(MailSendOperation.id == created)
                    .values(status="outcome_unknown")
                )
            else:
                await db.execute(
                    delete(MailSendOperation).where(MailSendOperation.id == created)
                )
            await db.commit()
        raise

    async with AsyncSessionFactory() as db:
        await db.execute(
            update(MailSendOperation)
            .where(MailSendOperation.id == created)
            .values(status="sent", sent_copy_confirmed=sent_copy_confirmed)
        )
        await db.commit()
    return MailSendResult(sent_copy_confirmed=sent_copy_confirmed)
