import json
from dataclasses import replace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError

import mato_loop_backend.tools.access as mock_tools_access
import mato_loop_backend.tools.coding as mock_tools_coding
import mato_loop_backend.tools.web as mock_tools_web
from mato_loop_backend.api.errors import ApiError
from mato_loop_backend.api.routes.agent_profiles import (
    AgentProfileRequest,
    _profile_from_request,
    _response,
)
from mato_loop_backend.integrations.registry import effective_integration_contributions
from mato_loop_backend.models import ChatSession, Project, UserAccount
from mato_loop_backend.permissions.policy import PermissionDecision
from mato_loop_backend.providers.base import ToolCall
from mato_loop_backend.settings.effective import (
    filter_agent_profile_integrations,
    load_agent_profile_tools,
)
from mato_loop_backend.settings.models import (
    GENERAL_PROFILE_ID,
    AgentProfile,
    AgentProfileAccessOverrides,
    AgentProfileOverrides,
    AgentProfilesSettings,
    CustomAgentProfile,
    IntegrationInstance,
    IntegrationPermissions,
    IntegrationSettings,
    ToolExecutionPolicy,
    ToolSettings,
    TrustLevel,
    UserIntegrationSettings,
    apply_profile_overrides,
    build_profile,
    general_profile,
    plan_profile,
)
from mato_loop_backend.settings.store import SettingsStore
from mato_loop_backend.tools.catalog import tool_registry
from mato_loop_backend.tools.coding import _coding_analyze
from mato_loop_backend.tools.context import ToolContext
from mato_loop_backend.tools.context_search import source_available
from mato_loop_backend.tools.contracts import ToolResult
from mato_loop_backend.tools.web import _web_search


@pytest.mark.parametrize("profile,enabled", [
    (general_profile(), False),
    (plan_profile(), True),
    (build_profile(), True),
    (AgentProfile(name="Custom", instructions="Task"), False),
])
def test_todo_tool_follows_profile_availability(profile, enabled):
    tools = ToolSettings()
    tools._agent_profile = profile

    effective = tool_registry.effective(IntegrationSettings(), tool_settings=tools)

    assert ("todo_write" in effective.tools) is enabled


@pytest.mark.parametrize("profile", [plan_profile(), build_profile()])
def test_coding_profiles_share_slim_overridable_tool_baseline(profile):
    assert profile.tool_availability == plan_profile().tool_availability == build_profile().tool_availability
    for name in ("task", "web_search", "webfetch", "search_tools", "memory_retrieve", "update_agent_identity"):
        assert profile.tool_availability.get(name) is not False
    assert source_available(ToolSettings(tool_availability=profile.tool_availability), "memories")
    for name in ("memory_write", "list_automations", "select_execution_agent", "save_skill"):
        assert profile.tool_availability[name] is False
    overridden = apply_profile_overrides(profile, AgentProfileAccessOverrides(
        tool_availability={"memory_write": True},
    ))
    assert overridden.tool_availability["memory_write"] is True


@pytest.mark.parametrize("temporary,available", [(False, True), (True, True), (True, False)])
def test_coding_profiles_share_file_schemas_and_respect_availability(temporary, available):
    tools = ToolSettings(tool_availability={"write_file": available})
    tools._agent_permissions = plan_profile().private_permissions
    tools._temporary_files_enabled = temporary
    first = tool_registry.effective(IntegrationSettings(), tool_settings=tools)
    tools._agent_permissions = build_profile().private_permissions
    second = tool_registry.effective(IntegrationSettings(), tool_settings=tools)
    assert first.schemas() == second.schemas()
    assert ("write_file" in first.tools) is available
    if available:
        assert first.tools["write_file"].schema() == tool_registry._definitions["write_file"].schema()
    tools._agent_permissions.policies.update({
        "filesystem.write": ToolExecutionPolicy.DISABLED,
        "session.files.write": ToolExecutionPolicy.DISABLED,
    })
    restricted = tool_registry.effective(IntegrationSettings(), tool_settings=tools)
    assert ("write_file" in restricted.tools) is (temporary and available)


@pytest.mark.parametrize("model", [AgentProfile, AgentProfileOverrides])
@pytest.mark.parametrize("value", ["timeline", "compact"])
def test_removed_answer_display_is_rejected(model, value):
    values = {"answerDisplay": value}
    if model is AgentProfile:
        values.update(name="Agent", instructions="Task")
    with pytest.raises(ValidationError):
        model(**values)


