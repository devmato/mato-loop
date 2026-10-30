import hashlib
import json
import traceback
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend.api.request_body_limit import _request_limit
from mato_loop_backend.api.routes import mail as mail_routes
from mato_loop_backend.error_contract import error_payload
from mato_loop_backend.integrations import registry as integration_registry
from mato_loop_backend.integrations.mail import adapter as mail_adapter
from mato_loop_backend.integrations.mail.adapter import (
    MAIL_ADAPTER,
    MAIL_CREDENTIAL_KIND,
    MailConfig,
    MailCreateDraftArguments,
    MailIntegrationArguments,
    MailSearchArguments,
    MailSendMessageArguments,
    MailTokenArguments,
    _permission,
    binding_revision,
    locator_from_token,
    search_messages,
    token_for,
)
from mato_loop_backend.integrations.mail.client import (
    MailClient,
    MailError,
    MessageLocator,
    MessagePage,
    build_message,
    parse_message,
)
from mato_loop_backend.integrations.mail.send import MailSendResult
from mato_loop_backend.integrations.mail.service import MailIntegrationService
from mato_loop_backend.models import ChatSession
from mato_loop_backend.permissions.policy import PermissionDecision
from mato_loop_backend.providers.base import ToolCall
from mato_loop_backend.security import CredentialCipher
from mato_loop_backend.settings import (
    InstanceLimits,
    IntegrationInstance,
    IntegrationSettings,
    SettingsStore,
)
from mato_loop_backend.tools.catalog import ToolRegistry
from mato_loop_backend.tools.context import ToolContext


def _account() -> IntegrationInstance:
    return IntegrationInstance(
        name="Mail",
        integration_type="mail",
        adapter_type="imap_smtp",
        config={
            "imap_host": "imap.example.com",
            "imap_port": 993,
            "imap_security": "tls",
            "smtp_host": "smtp.example.com",
            "smtp_port": 587,
            "smtp_security": "starttls",
            "username": "user@example.com",
            "from_address": "user@example.com",
        },
    )


def _client() -> MailClient:
    return MailClient(
        **MailConfig.model_validate(_account().config).model_dump(), password="secret"
    )


@pytest.fixture(autouse=True)
def mail_cipher(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "mato_loop_backend.auth.credentials.get_runtime_config",
        lambda: SimpleNamespace(secrets_dir=tmp_path),
    )


@pytest.fixture
def mail_binding():
    account = _account()
    return account, SimpleNamespace(
        bound_options={
            "integration_id": str(account.id),
            "binding_revision": binding_revision(account),
        },
        integration_settings=IntegrationSettings(instances=[account]),
    )


@pytest.mark.parametrize("part", [None, "2.1"])
async def test_mail_read_scopes_use_stable_private_locator_identity(mail_binding, part) -> None:
    class AttachmentArguments(MailIntegrationArguments):
        attachment_token: str
        limit: int = 50

    account, context = mail_binding
    revision = binding_revision(account)
    locator = MessageLocator("Confidential/Payroll", 172839, 987654)
    field = "attachment_token" if part else "message_token"
    model = AttachmentArguments if part else MailTokenArguments
    operation = "mail_get_attachment" if part else "mail_get_message"

    async def permission(token, **changes):
        arguments = model(integration_id=account.id, **{field: token}, **changes)
        before = arguments.model_dump()
        request = (await _permission("mail.read", operation, arguments, context))[0]
        assert arguments.model_dump() == before
        return request

    first = token_for(account.id, revision, locator, part=part)
    second = token_for(account.id, revision, locator, part=part)
    assert first != second
    request = await permission(first)
    assert request == await permission(second)
    assert request.available_decisions == ("allow_once", "allow_session", "deny")
    assert request.use_integration_policy
    assert request.policy_integration_id == account.id
    scope = json.loads(request.resource_scope)
    assert scope["integrationId"] == str(account.id)
    assert scope["bindingRevision"] == hashlib.sha256(revision.encode()).hexdigest()
    assert scope["operationKey"].startswith(f"{operation}:")
    assert len(scope["operationKey"].split(":")[1]) == 24
    for private in (locator.mailbox, str(locator.uidvalidity), str(locator.uid), first, second, revision):
        assert private not in repr(request)
    if part:
        assert part not in repr(request)
        assert (await permission(first, limit=10)).resource_scope != request.resource_scope

    variants = [
        (MessageLocator("Confidential/Other", locator.uidvalidity, locator.uid), part),
        (MessageLocator(locator.mailbox, locator.uidvalidity + 1, locator.uid), part),
        (MessageLocator(locator.mailbox, locator.uidvalidity, locator.uid + 1), part),
    ]
    if part:
        variants.append((locator, "2.2"))
    scopes = {request.resource_scope}
    for other_locator, other_part in variants:
        token = token_for(account.id, revision, other_locator, part=other_part)
        scopes.add((await permission(token)).resource_scope)
    assert len(scopes) == len(variants) + 1
    arguments = model(integration_id=account.id, **{field: first})
    assert (await _permission("mail.read", "other_read", arguments, context))[0].resource_scope not in scopes


