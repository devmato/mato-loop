import asyncio
import base64
import hashlib
import json
import logging
import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from mcp.types import LATEST_PROTOCOL_VERSION
from pydantic import ValidationError
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from starlette.requests import Request

from mato_loop_backend.api.errors import ApiError
from mato_loop_backend.api.routes import mcp as routes
from mato_loop_backend.config import get_runtime_config
from mato_loop_backend.integrations.mcp import adapter, oauth, service
from mato_loop_backend.integrations.mcp.client import McpClientError, _BoundedTransport
from mato_loop_backend.integrations.service import IntegrationSettingsError
from mato_loop_backend.models import Credential
from mato_loop_backend.security import hash_token, store_credential
from mato_loop_backend.settings import (
    IntegrationInstance,
    IntegrationSettings,
    SettingsStore,
    ToolSettings,
    UserIntegrationSettings,
)

ENDPOINT = "https://mcp.example.test/mcp"
ISSUER = "https://idp.example.test/auth/v1"
CALLBACK = "https://matoloop.example.test" + oauth.CALLBACK_PATH


@pytest.fixture
def transport(monkeypatch):
    def install(handler):
        original = _BoundedTransport.__init__
        monkeypatch.setattr(
            _BoundedTransport,
            "__init__",
            lambda self, transport=None, authentication_failed=None, **kwargs: original(
                self, httpx.MockTransport(handler), authentication_failed, **kwargs
            ),
        )

    return install


@pytest.fixture
def payload():
    return dict(
        endpoint=ENDPOINT,
        issuer=ISSUER,
        resource=ENDPOINT,
        token_endpoint=ISSUER + "/token",
        client_info=dict(
            client_id="client",
            issuer=ISSUER,
            redirect_uris=[CALLBACK],
            token_endpoint_auth_method="none",
        ),
        tokens=dict(access_token="old", refresh_token="refresh"),
        expires_at=0,
    )


