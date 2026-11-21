import asyncio
import json
from collections import OrderedDict
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jsonschema import ValidationError as JsonSchemaValidationError
from sqlalchemy.ext.asyncio import AsyncSession

import mato_loop_backend.integrations.mcp.adapter as adapter_module
import mato_loop_backend.integrations.mcp.client as client_module
import mato_loop_backend.integrations.mcp.oauth as oauth_module
import mato_loop_backend.integrations.mcp.service as service_module
from mato_loop_backend.api.errors import register_error_handlers
from mato_loop_backend.api.routes import mcp as mcp_routes
from mato_loop_backend.api.routes.mcp import (
    McpServerRequest,
    update_mcp_server,
)
from mato_loop_backend.api.routes.mcp import (
    router as mcp_router,
)
from mato_loop_backend.db.session import get_db_session
from mato_loop_backend.integrations.mcp.adapter import (
    MCP_ADAPTER,
    McpConfig,
    mcp_resource_key,
)
from mato_loop_backend.integrations.mcp.client import (
    McpClient,
    McpClientError,
    _BoundedTransport,
)
from mato_loop_backend.integrations.mcp.service import (
    McpIntegrationService,
)
from mato_loop_backend.integrations.registry import integration_adapters
from mato_loop_backend.integrations.service import IntegrationSettingsError
from mato_loop_backend.integrations.sonobin.adapter import SONOBIN_ADAPTER, SONOBIN_TOOL_LABELS
from mato_loop_backend.integrations.sonobin.service import SonoBinIntegrationService
from mato_loop_backend.permissions.resolution import resolve_tool_execution_policy
from mato_loop_backend.security import require_admin, require_user
from mato_loop_backend.settings import (
    GENERAL_PROFILE_ID,
    AgentProfilePermissions,
    IntegrationAdapterAvailability,
    IntegrationInstance,
    IntegrationSettings,
    SettingsStore,
    ToolExecutionPolicy,
    ToolSettings,
    UserIntegrationSettings,
    load_effective_integrations,
)
from mato_loop_backend.settings.models import IntegrationPermissions
from mato_loop_backend.tools.catalog import tool_registry
from mato_loop_backend.tools.contracts import (
    PERMISSION_REASON_ARGUMENT,
    DynamicToolArgumentsError,
    ToolDefinition,
    ToolResult,
)
from mato_loop_backend.tools.registry import ToolRegistry


class FakeSession:
    calls = []
    list_calls = []
    output_schema_at_call = "unset"

    def __init__(self, *_args, **_kwargs):
        self._tool_output_schemas = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def initialize(self):
        return None

    async def list_tools(self, cursor=None):
        if cursor is None:
            return SimpleNamespace(
                tools=[
                    SimpleNamespace(
                        name="lookup",
                        title="Lookup",
                        description="Look up a value",
                        inputSchema={
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                            "required": ["query"],
                            "additionalProperties": False,
                        },
                        outputSchema=None,
                        annotations=SimpleNamespace(
                            readOnlyHint=True,
                            destructiveHint=False,
                            idempotentHint=True,
                            openWorldHint=False,
                        ),
                    )
                ],
                nextCursor="next",
            )
        self.list_calls.append(cursor)
        return SimpleNamespace(tools=[], nextCursor=None)

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        type(self).output_schema_at_call = self._tool_output_schemas[name]
        return SimpleNamespace(
            isError=False,
            model_dump=lambda **_kwargs: {"content": [{"type": "text", "text": "ok"}]},
        )


@asynccontextmanager
async def fake_transport(url, *, http_client):
    assert url in {"https://mcp.example.test/rpc", "http://192.168.1.30:9000/mcp"}
    assert http_client.headers["Authorization"] == "Bearer secret"
    assert http_client.timeout.connect == 10
    assert http_client.timeout.read == 60
    assert isinstance(http_client._transport, _BoundedTransport)
    yield object(), object(), lambda: None


@pytest.mark.parametrize("endpoint", ["https://mcp.example.test/rpc", "http://192.168.1.30:9000/mcp"])
async def test_mcp_client_uses_streamable_http_transport(monkeypatch, endpoint) -> None:
    real_client = httpx.AsyncClient
    client_settings = []

    def client_factory(**kwargs):
        client_settings.append(kwargs)
        return real_client(**kwargs)

    FakeSession.calls.clear()
    FakeSession.list_calls.clear()
    monkeypatch.setattr(client_module.httpx, "AsyncClient", client_factory)
    monkeypatch.setattr(client_module, "streamable_http_client", fake_transport)
    monkeypatch.setattr(client_module, "ClientSession", FakeSession)
    client = McpClient(endpoint, "secret")

    tools = await client.discover_tools()
    result, is_error = await client.call_tool("lookup", {"query": "x"}, None)

    assert tools == [
        {
            "name": "lookup",
            "title": "Lookup",
            "description": "Look up a value",
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
            "output_schema": None,
            "annotations": {
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
            },
        }
    ]
    assert result == {"content": [{"type": "text", "text": "ok"}]}
    assert is_error is False
    assert FakeSession.calls[-1] == ("lookup", {"query": "x"})
    assert FakeSession.output_schema_at_call is None
    assert FakeSession.list_calls == ["next"]
    assert all(settings["trust_env"] is False for settings in client_settings)


