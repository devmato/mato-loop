"""Backend boundaries for owner-authored computer MCP connections."""

import asyncio
import base64
import hashlib
import json
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from PIL import Image
from pydantic import ValidationError
from sqlalchemy import select

import mato_loop_backend.execution.artifacts as artifacts
import mato_loop_backend.integrations.mcp.service as mcp_service
import mato_loop_backend.integrations.mcp.stdio as runtime
import mato_loop_backend.integrations.mcp.stdio_service as service
from mato_loop_backend.api.errors import ApiError
from mato_loop_backend.api.routes import integrations as generic_routes
from mato_loop_backend.api.routes import mcp as routes
from mato_loop_backend.execution.client import ExecutionClientError
from mato_loop_backend.integrations.mcp.adapter import McpCatalogTool, mcp_resource_key
from mato_loop_backend.integrations.mcp.client import McpClientError
from mato_loop_backend.integrations.registry import (
    effective_execution_integration_settings,
    effective_integration_contributions,
)
from mato_loop_backend.models import ExecutionAgent
from mato_loop_backend.settings import (
    IntegrationInstance,
    IntegrationSettings,
    SettingsStore,
    ToolSettings,
    UserIntegrationSettings,
)
from mato_loop_backend.settings.models import IntegrationPermissions

TOOL = {"name": "lookup", "input_schema": {"type": "object"}}
CONFIG = {"executable": "/usr/bin/server", "workingDirectory": "/work", "tools": [TOOL]}


def instance():
    return IntegrationInstance(
        name="Local",
        integration_type="mcp",
        adapter_type="mcp_stdio",
        execution_agent_id=uuid4(),
        config=CONFIG,
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"executable": "server"},
        {"workingDirectory": "../work"},
        {"args": "--unsafe"},
        {"args": ["bad\0arg"]},
        {"environment": {"BAD=KEY": "x"}},
        {"environment": {"KEY": "bad\0value"}},
        {"idleTimeoutSeconds": 0},
        {"tools": [TOOL, TOOL]},
    ],
)
def test_config_rejects_unsafe_or_invalid_binding(changes):
    with pytest.raises(ValidationError):
        runtime.McpStdioConfig.model_validate({**CONFIG, **changes})


def test_stdio_uses_existing_gate_and_allows_multiple_connections_per_computer():
    first = instance()
    second = first.model_copy(update={"id": uuid4()})
    settings = IntegrationSettings(instances=[first, second])
    assert settings.availability_for("mcp_stdio") == settings.availability_for(
        "mcp_streamable_http"
    )
    assert len(UserIntegrationSettings(instances=[first, second]).instances) == 2


def test_catalog_only_offered_for_selected_computer_and_supported_operations():
    local = instance()
    settings = IntegrationSettings(
        instances=[local], adapter_availability={"mcp_streamable_http": "full"}
    )

    def offered(agent_id, operations):
        return effective_integration_contributions(
            effective_execution_integration_settings(settings, agent_id, operations)
        ).tools

    operations = {"integration.mcp.call", "cache.export"}
    contribution = offered(local.execution_agent_id, operations)[0]
    assert contribution.actions == (runtime.MCP_ACTION,)
    assert (
        contribution.authorization_factory(
            {}, SimpleNamespace(bound_options={"binding_revision": "{}"})
        )[0].capability
        == "mcp.call"
    )
    assert offered(uuid4(), operations) == ()
    assert offered(local.execution_agent_id, {"integration.mcp.call"}) == ()


def test_configured_timeout_is_offered_before_the_first_call():
    local = instance()
    local.config = {**local.config, "idleTimeoutSeconds": 73}
    assert (
        "process idle timeout is 73 seconds"
        in runtime.MCP_STDIO_ADAPTER.contributions(local).tools[0].description
    )