@pytest.mark.parametrize(
    "mode",
    [
        "dynamic",
        "none",
        "client_secret_basic",
        "client_secret_post",
        "streaming",
        "redirected",
        "cimd",
        "ha_http",
        "lan_dynamic",
        "large_catalog",
        "invalid_catalog",
        "invalid_token_response",
    ],
)
async def test_sdk_login_and_discovery(transport, mode, caplog):
    requests, authorization = [], {}
    endpoint = "http://192.168.1.5:8123/mcp" if mode == "lan_dynamic" else ENDPOINT
    issuer = "http://192.168.1.5:8123/auth/v1" if mode == "lan_dynamic" else ISSUER
    callback = "http://192.168.1.30:10020" + oauth.CALLBACK_PATH if mode == "lan_dynamic" else CALLBACK
    routes.McpOAuthStartRequest(name="MCP", endpoint=endpoint, oauth={})

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield ("event: message\ndata: " + json.dumps(self.message) + "\n\n").encode()
            await asyncio.Event().wait()

    def handle(request):
        requests.append(request)
        path = request.url.path
        if str(request.url) == endpoint + "/":
            return httpx.Response(307, headers={"location": endpoint})
        if "oauth-protected-resource" in path:
            data = dict(
                resource=endpoint, authorization_servers=[issuer], scopes_supported=["mcp:tools"]
            )
        elif path == "/auth/v1/.well-known/openid-configuration":
            data = dict(
                issuer=issuer,
                authorization_endpoint=issuer + "/authorize",
                token_endpoint=issuer + "/token",
                registration_endpoint=None if mode in {"cimd", "ha_http"} else issuer + "/register",
                client_id_metadata_document_supported=mode in {"cimd", "ha_http", "lan_dynamic"},
                code_challenge_methods_supported=["S256"],
                grant_types_supported=["authorization_code"],
            )
        elif path.endswith("/register"):
            assert mode not in {"cimd", "ha_http"}
            data = {**json.loads(request.content), "client_id": "registered"}
            assert data["grant_types"] == ["authorization_code"]
        elif path.endswith("/token"):
            form = parse_qs(request.content.decode())
            digest = hashlib.sha256(form["code_verifier"][0].encode()).digest()
            assert (
                base64.urlsafe_b64encode(digest).decode().rstrip("=")
                == authorization["code_challenge"][0]
            )
            assert form["resource"] == [endpoint]
            data = dict(access_token="access", token_type="Bearer", expires_in=120)
            if mode == "invalid_token_response":
                data["token_type"] = {"private-value": "private-token"}
        elif str(request.url) == endpoint:
            if request.headers.get("authorization") != "Bearer access":
                return httpx.Response(401)
            if request.method != "POST":
                return httpx.Response(405)
            message = json.loads(request.content)
            if "id" not in message:
                return httpx.Response(202)
            tool = {"name": "lookup", "inputSchema": {"type": "object"}}
            if mode == "invalid_catalog":
                tool["inputSchema"]["type"] = "private-value"
            result = (
                dict(
                    protocolVersion=LATEST_PROTOCOL_VERSION,
                    capabilities={"tools": {}},
                    serverInfo={"name": "test", "version": "1"},
                )
                if message["method"] == "initialize"
                else {"tools": [tool]}
            )
            if mode == "large_catalog" and message["method"] == "tools/list":
                page = int(message.get("params", {}).get("cursor", 0))
                result = {"tools": [
                    dict(tool, name=f"lookup_{page}_{index}", title="Title" * 40,
                         description="Details" * 1000,
                         inputSchema={"type": "object", "description": "Schema" * 12_000})
                    for index in range(85 if page == 0 else 1)
                ]}
                if page < 20:
                    result["nextCursor"] = str(page + 1)
            data = dict(jsonrpc="2.0", id=message["id"], result=result)
            if mode == "streaming" and message["method"] == "initialize":
                stream = Stream()
                stream.message = data
                return httpx.Response(
                    200, headers={"content-type": "text/event-stream"}, stream=stream
                )
        else:
            return httpx.Response(404)
        if mode == "large_catalog":
            return httpx.Response(
                200, headers={"content-type": "application/json"},
                stream=httpx.ByteStream(json.dumps(data).encode()),
            )
        return httpx.Response(200, json=data)

    transport(handle)
    dynamic = mode in {"dynamic", "streaming", "redirected", "cimd", "lan_dynamic", "large_catalog", "invalid_catalog", "invalid_token_response"}
    config = oauth.OAuthConfig(
        client_id=None
        if dynamic
        else "http://192.168.1.30:10020"
        if mode == "ha_http"
        else "client",
        token_endpoint_auth_method="none" if dynamic or mode == "ha_http" else mode,
        scope="mcp:tools",
    )
    attempt = oauth.LoginAttempt(
        uuid4(),
        "session",
        None,
        dict(
            name="MCP",
            endpoint=endpoint + "/" if mode == "redirected" else endpoint,
            enabled=True,
            oauth=config.model_dump(),
        ),
        None,
        None,
    )
    manager = oauth.OAuthLoginManager()
    try:
        await manager.start(
            attempt,
            "secret" if mode.startswith("client_secret") else None,
            config.client_id + oauth.CALLBACK_PATH if mode == "ha_http" else callback,
        )
        authorization.update(parse_qs(urlsplit(attempt.authorization_url).query))
        if mode == "ha_http":
            assert authorization["redirect_uri"] == [
                "http://192.168.1.30:10020" + oauth.CALLBACK_PATH
            ]
        if mode == "cimd":
            assert authorization["client_id"] == [
                "https://matoloop.example.test/api/mcp/oauth/client-metadata"
            ]
        assert authorization["code_challenge_method"] == ["S256"] and authorization["scope"] == [
            "mcp:tools"
        ]
        manager.receive_callback(authorization["state"][0], "code", None)
        await asyncio.wait_for(attempt.task, 3)
        if mode == "invalid_catalog":
            assert (
                "phase=catalog_validation" in caplog.text
                and '"loc": ["tools", 0, "input_schema"]' in caplog.text
                and "value_error" in caplog.text
                and "private-value" not in caplog.text
            )
        elif mode == "invalid_token_response":
            assert (
                '"loc": ["token_type"]' in caplog.text
                and "literal_error" in caplog.text
                and "private-value" not in caplog.text
                and "private-token" not in caplog.text
            )
        elif mode == "large_catalog":
            assert attempt.error_code is None and len(attempt.tools) == 105
        else:
            assert attempt.error_code is None and attempt.tools[0]["name"] == "lookup"
        token_request = next(request for request in requests if request.url.path.endswith("/token"))
        if mode == "client_secret_basic":
            assert (
                token_request.headers["authorization"]
                == "Basic " + base64.b64encode(b"client:secret").decode()
            )
        elif mode == "client_secret_post":
            assert parse_qs(token_request.content.decode())["client_secret"] == ["secret"]
    finally:
        await manager.shutdown()


