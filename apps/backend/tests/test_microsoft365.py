import asyncio
import base64
import hashlib
import json
import time
from dataclasses import replace
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, Response
from fastapi.testclient import TestClient
from PIL import Image
from runtime_helpers import _runtime_setup, _tool_messages

from mato_loop_backend.agent.runtime import AgentRuntime
from mato_loop_backend.api.errors import ApiError, register_error_handlers
from mato_loop_backend.api.routes import mail as mail_routes
from mato_loop_backend.api.routes import microsoft365 as routes
from mato_loop_backend.files.service import RootFileService
from mato_loop_backend.files.storage import SessionStorage
from mato_loop_backend.integrations.mail.send import MailSendResult
from mato_loop_backend.integrations.microsoft365 import adapter, client, oauth, organizer, service
from mato_loop_backend.integrations.microsoft365 import mail as graph_mail
from mato_loop_backend.integrations.registry import effective_integration_contributions
from mato_loop_backend.integrations.service import IntegrationSettingsError
from mato_loop_backend.models import Credential, UserAccount, WebSearchPolicy
from mato_loop_backend.permissions.policy import PermissionDecision
from mato_loop_backend.permissions.resolution import tool_execution_policy
from mato_loop_backend.providers.base import ProviderResult, ToolCall
from mato_loop_backend.providers.service import ProviderService
from mato_loop_backend.security import CredentialCipher
from mato_loop_backend.settings import (
    InstanceLimits,
    IntegrationInstance,
    IntegrationSettings,
    SettingsStore,
    ToolExecutionPolicy,
    ToolSettings,
    UserIntegrationSettings,
)
from mato_loop_backend.tools.actions import ToolAction
from mato_loop_backend.tools.catalog import tool_registry
from mato_loop_backend.tools.contracts import ToolResult
from mato_loop_backend.tools.references import model_tool_references
from mato_loop_backend.tools.registry import ToolRegistry


@pytest.fixture(autouse=True)
def cipher(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "mato_loop_backend.auth.credentials.get_runtime_config",
        lambda: SimpleNamespace(secrets_dir=tmp_path),
    )


@pytest.fixture
def transport(monkeypatch):
    original = httpx.AsyncClient

    def install(handler):
        monkeypatch.setattr(
            client.httpx,
            "AsyncClient",
            lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
        )

    return install


def account(flow="device_code"):
    return IntegrationInstance(
        name="Work mail",
        integration_type="microsoft365",
        adapter_type="microsoft_graph",
        owner_id=uuid4(),
        credential_id=uuid4(),
        config={"tenant_id": str(uuid4()), "client_id": str(uuid4()), "auth_flow": flow},
    )


async def _parallel_todo_runtime(monkeypatch, count):
    instance = account()
    instance.config["read_tasks"] = True
    response = ProviderResult(tool_calls=[
        ToolCall(str(index), "m365_todo", {
            "integration_id": str(instance.id), "operation": "list_lists", "limit": index,
        })
        for index in range(1, count + 1)
    ])
    execute = tool_registry._execute_definition
    db, run, *_ = await _runtime_setup(
        monkeypatch, [response, ProviderResult(content="Done")],
        search_policy=WebSearchPolicy.DISABLED, input_budget_tokens=100_000,
    )
    db.get.return_value.user_id = instance.owner_id
    load_optional = SettingsStore.load_optional

    async def personal_integrations(db, kind, **kwargs):
        if kind is UserIntegrationSettings:
            return UserIntegrationSettings(instances=[instance])
        return await load_optional(db, kind, **kwargs)

    monkeypatch.setattr(SettingsStore, "load_optional", personal_integrations)
    monkeypatch.setattr(tool_registry, "_execute_definition", execute)
    monkeypatch.setattr(AgentRuntime, "_authorize", AsyncMock(return_value=PermissionDecision.ALLOW))
    monkeypatch.setattr(organizer, "access_token", AsyncMock(return_value="access"))
    return db, run, response.tool_calls


@pytest.mark.usefixtures("skill_library")
async def test_existing_todo_reads_overlap_with_a_bound_and_keep_result_order(monkeypatch):
    db, run, calls = await _parallel_todo_runtime(monkeypatch, 6)
    gates = {4: asyncio.Event(), 6: asyncio.Event()}
    completed = {4: asyncio.Event(), 6: asyncio.Event()}
    active, completion_order = set(), []
    peak = 0

    async def page(_client, _path, _model, *, params, **kwargs):
        nonlocal peak
        index = params["$top"]
        end = 4 if index <= 4 else 6
        active.add(index)
        peak = max(peak, len(active))
        if index == end:
            gates[end].set()
        await gates[end].wait()
        if index != end:
            await completed[end].wait()
        active.remove(index)
        completion_order.append(str(index))
        if index == end:
            completed[end].set()
        return [], None

    refreshing = False

    async def refresh(_session):
        nonlocal refreshing
        assert not refreshing
        refreshing = True
        await asyncio.sleep(0)
        refreshing = False

    db.refresh.side_effect = refresh
    monkeypatch.setattr(client.GraphClient, "page", page)
    await asyncio.wait_for(AgentRuntime._execute_loop(db, run), timeout=10)
    assert peak == 4, [(message.tool_call_id, message.content) for message in _tool_messages(db)]
    assert completion_order != [call.id for call in calls]
    assert [message.tool_call_id for message in _tool_messages(db)] == [call.id for call in calls]


@pytest.mark.usefixtures("skill_library")
@pytest.mark.parametrize("misdeclared_write", [False, True])
async def test_non_read_tool_drains_reads_before_starting_the_next_group(monkeypatch, misdeclared_write):
    db, run, calls = await _parallel_todo_runtime(monkeypatch, 3)
    calls.insert(2, ToolCall("barrier", "silence", {}))
    second_started = asyncio.Event()
    active, trace = set(), []

    async def page(_client, _path, _model, *, params, **kwargs):
        index = params["$top"]
        active.add(index)
        trace.append(f"start:{index}")
        if index == 1:
            await second_started.wait()
        elif index == 2:
            second_started.set()
        active.remove(index)
        trace.append(f"end:{index}")
        return [], None

    async def barrier(_arguments, _context):
        assert not active
        trace.append("barrier")
        return ToolResult.json({})

    monkeypatch.setattr(client.GraphClient, "page", page)
    monkeypatch.setitem(tool_registry._definitions, "silence", replace(
        tool_registry._definitions["silence"], handler=barrier, parallel_read=misdeclared_write,
        actions=(ToolAction("tasks.write", "Aufgaben ändern", category="write", mutates=True),),
    ))
    await asyncio.wait_for(AgentRuntime._execute_loop(db, run), timeout=10)
    assert trace.index("end:1") < trace.index("barrier") < trace.index("start:3")


@pytest.mark.usefixtures("skill_library")
@pytest.mark.parametrize("decision,executed", [
    (PermissionDecision.USER_DENIED, []),
    (PermissionDecision.DENY, [2]),
])
async def test_parallel_reads_keep_denials_before_effects(monkeypatch, decision, executed):
    db, run, _calls = await _parallel_todo_runtime(monkeypatch, 2)
    page = AsyncMock(return_value=([], None))
    monkeypatch.setattr(client.GraphClient, "page", page)
    monkeypatch.setattr(AgentRuntime, "_authorize", AsyncMock(side_effect=[
        decision, PermissionDecision.ALLOW,
    ]))
    await AgentRuntime._execute_loop(db, run)
    assert [call.kwargs["params"]["$top"] for call in page.call_args_list] == executed


@pytest.mark.usefixtures("skill_library")
async def test_cancelling_a_run_joins_started_parallel_reads(monkeypatch):
    db, run, _calls = await _parallel_todo_runtime(monkeypatch, 2)
    started, cancelled = set(), set()
    both_started = asyncio.Event()

    async def page(_client, _path, _model, *, params, **kwargs):
        index = params["$top"]
        started.add(index)
        if len(started) == 2:
            both_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.add(index)

    monkeypatch.setattr(client.GraphClient, "page", page)
    execution = asyncio.create_task(AgentRuntime._execute_loop(db, run))
    try:
        await asyncio.wait_for(both_started.wait(), timeout=10)
    finally:
        execution.cancel()
        with pytest.raises(asyncio.CancelledError):
            await execution
    assert cancelled == {1, 2}


@pytest.mark.usefixtures("skill_library")
async def test_rejected_read_waits_for_earlier_parallel_results(monkeypatch):
    db, run, calls = await _parallel_todo_runtime(monkeypatch, 2)
    read_started = False
    release = asyncio.Event()

    async def page(_client, _path, _model, *, params, **kwargs):
        nonlocal read_started
        if params["$top"] == 2:
            raise client.Microsoft365Error("microsoft365.invalidResponse")
        read_started = True
        await release.wait()
        return [], None

    async def refresh(_session):
        if read_started:
            release.set()

    db.refresh.side_effect = refresh
    monkeypatch.setattr(ProviderService, "complete", AsyncMock(side_effect=[
        ProviderResult(tool_calls=[
            ToolCall("previous-failure", calls[1].name, dict(calls[1].arguments)),
        ]),
        ProviderResult(tool_calls=calls),
        ProviderResult(content="Done"),
    ]))
    monkeypatch.setattr(client.GraphClient, "page", page)
    await asyncio.wait_for(AgentRuntime._execute_loop(db, run), timeout=10)
    assert [
        (message.tool_call_id, message.provider_data["_matoloop"].get("errorCode"))
        for message in _tool_messages(db)
    ] == [
        ("previous-failure", "microsoft365.invalidResponse"),
        ("1", None),
        ("2", "agent.repeatedToolCall"),
    ]


@pytest.fixture
def mailbox_api(monkeypatch):
    instance = account()
    instance.config["allow_drafts"] = True
    settings = IntegrationSettings(instances=[instance])
    user = SimpleNamespace(id=instance.owner_id)
    monkeypatch.setattr(mail_routes, "load_effective_integrations", AsyncMock(return_value=settings))
    monkeypatch.setattr(mail_routes, "access_token", AsyncMock(return_value="access"))
    monkeypatch.setattr(mail_routes.SettingsStore, "load", AsyncMock(return_value=InstanceLimits()))
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(mail_routes.router, prefix="/api")
    app.dependency_overrides[mail_routes.require_user] = lambda: user
    app.dependency_overrides[mail_routes.get_db_session] = lambda: None
    with TestClient(app) as browser:
        yield instance, browser