@pytest.mark.parametrize("field", ["message_token", "attachment_token"])
@pytest.mark.parametrize("invalid", ["malformed", "payload", "integration", "revision", "part"])
async def test_mail_read_rejects_invalid_tokens_before_permission_request(
    mail_binding, monkeypatch, field, invalid
) -> None:
    class AttachmentArguments(MailIntegrationArguments):
        attachment_token: str

    account, context = mail_binding
    revision = binding_revision(account)
    locator = MessageLocator("Confidential/Payroll", 172839, 987654)
    part = "2.1" if field == "attachment_token" else None
    if invalid == "part":
        part = None if part else "2.1"
    token = token_for(
        uuid4() if invalid == "integration" else account.id,
        "old binding" if invalid == "revision" else revision,
        locator,
        part=part,
    )
    if invalid == "malformed":
        token = "invalid ciphertext"
    elif invalid == "payload":
        payload = CredentialCipher().decrypt(token.encode("ascii"))
        payload["uid"] = "sensitive invalid uid"
        token = CredentialCipher().encrypt(payload).decode("ascii")
    permission = Mock(side_effect=AssertionError("invalid tokens must not request approval"))
    monkeypatch.setattr(
        "mato_loop_backend.permissions.policy.integration_operation_permission", permission
    )
    model = AttachmentArguments if field == "attachment_token" else MailTokenArguments
    arguments = model(integration_id=account.id, **{field: token})
    with pytest.raises(MailError) as error:
        await _permission("mail.read", "mail_get_message", arguments, context)
    assert error.value.code == "integration.staleReference"
    permission.assert_not_called()
    formatted = "".join(traceback.format_exception(error.value))
    for private in (locator.mailbox, revision, token, "sensitive invalid uid"):
        assert private not in formatted


@pytest.mark.parametrize("invalid", ["malformed", "integration", "revision"])
async def test_mail_registry_rejects_invalid_tokens_without_approval_or_execution(
    mail_binding, monkeypatch, caplog, invalid
) -> None:
    account, binding = mail_binding
    revision = binding_revision(account)
    locator = MessageLocator("Confidential/Payroll", 172839, 987654)
    token = token_for(
        uuid4() if invalid == "integration" else account.id,
        "old binding" if invalid == "revision" else revision,
        locator,
    )
    if invalid == "malformed":
        token = "invalid ciphertext"
    handler = AsyncMock(side_effect=AssertionError("invalid tokens must not execute"))
    contribution = next(
        tool for tool in MAIL_ADAPTER.shared_tools if tool.name == "mail_get_message"
    )
    monkeypatch.setitem(
        integration_registry._ADAPTERS,
        ("mail", "imap_smtp"),
        replace(MAIL_ADAPTER, shared_tools=(replace(contribution, handler=handler),)),
    )
    monkeypatch.setattr(
        ToolContext, "current_integrations",
        AsyncMock(return_value=binding.integration_settings),
    )
    call = ToolCall(
        "mail-read", "mail_get_message",
        {"integration_id": str(account.id), "message_token": token},
    )
    context = ToolContext(
        db=AsyncMock(spec=AsyncSession),
        run_id=uuid4(),
        session=ChatSession(id=uuid4(), user_id=uuid4()),
        tool_call=call,
        root_files=Mock(),
        integration_settings=binding.integration_settings,
        ask_handler=AsyncMock(side_effect=AssertionError("invalid tokens must not ask")),
    )
    monkeypatch.setattr("mato_loop_backend.tools.access.profile_tool_settings", AsyncMock(return_value=context.tool_settings))
    effective = ToolRegistry([]).effective(binding.integration_settings)

    result = await effective.execute(call, context)

    context.ask_handler.assert_not_called()
    handler.assert_not_called()
    assert result.is_error
    assert result.metadata == {"errorCode": "integration.staleReference"}
    assert json.loads(result.content) == error_payload("integration.staleReference")
    assert not caplog.records
    for private in (locator.mailbox, revision, token):
        assert private not in repr(result)