async def test_entra_v2_login_uses_v2_scope_without_resource(transport):
    tenant = "6cfacb1f-872d-4978-8672-97e207e24e71"
    issuer = f"https://login.microsoftonline.com/{tenant}/v2.0"
    scope = "https://docs.example.test/api/mcp/mcp.access"
    endpoint = "https://docs.example.test/api/mcp"
    requests, authorization = [], {}

    def reply(request):
        requests.append(request)
        path = request.url.path
        if "oauth-protected-resource" in path:
            data = {
                "resource": endpoint,
                "authorization_servers": [issuer],
                "scopes_supported": [scope],
            }
        elif path == f"/{tenant}/v2.0/.well-known/openid-configuration":
            data = {
                "issuer": issuer,
                "authorization_endpoint": f"{issuer}/authorize",
                "token_endpoint": f"{issuer}/token",
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["client_secret_post"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
            }
        elif path.endswith("/token"):
            form = parse_qs(request.content.decode())
            assert "resource" not in form
            assert form["client_secret"] == ["secret"]
            data = {
                "access_token": "access",
                "refresh_token": "refresh",
                "token_type": "Bearer",
                "expires_in": 120,
            }
        elif str(request.url) == endpoint:
            if request.headers.get("authorization") != "Bearer access":
                return httpx.Response(401)
            message = json.loads(request.content)
            if "id" not in message:
                return httpx.Response(202)
            result = (
                {
                    "protocolVersion": LATEST_PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "test", "version": "1"},
                }
                if message["method"] == "initialize"
                else {"tools": []}
            )
            data = {"jsonrpc": "2.0", "id": message["id"], "result": result}
        else:
            return httpx.Response(404)
        return httpx.Response(200, json=data)

    transport(reply)
    config = oauth.OAuthConfig(
        client_id="client",
        token_endpoint_auth_method="client_secret_post",
        scope=scope,
    )
    attempt = oauth.LoginAttempt(
        uuid4(),
        "session",
        None,
        {
            "name": "TBS Docs",
            "endpoint": endpoint,
            "enabled": True,
            "oauth": config.model_dump(),
        },
        None,
        None,
    )
    manager = oauth.OAuthLoginManager()
    try:
        await manager.start(attempt, "secret", CALLBACK)
        authorization.update(parse_qs(urlsplit(attempt.authorization_url).query))
        assert "resource" not in authorization
        assert set(authorization["scope"][0].split()) == {scope, "offline_access"}
        manager.receive_callback(authorization["state"][0], "code", None)
        await asyncio.wait_for(attempt.task, 3)
        assert attempt.error_code is None
        assert attempt.tokens.refresh_token == "refresh"
        assert any(request.url.path.endswith("/token") for request in requests)
    finally:
        await manager.shutdown()


@pytest.fixture
async def login(monkeypatch, payload):
    manager = oauth.OAuthLoginManager()
    user = SimpleNamespace(id=uuid4(), is_admin=True)
    attempt = oauth.LoginAttempt(user.id, hash_token("session"), None, {}, None, None)
    attempt.payload, attempt.tools, attempt.state = payload, [], "state"
    attempt.callback = asyncio.get_running_loop().create_future()
    manager.attempts[attempt.id] = attempt
    cookie = get_runtime_config().session_cookie_name
    request = Request({"type": "http", "headers": [(b"cookie", f"{cookie}=session".encode())]})
    monkeypatch.setattr(routes, "oauth_logins", manager)
    monkeypatch.setattr(SettingsStore, "load", AsyncMock(return_value=IntegrationSettings()))
    return manager, attempt, request, user, AsyncMock()


@pytest.mark.parametrize("failure", ["user", "session", "state", "replay", "expired"])
async def test_login_binding_and_single_use(login, failure):
    manager, attempt, _request, user, _db = login
    if failure == "expired":
        attempt.created_at -= oauth.LOGIN_TTL
    if failure == "replay":
        manager.receive_callback("state", "code", None)
    with pytest.raises(McpClientError, match="mcp.oauthExpired"):
        if failure in {"state", "replay"}:
            manager.receive_callback("wrong" if failure == "state" else "state", "code", None)
        else:
            manager.get(
                attempt.id,
                uuid4() if failure == "user" else user.id,
                "wrong" if failure == "session" else hash_token("session"),
            )
    await manager.shutdown()


async def test_public_callback_cannot_persist(login):
    manager, attempt, _request, _user, db = login
    app = FastAPI()
    app.include_router(routes.callback_router, prefix="/api")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="https://matoloop.example.test"
    ) as client:
        response = await client.get(oauth.CALLBACK_PATH, params={"state": "state", "code": "code"})
    assert response.status_code == 303 and "code=" not in response.headers["location"]
    db.commit.assert_not_awaited()
    await manager.shutdown()


