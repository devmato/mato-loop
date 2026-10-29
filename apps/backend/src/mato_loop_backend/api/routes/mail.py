from __future__ import annotations

import hashlib
import re
from pathlib import PurePath
from typing import Annotated
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Header, Query, UploadFile, status
from fastapi.responses import Response
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend.api.errors import ApiError
from mato_loop_backend.db.session import get_db_session
from mato_loop_backend.integrations.mail.adapter import (
    MAIL_CREDENTIAL_KIND,
    MailComposeArguments,
    MailConfig,
    MailSearchArguments,
    binding_revision,
    client_for,
    locator_from_token,
    mail_error_code,
    message_payload,
    search_messages,
    token_for,
)
from mato_loop_backend.integrations.mail.client import (
    MailError,
    MessageLocator,
    attachment_bytes,
    build_message,
    parse_message,
)
from mato_loop_backend.integrations.mail.send import MailSendResult, send_once, send_status
from mato_loop_backend.models import UserAccount
from mato_loop_backend.security import load_credential, read_credential, require_user
from mato_loop_backend.settings import (
    InstanceLimits,
    IntegrationInstance,
    SettingsStore,
    load_effective_integrations,
)

router = APIRouter(prefix="/mail", dependencies=[Depends(require_user)])
_CONTENT_TYPE = re.compile(
    r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+$"
)


def _mailbox_role(name: str, flags: frozenset[str]) -> str | None:
    for role in ("inbox", "drafts", "sent", "trash", "junk", "archive"):
        if f"\\{role}" in flags or (role == "inbox" and name.casefold() == "inbox"):
            return role
    return None


def _api_error(error: MailError) -> ApiError:
    code = mail_error_code(error)
    if code == "integration.remoteNotFound":
        return ApiError(404, code)
    if code in {
        "integration.operationUnsupported",
        "integration.permissionDenied",
        "integration.staleReference",
    }:
        return ApiError(409, code)
    if code == "integration.responseTooLarge":
        return ApiError(413, code)
    if code == "tool.invalidArguments":
        return ApiError(422, code)
    return ApiError(502, code)


async def _account(
    db: AsyncSession, user: UserAccount, integration_id: UUID
) -> tuple[IntegrationInstance, object, str]:
    settings = await load_effective_integrations(db, user.id)
    account = next(
        (
            item
            for item in settings.instances
            if item.id == integration_id
            and item.enabled
            and item.integration_type == "mail"
            and item.adapter_type == "imap_smtp"
        ),
        None,
    )
    if account is None or account.credential_id is None:
        raise ApiError(404, "integration.notFound")
    record = await load_credential(
        db, account.credential_id, owner_id=account.owner_id
    )
    if record is None or record.kind != MAIL_CREDENTIAL_KIND:
        raise ApiError(502, "integration.authenticationFailed")
    try:
        credential = read_credential(record)
    except Exception as exc:
        raise ApiError(502, "integration.authenticationFailed") from exc
    if (
        set(credential) != {"password"}
        or not isinstance(credential["password"], str)
        or not credential["password"]
    ):
        raise ApiError(502, "integration.authenticationFailed")
    return account, client_for(account, credential["password"]), binding_revision(account)


async def _attachments(
    uploads: list[UploadFile] | None, limits: InstanceLimits
) -> tuple[tuple[str, str, bytes], ...]:
    uploads = uploads or []
    if len(uploads) > limits.max_attachments_per_message:
        for upload in uploads:
            await upload.close()
        raise ApiError(413, "attachment.tooLarge")
    result = []
    total = 0
    try:
        for upload in uploads:
            data = await upload.read(limits.max_upload_bytes + 1)
            total += len(data)
            if len(data) > limits.max_upload_bytes:
                raise ApiError(413, "integration.attachmentTooLarge")
            if total > limits.max_message_upload_bytes:
                raise ApiError(413, "attachment.tooLarge")
            filename = PurePath(upload.filename or "attachment").name
            filename = "".join(
                character for character in filename if ord(character) >= 32
            )[:500] or "attachment"
            content_type = (upload.content_type or "application/octet-stream").split(
                ";", 1
            )[0][:200]
            if _CONTENT_TYPE.fullmatch(content_type) is None:
                content_type = "application/octet-stream"
            result.append((filename, content_type, data))
    finally:
        for upload in uploads:
            await upload.close()
    return tuple(result)


