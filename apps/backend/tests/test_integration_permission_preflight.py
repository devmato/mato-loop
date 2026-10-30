from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel

import mato_loop_backend.tools.access as mock_tools_access
import mato_loop_backend.tools.catalog as mock_tools_catalog
import mato_loop_backend.tools.executor as mock_tools_executor
from mato_loop_backend.integrations import registry as integration_registry
from mato_loop_backend.integrations.contracts import (
    IntegrationAdapterDefinition,
    IntegrationResource,
    ToolContribution,
)
from mato_loop_backend.models import ChatSession
from mato_loop_backend.permissions.policy import PermissionDecision, PermissionRequest
from mato_loop_backend.providers.base import ToolCall
from mato_loop_backend.settings import (
    AgentProfilePermissions,
    AgentProfileUnavailable,
    IntegrationInstance,
    IntegrationSettings,
    ToolExecutionPolicy,
    ToolSettings,
)
from mato_loop_backend.tools.access import profile_tool_settings
from mato_loop_backend.tools.actions import ToolAction
from mato_loop_backend.tools.context import ToolContext
from mato_loop_backend.tools.contracts import ToolResult


class _Arguments(BaseModel):
    integration_id: UUID
    resource_key: str


class _Config(BaseModel):
    pass


@pytest.fixture
def preflight(monkeypatch):
    events = []
    owner_id = uuid4()
    instance = IntegrationInstance(
        name="Test",
        integration_type="tasks",
        adapter_type="preflight",
        owner_id=owner_id,
        credential_id=uuid4(),
    )
    integrations = IntegrationSettings(instances=[instance])
    settings = ToolSettings()
    resource = IntegrationResource(
        "project:main",
        "host-only-label",
        binding="host-only-binding",
        hard_unavailable_capabilities=frozenset({"tasks.read"}),
    )
    resources = [resource]

    async def discover(_instance, credential):
        assert credential == {"token": "host-only-credential"}
        events.append("metadata")
        return tuple(resources)

    async def authorize(_request):
        events.append("authorization")
        return PermissionDecision.ALLOW

    content = AsyncMock(return_value="approved-content")
    mutation = AsyncMock()

    async def execute(_arguments, context):
        assert context.bound_resource is resources[0]
        events.append("content")
        value = await content()
        events.append("mutation")
        await mutation()
        return ToolResult.json({"value": value})

    request = PermissionRequest(
        capability="tasks.write",
        resource_type="integration_operation",
        resource_scope="project:main",
        action_kind="tasks_write",
        risk_level="medium",
        title="Aufgaben schreiben?",
        summary="Der Agent möchte Aufgaben schreiben.",
    )
    authorization_factory = Mock(return_value=(request,))
    handler = AsyncMock(side_effect=execute)
    loader = AsyncMock(side_effect=discover)
    action = ToolAction(
        "tasks.write",
        "Schreiben",
        category="write",
        mutates=True,
    )
    contribution = ToolContribution(
        name="preflight_write",
        description="Write to one test resource.",
        arguments_model=_Arguments,
        handler=handler,
        actions=(action,),
        instance_argument="integration_id",
        resource_argument="resource_key",
        authorization_factory=authorization_factory,
        releases_private_data=True,
    )
    monkeypatch.setitem(
        integration_registry._ADAPTERS,
        ("tasks", "preflight"),
        IntegrationAdapterDefinition(
            integration_type="tasks",
            adapter_type="preflight",
            config_model=_Config,
            resource_actions=(
                action,
                ToolAction(
                    "tasks.read", "Lesen", category="read"
                ),
            ),
            resource_loader=loader,
            shared_tools=(contribution,),
        ),
    )
    current = AsyncMock(return_value=integrations)
    monkeypatch.setattr(ToolContext, "current_integrations", current)
    monkeypatch.setattr(mock_tools_access, 'profile_tool_settings', AsyncMock(return_value=settings))
    credential = AsyncMock(return_value=object())
    _mock_value = credential
    for _mock_target in (mock_tools_executor,):
        monkeypatch.setattr(_mock_target, 'load_credential', _mock_value)
    _mock_value = Mock(return_value={"token": "host-only-credential"})
    for _mock_target in (mock_tools_executor,):
        monkeypatch.setattr(_mock_target, 'read_credential', _mock_value)
    call = ToolCall(
        "preflight", "preflight_write",
        {"integration_id": str(instance.id), "resource_key": resource.key},
    )
    context = ToolContext(
        db=AsyncMock(),
        run_id=uuid4(),
        session=ChatSession(id=uuid4(), user_id=owner_id),
        tool_call=call,
        root_files=Mock(),
        integration_settings=integrations,
        tool_settings=settings,
        ask_handler=AsyncMock(side_effect=authorize),
    )
    effective = mock_tools_catalog.ToolRegistry([]).effective(integrations)
    return SimpleNamespace(
        effective=effective, call=call, context=context, instance=instance,
        integrations=integrations, settings=settings, resource=resource, resources=resources,
        loader=loader, current=current, credential=credential, handler=handler,
        content=content, mutation=mutation, authorization_factory=authorization_factory,
        events=events,
    )


