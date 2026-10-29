from __future__ import annotations

import asyncio
import base64
import imaplib
import logging
import re
import smtplib
import ssl
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from email import policy
from email.message import EmailMessage, Message
from email.parser import BytesParser
from email.utils import formatdate, getaddresses, make_msgid, parsedate_to_datetime
from typing import Literal

from bs4 import BeautifulSoup

_OPERATION_TIMEOUT = 30
_MAX_MAILBOXES = 200
_MAX_SEARCH_RESPONSE = 1024 * 1024
_MAX_HEADER_BYTES = 256 * 1024
MAX_MESSAGE_BYTES = 64 * 1024 * 1024
MAX_BODY_CHARS = 100_000
MAX_ATTACHMENTS = 50
logger = logging.getLogger(__name__)
_LIST_RE = re.compile(rb"^\(([^)]*)\)\s+(?:\"(?:\\.|[^\"])*\"|NIL)\s+(.+)$")
_UID_RE = re.compile(rb"\bUID\s+(\d+)\b")
_SIZE_RE = re.compile(rb"\bRFC822\.SIZE\s+(\d+)\b")
_APPEND_UID_RE = re.compile(rb"\bAPPENDUID\s+(\d+)\s+(\d+)\b")
_DRAFT_HEADERS = ("From", "To", "Cc", "Bcc", "Subject")
_UNSAFE_DRAFT_TYPES = {
    "multipart/signed",
    "multipart/encrypted",
    "application/pkcs7-mime",
    "application/x-pkcs7-mime",
}


class MailError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class Mailbox:
    name: str
    flags: frozenset[str]


@dataclass(frozen=True, slots=True)
class MessageLocator:
    mailbox: str
    uidvalidity: int
    uid: int


@dataclass(frozen=True, slots=True)
class MessageSummary:
    locator: MessageLocator
    subject: str
    sender: str
    recipients: tuple[str, ...]
    date: datetime | None
    size: int
    flags: frozenset[str]


@dataclass(frozen=True, slots=True)
class MessagePage:
    messages: tuple[MessageSummary, ...]
    uidvalidity: int
    next_uid: int | None


@dataclass(frozen=True, slots=True)
class Attachment:
    part: str
    filename: str
    content_type: str
    size: int


@dataclass(frozen=True, slots=True)
class ParsedMessage:
    summary: MessageSummary
    cc: tuple[str, ...]
    bcc: tuple[str, ...]
    body: str
    body_format: Literal["text", "html"]
    attachments: tuple[Attachment, ...]
    body_truncated: bool
    attachments_truncated: bool
    raw: bytes


def _modified_utf7_decode(value: str) -> str:
    result: list[str] = []
    position = 0
    for match in re.finditer(r"&([^-]*)-", value):
        result.append(value[position : match.start()])
        encoded = match.group(1)
        if not encoded:
            result.append("&")
        else:
            padded = encoded.replace(",", "/") + "=" * (-len(encoded) % 4)
            try:
                result.append(base64.b64decode(padded).decode("utf-16be"))
            except (ValueError, UnicodeError):
                result.append(match.group(0))
        position = match.end()
    result.append(value[position:])
    return "".join(result)


def _modified_utf7_encode(value: str) -> str:
    if not value or any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise MailError("mail.invalidMessage")
    chunks: list[str] = []
    non_ascii: list[str] = []

    def flush() -> None:
        if non_ascii:
            encoded = base64.b64encode("".join(non_ascii).encode("utf-16be"))
            chunks.append("&" + encoded.decode("ascii").rstrip("=").replace("/", ",") + "-")
            non_ascii.clear()

    for character in value:
        if 0x20 <= ord(character) <= 0x7E:
            flush()
            chunks.append("&-" if character == "&" else character)
        else:
            non_ascii.append(character)
    flush()
    return "".join(chunks)


def _imap_mailbox(value: str) -> str:
    encoded = _modified_utf7_encode(value)
    return '"' + encoded.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _unquote(value: bytes) -> str:
    if value.startswith(b'"') and value.endswith(b'"'):
        value = value[1:-1].replace(b"\\\"", b'"').replace(b"\\\\", b"\\")
    return _modified_utf7_decode(value.decode("ascii", errors="replace"))


def _header(value: object, limit: int = 2_000) -> str:
    return " ".join(str(value or "").split())[:limit]