@pytest.mark.parametrize("model", [AgentProfile, AgentProfileOverrides])
@pytest.mark.parametrize("value", [0, -1, True, False, 1.0, "25"])
def test_profile_limit_requires_a_positive_strict_integer(model, value):
    values = {"maxToolCalls": value}
    if model is AgentProfile:
        values.update(name="Agent", instructions="Task")
    with pytest.raises(ValidationError):
        model(**values)


@pytest.mark.parametrize("model", [AgentProfile, AgentProfileOverrides])
@pytest.mark.parametrize(
    "overrides",
    [{}, {str(uuid4()): True}],
)
def test_removed_profile_connection_assignments_are_rejected(model, overrides):
    values = {"integrationAvailability": overrides}
    if model is AgentProfile:
        values.update(name="Agent", instructions="Task")
    with pytest.raises(ValidationError):
        model(**values)


def account(kind="calendar", adapter="caldav", **values):
    config = {"base_url": "https://example.com", "username": "user"}
    if kind == "mail":
        config = {
            "imap_host": "mail.example.com",
            "imap_port": 993,
            "imap_security": "tls",
            "smtp_host": "mail.example.com",
            "smtp_port": 465,
            "smtp_security": "tls",
            "username": "user",
            "from_address": "user@example.com",
        }
    return IntegrationInstance(
        name=adapter,
        integration_type=kind,
        adapter_type=adapter,
        config=config,
        **values,
    )


def mock_settings(monkeypatch, integrations, profile, *, personal=None):
    async def load(_db, model, **_kwargs):
        if model is IntegrationSettings:
            return integrations.model_copy(deep=True)
        if model is AgentProfilesSettings:
            return AgentProfilesSettings(profiles=[CustomAgentProfile(
                id=profile.id,
                name=profile.name,
                description=profile.description,
                baseProfileId=GENERAL_PROFILE_ID,
                overrides=profile.model_dump(
                    exclude={"id", "name", "description", "owner_id", "enabled"}
                ),
            )])
        return model()

    async def optional(_db, model, **_kwargs):
        if model is AgentProfilesSettings:
            return await load(_db, model)
        return personal if model is UserIntegrationSettings else None

    monkeypatch.setattr(SettingsStore, "load", AsyncMock(side_effect=load))
    monkeypatch.setattr(SettingsStore, "load_optional", AsyncMock(side_effect=optional))


async def test_profile_update_preserves_omitted_access_defaults(monkeypatch):
    profile = AgentProfile(
        name="Agent",
        instructions="Task",
        maxToolCalls=None,
        integrationsEnabledByDefault=False,
    )
    mock_settings(monkeypatch, IntegrationSettings(), profile)
    user = UserAccount(id=uuid4(), username="admin", password_hash="hash", is_admin=True)
    existing = CustomAgentProfile(
        id=profile.id, name=profile.name, description="",
        baseProfileId=GENERAL_PROFILE_ID,
        overrides={"maxToolCalls": None, "integrationsEnabledByDefault": False},
    )
    for overrides in ({}, {"maxToolCalls": None}):
        result = await _profile_from_request(
            AsyncMock(),
            user,
            None,
            AgentProfileRequest(
                name="Agent", description="", baseProfileId=GENERAL_PROFILE_ID,
                overrides=overrides,
            ),
            profile_id=profile.id,
            existing=existing,
        )
        assert result.overrides.model_dump(exclude_unset=True, by_alias=True) == {
            **overrides, "integrationsEnabledByDefault": False,
        }
    result = await _profile_from_request(
        AsyncMock(),
        user,
        None,
        AgentProfileRequest(
            name="Agent",
            description="",
            baseProfileId=GENERAL_PROFILE_ID,
            overrides={"maxToolCalls": None},
        ),
        existing=existing,
    )
    assert result.overrides.max_tool_calls is None
    assert result.overrides.integrations_enabled_by_default is False


async def test_profile_scope_validation_and_listing_include_option_only_integrations(monkeypatch):
    search = account("search", "searxng")
    coding = account("coding", "opencode")
    profile = AgentProfile(name="Agent", instructions="Task")
    mock_settings(
        monkeypatch,
        IntegrationSettings(instances=[search]),
        profile,
        personal=UserIntegrationSettings(instances=[coding]),
    )
    user = UserAccount(id=uuid4(), username="admin", password_hash="hash", is_admin=True)
    request = AgentProfileRequest(
        name="Agent", description="", baseProfileId=GENERAL_PROFILE_ID,
        overrides={"integrationsEnabledByDefault": True},
    )
    with pytest.raises(ApiError, match="tools.usePermissionSettings"):
        await _profile_from_request(AsyncMock(), user, None, request)
    with pytest.raises(ApiError, match="tools.usePermissionSettings"):
        await _profile_from_request(AsyncMock(), user, user.id, request)
    instance_result = await _response(AsyncMock(), user, scope="instance")
    personal_result = await _response(AsyncMock(), user, scope="personal")
    assert [item["id"] for item in instance_result["integrations"]] == [str(search.id)]
    assert {item["id"] for item in personal_result["integrations"]} == {
        str(search.id),
        str(coding.id),
    }