@pytest.mark.parametrize("failure", ["pending", "denied", "admin", "changed"])
async def test_failed_finish_keeps_connection(login, monkeypatch, failure):
    manager, attempt, request, user, db = login
    if failure in {"pending", "denied"}:
        attempt.payload = None
        attempt.error_code = "mcp.oauthDenied" if failure == "denied" else None
    user.is_admin = failure != "admin"
    if failure == "changed":
        attempt.integration_id, attempt.revision = uuid4(), {}
    else:
        persist = AsyncMock()
        monkeypatch.setattr(service.McpIntegrationService, "complete_oauth", persist)
    with pytest.raises(ApiError):
        await routes.finish_mcp_oauth(attempt.id, request, db, user)
    db.commit.assert_not_awaited()
    if failure != "changed":
        persist.assert_not_awaited()
    await manager.shutdown()


@pytest.mark.parametrize("owner_id", [None, uuid4()])
async def test_oauth_credential_uses_connection_owner(monkeypatch, payload, owner_id):
    settings = UserIntegrationSettings() if owner_id else IntegrationSettings()
    monkeypatch.setattr(SettingsStore, "load", AsyncMock(return_value=settings))
    monkeypatch.setattr(SettingsStore, "save", AsyncMock())
    store = AsyncMock(return_value=SimpleNamespace(id=uuid4()))
    monkeypatch.setattr(service, "store_credential", store)
    attempt = oauth.LoginAttempt(
        uuid4(),
        "session",
        owner_id,
        dict(name="MCP", endpoint=ENDPOINT, enabled=True, oauth={}),
        None,
        None,
    )
    attempt.payload, attempt.tools = payload, []
    await service.McpIntegrationService.complete_oauth(AsyncMock(), attempt)
    assert store.await_args.kwargs["owner_id"] == owner_id


async def test_user_oauth_completion_cannot_replace_global_catalog(monkeypatch, payload):
    user_id = uuid4()
    instance = IntegrationInstance(
        name="MCP",
        integration_type="mcp",
        adapter_type="mcp_streamable_http",
        credential_id=uuid4(),
        config={
            "endpoint": ENDPOINT,
            "oauth": {"client_id": "client", "user_scoped": True},
            "tools": [],
        },
    )
    settings = IntegrationSettings(instances=[instance])
    monkeypatch.setattr(service, "_load_integrations", AsyncMock(return_value=settings))
    monkeypatch.setattr(service, "_load_tools", AsyncMock(return_value=ToolSettings()))
    monkeypatch.setattr(service, "_save_settings", AsyncMock())
    monkeypatch.setattr(service, "find_user_oauth_credential", AsyncMock(return_value=None))
    store = AsyncMock(return_value=SimpleNamespace(id=uuid4()))
    monkeypatch.setattr(service, "store_credential", store)
    attempt = oauth.LoginAttempt(
        user_id,
        "session",
        None,
        {
            "name": "MCP",
            "endpoint": ENDPOINT,
            "enabled": True,
            "oauth": {"client_id": "client", "user_scoped": True},
        },
        instance.id,
        instance.model_dump(mode="json"),
    )
    attempt.payload = payload
    attempt.tools = [{"name": "attacker_tool", "input_schema": {"type": "object"}}]

    result = await service.McpIntegrationService.complete_oauth(AsyncMock(), attempt)

    assert result.credential_id == instance.credential_id
    assert result.config["tools"] == []
    assert store.await_args.kwargs["owner_id"] == user_id
    assert store.await_args.kwargs["metadata"] == {
        "integration_id": str(instance.id)
    }
    assert "client_secret" not in store.await_args.kwargs["payload"]["client_info"]