def test_control_center_reads_folders_messages_and_bounded_attachments(transport, mailbox_api):
    instance, browser = mailbox_api
    root = f"/api/mail/accounts/{instance.id}"
    folder = {"id": "inbox-id", "displayName": "Posteingang", "totalItemCount": 1, "unreadItemCount": 0, "childFolderCount": 1}
    message = {"id": "message", "subject": "Hello", "isRead": True, "hasAttachments": True}
    attachment = {"@odata.type": "#microsoft.graph.fileAttachment", "id": "file", "name": "report.txt", "size": 4}

    def remote(request):
        assert request.method == "GET"
        path = request.url.path.removeprefix("/v1.0")
        if path == "/me/mailFolders/inbox":
            return httpx.Response(200, json=folder)
        if path == "/me/mailFolders":
            return httpx.Response(200, json={"value": [folder]})
        if path.endswith("/childFolders"):
            return httpx.Response(200, json={"value": [{**folder, "id": "child", "displayName": "Child", "childFolderCount": 0}]})
        if path.endswith("/messages"):
            assert path == "/me/mailFolders/inbox-id/messages"
            return httpx.Response(200, json={"value": [message]})
        if path.endswith("/attachments"):
            return httpx.Response(200, json={"value": [attachment, {**attachment, "id": "item", "@odata.type": "#microsoft.graph.itemAttachment"}]})
        if path.endswith("/attachments/file"):
            return httpx.Response(200, json=attachment)
        if path.endswith("/$value"):
            return httpx.Response(200, content=b"Mail")
        return httpx.Response(200, json={**message, "body": {"contentType": "text", "content": "Message text"}})

    transport(remote)
    folders = browser.get(root + "/mailboxes").json()["mailboxes"]
    assert folders[0]["role"] == "inbox"
    assert browser.get(root + "/mailboxes", params={"parentKey": folders[0]["key"]}).json()["mailboxes"][0]["key"] == "child"
    messages = browser.get(root + "/messages", params={"mailboxKey": folders[0]["key"]}).json()["messages"]
    assert messages[0]["unread"] is False
    detail = browser.get(root + "/messages/" + messages[0]["messageToken"]).json()["message"]
    assert detail["body"] == "Message text"
    assert detail["attachments"][1]["downloadSupported"] is False
    download = browser.get(root + "/attachments/" + detail["attachments"][0]["attachmentToken"])
    assert download.content == b"Mail" and download.headers["Content-Disposition"].endswith("report.txt")


@pytest.mark.parametrize("reply", [False, True])
def test_control_center_creates_only_opted_in_drafts(transport, mailbox_api, reply):
    instance, browser = mailbox_api
    requests = []
    root = f"/api/mail/accounts/{instance.id}"

    def remote(request):
        requests.append(request)
        if reply and request.method == "PATCH":
            assert request.url.path == "/v1.0/me/messages/draft"
            assert json.loads(request.content) == {
                "subject": "Aw: Subject", "body": {"contentType": "text", "content": "Message\n\nOriginal"},
            }
            return httpx.Response(200, json={})
        assert request.method == "POST"
        assert request.url.path == "/v1.0/me/messages" + ("/original/createReply" if reply else "")
        payload = json.loads(request.content)
        assert payload == {} if reply else payload["body"] == {"contentType": "text", "content": "Message"}
        return httpx.Response(201, json={
            "id": "draft", "isDraft": True, "subject": "Re: Subject" if reply else "Subject",
            "body": {"contentType": "text", "content": "Original"},
        })

    transport(remote)
    data = {"body": "Message", "replyTo": adapter._encode(instance, "message", "original")} if reply else {
        "to": "recipient@example.com", "subject": "Subject", "body": "Message",
    }
    response = browser.post(root + "/drafts", data=data)
    assert response.status_code == 201 and response.json()["isDraft"] is True and response.json()["subject"] == ("Aw: Subject" if reply else "Subject")
    instance.config["allow_drafts"] = False
    response = browser.post(root + "/drafts", data=data)
    assert response.status_code == 409 and response.json()["error"]["code"] == "microsoft365.featureDisabled"
    assert len(requests) == (2 if reply else 1)


@pytest.mark.parametrize(("method", "path"), [
    ("put", "/drafts/token"), ("delete", "/drafts/token"),
    ("post", "/drafts/token/send"), ("post", "/messages/send"),
    ("delete", "/messages/token"),
])
def test_control_center_rejects_graph_writes_without_opt_in(transport, mailbox_api, method, path):
    instance, browser = mailbox_api
    instance.config["allow_drafts"] = False
    transport(lambda _request: pytest.fail("Unsupported writes must not contact Graph"))
    response = getattr(browser, method)(
        f"/api/mail/accounts/{instance.id}" + path,
        headers={"Idempotency-Key": "test-key"}, **({"data": {"to": "recipient@example.com"}} if method == "post" else {}),
    )
    assert response.status_code == 409 and response.json()["error"]["code"] == "microsoft365.featureDisabled"


@pytest.mark.parametrize("change", ["owner", "disabled"])
def test_control_center_does_not_expose_unavailable_microsoft_accounts(transport, mailbox_api, change):
    instance, browser = mailbox_api
    transport(lambda _request: pytest.fail("Unavailable accounts must not contact Graph"))
    if change == "owner":
        instance.owner_id = uuid4()
    else:
        instance.enabled = False
    assert browser.get("/api/mail/accounts").json()["accounts"] == []
    assert browser.get(f"/api/mail/accounts/{instance.id}/mailboxes").status_code == 404


def image_signature():
    images = []
    for index in range(7):
        stream = BytesIO()
        Image.new("RGB", (2, 2), (index * 30, 0, 0)).save(stream, "PNG")
        encoded = base64.b64encode(stream.getvalue()).decode()
        images.append('<img src="data:image/png;base64, ' + encoded[:32] + "\r\n" + encoded[32:] + '"/>')
    return (
        '<table cellpadding="0" cellspacing="0" style="border-collapse:collapse"><tr><td valign="top">'
        + "".join(images) + '</td>'
        '<td style="border-left:2px solid #800080;padding-left:6px;color:#800080;font-family:Arial,sans-serif;font-size:11pt">'
        '<b>Team</b></td></tr></table>'
    )


@pytest.mark.parametrize("operation", ["drafts", "messages/send"])
def test_control_center_composes_cc_bcc_files_and_cid_signature(transport, mailbox_api, monkeypatch, operation):
    instance, browser = mailbox_api
    instance.config.update(allow_send=True, signature_html=image_signature())
    monkeypatch.setattr(mail_routes.SettingsStore, "load", AsyncMock(return_value=InstanceLimits()))

    async def reserved_send(**arguments):
        await arguments["client"].send(arguments["raw"])
        return MailSendResult(sent_copy_confirmed=None)

    monkeypatch.setattr(graph_mail, "send_once", reserved_send)
    payloads = []

    def remote(request):
        assert request.method == "POST"
        payload = json.loads(request.content)
        payloads.append(payload["message"] if operation == "messages/send" else payload)
        return httpx.Response(202) if operation == "messages/send" else httpx.Response(201, json={"id": "draft", "isDraft": True})

    transport(remote)
    response = browser.post(
        f"/api/mail/accounts/{instance.id}/{operation}",
        headers={"Idempotency-Key": "compose"},
        data={"to": "to@example.com", "cc": "cc@example.com", "bcc": "bcc@example.com", "subject": "Test", "body": "<Plain text>"},
        files={"attachments": ("report.txt", b"report", "text/plain")},
    )
    assert response.status_code == (200 if operation == "messages/send" else 201)
    payload = payloads[0]
    assert payload["ccRecipients"][0]["emailAddress"]["address"] == "cc@example.com"
    assert payload["bccRecipients"][0]["emailAddress"]["address"] == "bcc@example.com"
    file, *inline = payload["attachments"]
    assert base64.b64decode(file["contentBytes"]) == b"report"
    assert len(inline) == 7 and all(item["isInline"] and "cid:" + item["contentId"] in payload["body"]["content"] for item in inline)
    assert "&lt;Plain text&gt;" in payload["body"]["content"] and "data:image" not in payload["body"]["content"]
    assert '<table cellpadding="0"' in payload["body"]["content"] and "border-left:2px solid #800080" in payload["body"]["content"]


@pytest.mark.parametrize("markup", [
    '<script>alert(1)</script>', '<img src="https://example.com/track.png">',
    '<img src="data:image/svg+xml;base64,PHN2Zy8+">',
    '<b onclick="alert(1)">Team</b>', '<a href="javascript:alert(1)">Team</a>',
    '<span style="background-image:url(https://example.com/track.png)">Team</span>',
    '<span style="color:expression(alert(1))">Team</span>',
    '<span style="position:fixed;top:0">Team</span>',
    '<span style="color:var(--private-value)">Team</span>',
    r'<span style="color:r\65 d">Team</span>',
    "<" * 200_000,
])
def test_signature_rejects_unsupported_markup_and_sizes(markup):
    with pytest.raises(ValueError):
        client.Microsoft365Config(tenant_id=uuid4(), client_id=uuid4(), signature_html=markup)


@pytest.mark.parametrize("large_signature", [False, True])
def test_outgoing_size_limit_is_enforced_before_graph(transport, mailbox_api, monkeypatch, large_signature):
    instance, browser = mailbox_api
    instance.config["allow_send"] = True
    if large_signature:
        instance.config["signature_html"] = image_signature()
    monkeypatch.setattr(mail_routes.SettingsStore, "load", AsyncMock(return_value=InstanceLimits(
        max_upload_mib=2, max_message_upload_mib=2,
    )))
    transport(lambda _request: pytest.fail("Oversized mail must not reach Graph"))
    response = browser.post(
        f"/api/mail/accounts/{instance.id}/messages/send",
        headers={"Idempotency-Key": "large"}, data={"to": "to@example.com"},
        files={"attachments": ("large.bin", b"x" * (2 * 1024 * 1024 + (0 if large_signature else 1)))},
    )
    assert response.status_code == 413


@pytest.mark.parametrize("operation", ["draft", "send", "reply"])
async def test_large_attachments_use_bounded_upload_sessions(transport, caplog, operation):
    from mato_loop_backend.integrations.microsoft365.composition import message_payload

    data = b"x" * (5 * 1024 * 1024)
    uploaded = bytearray()
    effects = []
    caplog.set_level("INFO", logger="httpx")

    def remote(request):
        effects.append(request.url.path)
        if request.method == "PUT":
            assert request.url.host == "outlook.office.com" and "Authorization" not in request.headers
            start = len(uploaded)
            uploaded.extend(request.content)
            end = len(uploaded)
            assert request.headers["Content-Range"] == f"bytes {start}-{end - 1}/{len(data)}"
            assert len(request.content) < 4 * 1024 * 1024
            return httpx.Response(201) if end == len(data) else httpx.Response(200, json={"nextExpectedRanges": [str(end)]})
        if request.url.path.endswith("/createUploadSession"):
            assert json.loads(request.content)["AttachmentItem"]["size"] == len(data)
            return httpx.Response(201, json={"uploadUrl": "https://outlook.office.com/api/upload?authtoken=private-upload-token"})
        if request.url.path.endswith("/send"):
            assert uploaded == data
            return httpx.Response(202)
        if request.method == "PATCH":
            return httpx.Response(200, json={})
        assert "attachments" not in json.loads(request.content)
        return httpx.Response(201, json={"id": "draft", "isDraft": True, "body": {"contentType": "text", "content": "Original"}})

    transport(remote)
    graph = client.GraphClient("access")
    payload = message_payload(to=["to@example.com"], subject="Test", body="Text", attachments=[("file.bin", "application/octet-stream", data)])
    if operation == "send":
        await graph.send_message(payload)
    elif operation == "reply":
        await graph.reply_draft("source", "Text", message=payload)
    else:
        await graph.new_draft([], "", "", message=payload)
    assert uploaded == data
    assert "private-upload-token" not in caplog.text
    assert effects[-1].endswith("/send") == (operation == "send")