async def test_catalog_transfer_uses_its_own_32_mib_read_bound(monkeypatch):
    data = b"[]"
    limit = 32 * 1024 * 1024
    digest = hashlib.sha256(data).hexdigest()
    descriptor = artifacts.ArtifactDescriptor(
        artifactId="catalog",
        backendId=str(uuid4()),
        sessionId=str(uuid4()),
        path="mcp/catalog.json",
        filename="catalog.json",
        size=len(data),
        sha256=digest,
        contentType="application/json",
    )
    stream = Mock(wraps=BytesIO(data))
    ticket = SimpleNamespace(content=stream, actual_size=len(data), actual_sha256=digest)
    store = Mock()
    store.create_upload.return_value = (
        SimpleNamespace(ticket_id=uuid4(), token="test-token"),
        ticket,
    )
    monkeypatch.setattr(artifacts, "transfer_tickets", store)
    monkeypatch.setattr(
        artifacts.execution_agent_connections,
        "transfer_url",
        lambda *_args: "https://example.test/transfer",
    )
    client = SimpleNamespace(
        agent_id=uuid4(),
        request=AsyncMock(
            return_value=artifacts.CacheExportResult(size=len(data), sha256=digest)
        ),
    )
    await artifacts.export_artifact(client, descriptor, limit=limit)
    stream.read.assert_called_once_with(limit + 1)


async def test_runtime_environment_is_owner_scoped_and_not_in_catalog(monkeypatch):
    local = instance()
    local.owner_id, local.credential_id = uuid4(), uuid4()
    load = AsyncMock(return_value=SimpleNamespace(kind=runtime.STDIO_CREDENTIAL_KIND))
    monkeypatch.setattr(runtime, "load_credential", load)
    monkeypatch.setattr(
        runtime, "read_credential", lambda _credential: {"environment": {"TOKEN": "test-value"}}
    )
    wire = await runtime.runtime_arguments(None, local, uuid4())
    assert wire["environment"] == {"TOKEN": "test-value"}
    load.assert_awaited_once_with(
        None, local.credential_id, owner_id=local.owner_id, populate_existing=True
    )
    assert "TOKEN" not in json.dumps(
        runtime.MCP_STDIO_ADAPTER.contributions(local).tools[0].arguments_schema
    )


@pytest.mark.parametrize(
    "payload",
    [{}, {"environment": []}, {"environment": {"KEY": 1}}, {"environment": {"BAD=KEY": "x"}}],
)
async def test_credential_environment_rejects_malformed_stored_values(monkeypatch, payload):
    local = instance()
    local.credential_id = uuid4()
    monkeypatch.setattr(
        runtime, "load_credential",
        AsyncMock(return_value=SimpleNamespace(kind=runtime.STDIO_CREDENTIAL_KIND)),
    )
    monkeypatch.setattr(runtime, "read_credential", lambda _credential: payload)
    with pytest.raises(McpClientError, match="mcp.configurationInvalid"):
        await service.credential_environment(None, local)


@pytest.mark.parametrize("wrong", ["computer", "session", "size", "mime"])
async def test_runtime_rejects_artifact_before_transfer(monkeypatch, wrong):
    local, session_id = instance(), uuid4()
    descriptor = {
        "artifactId": "result",
        "backendId": str(local.execution_agent_id),
        "sessionId": str(session_id),
        "path": "mcp/result.json",
        "filename": "result.json",
        "size": 2,
        "sha256": "a" * 64,
        "contentType": "application/json",
    }
    descriptor.update(
        {
            "computer": {"backendId": str(uuid4())},
            "session": {"sessionId": str(uuid4())},
            "size": {"size": 101},
            "mime": {"contentType": "text/plain"},
        }[wrong]
    )
    result = runtime.McpRuntimeResult.model_validate(
        {
            "artifact": descriptor,
            "session": {
                "id": "process",
                "started": True,
                "idleTimeoutSeconds": 1800,
                "expiresAt": "2026-11-04T22:00:00Z",
            },
        }
    )
    client = SimpleNamespace(
        agent_id=local.execution_agent_id, request=AsyncMock(return_value=result)
    )
    export = AsyncMock()
    monkeypatch.setattr(runtime, "export_artifact", export)
    with pytest.raises(McpClientError):
        await runtime.runtime_json(
            client, "integration.mcp.call", {"sessionId": str(session_id)}, limit=100
        )
    export.assert_not_called()


