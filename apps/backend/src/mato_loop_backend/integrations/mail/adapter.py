from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mato_loop_backend.integrations.contracts import (
    IntegrationAdapterDefinition,
    IntegrationContributions,
    ToolContribution,
)
from mato_loop_backend.integrations.mail.client import (
    Attachment,
    MailClient,
    MailError,
    MessageLocator,
    ParsedMessage,
    build_message,
)
from mato_loop_backend.integrations.mail.send import send_once, send_status
from mato_loop_backend.models import Credential
from mato_loop_backend.security import CredentialCipher, load_credential, read_credential
from mato_loop_backend.settings import IntegrationInstance
from mato_loop_backend.tools.actions import ToolAction
from mato_loop_backend.tools.snapshots import ToolSnapshotPolicy

MAIL_CREDENTIAL_KIND = "mail.password"
_HOST_RE = re.compile(r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?|[0-9A-Fa-f:]+)$")
_PART_RE = re.compile(r"^[1-9][0-9]*(?:\.[1-9][0-9]*)*$")
_READ = ToolAction("mail.read", "E-Mails lesen", category="read")
_DRAFTS = ToolAction(
    "mail.drafts", "E-Mail-Entwürfe verwalten", category="write", mutates=True
)
_SEND = ToolAction(
    "mail.send",
    "E-Mails senden",
    category="write",
    mutates=True,
)
_DELETE = ToolAction(
    "mail.delete",
    "E-Mails löschen",
    category="delete",
    mutates=True,
)


def _address(value: str) -> str:
    value = value.strip()
    if (
        not value
        or len(value) > 320
        or "\r" in value
        or "\n" in value
        or value.count("@") != 1
        or any(character.isspace() for character in value)
    ):
        raise ValueError("invalid email address")
    return value


class MailConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    imap_host: str = Field(min_length=1, max_length=253)
    imap_port: int = Field(ge=1, le=65535)
    imap_security: Literal["tls", "starttls"]
    smtp_host: str = Field(min_length=1, max_length=253)
    smtp_port: int = Field(ge=1, le=65535)
    smtp_security: Literal["tls", "starttls"]
    username: str = Field(min_length=1, max_length=320)
    from_address: str = Field(min_length=3, max_length=320)

    @field_validator("imap_host", "smtp_host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        value = value.strip()
        if _HOST_RE.fullmatch(value) is None:
            raise ValueError("invalid mail host")
        return value

    @field_validator("username")
    @classmethod
    def validate_username(cls, value: str) -> str:
        value = value.strip()
        if not value or "\r" in value or "\n" in value:
            raise ValueError("invalid username")
        return value

    @field_validator("from_address")
    @classmethod
    def validate_from_address(cls, value: str) -> str:
        return _address(value)


class _Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MailListAccountsArguments(_Arguments):
    pass


class MailIntegrationArguments(_Arguments):
    integration_id: UUID


class MailSearchArguments(MailIntegrationArguments):
    mailbox: str = Field(min_length=1, max_length=1_000)
    query: str | None = Field(default=None, min_length=1, max_length=500)
    limit: int = Field(default=50, ge=1, le=100)
    cursor: str | None = Field(default=None, min_length=1, max_length=50_000)


class _MailSearchCursor(_Arguments):
    integration_id: UUID
    binding_revision: str
    mailbox: str
    query: str | None
    uidvalidity: int = Field(gt=0)
    before_uid: int = Field(gt=1)


class MailTokenArguments(MailIntegrationArguments):
    message_token: str = Field(min_length=1, max_length=50_000)


class MailComposeArguments(MailIntegrationArguments):
    to: tuple[str, ...] = Field(default=(), max_length=100)
    cc: tuple[str, ...] = Field(default=(), max_length=100)
    bcc: tuple[str, ...] = Field(default=(), max_length=100)
    subject: str = Field(default="", max_length=1_000)
    body: str = Field(default="", max_length=100_000)

    @field_validator("to", "cc", "bcc")
    @classmethod
    def validate_addresses(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_address(value) for value in values)

    @field_validator("subject")
    @classmethod
    def validate_subject(cls, value: str) -> str:
        if "\r" in value or "\n" in value:
            raise ValueError("subject must not contain line breaks")
        return value


class MailCreateDraftArguments(MailComposeArguments):
    mailbox: str | None = Field(default=None, min_length=1, max_length=1_000)


class MailUpdateDraftArguments(MailComposeArguments):
    message_token: str = Field(min_length=1, max_length=50_000)


class MailSendMessageArguments(MailComposeArguments):
    @model_validator(mode="after")
    def require_recipient(self) -> MailSendMessageArguments:
        if not self.to and not self.cc and not self.bcc:
            raise ValueError("at least one recipient is required")
        return self


class _MailToken(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    integration_id: UUID
    binding_revision: str = Field(max_length=50_000)
    mailbox: str = Field(min_length=1, max_length=1_000)
    uidvalidity: int = Field(gt=0)
    uid: int = Field(gt=0)
    part: str | None = Field(default=None, pattern=r"^[1-9][0-9]*(?:\.[1-9][0-9]*)*$")


_ERROR_CODES = {
    "mail.authenticationFailed": "integration.authenticationFailed",
    "mail.invalidMessage": "tool.invalidArguments",
    "mail.invalidResponse": "integration.invalidResponse",
    "mail.notFound": "integration.remoteNotFound",
    "mail.operationUnsupported": "integration.operationUnsupported",
    "mail.permissionDenied": "integration.permissionDenied",
    "mail.outcomeUnknown": "integration.outcomeUnknown",
    "mail.responseTooLarge": "integration.responseTooLarge",
    "mail.staleReference": "integration.staleReference",
    "mail.unavailable": "integration.unavailable",
}


def mail_error_code(error: MailError) -> str:
    return _ERROR_CODES.get(error.code, "integration.unavailable")


def binding_revision(instance: IntegrationInstance) -> str:
    return json.dumps(
        instance.binding_data(),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _credential_payload(record: Credential | None) -> dict[str, str]:
    if record is None or record.kind != MAIL_CREDENTIAL_KIND:
        raise MailError("mail.authenticationFailed")
    try:
        payload = read_credential(record)
    except Exception as exc:
        raise MailError("mail.authenticationFailed") from exc
    if (
        set(payload) != {"password"}
        or not isinstance(payload["password"], str)
        or not payload["password"]
    ):
        raise MailError("mail.authenticationFailed")
    return payload


def client_for(instance: IntegrationInstance, password: str) -> MailClient:
    config = MailConfig.model_validate(instance.config)
    return MailClient(password=password, **config.model_dump())


def _binding(context: Any, integration_id: UUID) -> tuple[IntegrationInstance, str]:
    selected = context.bound_options.get("integration_id")
    revision = context.bound_options.get("binding_revision")
    if selected != str(integration_id) or not isinstance(revision, str):
        raise ValueError("missing selected integration binding")
    instance = next(
        (
            item
            for item in context.integration_settings.instances
            if item.id == integration_id
            and item.enabled
            and item.integration_type == "mail"
            and item.adapter_type == "imap_smtp"
        ),
        None,
    )
    if instance is None or binding_revision(instance) != revision:
        raise ValueError("stale selected integration binding")
    return instance, revision


async def _selected_client(
    integration_id: UUID, context: Any
) -> tuple[MailClient, IntegrationInstance, str]:
    instance, revision = _binding(context, integration_id)
    if instance.credential_id is None:
        raise MailError("mail.authenticationFailed")
    record = await load_credential(
        context.db, instance.credential_id, owner_id=instance.owner_id
    )
    password = _credential_payload(record)["password"]
    return client_for(instance, password), instance, revision


def token_for(
    integration_id: UUID,
    revision: str,
    locator: MessageLocator,
    *,
    part: str | None = None,
) -> str:
    return CredentialCipher().encrypt(
        _MailToken(
            version=1,
            integration_id=integration_id,
            binding_revision=revision,
            mailbox=locator.mailbox,
            uidvalidity=locator.uidvalidity,
            uid=locator.uid,
            part=part,
        ).model_dump(mode="json")
    ).decode("ascii")


def locator_from_token(
    value: str,
    integration_id: UUID,
    revision: str,
    *,
    require_part: bool = False,
) -> tuple[MessageLocator, str | None]:
    try:
        token = _MailToken.model_validate(
            CredentialCipher().decrypt(value.encode("ascii"))
        )
    except Exception as exc:
        raise ValueError("invalid mail token") from exc
    if (
        token.integration_id != integration_id
        or token.binding_revision != revision
        or (require_part and token.part is None)
        or (not require_part and token.part is not None)
    ):
        raise ValueError("mail token binding mismatch")
    return MessageLocator(token.mailbox, token.uidvalidity, token.uid), token.part


def _summary_payload(summary, integration_id: UUID, revision: str) -> dict[str, Any]:
    return {
        "messageToken": token_for(integration_id, revision, summary.locator),
        "mailbox": summary.locator.mailbox,
        "subject": summary.subject,
        "from": summary.sender,
        "to": list(summary.recipients),
        "date": summary.date.isoformat() if summary.date is not None else None,
        "size": summary.size,
        "flags": sorted(summary.flags),
    }


def message_payload(
    message: ParsedMessage, integration_id: UUID, revision: str
) -> dict[str, Any]:
    return {
        **_summary_payload(message.summary, integration_id, revision),
        "cc": list(message.cc),
        "bcc": list(message.bcc),
        "body": message.body,
        "bodyFormat": message.body_format,
        "attachments": [
            attachment_payload(
                attachment, integration_id, revision, message.summary.locator
            )
            for attachment in message.attachments
        ],
        "bodyTruncated": message.body_truncated,
        "attachmentsTruncated": message.attachments_truncated,
    }


def attachment_payload(
    attachment: Attachment,
    integration_id: UUID,
    revision: str,
    locator: MessageLocator,
) -> dict[str, Any]:
    return {
        "attachmentToken": token_for(
            integration_id, revision, locator, part=attachment.part
        ),
        "filename": attachment.filename,
        "contentType": attachment.content_type,
        "size": attachment.size,
    }


def _result(payload: dict[str, Any]):
    from mato_loop_backend.tools.contracts import ToolResult

    return ToolResult.json({**payload, "contentIsUntrusted": True})


def _error(error: MailError):
    from mato_loop_backend.tools.contracts import ToolResult

    return ToolResult.error(mail_error_code(error))


async def mail_list_accounts(_arguments: MailListAccountsArguments, context: Any):
    bound = context.bound_options.get("integration_ids", ())
    accounts = [
        {"integrationId": str(item.id), "name": item.name[:160]}
        for item in context.integration_settings.instances
        if str(item.id) in bound
        and item.enabled
        and item.integration_type == "mail"
        and item.adapter_type == "imap_smtp"
    ]
    return _result({"accounts": accounts})


async def mail_list_folders(arguments: MailIntegrationArguments, context: Any):
    try:
        client, _instance, _revision = await _selected_client(
            arguments.integration_id, context
        )
        mailboxes = await client.list_mailboxes()
        return _result(
            {
                "folders": [
                    {"mailbox": item.name, "flags": sorted(item.flags)}
                    for item in mailboxes
                ]
            }
        )
    except MailError as exc:
        return _error(exc)


async def search_messages(
    arguments: MailSearchArguments, client: MailClient, revision: str
) -> dict[str, Any]:
    cursor = None
    if arguments.cursor is not None:
        try:
            cursor = _MailSearchCursor.model_validate(
                CredentialCipher().decrypt(arguments.cursor.encode("ascii"))
            )
        except Exception as exc:
            raise ValueError("invalid mail search cursor") from exc
        if (
            cursor.integration_id != arguments.integration_id
            or cursor.binding_revision != revision
            or cursor.mailbox != arguments.mailbox
            or cursor.query != arguments.query
        ):
            raise ValueError("mail search cursor binding mismatch")
    page = await client.search(
        arguments.mailbox, query=arguments.query, limit=arguments.limit,
        before_uid=cursor.before_uid if cursor else None,
        expected_uidvalidity=cursor.uidvalidity if cursor else None,
    )
    next_cursor = None
    if page.next_uid is not None:
        next_cursor = CredentialCipher().encrypt(
            _MailSearchCursor(
                integration_id=arguments.integration_id,
                binding_revision=revision,
                mailbox=arguments.mailbox,
                query=arguments.query,
                uidvalidity=page.uidvalidity,
                before_uid=page.next_uid,
            ).model_dump(mode="json")
        ).decode("ascii")
    return {
        "messages": [
            _summary_payload(item, arguments.integration_id, revision) for item in page.messages
        ],
        "nextCursor": next_cursor,
    }


async def mail_search(arguments: MailSearchArguments, context: Any):
    try:
        client, _instance, revision = await _selected_client(
            arguments.integration_id, context
        )
        return _result(await search_messages(arguments, client, revision))
    except ValueError:
        from mato_loop_backend.tools.contracts import ToolResult

        return ToolResult.error("integration.staleReference", field="cursor")
    except MailError as exc:
        return _error(exc)


async def mail_get_message(arguments: MailTokenArguments, context: Any):
    try:
        client, _instance, revision = await _selected_client(
            arguments.integration_id, context
        )
        locator, _part = locator_from_token(
            arguments.message_token, arguments.integration_id, revision
        )
        return _result(
            {
                "message": message_payload(
                    await client.get_message(locator),
                    arguments.integration_id,
                    revision,
                )
            }
        )
    except ValueError:
        from mato_loop_backend.tools.contracts import ToolResult

        return ToolResult.error("integration.staleReference", field="message_token")
    except MailError as exc:
        return _error(exc)


def _raw(arguments: MailComposeArguments, instance: IntegrationInstance) -> bytes:
    return build_message(
        from_address=MailConfig.model_validate(instance.config).from_address,
        to=arguments.to,
        cc=arguments.cc,
        bcc=arguments.bcc,
        subject=arguments.subject,
        body=arguments.body,
    )


async def mail_create_draft(arguments: MailCreateDraftArguments, context: Any):
    try:
        client, instance, revision = await _selected_client(
            arguments.integration_id, context
        )
        locator = await client.create_draft(
            _raw(arguments, instance), mailbox=arguments.mailbox
        )
        return _result(
            {
                "messageToken": token_for(
                    arguments.integration_id, revision, locator
                ),
                "mailbox": locator.mailbox,
            }
        )
    except MailError as exc:
        return _error(exc)


async def mail_update_draft(arguments: MailUpdateDraftArguments, context: Any):
    try:
        client, instance, revision = await _selected_client(
            arguments.integration_id, context
        )
        locator, _part = locator_from_token(
            arguments.message_token, arguments.integration_id, revision
        )
        replacement = await client.update_draft(locator, _raw(arguments, instance))
        return _result(
            {
                "messageToken": token_for(
                    arguments.integration_id, revision, replacement
                ),
                "mailbox": replacement.mailbox,
            }
        )
    except ValueError:
        from mato_loop_backend.tools.contracts import ToolResult

        return ToolResult.error("integration.staleReference", field="message_token")
    except MailError as exc:
        return _error(exc)


async def mail_delete_draft(arguments: MailTokenArguments, context: Any):
    try:
        client, _instance, revision = await _selected_client(
            arguments.integration_id, context
        )
        locator, _part = locator_from_token(
            arguments.message_token, arguments.integration_id, revision
        )
        expected = context.bound_options.get("mail_approved_raw_hash")
        if expected is not None:
            message = await client.get_message(locator)
            if hashlib.sha256(message.raw).hexdigest() != expected:
                raise MailError("mail.staleReference")
        await client.delete_draft(locator)
        return _result({"deleted": True})
    except ValueError:
        from mato_loop_backend.tools.contracts import ToolResult

        return ToolResult.error("integration.staleReference", field="message_token")
    except MailError as exc:
        return _error(exc)


async def mail_send_draft(arguments: MailTokenArguments, context: Any):
    try:
        client, _instance, revision = await _selected_client(
            arguments.integration_id, context
        )
        key = f"agent:{context.run_id}:{context.tool_call.id}"
        locator, _part = locator_from_token(
            arguments.message_token, arguments.integration_id, revision
        )
        draft_reference = f"{locator.mailbox}\0{locator.uidvalidity}\0{locator.uid}"
        completed = await send_status(
            context.session.user_id,
            arguments.integration_id,
            key,
            draft_reference=draft_reference,
        )
        if completed is not None:
            if completed.sent_copy_confirmed is True:
                try:
                    await client.delete_draft(locator)
                except MailError:
                    pass
            return _result(completed.model_dump(by_alias=True))
        message = await client.get_message(locator)
        expected = context.bound_options.get("mail_approved_raw_hash")
        if expected is not None and hashlib.sha256(message.raw).hexdigest() != expected:
            raise MailError("mail.staleReference")
        result = await send_once(
            user_id=context.session.user_id,
            integration_id=arguments.integration_id,
            idempotency_key=key,
            raw=message.raw,
            client=client,
            draft_reference=draft_reference,
        )
        if result.sent_copy_confirmed is True:
            try:
                await client.delete_draft(locator)
            except MailError:
                pass
        return _result(result.model_dump(by_alias=True))
    except ValueError:
        from mato_loop_backend.tools.contracts import ToolResult

        return ToolResult.error("integration.staleReference", field="message_token")
    except MailError as exc:
        return _error(exc)


async def mail_send_message(arguments: MailSendMessageArguments, context: Any):
    try:
        client, instance, _revision = await _selected_client(
            arguments.integration_id, context
        )
        key = f"agent:{context.run_id}:{context.tool_call.id}"
        from_address = MailConfig.model_validate(instance.config).from_address
        raw = build_message(
            from_address=from_address,
            to=arguments.to,
            cc=arguments.cc,
            bcc=arguments.bcc,
            subject=arguments.subject,
            body=arguments.body,
            message_id=f"<{hashlib.sha256(key.encode()).hexdigest()}@{from_address.rpartition('@')[2]}>",
        )
        result = await send_once(
            user_id=context.session.user_id,
            integration_id=arguments.integration_id,
            idempotency_key=key,
            raw=raw,
            client=client,
        )
        return _result(result.model_dump(by_alias=True))
    except MailError as exc:
        return _error(exc)


async def _delete(arguments: MailTokenArguments, context: Any):
    try:
        client, _instance, revision = await _selected_client(
            arguments.integration_id, context
        )
        locator, _part = locator_from_token(
            arguments.message_token, arguments.integration_id, revision
        )
        expected = context.bound_options.get("mail_approved_raw_hash")
        if expected is not None:
            message = await client.get_message(locator)
            if hashlib.sha256(message.raw).hexdigest() != expected:
                raise MailError("mail.staleReference")
        await client.delete_message(locator)
        return _result({"deleted": True})
    except ValueError:
        from mato_loop_backend.tools.contracts import ToolResult

        return ToolResult.error("integration.staleReference", field="message_token")
    except MailError as exc:
        return _error(exc)


async def mail_delete_message(arguments: MailTokenArguments, context: Any):
    return await _delete(arguments, context)


async def _permission(capability: str, operation: str, arguments: Any, context: Any):
    from dataclasses import replace

    from mato_loop_backend.permissions.policy import (
        PermissionDecision,
        PermissionDisplayItem,
        integration_operation_permission,
    )

    instance, revision = _binding(context, arguments.integration_id)
    serialized = arguments.model_dump_json(exclude={"integration_id"})
    identity = arguments.model_dump(mode="json", exclude={"integration_id"})
    locator = None
    for field in ("message_token", "attachment_token"):
        if field not in identity:
            continue
        try:
            locator, part = locator_from_token(
                identity[field],
                arguments.integration_id,
                revision,
                require_part=field == "attachment_token",
            )
        except ValueError:
            raise MailError("integration.staleReference") from None
        # Hash the stable locator, never the randomized ciphertext or plaintext scope.
        identity[field] = {
            "mailbox": locator.mailbox,
            "uidvalidity": locator.uidvalidity,
            "uid": locator.uid,
            "part": part,
        }
        serialized = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(serialized.encode()).hexdigest()[:24]
    labels = {
        "mail.read": ("E-Mails lesen?", "Der Agent möchte E-Mail-Daten lesen."),
        "mail.drafts": (
            "E-Mail-Entwurf ändern?",
            "Der Agent möchte einen E-Mail-Entwurf ändern.",
        ),
        "mail.send": ("E-Mail senden?", "Der Agent möchte eine E-Mail senden."),
        "mail.delete": ("E-Mail löschen?", "Der Agent möchte eine E-Mail löschen."),
    }
    title, summary = labels[capability]
    request = integration_operation_permission(
        capability=capability,
        integration_id=arguments.integration_id,
        binding_revision=revision,
        operation_key=f"{operation}:{digest}",
        title=title,
        summary=summary,
        resource_display=instance.name,
        action_kind=operation,
        risk_level="high" if capability in {"mail.send", "mail.delete"} else "medium",
    )
    if (
        capability in {"mail.send", "mail.delete"}
        and hasattr(context, "ask_handler")
        and await context.ask_handler(request, check_only=True)
        == PermissionDecision.DENY
    ):
        return (request,)
    display_items = ()
    if capability == "mail.send":
        if locator is not None:
            client, _instance, _revision = await _selected_client(
                arguments.integration_id, context
            )
            message = await client.get_message(locator)
            if message.body_truncated or message.attachments_truncated:
                raise MailError("mail.responseTooLarge")
            context.bound_options["mail_approved_raw_hash"] = hashlib.sha256(
                message.raw
            ).hexdigest()
            values = {
                "An": message.summary.recipients,
                "Cc": message.cc,
                "Bcc": message.bcc,
                "Betreff": message.summary.subject,
                "Nachricht": message.body,
                "Anhänge": tuple(item.filename for item in message.attachments),
            }
        else:
            values = {
                "An": arguments.to,
                "Cc": arguments.cc,
                "Bcc": arguments.bcc,
                "Betreff": arguments.subject,
                "Nachricht": arguments.body,
            }
        display_items = tuple(
            PermissionDisplayItem(
                label,
                value,
                "list" if isinstance(value, tuple) else "text",
            )
            for label, value in values.items()
            if value
        )
    elif capability == "mail.delete" and locator is not None:
        client, _instance, _revision = await _selected_client(
            arguments.integration_id, context
        )
        message = await client.get_message(locator)
        context.bound_options["mail_approved_raw_hash"] = hashlib.sha256(
            message.raw
        ).hexdigest()
        display_items = (
            PermissionDisplayItem("Ordner", locator.mailbox),
            PermissionDisplayItem(
                "Betreff", message.summary.subject or "(Kein Betreff)"
            ),
            PermissionDisplayItem("Von", message.summary.sender),
        )
    return (replace(request, display_items=display_items),)


def _accounts_permission(_arguments: Any, _context: Any):
    from mato_loop_backend.permissions.policy import PermissionRequest

    return (
        PermissionRequest(
            capability="mail.read",
            resource_type="network_service",
            resource_scope="mail_accounts",
            action_kind="mail_list_accounts",
            risk_level="medium",
            title="E-Mail-Konten auflisten?",
            summary="Der Agent möchte die konfigurierten E-Mail-Konten lesen.",
            resource_display="Konfigurierte E-Mail-Konten",
            use_integration_policy=False,
        ),
    )


def _tool(
    name: str,
    description: str,
    arguments_model: type[BaseModel],
    handler,
    action: ToolAction,
    operation: str,
    *,
    aggregate: bool = False,
    snapshot_policy: ToolSnapshotPolicy | None = None,
) -> ToolContribution:
    return ToolContribution(
        name=name,
        description=description,
        arguments_model=arguments_model,
        handler=handler,
        label=action.label,
        icon="mail",
        actions=(action,),
        instance_argument=None if aggregate else "integration_id",
        authorization_factory=(
            _accounts_permission
            if aggregate
            else lambda arguments, context: _permission(
                action.capability, operation, arguments, context
            )
        ),
        releases_private_data=action is _READ,
        snapshot_policy=snapshot_policy,
    )


MAIL_SHARED_TOOLS = (
    _tool(
        "mail_list_accounts",
        "List active mail accounts before selecting an account.",
        MailListAccountsArguments,
        mail_list_accounts,
        _READ,
        "mail_list_accounts",
        aggregate=True,
        snapshot_policy=ToolSnapshotPolicy("accounts", ("integrationId", "name"), max_age_seconds=None),
    ),
    _tool(
        "mail_list_folders",
        "List folders in one mail account.",
        MailIntegrationArguments,
        mail_list_folders,
        _READ,
        "mail_list_folders",
        snapshot_policy=ToolSnapshotPolicy("folders", ("mailbox", "flags")),
    ),
    _tool(
        "mail_search",
        "List messages in one folder when query is omitted, or search their full text. "
        "Return up to 100 messages, newest UIDs first. Pass nextCursor as cursor with the "
        "same account, folder and query to read older results.",
        MailSearchArguments,
        mail_search,
        _READ,
        "mail_search",
    ),
    _tool(
        "mail_get_message",
        "Read one previously returned message and bounded attachment metadata.",
        MailTokenArguments,
        mail_get_message,
        _READ,
        "mail_get_message",
    ),
    _tool(
        "mail_create_draft",
        "Create a plain-text email draft. Omit mailbox to use the account's Drafts folder; "
        "an explicit mailbox must have the Drafts role, not INBOX.",
        MailCreateDraftArguments,
        mail_create_draft,
        _DRAFTS,
        "mail_create_draft",
    ),
    _tool(
        "mail_update_draft",
        "Replace a previously returned plain-text email draft.",
        MailUpdateDraftArguments,
        mail_update_draft,
        _DRAFTS,
        "mail_update_draft",
    ),
    _tool(
        "mail_delete_draft",
        "Delete a previously returned email draft.",
        MailTokenArguments,
        mail_delete_draft,
        _DELETE,
        "mail_delete_draft",
    ),
    _tool(
        "mail_send_draft",
        "Send a previously returned email draft without automatic retries. sent means SMTP "
        "accepted the message, not recipient delivery. sentCopyConfirmed reports whether "
        "the Sent copy was confirmed; false or null must not trigger another send.",
        MailTokenArguments,
        mail_send_draft,
        _SEND,
        "mail_send_draft",
    ),
    _tool(
        "mail_send_message",
        "Send one new plain-text email without automatic retries. sent means SMTP accepted "
        "the message, not recipient delivery. sentCopyConfirmed reports whether the Sent "
        "copy was confirmed; false or null must not trigger another send.",
        MailSendMessageArguments,
        mail_send_message,
        _SEND,
        "mail_send_message",
    ),
    _tool(
        "mail_delete_message",
        "Delete a previously returned message without a global expunge.",
        MailTokenArguments,
        mail_delete_message,
        _DELETE,
        "mail_delete_message",
    ),
)

MAIL_ADAPTER = IntegrationAdapterDefinition(
    integration_type="mail",
    adapter_type="imap_smtp",
    config_model=MailConfig,
    contribution_factory=lambda _instance: IntegrationContributions(),
    resource_actions=(_READ, _DRAFTS, _SEND, _DELETE),
    shared_tools=MAIL_SHARED_TOOLS,
)