@pytest.mark.parametrize("oauth", [False, True])
@pytest.mark.parametrize("path", ["/mcp", "/webhook/a%2Fb", "/mcp%3Fabc"])
async def test_discovery_recovers_missing_slash_and_persists_endpoint(monkeypatch, oauth, path):
    def reply(request):
        if request.url.raw_path == path.encode():
            return httpx.Response(404)
        if request.method == "GET":
            return httpx.Response(405)
        body = json.loads(request.content)
        if "id" not in body:
            return httpx.Response(202)
        result = (
            {"protocolVersion": body["params"]["protocolVersion"], "capabilities": {"tools": {}},
             "serverInfo": {"name": "Test", "version": "1"}}
            if body["method"] == "initialize" else {"tools": []}
        )
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    monkeypatch.setattr(client_module.httpx, "AsyncHTTPTransport", lambda **_kwargs: httpx.MockTransport(reply))
    instance = IntegrationInstance(
        name="Server", integration_type="mcp", adapter_type="mcp_streamable_http",
        config={"endpoint": "https://mcp.example.test" + path, **({"oauth": {}} if oauth else {})},
    )
    monkeypatch.setattr(SettingsStore, "load", AsyncMock(return_value=IntegrationSettings(instances=[instance])))
    monkeypatch.setattr(SettingsStore, "save", AsyncMock())
    if oauth:
        monkeypatch.setattr(service_module, "connection_token", AsyncMock(return_value="secret"))
        with pytest.raises(McpClientError, match="mcp.authorizationRequired"):
            await McpIntegrationService.refresh(AsyncMock(), instance.id, owner_id=None)
    else:
        instance = await McpIntegrationService.create(
            AsyncMock(), name="Server", endpoint="https://mcp.example.test" + path,
            token=None, enabled=True, owner_id=None,
        )
    assert instance.config["endpoint"] == "https://mcp.example.test" + path + ("" if oauth else "/")


@pytest.mark.parametrize("status, code", [(401, "mcp.authenticationFailed"), (403, "mcp.authenticationFailed"),
                                         (404, "mcp.endpointNotFound"), (500, "mcp.unavailable")])
async def test_discovery_maps_http_errors(monkeypatch, status, code):
    monkeypatch.setattr(client_module.httpx, "AsyncHTTPTransport",
                        lambda **_kwargs: httpx.MockTransport(lambda _request: httpx.Response(status)))
    with pytest.raises(McpClientError) as error:
        await McpClient("https://mcp.example.test/mcp/").discover_tools()
    assert error.value.code == code


@pytest.mark.parametrize("value", ["https://sonobin.example.test/app", "https://sonobin.example.test/app/",
                                 "https://sonobin.example.test/app/mcp", "https://sonobin.example.test/app/mcp/"])
def test_sonobin_api_accepts_base_url_and_preserves_installation_path(value):
    from mato_loop_backend.api.routes.integrations import SonoBinIntegrationCreateRequest

    request = SonoBinIntegrationCreateRequest(name="SonoBin", baseUrl=value, bearerToken="secret")
    assert SonoBinIntegrationService._validate_connection(str(request.base_url), "secret") == "https://sonobin.example.test/app/mcp/"


class ChunkStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"a" * 600_000
        yield b"b" * 600_000


class ChunkTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request):
        return httpx.Response(200, stream=ChunkStream())

    async def aclose(self):
        pass


async def test_mcp_transport_bounds_each_response() -> None:
    async with httpx.AsyncClient(
        transport=_BoundedTransport(ChunkTransport())
    ) as client:
        async with client.stream("GET", "https://mcp.example.test/rpc") as response:
            with pytest.raises(McpClientError, match="mcp.responseTooLarge"):
                await response.aread()


@pytest.mark.parametrize("failure", [RuntimeError("raw upstream failure"), McpClientError("mcp.authorizationRequired")])
async def test_mcp_call_failure_after_dispatch_is_outcome_unknown(monkeypatch, failure) -> None:
    calls = []
    class FailingSession(FakeSession):
        async def call_tool(self, name, arguments):
            assert self._tool_output_schemas[name] == {"type": "object"}
            calls.append(name)
            raise failure

    monkeypatch.setattr(client_module, "streamable_http_client", fake_transport)
    monkeypatch.setattr(client_module, "ClientSession", FailingSession)

    with pytest.raises(McpClientError) as exc_info:
        await McpClient(
            "https://mcp.example.test/rpc", "secret"
        ).call_tool("lookup", {}, {"type": "object"})

    assert exc_info.value.code == "mcp.outcomeUnknown"
    assert calls == ["lookup"]


async def test_mcp_call_cancellation_after_dispatch_is_outcome_unknown(monkeypatch) -> None:
    class CancelledSession(FakeSession):
        async def call_tool(self, name, arguments):
            raise asyncio.CancelledError

    monkeypatch.setattr(client_module, "streamable_http_client", fake_transport)
    monkeypatch.setattr(client_module, "ClientSession", CancelledSession)

    with pytest.raises(McpClientError) as exc_info:
        await McpClient("https://mcp.example.test/rpc", "secret").call_tool(
            "lookup", {}, None
        )

    assert exc_info.value.code == "mcp.outcomeUnknown"


async def test_oauth_registration_error_is_not_reported_as_catalog_failure(monkeypatch):
    from mcp.client.auth import OAuthRegistrationError

    class RejectedSession(FakeSession):
        async def initialize(self):
            raise ExceptionGroup("auth", [OAuthRegistrationError("private provider response")])

    monkeypatch.setattr(client_module, "streamable_http_client", fake_transport)
    monkeypatch.setattr(client_module, "ClientSession", RejectedSession)
    with pytest.raises(McpClientError, match="mcp.oauthRegistrationFailed"):
        await McpClient("https://mcp.example.test/rpc", "secret").discover_tools()


async def test_mcp_discovery_rejects_repeated_pagination_cursor(monkeypatch) -> None:
    class RepeatingSession(FakeSession):
        async def list_tools(self, cursor=None):
            return SimpleNamespace(tools=[], nextCursor="same")

    monkeypatch.setattr(client_module, "streamable_http_client", fake_transport)
    monkeypatch.setattr(client_module, "ClientSession", RepeatingSession)

    with pytest.raises(McpClientError) as exc_info:
        await McpClient("https://mcp.example.test/rpc", "secret").discover_tools()

    assert exc_info.value.code == "mcp.invalidResponse"