@pytest.mark.parametrize("raw", [b'{"value":NaN}', b'{"value":1e400}', b"not-json"])
async def test_runtime_rejects_nonfinite_or_invalid_json(monkeypatch, stdio_context, raw):
    _, context = stdio_context
    client = context.execution_client
    monkeypatch.setattr(runtime, "export_artifact", AsyncMock(return_value=raw))
    with pytest.raises(McpClientError, match="mcp.invalidResponse"):
        await runtime.runtime_json(
            client, "integration.mcp.call", {"sessionId": str(context.session.id)}, limit=100
        )


@pytest.mark.parametrize(
    "payload,schema",
    [
        ({"content": [{"type": "image", "data": "not-base64", "mimeType": "image/png"}]}, None),
        ({"content": [{"type": "text"}]}, None),
        (
            {"content": [], "structuredContent": {"value": "wrong"}},
            {"type": "object", "properties": {"value": {"type": "integer"}}},
        ),
    ],
)
def test_invalid_results_fail_closed(payload, schema):
    with pytest.raises(McpClientError, match="mcp.invalidResponse"):
        runtime.call_result(payload, schema)


def test_valid_images_become_attachments_and_resources_remain_data():
    stream = BytesIO()
    Image.new("RGB", (1, 1)).save(stream, format="PNG")
    result = runtime.call_result(
        {
            "content": [
                {
                    "type": "image",
                    "data": base64.b64encode(stream.getvalue()).decode(),
                    "mimeType": "image/png",
                },
                {"type": "resource_link", "uri": "file:///private", "name": "private"},
            ]
        },
        None,
    )
    assert result.attachments[0].data == stream.getvalue()
    assert result.attachments[0].kind == "image" and not result.attachments[0].ephemeral
    assert "base64" not in result.content
    assert json.loads(result.content)["content"][1]["uri"] == "file:///private"


@pytest.fixture
def stdio_context():
    local = instance()
    context = SimpleNamespace(
        bound_options={
            "integration_id": str(local.id),
            "binding_revision": json.dumps(
                local.binding_data(), ensure_ascii=True, separators=(",", ":"), sort_keys=True
            ),
        },
        integration_settings=IntegrationSettings(instances=[local]),
        db=SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(
            execution_agent_id=local.execution_agent_id,
            execution_operations=["integration.mcp.call", "cache.export"],
        ))),
        run_id=uuid4(),
        session=SimpleNamespace(id=uuid4()),
        execution_client=SimpleNamespace(agent_id=local.execution_agent_id, request=AsyncMock()),
        current_integrations=AsyncMock(return_value=IntegrationSettings(
            instances=[local.model_copy(deep=True)]
        )),
    )
    context.execution_client.request.return_value = SimpleNamespace(
        artifact=SimpleNamespace(
            backend_id=str(local.execution_agent_id), session_id=str(context.session.id),
            size=2, content_type="application/json",
        ),
        session=None,
    )
    return local, context


@pytest.mark.parametrize("failure", ["wrong_computer", "changed_binding"])
async def test_tool_rejects_changed_binding_before_dispatch(stdio_context, failure):
    local, context = stdio_context
    if failure == "wrong_computer":
        context.execution_client.agent_id = uuid4()
    else:
        context.current_integrations.return_value.instances[0].config["executable"] = "/usr/bin/other"
    result = await runtime.execute_stdio_tool(local, McpCatalogTool.model_validate(TOOL), {}, context)
    assert result.metadata["errorCode"] == "tool.unavailable"
    context.execution_client.request.assert_not_called()


@pytest.mark.parametrize(("code", "expected"), [
    ("executionAgent.unavailable", "mcp.outcomeUnknown"),
    ("executionAgent.requestTooLarge", "executionAgent.requestTooLarge"),
])
async def test_tool_dispatch_failure_is_classified_without_retry(stdio_context, code, expected):
    local, context = stdio_context
    context.execution_client.request.side_effect = ExecutionClientError(code)
    result = await runtime.execute_stdio_tool(local, McpCatalogTool.model_validate(TOOL), {}, context)
    assert result.metadata["errorCode"] == expected
    context.execution_client.request.assert_awaited_once()