@pytest.mark.parametrize("failure", ["missing", "inapplicable", "hard_limit", "discovery_error"])
@pytest.mark.parametrize("while_waiting", [False, True])
async def test_unavailable_resource_never_executes(preflight, failure, while_waiting):
    case = preflight

    def revoke():
        if failure == "missing":
            case.resources.clear()
        elif failure == "inapplicable":
            case.resources[0] = replace(case.resource, applicable_capabilities=frozenset())
        elif failure == "hard_limit":
            case.resources[0] = replace(
                case.resource, hard_unavailable_capabilities=frozenset({"tasks.write"})
            )
        else:
            case.loader.side_effect = ValueError("host-only-discovery-error")

    async def approve(_request):
        revoke()
        return PermissionDecision.ALLOW

    if while_waiting:
        case.context.ask_handler.side_effect = approve
    else:
        revoke()

    result = await case.effective.execute(case.call, case.context)

    assert result.is_error
    assert result.metadata == {"errorCode": "tool.unavailable"}
    assert "host-only" not in repr(result)
    assert case.context.ask_handler.await_count == int(while_waiting)
    assert case.loader.await_count == 1 + int(while_waiting)
    assert case.context.bound_resource is None
    case.handler.assert_not_awaited()
    case.content.assert_not_awaited()
    case.mutation.assert_not_awaited()
    assert not case.context.root_files.mock_calls


async def test_allowed_resource_is_revalidated_without_metadata_release(preflight):
    case = preflight

    result = await case.effective.execute(case.call, case.context)

    assert not result.is_error
    assert result.content == '{"value": "approved-content"}'
    assert result.metadata == {"privateIntegrationData": True}
    assert "host-only" not in repr(result)
    assert case.events == ["metadata", "authorization", "metadata", "content", "mutation"]
    request = case.context.ask_handler.await_args.args[0]
    assert request.policy_integration_id == case.instance.id
    assert request.policy_resource_key == case.resource.key
    assert request.policy_hard_unavailable_capabilities == frozenset({"tasks.read"})
    assert "host-only" not in repr(request)
    case.handler.assert_awaited_once()
    case.content.assert_awaited_once()
    case.mutation.assert_awaited_once()
    case.credential.assert_awaited_with(
        case.context.db, case.instance.credential_id, owner_id=case.context.session.user_id
    )


async def test_approval_uses_refreshed_labels_instead_of_batch_settings(preflight, monkeypatch):
    case = preflight
    refreshed = ToolSettings(resource_labels={str(case.instance.id): {case.resource.key: "Project name"}})
    monkeypatch.setattr(mock_tools_access, "profile_tool_settings", AsyncMock(return_value=refreshed))
    request = case.authorization_factory.return_value[0]

    def authorization(_arguments, context):
        labels = context.tool_settings.resource_labels.get(str(case.instance.id), {})
        return (replace(request, resource_display=labels.get(case.resource.key, case.resource.key)),)

    case.authorization_factory.side_effect = authorization
    await case.effective.execute(case.call, case.context)
    assert case.context.ask_handler.await_args.args[0].resource_display == "Project name"


async def test_denied_approval_does_not_fetch_content_or_mutate(preflight):
    case = preflight
    case.context.ask_handler.side_effect = None
    case.context.ask_handler.return_value = PermissionDecision.USER_DENIED

    result = await case.effective.execute(case.call, case.context)

    assert result.metadata == {"errorCode": "permission.denied"}
    assert "host-only" not in repr(result)
    assert case.context.bound_resource is None
    case.loader.assert_awaited_once()
    case.context.ask_handler.assert_awaited_once()
    case.handler.assert_not_awaited()
    case.content.assert_not_awaited()
    case.mutation.assert_not_awaited()