@pytest.mark.parametrize("url", [
    "https://attacker.example/api/upload?token=secret",
    "http://outlook.office.com/api/upload?token=secret",
    "https://outlook.office.com:444/api/upload?token=secret",
    "https://user@outlook.office.com/api/upload?token=secret",
    "https://outlook.office.com/api/upload?token=secret#fragment",
    "https://outlook.office.com/other?token=secret",
])
async def test_upload_session_rejects_untrusted_destinations(transport, url):
    def remote(request):
        assert request.method == "POST" and request.url.host == "graph.microsoft.com"
        return httpx.Response(201, json={"uploadUrl": url})

    transport(remote)
    with pytest.raises(client.Microsoft365Error, match="outcomeUnknown"):
        await client.GraphClient("access").add_attachment("draft", {
            "name": "file.bin", "contentType": "application/octet-stream",
            "contentBytes": base64.b64encode(b"x" * (3 * 1024 * 1024)).decode(),
        })


async def test_failed_staged_upload_is_not_sent_or_replayed(transport):
    from mato_loop_backend.integrations.microsoft365.composition import message_payload

    effects = []

    def remote(request):
        effects.append(request.url.path)
        if request.url.path.endswith("/createUploadSession"):
            return httpx.Response(201, json={"uploadUrl": "https://outlook.office.com/api/upload?token=test"})
        if request.method == "PUT":
            raise httpx.ReadTimeout("Lost upload response")
        return httpx.Response(201, json={"id": "draft", "isDraft": True})

    transport(remote)
    with pytest.raises(client.Microsoft365Error, match="outcomeUnknown"):
        await client.GraphClient("access").send_message(message_payload(
            to=["to@example.com"], subject="", body="",
            attachments=[("file.bin", "application/octet-stream", b"x" * (3 * 1024 * 1024))],
        ))
    assert len(effects) == 3 and not any(path.endswith("/send") for path in effects)


@pytest.mark.parametrize("preserve_body", [False, True])
def test_control_center_edits_drafts_without_losing_html_or_inline_images(transport, mailbox_api, preserve_body):
    instance, browser = mailbox_api
    token = adapter._encode(instance, "message", "draft")
    regular = {"@odata.type": "#microsoft.graph.fileAttachment", "id": "old", "name": "old.txt", "size": 1}
    inline = {**regular, "id": "logo", "isInline": True}
    patches, attachments, deleted = [], [], []
    data = b"x" * (2 * 1024 * 1024)
    html = '<table><tr><td>Edited</td><td><img src="cid:logo"></td></tr></table>'

    def remote(request):
        if request.method == "PATCH":
            patches.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "draft"})
        if request.method == "POST":
            attachments.append(json.loads(request.content))
            return httpx.Response(201, json={"id": "new", **attachments[-1]})
        if request.method == "DELETE":
            deleted.append(request.url.path)
            return httpx.Response(204)
        if request.url.path.endswith("/attachments"):
            return httpx.Response(200, json={"value": [regular, inline]})
        return httpx.Response(200, json={
            "id": "draft", "isDraft": True, "changeKey": "version",
            "body": {"contentType": "html" if 'body-content-type="text"' not in request.headers["Prefer"] else "text", "content": html},
        })

    transport(remote)
    response = browser.put(
        f"/api/mail/accounts/{instance.id}/drafts/{token}",
        data={
            "expectedVersion": "version", "subject": "Changed", "to": "to@example.com",
            "body": html, "bodyFormat": "html", "preserveBody": str(preserve_body).lower(), "replaceAttachments": "true",
        },
        files={"attachments": ("new.txt", data, "text/plain")},
    )
    assert response.status_code == 200 and response.json()["message"]["editBody"] == html
    assert patches == [{
        "toRecipients": [{"emailAddress": {"address": "to@example.com"}}], "ccRecipients": [], "bccRecipients": [],
        "subject": "Changed", **({} if preserve_body else {"body": {"contentType": "html", "content": html}}),
    }]
    assert deleted == ["/v1.0/me/messages/draft/attachments/old"]
    assert len(attachments) == 1 and base64.b64decode(attachments[0]["contentBytes"]) == data


@pytest.mark.parametrize("failure", ["stale", "not-draft", "foreign-attachment", "lossy-html", "size"])
def test_draft_edit_rejects_unsafe_updates_before_effects(transport, mailbox_api, monkeypatch, failure):
    instance, browser = mailbox_api
    token = adapter._encode(instance, "message", "draft")
    monkeypatch.setattr(mail_routes.SettingsStore, "load", AsyncMock(return_value=InstanceLimits(max_upload_mib=1, max_message_upload_mib=1)))

    def remote(request):
        assert request.method == "GET"
        if request.url.path.endswith("/attachments"):
            return httpx.Response(200, json={"value": [{
                "@odata.type": "#microsoft.graph.fileAttachment", "id": "old", "name": "old", "size": 2 * 1024 * 1024 if failure == "size" else 1,
            }]})
        return httpx.Response(200, json={
            "id": "draft", "isDraft": failure != "not-draft",
            "changeKey": "new-version" if failure == "stale" else "version",
            "body": {"contentType": "html", "content": "<b>Preserve</b>"},
        })

    transport(remote)
    response = browser.put(
        f"/api/mail/accounts/{instance.id}/drafts/{token}",
        data={
            "expectedVersion": "version",
            **({"replaceAttachments": "true", "retainedAttachmentToken": "foreign"} if failure == "foreign-attachment" else {}),
        },
    )
    assert response.status_code == (413 if failure == "size" else 409)


@pytest.mark.parametrize(("flag", "scope"), [("allow_send", "Mail.Send"), ("allow_delete", "Mail.ReadWrite")])
def test_mail_write_scopes_are_requested_and_required(flag, scope):
    instance = account()
    instance.config[flag] = True
    payload = credential(instance)
    assert scope in payload.config.scopes
    with pytest.raises(client.Microsoft365Error, match="invalidResponse"):
        oauth.apply_tokens(payload, tokens(scope="Mail.Read"))
    oauth.apply_tokens(payload, tokens(scope="Mail.ReadWrite " + scope))


@pytest.mark.parametrize("operation", ["send", "delete", "update"])
async def test_remote_mail_actions_preview_and_recheck_approved_version(transport, tool_context, monkeypatch, operation):
    instance, context = tool_context
    instance.config.update(allow_send=True, allow_delete=True)
    context.bound_options["binding_revision"] = adapter.binding_revision(instance)
    token = adapter._encode(instance, "message", "draft")
    version = "one"

    def remote(request):
        assert request.method == "GET"
        if request.url.path.endswith("/attachments"):
            return httpx.Response(200, json={"value": []})
        return httpx.Response(200, json={
            "id": "draft", "isDraft": True, "changeKey": version, "subject": "Subject",
            "bccRecipients": [{"emailAddress": {"address": "private@example.com"}}],
            "body": {"contentType": "text", "content": "Approved body"},
        })

    transport(remote)
    monkeypatch.setattr(graph_mail, "send_status", AsyncMock(return_value=None))
    model = {"send": adapter.SendArguments, "delete": adapter.MessageReferenceArguments, "update": adapter.NewDraftArguments}[operation]
    args = model(integration_id=instance.id, message_token=token)
    (permission,) = await adapter._permission(args, context)
    assert permission.capability == "mail." + ("drafts" if operation == "update" else operation)
    if operation == "send":
        assert next(item.value for item in permission.display_items if item.label == "Bcc") == ("private@example.com",)
    version = "two"
    result = await {"send": adapter.send_mail, "delete": adapter.delete_mail, "update": adapter.create_draft}[operation](args, context)
    assert result.is_error and "staleReference" in result.content


async def test_denied_send_does_not_inspect_graph(transport, tool_context):
    instance, context = tool_context
    instance.config["allow_send"] = True
    context.bound_options["binding_revision"] = adapter.binding_revision(instance)
    context.ask_handler = AsyncMock(return_value=PermissionDecision.DENY)
    transport(lambda _request: pytest.fail("Hard denial must precede remote inspection"))
    requests = await adapter._permission(adapter.SendArguments(
        integration_id=instance.id, message_token=adapter._encode(instance, "message", "draft"),
    ), context)
    assert requests[0].capability == "mail.send"