@pytest.mark.parametrize("budget, timeout, code", [
    (1, 30, "mcp.responseTooLarge"),
    (32 * 1024 * 1024, 0.01, "mcp.timeout"),
])
async def test_mcp_discovery_bounds_endless_unique_cursors(monkeypatch, budget, timeout, code):
    class EndlessSession(FakeSession):
        async def list_tools(self, cursor=None):
            await asyncio.sleep(0)
            return SimpleNamespace(tools=[], nextCursor=str(int(cursor or 0) + 1))

    monkeypatch.setattr(client_module, "MAX_MCP_CATALOG_BYTES", budget)
    monkeypatch.setattr(client_module, "streamable_http_client", fake_transport)
    monkeypatch.setattr(client_module, "ClientSession", EndlessSession)
    with pytest.raises(McpClientError, match=code):
        await McpClient("https://mcp.example.test/rpc", "secret").discover_tools(
            read_timeout_seconds=timeout
        )


def test_mcp_contribution_has_stable_schema_and_tool_resource_policy() -> None:
    instance = IntegrationInstance(
        id=uuid4(),
        name="Server",
        integration_type="mcp",
        adapter_type="mcp_streamable_http",
        config={
            "endpoint": "https://mcp.example.test/rpc",
            "tools": [
                {
                    "name": "lookup",
                    "input_schema": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                    },
                },
                {"name": "other", "input_schema": {"type": "object"}},
            ],
        },
    )
    contribution = MCP_ADAPTER.contributions(instance).tools[0]
    definition = ToolRegistry._contributed_definition(contribution)
    context = SimpleNamespace(bound_options={"binding_revision": "{}"})

    assert definition.schema()["parameters"]["required"] == ["query"]
    assert definition.validate_arguments({
        "query": "value", PERMISSION_REASON_ARGUMENT: "Lookup is required."
    }) == {"query": "value"}
    assert definition.validate_arguments({"query": "value"}) == {"query": "value"}
    with pytest.raises(DynamicToolArgumentsError):
        definition.validate_arguments({
            "query": "value", PERMISSION_REASON_ARGUMENT: " "
        })
    with pytest.raises(JsonSchemaValidationError):
        definition.validate_arguments({PERMISSION_REASON_ARGUMENT: "Lookup is required."})
    request = contribution.authorization_factory({}, context)[0]
    assert request.capability == "mcp.call"
    assert request.policy_resource_key == mcp_resource_key("lookup")
    assert request.policy_integration_id == instance.id
    assert len(request.effective_session_grants) == 1
    assert request.effective_session_grants[0].access == "write"
    assert request.effective_session_grants[0].resource_type == "integration_resource"
    assert json.loads(request.effective_session_grants[0].resource_scope)["resourceKey"] == mcp_resource_key("lookup")
    assert contribution.integration_id == str(instance.id)
    assert contribution.policy_resource_key == mcp_resource_key("lookup")
    assert contribution.name.startswith("mcp_server_lookup_")
    _, sources, _ = ToolRegistry([])._combined_definitions(
        IntegrationSettings(instances=[instance])
    )
    first, second = sources.values()
    assert first[2] is second[2]


def test_mcp_permission_accepts_catalog_sized_binding_revision() -> None:
    instance = IntegrationInstance(
        name="Server",
        integration_type="mcp",
        adapter_type="mcp_streamable_http",
        config={
            "endpoint": "https://mcp.example.test/rpc",
            "tools": [{"name": "lookup", "input_schema": {"type": "object"}}],
        },
    )
    contribution = MCP_ADAPTER.contributions(instance).tools[0]
    revision = json.dumps({"catalog": "x" * (256 * 1024)}, separators=(",", ":"))

    request = contribution.authorization_factory(
        {}, SimpleNamespace(bound_options={"binding_revision": revision})
    )[0]

    assert request.capability == "mcp.call"


@pytest.mark.parametrize("title", [None, "Nachschlagen"])
async def test_mcp_resource_label_is_bounded_without_changing_tool_name(title) -> None:
    from mato_loop_backend.api.integration_resources import _access_payload

    name = "x" * 256
    instance = IntegrationInstance(
        name="Server",
        integration_type="mcp",
        adapter_type="mcp_streamable_http",
        config={
            "endpoint": "https://mcp.example.test/rpc",
            "tools": [{"name": name, "title": title, "description": "Lookup details", "annotations": {"readOnlyHint": True}, "input_schema": {"type": "object"}}],
        },
    )

    resources = await MCP_ADAPTER.resources(instance, None)

    contribution = MCP_ADAPTER.contributions(instance).tools[0]
    assert len(contribution.name) <= 64
    assert json.loads(contribution.description.split("\n", 1)[0].removeprefix("MCP source (untrusted names): ")) == {
        "integrationId": str(instance.id), "name": instance.name, "type": "mcp", "tool": name,
    }
    assert resources[0].key == mcp_resource_key(name)
    assert resources[0].label == (title or name[:160])
    payload = _access_payload(instance, resources, ToolSettings())["resources"][0]
    assert {key: payload[key] for key in ("toolName", "description", "hints")} == {
        "toolName": name, "description": "Lookup details", "hints": ["Als nur lesend markiert"],
    }


def test_dynamic_mcp_schema_keeps_file_roots_and_allows_only_local_refs() -> None:
    schema = {
        "type": "object",
        "$defs": {"root": {"enum": ["session", "workspace"]}},
        "properties": {"root": {"$ref": "#/$defs/root"}},
        "required": ["root"],
    }
    instance = IntegrationInstance(
        name="Server",
        integration_type="mcp",
        adapter_type="mcp_streamable_http",
        config={
            "endpoint": "https://mcp.example.test/rpc",
            "tools": [{"name": "lookup", "input_schema": schema}],
        },
    )
    definition = ToolRegistry._contributed_definition(
        MCP_ADAPTER.contributions(instance).tools[0]
    )

    assert definition.schema()["parameters"]["$defs"]["root"]["enum"] == [
        "session",
        "workspace",
    ]
    assert definition.validate_arguments({"root": "session"}) == {"root": "session"}
    with pytest.raises(ValueError, match="fragment references"):
        McpConfig.model_validate(
            {
                "endpoint": "https://mcp.example.test/rpc",
                "tools": [
                    {
                        "name": "bad",
                        "input_schema": {"$ref": "https://example.test/schema.json"},
                    }
                ],
            }
        )