async def test_tool_cancellation_propagates_without_retry(stdio_context):
    local, context = stdio_context
    context.execution_client.request.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await runtime.execute_stdio_tool(local, McpCatalogTool.model_validate(TOOL), {}, context)
    context.execution_client.request.assert_awaited_once()


async def test_tool_transfers_large_image_as_artifact(monkeypatch, stdio_context):
    local, context = stdio_context
    client = context.execution_client
    stream = BytesIO()
    Image.new("RGB", (600, 600)).save(stream, format="PNG", compress_level=0)
    raw = json.dumps({"content": [{
        "type": "image", "mimeType": "image/png",
        "data": base64.b64encode(stream.getvalue()).decode(),
    }]}).encode()
    assert len(stream.getvalue()) > 1024 * 1024
    client.request.return_value = SimpleNamespace(
        artifact=SimpleNamespace(
            backend_id=str(local.execution_agent_id), session_id=str(context.session.id),
            size=len(raw), content_type="application/json",
        ),
        session=runtime.McpProcessSession(
            id="process", started=True, idleTimeoutSeconds=1800,
            expiresAt="2026-11-04T22:00:00Z",
        ),
    )
    export = AsyncMock(return_value=raw)
    monkeypatch.setattr(runtime, "export_artifact", export)
    result = await runtime.execute_stdio_tool(local, McpCatalogTool.model_validate(TOOL), {}, context)
    assert result.attachments[0].data == stream.getvalue()
    export.assert_awaited_once_with(
        client, client.request.return_value.artifact,
        limit=16 * 1024 * 1024, too_large_code="mcp.responseTooLarge",
    )
    client.request.assert_awaited_once()
    assert client.request.call_args.kwargs["timeout"] == 90


async def test_discovery_request_outlasts_sdk_deadline(monkeypatch, stdio_context):
    _, context = stdio_context
    client = context.execution_client
    monkeypatch.setattr(runtime, "export_artifact", AsyncMock(return_value=b"[]"))
    await runtime.runtime_json(
        client, "integration.mcp.discover", {"sessionId": str(context.session.id)}, limit=32 * 1024 * 1024
    )
    assert client.request.call_args.kwargs["timeout"] == 65


async def test_save_is_lazy_and_secrets_are_encrypted_outside_configuration(monkeypatch):
    settings = UserIntegrationSettings()
    monkeypatch.setattr(service, "owned_agent", AsyncMock())
    monkeypatch.setattr(service, "_load_integrations", AsyncMock(return_value=settings))
    monkeypatch.setattr(service, "_save_settings", AsyncMock())
    store = AsyncMock(return_value=SimpleNamespace(id=uuid4()))
    monkeypatch.setattr(service, "store_credential", store)
    request = service.McpStdioRequest.model_validate(
        {
            "name": "Local",
            "transport": "stdio",
            "executionAgentId": str(uuid4()),
            "executable": "/usr/bin/server",
            "workingDirectory": "/work",
            "environmentSecrets": {"TOKEN": "test-value"},
        }
    )
    local = await service.save_stdio(
        SimpleNamespace(commit=AsyncMock()), request, owner_id=uuid4(), user_id=uuid4()
    )
    assert local.config["tools"] == []
    assert local.config["environmentSecretNames"] == ["TOKEN"]
    assert "test-value" not in json.dumps(local.config)
    assert store.call_args.kwargs["kind"] == runtime.STDIO_CREDENTIAL_KIND