async def test_draft_attachments_require_session_read_and_preserve_file_bytes(transport, tool_context, tmp_path):
    instance, context = tool_context
    context.session.id = uuid4()
    storage = SessionStorage(tmp_path / "sessions")
    storage.provision(context.session.id)
    context.root_files = RootFileService(context.session.id, session_storage=storage)
    context.root_files.create_file("session", "report.txt", BytesIO(b"report"), max_bytes=1024)
    payloads = []

    def remote(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(201, json={"id": "draft", "isDraft": True})

    transport(remote)
    args = adapter.NewDraftArguments(integration_id=instance.id, to=["to@example.com"], attachments=["report.txt"])
    permissions = await adapter._permission(args, context)
    assert [item.capability for item in permissions] == ["mail.drafts", "session.files.read"]
    await adapter.create_draft(args, context)
    assert base64.b64decode(payloads[0]["attachments"][0]["contentBytes"]) == b"report"
    with pytest.raises(ValueError):
        await adapter._permission(args.model_copy(update={"attachments": ["../outside.txt"]}), context)


@pytest.mark.parametrize("operation", ["send", "delete"])
def test_control_center_sends_unchanged_drafts_and_accepts_empty_graph_success(transport, mailbox_api, monkeypatch, operation):
    instance, browser = mailbox_api
    instance.config.update(allow_send=True, allow_delete=True)
    token = adapter._encode(instance, "message", "draft")
    effects = []

    async def reserved_send(**arguments):
        await arguments["client"].send(arguments["raw"])
        return MailSendResult(sent_copy_confirmed=None)

    monkeypatch.setattr(graph_mail, "send_status", AsyncMock(return_value=None))
    monkeypatch.setattr(graph_mail, "send_once", reserved_send)

    def remote(request):
        if request.method == "GET":
            return httpx.Response(200, json={"id": "draft", "isDraft": True, "changeKey": "version", "body": {"contentType": "text", "content": "Existing body"}})
        effects.append(request)
        return httpx.Response(202 if operation == "send" else 204)

    transport(remote)
    root = f"/api/mail/accounts/{instance.id}/drafts/{token}"
    response = browser.post(root + "/send", headers={"Idempotency-Key": "existing"}, data={"expectedVersion": "version"}) if operation == "send" else browser.delete(root, params={"expectedVersion": "version"})
    assert response.status_code == (200 if operation == "send" else 204)
    assert len(effects) == 1 and not effects[0].content


def credential(instance):
    return oauth.OAuthCredential(
        config=client.Microsoft365Config.model_validate(instance.config),
        client_secret="client-secret" if instance.config["auth_flow"] == "browser" else None,
    )


def tokens(**overrides):
    return {
        "access_token": "access",
        "refresh_token": "refresh",
        "token_type": "Bearer",
        "expires_in": 3600,
        "scope": "Mail.Read",
        **overrides,
    }


@pytest.mark.parametrize("flow", ["browser", "device_code"])
async def test_login_flows_bind_owner_session_and_exchange_selected_scopes(transport, flow):
    instance, requests = account(flow), []
    if flow == "browser":
        instance.config.update(allow_drafts=True, read_calendar=True)
    attempt = oauth.LoginAttempt(
        uuid4(),
        "login-session",
        instance,
        credential(instance),
        redirect_uri="https://matoloop.test" + oauth.CALLBACK_PATH,
    )
    manager = oauth.LoginManager()

    def remote(request):
        requests.append(request)
        if request.url.path.endswith("/devicecode"):
            return httpx.Response(
                200,
                json={
                    "device_code": "device-secret",
                    "user_code": "CODE123",
                    "verification_uri": "https://microsoft.com/devicelogin",
                    "interval": 5,
                    "expires_in": 900,
                },
            )
        data = parse_qs(request.content.decode())
        assert data["client_id"] == [instance.config["client_id"]]
        if flow == "browser":
            assert data["code"] == ["authorization-code"]
            assert data["code_verifier"] == [attempt.verifier]
            assert data["client_secret"] == ["client-secret"]
        else:
            assert data["device_code"] == ["device-secret"]
            assert "client_secret" not in data
        return httpx.Response(200, json=tokens(scope="Mail.ReadWrite Calendars.Read" if flow == "browser" else "Mail.Read"))

    transport(remote)
    result = await manager.start(attempt)
    assert "secret" not in json.dumps(result)
    with pytest.raises(client.Microsoft365Error, match="loginExpired"):
        manager.get(attempt.id, uuid4(), "login-session")
    with pytest.raises(client.Microsoft365Error, match="loginExpired"):
        manager.get(attempt.id, attempt.user_id, "different-session")
    if flow == "browser":
        query = parse_qs(urlsplit(result["authorizationUrl"]).query)
        assert query["scope"] == [credential(instance).config.scopes]
        assert query["response_mode"] == ["form_post"]
        assert query["code_challenge"] == [
            base64.urlsafe_b64encode(hashlib.sha256(attempt.verifier.encode()).digest())
            .decode()
            .rstrip("=")
        ]
        state = query["state"][0]
        manager.callback(state, "authorization-code", None)
        with pytest.raises(client.Microsoft365Error, match="loginExpired"):
            manager.callback(state, "replayed-code", None)
    else:
        assert parse_qs(requests[0].content.decode())["scope"] == [credential(instance).config.scopes]
        assert await manager.poll(attempt) is False
        attempt.next_poll = 0
    assert await manager.poll(attempt) is True
    assert attempt.credential.tokens.access_token == "access"
    count = len(requests)
    assert await manager.poll(attempt) is True
    assert len(requests) == count


async def test_device_polling_handles_pending_slow_down_and_denial(transport):
    instance, calls = account(), []
    attempt = oauth.LoginAttempt(uuid4(), "session", instance, credential(instance))
    attempt.device_code = "device"
    manager = oauth.LoginManager()
    errors = iter(["authorization_pending", "slow_down", "authorization_declined"])

    def remote(request):
        calls.append(request)
        return httpx.Response(
            400, json={"error": next(errors), "error_description": "private provider data"}
        )

    transport(remote)
    assert await manager.poll(attempt) is False
    assert await manager.poll(attempt) is False
    assert len(calls) == 1
    attempt.next_poll = 0
    assert await manager.poll(attempt) is False
    assert attempt.interval == 10
    attempt.next_poll = 0
    with pytest.raises(client.Microsoft365Error, match="authorizationDenied"):
        await manager.poll(attempt)


async def test_uncertain_browser_exchange_is_not_replayed(transport):
    instance = account("browser")
    attempt = oauth.LoginAttempt(uuid4(), "session", instance, credential(instance), code="once")
    requests = []

    def remote(request):
        requests.append(request)
        raise httpx.ReadTimeout("private remote details", request=request)

    transport(remote)
    manager = oauth.LoginManager()
    with pytest.raises(client.Microsoft365Error, match="integration.unavailable"):
        await manager.poll(attempt)
    with pytest.raises(client.Microsoft365Error, match="authorizationFailed"):
        await manager.poll(attempt)
    assert len(requests) == 1


async def test_refresh_rotates_encrypted_tokens_in_separate_owner_scoped_transaction(
    monkeypatch, transport
):
    instance = account()
    payload = oauth.apply_tokens(
        credential(instance), tokens(access_token="old", refresh_token="old-refresh")
    )
    payload.expires_at = 0
    record = Credential(
        id=instance.credential_id,
        user_id=instance.owner_id,
        kind=oauth.CREDENTIAL_KIND,
        label=instance.name,
        encrypted_payload=CredentialCipher().encrypt(payload.model_dump(mode="json")),
    )
    db = AsyncMock()
    db.__aenter__.return_value = db
    db.scalar.return_value = record
    monkeypatch.setattr(oauth, "AsyncSessionFactory", lambda: db)
    stored = AsyncMock()
    monkeypatch.setattr(oauth, "store_credential", stored)

    def remote(request):
        values = parse_qs(request.content.decode())
        assert values["grant_type"] == ["refresh_token"]
        assert values["refresh_token"] == ["old-refresh"]
        return httpx.Response(200, json=tokens(refresh_token="rotated"))

    transport(remote)
    assert await oauth.access_token(instance) == "access"
    assert stored.call_args.kwargs["owner_id"] == instance.owner_id
    assert stored.call_args.kwargs["credential"] is record
    assert stored.call_args.kwargs["payload"]["tokens"]["refresh_token"] == "rotated"
    assert "FOR UPDATE" in str(db.scalar.call_args.args[0])
    db.commit.assert_awaited_once()


@pytest.mark.parametrize("status,error", [(400, "invalid_grant"), (503, "temporarily_unavailable")])
async def test_refresh_failures_preserve_credentials_and_only_revocation_requires_login(
    monkeypatch, transport, status, error
):
    instance = account()
    payload = oauth.apply_tokens(credential(instance), tokens())
    payload.expires_at = 0
    record = Credential(
        user_id=instance.owner_id,
        kind=oauth.CREDENTIAL_KIND,
        encrypted_payload=CredentialCipher().encrypt(payload.model_dump(mode="json")),
    )
    db = AsyncMock()
    db.__aenter__.return_value = db
    db.scalar.return_value = record
    monkeypatch.setattr(oauth, "AsyncSessionFactory", lambda: db)
    stored = AsyncMock()
    monkeypatch.setattr(oauth, "store_credential", stored)
    transport(
        lambda _: httpx.Response(
            status, json={"error": error, "error_description": "must not escape"}
        )
    )
    with pytest.raises(client.Microsoft365Error) as caught:
        await oauth.access_token(instance)
    assert "must not escape" not in str(caught.value)
    assert stored.await_count == (1 if error == "invalid_grant" else 0)
    if stored.await_count:
        assert stored.call_args.kwargs["payload"]["authorization_required"] is True
        assert stored.call_args.kwargs["payload"]["tokens"]["refresh_token"] == "refresh"


def test_refresh_without_replacement_keeps_refresh_token_and_requires_mail_scope():
    instance = account()
    previous = oauth.apply_tokens(credential(instance), tokens())
    assert (
        oauth.apply_tokens(previous, tokens(refresh_token=None)).tokens.refresh_token == "refresh"
    )
    with pytest.raises(client.Microsoft365Error, match="invalidResponse"):
        oauth.apply_tokens(previous, tokens(scope="User.Read"))


async def test_graph_tools_read_own_mailbox_with_bound_pagination_and_plain_text(
    monkeypatch, transport
):
    instance = account()
    context = SimpleNamespace(
        integration_settings=IntegrationSettings(instances=[instance]),
        bound_options={
            "integration_id": str(instance.id),
            "binding_revision": adapter.binding_revision(instance),
        },
    )
    monkeypatch.setattr(adapter, "access_token", AsyncMock(return_value="token"))
    requests = []
    next_link = client.GRAPH_BASE + "/me/mailFolders/inbox/messages?$skiptoken=opaque"

    def remote(request):
        requests.append(request)
        assert request.method == "GET"
        assert request.url.host == "graph.microsoft.com"
        assert request.headers["authorization"] == "Bearer token"
        if request.url.path.endswith("/message-id"):
            assert 'outlook.body-content-type="text"' in request.headers["prefer"]
            return httpx.Response(
                200,
                json={
                    "id": "message-id",
                    "subject": "Invoice",
                    "body": {"contentType": "text", "content": "External mail text"},
                },
            )
        if request.url.params.get("$skiptoken"):
            return httpx.Response(200, json={"value": []})
        assert request.url.path == "/v1.0/me/mailFolders/inbox/messages"
        assert request.url.params["$search"] == '"subject:invoice"'
        assert {"bodyPreview", "importance"} <= set(request.url.params["$select"].split(","))
        return httpx.Response(
            200,
            json={
                "value": [
                    {"id": "message-id", "subject": "Invoice", "importance": "high", "bodyPreview": "Preview " * 100},
                    {"id": "empty", "bodyPreview": ""},
                    {"id": "unknown"},
                ],
                "@odata.nextLink": next_link,
            },
        )

    transport(remote)
    args = adapter.SearchArguments(integration_id=instance.id, query="subject:invoice")
    page = json.loads((await adapter.search(args, context)).content)
    assert [item.get("bodyPreview") for item in page["messages"]] == [("Preview " * 100)[:255], "", None]
    assert page["messages"][0]["importance"] == "high"
    assert len(requests) == 1
    message_token, cursor = page["messages"][0]["messageToken"], page["nextCursor"]
    assert next_link not in cursor
    message = json.loads(
        (
            await adapter.get_message(
                adapter.MessageArguments(integration_id=instance.id, message_token=message_token),
                context,
            )
        ).content
    )
    assert message["message"]["body"] == "External mail text"
    assert message["contentIsUntrusted"] is True
    continuation = args.model_copy(update={"cursor": cursor})
    assert not (await adapter.search(continuation, context)).is_error
    count = len(requests)
    changed = continuation.model_copy(update={"query": "different"})
    assert (await adapter.search(changed, context)).is_error
    instance.credential_id = uuid4()
    context.bound_options["binding_revision"] = adapter.binding_revision(instance)
    assert (
        await adapter.get_message(
            adapter.MessageArguments(integration_id=instance.id, message_token=message_token),
            context,
        )
    ).is_error
    assert len(requests) == count


@pytest.mark.parametrize(
    "target",
    [
        "https://evil.test/v1.0/me/messages",
        "https://graph.microsoft.com/v1.0/users/another/messages",
        "https://graph.microsoft.com/v1.0/me/messages/id/attachments",
        "http://graph.microsoft.com/v1.0/me/messages",
        "https://user@graph.microsoft.com/v1.0/me/messages",
    ],
)
async def test_graph_continuations_cannot_change_origin_or_resource(transport, target):
    transport(lambda _: pytest.fail("A foreign continuation must not receive the token"))
    with pytest.raises(client.Microsoft365Error, match="staleReference"):
        await client.GraphClient("secret").get("/me/messages", cursor=target)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(302, headers={"Location": "https://evil.test"}, json={}),
        httpx.Response(200, content=b"x" * (client.MAX_RESPONSE_BYTES + 1)),
        httpx.Response(200, json={"value": "invalid"}),
        httpx.Response(403, json={"error": {"message": "private remote message"}}),
    ],
)
async def test_graph_rejects_redirects_oversized_and_invalid_results_without_remote_errors(
    transport, response
):
    requests = []

    def remote(request):
        requests.append(request)
        return response

    transport(remote)
    with pytest.raises(client.Microsoft365Error) as caught:
        await client.GraphClient("secret").page(
            "/me/messages", client.Message, params={}, cursor=None
        )
    assert "private remote message" not in str(caught.value)
    assert len(requests) == 1


