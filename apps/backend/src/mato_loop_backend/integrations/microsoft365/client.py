from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any, Literal, TypeVar
from urllib.parse import quote
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from mato_loop_backend.integrations.mail.client import MailError
from mato_loop_backend.integrations.microsoft365.composition import (
    MAX_OUTGOING_BYTES,
    SIMPLE_ATTACHMENT_BYTES,
    inline_payload,
    reply_body,
    validate_signature,
)

GRAPH_ORIGIN = "https://graph.microsoft.com"
GRAPH_BASE = GRAPH_ORIGIN + "/v1.0"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
# Simple attachment creation echoes base64 content plus bounded Graph metadata.
MAX_ATTACHMENT_RESPONSE_BYTES = 4 * ((SIMPLE_ATTACHMENT_BYTES + 2) // 3) + 64 * 1024
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
logger = logging.getLogger(__name__)


def _hide_upload_url(record: logging.LogRecord) -> bool:
    # Upload URLs contain a credential; httpx logs request URLs at INFO.
    return not (
        isinstance(record.args, tuple)
        and any(
            isinstance(value, httpx.URL) and (
                (value.host == "outlook.office.com" and value.query)
                or (value.host == "graph.microsoft.com" and "/attachmentSessions/" in value.path)
            )
            for value in record.args
        )
    )


logging.getLogger("httpx").addFilter(_hide_upload_url)


class Microsoft365Error(MailError):
    pass


class Microsoft365Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: UUID
    client_id: UUID
    auth_flow: Literal["device_code", "browser"] = "device_code"
    allow_drafts: bool = False
    allow_send: bool = False
    allow_delete: bool = False
    signature_html: str = Field(default="", max_length=750_000)
    read_calendar: bool = False
    write_calendar: bool = False
    delete_calendar: bool = False
    read_tasks: bool = False
    write_tasks: bool = False
    delete_tasks: bool = False

    @field_validator("signature_html")
    @classmethod
    def signature_markup(cls, value: str) -> str:
        return validate_signature(value)

    def authorization_binding(self) -> dict:
        return self.model_dump(mode="json", exclude={"signature_html"})

    @property
    def scopes(self) -> str:
        mail = "Mail.ReadWrite" if self.allow_drafts or self.allow_send or self.allow_delete else "Mail.Read"
        send = " https://graph.microsoft.com/Mail.Send" if self.allow_send else ""
        calendar = (
            " https://graph.microsoft.com/Calendars.ReadWrite" if self.write_calendar or self.delete_calendar
            else " https://graph.microsoft.com/Calendars.Read" if self.read_calendar else ""
        )
        tasks = (
            " https://graph.microsoft.com/Tasks.ReadWrite" if self.write_tasks or self.delete_tasks
            else " https://graph.microsoft.com/Tasks.Read" if self.read_tasks else ""
        )
        return f"https://graph.microsoft.com/{mail}{send}{calendar}{tasks} offline_access"

    @property
    def authority(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0"


async def request_json(
    method: Literal["GET", "POST", "PATCH", "DELETE", "PUT"], url: str, *, uncertain_effect: bool = False,
    max_response_bytes: int = MAX_RESPONSE_BYTES, **kwargs: Any,
) -> tuple[int, dict[str, Any]]:
    if not 1 <= max_response_bytes <= MAX_ATTACHMENT_RESPONSE_BYTES:
        raise ValueError("Invalid Microsoft 365 response size limit")
    try:
        async with (
            asyncio.timeout(30),
            httpx.AsyncClient(timeout=30, follow_redirects=False) as client,
        ):
            async with client.stream(method, url, **kwargs) as response:
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > max_response_bytes:
                        raise Microsoft365Error(
                            "integration.outcomeUnknown" if uncertain_effect else "integration.responseTooLarge"
                        )
                if not body and response.status_code in {201, 202, 204}:
                    return response.status_code, {}
                try:
                    payload = json.loads(body)
                except (ValueError, UnicodeError) as exc:
                    raise Microsoft365Error(
                        "integration.outcomeUnknown" if uncertain_effect else "integration.invalidResponse"
                    ) from exc
                if not isinstance(payload, dict):
                    raise Microsoft365Error(
                        "integration.outcomeUnknown" if uncertain_effect else "integration.invalidResponse"
                    )
                return response.status_code, payload
    except (httpx.HTTPError, TimeoutError) as exc:
        logger.warning("Microsoft 365 request failed with %s", type(exc).__name__)
        raise Microsoft365Error(
            "integration.outcomeUnknown" if uncertain_effect else "integration.unavailable"
        ) from exc


class _Remote(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)


class Folder(_Remote):
    id: str = Field(min_length=1, max_length=4096)
    displayName: str = Field(max_length=1024)
    totalItemCount: int
    unreadItemCount: int
    childFolderCount: int


class Address(_Remote):
    name: str = Field(default="", max_length=4096)
    address: str = Field(default="", max_length=4096)


class Recipient(_Remote):
    emailAddress: Address


class Body(_Remote):
    contentType: Literal["text", "html"]
    content: str


class Message(_Remote):
    id: str = Field(min_length=1, max_length=4096)
    subject: str = Field(default="", max_length=10_000)
    from_: Recipient | None = Field(default=None, alias="from")
    toRecipients: list[Recipient] = Field(default_factory=list, max_length=1000)
    ccRecipients: list[Recipient] = Field(default_factory=list, max_length=1000)
    bccRecipients: list[Recipient] = Field(default_factory=list, max_length=1000)
    changeKey: str | None = Field(default=None, max_length=4096)
    parentFolderId: str | None = Field(default=None, max_length=4096)
    receivedDateTime: str | None = None
    isRead: bool = False
    hasAttachments: bool = False
    body: Body | None = None
    bodyPreview: str | None = None
    importance: str | None = Field(default=None, max_length=128)
    conversationId: str | None = Field(default=None, max_length=4096)
    isDraft: bool = False


class Attachment(_Remote):
    type: str = Field(alias="@odata.type", max_length=128)
    id: str = Field(min_length=1, max_length=4096)
    name: str = Field(max_length=1024)
    contentType: str | None = Field(default=None, max_length=255)
    size: int = Field(ge=0)
    isInline: bool = False


class Calendar(_Remote):
    id: str = Field(min_length=1, max_length=4096)
    name: str = Field(max_length=1024)
    isDefaultCalendar: bool = False


class EventTime(_Remote):
    dateTime: str = Field(max_length=128)
    timeZone: str = Field(max_length=128)


class Location(_Remote):
    displayName: str = Field(default="", max_length=4096)


class Event(_Remote):
    id: str = Field(min_length=1, max_length=4096)
    subject: str = Field(default="", max_length=10_000)
    start: EventTime
    end: EventTime
    isAllDay: bool = False
    isCancelled: bool = False
    isOnlineMeeting: bool = False
    showAs: str = Field(default="unknown", max_length=128)
    location: Location | None = None
    organizer: Recipient | None = None
    body: Body | None = None
    bodyPreview: str | None = None
    importance: str | None = Field(default=None, max_length=128)
    changeKey: str | None = Field(default=None, max_length=4096)
    type: str = Field(default="singleInstance", max_length=128)
    seriesMasterId: str | None = Field(default=None, max_length=4096)
    isOrganizer: bool = True
    attendees: list[dict[str, Any]] = Field(default_factory=list, max_length=1000)
    recurrence: dict[str, Any] | None = None


SUMMARY_FIELDS = "id,subject,from,toRecipients,receivedDateTime,isRead,hasAttachments,conversationId,isDraft,importance,bodyPreview"
RemoteModel = TypeVar("RemoteModel", bound=BaseModel)


class GraphClient:
    def __init__(self, token: str) -> None:
        self.token = token

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Prefer": 'outlook.body-content-type="text", IdType="ImmutableId", outlook.timezone="UTC"',
            "Accept": "application/json",
        }

    @staticmethod
    def check_status(status: int, expected: int = 200, *, mutation: bool = False) -> None:
        if status == expected:
            return
        logger.warning("Microsoft Graph request failed with HTTP %s", status)
        code = {
            401: "microsoft365.authorizationRequired",
            403: "integration.permissionDenied",
            404: "integration.remoteNotFound",
            400: "tool.invalidArguments",
            412: "integration.staleReference",
            413: "integration.attachmentTooLarge",
            429: "microsoft365.throttled",
        }.get(status, "integration.outcomeUnknown" if mutation else "integration.unavailable")
        raise Microsoft365Error(code)

    async def get(
        self, path: str, *, params: dict[str, Any] | None = None, cursor: str | None = None,
        original_body: bool = False,
    ) -> dict[str, Any]:
        url = GRAPH_BASE + path
        if cursor is not None:
            candidate, expected = httpx.URL(cursor), httpx.URL(url)
            if (
                candidate.scheme != "https"
                or candidate.host != expected.host
                or candidate.port != expected.port
                or candidate.userinfo
                or candidate.fragment
                or candidate.raw_path.split(b"?", 1)[0] != expected.raw_path
            ):
                raise Microsoft365Error("integration.staleReference")
            url, params = cursor, None
        status, payload = await request_json(
            "GET",
            url,
            params=params,
            headers=self.headers if not original_body else {
                **self.headers, "Prefer": 'IdType="ImmutableId", outlook.timezone="UTC"',
            },
        )
        self.check_status(status)
        return payload

    async def page(
        self,
        path: str,
        model: type[RemoteModel],
        *,
        params: dict[str, Any],
        cursor: str | None,
    ) -> tuple[list[RemoteModel], str | None]:
        payload = await self.get(path, params=params, cursor=cursor)
        try:
            values = payload["value"]
            if not isinstance(values, list) or len(values) > 100:
                raise ValueError("Invalid collection")
            result = [model.model_validate(item) for item in values]
            next_link = payload.get("@odata.nextLink")
            if next_link is not None and (
                not isinstance(next_link, str) or len(next_link) > 32_000
            ):
                raise ValueError("Invalid continuation")
            return result, next_link
        except (KeyError, ValueError, ValidationError) as exc:
            raise Microsoft365Error("integration.invalidResponse") from exc

    async def message(self, message_id: str, *, original_body: bool = False) -> Message:
        payload = await self.get(
            "/me/messages/" + quote(message_id, safe=""),
            params={"$select": SUMMARY_FIELDS + ",ccRecipients,bccRecipients,body,changeKey,parentFolderId"},
            original_body=original_body,
        )
        try:
            message = Message.model_validate(payload)
            if message.body is None:
                raise ValueError("Missing message body")
            return message
        except (ValueError, ValidationError) as exc:
            raise Microsoft365Error("integration.invalidResponse") from exc

    async def download(self, message_id: str, attachment_id: str) -> bytes:
        path = "/me/messages/" + quote(message_id, safe="") + "/attachments/" + quote(attachment_id, safe="")
        metadata = await self.get(path, params={"$select": "id,name,contentType,size,isInline"})
        try:
            attachment = Attachment.model_validate(metadata)
        except ValidationError as exc:
            raise Microsoft365Error("integration.invalidResponse") from exc
        if attachment.type != "#microsoft.graph.fileAttachment":
            raise Microsoft365Error("integration.operationUnsupported")
        if attachment.size > MAX_ATTACHMENT_BYTES:
            raise Microsoft365Error("integration.attachmentTooLarge")
        return await self.download_file(path, max_bytes=MAX_ATTACHMENT_BYTES)

    async def download_file(self, path: str, *, max_bytes: int) -> bytes:
        try:
            async with asyncio.timeout(30), httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
                async with client.stream("GET", GRAPH_BASE + path + "/$value", headers=self.headers) as response:
                    self.check_status(response.status_code)
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > max_bytes:
                            raise Microsoft365Error("integration.attachmentTooLarge")
                    return bytes(data)
        except (httpx.HTTPError, TimeoutError) as exc:
            logger.warning("Microsoft 365 attachment download failed with %s", type(exc).__name__)
            raise Microsoft365Error("integration.unavailable") from exc

    async def reply_draft(self, message_id: str, body: str, *, message: dict | None = None) -> Message:
        draft = await self._create_draft(
            "/me/messages/" + quote(message_id, safe="") + "/createReply",
            {}, original_body=True,
        )
        try:
            if draft.body is None:
                raise Microsoft365Error("integration.invalidResponse")
            path = "/me/messages/" + quote(draft.id, safe="")
            content = message["body"] if message is not None else {"contentType": "text", "content": body}
            subject = draft.subject
            while subject[:3].lower() in {"re:", "aw:"}:
                subject = subject[3:].lstrip()
            subject = "Aw: " + subject
            update = {"subject": subject, "body": reply_body(content, draft.body.model_dump())}
            if message is not None:
                update.update({key: message[key] for key in ("ccRecipients", "bccRecipients") if message.get(key)})
            await self._write("PATCH", path, 200, json=update)
            if message is not None:
                for attachment in message.get("attachments", ()):
                    await self.add_attachment(draft.id, attachment)
        except Microsoft365Error as exc:
            logger.warning("Microsoft 365 reply draft was only partially created: %s", exc.code)
            raise Microsoft365Error("integration.outcomeUnknown") from exc
        return draft.model_copy(update={"body": None, "subject": subject})

    async def new_draft(self, to, subject: str, body: str, *, message: dict | None = None) -> Message:
        if message is not None and not inline_payload(message):
            draft = await self._create_draft("/me/messages", {key: value for key, value in message.items() if key != "attachments"})
            try:
                for attachment in message.get("attachments", ()):
                    await self.add_attachment(draft.id, attachment)
                return draft
            except Microsoft365Error as exc:
                logger.warning("Microsoft 365 draft was only partially created: %s", exc.code)
                raise Microsoft365Error("integration.outcomeUnknown") from exc
        return await self._create_draft(
            "/me/messages",
            message if message is not None else {
                "subject": subject,
                "body": {"contentType": "text", "content": body},
                "toRecipients": [{"emailAddress": {"address": address}} for address in to],
            },
        )

    async def _create_draft(self, path: str, payload: dict[str, Any], *, original_body: bool = False) -> Message:
        status, payload = await request_json(
            "POST",
            GRAPH_BASE + path,
            headers=self.headers if not original_body else {
                **self.headers, "Prefer": 'IdType="ImmutableId", outlook.timezone="UTC"',
            },
            json=payload,
            uncertain_effect=True,
        )
        self.check_status(status, 201, mutation=True)
        try:
            result = Message.model_validate({key: value for key, value in payload.items() if original_body or key != "body"})
            if not result.isDraft:
                raise ValueError("Missing draft confirmation")
            return result
        except (ValueError, ValidationError) as exc:
            raise Microsoft365Error("integration.outcomeUnknown") from exc

    async def send_message(self, message: dict) -> None:
        if inline_payload(message):
            await self._write("POST", "/me/sendMail", 202, json={"message": message})
            return
        draft = await self.new_draft((), "", "", message=message)
        try:
            await self.send_draft(draft.id)
        except Microsoft365Error as exc:
            logger.warning("Microsoft 365 staged send failed: %s", exc.code)
            raise Microsoft365Error("integration.outcomeUnknown") from exc

    async def add_attachment(self, message_id: str, attachment: dict) -> None:
        path = "/me/messages/" + quote(message_id, safe="") + "/attachments"
        data = base64.b64decode(attachment["contentBytes"], validate=True)
        if len(data) < SIMPLE_ATTACHMENT_BYTES:
            await self._write("POST", path, 201, json=attachment, max_response_bytes=MAX_ATTACHMENT_RESPONSE_BYTES)
            return
        if len(data) > MAX_OUTGOING_BYTES or attachment.get("isInline"):
            raise Microsoft365Error("integration.attachmentTooLarge")
        status, session = await request_json(
            "POST", GRAPH_BASE + path + "/createUploadSession", headers=self.headers,
            json={"AttachmentItem": {
                "attachmentType": "file", "name": attachment["name"],
                "contentType": attachment["contentType"], "size": len(data),
            }}, uncertain_effect=True,
        )
        self.check_status(status, 201, mutation=True)
        url = session.get("uploadUrl")
        if not isinstance(url, str) or len(url) > 32_000:
            raise Microsoft365Error("integration.outcomeUnknown")
        try:
            candidate = httpx.URL(url)
        except httpx.InvalidURL as exc:
            raise Microsoft365Error("integration.outcomeUnknown") from exc
        if (
            candidate.scheme != "https" or candidate.host != "outlook.office.com"
            or candidate.port not in {None, 443} or candidate.userinfo or candidate.fragment
            or not candidate.path.startswith("/api/") or not candidate.query
        ):
            raise Microsoft365Error("integration.outcomeUnknown")
        await self.upload_chunks(url, data)

    async def upload_chunks(self, url: str, data: bytes, *, headers: dict[str, str] | None = None) -> None:
        for start in range(0, len(data), SIMPLE_ATTACHMENT_BYTES):
            end = min(start + SIMPLE_ATTACHMENT_BYTES, len(data))
            status, payload = await request_json(
                "PUT", url, content=data[start:end], uncertain_effect=True,
                headers={
                    **(headers or {}),
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(end - start),
                    "Content-Range": f"bytes {start}-{end - 1}/{len(data)}",
                },
            )
            self.check_status(status, 201 if end == len(data) else 200, mutation=True)
            if end < len(data) and payload.get("nextExpectedRanges") not in ([str(end)], [f"{end}-"]):
                raise Microsoft365Error("integration.outcomeUnknown")

    async def update_draft(self, message_id: str, payload: dict) -> None:
        await self._write("PATCH", "/me/messages/" + quote(message_id, safe=""), 200, json=payload)

    async def delete_attachment(self, message_id: str, attachment_id: str) -> None:
        await self._write(
            "DELETE", "/me/messages/" + quote(message_id, safe="") + "/attachments/" + quote(attachment_id, safe=""), 204,
        )

    async def send_draft(self, message_id: str) -> None:
        await self._write("POST", "/me/messages/" + quote(message_id, safe="") + "/send", 202)

    async def delete_message(self, message_id: str) -> None:
        await self._write("DELETE", "/me/messages/" + quote(message_id, safe=""), 204)

    async def _write(self, method, path: str, expected: int, **kwargs) -> None:
        status, _payload = await request_json(
            method, GRAPH_BASE + path, headers=self.headers, uncertain_effect=True, **kwargs,
        )
        self.check_status(status, expected, mutation=True)

    async def mutate(self, method: Literal["POST", "PATCH", "DELETE"], path: str, *, payload: dict | None = None, etag: str | None = None, max_response_bytes: int = MAX_RESPONSE_BYTES) -> dict:
        status, result = await request_json(
            method, GRAPH_BASE + path, headers={**self.headers, **({"If-Match": etag} if etag else {})},
            json=payload, uncertain_effect=True, max_response_bytes=max_response_bytes,
        )
        self.check_status(status, {"POST": 201, "PATCH": 200, "DELETE": 204}[method], mutation=True)
        return result