@pytest.mark.parametrize(("changes", "status", "code"), [
    (
        {"environment": {f"KEY_{i}": "x" for i in range(128)}, "environmentSecrets": {"TOKEN": "x"}},
        422,
        "mcp.configurationInvalid",
    ),
    # The UTF-8 argument values alone fit; the full discovery frame crosses the limit.
    ({"args": ["🙂" * 32763, "🙂" * 32763]}, 413, "executionAgent.requestTooLarge"),
])
async def test_create_rejects_unsendable_stdio_configuration(monkeypatch, changes, status, code):
    local = instance()
    monkeypatch.setattr(service, "_load_integrations", AsyncMock(return_value=IntegrationSettings()))
    request = service.McpStdioRequest.model_validate({
        **local.config, "tools": [], "name": local.name, "transport": "stdio",
        "executionAgentId": local.execution_agent_id, **changes,
    })
    with pytest.raises(ApiError, match=code) as rejected:
        await routes.create_mcp_server(request, AsyncMock(), user=SimpleNamespace(id=uuid4(), is_admin=True))
    assert rejected.value.status_code == status


@pytest.mark.parametrize("change", ["name", "enabled", "computer", "config", "environment", "secret", "same_secret"])
async def test_save_preserves_policies_only_for_unchanged_execution_binding(monkeypatch, change):
    local, owner_id, profile_id = instance(), uuid4(), uuid4()
    local.credential_id = uuid4()
    local.permissions.resource_availability = {mcp_resource_key("lookup"): False}
    local.profile_permissions[profile_id] = IntegrationPermissions.model_validate({
        "privatePermissions": {"policies": {"mcp.call": "auto"}},
    })
    policy_fields = {"permissions", "profile_permissions"}
    expected = local.model_dump(include=policy_fields) if change in {"name", "enabled"} else {
        "permissions": IntegrationPermissions().model_dump(), "profile_permissions": {},
    }
    settings = UserIntegrationSettings(instances=[local])
    previous_agent = local.execution_agent_id
    ownership = AsyncMock()
    monkeypatch.setattr(service, "owned_agent", ownership)
    monkeypatch.setattr(service, "_load_integrations", AsyncMock(return_value=settings))
    monkeypatch.setattr(service, "_save_settings", AsyncMock())
    monkeypatch.setattr(service, "credential_environment", AsyncMock(return_value={"TOKEN": "old"}))
    monkeypatch.setattr(service, "store_credential", AsyncMock(return_value=SimpleNamespace(id=uuid4())))
    monkeypatch.setattr(service, "delete_credential", AsyncMock())
    monkeypatch.setattr(SettingsStore, "load_optional", AsyncMock(return_value=ToolSettings()))
    payload = {
        "name": local.name, "enabled": local.enabled, "transport": "stdio",
        "executionAgentId": local.execution_agent_id,
        **runtime.McpStdioConfig.model_validate(local.config).model_dump(
            include=set(runtime.McpStdioBinding.model_fields)
        ),
    }
    payload.update({
        "name": {"name": "Renamed"},
        "enabled": {"enabled": False},
        "computer": {"executionAgentId": uuid4()},
        "config": {"executable": "/usr/bin/other"},
        "environment": {"environment": {"LANG": "en_US.UTF-8"}},
        "secret": {"environmentSecrets": {"TOKEN": "new"}},
        "same_secret": {"environmentSecrets": {"TOKEN": "old"}},
    }[change])
    saved = await service.save_stdio(
        SimpleNamespace(commit=AsyncMock()), service.McpStdioRequest.model_validate(payload),
        owner_id=owner_id, user_id=owner_id, integration_id=local.id,
    )
    assert saved.model_dump(include=policy_fields) == expected
    if change == "computer":
        assert any(args[2] == previous_agent for args, _ in ownership.await_args_list)