async def test_admin_bootstrap_updates_global_catalog_and_canonical_endpoint(
    monkeypatch, payload
):
    user_id = uuid4()
    canonical = ENDPOINT + "/"
    template = SimpleNamespace(
        id=uuid4(),
        user_id=None,
        kind=oauth.OAUTH_CLIENT_CREDENTIAL_KIND,
        label="MCP",
    )
    instance = IntegrationInstance(
        name="MCP",
        integration_type="mcp",
        adapter_type="mcp_streamable_http",
        credential_id=template.id,
        config={
            "endpoint": ENDPOINT,
            "oauth": {
                "client_id": "client",
                "user_scoped": True,
            },
            "tools": [],
        },
    )
    settings = IntegrationSettings(instances=[instance])
    monkeypatch.setattr(service, "_load_integrations", AsyncMock(return_value=settings))
    monkeypatch.setattr(service, "_load_tools", AsyncMock(return_value=ToolSettings()))
    monkeypatch.setattr(service, "_save_settings", AsyncMock())
    monkeypatch.setattr(
        service, "find_user_oauth_credential", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(service, "load_credential", AsyncMock(return_value=template))
    monkeypatch.setattr(
        service,
        "read_credential",
        lambda _record: {
            "catalog_initialized": False,
            "client_info": {"client_id": "client"},
        },
    )
    store = AsyncMock(return_value=SimpleNamespace(id=uuid4()))
    monkeypatch.setattr(service, "store_credential", store)
    clear_policies = AsyncMock()
    monkeypatch.setattr(service.McpIntegrationService, "_clear_binding_policies", clear_policies)
    attempt = oauth.LoginAttempt(
        user_id,
        "session",
        None,
        {
            "name": "MCP",
            "endpoint": canonical,
            "enabled": True,
            "oauth": {"client_id": "client", "user_scoped": True},
        },
        instance.id,
        instance.model_dump(mode="json"),
        bootstrap=True,
    )
    attempt.payload = {**payload, "endpoint": canonical}
    attempt.tools = [{"name": "lookup", "input_schema": {"type": "object"}}]

    result = await service.McpIntegrationService.complete_oauth(AsyncMock(), attempt)

    assert result.config["endpoint"] == canonical
    assert result.config["tools"][0]["name"] == "lookup"
    assert any(
        call.kwargs.get("payload", {}).get("catalog_initialized") is True
        for call in store.await_args_list
    )
    clear_policies.assert_awaited()


async def test_user_login_rejects_uncanonical_global_endpoint(monkeypatch, payload):
    instance = IntegrationInstance(
        name="MCP",
        integration_type="mcp",
        adapter_type="mcp_streamable_http",
        credential_id=uuid4(),
        config={
            "endpoint": ENDPOINT,
            "oauth": {"client_id": "client", "user_scoped": True},
            "tools": [{"name": "lookup", "input_schema": {"type": "object"}}],
        },
    )
    settings = IntegrationSettings(instances=[instance])
    monkeypatch.setattr(service, "_load_integrations", AsyncMock(return_value=settings))
    monkeypatch.setattr(
        service, "find_user_oauth_credential", AsyncMock(return_value=None)
    )
    attempt = oauth.LoginAttempt(
        uuid4(),
        "session",
        None,
        {
            "name": "MCP",
            "endpoint": ENDPOINT + "/",
            "enabled": True,
            "oauth": {"client_id": "client", "user_scoped": True},
        },
        instance.id,
        instance.model_dump(mode="json"),
    )
    attempt.payload = {**payload, "endpoint": ENDPOINT + "/"}
    attempt.tools = []

    with pytest.raises(IntegrationSettingsError, match="integration.changed"):
        await service.McpIntegrationService.complete_oauth(AsyncMock(), attempt)


async def test_global_oauth_configuration_keeps_only_client_template(monkeypatch):
    settings = IntegrationSettings()
    monkeypatch.setattr(service, "_load_integrations", AsyncMock(return_value=settings))
    monkeypatch.setattr(service, "_save_settings", AsyncMock())
    credential = SimpleNamespace(id=uuid4())
    store = AsyncMock(return_value=credential)
    monkeypatch.setattr(service, "store_credential", store)

    instance = await service.McpIntegrationService.create_user_scoped_oauth(
        AsyncMock(),
        name="MCP",
        endpoint=ENDPOINT,
        enabled=True,
        oauth={
            "client_id": "client",
            "token_endpoint_auth_method": "client_secret_post",
            "scope": "mcp:tools",
            "user_scoped": True,
        },
        client_secret="secret",
        owner_id=None,
    )

    assert instance.credential_id == credential.id
    assert instance.config["oauth"]["user_scoped"] is True
    assert store.await_args.kwargs["owner_id"] is None
    assert store.await_args.kwargs["kind"] == oauth.OAUTH_CLIENT_CREDENTIAL_KIND
    assert "tokens" not in store.await_args.kwargs["payload"]


def test_user_scoped_oauth_requires_registered_client():
    with pytest.raises(ValidationError, match="registered client"):
        oauth.OAuthConfig(user_scoped=True)


async def test_transition_existing_server_to_user_scoped_oauth(monkeypatch):
    instance = IntegrationInstance(
        name="MCP",
        integration_type="mcp",
        adapter_type="mcp_streamable_http",
        credential_id=uuid4(),
        config={
            "endpoint": ENDPOINT,
            "tools": [{"name": "lookup", "input_schema": {"type": "object"}}],
        },
    )
    settings = IntegrationSettings(instances=[instance])
    monkeypatch.setattr(service, "_load_integrations", AsyncMock(return_value=settings))
    monkeypatch.setattr(service, "_save_settings", AsyncMock())
    monkeypatch.setattr(
        service, "delete_user_oauth_credentials", AsyncMock(return_value=0)
    )
    monkeypatch.setattr(service, "delete_credential", AsyncMock(return_value=True))
    monkeypatch.setattr(
        service.McpIntegrationService,
        "_clear_binding_policies",
        AsyncMock(),
    )
    template = SimpleNamespace(id=uuid4())
    monkeypatch.setattr(service, "store_credential", AsyncMock(return_value=template))

    result = await service.McpIntegrationService.transition_to_user_scoped_oauth(
        AsyncMock(),
        instance.id,
        name="MCP",
        endpoint=ENDPOINT,
        enabled=True,
        oauth={
            "client_id": "client",
            "token_endpoint_auth_method": "client_secret_post",
            "scope": "mcp:tools",
            "user_scoped": True,
        },
        client_secret="secret",
        owner_id=None,
    )

    assert result.credential_id == template.id
    assert result.config["oauth"]["user_scoped"] is True
    assert result.config["tools"][0]["name"] == "lookup"


async def test_transition_user_scoped_oauth_to_shared(monkeypatch, payload):
    old_credential_id = uuid4()
    instance = IntegrationInstance(
        name="MCP",
        integration_type="mcp",
        adapter_type="mcp_streamable_http",
        credential_id=old_credential_id,
        config={
            "endpoint": ENDPOINT,
            "oauth": {"client_id": "client", "user_scoped": True},
            "tools": [{"name": "lookup", "input_schema": {"type": "object"}}],
        },
    )
    settings = IntegrationSettings(instances=[instance])
    monkeypatch.setattr(service, "_load_integrations", AsyncMock(return_value=settings))
    monkeypatch.setattr(service, "_save_settings", AsyncMock())
    delete_users = AsyncMock(return_value=1)
    monkeypatch.setattr(service, "delete_user_oauth_credentials", delete_users)
    monkeypatch.setattr(service, "delete_credential", AsyncMock(return_value=True))
    monkeypatch.setattr(
        service.McpIntegrationService,
        "_clear_binding_policies",
        AsyncMock(),
    )
    store = AsyncMock(return_value=SimpleNamespace(id=uuid4()))
    monkeypatch.setattr(service, "store_credential", store)
    attempt = oauth.LoginAttempt(
        uuid4(),
        "session",
        None,
        {
            "name": "MCP",
            "endpoint": ENDPOINT,
            "enabled": True,
            "oauth": {"client_id": "client", "user_scoped": False},
        },
        instance.id,
        instance.model_dump(mode="json"),
    )
    attempt.payload, attempt.tools = payload, [
        {"name": "lookup", "input_schema": {"type": "object"}}
    ]
    db = AsyncMock()

    result = await service.McpIntegrationService.complete_oauth(db, attempt)

    assert result.config["oauth"]["user_scoped"] is False
    assert store.await_args.kwargs["owner_id"] is None
    delete_users.assert_awaited_once_with(db, instance.id)


async def test_user_scoped_token_requires_matching_user(monkeypatch, payload, transport):
    payload["expires_at"] = 0
    instance = IntegrationInstance(
        name="MCP",
        integration_type="mcp",
        adapter_type="mcp_streamable_http",
        credential_id=uuid4(),
        config={
            "endpoint": ENDPOINT,
            "oauth": {"client_id": "client", "user_scoped": True},
        },
    )
    owner = uuid4()
    credential = SimpleNamespace(
        id=uuid4(),
        user_id=owner,
        kind=oauth.OAUTH_CREDENTIAL_KIND,
        label="MCP",
    )
    db = AsyncMock()
    db.__aenter__.return_value = db
    db.__aexit__.return_value = False
    monkeypatch.setattr(oauth, "AsyncSessionFactory", lambda: db)
    lookup = AsyncMock(return_value=credential)
    monkeypatch.setattr(oauth, "find_user_oauth_credential", lookup)
    template = SimpleNamespace(
        id=uuid4(),
        user_id=None,
        kind=oauth.OAUTH_CLIENT_CREDENTIAL_KIND,
        label="MCP",
    )
    monkeypatch.setattr(oauth, "load_credential", AsyncMock(return_value=template))
    monkeypatch.setattr(
        oauth,
        "read_credential",
        lambda record: (
            {
                "client_info": {
                    "client_id": "client",
                    "client_secret": "secret",
                    "token_endpoint_auth_method": "client_secret_post",
                }
            }
            if record is template
            else payload
        ),
    )
    save = AsyncMock()
    monkeypatch.setattr(oauth, "_save_oauth_credential", save)
    requests = []

    def reply(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={"access_token": "new", "expires_in": 60, "refresh_token": "refresh"},
        )

    transport(reply)
    assert await oauth.oauth_token(instance, owner) == "new"
    lookup.assert_awaited_once_with(db, instance, owner, for_update=True)
    assert parse_qs(requests[0].content.decode())["client_secret"] == ["secret"]
    with pytest.raises(McpClientError, match="mcp.authorizationRequired"):
        await oauth.oauth_token(instance)


async def test_rebind_during_token_preparation_stops_dispatch(monkeypatch):
    instance = IntegrationInstance(
        name="MCP",
        integration_type="mcp",
        adapter_type="mcp_streamable_http",
        credential_id=uuid4(),
        config={
            "endpoint": ENDPOINT,
            "oauth": {},
            "tools": [{"name": "lookup", "input_schema": {"type": "object"}}],
        },
    )
    monkeypatch.setattr(adapter, "connection_token", AsyncMock(return_value="token"))
    current = instance.model_copy(update={"credential_id": uuid4()})
    context = SimpleNamespace(
        db=AsyncMock(),
        session=SimpleNamespace(user_id=uuid4()),
        integration_settings=IntegrationSettings(instances=[instance]),
        current_integrations=AsyncMock(return_value=IntegrationSettings(instances=[current])),
    )
    assert (
        await adapter.MCP_ADAPTER.contributions(instance).tools[0].handler({}, context)
    ).is_error


@pytest.mark.parametrize("operation", ["refresh", "update"])
async def test_slash_recovery_keeps_oauth_credential_usable(monkeypatch, transport, payload, operation):
    def reply(request):
        if str(request.url) == ENDPOINT:
            return httpx.Response(404)
        if request.method != "POST":
            return httpx.Response(405)
        message = json.loads(request.content)
        if "id" not in message:
            return httpx.Response(202)
        result = (
            dict(protocolVersion=LATEST_PROTOCOL_VERSION, capabilities={"tools": {}}, serverInfo={"name": "test", "version": "1"})
            if message["method"] == "initialize" else {"tools": []}
        )
        return httpx.Response(200, json=dict(jsonrpc="2.0", id=message["id"], result=result))

    transport(reply)
    payload["expires_at"] = time.time() + 3600
    credential = SimpleNamespace(id=uuid4(), user_id=None, kind=oauth.OAUTH_CREDENTIAL_KIND, label="Server")
    instance = IntegrationInstance(name="Server", integration_type="mcp", adapter_type="mcp_streamable_http",
                                   credential_id=credential.id, config={"endpoint": ENDPOINT, "oauth": {}})
    db = AsyncMock()
    db.scalar.return_value = credential
    db.__aenter__.return_value = db
    monkeypatch.setattr(oauth, "AsyncSessionFactory", lambda: db)
    monkeypatch.setattr(oauth, "read_credential", lambda _record: payload)
    monkeypatch.setattr(oauth, "store_credential", AsyncMock(side_effect=lambda _db, **kwargs: payload.update(kwargs["payload"])))
    monkeypatch.setattr(service, "_load_integrations", AsyncMock(return_value=IntegrationSettings(instances=[instance])))
    monkeypatch.setattr(service, "_load_tools", AsyncMock(return_value=ToolSettings()))
    monkeypatch.setattr(service, "_save_settings", AsyncMock())
    if operation == "refresh":
        await service.McpIntegrationService.refresh(db, instance.id, owner_id=None)
    else:
        await service.McpIntegrationService.update(db, instance.id, name="Server", endpoint=ENDPOINT,
                                                 token=None, remove_token=False, enabled=True, owner_id=None)
    assert await oauth.oauth_token(instance) == "old"


async def test_oauth_endpoint_rebind_cannot_expand_resource(monkeypatch, payload):
    credential = SimpleNamespace(kind=oauth.OAUTH_CREDENTIAL_KIND)
    db = AsyncMock()
    db.scalar.return_value = credential
    instance = IntegrationInstance(name="Server", integration_type="mcp", adapter_type="mcp_streamable_http",
                                   credential_id=uuid4(), config={"endpoint": ENDPOINT, "oauth": {}})
    monkeypatch.setattr(oauth, "read_credential", lambda _record: payload)
    with pytest.raises(McpClientError, match="mcp.oauthConfigurationInvalid"):
        await oauth.rebind_oauth_endpoint(db, instance, "https://other.example.test/mcp/")


async def test_duplicate_finish_does_not_cancel_original(login, monkeypatch):
    manager, attempt, request, user, db = login
    started, release = asyncio.Event(), asyncio.Event()

    async def persist(_db, current):
        started.set()
        await release.wait()
        assert current.payload is not None
        return IntegrationInstance(
            name="MCP",
            integration_type="mcp",
            adapter_type="mcp_streamable_http",
            config={"endpoint": ENDPOINT},
        )

    monkeypatch.setattr(service.McpIntegrationService, "complete_oauth", persist)
    first = asyncio.create_task(routes.finish_mcp_oauth(attempt.id, request, db, user))
    await started.wait()
    with pytest.raises(ApiError):
        await routes.finish_mcp_oauth(attempt.id, request, db, user)
    release.set()
    await first
    await manager.shutdown()


@pytest.mark.parametrize("status", [200, 400, 503])
async def test_refresh_preserves_token_and_distinguishes_failures(transport, payload, status):
    transport(
        lambda _request: httpx.Response(status, json={"access_token": "new", "expires_in": 60})
    )
    if status != 200:
        with pytest.raises(
            McpClientError,
            match="mcp.authorizationRequired" if status == 400 else "mcp.unavailable",
        ):
            await oauth.refresh_oauth_payload(payload)
    else:
        result = await oauth.refresh_oauth_payload(payload)
        assert result["tokens"]["refresh_token"] == "refresh" and result["expires_at"] > time.time()
        assert await oauth.refresh_oauth_payload(result) is result


async def test_entra_v2_refresh_omits_resource(transport, payload):
    issuer = "https://login.microsoftonline.com/tenant/v2.0"
    payload.update(
        issuer=issuer,
        token_endpoint=issuer + "/token",
        client_info={
            **payload["client_info"],
            "issuer": issuer,
            "token_endpoint_auth_method": "client_secret_post",
            "client_secret": "secret",
        },
    )
    requests = []

    def reply(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={"access_token": "new", "expires_in": 60, "refresh_token": "refresh"},
        )

    transport(reply)
    await oauth.refresh_oauth_payload(payload)
    form = parse_qs(requests[0].content.decode())
    assert "resource" not in form
    assert form["client_secret"] == ["secret"]


async def test_expired_without_refresh_requires_login(payload):
    payload["tokens"].pop("refresh_token")
    with pytest.raises(McpClientError, match="mcp.authorizationRequired"):
        await oauth.refresh_oauth_payload(payload)


@pytest.mark.parametrize(
    "url", ["https://user:secret@example.test", "file:///secret"]
)
def test_oauth_url_boundary(url):
    with pytest.raises(McpClientError, match="mcp.oauthConfigurationInvalid"):
        oauth.validate_oauth_url(url)


def test_callback_log_redaction():
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        "%s %s %s %s %s",
        ("client", "GET", oauth.CALLBACK_PATH + "?code=private", "1.1", 303),
        None,
    )
    oauth._redact_callback_log(record)
    assert "private" not in record.getMessage()