async def test_mail_search_scopes_preserve_query_arguments(mail_binding) -> None:
    account, context = mail_binding
    arguments = MailSearchArguments(
        integration_id=account.id, mailbox="Confidential/Payroll", query="salary", limit=50
    )
    scopes = set()
    for changes in ({}, {"mailbox": "Other"}, {"query": "bonus"}, {"limit": 10}):
        selected = arguments.model_copy(update=changes)
        request = (await _permission("mail.read", "mail_search", selected, context))[0]
        digest = hashlib.sha256(
            selected.model_dump_json(exclude={"integration_id"}).encode()
        ).hexdigest()[:24]
        assert json.loads(request.resource_scope)["operationKey"] == f"mail_search:{digest}"
        assert selected.mailbox not in repr(request)
        assert selected.query not in repr(request)
        scopes.add(request.resource_scope)
    assert len(scopes) == 4


async def test_mail_draft_updates_offer_account_scoped_write_grants(mail_binding) -> None:
    account, context = mail_binding
    operation = "mail_update_draft"
    tool = next(tool for tool in MAIL_ADAPTER.shared_tools if tool.name == operation)
    scopes = set()
    for _ in range(2):
        token = token_for(account.id, binding_revision(account), MessageLocator("INBOX", 17, 42))
        arguments = tool.arguments_model(integration_id=account.id, message_token=token)
        request = (await tool.authorization_factory(arguments, context))[0]
        assert json.loads(request.resource_scope)["operationKey"].startswith(
            f"{operation}:"
        )
        assert request.available_decisions == ("allow_once", "allow_session", "deny")
        assert request.effective_session_grants[0].access == "write"
        assert request.effective_session_grants[0].resource_type == "integration_resource"
        scopes.add(request.resource_scope)
    assert len(scopes) == 1


async def test_mail_config_and_adapter_allow_only_encrypted_transports_and_account_policies() -> None:
    with pytest.raises(ValidationError):
        MailConfig.model_validate(_account().config | {"imap_security": "plain"})
    with pytest.raises(ValidationError):
        MailConfig.model_validate(_account().config | {"smtp_security": "plain"})

    actions = {action.capability: action for action in MAIL_ADAPTER.resource_actions}
    assert MAIL_ADAPTER.resource_loader is None
    assert set(actions) == {"mail.read", "mail.drafts", "mail.send", "mail.delete"}
    assert {key: action.category for key, action in actions.items()} == {
        "mail.read": "read", "mail.drafts": "write", "mail.send": "write", "mail.delete": "delete",
    }
    read_tools = {
        "mail_list_accounts",
        "mail_list_folders",
        "mail_search",
        "mail_get_message",
    }
    assert all(
        tool.releases_private_data == (tool.name in read_tools)
        for tool in MAIL_ADAPTER.shared_tools
    )
    send_message = next(tool for tool in MAIL_ADAPTER.shared_tools if tool.name == "mail_send_message")
    assert send_message.actions[0].capability == "mail.send"
    account = _account()
    context = SimpleNamespace(
        bound_options={
            "integration_id": str(account.id),
            "binding_revision": binding_revision(account),
        },
        integration_settings=IntegrationSettings(instances=[account]),
    )
    request = (await send_message.authorization_factory(
        MailSendMessageArguments(
            integration_id=account.id,
            to=("other@example.com",),
            bcc=("hidden@example.com",),
            subject="Private subject",
            body="Reviewed body",
        ),
        context,
    ))[0]
    assert request.available_decisions == ("allow_once", "allow_session", "deny")
    assert not request.sensitive_access
    assert [(item.label, item.value) for item in request.display_items] == [
        ("An", ("other@example.com",)),
        ("Bcc", ("hidden@example.com",)),
        ("Betreff", "Private subject"),
        ("Nachricht", "Reviewed body"),
    ]