def _addresses(
    message: Message, *headers: str, limit: int | None = 100
) -> tuple[str, ...]:
    values = [str(value) for name in headers for value in message.get_all(name, [])]
    addresses = tuple(
        address[:320]
        for _display, address in getaddresses(values)
        if address and "\r" not in address and "\n" not in address
    )
    return addresses if limit is None else addresses[:limit]


def _date(message: Message) -> datetime | None:
    try:
        return parsedate_to_datetime(str(message.get("Date")))
    except (TypeError, ValueError, OverflowError):
        return None


def _part_items(message: Message, prefix: str = ""):
    if message.is_multipart():
        for index, child in enumerate(message.iter_parts(), 1):
            path = f"{prefix}.{index}" if prefix else str(index)
            yield from _part_items(child, path)
    else:
        yield prefix or "1", message


def _payload_bytes(part: Message) -> bytes:
    try:
        payload = part.get_payload(decode=True)
    except (LookupError, TypeError, ValueError):
        return b""
    return payload if isinstance(payload, bytes) else b""


def _parse_summary(
    raw_headers: bytes,
    locator: MessageLocator,
    *,
    size: int,
    flags: frozenset[str],
) -> MessageSummary:
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw_headers, headersonly=True)
    except Exception as exc:
        raise MailError("mail.invalidResponse") from exc
    return MessageSummary(
        locator=locator,
        subject=_header(message.get("Subject"), 1_000),
        sender=_header(message.get("From"), 1_000),
        recipients=_addresses(message, "To"),
        date=_date(message),
        size=size,
        flags=flags,
    )


def parse_message(raw: bytes, locator: MessageLocator) -> ParsedMessage:
    if len(raw) > MAX_MESSAGE_BYTES:
        raise MailError("mail.responseTooLarge")
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw)
    except Exception as exc:
        raise MailError("mail.invalidResponse") from exc
    summary = MessageSummary(
        locator=locator,
        subject=_header(message.get("Subject"), 1_000),
        sender=_header(message.get("From"), 1_000),
        recipients=_addresses(message, "To"),
        date=_date(message),
        size=len(raw),
        flags=frozenset(),
    )
    plain_body = ""
    html_body = ""
    attachments: list[Attachment] = []
    attachment_count = 0
    body_truncated = False
    for path, part in _part_items(message):
        filename = _header(part.get_filename(), 500)
        disposition = part.get_content_disposition()
        if filename or disposition == "attachment":
            attachment_count += 1
            if len(attachments) < MAX_ATTACHMENTS:
                data = _payload_bytes(part)
                attachments.append(
                    Attachment(
                        part=path,
                        filename=filename or "attachment",
                        content_type=part.get_content_type()[:200],
                        size=len(data),
                    )
                )
            continue
        if part.get_content_type() in {"text/plain", "text/html"}:
            try:
                content = part.get_content()
            except (LookupError, TypeError, UnicodeError, ValueError):
                content = ""
            if isinstance(content, str):
                body_truncated = body_truncated or len(content) > MAX_BODY_CHARS
                if part.get_content_type() == "text/plain" and not plain_body:
                    plain_body = content[:MAX_BODY_CHARS]
                elif part.get_content_type() == "text/html" and not html_body:
                    html_body = content[:MAX_BODY_CHARS]
    if plain_body:
        body = plain_body
    else:
        soup = BeautifulSoup(html_body, "html.parser")
        for element in soup(["script", "style", "template"]):
            element.decompose()
        body = soup.get_text("\n", strip=True)[:MAX_BODY_CHARS]
    return ParsedMessage(
        summary=summary,
        cc=_addresses(message, "Cc"),
        bcc=_addresses(message, "Bcc"),
        body=body,
        body_format="text",
        attachments=tuple(attachments),
        body_truncated=body_truncated,
        attachments_truncated=attachment_count > MAX_ATTACHMENTS,
        raw=raw,
    )


def attachment_bytes(raw: bytes, part_path: str) -> tuple[bytes, str, str]:
    message = parse_message(raw, MessageLocator("", 1, 1))
    parsed = BytesParser(policy=policy.default).parsebytes(message.raw)
    for path, part in _part_items(parsed):
        if path == part_path and (part.get_filename() or part.get_content_disposition() == "attachment"):
            return (
                _payload_bytes(part),
                _header(part.get_filename(), 500) or "attachment",
                part.get_content_type()[:200],
            )
    raise MailError("mail.notFound")