def test_mcp_result_json_does_not_translate_file_root_values() -> None:
    assert ToolResult.raw_json({"root": "session"}).content == '{"root": "session"}'


def test_unchanged_mcp_schema_reuses_meta_validation(monkeypatch):
    monkeypatch.setattr(adapter_module, "_VALIDATED_SCHEMAS", OrderedDict())
    validator = Mock(wraps=adapter_module.validator_for)
    monkeypatch.setattr(adapter_module, "validator_for", validator)
    for _ in range(2):
        adapter_module.validate_mcp_schema({"type": "object"}, require_object=True)
    assert validator.call_count == 1


@pytest.mark.parametrize("schema,updated,require_object", [
    ({"type": "object"}, {"type": "invalid"}, True),
    ({"type": "string"}, {"type": "string"}, True),
    ({"enum": ["x"]}, {"enum": ("x",)}, False),
])
def test_cached_mcp_schema_cannot_bypass_validation(schema, updated, require_object):
    adapter_module.validate_mcp_schema(schema, require_object=False)
    schema.clear()
    schema.update(updated)
    with pytest.raises(ValueError):
        adapter_module.validate_mcp_schema(schema, require_object=require_object)


@pytest.mark.parametrize("adapter", [MCP_ADAPTER, SONOBIN_ADAPTER])
async def test_mcp_tool_errors_keep_payload_and_persistent_error_status(monkeypatch, adapter):
    instance = IntegrationInstance(
        name="Server", integration_type=adapter.integration_type, adapter_type=adapter.adapter_type,
        config={"endpoint": "https://mcp.example.test/mcp/", "tools": [{"name": "lookup", "input_schema": {"type": "object"}}]},
    )
    payload = {"isError": True, "content": [{"type": "text", "text": "Missing required remote field"}]}
    monkeypatch.setattr(McpClient, "call_tool", AsyncMock(return_value=(payload, True)))
    context = SimpleNamespace(db=None, integration_settings=IntegrationSettings(instances=[instance]))
    result = await adapter.contributions(instance).tools[0].handler({}, context)
    assert (result.is_error, result.metadata.get("errorCode"), json.loads(result.content)) == (True, "mcp.callFailed", payload)


def test_mcp_config_rejects_endpoint_query() -> None:
    with pytest.raises(ValueError):
        McpConfig(endpoint="https://mcp.example.test/rpc?token=secret")


def test_sonobin_adapter_covers_the_complete_catalog() -> None:
    expected = {
        "list_recordings",
        "list_recordings_without_tracks",
        "get_recording",
        "get_metadata_candidate",
        "list_tracks",
        "get_track",
        "get_mcp_context",
        "list_providers",
        "search_music",
        "list_playlists",
        "get_playlist",
        "list_playlist_tracks",
        "list_likes",
        "get_acquisition_status",
        "get_asset",
        "search_musicbrainz",
        "get_asset_download_url",
        "list_jobs",
        "get_job",
        "get_diagnostics_overview",
        "list_diagnostic_entries",
        "get_diagnostic_entry",
        "get_runtime_diagnostics",
        "get_integration_diagnostics",
        "get_player_diagnostics",
        "get_playback_diagnostic_events",
        "list_generator_runs",
        "get_generator_run",
        "create_playlist",
        "update_playlist",
        "delete_playlist",
        "add_playlist_tracks",
        "remove_playlist_tracks",
        "reorder_playlist_tracks",
        "set_track_like",
        "import_remote_track",
        "create_manual_candidate",
        "update_manual_candidate",
        "add_musicbrainz_candidate",
        "identify_asset",
        "create_track",
        "set_track_candidate",
        "set_preferred_asset",
        "set_asset_source_candidate",
        "move_asset",
        "acquire_recording_asset",
        "cancel_job",
        "delete_candidate",
        "delete_track",
        "delete_asset",
        "delete_recording",
        "merge_recordings",
    }

    assert set(SONOBIN_TOOL_LABELS) == expected
    assert all(SONOBIN_TOOL_LABELS.values())


@pytest.mark.parametrize(
    "arguments,params",
    [
        ({"value": float("nan")}, {"reason": "invalid_json"}),
        ({"value": "x" * (256 * 1024)}, {"reason": "too_large", "maxBytes": 256 * 1024}),
    ],
)
def test_dynamic_mcp_arguments_are_bounded_finite_json(arguments, params) -> None:
    instance = IntegrationInstance(
        name="Server",
        integration_type="mcp",
        adapter_type="mcp_streamable_http",
        config={
            "endpoint": "https://mcp.example.test/rpc",
            "tools": [{"name": "lookup", "input_schema": {"type": "object"}}],
        },
    )
    definition = ToolRegistry._contributed_definition(
        MCP_ADAPTER.contributions(instance).tools[0]
    )
    with pytest.raises(DynamicToolArgumentsError) as error:
        definition.validate_arguments(arguments)
    assert error.value.params == params


def test_dynamic_mcp_arguments_have_bounded_depth() -> None:
    value = 0
    for _ in range(66):
        value = [value]
    instance = IntegrationInstance(
        name="Server",
        integration_type="mcp",
        adapter_type="mcp_streamable_http",
        config={
            "endpoint": "https://mcp.example.test/rpc",
            "tools": [{"name": "lookup", "input_schema": {"type": "object"}}],
        },
    )
    definition = ToolRegistry._contributed_definition(
        MCP_ADAPTER.contributions(instance).tools[0]
    )
    with pytest.raises(DynamicToolArgumentsError, match="depth") as error:
        definition.validate_arguments({"value": value})
    assert error.value.params == {"reason": "too_deep", "maxDepth": 64}