async def test_disabled_mail_mutation_does_not_read_remote_message(
    mail_binding, monkeypatch
) -> None:
    account, context = mail_binding
    context.ask_handler = AsyncMock(return_value=PermissionDecision.DENY)
    selected_client = AsyncMock(
        side_effect=AssertionError("disabled mail actions must not access IMAP")
    )
    monkeypatch.setattr(
        "mato_loop_backend.integrations.mail.adapter._selected_client", selected_client
    )
    arguments = MailTokenArguments(
        integration_id=account.id,
        message_token=token_for(
            account.id,
            binding_revision(account),
            MessageLocator("Drafts", 17, 42),
        ),
    )

    request = (await _permission(
        "mail.send", "mail_send_draft", arguments, context
    ))[0]

    assert request.display_items == ()
    context.ask_handler.assert_awaited_once_with(request, check_only=True)
    selected_client.assert_not_awaited()


async def test_mail_send_rejects_incomplete_approval_preview(
    mail_binding, monkeypatch
) -> None:
    account, context = mail_binding
    context.ask_handler = AsyncMock(return_value=PermissionDecision.ASK)
    locator = MessageLocator("Drafts", 17, 42)
    message = parse_message(
        build_message(
            from_address="user@example.com",
            to=("recipient@example.com",),
            subject="Large draft",
            body="x" * 100_001,
        ),
        locator,
    )
    client = SimpleNamespace(get_message=AsyncMock(return_value=message))
    monkeypatch.setattr(
        "mato_loop_backend.integrations.mail.adapter._selected_client",
        AsyncMock(return_value=(client, account, binding_revision(account))),
    )
    arguments = MailTokenArguments(
        integration_id=account.id,
        message_token=token_for(account.id, binding_revision(account), locator),
    )

    with pytest.raises(MailError) as error:
        await _permission("mail.send", "mail_send_draft", arguments, context)

    assert error.value.code == "mail.responseTooLarge"


def test_mail_drafts_allow_missing_recipients_but_sending_does_not() -> None:
    integration_id = uuid4()
    assert MailCreateDraftArguments(integration_id=integration_id).to == ()
    with pytest.raises(ValidationError):
        MailSendMessageArguments(integration_id=integration_id)


def test_html_only_mail_is_reduced_to_safe_text() -> None:
    message = parse_message(
        b"Content-Type: text/html\r\n\r\n<h1>Hello</h1><script>bad()</script><p>World</p>",
        MessageLocator("INBOX", 1, 1),
    )
    assert message.body_format == "text"
    assert "Hello" in message.body
    assert "World" in message.body
    assert "bad" not in message.body
    assert "<h1>" not in message.body


async def test_mail_service_stores_only_encrypted_password_credential(monkeypatch) -> None:
    settings = IntegrationSettings()
    credential = SimpleNamespace(id=uuid4())
    store_credential = AsyncMock(return_value=credential)
    monkeypatch.setattr(SettingsStore, "load", AsyncMock(return_value=settings))
    monkeypatch.setattr(SettingsStore, "save", AsyncMock())
    monkeypatch.setattr(
        "mato_loop_backend.integrations.mail.service.store_credential", store_credential
    )
    db = AsyncMock(spec=AsyncSession)

    result = await MailIntegrationService.create(
        db,
        name="Mail",
        config=dict(_account().config),
        password="secret",
        enabled=True,
    )

    assert len(result.instances) == 1
    assert "password" not in result.instances[0].config
    assert store_credential.await_args.kwargs == {
        "owner_id": None,
        "kind": MAIL_CREDENTIAL_KIND,
        "label": "Mail",
        "payload": {"password": "secret"},
    }
    db.commit.assert_awaited_once()