async def test_authorization_and_reconnection_use_existing_mail_policy_and_clear_old_binding(
    monkeypatch,
):
    instance = account()
    context = SimpleNamespace(
        integration_settings=IntegrationSettings(instances=[instance]),
        bound_options={
            "integration_id": str(instance.id),
            "binding_revision": adapter.binding_revision(instance),
        },
    )
    args = adapter.SearchArguments(integration_id=instance.id)
    (request,) = await adapter._permission(args, context)
    assert request.capability == "mail.read"
    tools = effective_integration_contributions(context.integration_settings).tools
    assert {tool.name for tool in tools} == {
        "m365_list_accounts",
        "m365_list_folders",
        "m365_search",
        "m365_get_message",
        "m365_download",
        "m365_reply",
        "m365_draft",
        "m365_send",
        "m365_delete",
        "m365_calendar",
        "m365_get_event", "m365_create_event", "m365_update_event", "m365_delete_event",
        "m365_todo", "m365_todo_create", "m365_todo_update", "m365_todo_delete",
        "m365_todo_attach", "m365_todo_download",
    }
    assert all(tool.releases_private_data for tool in tools)
    assert next(tool for tool in tools if tool.name == "m365_reply").actions == (adapter.DRAFTS, adapter._SESSION_FILES_READ)
    settings = UserIntegrationSettings(instances=[instance])
    load = AsyncMock(return_value=settings)
    monkeypatch.setattr(service, "_load_integrations", load)
    replace = AsyncMock()
    save = AsyncMock()
    monkeypatch.setattr(service.Microsoft365Service, "_replace_credential", replace)
    monkeypatch.setattr(service.Microsoft365Service, "_save", save)
    attempt = oauth.LoginAttempt(
        instance.owner_id, "session", instance, oauth.apply_tokens(credential(instance), tokens())
    )
    await service.Microsoft365Service.authorize(AsyncMock(), attempt)
    assert load.call_args.kwargs["expected_instance"] is instance
    assert save.call_args.kwargs["clear_policies"] == instance.id
    assert replace.call_args.kwargs["owner_id"] == instance.owner_id


@pytest.fixture
def tool_context(monkeypatch):
    instance = account()
    instance.config.update(allow_drafts=True, read_calendar=True)
    settings = IntegrationSettings(instances=[instance])
    monkeypatch.setattr(adapter, "access_token", AsyncMock(return_value="access"))
    monkeypatch.setattr(adapter.SettingsStore, "load", AsyncMock(return_value=InstanceLimits()))
    return instance, SimpleNamespace(
        integration_settings=settings,
        bound_options={"integration_id": str(instance.id), "binding_revision": adapter.binding_revision(instance)},
        current_integrations=AsyncMock(return_value=settings),
        prepared_arguments={},
        session=SimpleNamespace(user_id=instance.owner_id, parent_session_id=None),
        run_id=uuid4(), tool_call=SimpleNamespace(id="call"),
        db=AsyncMock(),
    )


async def test_conversation_includes_sent_mail_and_binds_continuation_to_message(transport, tool_context):
    instance, context = tool_context
    requests = []

    def remote(request):
        requests.append(request)
        if request.url.path.endswith("/source"):
            return httpx.Response(200, json={"id": "source", "conversationId": "thread'id", "body": {"contentType": "text", "content": "Received"}})
        if request.url.path.endswith("/sent"):
            return httpx.Response(200, json={"id": "sent", "body": {"contentType": "text", "content": "Our reply"}})
        assert request.url.path == "/v1.0/me/messages"
        assert request.url.params["$filter"] == "conversationId eq 'thread''id'"
        return httpx.Response(200, json={
            "value": [{"id": "sent", "subject": "Re: Request", "bodyPreview": "Our reply", "body": {"contentType": "text", "content": "Our reply"}}],
            "@odata.nextLink": client.GRAPH_BASE + "/me/messages?$skiptoken=next",
        })

    transport(remote)
    args = adapter.MessageArguments(integration_id=instance.id, message_token=adapter._encode(instance, "message", "source"), view="conversation")
    result = json.loads((await adapter.get_message(args, context)).content)
    assert "body" not in result["messages"][0]
    assert result["messages"][0]["bodyPreview"] == "Our reply"
    detail = json.loads((await adapter.get_message(args.model_copy(update={
        "message_token": result["messages"][0]["messageToken"], "view": "message",
    }), context)).content)
    assert detail["message"]["body"] == "Our reply"
    count = len(requests)
    changed = args.model_copy(update={"cursor": result["nextCursor"], "message_token": adapter._encode(instance, "message", "other")})
    assert (await adapter.get_message(changed, context)).is_error
    assert len(requests) == count


async def test_attachment_download_uses_bound_token_and_independent_session_write_permission(
    transport, tool_context, tmp_path,
):
    instance, context = tool_context
    context.session = SimpleNamespace(id=uuid4(), parent_session_id=None)
    storage = SessionStorage(tmp_path / "sessions")
    storage.provision(context.session.id)
    context.root_files = RootFileService(context.session.id, session_storage=storage)
    context.db, context.run_id = AsyncMock(), uuid4()
    context.db.scalar.return_value = None
    requests = []
    metadata = {"@odata.type": "#microsoft.graph.fileAttachment", "id": "file/id", "name": "../untrusted.txt", "size": 4}

    def remote(request):
        requests.append(request)
        assert request.method == "GET"
        if request.url.path.endswith("/$value"):
            return httpx.Response(200, content=b"Mail")
        return httpx.Response(200, json={"value": [metadata]} if request.url.path.endswith("/attachments") else metadata)

    transport(remote)
    listed = json.loads((await adapter.get_message(adapter.MessageArguments(
        integration_id=instance.id, message_token=adapter._encode(instance, "message", "source"), view="attachments",
    ), context)).content)
    args = adapter.DownloadArguments(integration_id=instance.id, attachment_token=listed["attachments"][0]["attachmentToken"], path="attachment.txt")
    permissions = await adapter._permission(args, context)
    assert [request.capability for request in permissions] == ["mail.read", "session.files.write"]
    result = await adapter.download_attachment(args, context)
    assert not result.is_error
    with context.root_files.open_file("session", "attachment.txt") as handle:
        assert handle.read() == b"Mail"
    assert requests[-1].url.raw_path == b"/v1.0/me/messages/source/attachments/file%2Fid/$value"
    other = account()
    context.integration_settings = IntegrationSettings(instances=[other])
    context.bound_options = {"integration_id": str(other.id), "binding_revision": adapter.binding_revision(other)}
    with pytest.raises(client.Microsoft365Error, match="staleReference"):
        await adapter._permission(args.model_copy(update={"integration_id": other.id}), context)


@pytest.mark.parametrize(("attachment_type", "size", "body", "error"), [
    ("referenceAttachment", 1, b"x", "operationUnsupported"),
    ("itemAttachment", 1, b"x", "operationUnsupported"),
    ("fileAttachment", 5, b"x", "attachmentTooLarge"),
    ("fileAttachment", 1, b"12345", "attachmentTooLarge"),
])
async def test_attachment_download_rejects_nonfiles_and_bounds_metadata_and_stream(
    monkeypatch, transport, attachment_type, size, body, error,
):
    monkeypatch.setattr(client, "MAX_ATTACHMENT_BYTES", 4)

    def remote(request):
        if request.url.path.endswith("/$value"):
            assert attachment_type == "fileAttachment" and size <= 4
            return httpx.Response(200, content=body)
        return httpx.Response(200, json={"@odata.type": "#microsoft.graph." + attachment_type, "id": "file", "name": "file", "size": size})

    transport(remote)
    with pytest.raises(client.Microsoft365Error, match=error):
        await client.GraphClient("access").download("message", "file")


@pytest.mark.parametrize(("original", "signature", "expected"), [
    ({"contentType": "text", "content": "From: Sender\nOriginal"}, "", {
        "contentType": "text", "content": "Thank <you>\n\nFrom: Sender\nOriginal",
    }),
    ({"contentType": "html", "content": '<html><body><hr><b>From: Sender</b><p>Original<img src="cid:original"></p></body></html>'}, "", {
        "contentType": "html", "content": '<html><body>Thank &lt;you&gt;<br/><br/><hr/><b>From: Sender</b><p>Original<img src="cid:original"/></p></body></html>',
    }),
    ({"contentType": "text", "content": "From: Sender\nOriginal"}, "<b>Team</b>", {
        "contentType": "html", "content": "<div>Thank &lt;you&gt;</div><br/><b>Team</b><br/><br/>From: Sender<br/>Original",
    }),
])
async def test_reply_preserves_outlooks_original_below_response_and_signature(transport, tool_context, original, signature, expected):
    instance, context = tool_context
    instance.config["signature_html"] = signature
    context.bound_options["binding_revision"] = adapter.binding_revision(instance)
    requests = []

    def remote(request):
        requests.append(request)
        if request.method == "PATCH":
            assert request.url.path == "/v1.0/me/messages/reply"
            assert json.loads(request.content)["body"] == expected
            return httpx.Response(200, json={})
        assert request.method == "POST"
        assert request.url.path == "/v1.0/me/messages/source/createReply"
        assert json.loads(request.content) == {}
        assert "body-content-type" not in request.headers["Prefer"]
        return httpx.Response(201, json={"id": "reply", "isDraft": True, "body": original})

    transport(remote)
    args = adapter.DraftArguments(integration_id=instance.id, message_token=adapter._encode(instance, "message", "source"), body="Thank <you>")
    (permission,) = await adapter._permission(args, context)
    assert permission.capability == "mail.drafts"
    result = json.loads((await adapter.create_reply_draft(args, context)).content)
    assert result["draft"]["isDraft"] is True and result["sent"] is False
    assert len(requests) == 2