def test_mcp_policy_resolves_global_server_and_tool_levels() -> None:
    integration_id = uuid4()
    resource_key = mcp_resource_key("lookup")
    tools = ToolSettings()
    tools.private_permissions.policies["mcp.call"] = ToolExecutionPolicy.AUTO
    tools.private_permissions.integration_policies[str(integration_id)] = {
        "mcp.call": ToolExecutionPolicy.ASK
    }
    tools.private_permissions.resource_policies[str(integration_id)] = {
        resource_key: {"mcp.call": ToolExecutionPolicy.DISABLED}
    }

    assert resolve_tool_execution_policy(tools, "mcp.call").policy == ToolExecutionPolicy.AUTO
    assert (
        resolve_tool_execution_policy(
            tools, "mcp.call", integration_id=integration_id
        ).policy
        == ToolExecutionPolicy.ASK
    )
    assert (
        resolve_tool_execution_policy(
            tools,
            "mcp.call",
            integration_id=integration_id,
            resource_key=resource_key,
        ).policy
        == ToolExecutionPolicy.DISABLED
    )


@pytest.mark.parametrize(
    ("global_policy", "server_policy", "tool_policy", "profile_policy", "offered"),
    [
        ("disabled", "auto", None, None, True),
        (None, "disabled", "auto", None, True),
        ("auto", "auto", "disabled", None, False),
        ("auto", "auto", "auto", "disabled", False),
    ],
    ids=[
        "server-widens-global-disabled",
        "tool-widens-server-disabled",
        "tool-disabled",
        "profile-caps-tool",
    ],
)
def test_mcp_tool_offering_uses_exact_policy_scope(
    global_policy, server_policy, tool_policy, profile_policy, offered
) -> None:
    instance = IntegrationInstance(
        name="Server",
        integration_type="mcp",
        adapter_type="mcp_streamable_http",
        config={
            "endpoint": "https://mcp.example.test/rpc",
            "tools": [{"name": "lookup", "input_schema": {"type": "object"}}],
        },
    )
    contribution = MCP_ADAPTER.contributions(instance).tools[0]
    resource_key = mcp_resource_key("lookup")
    settings = ToolSettings()
    if global_policy is not None:
        settings.private_permissions.policies["mcp.call"] = global_policy
    if server_policy is not None:
        settings.private_permissions.integration_policies[str(instance.id)] = {
            "mcp.call": server_policy
        }
    if tool_policy is not None:
        settings.private_permissions.resource_policies[str(instance.id)] = {
            resource_key: {"mcp.call": tool_policy}
        }
    if profile_policy is not None:
        settings._agent_permissions = AgentProfilePermissions(
            resource_policies={
                str(instance.id): {resource_key: {"mcp.call": profile_policy}}
            }
        )

    effective = tool_registry.effective(
        IntegrationSettings(instances=[instance]), tool_settings=settings
    )

    assert (contribution.name in effective.tools) is offered


def test_mcp_tool_offering_does_not_use_another_profile_resource_override() -> None:
    instance = IntegrationInstance(
        name="Server",
        integration_type="mcp",
        adapter_type="mcp_streamable_http",
        config={
            "endpoint": "https://mcp.example.test/rpc",
            "tools": [{"name": "lookup", "input_schema": {"type": "object"}}],
        },
    )
    contribution = MCP_ADAPTER.contributions(instance).tools[0]
    settings = ToolSettings()
    settings._agent_permissions = AgentProfilePermissions(
        integration_policies={str(instance.id): {"mcp.call": "disabled"}},
        resource_policies={
            str(instance.id): {
                mcp_resource_key("another-tool"): {"mcp.call": "auto"}
            }
        },
    )

    effective = tool_registry.effective(
        IntegrationSettings(instances=[instance]), tool_settings=settings
    )

    assert contribution.name not in effective.tools


def test_resource_argument_tools_remain_unscoped_during_offering() -> None:
    calls = []

    def resolve(*_args, **kwargs):
        calls.append(kwargs)
        return ToolExecutionPolicy.AUTO

    registry = ToolRegistry([], policy_resolver=resolve)
    definition = ToolDefinition(
        "dynamic_resource",
        "Dynamic resource",
        None,
        AsyncMock(),
        actions=MCP_ADAPTER.resource_actions,
        resource_argument="resource_key",
    )

    assert registry._actions_enabled(definition, ToolSettings(), str(uuid4()))
    assert calls == [
        {
            "integration_id": None,
            "resource_key": None,
            "include_scoped": True,
        }
    ]


def test_mcp_adapter_is_hidden_from_normal_integration_catalog() -> None:
    assert "mcp_streamable_http" not in {
        adapter.adapter_type for adapter in integration_adapters()
    }


def test_mcp_availability_defaults_instance_only_and_has_dedicated_admin_api(
    monkeypatch,
) -> None:
    settings = IntegrationSettings()
    load = AsyncMock(return_value=settings)
    monkeypatch.setattr(SettingsStore, "load", load)
    monkeypatch.setattr(SettingsStore, "save", AsyncMock())
    db = AsyncMock(spec=AsyncSession)
    user = SimpleNamespace(id=uuid4(), is_admin=True)
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(mcp_router, prefix="/api")

    async def override_db():
        yield db

    app.dependency_overrides[get_db_session] = override_db
    app.dependency_overrides[require_user] = lambda: user
    app.dependency_overrides[require_admin] = lambda: user

    with TestClient(app) as client:
        initial = client.get("/api/mcp/settings")
        assert client.get("/api/mcp/servers?scope=personal").status_code == 200
        user.is_admin = False
        blocked = client.get("/api/mcp/servers?scope=personal")
        user.is_admin = True
        enabled = client.put("/api/mcp/settings", json={"personalAllowed": True})
        del app.dependency_overrides[require_admin]
        user.is_admin = False
        denied = client.put("/api/mcp/settings", json={"personalAllowed": False})

    assert settings.availability_for("mcp_streamable_http") == IntegrationAdapterAvailability.FULL
    assert initial.json() == {"personalAllowed": False}
    assert blocked.status_code == 409
    assert enabled.json() == {"personalAllowed": True}
    assert denied.status_code == 403
    assert settings.adapter_availability == {
        "mcp_streamable_http": IntegrationAdapterAvailability.FULL
    }