@pytest.mark.parametrize("change", ["owner", "revision"])
@pytest.mark.parametrize("when", ["before", "before_metadata", "while_waiting"])
async def test_changed_binding_blocks_metadata_and_execution(preflight, change, when):
    case = preflight
    changed = case.instance.model_copy(deep=True)
    if change == "owner":
        changed.owner_id = uuid4()
    else:
        changed.config = {"endpoint": "changed"}
    revoked = IntegrationSettings(instances=[changed])

    async def approve(_request):
        case.current.return_value = revoked
        return PermissionDecision.ALLOW

    if when == "before":
        case.current.return_value = revoked
    elif when == "before_metadata":
        case.current.side_effect = [case.integrations, revoked]
    else:
        case.context.ask_handler.side_effect = approve

    result = await case.effective.execute(case.call, case.context)

    assert result.metadata == {"errorCode": "tool.unavailable"}
    assert case.context.ask_handler.await_count == int(when == "while_waiting")
    assert case.loader.await_count == int(when == "while_waiting")
    assert case.credential.await_count == int(when == "while_waiting")
    case.handler.assert_not_awaited()
    case.content.assert_not_awaited()
    case.mutation.assert_not_awaited()


@pytest.mark.parametrize("scope", ["integration", "resource"])
async def test_disabled_policy_blocks_metadata_before_approval(preflight, scope):
    case = preflight
    permissions = case.settings.permissions
    if scope == "global":
        permissions.policies["tasks.write"] = ToolExecutionPolicy.DISABLED
    elif scope == "integration":
        permissions.integration_policies[str(case.instance.id)] = {"tasks.write": "disabled"}
    else:
        permissions.resource_policies[str(case.instance.id)] = {
            case.resource.key: {"tasks.write": "disabled"}
        }

    result = await case.effective.execute(case.call, case.context)

    assert result.metadata == {"errorCode": "permission.denied"}
    case.context.ask_handler.assert_not_awaited()
    case.loader.assert_not_awaited()
    case.credential.assert_not_awaited()
    case.handler.assert_not_awaited()
    case.mutation.assert_not_awaited()


@pytest.mark.parametrize("restriction", ["integration", "resource", "profile", "tool", "missing_profile"])
async def test_refreshed_settings_block_metadata_before_approval(preflight, monkeypatch, restriction):
    case = preflight
    assert case.effective.schemas()
    refreshed = case.settings.model_copy(deep=True)
    if restriction == "integration":
        refreshed.permissions.integration_policies[str(case.instance.id)] = {"tasks.write": "disabled"}
    elif restriction == "resource":
        refreshed.permissions.resource_policies[str(case.instance.id)] = {
            case.resource.key: {"tasks.write": "disabled"}
        }
    elif restriction == "profile":
        refreshed._agent_permissions = AgentProfilePermissions(integration_policies={str(case.instance.id): {"tasks.write": "disabled"}})
    elif restriction == "tool":
        refreshed.tool_availability[case.call.name] = False
    case.context.agent_profile_id = uuid4()
    load = AsyncMock(return_value=(refreshed, None))
    if restriction == "missing_profile":
        load.side_effect = AgentProfileUnavailable
        monkeypatch.setattr(mock_tools_access, 'load_effective_tools', AsyncMock(return_value=refreshed))
    monkeypatch.setattr(mock_tools_access, 'load_agent_profile_tools', load)
    monkeypatch.setattr(mock_tools_access, 'profile_tool_settings', profile_tool_settings)

    result = await case.effective.execute(case.call, case.context)

    assert result.is_error
    assert result.metadata == {
        "errorCode": "tool.unavailable" if restriction in {"tool", "missing_profile"} else "permission.denied"
    }
    assert case.context.tool_settings is refreshed
    load.assert_awaited_once_with(
        case.context.db, case.context.session.user_id,
        case.context.agent_profile_id, case.context.model_trust_level,
    )
    case.context.ask_handler.assert_not_awaited()
    case.loader.assert_not_awaited()
    case.credential.assert_not_awaited()
    case.handler.assert_not_awaited()
    case.content.assert_not_awaited()
    case.mutation.assert_not_awaited()