def test_shared_accounts_opt_in_is_separate_from_permissions_and_does_not_mutate_source():
    first, hidden = account(), account()
    settings = IntegrationSettings(instances=[first, hidden])
    profile = AgentProfile(
        name="Agent",
        instructions="Task",
        integrationsEnabledByDefault=False,
        privatePermissions={"policies": {"calendar.read": "ask"}},
    )
    first.profile_permissions[profile.id] = IntegrationPermissions(available=True)
    hidden.profile_permissions[profile.id] = IntegrationPermissions(available=False)
    tools = ToolSettings()
    tools._agent_profile = profile
    before = settings.model_dump_json(), profile.model_dump_json(), tools.model_dump_json()
    effective = tool_registry.effective(settings, tool_settings=tools)
    bound = effective.tools["calendar_list_calendars"]
    assert set(dict(bound.integration_revisions)) == {str(first.id)}
    assert bound.schema()["parameters"]["properties"]["integration_id"]["enum"] == [str(first.id)]
    assert str(hidden.id) not in json.dumps(effective.schemas())
    filtered = filter_agent_profile_integrations(settings, profile)
    assert [item.id for item in filtered.instances] == [first.id]
    assert filter_agent_profile_integrations(settings, profile, allowed_ids=[]).instances == []
    assert before == (
        settings.model_dump_json(),
        profile.model_dump_json(),
        tools.model_dump_json(),
    )
    profile.integrations_enabled_by_default = True
    future = account()
    settings.instances.append(future)
    assert [item.id for item in filter_agent_profile_integrations(settings, profile).instances] == [
        first.id,
        future.id,
    ]
    assert (first.profile_permissions[profile.id].available, hidden.profile_permissions[profile.id].available) == (True, False)


@pytest.mark.parametrize(
    "kind,adapter,tool_name",
    [
        ("openscad", "openscad", "render_openscad"),
        ("coding", "opencode", "coding_analyze"),
        ("search", "searxng", "web_search"),
    ],
)
def test_profile_selection_covers_execution_builtin_and_option_tools(kind, adapter, tool_name):
    integration = account(
        kind, adapter, execution_agent_id=uuid4() if kind == "openscad" else None
    )
    if kind == "openscad":
        integration.config = {"binary_path": "/usr/bin/openscad"}
    profile = AgentProfile(name="Agent", instructions="Task", integrationsEnabledByDefault=False)
    settings = IntegrationSettings(instances=[integration])
    tools = ToolSettings()
    tools._agent_profile = profile
    if kind == "search":
        tools.web_search.primary_provider = f"searxng:{integration.id}"
    assert (
        tool_name
        not in tool_registry.effective(
            settings, tool_settings=tools, tool_capabilities={"project.coding"}
        ).tools
    )
    integration.profile_permissions[profile.id] = IntegrationPermissions(available=True)
    effective = tool_registry.effective(
        settings, tool_settings=tools, tool_capabilities={"project.coding"}
    )
    assert tool_name in effective.tools
    if kind == "coding":
        assert (
            tool_name
            not in tool_registry.effective(
                settings,
                tool_settings=tools,
                tool_capabilities={"project.coding"},
                coding_integration_id=uuid4(),
            ).tools
        )
    if kind == "search":
        integration.profile_permissions[profile.id].available = False
        filtered = filter_agent_profile_integrations(settings, profile)
        assert not effective_integration_contributions(filtered).tool_options
        tools.web_search.primary_provider = "no_key"
        assert "web_search" in tool_registry.effective(settings, tool_settings=tools).tools


async def tool_context(monkeypatch, settings, profile, call):
    mock_settings(monkeypatch, settings, profile)
    session = ChatSession(id=uuid4(), user_id=uuid4(), agent_profile_id=profile.id)
    tools, loaded_profile = await load_agent_profile_tools(
        AsyncMock(), session.user_id, profile.id, TrustLevel.PRIVATE
    )
    assert tools._agent_profile == loaded_profile
    return ToolContext(
        db=AsyncMock(),
        session=session,
        run_id=uuid4(),
        tool_call=call,
        root_files=None,
        integration_settings=settings,
        tool_settings=tools,
        ask_handler=AsyncMock(return_value=PermissionDecision.ALLOW),
    )