async def test_omitted_mcp_availability_excludes_personal_runtime_server(
    monkeypatch,
) -> None:
    global_server = IntegrationInstance(
        name="Global",
        integration_type="mcp",
        adapter_type="mcp_streamable_http",
        config={"endpoint": "https://global.example.test/rpc", "tools": []},
    )
    personal_server = IntegrationInstance(
        name="Personal",
        integration_type="mcp",
        adapter_type="mcp_streamable_http",
        config={"endpoint": "https://personal.example.test/rpc", "tools": []},
    )
    monkeypatch.setattr(
        SettingsStore,
        "load",
        AsyncMock(return_value=IntegrationSettings(instances=[global_server])),
    )
    monkeypatch.setattr(
        SettingsStore,
        "load_optional",
        AsyncMock(return_value=UserIntegrationSettings(instances=[personal_server])),
    )

    effective = await load_effective_integrations(AsyncMock(), uuid4())

    assert [server.id for server in effective.instances] == [global_server.id]


async def test_failed_refresh_keeps_persisted_catalog(monkeypatch) -> None:
    instance = IntegrationInstance(
        name="Server",
        integration_type="mcp",
        adapter_type="mcp_streamable_http",
        config={
            "endpoint": "https://mcp.example.test/rpc",
            "tools": [{"name": "old", "input_schema": {"type": "object"}}],
        },
    )
    settings = IntegrationSettings(instances=[instance])
    monkeypatch.setattr(SettingsStore, "load", AsyncMock(return_value=settings))
    save = AsyncMock()
    monkeypatch.setattr(SettingsStore, "save", save)
    monkeypatch.setattr(
        McpClient,
        "discover_tools",
        AsyncMock(side_effect=McpClientError("mcp.refreshFailed")),
    )
    db = AsyncMock()

    with pytest.raises(McpClientError, match="mcp.refreshFailed"):
        await McpIntegrationService.refresh(db, instance.id, owner_id=None)

    assert McpConfig.model_validate(instance.config).tools[0].name == "old"
    save.assert_not_awaited()
    db.commit.assert_not_awaited()


def test_api_request_accepts_bearer_over_http() -> None:
    request = McpServerRequest(
        name="Server",
        endpoint="http://192.168.1.30:9000/mcp",
        bearerToken="secret",
    )
    assert request.bearer_token == "secret"


async def test_api_update_allows_retained_bearer_over_http(monkeypatch) -> None:
    instance = IntegrationInstance(
        name="Server",
        integration_type="mcp",
        adapter_type="mcp_streamable_http",
        credential_id=uuid4(),
        config={"endpoint": "http://192.168.1.30:9000/mcp", "tools": []},
    )
    monkeypatch.setattr(mcp_routes, "_owner_id", AsyncMock(return_value=None))
    monkeypatch.setattr(mcp_routes, "_settings", AsyncMock(return_value=IntegrationSettings(instances=[instance])))
    update = AsyncMock(return_value=instance)
    monkeypatch.setattr(McpIntegrationService, "update", update)

    result = await update_mcp_server(
        instance.id,
        McpServerRequest(name="Server", endpoint="http://192.168.1.30:9000/mcp"),
        AsyncMock(),
        "instance",
        SimpleNamespace(is_admin=True),
    )

    assert result["hasBearerToken"] is True
    assert update.await_args.kwargs["endpoint"] == "http://192.168.1.30:9000/mcp"
    assert update.await_args.kwargs["token"] is None


async def test_service_allows_retained_bearer_over_http(monkeypatch) -> None:
    instance = IntegrationInstance(
        name="Server",
        integration_type="mcp",
        adapter_type="mcp_streamable_http",
        credential_id=uuid4(),
        config={"endpoint": "http://192.168.1.30:9000/mcp", "tools": []},
    )
    integrations = IntegrationSettings(instances=[instance])

    async def load(_db, settings_type, **_kwargs):
        return integrations if settings_type is IntegrationSettings else ToolSettings()

    monkeypatch.setattr(
        SettingsStore,
        "load",
        AsyncMock(side_effect=load),
    )
    monkeypatch.setattr(SettingsStore, "save", AsyncMock())
    monkeypatch.setattr(service_module, "connection_token", AsyncMock(return_value="secret"))
    discover = AsyncMock(return_value=("http://192.168.1.30:9000/mcp", []))
    monkeypatch.setattr(McpIntegrationService, "_discover", discover)

    await McpIntegrationService.update(
        AsyncMock(),
        instance.id,
        name="Server",
        endpoint="http://192.168.1.30:9000/mcp",
        token=None,
        remove_token=False,
        enabled=True,
        owner_id=None,
    )

    discover.assert_awaited_once_with("http://192.168.1.30:9000/mcp", "secret")