def _draft_body_part(message: Message) -> Message:
    if any(part.get_content_type() in _UNSAFE_DRAFT_TYPES for part in message.walk()):
        raise MailError("mail.invalidMessage")
    if not message.is_multipart():
        candidates = [message]
    elif message.get_content_type() == "multipart/mixed":
        candidates = [
            part
            for part in message.iter_parts()
            if not part.get_filename() and part.get_content_disposition() != "attachment"
        ]
    else:
        raise MailError("mail.invalidMessage")
    if (
        len(candidates) != 1
        or candidates[0].is_multipart()
        or candidates[0].get_content_type() != "text/plain"
    ):
        raise MailError("mail.invalidMessage")
    part = candidates[0]
    content = part.get_content()
    if not isinstance(content, str) or len(content) > MAX_BODY_CHARS:
        raise MailError("mail.invalidMessage")
    return part


def _patch_draft(
    raw: bytes, replacement: bytes, replace_attachments: bool = False
) -> bytes:
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw)
        changes = BytesParser(policy=policy.default).parsebytes(replacement)
        body = _draft_body_part(changes).get_content()
        _draft_body_part(message)
    except MailError:
        raise
    except Exception as exc:
        raise MailError("mail.invalidMessage") from exc
    if not isinstance(body, str):
        raise MailError("mail.invalidMessage")
    if len(_addresses(changes, "To", "Cc", "Bcc", limit=None)) > 100:
        raise MailError("mail.invalidMessage")
    if sum(
        bool(part.get_filename() or part.get_content_disposition() == "attachment")
        for part in message.walk()
    ) > MAX_ATTACHMENTS:
        raise MailError("mail.invalidMessage")
    if replace_attachments:
        preserved = {
            name.casefold()
            for name in (*_DRAFT_HEADERS, "MIME-Version", "Content-Type", "Content-Transfer-Encoding")
        }
        for name in dict(message.raw_items()):
            if name.casefold() not in preserved:
                while changes.get(name) is not None:
                    del changes[name]
                for value in message.get_all(name, []):
                    changes[name] = value
        patched = changes.as_bytes(policy=policy.SMTP)
        if len(patched) > MAX_MESSAGE_BYTES:
            raise MailError("mail.responseTooLarge")
        return patched
    for name in _DRAFT_HEADERS:
        while message.get(name) is not None:
            del message[name]
        for value in changes.get_all(name, []):
            message[name] = value
    _draft_body_part(message).set_content(body)
    patched = message.as_bytes(policy=policy.SMTP)
    if len(patched) > MAX_MESSAGE_BYTES:
        raise MailError("mail.responseTooLarge")
    return patched