async def test_session_management_filters_connection_and_chat_ownership(monkeypatch):
    local, owned_chat = instance(), uuid4()
    process_id = uuid4()
    monkeypatch.setattr(service, "owned_agent", AsyncMock())
    sessions = runtime.McpSessionsResult.model_validate(
        {
            "sessions": [
                {
                    "connectionId": str(connection_id),
                    "sessionId": str(session_id),
                    "id": str(process_id),
                    "idleTimeoutSeconds": 1800,
                    "expiresAt": "2026-11-04T22:00:00Z",
                    "busy": False,
                }
                for connection_id, session_id in [
                    (local.id, owned_chat),
                    (local.id, uuid4()),
                    (uuid4(), owned_chat),
                ]
            ]
        }
    )
    client = SimpleNamespace(request=AsyncMock(return_value=sessions))
    monkeypatch.setattr(service, "connected_client", lambda _id: client)
    db = SimpleNamespace(scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: [owned_chat])))
    assert [item.session_id for item in await service.managed_sessions(db, local, uuid4())] == [
        owned_chat
    ]
    with pytest.raises(McpClientError, match="mcp.notFound"):
        await service.close_session(db, local, uuid4(), uuid4(), uuid4())
    assert all(
        call.args[:2] == ("integration.mcp.manage", {"action": "list"})
        for call in client.request.call_args_list
    )
    client.request.side_effect = [sessions, runtime.McpCloseResult(closed=True)]
    assert await service.close_session(db, local, owned_chat, process_id, uuid4()) == {"closed": True}
    client.request.assert_awaited_with(
        "integration.mcp.manage",
        {
            "action": "close",
            "connectionId": str(local.id),
            "sessionId": str(owned_chat),
            "processId": str(process_id),
        },
        runtime.McpCloseResult,
    )


@pytest.mark.parametrize("allow_revoked", [False, True])
async def test_unowned_computer_cannot_be_configured_or_managed(allow_revoked):
    db = SimpleNamespace(scalar=AsyncMock(return_value=None))
    user_id, agent_id = uuid4(), uuid4()
    with pytest.raises(ApiError):
        await service.owned_agent(db, user_id, agent_id, allow_revoked=allow_revoked)
    expected = select(ExecutionAgent).where(
        ExecutionAgent.id == agent_id, ExecutionAgent.user_id == user_id
    )
    if not allow_revoked:
        expected = expected.where(ExecutionAgent.revoked_at.is_(None))
    assert db.scalar.call_args.args[0].whereclause.compare(expected.whereclause)


@pytest.mark.parametrize(("operation", "rebound"), [
    ("enable", False), ("read", False), ("permissions", False),
    ("enable", True), ("permissions", True),
])
async def test_generic_stdio_routes_require_computer_ownership(monkeypatch, operation, rebound):
    local = instance()
    local.enabled = False
    user = SimpleNamespace(id=uuid4(), is_admin=True)
    locked = local.model_copy(update={"execution_agent_id": uuid4()})
    initial_settings = IntegrationSettings(instances=[local])
    locked_settings = IntegrationSettings(instances=[locked])
    monkeypatch.setattr(SettingsStore, "load", AsyncMock(side_effect=lambda _db, _kind, **kwargs: (
        locked_settings if kwargs.get("for_update") else initial_settings
    )))
    owned_computer = select(ExecutionAgent).where(
        ExecutionAgent.id == local.execution_agent_id,
        ExecutionAgent.user_id == user.id,
        ExecutionAgent.revoked_at.is_(None),
    )
    db = SimpleNamespace(scalar=AsyncMock(side_effect=lambda query: (
        object() if rebound and query.whereclause.compare(owned_computer.whereclause) else None
    )))
    with pytest.raises(ApiError, match="executionAgent.notFound"):
        if operation == "enable":
            await generic_routes.set_integration_enabled(
                local.id, generic_routes.EnabledRequest(enabled=True), db, user, "instance"
            )
        elif operation == "read":
            await generic_routes.get_integration_permissions(local.id, user, db, "instance")
        else:
            await generic_routes.update_integration_permissions(
                local.id, IntegrationPermissions(), user, db, "instance"
            )