async def test_endpoint_change_clears_server_and_tool_policy_state(monkeypatch) -> None:
    instance = IntegrationInstance(
        name="Server",
        integration_type="mcp",
        adapter_type="mcp_streamable_http",
        config={
            "endpoint": "https://old.example.test/rpc",
            "tools": [{"name": "lookup", "input_schema": {"type": "object"}}],
        },
    )
    integrations = IntegrationSettings(instances=[instance])
    tools = ToolSettings()
    for permissions in (instance.permissions.private_permissions, instance.permissions.public_permissions):
        permissions.policies = {
            "mcp.call": ToolExecutionPolicy.AUTO
        }
        permissions.resource_policies = {
            mcp_resource_key("lookup"): {"mcp.call": ToolExecutionPolicy.ASK}
        }
    tools.resource_labels[str(instance.id)] = {mcp_resource_key("lookup"): "Lookup"}

    async def load(_db, settings_type, **_kwargs):
        return integrations if settings_type is IntegrationSettings else tools

    monkeypatch.setattr(SettingsStore, "load", AsyncMock(side_effect=load))
    monkeypatch.setattr(SettingsStore, "save", AsyncMock())
    monkeypatch.setattr(
        McpClient,
        "discover_tools",
        AsyncMock(return_value=[{"name": "new", "input_schema": {"type": "object"}}]),
    )
    db = AsyncMock()

    await McpIntegrationService.update(
        db,
        instance.id,
        name="Server",
        endpoint="https://new.example.test/rpc",
        token=None,
        remove_token=False,
        enabled=True,
        owner_id=None,
    )

    assert instance.permissions == IntegrationPermissions()
    assert str(instance.id) not in tools.resource_labels
    db.commit.assert_awaited_once()


@pytest.mark.parametrize("oauth_config", [None, {"scope": "mcp:tools"}])
async def test_same_endpoint_update_prunes_only_removed_tool_state(monkeypatch, oauth_config) -> None:
    endpoint = "https://mcp.example.test/rpc"
    instance = IntegrationInstance(
        name="Server",
        integration_type="mcp",
        adapter_type="mcp_streamable_http",
        config={
            "endpoint": endpoint,
            "tools": [
                {"name": "keep", "input_schema": {"type": "object"}},
                {"name": "remove", "input_schema": {"type": "object"}},
            ],
        },
    )
    integrations = IntegrationSettings(instances=[instance])
    instance.config["oauth"] = oauth_config
    monkeypatch.setattr(service_module, "connection_token", AsyncMock(return_value="secret"))
    tools = ToolSettings()
    keep = mcp_resource_key("keep")
    remove = mcp_resource_key("remove")
    instance.permissions.private_permissions.policies = {
        "mcp.call": ToolExecutionPolicy.AUTO
    }
    instance.permissions.private_permissions.resource_policies = {
        keep: {"mcp.call": ToolExecutionPolicy.AUTO},
        remove: {"mcp.call": ToolExecutionPolicy.DISABLED},
    }
    tools.resource_labels[str(instance.id)] = {keep: "Keep", remove: "Remove"}
    instance.permissions.resource_availability = {keep: True, remove: False}
    instance.profile_permissions[GENERAL_PROFILE_ID] = instance.permissions.model_copy(deep=True)

    async def load(_db, settings_type, **_kwargs):
        return integrations if settings_type is IntegrationSettings else tools

    monkeypatch.setattr(SettingsStore, "load", AsyncMock(side_effect=load))
    monkeypatch.setattr(SettingsStore, "save", AsyncMock())
    monkeypatch.setattr(
        McpClient,
        "discover_tools",
        AsyncMock(return_value=[{"name": "keep", "input_schema": {"type": "object"}}]),
    )

    await McpIntegrationService.update(
        AsyncMock(),
        instance.id,
        name="Renamed",
        endpoint=endpoint,
        token=None,
        remove_token=False,
        enabled=True,
        owner_id=None,
    )

    for permissions in (instance.permissions, instance.profile_permissions[GENERAL_PROFILE_ID]):
        assert permissions.private_permissions.policies == {"mcp.call": ToolExecutionPolicy.AUTO}
        assert permissions.private_permissions.resource_policies == {
            keep: {"mcp.call": ToolExecutionPolicy.AUTO}
        }
        assert permissions.resource_availability == {keep: True}
    assert tools.resource_labels[str(instance.id)] == {keep: "Keep"}
    assert instance.config["oauth"] == oauth_config


async def test_refresh_prunes_removed_tool_state_only(monkeypatch) -> None:
    instance = IntegrationInstance(
        name="Server",
        integration_type="mcp",
        adapter_type="mcp_streamable_http",
        config={
            "endpoint": "https://mcp.example.test/rpc",
            "tools": [
                {"name": "keep", "input_schema": {"type": "object"}},
                {"name": "remove", "input_schema": {"type": "object"}},
            ],
        },
    )
    integrations = IntegrationSettings(instances=[instance])
    tools = ToolSettings()
    keep = mcp_resource_key("keep")
    remove = mcp_resource_key("remove")
    instance.permissions.private_permissions.resource_policies = {
        keep: {"mcp.call": ToolExecutionPolicy.AUTO},
        remove: {"mcp.call": ToolExecutionPolicy.DISABLED},
    }
    tools.resource_labels[str(instance.id)] = {keep: "Keep", remove: "Remove"}

    async def load(_db, settings_type, **_kwargs):
        return integrations if settings_type is IntegrationSettings else tools

    monkeypatch.setattr(SettingsStore, "load", AsyncMock(side_effect=load))
    monkeypatch.setattr(SettingsStore, "save", AsyncMock())
    monkeypatch.setattr(
        McpClient,
        "discover_tools",
        AsyncMock(return_value=[{"name": "keep", "input_schema": {"type": "object"}}]),
    )

    await McpIntegrationService.refresh(AsyncMock(), instance.id, owner_id=None)

    assert instance.permissions.private_permissions.resource_policies == {
        keep: {"mcp.call": ToolExecutionPolicy.AUTO}
    }
    assert tools.resource_labels[str(instance.id)] == {keep: "Keep"}


async def test_credential_decode_failure_maps_to_safe_configuration_error(monkeypatch) -> None:
    instance = IntegrationInstance(
        name="Server",
        integration_type="mcp",
        adapter_type="mcp_streamable_http",
        credential_id=uuid4(),
    )
    monkeypatch.setattr(
        oauth_module, "load_credential", AsyncMock(return_value=SimpleNamespace(kind="mcp.bearer"))
    )
    monkeypatch.setattr(
        oauth_module,
        "read_credential",
        lambda _record: (_ for _ in ()).throw(ValueError("secret raw error")),
    )

    with pytest.raises(McpClientError) as exc_info:
        await oauth_module.connection_token(AsyncMock(), instance)

    assert exc_info.value.code == "mcp.configurationInvalid"


