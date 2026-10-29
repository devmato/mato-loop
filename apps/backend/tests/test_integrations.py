import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from ddgs.exceptions import DDGSException
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

import mato_loop_backend.api.integration_resources as api_integration_resources
import mato_loop_backend.api.routes.integrations as api_routes_integrations
import mato_loop_backend.integrations.bookstack.adapter as integrations_bookstack_adapter
import mato_loop_backend.integrations.caldav.adapter as integrations_caldav_adapter
import mato_loop_backend.integrations.caldav.service as caldav_service_module
import mato_loop_backend.integrations.registry as integration_registry
import mato_loop_backend.integrations.search as search_module
import mato_loop_backend.integrations.searxng.client as searxng_client
import mato_loop_backend.security as security
from mato_loop_backend.api.errors import ApiError, register_error_handlers
from mato_loop_backend.api.routes.integrations import (
    CalendarIntegrationRequest,
    IntegrationDefaultsRequest,
    SearchIntegrationRequest,
    create_calendar_integration,
    create_search_integration,
    delete_calendar_integration,
    delete_search_integration,
    get_integration_access,
    set_integration_enabled,
    update_calendar_integration,
    update_integration_access,
    update_search_integration,
    visible_instance_integrations,
)
from mato_loop_backend.db.session import get_db_session
from mato_loop_backend.integrations.bookstack.service import BookStackIntegrationService
from mato_loop_backend.integrations.caldav.client import CalDavError
from mato_loop_backend.integrations.contracts import (
    IntegrationContributions,
    IntegrationResource,
    ToolContribution,
)
from mato_loop_backend.integrations.mcp.client import McpClientError
from mato_loop_backend.integrations.search import (
    SearchIntegrationError,
    search_available,
    search_provider_options,
    search_web,
)
from mato_loop_backend.integrations.searxng.service import SearchIntegrationService
from mato_loop_backend.models import Credential, UserAccount
from mato_loop_backend.permissions.policy import PermissionRequest
from mato_loop_backend.security import require_user
from mato_loop_backend.settings import (
    DefaultCalendarSettings,
    DefaultTaskListSettings,
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


async def test_integration_switch_updates_only_the_owned_status(monkeypatch) -> None:
    user = SimpleNamespace(id=uuid4(), is_admin=False)
    shared = IntegrationInstance(name="Shared", integration_type="calendar", adapter_type="caldav")
    own = IntegrationInstance(
        name="Docker", integration_type="infrastructure", adapter_type="arcane",
        credential_id=uuid4(), config={"base_url": "https://docker.example"},
    )
    instance = IntegrationSettings(instances=[shared])
    personal = UserIntegrationSettings(instances=[own])

    async def load(_db, kind, **kwargs):
        if kind is UserIntegrationSettings:
            assert kwargs["user_id"] == user.id
            return personal
        return instance

    monkeypatch.setattr(SettingsStore, "load", AsyncMock(side_effect=load))
    save = AsyncMock()
    monkeypatch.setattr(SettingsStore, "save", save)
    db = AsyncMock()

    result = await set_integration_enabled(
        own.id, api_routes_integrations.EnabledRequest(enabled=False), db, user,
        scope="personal",
    )

    assert result == {"id": str(own.id), "enabled": False}
    assert own.config == {"base_url": "https://docker.example"}
    assert own.credential_id is not None
    assert shared.enabled is True
    save.assert_awaited_once_with(db, personal, user_id=user.id)
    result = await set_integration_enabled(
        own.id, api_routes_integrations.EnabledRequest(enabled=True), db, user,
        scope="personal",
    )
    assert result == {"id": str(own.id), "enabled": True}
    assert own.config == {"base_url": "https://docker.example"}
    assert own.credential_id is not None
    assert save.await_count == 2
    with pytest.raises(ApiError, match="auth.adminRequired"):
        await set_integration_enabled(
            shared.id, api_routes_integrations.EnabledRequest(enabled=False), db, user,
            scope="instance",
        )
    with pytest.raises(ValidationError):
        api_routes_integrations.EnabledRequest.model_validate({"enabled": True, "apiKey": "secret"})


async def test_mcp_reenable_uses_discovery_without_resending_a_token(monkeypatch) -> None:
    user = SimpleNamespace(id=uuid4(), is_admin=True)
    server = IntegrationInstance(
        name="MCP", integration_type="mcp", adapter_type="mcp_streamable_http",
        enabled=False, credential_id=uuid4(),
        config={"endpoint": "https://mcp.example/mcp", "tools": []},
    )
    settings = IntegrationSettings(instances=[server])
    monkeypatch.setattr(SettingsStore, "load", AsyncMock(return_value=settings))
    update = AsyncMock(side_effect=McpClientError("mcp.unavailable"))
    monkeypatch.setattr(api_routes_integrations.McpIntegrationService, "update", update)
    db = AsyncMock()

    with pytest.raises(ApiError, match="mcp.unavailable"):
        await set_integration_enabled(
            server.id, api_routes_integrations.EnabledRequest(enabled=True), db, user,
            scope="instance",
        )

    assert update.await_args.kwargs["token"] is None
    assert server.enabled is False
    db.rollback.assert_awaited_once()


async def test_opencode_reenable_uses_existing_connection_check(monkeypatch) -> None:
    user = SimpleNamespace(id=uuid4(), is_admin=False)
    instance = IntegrationInstance(
        name="Coding", integration_type="coding", adapter_type="opencode",
        enabled=False, config={
            "base_url": "https://code.example/", "username": "opencode",
            "server_root": "/srv/code", "server_version": "1.0",
        },
    )
    personal = UserIntegrationSettings(instances=[instance])
    monkeypatch.setattr(SettingsStore, "load", AsyncMock(side_effect=lambda _db, kind, **_kw: (
        personal if kind is UserIntegrationSettings else IntegrationSettings()
    )))
    update = AsyncMock(return_value=personal)
    monkeypatch.setattr(api_routes_integrations.OpenCodeIntegrationService, "update", update)

    await set_integration_enabled(
        instance.id, api_routes_integrations.EnabledRequest(enabled=True),
        AsyncMock(), user, scope="personal",
    )

    assert update.await_args.kwargs["user_id"] == user.id
    assert update.await_args.kwargs["password"] is None
    assert update.await_args.kwargs["remove_password"] is False
    assert update.await_args.kwargs["enabled"] is True


@pytest.mark.asyncio
async def test_visible_integrations_do_not_expose_shared_or_personal_connection_config(monkeypatch) -> None:
    shared = IntegrationInstance(
        name="Team", integration_type="calendar", adapter_type="caldav",
        config={"base_url": "https://private.example", "username": "secret"},
    )
    own = shared.model_copy(update={"id": uuid4(), "name": "Mine"})
    monkeypatch.setattr(SettingsStore, "load", AsyncMock(return_value=IntegrationSettings(instances=[shared])))
    load_optional = AsyncMock(return_value=UserIntegrationSettings(instances=[own]))
    monkeypatch.setattr(SettingsStore, "load_optional", load_optional)
    user_id = uuid4()

    items = await visible_instance_integrations(AsyncMock(), SimpleNamespace(id=user_id))

    assert [(item["name"], item["scope"]) for item in items] == [
        ("Team", "instance"), ("Mine", "personal")
    ]
    assert all(item["config"] == {} for item in items)
    assert load_optional.await_args.kwargs["user_id"] == user_id


class _StreamResponse:
    def __init__(
        self,
        payload,
        *,
        status_code: int = 200,
        raw: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self.body = raw if raw is not None else json.dumps(payload).encode()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def aiter_raw(self):
        yield self.body


def _catalog_app(db, user=None) -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(api_routes_integrations.router, prefix="/api")

    async def override_db():
        yield db

    app.dependency_overrides[get_db_session] = override_db
    if user is not None:
        app.dependency_overrides[require_user] = lambda: user
    return app


def test_integration_catalog_requires_authentication(monkeypatch) -> None:
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = None
    load = AsyncMock()
    monkeypatch.setattr(SettingsStore, "load", load)

    response = TestClient(_catalog_app(db)).get("/api/integrations/catalog")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "auth.notAuthenticated"
    load.assert_not_awaited()


@pytest.mark.parametrize(("method", "path", "payload"), [
    ("GET", "/webfetch", None),
    ("POST", "/webfetch/providers", {"name": "Reader", "base_url": "https://reader.test"}),
    ("PUT", f"/webfetch/providers/{uuid4()}", {"name": "Reader", "base_url": "https://reader.test"}),
    ("DELETE", f"/webfetch/providers/{uuid4()}", None),
    ("POST", f"/webfetch/providers/{uuid4()}/test", None),
])
def test_jina_management_is_admin_only_and_never_personal(monkeypatch, method, path, payload):
    db = AsyncMock(spec=AsyncSession)
    monkeypatch.setattr(SettingsStore, "load", AsyncMock(return_value=IntegrationSettings()))
    user = UserAccount(id=uuid4(), username="test", password_hash="hash", is_admin=False, is_active=True)
    with TestClient(_catalog_app(db, user)) as client:
        assert client.request(method, "/api/integrations" + path, json=payload).status_code == 403
        user.is_admin = True
        response = client.request(method, "/api/integrations" + path, json=payload, params={"scope": "personal"})
        assert response.json()["error"]["code"] == "integration.personalAdapterDisabled"


def test_integration_catalog_read_and_admin_update(monkeypatch) -> None:
    db = AsyncMock(spec=AsyncSession)
    settings = IntegrationSettings(
        adapter_availability={"caldav": "instance_only"}
    )
    monkeypatch.setattr(SettingsStore, "load", AsyncMock(return_value=settings))
    save = AsyncMock()
    monkeypatch.setattr(SettingsStore, "save", save)
    user = UserAccount(
        id=uuid4(),
        username="test",
        password_hash="hash",
        is_admin=False,
        is_active=True,
    )
    app = _catalog_app(db, user)

    with TestClient(app) as client:
        listed = client.get("/api/integrations/catalog")
        forbidden = client.put(
            "/api/integrations/catalog/caldav", json={"availability": "full"}
        )
        user.is_admin = True
        invalid = client.put(
            "/api/integrations/catalog/caldav", json={"availability": "sometimes"}
        )
        invalid_agent = client.put(
            "/api/integrations/catalog/openscad", json={"availability": "instance_only"}
        )
        updated = client.put(
            "/api/integrations/catalog/caldav", json={"availability": "full"}
        )
        unknown = client.put(
            "/api/integrations/catalog/unknown", json={"availability": "disabled"}
        )
        hidden = client.put(
            "/api/integrations/catalog/mcp_streamable_http",
            json={"availability": "full"},
        )

    adapters = listed.json()["adapters"]
    assert listed.status_code == 200
    assert all(item["adapterType"] != "git" for item in adapters)
    assert not any("/git" in path for path in app.openapi()["paths"])
    assert not any(
        tool.name.startswith("git_")
        for adapter in integration_registry.integration_adapters()
        for tool in (*adapter.shared_tools, *adapter.execution_tools)
    )
    assert [(item["integrationType"], item["adapterType"]) for item in adapters] == sorted(
        (item["integrationType"], item["adapterType"]) for item in adapters
    )
    assert next(item for item in adapters if item["adapterType"] == "caldav") == {
        "integrationType": "calendar",
        "adapterType": "caldav",
        "executionLocation": "backend",
        "availability": "instance_only",
    }
    assert next(item for item in adapters if item["adapterType"] == "opencode")[
        "personalOnly"
    ] is True
    assert forbidden.status_code == 403
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "validation.invalidChoice"
    assert invalid_agent.status_code == 422
    assert invalid_agent.json()["error"] == {
        "code": "validation.invalidChoice",
        "message": "The selected value is invalid.",
        "field": "availability",
    }
    assert updated.json() == {
        "integrationType": "calendar",
        "adapterType": "caldav",
        "executionLocation": "backend",
        "availability": "full",
    }
    assert unknown.status_code == 409
    assert unknown.json()["error"]["code"] == "integration.providerUnsupported"
    assert hidden.status_code == 409
    assert hidden.json()["error"]["code"] == "integration.providerUnsupported"
    assert settings.adapter_availability == {}
    save.assert_awaited_once_with(db, settings)
    db.commit.assert_awaited_once()


@pytest.mark.parametrize("scope", ["instance", "personal"])
async def test_disabled_adapter_blocks_create_but_allows_delete(
    monkeypatch, scope
) -> None:
    integration = _searxng()
    global_settings = IntegrationSettings(
        adapter_availability={"searxng": "disabled"},
        instances=[integration] if scope == "instance" else [],
    )
    personal_settings = UserIntegrationSettings(instances=[integration])

    async def load(_db, settings_type, **_kwargs):
        if settings_type is IntegrationSettings:
            return global_settings
        if settings_type is UserIntegrationSettings:
            return personal_settings
        return ToolSettings()

    monkeypatch.setattr(SettingsStore, "load", AsyncMock(side_effect=load))
    monkeypatch.setattr(SettingsStore, "load_optional", AsyncMock(return_value=None))
    create = AsyncMock()
    delete = AsyncMock()
    monkeypatch.setattr(SearchIntegrationService, "create", create)
    monkeypatch.setattr(SearchIntegrationService, "delete", delete)
    user = SimpleNamespace(id=uuid4(), is_admin=scope == "instance")
    db = AsyncMock(spec=AsyncSession)

    with pytest.raises(ApiError, match="integration.adapterDisabled" if scope == "instance" else "integration.personalAdapterDisabled"):
        await create_search_integration(
            _request(), db, scope=scope, user=user
        )
    listed = await api_routes_integrations.get_search_integrations(
        db, scope=scope, user=user
    )
    await delete_search_integration(
        integration.id, db, scope=scope, user=user
    )

    assert listed["providers"][0]["id"] == str(integration.id)
    create.assert_not_awaited()
    delete.assert_awaited_once_with(
        db,
        integration.id,
        owner_id=None if scope == "instance" else user.id,
    )


async def test_bookstack_policy_cleanup_removes_resource_labels(monkeypatch) -> None:
    integration_id = uuid4()
    tools = ToolSettings(
        resource_labels={str(integration_id): {"book:7": "Handbuch"}},
    )
    monkeypatch.setattr(SettingsStore, "load", AsyncMock(return_value=tools))
    monkeypatch.setattr(SettingsStore, "save", AsyncMock())
    db = AsyncMock(spec=AsyncSession)

    await BookStackIntegrationService._save(
        db, IntegrationSettings(), clear_policies=integration_id
    )

    assert str(integration_id) not in tools.resource_labels


async def test_nonadmin_cannot_read_instance_integration_or_use_credential_test(
    monkeypatch,
) -> None:
    user = SimpleNamespace(id=uuid4(), is_admin=False)
    load = AsyncMock(return_value=IntegrationSettings())
    discover = AsyncMock()
    monkeypatch.setattr(SettingsStore, "load", load)
    monkeypatch.setattr(integrations_caldav_adapter, "load_caldav_resources", discover)
    db = AsyncMock(spec=AsyncSession)

    with pytest.raises(ApiError, match="auth.adminRequired"):
        await api_routes_integrations.get_calendar_integrations(
            db, scope="instance", user=user
        )
    with pytest.raises(ApiError, match="auth.adminRequired"):
        await api_routes_integrations.test_calendar_integration(
            uuid4(), db, scope="instance", user=user
        )

    load.assert_not_awaited()
    discover.assert_not_awaited()


async def test_personal_policy_read_exposes_no_integration_configuration(
    monkeypatch,
) -> None:
    settings = IntegrationSettings(
        adapter_availability={
            "searxng": "instance_only",
            "bookstack": "instance_only",
            "mealie": "instance_only",
            "openscad": "disabled",
        },
        instances=[_searxng()],
    )
    monkeypatch.setattr(SettingsStore, "load", AsyncMock(return_value=settings))

    result = await api_routes_integrations.get_personal_integration_settings(
        SimpleNamespace(id=uuid4(), is_admin=False), AsyncMock(spec=AsyncSession)
    )

    assert result == {
        "enabled": True,
        "allowedAdapterTypes": [
            "arcane",
            "browser",
            "caldav",
            "imap_smtp",
            "opencode",
            "sonobin",
            "tbs-desk",
        ],
    }


def _request(*, name: str = "SearXNG", enabled: bool = True) -> SearchIntegrationRequest:
    return SearchIntegrationRequest(
        integration_type="search",
        provider_type="searxng",
        name=name,
        base_url="https://search.example.com",
        enabled=enabled,
    )


def _searxng(*, name: str = "SearXNG", enabled: bool = True) -> IntegrationInstance:
    return IntegrationInstance(
        name=name,
        integration_type="search",
        adapter_type="searxng",
        enabled=enabled,
        config={"base_url": "https://search.example.com"},
    )


class _ResourceArguments(BaseModel):
    resource_key: str


async def _resource_handler(_arguments, _context) -> ToolResult:
    return ToolResult.json({"ok": True})


def _resource_adapter(
    monkeypatch,
    allowed_policies: tuple[ToolExecutionPolicy, ...] = tuple(ToolExecutionPolicy),
) -> None:
    async def load_resources(_instance, _credential):
        return (
            IntegrationResource(
                "calendar:private",
                "Privat",
                applicable_capabilities=frozenset({"network.search"}),
            ),
            IntegrationResource(
                "calendar:work",
                "Arbeit",
                applicable_capabilities=frozenset({"network.search"}),
            ),
        )

    current = integration_registry._ADAPTERS[("search", "searxng")]
    action = ToolAction(
        "network.search",
        "Suchen",
        category="read",
        allowed_policies=allowed_policies,
    )

    def contributions(instance):
        base = current.contributions(instance)
        return IntegrationContributions(
            tools=(
                ToolContribution(
                    name="integration_resource_search",
                    description="Search one integration resource.",
                    arguments_model=_ResourceArguments,
                    handler=_resource_handler,
                    actions=(action,),
                    authorization_factory=lambda arguments, _context: (
                        PermissionRequest(
                            capability="network.search",
                            resource_type="network_service",
                            resource_scope=arguments.resource_key,
                            action_kind="integration_resource_search",
                            risk_level="medium",
                            title="Ressource durchsuchen?",
                            summary="Der Agent möchte eine Integrationsressource durchsuchen.",
                            policy_resource_key=arguments.resource_key,
                        ),
                    ),
                ),
            ),
            tool_options=base.tool_options,
            prompt_fragments=base.prompt_fragments,
        )

    monkeypatch.setitem(
        integration_registry._ADAPTERS,
        ("search", "searxng"),
        replace(
            current,
            contribution_factory=contributions,
            resource_actions=(action,),
            resource_loader=load_resources,
        ),
    )


def test_mail_send_and_delete_policy_metadata_supports_all_policies() -> None:
    adapter = integration_registry._ADAPTERS[("mail", "imap_smtp")]

    actions = {
        action.capability: action.allowed_policies
        for action in adapter.resource_actions
    }
    assert actions["mail.send"] == actions["mail.delete"] == tuple(ToolExecutionPolicy)
    assert all(
        action.allowed_policies == actions[action.capability]
        for tool in adapter.shared_tools
        for action in tool.actions
        if action.capability in {"mail.send", "mail.delete"}
    )


async def test_revoked_personal_adapters_block_external_clients(monkeypatch) -> None:
    _resource_adapter(monkeypatch)
    resource_loader = AsyncMock(return_value=())
    current = integration_registry._ADAPTERS[("search", "searxng")]
    monkeypatch.setitem(
        integration_registry._ADAPTERS,
        ("search", "searxng"),
        replace(current, resource_loader=resource_loader),
    )
    search = _searxng()
    calendar = IntegrationInstance(
        name="Calendar",
        integration_type="calendar",
        adapter_type="caldav",
    )
    knowledge = IntegrationInstance(
        name="Knowledge",
        integration_type="knowledge",
        adapter_type="bookstack",
    )
    openscad = IntegrationInstance(
        name="OpenSCAD",
        integration_type="openscad",
        adapter_type="openscad",
        execution_agent_id=uuid4(),
        config={"binary_path": "/usr/bin/openscad"},
    )
    instance = IntegrationSettings(
        adapter_availability={
            "searxng": "instance_only",
            "caldav": "instance_only",
            "bookstack": "instance_only",
            "openscad": "disabled",
        }
    )
    personal = UserIntegrationSettings(
        instances=[search, calendar, knowledge, openscad]
    )

    async def load(_db, settings_type, **_kwargs):
        return instance if settings_type is IntegrationSettings else personal

    search_client = AsyncMock()
    calendar_client = AsyncMock()
    knowledge_client = AsyncMock()
    execution_client = Mock()
    monkeypatch.setattr(SettingsStore, "load", AsyncMock(side_effect=load))
    monkeypatch.setattr(
        api_routes_integrations, "search_with_provider", search_client
    )
    monkeypatch.setattr(
        integrations_caldav_adapter, "load_caldav_resources", calendar_client
    )
    monkeypatch.setattr(
        integrations_bookstack_adapter, "load_bookstack_resources", knowledge_client
    )
    monkeypatch.setattr(
        api_routes_integrations, "_connected_client", execution_client
    )
    user = SimpleNamespace(id=uuid4(), is_admin=False)
    db = AsyncMock(spec=AsyncSession)
    operations = (
        api_routes_integrations.test_search_integration(
            search.id, db, scope="personal", user=user
        ),
        api_routes_integrations.test_calendar_integration(
            calendar.id, db, scope="personal", user=user
        ),
        api_routes_integrations.test_knowledge_integration(
            knowledge.id, db, scope="personal", user=user
        ),
        api_routes_integrations.test_openscad_integration(
            openscad.id, user, db, scope="personal"
        ),
    )

    for operation, code in zip(
        operations,
        [
            "integration.personalAdapterDisabled",
            "integration.personalAdapterDisabled",
            "integration.personalAdapterDisabled",
            "integration.adapterDisabled",
        ],
        strict=True,
    ):
        with pytest.raises(ApiError, match=code):
            await operation

    search_client.assert_not_awaited()
    calendar_client.assert_not_awaited()
    knowledge_client.assert_not_awaited()
    execution_client.assert_not_called()
    resource_loader.assert_not_awaited()


def test_no_key_provider_makes_search_available_without_configuration() -> None:
    settings = IntegrationSettings()

    assert search_available(settings) is True
    schemas = tool_registry.schemas(settings)
    search_schema = next(item for item in schemas if item["name"] == "web_search")
    properties = search_schema["parameters"]["properties"]
    assert set(properties) == {"_reason", "query", "search_type"}
    assert properties["query"]["description"] == (
        "One compact search-engine query for one information need. Preserve user-supplied dates, "
        "versions, model numbers, and exact names; never add a date."
    )
    assert properties["search_type"]["enum"] == ["web", "images"]
    assert properties["search_type"]["default"] == "web"


def test_active_integrations_extend_the_host_search_provider_catalog() -> None:
    first = _searxng(name="Intern")
    second = _searxng(name="Extern")
    disabled = _searxng(name="Aus", enabled=False)
    settings = IntegrationSettings(instances=[first, second, disabled])

    expected = ["no_key", f"searxng:{first.id}", f"searxng:{second.id}"]
    options = search_provider_options(settings)
    assert [item["value"] for item in options] == expected
    assert str(disabled.id) not in str(options)


@pytest.mark.parametrize(
    "resource",
    [
        IntegrationResource(
            "project:main",
            "Hauptprojekt",
            hard_unavailable_capabilities=frozenset({"tasks.invented"}),
        ),
        IntegrationResource(
            "project:main",
            "Hauptprojekt",
            applicable_capabilities=frozenset({"tasks.invented"}),
        ),
        IntegrationResource(
            "project:main",
            "Hauptprojekt",
            group_key="invalid group key",
        ),
    ],
)
async def test_resource_discovery_cannot_invent_adapter_capabilities(
    monkeypatch, resource
) -> None:
    _resource_adapter(monkeypatch)
    current = integration_registry._ADAPTERS[("search", "searxng")]

    async def load_resources(_instance, _credential):
        return (resource,)

    monkeypatch.setitem(
        integration_registry._ADAPTERS,
        ("search", "searxng"),
        replace(current, resource_loader=load_resources),
    )
    integration = _searxng()
    with pytest.raises(ApiError) as exc_info:
        await api_integration_resources._integration_resources(
            AsyncMock(spec=AsyncSession), integration
        )

    assert exc_info.value.code == "integration.resourceDiscoveryFailed"


async def test_caldav_discovery_rejects_duplicate_resource_keys(monkeypatch) -> None:
    current = integration_registry._ADAPTERS[("calendar", "caldav")]
    key = f"calendar:{'a' * 32}"

    async def load_resources(_instance, _credential):
        return (IntegrationResource(key, "Privat"), IntegrationResource(key, "Duplikat"))

    monkeypatch.setitem(
        integration_registry._ADAPTERS,
        ("calendar", "caldav"),
        replace(current, resource_loader=load_resources),
    )
    instance = IntegrationInstance(
        name="Kalender", integration_type="calendar", adapter_type="caldav",
        config={"base_url": "https://calendar.example.com", "username": "user"},
    )
    with pytest.raises(ApiError) as exc_info:
        await api_integration_resources._integration_resources(
            AsyncMock(spec=AsyncSession), instance,
        )
    assert exc_info.value.code == "integration.resourceDiscoveryFailed"


async def test_caldav_access_manages_independent_defaults(monkeypatch) -> None:
    key = f"calendar:{'a' * 32}"
    task_list_key = f"tasklist:{'a' * 32}"
    other_task_list_key = f"tasklist:{'b' * 32}"

    async def load_resources(_instance, _credential):
        return (
            IntegrationResource(
                key,
                "Privat",
                applicable_capabilities=frozenset(
                    {"calendar.read", "calendar.write", "calendar.delete"}
                ),
                group_key="a" * 32,
            ),
            IntegrationResource(
                task_list_key,
                "Aufgaben",
                applicable_capabilities=frozenset(
                    {"tasks.read", "tasks.write", "tasks.delete"}
                ),
                group_key="a" * 32,
            ),
            IntegrationResource(
                other_task_list_key,
                "Weitere Aufgaben",
                applicable_capabilities=frozenset(
                    {"tasks.read", "tasks.write", "tasks.delete"}
                ),
                group_key="b" * 32,
            ),
        )

    current_adapter = integration_registry._ADAPTERS[("calendar", "caldav")]
    monkeypatch.setitem(
        integration_registry._ADAPTERS,
        ("calendar", "caldav"),
        replace(current_adapter, resource_loader=load_resources),
    )
    integration = IntegrationInstance(
        name="Kalender",
        integration_type="calendar",
        adapter_type="caldav",
        config={"base_url": "https://calendar.example.com", "username": "user"},
    )
    integrations = IntegrationSettings(instances=[integration])
    other_id = uuid4()
    tools = ToolSettings.model_validate(
        {
            "defaultCalendar": {
                "integrationId": str(integration.id),
                "calendarKey": key,
            },
            "defaultTaskList": {
                "integrationId": str(integration.id),
                "taskListKey": task_list_key,
            },
        }
    )

    async def load_settings(_db, settings_type, **_kwargs):
        return integrations if settings_type is IntegrationSettings else tools

    monkeypatch.setattr(SettingsStore, "load", AsyncMock(side_effect=load_settings))
    monkeypatch.setattr(SettingsStore, "save", AsyncMock())
    db = AsyncMock(spec=AsyncSession)

    access = await get_integration_access(integration.id, db)
    access_resources = {resource["key"]: resource for resource in access["resources"]}
    assert access["defaultResourceKey"] == key
    assert access["defaultTaskListKey"] == task_list_key
    assert access_resources[key]["groupKey"] == "a" * 32
    assert access_resources[task_list_key]["groupKey"] == "a" * 32

    preserved = await update_integration_access(
        integration.id,
        IntegrationDefaultsRequest(),
        db,
    )
    assert tools.default_calendar.calendar_key == key
    assert tools.default_task_list.task_list_key == task_list_key
    assert preserved["defaultResourceKey"] == key
    assert preserved["defaultTaskListKey"] == task_list_key

    tools.default_task_list = DefaultTaskListSettings(
        integration_id=other_id,
        task_list_key=other_task_list_key,
    )
    unchanged = await update_integration_access(
        integration.id,
        IntegrationDefaultsRequest.model_validate(
            {"defaultTaskListKey": None}
        ),
        db,
    )
    assert tools.default_task_list.integration_id == other_id
    assert unchanged["defaultTaskListKey"] is None

    selected = await update_integration_access(
        integration.id,
        IntegrationDefaultsRequest.model_validate(
            {"defaultTaskListKey": task_list_key}
        ),
        db,
    )
    assert tools.default_task_list.integration_id == integration.id
    assert tools.default_task_list.task_list_key == task_list_key
    assert selected["defaultResourceKey"] == key
    assert selected["defaultTaskListKey"] == task_list_key
    resources = {resource["key"]: resource for resource in selected["resources"]}
    assert resources[key]["label"] == "Privat"
    assert resources[task_list_key]["label"] == "Aufgaben"

    replaced = await update_integration_access(
        integration.id,
        IntegrationDefaultsRequest.model_validate(
            {"defaultTaskListKey": other_task_list_key}
        ),
        db,
    )
    assert tools.default_task_list.task_list_key == other_task_list_key
    assert replaced["defaultResourceKey"] == key
    assert replaced["defaultTaskListKey"] == other_task_list_key

    cleared = await update_integration_access(
        integration.id,
        IntegrationDefaultsRequest.model_validate(
            {"defaultTaskListKey": None}
        ),
        db,
    )
    assert tools.default_task_list is None
    assert tools.default_calendar.calendar_key == key
    assert cleared["defaultResourceKey"] == key
    assert cleared["defaultTaskListKey"] is None

    with pytest.raises(ApiError) as task_default:
        await update_integration_access(
            integration.id,
            IntegrationDefaultsRequest.model_validate(
                {"defaultResourceKey": task_list_key}
            ),
            db,
        )
    assert task_default.value.code == "validation.invalidChoice"
    assert task_default.value.field == "default_resource_key"

    for invalid_key in (key, f"tasklist:{'c' * 32}"):
        with pytest.raises(ApiError) as exc_info:
            await update_integration_access(
                integration.id,
                IntegrationDefaultsRequest.model_validate(
                    {"defaultTaskListKey": invalid_key}
                ),
                db,
            )
        assert exc_info.value.code == "validation.invalidChoice"
        assert exc_info.value.field == "default_task_list_key"

    with pytest.raises(ApiError) as stale_calendar:
        await update_integration_access(
            integration.id,
            IntegrationDefaultsRequest.model_validate(
                {"defaultResourceKey": f"calendar:{'c' * 32}"}
            ),
            db,
        )
    assert stale_calendar.value.code == "validation.invalidChoice"
    assert stale_calendar.value.field == "default_resource_key"


async def test_non_caldav_default_resources_are_unsupported(monkeypatch) -> None:
    _resource_adapter(monkeypatch)
    integration = _searxng()
    integrations = IntegrationSettings(instances=[integration])
    monkeypatch.setattr(SettingsStore, "load", AsyncMock(return_value=integrations))

    with pytest.raises(ApiError) as exc_info:
        await update_integration_access(
            integration.id,
            IntegrationDefaultsRequest.model_validate(
                {
                    "defaultTaskListKey": f"tasklist:{'a' * 32}",
                }
            ),
            AsyncMock(spec=AsyncSession),
        )

    assert exc_info.value.code == "integration.resourcePoliciesUnsupported"


def test_search_management_never_allows_personal_connections_for_admins(monkeypatch):
    monkeypatch.setattr(SettingsStore, "load", AsyncMock(return_value=IntegrationSettings()))
    user = UserAccount(id=uuid4(), username="admin", password_hash="hash", is_admin=True, is_active=True)
    response = TestClient(_catalog_app(AsyncMock(spec=AsyncSession), user)).post(
        "/api/integrations/search/providers?scope=personal",
        json=_request().model_dump(mode="json"),
    )
    assert response.json()["error"]["code"] == "integration.personalAdapterDisabled"


def test_search_snippet_preserves_escaped_technical_syntax() -> None:
    assert search_module._normalize_snippet("Use std::vector&lt;T&gt; safely.") == (
        "Use std::vector<T> safely."
    )
    assert search_module._normalize_snippet("<b>Result</b><img src=x>") == "Result"


async def test_no_key_provider_uses_ddgs_auto_search_and_normalizes_results(
    monkeypatch,
) -> None:
    text = Mock(
        return_value=[
            {
                "title": "MatoLoop",
                "href": "https://example.com/result",
                "body": "&lt;b&gt;Beschreibung&lt;/b&gt;   mit Abstand",
            },
            {
                "title": "Duplikat",
                "href": "https://example.com/result",
                "body": "Andere Beschreibung",
            },
        ]
    )
    client = Mock(text=text)
    constructor = Mock(return_value=client)
    monkeypatch.setattr(search_module, "DDGS", constructor)

    result = await search_module.search_without_api_key("MatoLoop")

    constructor.assert_called_once_with(timeout=30)
    text.assert_called_once_with(query="MatoLoop", max_results=8)
    assert json.loads(result) == [
        {
            "sourceId": "src-87009d3e3bbb",
            "title": "MatoLoop",
            "url": "https://example.com/result",
            "snippet": "<b>Beschreibung</b> mit Abstand",
            "publishedDate": None,
        }
    ]

    text.side_effect = DDGSException("No results found.")
    assert json.loads(await search_module.search_without_api_key("MatoLoop")) == []


async def test_no_key_provider_supports_image_search(monkeypatch) -> None:
    images = Mock(
        return_value=[
            {
                "title": "Official product image",
                "url": "https://example.com/product",
                "image": "https://cdn.example.com/product.jpg",
                "body": "Rear view",
            }
        ]
    )
    client = Mock(images=images)
    monkeypatch.setattr(search_module, "DDGS", Mock(return_value=client))

    result = await search_module.search_without_api_key(
        "product rear view", search_type="images"
    )

    images.assert_called_once_with(query="product rear view", max_results=8)
    assert json.loads(result) == [
        {
            "sourceId": search_module.evidence_source_id(
                "https://example.com/product"
            ),
            "title": "Official product image",
            "url": "https://example.com/product",
            "snippet": "Rear view",
            "publishedDate": None,
            "imageUrl": "https://cdn.example.com/product.jpg",
        }
    ]


async def test_searxng_reports_unresponsive_engines_as_unavailable(
    monkeypatch,
) -> None:
    response = _StreamResponse(
        {
            "results": [],
            "unresponsive_engines": [["duckduckgo", "CAPTCHA"]],
        }
    )
    client = AsyncMock()
    client.stream = Mock(return_value=response)
    context = AsyncMock()
    context.__aenter__.return_value = client
    monkeypatch.setattr(
        searxng_client.httpx, "AsyncClient", Mock(return_value=context)
    )

    with pytest.raises(SearchIntegrationError) as exc_info:
        await searxng_client.search_with_provider(_searxng(), "MatoLoop")

    assert exc_info.value.code == "integration.unavailable"


async def test_searxng_retries_unresponsive_default_engines(monkeypatch) -> None:
    unavailable = _StreamResponse(
        {
            "results": [],
            "unresponsive_engines": [["duckduckgo", "CAPTCHA"]],
        }
    )
    recovered = _StreamResponse(
        {
            "results": [
                {
                    "title": "MatoLoop",
                    "url": "https://example.com/result",
                    "content": "Beschreibung",
                }
            ],
            "unresponsive_engines": [],
        }
    )
    client = AsyncMock()
    client.stream = Mock(side_effect=[unavailable, recovered])
    context = AsyncMock()
    context.__aenter__.return_value = client
    monkeypatch.setattr(
        searxng_client.httpx, "AsyncClient", Mock(return_value=context)
    )

    result = await searxng_client.search_with_provider(_searxng(), "MatoLoop")

    assert json.loads(result)[0]["url"] == "https://example.com/result"
    assert client.stream.call_args_list[1].kwargs["params"]["engines"] == (
        "bing,mojeek,presearch"
    )


async def test_searxng_supports_image_search(monkeypatch) -> None:
    response = _StreamResponse(
        {
            "results": [
                {
                    "title": "Official product image",
                    "url": "https://example.com/product",
                    "img_src": "https://cdn.example.com/product.webp",
                    "content": "Rear view",
                }
            ],
            "unresponsive_engines": [],
        }
    )
    client = AsyncMock()
    client.stream = Mock(return_value=response)
    context = AsyncMock()
    context.__aenter__.return_value = client
    monkeypatch.setattr(
        searxng_client.httpx, "AsyncClient", Mock(return_value=context)
    )

    result = await searxng_client.search_with_provider(
        _searxng(), "product rear view", search_type="images"
    )

    assert client.stream.call_args.kwargs["params"] == {
        "q": "product rear view",
        "format": "json",
        "categories": "images",
    }
    assert json.loads(result)[0]["imageUrl"] == (
        "https://cdn.example.com/product.webp"
    )


async def test_searxng_rejects_oversized_streamed_responses(monkeypatch) -> None:
    response = _StreamResponse(
        None,
        raw=b"x" * (search_module.MAX_SEARCH_RESPONSE_BYTES + 1),
    )
    client = AsyncMock()
    client.stream = Mock(return_value=response)
    context = AsyncMock()
    context.__aenter__.return_value = client
    monkeypatch.setattr(
        searxng_client.httpx, "AsyncClient", Mock(return_value=context)
    )

    with pytest.raises(SearchIntegrationError) as exc_info:
        await searxng_client.search_with_provider(_searxng(), "MatoLoop")

    assert exc_info.value.code == "integration.invalidResponse"


async def test_searxng_rejects_compressed_responses(monkeypatch) -> None:
    response = _StreamResponse(
        {"results": []},
        headers={"Content-Encoding": "gzip"},
    )
    client = AsyncMock()
    client.stream = Mock(return_value=response)
    context = AsyncMock()
    context.__aenter__.return_value = client
    monkeypatch.setattr(
        searxng_client.httpx, "AsyncClient", Mock(return_value=context)
    )

    with pytest.raises(SearchIntegrationError) as exc_info:
        await searxng_client.search_with_provider(_searxng(), "MatoLoop")

    assert exc_info.value.code == "integration.invalidResponse"
    assert client.stream.call_args.kwargs["headers"] == {"Accept-Encoding": "identity"}


async def test_search_deadline_includes_provider_request(monkeypatch) -> None:
    async def execute(_settings, _provider, _query, **_kwargs):
        await search_module.asyncio.sleep(1)
        return "[]"

    monkeypatch.setattr(search_module, "search_with_key", execute)
    monkeypatch.setattr(search_module, "SEARCH_DEADLINE_SECONDS", 0.01)

    with pytest.raises(SearchIntegrationError) as exc_info:
        await search_web(
            IntegrationSettings(),
            "query",
            provider="no_key",
        )

    assert exc_info.value.code == "integration.unavailable"


async def test_search_integration_crud_keeps_explicit_provider_selection(monkeypatch) -> None:
    settings = IntegrationSettings()
    tools = ToolSettings()
    db = AsyncMock(spec=AsyncSession)

    async def load_settings(_db, settings_type, **_kwargs):
        return settings if settings_type is IntegrationSettings else tools

    monkeypatch.setattr(SettingsStore, "load", AsyncMock(side_effect=load_settings))
    monkeypatch.setattr(SettingsStore, "save", AsyncMock())

    created = await create_search_integration(_request(), db)
    integration_id = settings.instances[0].id

    assert created["effectiveEnabled"] is True
    assert created["providers"][0]["id"] == str(integration_id)
    tools.web_search.primary_provider = f"searxng:{integration_id}"

    updated = await update_search_integration(
        integration_id, _request(name="Privat", enabled=False), db
    )
    assert updated["providers"][0]["name"] == "Privat"
    assert tools.web_search.primary_provider == f"searxng:{integration_id}"

    await delete_search_integration(integration_id, db)
    assert settings.instances == []
    assert db.commit.await_count == 3


async def test_legacy_fallback_does_not_change_primary_selection(monkeypatch) -> None:
    integration = _searxng()
    settings = IntegrationSettings(instances=[integration])
    tools = ToolSettings.model_validate(
        {
            "webSearch": {
                "primaryProvider": "no_key",
                "fallbackProvider": f"searxng:{integration.id}",
            },
        }
    )
    db = AsyncMock(spec=AsyncSession)

    async def load_settings(_db, settings_type, **_kwargs):
        return settings if settings_type is IntegrationSettings else tools

    monkeypatch.setattr(SettingsStore, "load", AsyncMock(side_effect=load_settings))
    save = AsyncMock()
    monkeypatch.setattr(SettingsStore, "save", save)

    await delete_search_integration(integration.id, db)

    assert tools.web_search.primary_provider == "no_key"
    assert not settings.instances
    assert not any(call.args[1] is tools for call in save.await_args_list)


async def test_changing_connection_clears_its_resource_policies(monkeypatch) -> None:
    integration = _searxng()
    settings = IntegrationSettings(instances=[integration])
    tools = ToolSettings()
    integration.permissions.public_permissions.resource_policies = {
        "calendar:work": {"network.search": "auto"}
    }

    async def load_settings(_db, settings_type, **_kwargs):
        return settings if settings_type is IntegrationSettings else tools

    monkeypatch.setattr(SettingsStore, "load", AsyncMock(side_effect=load_settings))
    monkeypatch.setattr(SettingsStore, "save", AsyncMock())

    await update_search_integration(
        integration.id,
        SearchIntegrationRequest(
            name="SearXNG",
            base_url="https://other.example.com",
            enabled=True,
        ),
        AsyncMock(spec=AsyncSession),
    )

    assert not integration.permissions.public_permissions.resource_policies


async def test_search_does_not_switch_provider_on_failure(monkeypatch) -> None:
    execute = AsyncMock(
        side_effect=SearchIntegrationError("integration.rateLimited")
    )
    monkeypatch.setattr(search_module, "search_with_key", execute)

    with pytest.raises(SearchIntegrationError):
        await search_web(
            IntegrationSettings(),
            "query",
            provider="no_key",
        )

    execute.assert_awaited_once()


def _calendar_request(
    *,
    name: str = "Kalender",
    base_url: str = "https://calendar.example.com",
    username: str = "user",
    password: str | None = "secret",
    enabled: bool = True,
) -> CalendarIntegrationRequest:
    return CalendarIntegrationRequest(
        integration_type="calendar",
        provider_type="caldav",
        name=name,
        base_url=base_url,
        username=username,
        password=password,
        enabled=enabled,
    )


async def test_caldav_account_crud_rotates_secret_and_keeps_accounts_independent(
    monkeypatch,
) -> None:
    settings = IntegrationSettings()
    tools = ToolSettings()
    db = AsyncMock(spec=AsyncSession)
    db.get.return_value = Mock(user_id=None)
    credential_ids = [uuid4(), uuid4(), uuid4()]
    store_credential = AsyncMock(
        side_effect=[Mock(id=credential_id) for credential_id in credential_ids]
    )

    async def load_settings(_db, settings_type, **_kwargs):
        return settings if settings_type is IntegrationSettings else tools

    monkeypatch.setattr(SettingsStore, "load", AsyncMock(side_effect=load_settings))
    monkeypatch.setattr(SettingsStore, "save", AsyncMock())
    monkeypatch.setattr(
        caldav_service_module, "store_credential", store_credential
    )

    first_payload = await create_calendar_integration(_calendar_request(), db)
    first = settings.instances[0]
    await create_calendar_integration(
        _calendar_request(
            name="Arbeit",
            base_url="https://work.example.com",
            username="worker",
            password="work-secret",
        ),
        db,
    )
    second = settings.instances[1]
    first.permissions.private_permissions.policies = {"calendar.read": ToolExecutionPolicy.AUTO}
    second.permissions.private_permissions.policies = {"calendar.read": ToolExecutionPolicy.ASK}
    first.permissions.private_permissions.resource_policies = {
        f"calendar:{'a' * 32}": {"calendar.write": ToolExecutionPolicy.ASK}
    }
    second.permissions.private_permissions.resource_policies = {
        f"calendar:{'b' * 32}": {"calendar.read": ToolExecutionPolicy.AUTO}
    }
    tools.resource_labels = {
        str(first.id): {f"calendar:{'a' * 32}": "Privat"},
        str(second.id): {f"calendar:{'b' * 32}": "Arbeit"},
    }
    tools.default_calendar = DefaultCalendarSettings(
        integration_id=first.id,
        calendar_key=f"calendar:{'a' * 32}",
    )
    tools.default_task_list = DefaultTaskListSettings(
        integration_id=first.id,
        task_list_key=f"tasklist:{'a' * 32}",
    )

    assert first.credential_id == credential_ids[0]
    assert first_payload["accounts"][0]["config"] == {
        "baseUrl": "https://calendar.example.com/",
        "username": "user",
    }
    assert first_payload["accounts"][0]["hasCredential"] is True
    assert "secret" not in json.dumps(first_payload)
    assert "credentialId" not in json.dumps(first_payload)

    await update_calendar_integration(
        first.id,
        _calendar_request(name="Umbenannt", password="new-secret", enabled=False),
        db,
    )

    assert first.credential_id == credential_ids[2]
    assert first.permissions.private_permissions.policies
    assert first.permissions.private_permissions.resource_policies
    assert tools.default_calendar.integration_id == first.id
    assert tools.default_task_list.integration_id == first.id
    assert second.permissions.private_permissions.policies
    assert second.permissions.private_permissions.resource_policies
    assert store_credential.await_args_list[-1].args == (db,)
    assert store_credential.await_args_list[-1].kwargs == {
        "owner_id": None,
        "kind": "caldav.password",
        "label": "Umbenannt",
        "payload": {"password": "new-secret"},
    }
    assert db.delete.await_count == 1

    await update_calendar_integration(
        first.id,
        _calendar_request(
            base_url="https://moved.example.com",
            password=None,
        ),
        db,
    )

    assert not first.permissions.private_permissions.policies
    assert not first.permissions.private_permissions.resource_policies
    assert str(first.id) not in tools.resource_labels
    assert second.permissions.private_permissions.policies
    assert second.permissions.private_permissions.resource_policies
    assert str(second.id) in tools.resource_labels
    assert tools.default_calendar is None
    assert tools.default_task_list is None
    assert first.credential_id == credential_ids[2]

    tools.default_calendar = DefaultCalendarSettings(
        integration_id=second.id,
        calendar_key=f"calendar:{'b' * 32}",
    )
    tools.default_task_list = DefaultTaskListSettings(
        integration_id=first.id,
        task_list_key=f"tasklist:{'a' * 32}",
    )
    await delete_calendar_integration(first.id, db)

    assert [instance.id for instance in settings.instances] == [second.id]
    assert tools.default_calendar.integration_id == second.id
    assert tools.default_task_list is None
    assert db.delete.await_count == 2


async def test_caldav_test_endpoint_maps_remote_failure_to_controlled_error(
    monkeypatch,
) -> None:
    account = IntegrationInstance(
        name="Kalender",
        integration_type="calendar",
        adapter_type="caldav",
        credential_id=uuid4(),
        config={"base_url": "https://calendar.example.com", "username": "user"},
    )
    settings = IntegrationSettings(instances=[account])
    record = Mock(spec=Credential)
    record.kind = "caldav.password"
    db = AsyncMock(spec=AsyncSession)
    db.get.return_value = record
    monkeypatch.setattr(SettingsStore, "load", AsyncMock(return_value=settings))
    monkeypatch.setattr(
        security,
        "read_credential",
        Mock(return_value={"password": "secret"}),
    )
    monkeypatch.setattr(
        integrations_caldav_adapter,
        "load_caldav_resources",
        AsyncMock(side_effect=CalDavError("caldav.authenticationFailed", status_code=401)),
    )

    with pytest.raises(ApiError) as exc_info:
        await api_routes_integrations.test_calendar_integration(account.id, db)

    assert exc_info.value.status_code == 502
    assert exc_info.value.code == "integration.authenticationFailed"
    assert exc_info.value.error.message == "The integration credentials were rejected."
    assert "caldav" not in exc_info.value.error.message.lower()
