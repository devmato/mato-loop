import importlib
from contextlib import contextmanager
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser

import pytest

# Initialize the agent package first; integrations and agent display metadata import each other.
importlib.import_module("mato_loop_backend.agent")
mail_client = importlib.import_module("mato_loop_backend.integrations.mail.client")
MailClient = mail_client.MailClient
MailError = mail_client.MailError
_imap_mailbox = mail_client._imap_mailbox
_patch_draft = mail_client._patch_draft
build_message = mail_client.build_message


def _client() -> MailClient:
    return MailClient(
        imap_host="imap.example.com",
        imap_port=993,
        imap_security="tls",
        smtp_host="smtp.example.com",
        smtp_port=465,
        smtp_security="tls",
        username="user@example.com",
        password="secret",
        from_address="user@example.com",
    )


def test_mailbox_arguments_are_quoted_and_search_uses_charset_keyword(monkeypatch) -> None:
    calls = []

    class FakeImap:
        def select(self, mailbox, readonly=False):
            calls.append(("SELECT", mailbox, readonly))
            return "OK", [b"0"]

        def response(self, name):
            assert name == "UIDVALIDITY"
            return "OK", [b"1"]

        def uid(self, *arguments):
            calls.append(arguments)
            return "OK", [b""]

    client = _client()

    @contextmanager
    def imap():
        yield FakeImap()

    monkeypatch.setattr(client, "_imap", imap)
    assert client._search_sync('Projekte "Q"\\Über', 'Text "x"\\y', 10).messages == ()
    assert calls == [
        ("SELECT", '"Projekte \\"Q\\"\\\\&ANw-ber"', True),
        ("SEARCH", "CHARSET", "UTF-8", "TEXT", b'"Text \\"x\\"\\\\y"'),
    ]
    assert _imap_mailbox("INBOX") == '"INBOX"'


def test_total_recipient_limit_rejects_instead_of_truncating(monkeypatch) -> None:
    recipients = tuple(f"person{index}@example.com" for index in range(101))
    with pytest.raises(MailError, match="mail.invalidMessage"):
        build_message(
            from_address="user@example.com",
            to=recipients,
            subject="Too many",
            body="Body",
        )

    message = EmailMessage(policy=policy.SMTP)
    message["From"] = "user@example.com"
    message["To"] = ", ".join(recipients)
    message.set_content("Body")
    client = _client()

    @contextmanager
    def smtp():
        raise AssertionError("invalid recipients must be rejected before SMTP")
        yield

    monkeypatch.setattr(client, "_smtp", smtp)
    with pytest.raises(MailError, match="mail.invalidMessage"):
        client._send_sync(message.as_bytes())


def test_draft_patch_preserves_attachments_and_unrelated_headers() -> None:
    original = EmailMessage(policy=policy.SMTP)
    original["From"] = "old@example.com"
    original["To"] = "old-recipient@example.com"
    original["Cc"] = "old-copy@example.com"
    original["Subject"] = "Old subject"
    original["Message-ID"] = "<stable@example.com>"
    original["X-Keep"] = "untouched"
    original.set_content("Old body")
    original.add_attachment(
        b"attachment bytes",
        maintype="application",
        subtype="octet-stream",
        filename="report.bin",
    )
    replacement = build_message(
        from_address="new@example.com",
        to=("new-recipient@example.com",),
        bcc=("hidden@example.com",),
        subject="New subject",
        body="New body",
    )

    patched = BytesParser(policy=policy.default).parsebytes(
        _patch_draft(original.as_bytes(), replacement)
    )

    assert str(patched["From"]) == "new@example.com"
    assert str(patched["To"]) == "new-recipient@example.com"
    assert patched["Cc"] is None
    assert str(patched["Bcc"]) == "hidden@example.com"
    assert str(patched["Subject"]) == "New subject"
    assert str(patched["Message-ID"]) == "<stable@example.com>"
    assert str(patched["X-Keep"]) == "untouched"
    parts = list(patched.iter_parts())
    assert parts[0].get_content().strip() == "New body"
    assert parts[1].get_filename() == "report.bin"
    assert parts[1].get_payload(decode=True) == b"attachment bytes"


@pytest.mark.parametrize(
    "content_type",
    ["multipart/alternative", "multipart/signed", "multipart/encrypted"],
)
def test_draft_patch_rejects_ambiguous_or_protected_mime(content_type) -> None:
    original = EmailMessage(policy=policy.SMTP)
    original["From"] = "user@example.com"
    original["To"] = "other@example.com"
    original.set_type(content_type)
    replacement = build_message(
        from_address="user@example.com",
        to=("other@example.com",),
        subject="Subject",
        body="Body",
    )

    with pytest.raises(MailError, match="mail.invalidMessage"):
        _patch_draft(original.as_bytes(), replacement)