async def _raw_message(
    account: IntegrationInstance,
    limits: InstanceLimits,
    *,
    to: list[str] | None,
    cc: list[str] | None,
    bcc: list[str] | None,
    subject: str,
    body: str,
    attachments: list[UploadFile] | None,
    retained_attachments: tuple[tuple[str, str, bytes], ...] = (),
    message_id: str | None = None,
) -> bytes:
    try:
        compose = MailComposeArguments(
            integration_id=account.id,
            to=tuple(to or ()),
            cc=tuple(cc or ()),
            bcc=tuple(bcc or ()),
            subject=subject,
            body=body,
        )
    except ValidationError as exc:
        raise ApiError(422, "validation.invalid") from exc
    uploaded = await _attachments(attachments, limits)
    combined = (*retained_attachments, *uploaded)
    if (
        len(combined) > limits.max_attachments_per_message
        or sum(len(item[2]) for item in combined) > limits.max_message_upload_bytes
    ):
        raise ApiError(413, "attachment.tooLarge")
    return build_message(
        from_address=MailConfig.model_validate(account.config).from_address,
        to=compose.to,
        cc=compose.cc,
        bcc=compose.bcc,
        subject=compose.subject,
        body=compose.body,
        attachments=combined,
        message_id=message_id,
    )


async def _retained_attachments(
    client,
    tokens: list[str] | None,
    integration_id: UUID,
    revision: str,
    locator: MessageLocator,
    limits: InstanceLimits,
) -> tuple[tuple[str, str, bytes], ...]:
    if not tokens:
        return ()
    if len(tokens) > limits.max_attachments_per_message:
        raise ApiError(413, "attachment.tooLarge")
    message = await client.get_message(locator)
    result = []
    total = 0
    for token in tokens:
        retained_locator, part = locator_from_token(
            token, integration_id, revision, require_part=True
        )
        if retained_locator != locator or part is None:
            raise ValueError("retained attachment binding mismatch")
        data, filename, content_type = attachment_bytes(message.raw, part)
        total += len(data)
        if len(data) > limits.max_upload_bytes or total > limits.max_message_upload_bytes:
            raise ApiError(413, "attachment.tooLarge")
        result.append((filename, content_type, data))
    return tuple(result)