@pytest.mark.skipif(
    os.getenv("MATO_LOOP_MCP_DB_TEST") != "1", reason="Requires development PostgreSQL"
)
async def test_concurrent_refresh_uses_token_once(transport, payload, monkeypatch):
    calls = []

    def refresh(request):
        calls.append(request)
        return httpx.Response(
            200, json={"access_token": "new", "expires_in": 60, "refresh_token": "rotated"}
        )

    transport(refresh)
    engine = create_async_engine(get_runtime_config().database_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(oauth, "AsyncSessionFactory", factory)
    credential_id = None
    try:
        async with factory() as db:
            credential = await store_credential(
                db,
                owner_id=None,
                kind=oauth.OAUTH_CREDENTIAL_KIND,
                label="MCP OAuth concurrency test",
                payload=payload,
            )
            credential_id = credential.id
            await db.commit()
        instance = IntegrationInstance(
            name="MCP",
            integration_type="mcp",
            adapter_type="mcp_streamable_http",
            credential_id=credential_id,
            config={"endpoint": ENDPOINT, "oauth": {}},
        )
        assert await asyncio.gather(oauth.oauth_token(instance), oauth.oauth_token(instance)) == [
            "new",
            "new",
        ]
        assert len(calls) == 1
    finally:
        if credential_id:
            async with factory() as db:
                await db.execute(delete(Credential).where(Credential.id == credential_id))
                await db.commit()
        await engine.dispose()