def test_message_and_attachment_tokens_are_opaque_and_revision_bound() -> None:
    raw = build_message(
        from_address="user@example.com",
        to=("other@example.com",),
        subject="Bericht",
        body="Vertraulicher Inhalt",
        attachments=(("report.bin", "application/octet-stream", b"\x00\x01"),),
    )
    locator = MessageLocator("INBOX", 17, 42)
    message = parse_message(raw, locator)
    account = _account()
    revision = binding_revision(account)

    assert message.body.startswith("Vertraulicher Inhalt")
    assert message.attachments[0].filename == "report.bin"
    token = token_for(account.id, revision, locator, part=message.attachments[0].part)
    assert "INBOX" not in token
    decoded = CredentialCipher().decrypt(token.encode("ascii"))
    assert decoded["mailbox"] == "INBOX"
    assert decoded["uidvalidity"] == 17
    assert decoded["uid"] == 42

    resolved, part = locator_from_token(
        token, account.id, revision, require_part=True
    )
    assert resolved == locator
    assert part == message.attachments[0].part
    changed = account.model_copy(deep=True)
    changed.config["imap_host"] = "new.example.com"
    with pytest.raises(ValueError, match="binding mismatch"):
        locator_from_token(
            token, account.id, binding_revision(changed), require_part=True
        )


async def test_imap_delete_uses_uid_expunge_and_never_global_expunge(monkeypatch) -> None:
    calls = []

    class FakeImap:
        capabilities = (b"IMAP4rev1", b"UIDPLUS")

        def list(self):
            return "OK", [b'(\\HasNoChildren) "/" "INBOX"']

        def select(self, mailbox, readonly=False):
            calls.append(("SELECT", mailbox, readonly))
            return "OK", [b"1"]

        def response(self, name):
            assert name == "UIDVALIDITY"
            return "OK", [b"17"]

        def uid(self, *arguments):
            calls.append(arguments)
            return "OK", [b""]

        def expunge(self):
            raise AssertionError("global EXPUNGE must not be called")

    client = _client()

    @contextmanager
    def imap():
        yield FakeImap()

    monkeypatch.setattr(client, "_imap", imap)
    await client.delete_message(MessageLocator("INBOX", 17, 42))

    assert ("STORE", "42", "+FLAGS.SILENT", "(\\Deleted)") in calls
    assert ("EXPUNGE", "42") in calls


def test_imap_folder_roles_are_preserved() -> None:
    client = SimpleNamespace(list=lambda: ("OK", [
        b'(\\Sent) "/" "Sent"', b'(\\Drafts) "/" "Drafts"', b'(\\Trash) "/" "Trash"',
    ]))
    assert [mailbox.flags for mailbox in MailClient._mailboxes(client)] == [
        frozenset({"\\sent"}), frozenset({"\\drafts"}), frozenset({"\\trash"}),
    ]


@pytest.mark.parametrize("outcome", ["stored", "missing_folder", "append_failed"])
async def test_smtp_success_reports_sent_copy_separately(monkeypatch, outcome) -> None:
    client = _client()
    smtp = SimpleNamespace(send_message=Mock(return_value={}))
    imap_client = SimpleNamespace(
        list=lambda: ("OK", [] if outcome == "missing_folder" else [b'(\\Sent) "/" "Sent"']),
        append=lambda *_args: ("NO" if outcome == "append_failed" else "OK", []),
    )

    @contextmanager
    def smtp_connection():
        yield smtp

    @contextmanager
    def imap_connection():
        yield imap_client

    monkeypatch.setattr(client, "_smtp", smtp_connection)
    monkeypatch.setattr(client, "_imap", imap_connection)
    raw = build_message(from_address="user@example.com", to=("other@example.com",), subject="Test", body="Body")
    assert await client.send(raw) is (outcome == "stored")
    smtp.send_message.assert_called_once()