@pytest.mark.parametrize("scope", ["personal", "instance"])
async def test_revoked_own_computer_is_listed_unavailable_and_config_and_credential_deleted(monkeypatch, scope):
    local, other = instance(), instance()
    user = SimpleNamespace(id=uuid4(), is_admin=scope == "instance")
    owner_id = user.id if scope == "personal" else None
    local.credential_id = uuid4()
    local.owner_id = owner_id
    settings = (UserIntegrationSettings if scope == "personal" else IntegrationSettings)(
        instances=[local, other]
    )
    global_settings = settings if scope == "instance" else IntegrationSettings(
        adapter_availability={"mcp_streamable_http": "full"}
    )
    monkeypatch.setattr(SettingsStore, "load", AsyncMock(side_effect=lambda _db, kind, **_kw: settings if kind is UserIntegrationSettings else global_settings))
    monkeypatch.setattr(SettingsStore, "load_optional", AsyncMock(return_value=settings if scope == "personal" else None))
    monkeypatch.setattr(service, "_load_integrations", AsyncMock(return_value=settings))
    monkeypatch.setattr(mcp_service, "_load_tools", AsyncMock(return_value=ToolSettings()))
    monkeypatch.setattr(mcp_service, "_save_settings", AsyncMock())
    monkeypatch.setattr(service, "credential_environment", AsyncMock(return_value={"TOKEN": "test-value"}))
    delete = AsyncMock()
    monkeypatch.setattr(mcp_service, "delete_credential", delete)
    revoked = SimpleNamespace(id=local.execution_agent_id, revoked_at="revoked")
    db = SimpleNamespace(
        scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: [revoked])),
        scalar=AsyncMock(return_value=revoked), commit=AsyncMock(),
    )
    for result in (
        await routes.list_mcp_servers(db, scope=scope, user=user),
        await routes.list_visible_mcp_servers(db, user),
    ):
        assert [(row["id"], row["deletedComputer"], row["available"]) for row in result["servers"]] == [(str(local.id), True, False)]
    expected = select(ExecutionAgent).where(ExecutionAgent.user_id == user.id)
    assert all(
        call.args[0].whereclause.compare(expected.whereclause)
        for call in db.scalars.call_args_list
    )
    await routes.delete_mcp_server(local.id, db, scope=scope, user=user)
    expected = expected.where(ExecutionAgent.id == local.execution_agent_id)
    assert db.scalar.call_args.args[0].whereclause.compare(expected.whereclause)
    assert [item.id for item in settings.instances] == [other.id]
    delete.assert_awaited_once_with(db, local.credential_id, owner_id=owner_id)


async def test_list_survives_missing_rotated_credential_but_runtime_rejects_it(monkeypatch):
    local = instance()
    local.credential_id = uuid4()
    local.config = {**local.config, "environmentSecretNames": ["TOKEN"]}
    legacy = instance()
    legacy.credential_id = uuid4()
    settings = IntegrationSettings(instances=[local, legacy])
    monkeypatch.setattr(SettingsStore, "load", AsyncMock(return_value=settings))
    monkeypatch.setattr(SettingsStore, "load_optional", AsyncMock(return_value=None))
    monkeypatch.setattr(routes, "owned_agent_states", AsyncMock(return_value={
        local.execution_agent_id: False, legacy.execution_agent_id: False,
    }))
    monkeypatch.setattr(runtime, "load_credential", AsyncMock(return_value=None))
    user = SimpleNamespace(id=uuid4(), is_admin=True)
    for result in (await routes.list_mcp_servers(None, user=user), await routes.list_visible_mcp_servers(None, user)):
        assert [row["environmentSecretNames"] for row in result["servers"]] == [["TOKEN"], []]
    with pytest.raises(McpClientError, match="mcp.configurationInvalid"):
        await runtime.runtime_arguments(None, local, uuid4())


async def test_revoked_computer_still_requires_active_ownership_for_runtime_management():
    db = SimpleNamespace(scalar=AsyncMock(return_value=None))
    with pytest.raises(ApiError):
        await service.refresh_stdio(db, instance(), uuid4())
    assert "revoked_at IS NULL" in str(db.scalar.call_args.args[0])
    with pytest.raises(ApiError):
        await service.managed_sessions(db, instance(), uuid4())
    assert "revoked_at IS NULL" in str(db.scalar.call_args.args[0])