async def test_create_encrypts_bearer_outside_persisted_config(monkeypatch) -> None:
    settings = IntegrationSettings()
    monkeypatch.setattr(SettingsStore, "load", AsyncMock(return_value=settings))
    monkeypatch.setattr(SettingsStore, "save", AsyncMock())
    monkeypatch.setattr(
        McpClient,
        "discover_tools",
        AsyncMock(return_value=[{"name": "lookup", "input_schema": {"type": "object"}}]),
    )
    credential_id = uuid4()
    store = AsyncMock(return_value=SimpleNamespace(id=credential_id))
    monkeypatch.setattr(service_module, "store_credential", store)
    db = AsyncMock()

    instance = await McpIntegrationService.create(
        db,
        name="Server",
        endpoint="http://192.168.1.30:9000/mcp",
        token="secret",
        enabled=True,
        owner_id=None,
    )

    assert instance.credential_id == credential_id
    assert instance.config["endpoint"] == "http://192.168.1.30:9000/mcp"
    assert "secret" not in str(instance.config)
    assert store.await_args.kwargs["payload"] == {"token": "secret"}


def test_sonobin_still_requires_https() -> None:
    with pytest.raises(IntegrationSettingsError, match="mcp.configurationInvalid"):
        SonoBinIntegrationService._validate_connection(
            "http://192.168.1.30:9000/mcp/", "secret"
        )


@pytest.mark.parametrize("base_url", ["https://sonobin.example.test", "https://sonobin.example.test/",
                                      "https://sonobin.example.test/mcp", "https://sonobin.example.test/mcp/"])
async def test_sonobin_create_inherits_permission_defaults(monkeypatch, base_url) -> None:
    integrations = IntegrationSettings()
    tools = ToolSettings()

    async def load(_db, settings_type, **_kwargs):
        return integrations if settings_type is IntegrationSettings else tools

    monkeypatch.setattr(SettingsStore, "load", AsyncMock(side_effect=load))
    monkeypatch.setattr(SettingsStore, "save", AsyncMock())
    monkeypatch.setattr(
        service_module,
        "store_credential",
        AsyncMock(return_value=SimpleNamespace(id=uuid4())),
    )
    monkeypatch.setattr(
        McpClient,
        "discover_tools",
        AsyncMock(
            return_value=[
                {"name": "list_recordings", "input_schema": {"type": "object"}},
                {"name": "delete_recording", "input_schema": {"type": "object"}},
                {"name": "future_tool", "input_schema": {"type": "object"}},
            ]
        ),
    )
    db = AsyncMock()

    instance = await SonoBinIntegrationService.create(
        db,
        name="SonoBin",
        endpoint=base_url,
        token="secret",
        enabled=True,
        owner_id=None,
    )

    assert (instance.integration_type, instance.adapter_type) == ("music", "sonobin")
    assert instance.config["endpoint"] == "https://sonobin.example.test/mcp/"
    assert not any(branch.policies or branch.resource_policies for branch in (instance.permissions.private_permissions, instance.permissions.public_permissions))
    assert tools == ToolSettings()
    contributions = SONOBIN_ADAPTER.contributions(instance).tools
    assert contributions[0].label == "Aufnahmen auflisten"
    assert contributions[2].label == "future_tool"
    db.commit.assert_awaited_once()


def test_generic_mcp_servers_are_available_only_from_dedicated_route(monkeypatch) -> None:
    instance = IntegrationInstance(
        name="Server",
        integration_type="mcp",
        adapter_type="mcp_streamable_http",
        config={
            "endpoint": "https://mcp.example.test/rpc",
            "tools": [{"name": "lookup", "input_schema": {"type": "object"}}],
        },
    )
    monkeypatch.setattr(
        SettingsStore, "load", AsyncMock(return_value=IntegrationSettings(instances=[instance]))
    )
    db = AsyncMock(spec=AsyncSession)
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(mcp_router, prefix="/api")

    async def override_db():
        yield db

    app.dependency_overrides[get_db_session] = override_db
    app.dependency_overrides[require_user] = lambda: SimpleNamespace(
        id=uuid4(), is_admin=True
    )

    response = TestClient(app).get("/api/mcp/servers")

    assert response.status_code == 200
    assert response.json()["servers"][0]["tools"][0]["name"] == "lookup"
    assert all(adapter.adapter_type != "mcp_streamable_http" for adapter in integration_adapters())
    assert any(adapter.adapter_type == "sonobin" for adapter in integration_adapters())


@pytest.mark.asyncio
async def test_visible_mcp_servers_include_only_shared_and_owned_entries(monkeypatch) -> None:
    shared = IntegrationInstance(
        name="Shared", integration_type="mcp", adapter_type="mcp_streamable_http",
        config={"endpoint": "https://shared.example/mcp", "tools": []},
    )
    personal = shared.model_copy(update={
        "id": uuid4(), "name": "Mine",
        "config": {"endpoint": "https://mine.example/mcp", "tools": []},
    })
    user_id = uuid4()
    load = AsyncMock(return_value=IntegrationSettings(
        instances=[shared], adapter_availability={"mcp_streamable_http": "instance_only"}
    ))
    load_optional = AsyncMock(return_value=UserIntegrationSettings(instances=[personal]))
    monkeypatch.setattr(SettingsStore, "load", load)
    monkeypatch.setattr(SettingsStore, "load_optional", load_optional)

    result = await mcp_routes.list_visible_mcp_servers(AsyncMock(), SimpleNamespace(id=user_id, is_admin=False))

    assert [(item["name"], item["scope"]) for item in result["servers"]] == [
        ("Shared", "instance"), ("Mine", "personal")
    ]
    assert all("bearerToken" not in item for item in result["servers"])
    assert [item["available"] for item in result["servers"]] == [True, False]
    assert load_optional.await_args.kwargs["user_id"] == user_id