@pytest.mark.parametrize("name", ["m365_reply", "m365_draft", "m365_send"])
async def test_mail_tools_execute_without_optional_file_permission(transport, tool_context, monkeypatch, name):
    instance, context = tool_context
    instance.config["allow_send"] = True
    context.session.id = uuid4()
    context.tool_settings = ToolSettings()
    context.tool_settings.private_permissions.policies["session.files.read"] = ToolExecutionPolicy.DISABLED
    context.tool_settings.public_permissions.policies["session.files.read"] = ToolExecutionPolicy.DISABLED
    context.ask_handler = AsyncMock(return_value=PermissionDecision.ALLOW)
    monkeypatch.setattr(
        "mato_loop_backend.tools.access.profile_tool_settings",
        AsyncMock(return_value=context.tool_settings),
    )
    arguments = {"integration_id": str(instance.id), "body": "Test"}
    if name == "m365_reply":
        arguments["message_token"] = adapter._encode(instance, "message", "source")
    else:
        arguments["to"] = ["recipient@example.com"]
    call = context.tool_call = ToolCall("test", name, arguments)
    registry = ToolRegistry([], policy_resolver=tool_execution_policy, allow_handler=AsyncMock(return_value=True))
    effective = registry.effective(context.integration_settings, tool_settings=context.tool_settings)
    definition = effective.tools[name].definition
    primary = adapter.SEND if name == "m365_send" else adapter.DRAFTS
    assert definition.actions_satisfied({primary.capability})
    assert not definition.actions_satisfied({"session.files.read"})

    async def reserved_send(**arguments):
        await arguments["client"].send(arguments["raw"])
        return MailSendResult(sent_copy_confirmed=None)

    monkeypatch.setattr(graph_mail, "send_once", reserved_send)
    transport(lambda request: httpx.Response(202) if name == "m365_send" else httpx.Response(
        200 if request.method == "PATCH" else 201,
        json={"id": "draft", "isDraft": True, "body": {"contentType": "text", "content": "Original"}},
    ))
    result = await effective.execute(call, context)
    assert not result.is_error


async def test_new_draft_previews_recipients_and_creates_only_an_outlook_draft(transport, tool_context):
    instance, context = tool_context
    requests = []
    args = adapter.NewDraftArguments(
        integration_id=instance.id, to=["recipient@example.com"],
        subject="Test draft", body="Test message",
    )
    (permission,) = await adapter._permission(args, context)
    assert permission.capability == "mail.drafts"
    assert [(item.label, item.value) for item in permission.display_items[:3]] == [
        ("Empfänger", ("recipient@example.com",)), ("Betreff", "Test draft"), ("Nachricht", "Test message"),
    ]

    def remote(request):
        requests.append(request)
        assert request.method == "POST"
        assert request.url.path == "/v1.0/me/messages"
        payload = json.loads(request.content)
        assert payload == {
            "toRecipients": [{"emailAddress": {"address": "recipient@example.com"}}],
            "subject": "Test draft", "body": {"contentType": "text", "content": "Test message"},
        }
        return httpx.Response(201, json={"id": "new-draft", "isDraft": True})

    transport(remote)
    result = json.loads((await adapter.create_draft(args, context)).content)
    assert result["draft"]["isDraft"] is True and result["sent"] is False
    assert len(requests) == 1


@pytest.mark.parametrize("changes", [
    {"to": []}, {"to": ["not-an-address"]}, {"to": ["a@example.com\nBcc: other@example.com"]},
    {"subject": "Test\nOther header"},
])
def test_new_draft_rejects_missing_or_invalid_recipients_and_subjects(changes):
    with pytest.raises(ValueError):
        adapter.NewDraftArguments.model_validate({
            "integration_id": uuid4(), "to": ["recipient@example.com"],
            "subject": "Test", "body": "Message", **changes,
        })


@pytest.mark.parametrize("operation", ["reply", "reply_update", "new"])
async def test_uncertain_draft_creation_is_not_replayed(transport, operation):
    requests = []

    def remote(request):
        requests.append(request)
        if operation == "reply_update" and request.method == "POST":
            return httpx.Response(201, json={"id": "reply", "isDraft": True, "body": {"contentType": "text", "content": "Original"}})
        raise httpx.ReadTimeout("Lost response")

    transport(remote)
    with pytest.raises(client.Microsoft365Error, match="outcomeUnknown"):
        graph = client.GraphClient("access")
        if operation.startswith("reply"):
            await graph.reply_draft("source", "Reply")
        else:
            await graph.new_draft(["recipient@example.com"], "Test", "Message")
    assert len(requests) == (2 if operation == "reply_update" else 1)


async def test_new_draft_requires_confirmation_that_it_is_a_draft(transport):
    transport(lambda _request: httpx.Response(201, json={"id": "not-a-draft", "isDraft": False}))
    with pytest.raises(client.Microsoft365Error, match="outcomeUnknown"):
        await client.GraphClient("access").new_draft(["recipient@example.com"], "Test", "Message")


async def test_calendar_view_uses_offset_period_and_query_bound_paging(transport, organizer_context):
    instance, context = organizer_context
    requests = []

    def remote(request):
        requests.append(request)
        assert request.method == "GET"
        assert 'outlook.timezone="UTC"' in request.headers["Prefer"]
        if request.url.path == "/v1.0/me/calendars":
            return httpx.Response(200, json={"value": [{"id": "primary", "name": "Calendar", "isDefaultCalendar": True}]})
        assert request.url.path == "/v1.0/me/calendar/calendarView"
        if "$skiptoken" in request.url.params:
            return httpx.Response(200, json={"value": []})
        assert request.url.params["startDateTime"] == "2026-11-11T00:00:00+01:00"
        assert {"bodyPreview", "importance", "organizer", "showAs"} <= set(request.url.params["$select"].split(","))
        return httpx.Response(200, json={"value": [{
            "id": "occurrence", "subject": "Standup",
            "start": {"dateTime": "2026-11-11T09:00:00", "timeZone": "UTC"},
            "end": {"dateTime": "2026-11-11T09:30:00", "timeZone": "UTC"},
            "body": {"contentType": "text", "content": "Agenda"},
            "bodyPreview": "Agenda " * 100, "importance": "high", "showAs": "busy",
            "organizer": {"emailAddress": {"name": "Organizer", "address": "organizer@example.com"}},
        }], "@odata.nextLink": client.GRAPH_BASE + "/me/calendar/calendarView?$skiptoken=next"})

    transport(remote)
    listed = await adapter.read_calendar(adapter.CalendarArguments(integration_id=instance.id, operation="list_calendars"), context)
    assert json.loads(listed.content)["calendars"][0]["id"] == "primary"
    args = adapter.CalendarArguments(integration_id=instance.id, operation="list_events", start="2026-11-11T00:00:00+01:00", end="2026-11-12T00:00:00+01:00")
    (permission,) = await adapter._permission(args, context)
    assert permission.capability == "calendar.read"
    page = json.loads((await adapter.read_calendar(args, context)).content)
    assert not {"body", "id", "attendees", "recurrence", "changeKey"} & page["events"][0].keys()
    assert page["events"][0]["bodyPreview"] == ("Agenda " * 100)[:255]
    assert {key: page["events"][0][key] for key in ("importance", "showAs", "organizer")} == {
        "importance": "high", "showAs": "busy",
        "organizer": {"emailAddress": {"name": "Organizer", "address": "organizer@example.com"}},
    }
    assert len(requests) == 2
    transport(lambda _request: httpx.Response(200, json={
        **organizer_resource("event"), "id": "occurrence", "body": {"contentType": "text", "content": "Agenda"},
    }))
    detail = json.loads((await organizer.get_event(organizer.EventTarget(
        integration_id=instance.id, event_token=page["events"][0]["eventToken"],
    ), context)).content)
    assert detail["event"]["body"] == "Agenda"
    transport(remote)
    continued = args.model_copy(update={"cursor": page["nextCursor"]})
    assert not (await adapter.read_calendar(continued, context)).is_error
    count = len(requests)
    assert (await adapter.read_calendar(continued.model_copy(update={"calendar_id": "other"}), context)).is_error
    assert len(requests) == count


@pytest.mark.parametrize(("start", "end"), [
    ("2026-11-11T00:00:00", "2026-11-12T00:00:00+00:00"),
    ("2026-11-11T00:00:00+00:00", "2026-11-10T00:00:00+00:00"),
    ("2026-01-01T00:00:00+00:00", "2028-01-01T00:00:00+00:00"),
])
def test_calendar_requires_bounded_timezone_aware_period(start, end):
    with pytest.raises(ValueError):
        adapter.CalendarArguments(integration_id=uuid4(), operation="list_events", start=start, end=end)


@pytest.mark.parametrize("scope", ["Mail.Read", "Mail.ReadWrite"])
def test_enabled_features_require_their_delegated_scopes(scope):
    instance = account()
    instance.config.update(allow_drafts=True, read_calendar=True)
    with pytest.raises(client.Microsoft365Error, match="invalidResponse"):
        oauth.apply_tokens(credential(instance), tokens(scope=scope))


@pytest.mark.parametrize("model,extra", [
    (adapter.DraftArguments, {"message_token": "unread", "body": "Reply"}),
    (adapter.NewDraftArguments, {"to": ["recipient@example.com"], "subject": "Test", "body": "Message"}),
    (adapter.CalendarArguments, {"operation": "list_calendars"}),
])
async def test_read_only_connection_cannot_authorize_optional_features(model, extra):
    instance = account()
    context = SimpleNamespace(
        integration_settings=IntegrationSettings(instances=[instance]),
        bound_options={"integration_id": str(instance.id), "binding_revision": adapter.binding_revision(instance)},
    )
    with pytest.raises(client.Microsoft365Error, match="featureDisabled"):
        await adapter._permission(model(integration_id=instance.id, **extra), context)


@pytest.mark.parametrize("access_change", [True, False])
async def test_access_changes_discard_tokens_but_signature_changes_preserve_them(monkeypatch, access_change):
    instance = account("browser")
    stored = oauth.apply_tokens(credential(instance), tokens())
    record = Credential(
        id=instance.credential_id, user_id=instance.owner_id, kind=oauth.CREDENTIAL_KIND,
        encrypted_payload=CredentialCipher().encrypt(stored.model_dump(mode="json")),
    )
    monkeypatch.setattr(service, "_load_integrations", AsyncMock(return_value=UserIntegrationSettings(instances=[instance])))
    monkeypatch.setattr(service, "load_credential", AsyncMock(return_value=record))
    update = AsyncMock()
    monkeypatch.setattr(service.Microsoft365Service, "_update", update)
    config = stored.config.model_copy(update={"allow_drafts": True, "read_calendar": True} if access_change else {"signature_html": "<b>Team</b>"})
    await service.Microsoft365Service.save(
        AsyncMock(), owner_id=instance.owner_id, integration_id=instance.id,
        name=instance.name, config=config, enabled=True, client_secret=None,
    )
    payload = oauth.OAuthCredential.model_validate(update.call_args.kwargs["payload"])
    assert payload.client_secret == "client-secret"
    assert payload.tokens == (None if access_change else stored.tokens)
    assert update.call_args.kwargs["connection_changed"] is access_change