async def test_imap_search_pages_use_exclusive_uid_boundary(monkeypatch) -> None:
    class FakeImap:
        def select(self, _mailbox, readonly=False):
            return "OK", [b"3"]

        def response(self, _name):
            return "OK", [b"17"]

        def uid(self, command, *args):
            if command == "SEARCH":
                return "OK", [b"1" if "1:1" in args else b"1 2 3"]
            return "OK", [(f"1 (UID {args[0]} FLAGS () RFC822.SIZE 0)".encode(), b"")]

    client = _client()

    @contextmanager
    def imap():
        yield FakeImap()

    monkeypatch.setattr(client, "_imap", imap)
    first = await client.search("INBOX", limit=2)
    second = await client.search("INBOX", limit=2, before_uid=first.next_uid, expected_uidvalidity=17)
    assert ([item.locator.uid for item in first.messages], [item.locator.uid for item in second.messages], second.next_uid) == ([3, 2], [1], None)


@pytest.mark.parametrize("changed", [None, "integration_id", "revision", "mailbox", "query", "invalid"])
async def test_mail_search_cursor_scope(changed) -> None:
    arguments = MailSearchArguments(integration_id=uuid4(), mailbox="INBOX", query="workshop")
    client = SimpleNamespace(search=AsyncMock(return_value=MessagePage((), 17, 42)))
    result = await search_messages(arguments, client, "revision")
    arguments.cursor = result["nextCursor"]
    if changed is None:
        await search_messages(arguments, client, "revision")
        client.search.assert_awaited_with(
            "INBOX", query="workshop", limit=50, before_uid=42, expected_uidvalidity=17,
        )
        return
    revision = "revision"
    if changed == "revision":
        revision = "changed"
    elif changed == "invalid":
        arguments.cursor = "invalid"
    else:
        setattr(arguments, changed, uuid4() if changed == "integration_id" else "changed")
    with pytest.raises(ValueError):
        await search_messages(arguments, client, revision)


async def test_mail_search_cursor_rejects_changed_uidvalidity(monkeypatch) -> None:
    client = _client()

    @contextmanager
    def imap():
        yield SimpleNamespace(select=lambda *_args, **_kwargs: ("OK", []), response=lambda _name: ("OK", [b"18"]))

    monkeypatch.setattr(client, "_imap", imap)
    with pytest.raises(MailError, match="mail.staleReference"):
        await client.search("INBOX", before_uid=42, expected_uidvalidity=17)


@pytest.mark.parametrize("channel", ["agent", "api"])
@pytest.mark.parametrize("replay", [False, True])
@pytest.mark.parametrize("confirmed", [True, False, None])
async def test_sent_draft_cleanup_requires_confirmed_copy(monkeypatch, channel, replay, confirmed) -> None:
    account = _account()
    revision = binding_revision(account)
    token = token_for(account.id, revision, MessageLocator("Drafts", 17, 42))
    client = SimpleNamespace(
        get_message=AsyncMock(return_value=SimpleNamespace(raw=b"saved draft")),
        delete_draft=AsyncMock(),
    )
    result = MailSendResult(sent_copy_confirmed=confirmed)
    module = mail_adapter if channel == "agent" else mail_routes
    monkeypatch.setattr(module, "send_status", AsyncMock(return_value=result if replay else None))
    monkeypatch.setattr(module, "send_once", AsyncMock(return_value=result))
    user = SimpleNamespace(id=uuid4())
    if channel == "agent":
        monkeypatch.setattr(module, "_selected_client", AsyncMock(return_value=(client, account, revision)))
        await module.mail_send_draft(
            MailTokenArguments(integration_id=account.id, message_token=token),
            SimpleNamespace(run_id=uuid4(), tool_call=SimpleNamespace(id="call"), session=SimpleNamespace(user_id=user.id), bound_options={}),
        )
    else:
        monkeypatch.setattr(module, "_account", AsyncMock(return_value=(account, client, revision)))
        await module.send_draft(account.id, token, user, None, "key")
    assert client.delete_draft.await_count == int(confirmed is True)