@pytest.mark.parametrize("revoke", ["before", "approval", "tamper", "none"])
async def test_shared_binding_guards_selection_before_and_after_approval(monkeypatch, revoke):
    first, hidden = account(), account()
    settings = IntegrationSettings(instances=[first, hidden])
    profile = AgentProfile(
        name="Agent",
        instructions="Task",
        integrationsEnabledByDefault=False,
    )
    first.profile_permissions[profile.id] = IntegrationPermissions(available=True)
    call = ToolCall(
        id="call",
        name="calendar_list_calendars",
        arguments={"integration_id": str(hidden.id if revoke == "tamper" else first.id)},
    )
    context = await tool_context(monkeypatch, settings, profile, call)
    effective = tool_registry.effective(settings, tool_settings=context.tool_settings)
    bound = effective.tools[call.name]
    handler = AsyncMock(return_value=ToolResult.json({"ok": True}))
    effective.tools[call.name] = replace(
        bound, definition=replace(bound.definition, handler=handler)
    )
    if revoke == "before":
        first.profile_permissions[profile.id].available = False
    if revoke == "approval":

        async def approve(_request):
            first.profile_permissions[profile.id].available = False
            return PermissionDecision.ALLOW

        context.ask_handler = approve
    result = await effective.execute(call, context)
    assert result.is_error is (revoke != "none")
    if revoke != "none":
        assert result.metadata["errorCode"] == "tool.unavailable"
        handler.assert_not_awaited()
    else:
        assert [item.id for item in context.integration_settings.instances] == [first.id]
        handler.assert_awaited_once()


@pytest.mark.parametrize("kind,adapter", [("search", "searxng"), ("coding", "opencode")])
async def test_search_and_coding_recheck_selection_after_approval(monkeypatch, kind, adapter):
    pass
    from mato_loop_backend.tools.arguments import CodingAnalyzeArguments, WebSearchArguments

    integration = account(kind, adapter)
    profile = AgentProfile(name="Agent", instructions="Task")
    settings = IntegrationSettings(instances=[integration])
    call = ToolCall(id="call", name="web_search", arguments={"query": "test"})
    context = await tool_context(monkeypatch, settings, profile, call)

    async def approve(_request):
        integration.profile_permissions[profile.id] = IntegrationPermissions(available=False)
        return PermissionDecision.ALLOW

    context.ask_handler = approve
    if integration.adapter_type == "searxng":
        tools = ToolSettings(web_search={"primary_provider": f"searxng:{integration.id}"})
        monkeypatch.setattr(mock_tools_access, 'profile_tool_settings', AsyncMock(return_value=tools))
        execute = AsyncMock()
        monkeypatch.setattr(mock_tools_web, 'search_web', execute)
        result = await _web_search(WebSearchArguments(query="test"), context)
    else:
        context.session.project_id = uuid4()
        context.db.scalar.return_value = Project(
            id=context.session.project_id,
            user_id=context.session.user_id,
            name="Project",
            coding_integration_id=integration.id,
        )
        execute = AsyncMock()
        monkeypatch.setattr(mock_tools_coding.ProjectCodingService, "analyze", execute)
        result = await _coding_analyze(
            CodingAnalyzeArguments(title="Task", brief="Inspect"), context
        )
    assert result.is_error
    execute.assert_not_awaited()


@pytest.mark.parametrize("delegated", [False, True])
@pytest.mark.parametrize("kind,adapter", [("calendar", "caldav"), ("mail", "imap_smtp")])
async def test_account_discovery_and_helper_reloads_never_expand_the_selection(
    monkeypatch, delegated, kind, adapter
):
    from mato_loop_backend.agent.subagents import permission_snapshot

    first, hidden = account(kind, adapter), account(kind, adapter)
    settings = IntegrationSettings(instances=[first, hidden])
    profile = AgentProfile(
        name="Agent",
        instructions="Task",
        integrationsEnabledByDefault=False,
    )
    first.profile_permissions[profile.id] = IntegrationPermissions(available=True)
    call = ToolCall(id="call", name=f"{kind}_list_accounts", arguments={})
    context = await tool_context(monkeypatch, settings, profile, call)
    if delegated:
        context.tool_settings._delegation_ceiling = permission_snapshot(
            context.tool_settings, set(), IntegrationSettings(instances=[first])
        )
        profile.integrations_enabled_by_default = True
        context.tool_settings._agent_profile = profile.model_copy(deep=True)
    effective = tool_registry.effective(settings, tool_settings=context.tool_settings)
    result = await effective.execute(call, context)
    assert not result.is_error
    assert json.loads(result.content)["accounts"] == [
        {"integrationId": str(first.id), "name": first.name}
    ]
    assert [item.id for item in (await context.current_integrations()).instances] == [first.id]