@pytest.fixture
def organizer_context(tool_context, monkeypatch):
    instance, context = tool_context
    instance.config.update(write_calendar=True, delete_calendar=True, read_tasks=True, write_tasks=True, delete_tasks=True)
    context.bound_options["binding_revision"] = adapter.binding_revision(instance)
    monkeypatch.setattr(organizer, "access_token", AsyncMock(return_value="access"))
    return instance, context


def todo_token(instance, kind="task"):
    path = "/me/todo/lists/list" + ("/tasks/task" if kind != "list" else "")
    if kind in {"checklist", "link", "attachment"}:
        path += "/" + {"checklist": "checklistItems", "link": "linkedResources", "attachment": "attachments"}[kind] + "/item"
    return adapter._encode(instance, "todo", organizer.TodoReference(kind=kind, path=path).model_dump_json())


def organizer_resource(kind):
    return {
        "event": {
            "id": "event", "subject": "Appointment", "changeKey": "one",
            "start": {"dateTime": "2026-11-12T10:00:00", "timeZone": "UTC"},
            "end": {"dateTime": "2026-11-12T11:00:00", "timeZone": "UTC"},
            "body": {"contentType": "text", "content": "Agenda"},
        },
        "list": {"id": "list", "displayName": "Work"},
        "task": {"id": "task", "title": "Task", "lastModifiedDateTime": "2026-11-12T10:00:00Z"},
        "checklist": {"id": "item", "displayName": "Step", "isChecked": False},
        "link": {"id": "item", "displayName": "Source", "webUrl": "https://example.com/"},
        "attachment": {"@odata.type": "#microsoft.graph.taskFileAttachment", "id": "item", "name": "file.txt", "size": 4},
    }[kind]


@pytest.mark.parametrize("scenario", [
    "m365_get_event", "m365_create_event", "m365_update_event", "m365_delete_event",
    "m365_todo", "m365_todo_create:list", "m365_todo_create:task",
    "m365_todo_create:checklist", "m365_todo_create:link",
    "m365_todo_update:list", "m365_todo_update:task",
    "m365_todo_update:checklist", "m365_todo_update:link",
    "m365_todo_delete:list", "m365_todo_delete:task", "m365_todo_delete:checklist",
    "m365_todo_delete:link", "m365_todo_delete:attachment",
    "m365_todo_attach", "m365_todo_download",
])
async def test_organizer_tools_execute_through_authorized_registry(transport, organizer_context, monkeypatch, tmp_path, scenario):
    instance, context = organizer_context
    name, _, kind = scenario.partition(":")
    kind = kind or ("event" if "event" in name else "attachment" if name.endswith("download") else "task")
    context.session.id = uuid4()
    context.tool_settings = ToolSettings()
    context.ask_handler = AsyncMock(return_value=PermissionDecision.ALLOW)
    monkeypatch.setattr("mato_loop_backend.tools.access.profile_tool_settings", AsyncMock(return_value=context.tool_settings))
    if name in {"m365_todo_attach", "m365_todo_download"}:
        storage = SessionStorage(tmp_path / "sessions")
        storage.provision(context.session.id)
        context.root_files = RootFileService(context.session.id, session_storage=storage)
        file_data = b"F" * (2 * 1024 * 1024) if name == "m365_todo_attach" else b"File"
        context.root_files.create_file("session", "file.txt", BytesIO(file_data), max_bytes=len(file_data))
    else:
        for policy in (context.tool_settings.private_permissions, context.tool_settings.public_permissions):
            policy.policies.update({"session.files.read": ToolExecutionPolicy.DISABLED, "session.files.write": ToolExecutionPolicy.DISABLED})
    arguments = {"integration_id": str(instance.id)}
    if kind == "event":
        if name != "m365_create_event":
            arguments["event_token"] = adapter._encode(instance, "event", "event")
        if name in {"m365_create_event", "m365_update_event"}:
            arguments["fields"] = {"subject": "New", **({
                "start": "2026-11-12T10:00:00+01:00", "end": "2026-11-12T11:00:00+01:00",
                "time_zone": "Europe/Berlin", "attendees": ["person@example.com"],
            } if name == "m365_create_event" else {})}
    elif name == "m365_todo":
        arguments["operation"] = "list_lists"
    else:
        fields = {
            "list": {"displayName": "New"}, "task": {"title": "New", "status": "completed", "body": "<Notes>", "due": "2026-11-15T12:00:00+01:00"},
            "checklist": {"displayName": "New", "isChecked": True},
            "link": {"displayName": "New", "webUrl": "https://example.com/item"},
        }
        if name == "m365_todo_create":
            arguments.update(kind=kind, fields=fields[kind])
            if kind != "list":
                arguments["parent_token"] = todo_token(instance, "list" if kind == "task" else "task")
        else:
            arguments["resource_token"] = todo_token(instance, kind if name != "m365_todo_attach" else "task")
            if name == "m365_todo_update":
                arguments.update(kind=kind, fields=fields[kind])
            if name in {"m365_todo_attach", "m365_todo_download"}:
                arguments["path"] = "file.txt" if name.endswith("attach") else "download.txt"

    effects = []
    download_data = b"F" * (2 * 1024 * 1024) if name == "m365_todo_download" else b"File"

    def remote(request):
        path = request.url.path
        if request.method == "GET":
            if path.endswith("/$value"):
                return httpx.Response(200, content=download_data)
            if path == "/v1.0/me/todo/lists":
                return httpx.Response(200, json={"value": [organizer_resource("list")]})
            if path.endswith("/attachments"):
                return httpx.Response(200, json={"value": [
                    {**organizer_resource("attachment"), "size": len(download_data)},
                ] if kind == "attachment" else []})
            selected = (
                "event" if "/events/" in path else "attachment" if "/attachments/" in path
                else "checklist" if "/checklistItems/" in path else "link" if "/linkedResources/" in path
                else "task" if "/tasks/" in path else "list"
            )
            return httpx.Response(200, json={
                **organizer_resource(selected),
                **({"size": len(download_data), "contentBytes": base64.b64encode(download_data).decode("ascii")}
                   if selected == "attachment" else {}),
            })
        payload = json.loads(request.content) if request.content else None
        effects.append((request.method, path, payload))
        if request.method == "DELETE":
            return httpx.Response(204)
        selected = "attachment" if name == "m365_todo_attach" else kind
        return httpx.Response(201 if request.method == "POST" else 200, json={**organizer_resource(selected), **payload})

    transport(remote)
    call = context.tool_call = ToolCall("organizer", name, arguments)
    registry = ToolRegistry([], policy_resolver=tool_execution_policy, allow_handler=AsyncMock(return_value=True))
    result = await registry.effective(context.integration_settings, tool_settings=context.tool_settings).execute(call, context)
    assert not result.is_error, result.content
    if name == "m365_todo_download":
        assert context.root_files.read_bytes("session", "download.txt", limit=len(download_data)) == download_data
    if name == "m365_create_event":
        assert effects[0][2]["attendees"] == [{"emailAddress": {"address": "person@example.com"}, "type": "required"}]
    if kind == "task" and name in {"m365_todo_create", "m365_todo_update"}:
        assert effects[0][2] == {
            "title": "New", "status": "completed", "body": {"contentType": "html", "content": "<div>&lt;Notes&gt;</div>"},
            "dueDateTime": {"dateTime": "2026-11-15T11:00:00", "timeZone": "UTC"},
        }


@pytest.mark.parametrize(("flag", "model", "extra"), [
    ("write_calendar", organizer.EventCreate, {"fields": {"subject": "New", "start": "2026-11-12T10:00:00Z", "end": "2026-11-12T11:00:00Z"}}),
    ("delete_calendar", organizer.EventTarget, {"event_token": "unread"}),
    ("read_tasks", organizer.TodoRead, {"operation": "list_lists"}),
    ("write_tasks", organizer.TodoCreate, {"kind": "list", "fields": {"displayName": "New"}}),
    ("delete_tasks", organizer.TodoTarget, {"resource_token": "unread"}),
])
async def test_organizer_options_are_independent_hard_gates(transport, organizer_context, flag, model, extra):
    instance, context = organizer_context
    instance.config[flag] = False
    context.bound_options["binding_revision"] = adapter.binding_revision(instance)
    transport(lambda _request: pytest.fail("A disabled feature must not access Graph"))
    with pytest.raises(client.Microsoft365Error, match="featureDisabled"):
        await organizer.permission(model(integration_id=instance.id, **extra), context)


@pytest.mark.parametrize("kind", ["event", "task"])
async def test_organizer_rechecks_approved_resource_before_mutation(transport, organizer_context, kind):
    instance, context = organizer_context
    version = "one"
    transport(lambda _request: httpx.Response(200, json={
        **organizer_resource(kind), **({"changeKey": version} if kind == "event" else {"lastModifiedDateTime": version}),
    }) if _request.method == "GET" else pytest.fail("Changed entries must not be modified"))
    args = organizer.EventUpdate(
        integration_id=instance.id, event_token=adapter._encode(instance, "event", "event"), fields={"subject": "Changed"},
    ) if kind == "event" else organizer.TodoUpdate(
        integration_id=instance.id, kind="task", resource_token=todo_token(instance), fields={"status": "completed"},
    )
    await organizer.permission(args, context)
    version = "two"
    result = await (organizer.write_event if kind == "event" else organizer.write_todo)(args, context)
    assert result.is_error and "staleReference" in result.content


async def test_sparse_todo_due_update_accepts_equal_graph_start_with_fractional_precision(transport, organizer_context):
    instance, context = organizer_context
    task = {
        **organizer_resource("task"),
        "startDateTime": {"dateTime": "2026-11-12T10:00:00.0000000", "timeZone": "UTC"},
    }

    def remote(request):
        return httpx.Response(200, json={**task, **(json.loads(request.content) if request.method == "PATCH" else {})})

    transport(remote)
    args = organizer.TodoUpdate(
        integration_id=instance.id, kind="task", resource_token=todo_token(instance),
        fields={"due": "2026-11-12T11:00:00+01:00"},
    )
    await organizer.permission(args, context)
    result = await organizer.write_todo(args, context)
    assert not result.is_error, result.content


@pytest.mark.parametrize(("remote", "fields", "hint"), [
    ({"type": "seriesMaster"}, None, "entire recurring series"),
    ({"isOnlineMeeting": True}, {"body": "Changed"}, "online-meeting body"),
])
async def test_unsafe_calendar_changes_fail_before_effects(transport, organizer_context, remote, fields, hint):
    instance, context = organizer_context
    transport(lambda _request: httpx.Response(200, json={**organizer_resource("event"), **remote}))
    with pytest.raises(ValueError, match=hint):
        await organizer.permission((organizer.EventTarget if fields is None else organizer.EventUpdate)(
            integration_id=instance.id, event_token=adapter._encode(instance, "event", "event"), **({"fields": fields} if fields else {}),
        ), context)