class MailClient:
    def __init__(
        self,
        *,
        imap_host: str,
        imap_port: int,
        imap_security: Literal["tls", "starttls"],
        smtp_host: str,
        smtp_port: int,
        smtp_security: Literal["tls", "starttls"],
        username: str,
        password: str,
        from_address: str,
    ) -> None:
        self.imap_host = imap_host
        self.imap_port = imap_port
        self.imap_security = imap_security
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.smtp_security = smtp_security
        self.username = username
        self.password = password
        self.from_address = from_address

    async def _run(self, function, *args, mutation: bool = False):
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(function, *args), timeout=_OPERATION_TIMEOUT
            )
        except MailError:
            raise
        except (TimeoutError, OSError, smtplib.SMTPServerDisconnected) as exc:
            raise MailError("mail.outcomeUnknown" if mutation else "mail.unavailable") from exc
        except (imaplib.IMAP4.error, smtplib.SMTPException) as exc:
            raise MailError("mail.outcomeUnknown" if mutation else "mail.unavailable") from exc

    @contextmanager
    def _imap(self):
        context = ssl.create_default_context()
        client: imaplib.IMAP4
        if self.imap_security == "tls":
            client = imaplib.IMAP4_SSL(
                self.imap_host,
                self.imap_port,
                ssl_context=context,
                timeout=_OPERATION_TIMEOUT,
            )
        else:
            client = imaplib.IMAP4(self.imap_host, self.imap_port, timeout=_OPERATION_TIMEOUT)
            client.starttls(ssl_context=context)
        try:
            try:
                status, _data = client.login(self.username, self.password)
            except imaplib.IMAP4.error as exc:
                raise MailError("mail.authenticationFailed") from exc
            if status != "OK":
                raise MailError("mail.authenticationFailed")
            yield client
        finally:
            try:
                client.logout()
            except (imaplib.IMAP4.error, OSError):
                pass

    @contextmanager
    def _smtp(self):
        context = ssl.create_default_context()
        client: smtplib.SMTP
        if self.smtp_security == "tls":
            client = smtplib.SMTP_SSL(
                self.smtp_host,
                self.smtp_port,
                timeout=_OPERATION_TIMEOUT,
                context=context,
            )
        else:
            client = smtplib.SMTP(
                self.smtp_host, self.smtp_port, timeout=_OPERATION_TIMEOUT
            )
            client.ehlo()
            client.starttls(context=context)
            client.ehlo()
        try:
            try:
                client.login(self.username, self.password)
            except smtplib.SMTPAuthenticationError as exc:
                raise MailError("mail.authenticationFailed") from exc
            yield client
        finally:
            try:
                client.quit()
            except (OSError, smtplib.SMTPException):
                client.close()

    @staticmethod
    def _ok(status: str, code: str = "mail.unavailable") -> None:
        if status != "OK":
            raise MailError(code)

    @staticmethod
    def _uidvalidity(client: imaplib.IMAP4) -> int:
        _status, values = client.response("UIDVALIDITY")
        try:
            value = int(values[0])
        except (IndexError, TypeError, ValueError) as exc:
            raise MailError("mail.invalidResponse") from exc
        if value <= 0:
            raise MailError("mail.invalidResponse")
        return value

    @classmethod
    def _select(cls, client: imaplib.IMAP4, mailbox: str, *, readonly: bool) -> int:
        status, _data = client.select(_imap_mailbox(mailbox), readonly=readonly)
        cls._ok(status, "mail.notFound")
        return cls._uidvalidity(client)

    @staticmethod
    def _mailboxes(client: imaplib.IMAP4) -> tuple[Mailbox, ...]:
        status, values = client.list()
        MailClient._ok(status)
        result = []
        for value in values or []:
            if not isinstance(value, bytes) or len(value) > 8_192:
                continue
            match = _LIST_RE.fullmatch(value)
            if match is None:
                continue
            flags = frozenset(
                item.decode("ascii", errors="ignore").casefold()
                for item in imaplib.ParseFlags(b"FLAGS (" + match.group(1) + b")")
            )
            result.append(Mailbox(_unquote(match.group(2)), flags))
            if len(result) >= _MAX_MAILBOXES:
                break
        return tuple(result)

    async def list_mailboxes(self) -> tuple[Mailbox, ...]:
        def operation():
            with self._imap() as client:
                return self._mailboxes(client)

        return await self._run(operation)

    @staticmethod
    def _fetch_literal(data: list[object] | None) -> tuple[bytes, bytes]:
        for item in data or []:
            if isinstance(item, tuple) and len(item) == 2:
                metadata, literal = item
                if isinstance(metadata, bytes) and isinstance(literal, bytes):
                    return metadata, literal
        raise MailError("mail.invalidResponse")

    @staticmethod
    def _fetch_metadata(data: list[object] | None) -> bytes:
        for item in data or []:
            if isinstance(item, bytes) and b"UID" in item:
                return item
            if isinstance(item, tuple) and item and isinstance(item[0], bytes):
                return item[0]
        raise MailError("mail.invalidResponse")

    def _search_sync(
        self, mailbox: str, query: str | None, limit: int,
        before_uid: int | None = None, expected_uidvalidity: int | None = None,
    ) -> MessagePage:
        _modified_utf7_encode(mailbox)
        if query and any(ord(character) < 32 or ord(character) == 127 for character in query):
            raise MailError("mail.invalidMessage")
        with self._imap() as client:
            uidvalidity = self._select(client, mailbox, readonly=True)
            if expected_uidvalidity is not None and uidvalidity != expected_uidvalidity:
                raise MailError("mail.staleReference")
            criteria = ("UID", f"1:{before_uid - 1}") if before_uid is not None else ()
            if query:
                escaped = query.replace("\\", "\\\\").replace('"', '\\"')
                status, values = client.uid(
                    "SEARCH", "CHARSET", "UTF-8", *criteria, "TEXT", f'"{escaped}"'.encode()
                )
            else:
                status, values = client.uid("SEARCH", None, *criteria, "ALL")
            self._ok(status)
            raw_uids = values[0] if values else b""
            if not isinstance(raw_uids, bytes) or len(raw_uids) > _MAX_SEARCH_RESPONSE:
                raise MailError("mail.responseTooLarge")
            try:
                uids = sorted({int(value) for value in raw_uids.split()}, reverse=True)
                if any(uid <= 0 for uid in uids):
                    raise ValueError("invalid message UID")
            except ValueError as exc:
                raise MailError("mail.invalidResponse") from exc
            summaries = []
            for uid in uids[:limit]:
                status, data = client.uid(
                    "FETCH",
                    str(uid),
                    "(UID FLAGS RFC822.SIZE BODY.PEEK[HEADER.FIELDS (DATE FROM TO SUBJECT)])",
                )
                self._ok(status)
                metadata, headers = self._fetch_literal(data)
                if len(headers) > _MAX_HEADER_BYTES:
                    raise MailError("mail.responseTooLarge")
                uid_match = _UID_RE.search(metadata)
                size_match = _SIZE_RE.search(metadata)
                if uid_match is None or size_match is None or int(uid_match.group(1)) != uid:
                    raise MailError("mail.invalidResponse")
                summaries.append(
                    _parse_summary(
                        headers,
                        MessageLocator(mailbox, uidvalidity, uid),
                        size=int(size_match.group(1)),
                        flags=frozenset(
                            flag.decode("ascii", errors="ignore")
                            for flag in imaplib.ParseFlags(metadata)
                        ),
                    )
                )
            return MessagePage(
                tuple(summaries), uidvalidity, uids[limit - 1] if len(uids) > limit else None
            )

    async def search(
        self, mailbox: str, *, query: str | None = None, limit: int = 50,
        before_uid: int | None = None, expected_uidvalidity: int | None = None,
    ) -> MessagePage:
        return await self._run(
            self._search_sync, mailbox, query, limit, before_uid, expected_uidvalidity
        )

    def _get_raw_sync(self, locator: MessageLocator) -> tuple[bytes, frozenset[str]]:
        with self._imap() as client:
            current = self._select(client, locator.mailbox, readonly=True)
            if current != locator.uidvalidity:
                raise MailError("mail.staleReference")
            status, metadata_data = client.uid(
                "FETCH", str(locator.uid), "(UID FLAGS RFC822.SIZE)"
            )
            self._ok(status, "mail.notFound")
            metadata = self._fetch_metadata(metadata_data)
            uid_match = _UID_RE.search(metadata)
            size_match = _SIZE_RE.search(metadata)
            if uid_match is None or int(uid_match.group(1)) != locator.uid:
                raise MailError("mail.notFound")
            if size_match is None:
                raise MailError("mail.invalidResponse")
            if int(size_match.group(1)) > MAX_MESSAGE_BYTES:
                raise MailError("mail.responseTooLarge")
            flags = frozenset(
                flag.decode("ascii", errors="ignore")
                for flag in imaplib.ParseFlags(metadata)
            )
            status, data = client.uid(
                "FETCH", str(locator.uid), "(UID BODY.PEEK[])"
            )
            self._ok(status, "mail.notFound")
            body_metadata, raw = self._fetch_literal(data)
            body_uid_match = _UID_RE.search(body_metadata)
            if body_uid_match is None or int(body_uid_match.group(1)) != locator.uid:
                raise MailError("mail.notFound")
            if len(raw) > MAX_MESSAGE_BYTES:
                raise MailError("mail.responseTooLarge")
            return raw, flags

    async def get_message(self, locator: MessageLocator) -> ParsedMessage:
        raw, flags = await self._run(self._get_raw_sync, locator)
        message = parse_message(raw, locator)
        return replace(message, summary=replace(message.summary, flags=flags))

    async def get_attachment(
        self, locator: MessageLocator, part: str
    ) -> tuple[bytes, str, str]:
        raw, _flags = await self._run(self._get_raw_sync, locator)
        return attachment_bytes(raw, part)

    @staticmethod
    def _special_mailbox(client: imaplib.IMAP4, flag: str) -> str | None:
        return next(
            (mailbox.name for mailbox in MailClient._mailboxes(client) if flag in mailbox.flags),
            None,
        )

    @staticmethod
    def _mailbox_has_flag(client: imaplib.IMAP4, mailbox: str, flag: str) -> bool:
        return any(
            item.name == mailbox and flag in item.flags
            for item in MailClient._mailboxes(client)
        )

    @classmethod
    def _safe_delete(
        cls, client: imaplib.IMAP4, locator: MessageLocator, *, trash: str | None
    ) -> None:
        capabilities = {
            value.decode("ascii", errors="ignore").upper()
            for value in client.capabilities
            if isinstance(value, bytes)
        }
        if "MOVE" in capabilities and trash and trash != locator.mailbox:
            status, _data = client.uid(
                "MOVE", str(locator.uid), _imap_mailbox(trash)
            )
            cls._ok(status, "mail.notFound")
            return
        if "UIDPLUS" not in capabilities:
            raise MailError("mail.operationUnsupported")
        status, _data = client.uid(
            "STORE", str(locator.uid), "+FLAGS.SILENT", "(\\Deleted)"
        )
        cls._ok(status, "mail.notFound")
        status, _data = client.uid("EXPUNGE", str(locator.uid))
        cls._ok(status)

    def _delete_sync(self, locator: MessageLocator) -> None:
        with self._imap() as client:
            trash = self._special_mailbox(client, "\\trash")
            current = self._select(client, locator.mailbox, readonly=False)
            if current != locator.uidvalidity:
                raise MailError("mail.staleReference")
            self._safe_delete(client, locator, trash=trash)

    async def delete_message(self, locator: MessageLocator) -> None:
        await self._run(self._delete_sync, locator, mutation=True)

    @classmethod
    def _append(cls, client: imaplib.IMAP4, mailbox: str, raw: bytes) -> MessageLocator:
        status, data = client.append(
            _imap_mailbox(mailbox), "(\\Draft)", None, raw
        )
        cls._ok(status)
        combined = b" ".join(value for value in data or [] if isinstance(value, bytes))
        match = _APPEND_UID_RE.search(combined)
        if match is None:
            _status, response = client.response("APPENDUID")
            combined = b" ".join(
                value for value in response or [] if isinstance(value, bytes)
            )
            match = _APPEND_UID_RE.search(b"APPENDUID " + combined)
        if match is None:
            raise MailError("mail.outcomeUnknown")
        return MessageLocator(mailbox, int(match.group(1)), int(match.group(2)))

    def _create_draft_sync(self, mailbox: str | None, raw: bytes) -> MessageLocator:
        with self._imap() as client:
            target = mailbox or self._special_mailbox(client, "\\drafts")
            if target is None or not self._mailbox_has_flag(client, target, "\\drafts"):
                raise MailError("mail.operationUnsupported")
            return self._append(client, target, raw)

    async def create_draft(self, raw: bytes, mailbox: str | None = None) -> MessageLocator:
        if len(raw) > MAX_MESSAGE_BYTES:
            raise MailError("mail.responseTooLarge")
        return await self._run(self._create_draft_sync, mailbox, raw, mutation=True)

    def _update_draft_sync(
        self, locator: MessageLocator, raw: bytes, replace_attachments: bool
    ) -> MessageLocator:
        with self._imap() as client:
            if not self._mailbox_has_flag(client, locator.mailbox, "\\drafts"):
                raise MailError("mail.permissionDenied")
            current = self._select(client, locator.mailbox, readonly=False)
            if current != locator.uidvalidity:
                raise MailError("mail.staleReference")
            capabilities = {
                value.decode("ascii", errors="ignore").upper()
                for value in client.capabilities
                if isinstance(value, bytes)
            }
            if "UIDPLUS" not in capabilities:
                raise MailError("mail.operationUnsupported")
            status, data = client.uid(
                "FETCH", str(locator.uid), "(UID RFC822.SIZE BODY.PEEK[])"
            )
            self._ok(status, "mail.notFound")
            metadata, existing = self._fetch_literal(data)
            uid_match = _UID_RE.search(metadata)
            size_match = _SIZE_RE.search(metadata)
            if uid_match is None or int(uid_match.group(1)) != locator.uid:
                raise MailError("mail.notFound")
            if size_match is None:
                raise MailError("mail.invalidResponse")
            if int(size_match.group(1)) > MAX_MESSAGE_BYTES or len(existing) > MAX_MESSAGE_BYTES:
                raise MailError("mail.responseTooLarge")
            replacement = self._append(
                client,
                locator.mailbox,
                _patch_draft(existing, raw, replace_attachments),
            )
            self._safe_delete(client, locator, trash=None)
            return replacement

    async def update_draft(
        self,
        locator: MessageLocator,
        raw: bytes,
        *,
        replace_attachments: bool = False,
    ) -> MessageLocator:
        if len(raw) > MAX_MESSAGE_BYTES:
            raise MailError("mail.responseTooLarge")
        return await self._run(
            self._update_draft_sync,
            locator,
            raw,
            replace_attachments,
            mutation=True,
        )

    def _delete_draft_sync(self, locator: MessageLocator) -> None:
        with self._imap() as client:
            if not self._mailbox_has_flag(client, locator.mailbox, "\\drafts"):
                raise MailError("mail.permissionDenied")
            current = self._select(client, locator.mailbox, readonly=False)
            if current != locator.uidvalidity:
                raise MailError("mail.staleReference")
            self._safe_delete(client, locator, trash=None)

    async def delete_draft(self, locator: MessageLocator) -> None:
        await self._run(self._delete_draft_sync, locator, mutation=True)

    def _send_sync(self, raw: bytes) -> None:
        if len(raw) > MAX_MESSAGE_BYTES:
            raise MailError("mail.responseTooLarge")
        try:
            message = BytesParser(policy=policy.default).parsebytes(raw)
        except Exception as exc:
            raise MailError("mail.invalidMessage") from exc
        while message.get("From") is not None:
            del message["From"]
        message["From"] = self.from_address
        recipients = _addresses(message, "To", "Cc", "Bcc", limit=None)
        if not recipients or len(recipients) > 100:
            raise MailError("mail.invalidMessage")
        with self._smtp() as client:
            refused = client.send_message(
                message, from_addr=self.from_address, to_addrs=list(recipients)
            )
            if refused:
                raise MailError("mail.outcomeUnknown")

    def _archive_sent_sync(self, raw: bytes) -> bool:
        with self._imap() as client:
            mailbox = self._special_mailbox(client, "\\sent")
            if mailbox is None:
                return False
            status, _data = client.append(_imap_mailbox(mailbox), None, None, raw)
            self._ok(status)
            return True

    async def send(self, raw: bytes) -> bool:
        await self._run(self._send_sync, raw, mutation=True)
        try:
            return await self._run(self._archive_sent_sync, raw, mutation=True)
        except MailError as exc:
            # SMTP already accepted the message; a missing Sent copy must not invite a retry.
            logger.warning("Sent copy could not be confirmed (%s)", exc.code)
            return False

    async def send_draft(self, locator: MessageLocator) -> None:
        def get_draft() -> bytes:
            with self._imap() as client:
                if not self._mailbox_has_flag(client, locator.mailbox, "\\drafts"):
                    raise MailError("mail.permissionDenied")
            return self._get_raw_sync(locator)[0]

        raw = await self._run(get_draft)
        if await self.send(raw):
            try:
                await self.delete_draft(locator)
            except MailError:
                # The SMTP result is definitive; leaving a sent draft is safer than suggesting a retry.
                pass

    async def test_connection(self) -> None:
        await self.list_mailboxes()

        def smtp_login() -> None:
            with self._smtp():
                pass

        await self._run(smtp_login)