@pytest.mark.parametrize("channel", ["agent", "api"])
async def test_generated_message_id_uses_sender_domain(monkeypatch, channel) -> None:
    account = _account()
    sent = AsyncMock(return_value=MailSendResult(sent_copy_confirmed=True))
    if channel == "agent":
        monkeypatch.setattr(mail_adapter, "_selected_client", AsyncMock(return_value=(None, account, "revision")))
        monkeypatch.setattr(mail_adapter, "send_once", sent)
        await mail_adapter.mail_send_message(
            MailSendMessageArguments(integration_id=account.id, to=("other@example.com",)),
            SimpleNamespace(run_id=uuid4(), tool_call=SimpleNamespace(id="call"), session=SimpleNamespace(user_id=uuid4())),
        )
    else:
        monkeypatch.setattr(mail_routes, "_account", AsyncMock(return_value=(account, None, "revision")))
        monkeypatch.setattr(mail_routes, "send_once", sent)
        monkeypatch.setattr(SettingsStore, "load", AsyncMock(return_value=InstanceLimits()))
        await mail_routes.send_message(
            integration_id=account.id, user=SimpleNamespace(id=uuid4()), db=None,
            to=["other@example.com"], idempotency_key="key",
        )
    from email.parser import BytesParser

    assert str(BytesParser().parsebytes(sent.await_args.kwargs["raw"])["Message-ID"]).endswith("@example.com>")


async def test_imap_rejects_control_characters_before_select(monkeypatch) -> None:
    client = _client()

    @contextmanager
    def imap():
        raise AssertionError("invalid mailbox must be rejected before opening IMAP")
        yield

    monkeypatch.setattr(client, "_imap", imap)
    with pytest.raises(MailError) as error:
        await client.search("INBOX\r\nLOGOUT")
    assert error.value.code == "mail.invalidMessage"


async def test_imap_rejects_oversized_message_before_fetching_body(monkeypatch) -> None:
    body_fetched = False

    class FakeImap:
        def select(self, _mailbox, readonly=False):
            assert readonly
            return "OK", [b"1"]

        def response(self, _name):
            return "OK", [b"17"]

        def uid(self, _command, _uid, fields):
            nonlocal body_fetched
            if fields == "(UID FLAGS RFC822.SIZE)":
                return "OK", [b"1 (UID 42 FLAGS () RFC822.SIZE 999999999)"]
            body_fetched = True
            return "OK", []

    client = _client()

    @contextmanager
    def imap():
        yield FakeImap()

    monkeypatch.setattr(client, "_imap", imap)
    with pytest.raises(MailError) as error:
        await client.get_message(MessageLocator("INBOX", 17, 42))
    assert error.value.code == "mail.responseTooLarge"
    assert not body_fetched


async def test_smtp_disconnect_is_outcome_unknown_and_is_not_retried(monkeypatch) -> None:
    attempts = 0
    sent_from = None

    class FakeSmtp:
        def send_message(self, message, *_args, **_kwargs):
            nonlocal attempts, sent_from
            attempts += 1
            sent_from = str(message["From"])
            raise OSError("connection lost after DATA")

    client = _client()

    @contextmanager
    def smtp():
        yield FakeSmtp()

    monkeypatch.setattr(client, "_smtp", smtp)
    raw = build_message(
        from_address="forged@example.com",
        to=("other@example.com",),
        subject="Test",
        body="Body",
    )

    with pytest.raises(MailError) as error:
        await client.send(raw)
    assert error.value.code == "mail.outcomeUnknown"
    assert attempts == 1
    assert sent_from == "user@example.com"


def test_mail_multipart_routes_have_request_body_limits() -> None:
    for method, path in (
        ("POST", f"/api/mail/accounts/{uuid4()}/drafts"),
        ("PUT", f"/api/mail/accounts/{uuid4()}/drafts/token"),
        ("POST", f"/api/mail/accounts/{uuid4()}/messages/send"),
    ):
        assert _request_limit({"type": "http", "method": method, "path": path})