async def test_todo_tokens_and_pagination_remain_owner_and_query_bound(transport, organizer_context):
    instance, context = organizer_context
    notes = "Detailed notes " * 1000

    def remote(request):
        if request.url.path.endswith("/tasks/task-0"):
            return httpx.Response(200, json={
                **organizer_resource("task"), "id": "task-0", "body": {"contentType": "text", "content": notes},
            })
        assert request.url.params.get("$filter") == "status ne 'completed'"
        assert "$select" not in request.url.params
        return httpx.Response(200, json={
            "value": [{
                **organizer_resource("task"), "id": f"task-{index}", "title": f"Task {index:02} " + "x" * 55,
                "dueDateTime": {"dateTime": "2026-11-12T23:00:00.0000000", "timeZone": "UTC"},
                "body": {"contentType": "text", "content": notes},
            } for index in range(60)],
            "@odata.nextLink": client.GRAPH_BASE + "/me/todo/lists/list/tasks?$skiptoken=next",
        })

    transport(remote)
    args = organizer.TodoRead(integration_id=instance.id, operation="list_tasks", resource_token=todo_token(instance, "list"), limit=100)
    content = (await organizer.read_todo(args, context)).content
    page = json.loads(content)
    projected = model_tool_references("m365_todo", content, uuid4())
    assert len(json.loads(projected)["resources"]) == 60 and len(projected) <= 16_000
    assert not {"id", "body", "recurrence", "@odata.etag", "lastModifiedDateTime"} & page["resources"][0].keys()
    assert json.loads(projected)["resources"][0]["hasNotes"] is True
    detail = json.loads((await organizer.read_todo(organizer.TodoRead(
        integration_id=instance.id, operation="get", resource_token=page["resources"][0]["resourceToken"],
    ), context)).content)
    assert detail["resource"]["body"] == notes
    transport(lambda _request: pytest.fail("Wrong-scope references must fail before network access"))
    result = await organizer.read_todo(args.model_copy(update={"cursor": page["nextCursor"], "limit": 1}), context)
    assert result.is_error and "staleReference" in result.content
    with pytest.raises(client.Microsoft365Error, match="staleReference"):
        organizer._todo_reference(todo_token(account()), instance)


@pytest.mark.parametrize(("fields", "expected"), [
    ({}, {"hasNotes": None}),
    ({"body": None, "recurrence": None}, {"hasNotes": None, "isRecurring": False}),
    ({"body": {"contentType": "text", "content": " \n "}}, {"hasNotes": False}),
    ({"body": {"contentType": "text", "content": "Notes"}}, {"hasNotes": True}),
    ({"body": {"contentType": "html", "content": "<div><p>&nbsp;</p></div>"}}, {"hasNotes": False}),
    ({"body": {"contentType": "html", "content": '<div><img src="cid:image"></div>'}}, {"hasNotes": True}),
    ({"recurrence": {
        "pattern": {"type": "daily", "interval": 1},
        "range": {"type": "noEnd", "startDate": "2026-11-14"},
    }}, {"hasNotes": None, "isRecurring": True}),
])
def test_task_summary_distinguishes_empty_notes_from_unknown_and_media(fields, expected):
    summary = organizer._resource(
        "task", "/me/todo/lists/list/tasks/task",
        organizer.Task.model_validate({**organizer_resource("task"), **fields}),
        account(), summary=True,
    )
    assert {key: value for key, value in summary.items() if key in {"hasNotes", "isRecurring"}} == expected


@pytest.mark.parametrize(("option", "required"), [
    ("write_calendar", "Calendars.ReadWrite"), ("delete_calendar", "Calendars.ReadWrite"),
    ("read_tasks", "Tasks.Read"), ("write_tasks", "Tasks.ReadWrite"), ("delete_tasks", "Tasks.ReadWrite"),
])
def test_organizer_oauth_requires_enabled_scopes(option, required):
    instance = account()
    instance.config[option] = True
    payload = credential(instance)
    with pytest.raises(client.Microsoft365Error, match="invalidResponse"):
        oauth.apply_tokens(payload, tokens(scope="Mail.Read"))
    assert required in payload.config.scopes
    assert oauth.apply_tokens(payload, tokens(scope="Mail.Read " + required)).tokens is not None


@pytest.mark.parametrize(("model", "values"), [
    (organizer.EventFields, {"start": "2026-11-12T11:00:00Z", "end": "2026-11-12T10:00:00Z"}),
    (organizer.EventFields, {"time_zone": "Not/AZone"}),
    (organizer.EventFields, {"is_all_day": True, "start": "2026-11-12T11:00:00Z", "end": "2026-11-13T11:00:00Z"}),
    (organizer.TaskFields, {"start": "2026-11-13T10:00:00Z", "due": "2026-11-12T10:00:00Z"}),
    (organizer.RecurrencePattern, {"type": "weekly"}),
    (organizer.RecurrenceRange, {"type": "numbered", "startDate": "2026-11-12"}),
    (organizer.LinkFields, {"webUrl": "https://user:secret@example.com/"}),
])
def test_organizer_rejects_invalid_fields(model, values):
    with pytest.raises(ValueError):
        model.model_validate(values)


@pytest.mark.parametrize("unsafe_url", [False, True])
async def test_todo_large_attachments_use_scoped_authenticated_uploads(transport, organizer_context, tmp_path, unsafe_url):
    instance, context = organizer_context
    context.session.id = uuid4()
    storage = SessionStorage(tmp_path / "sessions")
    storage.provision(context.session.id)
    context.root_files = RootFileService(context.session.id, session_storage=storage)
    data = b"x" * (5 * 1024 * 1024)
    context.root_files.create_file("session", "large.bin", BytesIO(data), max_bytes=len(data))
    uploaded = bytearray()

    def remote(request):
        if request.method == "GET":
            return httpx.Response(200, json={"value": []} if request.url.path.endswith("/attachments") else organizer_resource("task"))
        if request.url.path.endswith("/createUploadSession"):
            return httpx.Response(200, json={
                "uploadUrl": ("https://attacker.example" if unsafe_url else client.GRAPH_ORIGIN)
                + "/v1.0/users/owner/todo/lists/list=/tasks/task%2Fid=/attachmentSessions/upload",
            })
        assert request.method == "PUT" and request.url.path.endswith("/attachmentSessions/upload/content")
        assert request.headers["Authorization"] == "Bearer access"
        uploaded.extend(request.content)
        return httpx.Response(201) if len(uploaded) == len(data) else httpx.Response(200, json={"nextExpectedRanges": [str(len(uploaded))]})

    transport(remote)
    token = adapter._encode(instance, "todo", organizer.TodoReference(
        kind="task", path="/me/todo/lists/list%3D/tasks/task%2Fid%3D",
    ).model_dump_json())
    args = organizer.TodoAttach(integration_id=instance.id, resource_token=token, path="large.bin")
    await organizer.permission(args, context)
    result = await organizer.attach_todo(args, context)
    assert ("outcomeUnknown" in result.content and not uploaded) if unsafe_url else (not result.is_error and uploaded == data)


async def test_account_discovery_explains_the_appended_signature(tool_context):
    instance, context = tool_context
    instance.config["signature_html"] = "<p>Mit freundlichen Grüßen</p><p>Example Team</p>"
    context.bound_options["integration_ids"] = (str(instance.id),)
    result = json.loads((await adapter.list_accounts(adapter.Arguments(), context)).content)
    assert result["accounts"][0]["automaticSignature"]["text"] == "Mit freundlichen Grüßen\nExample Team"


async def test_stale_login_cannot_persist_credentials(monkeypatch):
    instance = account()
    attempt = oauth.LoginAttempt(instance.owner_id, "session", instance, credential(instance))
    monkeypatch.setattr(
        service,
        "_load_integrations",
        AsyncMock(side_effect=IntegrationSettingsError("integration.changed")),
    )
    replace = AsyncMock()
    monkeypatch.setattr(service.Microsoft365Service, "_replace_credential", replace)
    with pytest.raises(IntegrationSettingsError, match="integration.changed"):
        await service.Microsoft365Service.authorize(AsyncMock(), attempt)
    replace.assert_not_awaited()


async def test_login_completion_reloads_user_expired_by_rollback(monkeypatch):
    instance = account()
    user = UserAccount(id=instance.owner_id)
    attempt = oauth.LoginAttempt(user.id, "session", instance, credential(instance))
    manager = oauth.LoginManager()
    manager.attempts[attempt.id] = attempt
    monkeypatch.setattr(routes, "oauth_logins", manager)
    monkeypatch.setattr(routes, "_session_hash", lambda _request: "session")
    monkeypatch.setattr(manager, "poll", AsyncMock(return_value=True))
    monkeypatch.setattr(routes.SettingsStore, "load", AsyncMock(return_value=IntegrationSettings()))
    monkeypatch.setattr(service.Microsoft365Service, "authorize", AsyncMock())
    db = AsyncMock()

    async def rollback():
        user.__dict__.pop("id", None)
        user._sa_instance_state.expired_attributes.add("id")

    async def refresh(_user):
        user.__dict__["id"] = instance.owner_id
        user._sa_instance_state.expired_attributes.discard("id")

    db.rollback.side_effect = rollback
    db.refresh.side_effect = refresh
    result = await routes.finish_login(attempt.id, None, Response(), db, user)
    assert result["status"] == "authorized"


def test_browser_callback_accepts_form_posts_without_exposing_code(monkeypatch):
    instance = account("browser")
    attempt = oauth.LoginAttempt(uuid4(), "session", instance, credential(instance), state="state")
    manager = oauth.LoginManager()
    manager.attempts[attempt.id] = attempt
    monkeypatch.setattr(routes, "oauth_logins", manager)
    app = FastAPI()
    app.include_router(routes.callback_router, prefix="/api")
    with TestClient(app) as browser:
        response = browser.post(
            oauth.CALLBACK_PATH, data={"state": "state", "code": "private-code"}
        )
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert "private-code" not in response.text
        assert (
            browser.post(oauth.CALLBACK_PATH, data={"state": "state", "code": "replay"}).status_code
            == 400
        )
    assert attempt.code == "private-code"


async def test_global_microsoft365_creation_is_rejected(monkeypatch):
    from mato_loop_backend.api.integration_access import _load_scoped_integrations

    monkeypatch.setattr(routes.SettingsStore, "load", AsyncMock(return_value=IntegrationSettings()))
    with pytest.raises(ApiError, match="personalOnly"):
        await _load_scoped_integrations(
            AsyncMock(),
            "instance",
            SimpleNamespace(is_admin=True),
            adapter_type="microsoft_graph",
        )


def test_expired_login_is_removed():
    instance = account()
    attempt = oauth.LoginAttempt(
        uuid4(), "session", instance, credential(instance), expires_at=time.monotonic() - 1
    )
    manager = oauth.LoginManager()
    manager.attempts[attempt.id] = attempt
    with pytest.raises(client.Microsoft365Error, match="loginExpired"):
        manager.get(attempt.id, attempt.user_id, attempt.session_hash)
    assert not manager.attempts