def build_message(
    *,
    from_address: str,
    to: tuple[str, ...],
    cc: tuple[str, ...] = (),
    bcc: tuple[str, ...] = (),
    subject: str,
    body: str,
    attachments: tuple[tuple[str, str, bytes], ...] = (),
    message_id: str | None = None,
) -> bytes:
    message = EmailMessage(policy=policy.SMTP)
    message["From"] = from_address
    message["To"] = ", ".join(to)
    if cc:
        message["Cc"] = ", ".join(cc)
    if bcc:
        message["Bcc"] = ", ".join(bcc)
    if len(_addresses(message, "To", "Cc", "Bcc", limit=None)) > 100:
        raise MailError("mail.invalidMessage")
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = message_id or make_msgid(
        domain=from_address.rpartition("@")[2]
    )
    message["Subject"] = subject
    message.set_content(body)
    for filename, content_type, data in attachments:
        maintype, separator, subtype = content_type.partition("/")
        if not separator:
            maintype, subtype = "application", "octet-stream"
        message.add_attachment(
            data,
            maintype=maintype or "application",
            subtype=subtype or "octet-stream",
            filename=filename,
        )
    raw = message.as_bytes()
    if len(raw) > MAX_MESSAGE_BYTES:
        raise MailError("mail.responseTooLarge")
    return raw
