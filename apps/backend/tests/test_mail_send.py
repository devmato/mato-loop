from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

# Initialize the agent package before the integrations package's public imports.
import mato_loop_backend.integrations.mail.send as mail_send
from mato_loop_backend.integrations.mail.client import MailError


class _Session:
    def __init__(self, values=()):
        self.values = iter(values)
        self.execute = AsyncMock()
        self.commit = AsyncMock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def scalar(self, _statement):
        return next(self.values)


@pytest.mark.parametrize("confirmed", [True, False, None])
async def test_replayed_completed_send_does_not_use_smtp(monkeypatch, confirmed) -> None:
    operation_id = uuid4()
    existing = SimpleNamespace(
        request_hash=mail_send._message_digest(b"raw"), status="sent",
        sent_copy_confirmed=None,
    )
    saved = _Session()
    sessions = iter([_Session([operation_id]), saved, _Session([None, existing])])
    monkeypatch.setattr(mail_send, "AsyncSessionFactory", lambda: next(sessions))
    client = SimpleNamespace(send=AsyncMock(return_value=confirmed))
    arguments = {
        "user_id": uuid4(),
        "integration_id": uuid4(),
        "idempotency_key": "stable-key",
        "raw": b"raw",
        "client": client,
    }

    assert (await mail_send.send_once(**arguments)).sent_copy_confirmed is confirmed
    stored = saved.execute.await_args.args[0].compile().params
    assert stored["sent_copy_confirmed"] is confirmed
    existing.sent_copy_confirmed = stored["sent_copy_confirmed"]
    assert (await mail_send.send_once(**arguments)).sent_copy_confirmed is confirmed
    client.send.assert_awaited_once_with(b"raw")


async def test_unknown_send_is_persisted_and_never_retried(monkeypatch) -> None:
    operation_id = uuid4()
    existing = SimpleNamespace(
        request_hash=mail_send._message_digest(b"raw"), status="outcome_unknown"
    )
    sessions = iter([_Session([operation_id]), _Session(), _Session([None, existing])])
    monkeypatch.setattr(mail_send, "AsyncSessionFactory", lambda: next(sessions))
    client = SimpleNamespace(send=AsyncMock(side_effect=MailError("mail.outcomeUnknown")))
    arguments = {
        "user_id": uuid4(),
        "integration_id": uuid4(),
        "idempotency_key": "stable-key",
        "raw": b"raw",
        "client": client,
    }

    with pytest.raises(MailError, match="mail.outcomeUnknown"):
        await mail_send.send_once(**arguments)
    with pytest.raises(MailError, match="mail.outcomeUnknown"):
        await mail_send.send_once(**arguments)
    client.send.assert_awaited_once_with(b"raw")


def test_message_digest_ignores_generated_mime_boundaries_and_date() -> None:
    from mato_loop_backend.integrations.mail.client import build_message

    arguments = {
        "from_address": "sender@example.com",
        "to": ("recipient@example.com",),
        "subject": "Report",
        "body": "Hello",
        "attachments": (("report.txt", "text/plain", b"data"),),
    }

    first = build_message(**arguments)
    second = build_message(**arguments)

    assert first != second
    assert mail_send._message_digest(first) == mail_send._message_digest(second)
    assert mail_send._message_digest(first) != mail_send._message_digest(
        build_message(**(arguments | {"body": "Changed"}))
    )