async def test_scoped_policy_can_override_global_disabled(preflight):
    case = preflight
    case.settings.permissions.policies["tasks.write"] = ToolExecutionPolicy.DISABLED
    case.settings.permissions.resource_policies[str(case.instance.id)] = {
        case.resource.key: {"tasks.write": "ask"}
    }

    result = await case.effective.execute(case.call, case.context)

    assert not result.is_error
    case.context.ask_handler.assert_awaited_once()
    case.mutation.assert_awaited_once()


@pytest.mark.parametrize("source_allowed", [False, True])
async def test_preflight_does_not_bypass_handler_source_permission(preflight, source_allowed):
    case = preflight
    execute = case.handler.side_effect
    source_request = PermissionRequest(
        capability="session.files.read",
        resource_type="root_path",
        resource_scope="source.txt",
        action_kind="read",
        risk_level="low",
        title="Datei lesen?",
        summary="Der Agent möchte eine Quelldatei lesen.",
        use_integration_policy=False,
    )

    async def with_source_permission(arguments, context):
        if not await mock_tools_access.allow(context, source_request):
            return ToolResult.error("permission.denied")
        return await execute(arguments, context)

    case.handler.side_effect = with_source_permission
    case.context.ask_handler.side_effect = [
        PermissionDecision.ALLOW,
        PermissionDecision.ALLOW if source_allowed else PermissionDecision.DENY,
    ]

    result = await case.effective.execute(case.call, case.context)

    assert result.is_error is not source_allowed
    assert case.context.ask_handler.await_count == 2
    assert case.context.ask_handler.await_args.args[0] is source_request
    assert source_request.policy_integration_id is None
    assert not source_request.policy_hard_unavailable_capabilities
    assert case.content.await_count == int(source_allowed)
    assert case.mutation.await_count == int(source_allowed)
    assert "host-only" not in repr(result)


async def test_tools_without_resource_argument_skip_metadata_discovery(preflight):
    case = preflight
    case.authorization_factory.return_value = (replace(
        case.authorization_factory.return_value[0],
        use_integration_policy=False, policy_integration_id=case.instance.id,
    ),)
    tool = case.effective.tools[case.call.name]
    case.effective.tools[case.call.name] = replace(
        tool, definition=replace(tool.definition, resource_argument=None)
    )
    case.handler.side_effect = None
    case.handler.return_value = ToolResult.json({"ok": True})

    result = await case.effective.execute(case.call, case.context)

    assert not result.is_error
    case.context.ask_handler.assert_awaited_once()
    assert case.context.ask_handler.await_args.args[0].policy_integration_id == case.instance.id
    case.handler.assert_awaited_once()
    case.loader.assert_not_awaited()
    case.credential.assert_not_awaited()


async def test_authorization_factory_value_error_is_safely_rejected(preflight, monkeypatch):
    case = preflight
    case.authorization_factory.side_effect = ValueError("invalid host-only-token")
    record = Mock()
    monkeypatch.setattr(mock_tools_executor, "schedule_diagnostic_event", record)

    result = await case.effective.execute(case.call, case.context)

    assert result.is_error
    assert result.metadata == {"errorCode": "tool.executionFailed"}
    assert "host-only" not in repr(result)
    record.assert_called_once_with(
        case.context.session.user_id, "tool.failed", component="tool",
        session_id=case.context.session.id, run_id=case.context.run_id,
        correlation_id=case.call.id, payload=record.call_args.kwargs["payload"],
    )
    payload = record.call_args.kwargs["payload"]
    assert set(payload) == {"name", "errorCode", "exceptionClass", "stackTrace"}
    assert payload["name"] == case.call.name
    assert payload["errorCode"] == "tool.executionFailed"
    assert payload["exceptionClass"] == "ValueError"
    assert 0 < len(payload["stackTrace"]) <= 12
    assert all(set(frame) == {"module", "function", "line"} for frame in payload["stackTrace"])
    assert any(
        frame["module"] == mock_tools_executor.__name__
        and frame["function"] == "execute_definition" and frame["line"] > 0
        for frame in payload["stackTrace"]
    )
    assert "host-only" not in repr(payload)
    case.context.ask_handler.assert_not_awaited()
    case.loader.assert_not_awaited()
    case.handler.assert_not_awaited()
    case.content.assert_not_awaited()
    case.mutation.assert_not_awaited()