@router.get("/accounts")
async def list_accounts(
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict:
    settings = await load_effective_integrations(db, user.id)
    return {
        "accounts": [
            {
                "integrationId": str(item.id),
                "name": item.name,
                "fromAddress": item.config.get("from_address"),
            }
            for item in settings.instances
            if item.enabled
            and item.integration_type == "mail"
            and item.adapter_type == "imap_smtp"
        ]
    }


@router.get("/accounts/{integration_id}/mailboxes")
async def list_mailboxes(
    integration_id: UUID,
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict:
    _instance, client, _revision = await _account(db, user, integration_id)
    try:
        mailboxes = await client.list_mailboxes()
    except MailError as exc:
        raise _api_error(exc) from exc
    return {
        "mailboxes": [
            {
                "key": mailbox.name,
                "name": mailbox.name,
                "label": mailbox.name,
                "role": _mailbox_role(mailbox.name, mailbox.flags),
                "flags": sorted(mailbox.flags),
            }
            for mailbox in mailboxes
        ]
    }


@router.get("/accounts/{integration_id}/messages")
async def list_messages(
    integration_id: UUID,
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    mailbox: Annotated[
        str, Query(alias="mailboxKey", min_length=1, max_length=1_000)
    ],
    query: Annotated[str | None, Query(min_length=1, max_length=500)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(min_length=1, max_length=50_000)] = None,
) -> dict:
    _instance, client, revision = await _account(db, user, integration_id)
    try:
        return await search_messages(
            MailSearchArguments(
                integration_id=integration_id, mailbox=mailbox, query=query,
                limit=limit, cursor=cursor,
            ),
            client, revision,
        )
    except ValueError as exc:
        raise ApiError(409, "integration.staleReference", field="cursor") from exc
    except MailError as exc:
        raise _api_error(exc) from exc


@router.get("/accounts/{integration_id}/messages/{message_token}")
async def get_message(
    integration_id: UUID,
    message_token: str,
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict:
    _instance, client, revision = await _account(db, user, integration_id)
    try:
        locator, _part = locator_from_token(message_token, integration_id, revision)
        message = await client.get_message(locator)
    except ValueError as exc:
        raise ApiError(409, "integration.staleReference") from exc
    except MailError as exc:
        raise _api_error(exc) from exc
    return {"message": message_payload(message, integration_id, revision)}


@router.post("/accounts/{integration_id}/drafts", status_code=status.HTTP_201_CREATED)
async def create_draft(
    integration_id: UUID,
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    to: Annotated[list[str] | None, Form()] = None,
    subject: Annotated[str, Form(max_length=1_000)] = "",
    body: Annotated[str, Form(max_length=100_000)] = "",
    cc: Annotated[list[str] | None, Form()] = None,
    bcc: Annotated[list[str] | None, Form()] = None,
    mailbox: Annotated[str | None, Form(max_length=1_000)] = None,
    attachments: Annotated[list[UploadFile] | None, File()] = None,
) -> dict:
    account, client, revision = await _account(db, user, integration_id)
    limits = await SettingsStore.load(db, InstanceLimits)
    try:
        locator = await client.create_draft(
            await _raw_message(
                account,
                limits,
                to=to,
                cc=cc,
                bcc=bcc,
                subject=subject,
                body=body,
                attachments=attachments,
            ),
            mailbox=mailbox,
        )
    except MailError as exc:
        raise _api_error(exc) from exc
    return {
        "messageToken": token_for(integration_id, revision, locator),
        "mailbox": locator.mailbox,
        "isDraft": True,
    }


@router.put("/accounts/{integration_id}/drafts/{message_token}")
async def update_draft(
    integration_id: UUID,
    message_token: str,
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    to: Annotated[list[str] | None, Form()] = None,
    subject: Annotated[str, Form(max_length=1_000)] = "",
    body: Annotated[str, Form(max_length=100_000)] = "",
    cc: Annotated[list[str] | None, Form()] = None,
    bcc: Annotated[list[str] | None, Form()] = None,
    attachments: Annotated[list[UploadFile] | None, File()] = None,
    retained_attachment_tokens: Annotated[
        list[str] | None, Form(alias="retainedAttachmentToken")
    ] = None,
    replace_attachments: Annotated[bool, Form(alias="replaceAttachments")] = False,
) -> dict:
    account, client, revision = await _account(db, user, integration_id)
    limits = await SettingsStore.load(db, InstanceLimits)
    try:
        locator, _part = locator_from_token(message_token, integration_id, revision)
        replace_attachments = replace_attachments or bool(attachments)
        retained = (
            await _retained_attachments(
                client,
                retained_attachment_tokens,
                integration_id,
                revision,
                locator,
                limits,
            )
            if replace_attachments
            else ()
        )
        raw = await _raw_message(
            account,
            limits,
            to=to,
            cc=cc,
            bcc=bcc,
            subject=subject,
            body=body,
            attachments=attachments,
            retained_attachments=retained,
        )
        replacement = await client.update_draft(
            locator,
            raw,
            replace_attachments=replace_attachments,
        )
    except ValueError as exc:
        raise ApiError(409, "integration.staleReference") from exc
    except MailError as exc:
        raise _api_error(exc) from exc
    result = {
        "messageToken": token_for(integration_id, revision, replacement),
        "mailbox": replacement.mailbox,
        "isDraft": True,
    }
    if replace_attachments:
        result["message"] = {
            **message_payload(parse_message(raw, replacement), integration_id, revision),
            "isDraft": True,
        }
    return result


@router.delete(
    "/accounts/{integration_id}/drafts/{message_token}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_draft(
    integration_id: UUID,
    message_token: str,
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> None:
    _instance, client, revision = await _account(db, user, integration_id)
    try:
        locator, _part = locator_from_token(message_token, integration_id, revision)
        await client.delete_draft(locator)
    except ValueError as exc:
        raise ApiError(409, "integration.staleReference") from exc
    except MailError as exc:
        raise _api_error(exc) from exc


@router.post("/accounts/{integration_id}/drafts/{message_token}/send")
async def send_draft(
    integration_id: UUID,
    message_token: str,
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    idempotency_key: Annotated[
        str, Header(alias="Idempotency-Key", min_length=1, max_length=200)
    ],
    to: Annotated[list[str] | None, Form()] = None,
    subject: Annotated[str | None, Form(max_length=1_000)] = None,
    body: Annotated[str | None, Form(max_length=100_000)] = None,
    cc: Annotated[list[str] | None, Form()] = None,
    bcc: Annotated[list[str] | None, Form()] = None,
    attachments: Annotated[list[UploadFile] | None, File()] = None,
    retained_attachment_tokens: Annotated[
        list[str] | None, Form(alias="retainedAttachmentToken")
    ] = None,
) -> MailSendResult:
    account, client, revision = await _account(db, user, integration_id)
    try:
        locator, _part = locator_from_token(message_token, integration_id, revision)
        draft_reference = f"{locator.mailbox}\0{locator.uidvalidity}\0{locator.uid}"
        completed = await send_status(
            user.id,
            integration_id,
            idempotency_key,
            draft_reference=draft_reference,
        )
        if completed is not None:
            if completed.sent_copy_confirmed is True:
                try:
                    await client.delete_draft(locator)
                except MailError:
                    pass
            return completed
        compose_requested = any(
            value is not None
            for value in (
                to,
                subject,
                body,
                cc,
                bcc,
                attachments,
                retained_attachment_tokens,
            )
        )
        if compose_requested:
            if not any((to, cc, bcc)):
                raise ApiError(422, "validation.invalid")
            limits = await SettingsStore.load(db, InstanceLimits)
            retained = await _retained_attachments(
                client,
                retained_attachment_tokens,
                integration_id,
                revision,
                locator,
                limits,
            )
            raw = await _raw_message(
                account,
                limits,
                to=to,
                cc=cc,
                bcc=bcc,
                subject=subject or "",
                body=body or "",
                attachments=attachments,
                retained_attachments=retained,
                message_id=(
                    f"<{hashlib.sha256(idempotency_key.encode()).hexdigest()}"
                    f"@{MailConfig.model_validate(account.config).from_address.rpartition('@')[2]}>"
                ),
            )
        else:
            raw = (await client.get_message(locator)).raw
        result = await send_once(
            user_id=user.id,
            integration_id=integration_id,
            idempotency_key=idempotency_key,
            raw=raw,
            client=client,
            draft_reference=draft_reference,
        )
        if result.sent_copy_confirmed is True:
            try:
                await client.delete_draft(locator)
            except MailError:
                pass
    except ValueError as exc:
        raise ApiError(409, "integration.staleReference") from exc
    except MailError as exc:
        raise _api_error(exc) from exc
    return result


@router.post("/accounts/{integration_id}/messages/send")
async def send_message(
    integration_id: UUID,
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    to: Annotated[list[str], Form()],
    idempotency_key: Annotated[
        str, Header(alias="Idempotency-Key", min_length=1, max_length=200)
    ],
    subject: Annotated[str, Form(max_length=1_000)] = "",
    body: Annotated[str, Form(max_length=100_000)] = "",
    cc: Annotated[list[str] | None, Form()] = None,
    bcc: Annotated[list[str] | None, Form()] = None,
    attachments: Annotated[list[UploadFile] | None, File()] = None,
) -> MailSendResult:
    account, client, _revision = await _account(db, user, integration_id)
    limits = await SettingsStore.load(db, InstanceLimits)
    try:
        return await send_once(
            user_id=user.id,
            integration_id=integration_id,
            idempotency_key=idempotency_key,
            client=client,
            raw=await _raw_message(
                account,
                limits,
                to=to,
                cc=cc,
                bcc=bcc,
                subject=subject,
                body=body,
                attachments=attachments,
                message_id=(
                    f"<{hashlib.sha256(idempotency_key.encode()).hexdigest()}"
                    f"@{MailConfig.model_validate(account.config).from_address.rpartition('@')[2]}>"
                ),
            ),
        )
    except MailError as exc:
        raise _api_error(exc) from exc


@router.delete(
    "/accounts/{integration_id}/messages/{message_token}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_message(
    integration_id: UUID,
    message_token: str,
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> None:
    _instance, client, revision = await _account(db, user, integration_id)
    try:
        locator, _part = locator_from_token(message_token, integration_id, revision)
        await client.delete_message(locator)
    except ValueError as exc:
        raise ApiError(409, "integration.staleReference") from exc
    except MailError as exc:
        raise _api_error(exc) from exc


@router.get("/accounts/{integration_id}/attachments/{attachment_token}")
async def download_attachment(
    integration_id: UUID,
    attachment_token: str,
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> Response:
    _instance, client, revision = await _account(db, user, integration_id)
    try:
        locator, part = locator_from_token(
            attachment_token, integration_id, revision, require_part=True
        )
        assert part is not None
        data, filename, _content_type = await client.get_attachment(locator, part)
    except ValueError as exc:
        raise ApiError(409, "integration.staleReference") from exc
    except MailError as exc:
        raise _api_error(exc) from exc
    return Response(
        data,
        media_type="application/octet-stream",
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": (
                'attachment; filename="download"; '
                f"filename*=UTF-8''{quote(filename, safe='')}"
            ),
            "X-Content-Type-Options": "nosniff",
        },
    )
