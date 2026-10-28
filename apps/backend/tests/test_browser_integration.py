import base64
import hashlib
import json
import sys
from io import BytesIO
from types import SimpleNamespace
from typing import get_args
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import ValidationError

import mato_loop_backend.api.routes.browser_integrations as api_routes_browser_integrations
import mato_loop_backend.integrations.browser.adapter as browser_module
import mato_loop_backend.tools.access as mock_tools_access
import mato_loop_backend.tools.catalog as mock_tools_catalog
import mato_loop_backend.tools.context as mock_tools_context
from mato_loop_backend.agent.run_support import _effective_execution_operations
from mato_loop_backend.api.errors import register_error_handlers
from mato_loop_backend.db.session import get_db_session
from mato_loop_backend.error_contract import describe_error
from mato_loop_backend.execution.browser_contracts import (
    BROWSER_SNAPSHOT_ACTIONS,
    BrowserActionArguments,
    BrowserActionResult,
    BrowserConfig,
    BrowserErrorCode,
    BrowserExecuteArguments,
    BrowserFileArguments,
    BrowserFileResult,
    BrowserInspectionData,
    BrowserSetupReadResult,
    BrowserSetupWriteArguments,
    BrowserStatusResult,
)
from mato_loop_backend.execution.client import ExecutionClient, ExecutionClientError
from mato_loop_backend.execution.manager import ExecutionAgentError, disconnect_error_code
from mato_loop_backend.execution.protocol import SUPPORTED_OPERATIONS, ResponseMessage
from mato_loop_backend.files.service import RootFileError
from mato_loop_backend.integrations.browser.adapter import (
    BROWSER_ADAPTER,
    BROWSER_DOWNLOADS,
    BROWSER_TOOL_ACTIONS,
    BROWSER_TOOLS,
    BROWSER_UPLOAD,
    BrowserEvaluateArguments,
    BrowserRunCodeArguments,
)
from mato_loop_backend.integrations.registry import (
    effective_execution_integration_settings,
    effective_integration_contributions,
)
from mato_loop_backend.models import ChatSession
from mato_loop_backend.permissions.policy import PermissionDecision
from mato_loop_backend.providers.base import ToolCall
from mato_loop_backend.security import require_user
from mato_loop_backend.settings import (
    AgentProfile,
    IntegrationInstance,
    IntegrationSettings,
    SettingsStore,
    ToolSettings,
    UserIntegrationSettings,
    UserToolSettings,
    load_effective_integrations,
)
from mato_loop_backend.settings.effective import filter_agent_profile_integrations
from mato_loop_backend.tools.context import ToolContext


def _instance():
    return IntegrationInstance(
        name="Browser", integration_type="browser", adapter_type="browser",
        execution_agent_id=uuid4(), owner_id=uuid4(),
    )


@pytest.mark.parametrize("payload", [
    {"action": "eval", "text": "process.env"},
    {"action": "setup"}, {"action": "navigate", "url": "https://example.org"},
    {"action": "fill", "tabId": str(uuid4()), "selector": "input"},
    {"action": "upload", "tabId": str(uuid4()), "selector": "input", "uploadId": "/etc/passwd"},
    {"action": "tabs", "allowNetwork": True}, {"action": "tabs", "browserId": str(uuid4())},
    {"action": "tabs", "settle": True},
    {"action": "tabs", "expectedBrowserId": str(uuid4())},
    {"action": "click", "tabId": str(uuid4()), "selector": "button", "expectedSnapshot": "a" * 64},
    {"action": "tabs", "text": "unexpected"}, {"action": "tabs", "profilePath": "/tmp"},
    {"action": "tabs", "tabId": "../profile"},
    {"action": "press", "tabId": str(uuid4()), "key": 123},
])
def test_model_action_boundary_rejects_invalid_or_host_only_fields(payload):
    with pytest.raises(ValidationError):
        BrowserActionArguments.model_validate(payload)


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "javascript:alert(1)", "data:text/html,hi", "about:blank",
    "https://user:password@example.org", "https://example.org\\@evil.test",
    "https://example.org\n", "https://example.org:99999", "https://",
])
def test_navigation_is_http_only_without_credentials_or_control_characters(url):
    with pytest.raises(ValidationError):
        BrowserActionArguments(action="navigate", tabId=str(uuid4()), url=url)


def test_config_empty_action_requirements_and_wire_host_fields():
    assert BrowserConfig().model_dump() == {}
    with pytest.raises(ValidationError):
        BrowserConfig(profilePath="/tmp/profile")
    args = BrowserActionArguments(action="fill", tabId=str(uuid4()), selector="input", text="")
    assert args.text == ""
    for invalid in (
        {"selector": "aria-ref=e12", "snapshotSha256": "a" * 64},
        {"selector": "aria-ref=bad"}, {"selector": "aria-ref=e12 >> nth=0"},
    ):
        with pytest.raises(ValidationError):
            BrowserActionArguments(
                action="click", tabId=str(uuid4()), **invalid,
            )
    assert BrowserActionArguments(action="newtab").tab_id is None
    wire = BrowserExecuteArguments(browserId=str(uuid4()), **args.model_dump())
    assert "allowNetwork" not in wire.model_dump()
    schema = BrowserActionArguments.model_json_schema()
    assert schema["additionalProperties"] is False
    assert not {"browserId", "ownerId", "allowNetwork", "settle", "profilePath", "command", "script"} & schema["properties"].keys()
    assert BROWSER_ADAPTER.personal_only


def test_public_browser_tools_have_narrow_static_schemas_and_no_legacy_tool():
    expected = {
        "browser_tabs": {"action", "tabId", "url", "observe"},
        "browser_navigate": {"tabId", "url", "observe"},
        "browser_snapshot": {"tabId", "selector", "depth"},
        "browser_take_screenshot": {"tabId"},
        "browser_click": {"tabId", "ref", "element", "observe"},
        "browser_type": {"tabId", "ref", "text", "slowly", "submit", "observe"},
        "browser_fill_form": {"tabId", "fields", "observe"},
        "browser_press_key": {"tabId", "key", "observe"},
        "browser_scroll": {"tabId", "direction", "observe"},
        "browser_diagnostics": set(),
        "browser_evaluate": {"tabId", "function", "ref", "observe"},
        "browser_run_code": {"tabId", "code", "observe"},
    }
    assert BROWSER_ADAPTER.execution_tools == (
        *BROWSER_TOOLS, BROWSER_UPLOAD, BROWSER_DOWNLOADS,
    )
    upload = mock_tools_catalog.ToolRegistry._contributed_definition(BROWSER_UPLOAD).schema()
    assert set(upload["parameters"]["properties"]) == {
        "_reason", "root", "path", "tabId", "ref", "element", "observe",
    }
    assert upload["parameters"]["properties"]["root"]["enum"] == ["files", "computer"]
    assert BROWSER_UPLOAD.required_execution_operations == (
        "integration.browser.file", "integration.browser.upload",
    )
    assert set(BROWSER_DOWNLOADS.arguments_model.model_json_schema()["properties"]) == {"downloadId"}
    assert set(BROWSER_TOOL_ACTIONS) == set(expected)
    for tool in BROWSER_TOOLS:
        schema = tool.arguments_model.model_json_schema()
        assert set(schema["properties"]) == expected[tool.name]
        assert schema["additionalProperties"] is False
        assert not issubclass(tool.arguments_model, BrowserActionArguments)
        assert tool.result_model is BrowserActionResult
        assert tool.execution_operation == "integration.browser.execute"
    assert BROWSER_TOOL_ACTIONS["browser_tabs"] == "tabs"


@pytest.mark.parametrize("action,fields,internal", [
    ("list", {}, "tabs"), ("new", {"url": "https://example.org"}, "newtab"),
    ("close", {"tabId": str(uuid4())}, "closetab"), ("select", {"tabId": str(uuid4())}, "select"),
])
def test_public_tabs_convert_only_action_specific_arguments(action, fields, internal):
    model = browser_module.BrowserTabsArguments
    assert model(action=action, **fields).to_action().action == internal
    invalid = {"tabId": str(uuid4())} if action in {"list", "new"} else {"url": "https://example.org"}
    with pytest.raises(ValidationError):
        model(action=action, **{**fields, **invalid})


@pytest.mark.parametrize("model,source", [
    (browser_module.BrowserEvaluateArguments, {"function": "() => 1"}),
    (browser_module.BrowserRunCodeArguments, {"code": "async def run(page):\n    return 1"}),
])
def test_public_script_source_limits_and_host_only_flags(model, source):
    arguments = {"tabId": str(uuid4()), **source}
    assert model(**arguments).to_action().action in {"run_code", "evaluate"}
    key = next(iter(source))
    assert getattr(model(**{**arguments, key: "x" * 32768}), key) == "x" * 32768
    for extra in ({key: "x" * 32769}, {key: 3}, {"guard": True}, {"settle": True},
                  {"allowNetwork": True}, {"snapshotSha256": "a" * 64}, {"slowly": False}, {"submit": False}):
        with pytest.raises(ValidationError):
            model(**{**arguments, **extra})


@pytest.mark.parametrize("script", [
    {"value": float("nan")}, {"value": "x" * 32768}, {"output": "x" * 32769},
    {"value": "\u00e4" * 16384}, {"outputTruncated": 1}, {"errorCode": "private.error"},
    {"error": "raw private exception"},
    {"failureReason": "runtimeError"}, {"line": 1},
    {"errorCode": "browser.outcomeUnknown", "failureReason": "rawException"},
    {"errorCode": "browser.outcomeUnknown", "failureReason": "runtimeError",
     "failureDetail": "resultTooLarge"},
    {"errorCode": "browser.outcomeUnknown", "failureReason": "invalidResult",
     "failureDetail": "typeError"},
])
def test_script_results_reject_unbounded_or_uncontrolled_data(script):
    with pytest.raises(ValidationError):
        BrowserActionResult(script=script)


def test_historical_script_error_without_diagnostics_remains_readable():
    assert BrowserActionResult(
        script={"errorCode": "browser.outcomeUnknown"}
    ).script.failure_reason is None


@pytest.mark.parametrize("action", ["tabs", "inspect", "screenshot", "diagnostics"])
@pytest.mark.parametrize("observe", ["omitted", True, False])
def test_browser_read_observe_accepts_full_payload_and_round_trip(action, observe):
    arguments = BrowserActionArguments(
        action=action, tabId=str(uuid4()) if action in {"inspect", "screenshot"} else None,
    ).model_dump()
    if observe == "omitted":
        arguments.pop("observe")
    else:
        arguments["observe"] = observe
    parsed = BrowserActionArguments.model_validate(arguments)
    assert parsed.observe is (True if observe == "omitted" else observe)
    assert BrowserActionArguments.model_validate(parsed.model_dump()) == parsed
    wire = BrowserExecuteArguments(browserId=str(uuid4()), **parsed.model_dump())
    assert BrowserExecuteArguments.model_validate(wire.model_dump()) == wire


@pytest.mark.parametrize("observe", [None, "true", "false", 0, 1])
def test_browser_observe_remains_a_strict_boolean(observe):
    with pytest.raises(ValidationError):
        BrowserActionArguments(action="tabs", observe=observe)


@pytest.mark.parametrize("model,source", [
    (BrowserRunCodeArguments, {"code": "async def run(page): return 1"}),
    (BrowserEvaluateArguments, {"function": "() => 1"}),
])
def test_browser_code_observation_is_explicit(model, source):
    assert model(tabId=str(uuid4()), **source).observe is False


@pytest.mark.parametrize("name,guidance", [
    ("browser_run_code", "page.locator('aria-ref=e12')"),
    ("browser_run_code", "locator.first and locator.last are properties"),
    ("browser_run_code", "verify relevant input values"),
    ("browser_run_code", "inspect current state before retrying"),
    ("browser_evaluate", "normalize missing values with value ?? null"),
    ("browser_snapshot", "Prefer selector and depth"),
])
def test_active_browser_guidance_supports_reliable_compact_workflows(name, guidance):
    assert guidance in next(tool.description for tool in BROWSER_ADAPTER.execution_tools if tool.name == name)


@pytest.mark.parametrize("changes", [
    {"fields": None}, {"fields": []}, {"tabId": None},
    {"selector": "input"}, {"text": "unexpected"}, {"key": "Enter"},
    {"direction": "down"}, {"url": "https://example.org"}, {"uploadId": str(uuid4())},
    {"fields": [{"name": "Field", "ref": "e1", "type": "textbox", "value": ""}] * 2},
    {"fields": [{"name": "Field", "ref": f"e{i}", "type": "textbox", "value": ""} for i in range(21)]},
    {"fields": [{"name": "Field", "ref": f"e{i}", "type": "textbox", "value": "x" * 16385} for i in range(2)]},
])
def test_fill_form_rejects_invalid_action_fields(changes):
    with pytest.raises(ValidationError):
        BrowserActionArguments.model_validate({
            "action": "fill_form", "tabId": str(uuid4()),
            "fields": [{"name": "Field", "ref": "e1", "type": "textbox", "value": ""}],
            **changes,
        })


@pytest.mark.parametrize("changes", [
    {"name": ""}, {"name": "x" * 201}, {"name": 1}, {"ref": "aria-ref=e1"},
    {"ref": "e1 >> input"}, {"ref": "#input"}, {"type": "button"}, {"value": True},
    {"value": "x" * 32769}, {"type": "checkbox", "value": "True"},
    {"type": "radio", "value": "false"}, {"selector": "input"},
])
def test_fill_form_rejects_invalid_native_field(changes):
    with pytest.raises(ValidationError):
        BrowserActionArguments(
            action="fill_form", tabId=str(uuid4()),
            fields=[{"name": "Field", "ref": "e1", "type": "textbox", "value": "", **changes}],
        )


def test_fill_form_boundary_and_host_only_settle():
    fields = [{"name": "N" * 200, "ref": f"f0e{i}", "type": "textbox", "value": ""} for i in range(20)]
    fields[0]["value"] = "x" * 32768
    arguments = BrowserActionArguments(
        action="fill_form", tabId=str(uuid4()), fields=fields,
    )
    assert "fill_form" in BROWSER_SNAPSHOT_ACTIONS
    assert BrowserActionArguments.model_validate(arguments.model_dump()) == arguments
    assert BrowserExecuteArguments(browserId=str(uuid4()), **arguments.model_dump()).settle is False
    for action in ("tabs", "fill", "inspect"):
        with pytest.raises(ValidationError):
            BrowserActionArguments.model_validate({**arguments.model_dump(), "action": action})
    for settle in (None, 1, "true"):
        with pytest.raises(ValidationError):
            BrowserExecuteArguments(browserId=str(uuid4()), **arguments.model_dump(), settle=settle)


@pytest.mark.parametrize("batch", [
    {"completed": -1, "total": 1, "errorCode": "browser.snapshotChanged"},
    {"completed": 21, "total": 21}, {"completed": 0, "total": 0},
    {"completed": 2, "total": 1}, {"completed": 0, "total": 1},
    {"completed": 1, "total": 1, "errorCode": "browser.outcomeUnknown"},
    {"completed": 0, "total": 1, "errorCode": "private.error"},
    {"completed": True, "total": 1}, {"completed": "1", "total": 1},
    {"completed": 1, "total": 1, "secret": "private"},
])
def test_browser_batch_rejects_invalid_progress(batch):
    with pytest.raises(ValidationError):
        BrowserActionResult(batch=batch)


@pytest.mark.parametrize("payload", [
    {"text": "x" * 32769}, {"tabs": [{"id": str(uuid4()), "url": "a", "title": "b", "secret": "x"}]},
    {"screenshot": "not base64"}, {"screenshot": base64.b64encode(b"not png").decode()},
    {"data": {"large": "x" * 32769}}, {"data": {"nan": float("nan")}},
    {"downloads": [{"id": str(uuid4()), "filename": "../secret", "size": 1}]},
    {"downloads": [{"id": str(uuid4()), "filename": "secret\n", "size": 1}]},
    {"tabs": [], "unbounded": {}},
])
def test_bounded_action_result(payload):
    with pytest.raises(ValidationError):
        BrowserActionResult.model_validate(payload)


def test_setup_and_file_contracts_are_bounded():
    with pytest.raises(ValidationError):
        BrowserSetupWriteArguments(setupId=str(uuid4()), data=base64.b64encode(b"x" * 65537).decode())
    with pytest.raises(ValidationError):
        BrowserSetupReadResult(data="!", offset=0, running=True)
    with pytest.raises(ValidationError):
        BrowserStatusResult(state="ready", errorCode="private.secret")
    with pytest.raises(ValidationError):
        BrowserFileArguments(browserId=str(uuid4()), action="begin", filename="../x", size=1)
    with pytest.raises(ValidationError):
        BrowserFileResult(fileId=str(uuid4()), offset=2, size=1, complete=True)


def test_browser_schema_is_available_without_viewer_and_preserves_profile_exclusion():
    instance = _instance()
    settings = IntegrationSettings(instances=[instance])
    assert effective_integration_contributions(settings).tools
    assert [tool.name for tool in effective_integration_contributions(effective_execution_integration_settings(
        settings, instance.execution_agent_id, {"integration.browser.execute"},
    )).tools] == list(BROWSER_TOOL_ACTIONS)
    active = effective_execution_integration_settings(
        settings, instance.execution_agent_id,
        {
            "integration.browser.execute", "integration.browser.file", "integration.browser.upload",
            "integration.browser.files",
        },
    )
    assert [tool.name for tool in effective_integration_contributions(active).tools] == [
        *BROWSER_TOOL_ACTIONS, "browser_upload", "browser_downloads",
    ]
    excluded = filter_agent_profile_integrations(active, None, allowed_ids=[])
    assert not effective_integration_contributions(excluded).tools
    for agent, operations in [(uuid4(), {"integration.browser.execute"}), (instance.execution_agent_id, set())]:
        assert not effective_integration_contributions(effective_execution_integration_settings(
            settings, agent, operations,
        )).tools
    assert _effective_execution_operations(SimpleNamespace(
        execution_operations=["integration.browser.execute", "filesystem.read"], current_directory_snapshot=None,
    )) == {"integration.browser.execute"}
    with pytest.raises(ValidationError):
        UserIntegrationSettings(instances=[instance, instance.model_copy(update={"id": uuid4()})])


@pytest.fixture
def browser_case(monkeypatch):
    instance = _instance()
    settings = IntegrationSettings(instances=[instance])
    session = ChatSession(id=uuid4(), user_id=instance.owner_id)
    connection = SimpleNamespace(
        browser_id=uuid4(), integration_id=instance.id,
        agent_id=instance.execution_agent_id, owner_id=instance.owner_id,
        session_id=session.id, model_available=True, closed=False, check_access=AsyncMock(),
    )
    async def execute(*args):
        arguments = args[-1]
        if arguments["action"] not in {"tabs", "inspect", "screenshot", "diagnostics"} and arguments.get("observe", True):
            return BrowserActionResult(text="- document", data={"snapshotSha256": "a" * 64})
        return BrowserActionResult()

    coordinator = SimpleNamespace(
        current_for=Mock(return_value=connection), execute=AsyncMock(side_effect=execute),
        snapshot=AsyncMock(return_value="a" * 64),
    )
    # The parent owns the coordinator; this test exercises only its published interface.
    monkeypatch.setitem(sys.modules, "mato_loop_backend.execution.browsers", SimpleNamespace(browser_coordinator=coordinator))
    current = AsyncMock(return_value=settings)
    monkeypatch.setattr(ToolContext, "current_integrations", current)
    monkeypatch.setattr(mock_tools_access, 'profile_tool_settings', AsyncMock(return_value=ToolSettings()))
    call = ToolCall("browser-call", "browser_navigate", {"tabId": str(uuid4()), "url": "https://example.org"})
    context = ToolContext(
        db=AsyncMock(), run_id=uuid4(), session=session, tool_call=call,
        root_files=Mock(), integration_settings=settings, ask_handler=AsyncMock(return_value=PermissionDecision.ALLOW),
        execution_client=ExecutionClient(instance.execution_agent_id, frozenset({"integration.browser.execute"})),
    )
    context.db.get.return_value = SimpleNamespace(execution_agent_id=instance.execution_agent_id, execution_operations=["integration.browser.execute"])
    effective = mock_tools_catalog.ToolRegistry([]).effective(settings)
    return SimpleNamespace(
        instance=instance, settings=settings, connection=connection, coordinator=coordinator,
        current=current, context=context, call=call, effective=effective,
    )


@pytest.mark.parametrize("observe", [True, False])
@pytest.mark.parametrize("failed", [True, False])
async def test_worker_code_feedback_preserves_completed_and_attempted_actions(browser_case, observe, failed):
    case = browser_case
    case.call.name = "browser_type"
    case.call.arguments = {"tabId": str(uuid4()), "ref": "e1", "text": "Ada", "submit": True, "observe": observe}
    feedback = {
        "completed": "await page.locator('aria-ref=e1').fill('Ada')",
        "truncated": False,
    }
    if failed:
        feedback.update(attempted="await page.locator('aria-ref=e1').press('Enter')", errorCode="browser.outcomeUnknown")
    else:
        feedback["completed"] += "\nawait page.locator('aria-ref=e1').press('Enter')"
    case.coordinator.execute.side_effect = None
    case.coordinator.execute.return_value = BrowserActionResult(
        playwrightCode=feedback,
        data={"observationFailed": True},
    )
    result = await case.effective.execute(case.call, case.context)
    assert result.is_error is failed
    assert json.loads(result.content)["playwrightCode"] == feedback
    assert result.metadata == {"privateIntegrationData": True, **({"errorCode": "browser.outcomeUnknown"} if failed else {})}
    case.coordinator.execute.assert_awaited_once()
    case.coordinator.snapshot.assert_not_awaited()


@pytest.mark.parametrize("feedback", [
    {"completed": "x" * 32769}, {"completed": "x" * 20000, "attempted": "x" * 20000},
    {"attempted": "await page.close()"}, {"truncated": "true"}, {"errorCode": "private.error"},
])
def test_browser_code_feedback_is_bounded_and_typed(feedback):
    with pytest.raises(ValidationError):
        BrowserActionResult(playwrightCode=feedback)


@pytest.fixture
def guarded_browser_case(browser_case):
    case = browser_case
    case.approval = AsyncMock(return_value=PermissionDecision.ALLOW)

    async def ask(request):
        request = await browser_module.capture_browser_approval(
            request, case.call, case.context.session,
            SimpleNamespace(execution_agent_id=case.connection.agent_id),
            case.context.approval_resolution,
        )
        return await case.approval(request)

    case.context.ask_handler.side_effect = ask
    return case


@pytest.mark.parametrize("action", ["navigate", "click"])
@pytest.mark.parametrize("change", ["access", "owner", "session", "agent", "integration", "binding", "profile", "disabled", "run"])
async def test_browser_revalidates_after_approval(browser_case, change, action):
    case = browser_case
    if action == "click":
        case.call.name = "browser_click"
        case.call.arguments = {"tabId": str(uuid4()), "ref": "e1"}

    async def approve(_request):
        if change == "access":
            case.connection.model_available = False
        elif change in {"owner", "session", "agent", "integration"}:
            setattr(case.connection, f"{change}_id", uuid4())
        elif change == "binding":
            case.connection.browser_id = uuid4()
        elif change == "profile":
            case.current.return_value = IntegrationSettings()
        elif change == "disabled":
            case.instance.enabled = False
        elif change == "run":
            case.context.db.get.return_value.execution_agent_id = uuid4()
        return PermissionDecision.ALLOW

    case.context.ask_handler.side_effect = approve
    result = await case.effective.execute(case.call, case.context)
    assert result.is_error
    case.coordinator.execute.assert_not_awaited()


async def test_wrong_owner_never_asks_or_executes(browser_case):
    case = browser_case
    case.connection.owner_id = uuid4()
    result = await case.effective.execute(case.call, case.context)
    assert result.metadata["errorCode"] == "browser.notConnected"
    case.context.ask_handler.assert_not_awaited()
    case.coordinator.execute.assert_not_awaited()
    case.coordinator.snapshot.assert_not_awaited()


async def test_approval_scope_binds_exact_action_and_live_browser(browser_case):
    case = browser_case
    result = await case.effective.execute(case.call, case.context)
    assert not result.is_error
    requests = [call.args[0] for call in case.context.ask_handler.await_args_list]
    assert [request.capability for request in requests] == ["browser.use"]
    request = requests[0]
    grant_scope = request.effective_session_grants
    assert request.policy_integration_id == case.instance.id
    assert all(
        item.effective_session_grants[0].resource_display
        == "Browser dieses Chats (alle Tabs)"
        for item in requests
    )
    scope = json.loads(request.resource_scope)
    assert scope["integrationId"] == str(case.instance.id)
    args = case.coordinator.execute.await_args.args[-1]
    assert args["expectedBrowserId"] == str(case.connection.browser_id)
    assert "allowNetwork" not in args
    case.call.arguments["url"] = "https://other.example/"
    await case.effective.execute(case.call, case.context)
    assert case.context.ask_handler.await_args.args[0].resource_scope != request.resource_scope
    assert case.context.ask_handler.await_args.args[0].effective_session_grants == grant_scope
    assert result.metadata == {"privateIntegrationData": True}


async def test_denial_never_dispatch(browser_case):
    case = browser_case
    case.context.ask_handler.return_value = PermissionDecision.USER_DENIED
    result = await case.effective.execute(case.call, case.context)
    assert result.metadata["errorCode"] == "permission.denied"
    case.coordinator.execute.assert_not_awaited()


async def test_public_ref_action_rejects_model_supplied_guard(browser_case):
    case = browser_case
    case.call.name = "browser_click"
    case.call.arguments = {
        "tabId": str(uuid4()), "ref": "e12",
        "snapshotSha256": "b" * 64,
    }
    result = await case.effective.execute(case.call, case.context)
    assert result.metadata["errorCode"] == "tool.invalidArguments"
    case.context.ask_handler.assert_not_awaited()
    case.coordinator.execute.assert_not_awaited()


async def test_write_observation_can_be_disabled_or_fail_after_success(browser_case):
    case = browser_case
    case.call.name = "browser_click"
    case.call.arguments = {
        "tabId": str(uuid4()), "ref": "e1",
        "observe": False,
    }
    result = await case.effective.execute(case.call, case.context)
    assert not result.is_error
    assert [call.args[0].capability for call in case.context.ask_handler.await_args_list] == [
        "browser.use"
    ]

    case.context.ask_handler.reset_mock()
    case.call.arguments["observe"] = True
    case.coordinator.execute.side_effect = None
    case.coordinator.execute.return_value = BrowserActionResult(data={"observationFailed": True})
    result = await case.effective.execute(case.call, case.context)
    assert not result.is_error
    assert json.loads(result.content)["data"] == {"observationFailed": True}
    assert [call.args[0].capability for call in case.context.ask_handler.await_args_list] == [
        "browser.use"
    ]


async def test_screenshot_is_a_model_image_not_base64_text(browser_case):
    case = browser_case
    output = BytesIO()
    Image.new("RGB", (2, 2), "white").save(output, format="PNG")
    image = output.getvalue()
    case.coordinator.execute.side_effect = None
    case.coordinator.execute.return_value = BrowserActionResult(
        screenshot={"fileId": str(uuid4()), "size": len(image)},
    )
    case.coordinator.execute.return_value._screenshot_bytes = image
    case.call.name = "browser_take_screenshot"
    case.call.arguments = {"tabId": str(uuid4())}
    result = await case.effective.execute(case.call, case.context)
    assert not result.is_error
    assert result.attachments[0].data == image
    assert result.attachments[0].kind == "image"
    assert result.attachments[0].presentation == "internal"
    assert result.attachments[0].ephemeral
    assert "screenshot" not in result.content
    assert case.context.ask_handler.await_args.args[0].capability == "browser.use"
    assert "observe" not in case.coordinator.execute.await_args.args[-1]


async def test_large_snapshot_becomes_a_readable_session_file_reference(browser_case):
    case = browser_case
    case.call.name = "browser_snapshot"
    case.call.arguments = {"tabId": str(uuid4()), "selector": "main", "depth": 4}
    snapshot = ("- main\n  - button \"Target\"\n" * 2000).encode()
    wire = BrowserActionResult(
        snapshot={"fileId": str(uuid4()), "size": len(snapshot)},
        data={
            "snapshotSha256": "a" * 64,
            "snapshotCharacters": len(snapshot.decode()),
            "snapshotScope": "element",
            "snapshotDepth": 4,
        },
    )
    wire._snapshot_stream = BytesIO(snapshot)
    case.coordinator.execute.side_effect = None
    case.coordinator.execute.return_value = wire
    revision = SimpleNamespace(
        file_id=uuid4(), revision=None, path="browser-snapshot-abcd1234.yml",
        size_bytes=len(snapshot), sha256=hashlib.sha256(snapshot).hexdigest(),
    )

    def create_output(filename, source, *, max_bytes):
        assert filename == "browser-snapshot.yml"
        assert source.read() == snapshot
        assert max_bytes >= len(snapshot)
        return revision

    case.context.root_files.create_output_stream = Mock(side_effect=create_output)
    case.context.root_files.root_identity.return_value = str(uuid4())
    result = await case.effective.execute(case.call, case.context)

    assert not result.is_error
    payload = json.loads(result.content)
    assert payload["snapshotFile"] == {
        "root": "files",
        "path": revision.path,
        "sizeBytes": len(snapshot),
        "sha256": revision.sha256,
    }
    assert "snapshot" not in payload and "text" not in payload
    assert not result.file_actions
    dispatched = case.coordinator.execute.await_args.args[-1]
    assert dispatched["selector"] == "main" and dispatched["depth"] == 4


@pytest.mark.parametrize("action", ["inspect", "navigate"])
async def test_snapshot_storage_failure_preserves_completed_actions(browser_case, action):
    case = browser_case
    case.call.name = "browser_snapshot" if action == "inspect" else "browser_navigate"
    case.call.arguments = {"tabId": str(uuid4())}
    if action == "navigate":
        case.call.arguments["url"] = "https://example.org"
    wire = BrowserActionResult(
        snapshot={"fileId": str(uuid4()), "size": 3},
        data={"snapshotSha256": "a" * 64, "snapshotCharacters": 3},
        **(
            {"playwrightCode": {"completed": "await page.goto('https://example.org')"}}
            if action == "navigate"
            else {}
        ),
    )
    wire._snapshot_stream = BytesIO(b"abc")
    case.coordinator.execute.side_effect = None
    case.coordinator.execute.return_value = wire
    case.context.root_files.create_output_stream.side_effect = RootFileError(
        "file.userQuotaExceeded", "quota"
    )

    result = await case.effective.execute(case.call, case.context)

    if action == "inspect":
        assert result.is_error
        assert result.metadata["errorCode"] == "file.userQuotaExceeded"
    else:
        assert not result.is_error
        payload = json.loads(result.content)
        assert payload["data"] == {"observationFailed": True}
        assert payload["playwrightCode"]["completed"].startswith("await page.goto")


async def test_flexible_result_data_is_only_diagnostics(browser_case):
    case = browser_case
    case.coordinator.execute.side_effect = None
    case.coordinator.execute.return_value = BrowserActionResult(data={"secret": "unexpected"})
    result = await case.effective.execute(case.call, case.context)
    assert result.metadata["errorCode"] == "executionAgent.protocolError"
    assert "unexpected" not in result.content


@pytest.mark.parametrize("data", [
    None, {}, {"snapshot_sha256": "a" * 64}, {"snapshotSha256": "a" * 64, "secret": "page"},
    {"snapshotSha256": "a" * 63}, {"snapshotSha256": "A" * 64},
    {"snapshotSha256": "a" * 64 + "\n"}, {"snapshotSha256": 123},
])
async def test_inspect_accepts_only_exact_snapshot_data(browser_case, data):
    case = browser_case
    with pytest.raises(ValidationError):
        BrowserInspectionData.model_validate(data)
    case.call.name = "browser_snapshot"
    case.call.arguments = {"tabId": str(uuid4())}
    case.coordinator.execute.side_effect = None
    case.coordinator.execute.return_value = BrowserActionResult(text='{"elements":[]}', data=data)
    result = await case.effective.execute(case.call, case.context)
    assert result.metadata["errorCode"] == "executionAgent.protocolError"
    assert "elements" not in result.content


@pytest.mark.parametrize("name,arguments", [
    ("browser_tabs", {"action": "close"}), ("browser_snapshot", {}), ("browser_take_screenshot", {}),
    ("browser_click", {"ref": "e1", "_reason": "Open the requested article"}), ("browser_type", {"ref": "e1", "text": "value"}),
    ("browser_press_key", {"key": "Enter"}), ("browser_scroll", {"direction": "down"}),
])
async def test_page_actions_bind_host_snapshot_before_approval(guarded_browser_case, name, arguments):
    case = guarded_browser_case
    events = []
    tab_id = str(uuid4())
    case.call.name = name
    case.call.arguments = {**arguments, "tabId": tab_id}
    action = case.effective.tools[name].definition.validate_arguments(case.call.arguments).to_action().action
    assert action in BROWSER_SNAPSHOT_ACTIONS
    inspection = '{"url":"https://private.example","text":"private page","elements":[]}'
    snapshot = hashlib.sha256(inspection.encode()).hexdigest()

    async def capture(*args):
        events.append("snapshot")
        assert case.context.bound_options["browser_id"] == case.connection.browser_id
        assert args == (case.context.session.id, case.connection.agent_id, case.instance.id, tab_id)
        return snapshot

    async def approve(request):
        events.append("approval")
        assert request.approval_details["browser"]["snapshotSha256"] == snapshot
        assert "private page" not in repr(request)
        assert "private.example" not in repr(request)
        assert "screenshot" not in request.approval_details["browser"]
        assert case.context.approval_resolution["expected_snapshot"] == snapshot
        return PermissionDecision.ALLOW

    async def execute(*args):
        events.append("execute")
        assert args[-1]["expectedSnapshot"] == snapshot
        assert args[-1]["expectedBrowserId"] == str(case.connection.browser_id)
        if action == "inspect":
            return BrowserActionResult(text=inspection, data={"snapshotSha256": snapshot})
        if action == "screenshot":
            output = BytesIO()
            Image.new("RGB", (2, 2), "white").save(output, format="PNG")
            result = BrowserActionResult(screenshot={"fileId": str(uuid4()), "size": len(output.getvalue())})
            result._screenshot_bytes = output.getvalue()
            return result
        return BrowserActionResult(text=inspection, data={"snapshotSha256": snapshot})

    case.coordinator.snapshot.side_effect = capture
    case.approval.side_effect = approve
    case.coordinator.execute.side_effect = execute
    result = await case.effective.execute(case.call, case.context)
    assert not result.is_error
    assert events == ["snapshot", "approval", "execute"]
    if action == "inspect":
        assert json.loads(result.content)["data"] == {"snapshotSha256": snapshot}
        assert json.loads(result.content)["text"] == inspection


async def test_upload_approval_binds_exact_unobserved_action(browser_case):
    case = browser_case
    tab_id = str(uuid4())
    case.call.name = "browser_upload"
    case.call.arguments = {
        "root": "files", "path": "sample.txt", "tabId": tab_id,
        "ref": "e7", "observe": False,
    }
    case.context.root_files.normalize_relative_path.return_value = "sample.txt"
    case.context.root_files.root_identity.return_value = "session-files"
    case.context.bound_options["integration_id"] = str(case.instance.id)
    case.context.bound_options["binding_revision"] = "{}"
    arguments = BROWSER_UPLOAD.arguments_model.model_validate({
        **case.call.arguments, "root": "session",
    })
    requests = await BROWSER_UPLOAD.authorization_factory(arguments, case.context)

    captured = await browser_module.capture_browser_approval(
        requests[-1], case.call, case.context.session,
        SimpleNamespace(execution_agent_id=case.connection.agent_id),
        case.context.approval_resolution,
    )

    case.coordinator.snapshot.assert_awaited_once_with(
        case.context.session.id, case.connection.agent_id, case.instance.id, tab_id,
    )
    assert captured.approval_details["browser"] == {
        "browserId": str(case.connection.browser_id),
        **arguments.to_action().model_dump(mode="json"),
        "snapshotSha256": "a" * 64,
    }


@pytest.mark.parametrize("name,arguments", [
    ("browser_navigate", {"tabId": str(uuid4()), "url": "https://example.org"}),
    ("browser_tabs", {"action": "new", "url": "https://example.org"}),
    ("browser_tabs", {"action": "list"}), ("browser_diagnostics", {}),
])
async def test_auto_actions_do_not_snapshot(browser_case, name, arguments):
    case = browser_case
    case.call.name = name
    case.call.arguments = arguments
    result = await case.effective.execute(case.call, case.context)
    assert not result.is_error
    case.coordinator.snapshot.assert_not_awaited()
    assert "expectedSnapshot" not in case.coordinator.execute.await_args.args[-1]
    assert "snapshotSha256" not in case.context.ask_handler.await_args.args[0].approval_details["browser"]


@pytest.mark.parametrize("change", ["binding", "access", "owner"])
async def test_browser_changed_during_snapshot_never_asks(guarded_browser_case, change):
    case = guarded_browser_case
    case.call.name = "browser_click"
    case.call.arguments = {"tabId": str(uuid4()), "ref": "e1"}

    async def capture(*_args):
        if change == "binding":
            case.connection.browser_id = uuid4()
        elif change == "access":
            case.connection.model_available = False
        else:
            case.connection.owner_id = uuid4()
        return "a" * 64

    case.coordinator.snapshot.side_effect = capture
    result = await case.effective.execute(case.call, case.context)
    assert result.metadata["errorCode"] in {"browser.bindingChanged", "browser.notConnected"}
    case.approval.assert_not_awaited()
    case.coordinator.execute.assert_not_awaited()


@pytest.mark.parametrize("invalid", [None, "raw private page", "A" * 64, "a" * 65, {"snapshotSha256": "a" * 64}])
async def test_invalid_host_snapshot_never_asks_or_leaks_content(guarded_browser_case, invalid):
    case = guarded_browser_case
    case.call.name = "browser_click"
    case.call.arguments = {"tabId": str(uuid4()), "ref": "e1"}
    case.coordinator.snapshot.return_value = invalid
    result = await case.effective.execute(case.call, case.context)
    assert result.metadata["errorCode"] == "executionAgent.protocolError"
    assert "private page" not in result.content
    case.approval.assert_not_awaited()
    case.coordinator.execute.assert_not_awaited()


async def test_snapshot_changes_guard_not_grant_scope_and_stale_rejection_is_not_retried(guarded_browser_case):
    case = guarded_browser_case
    case.call.name = "browser_click"
    case.call.arguments = {"tabId": str(uuid4()), "ref": "e1"}
    await case.effective.execute(case.call, case.context)
    original_scope = case.context.ask_handler.await_args.args[0].resource_scope
    case.coordinator.snapshot.return_value = "b" * 64
    await case.effective.execute(case.call, case.context)
    assert case.context.ask_handler.await_args.args[0].resource_scope == original_scope
    assert case.approval.await_args.args[0].approval_details["browser"]["snapshotSha256"] == "b" * 64

    # The coordinator owns the locked comparison; its stale result must stop the tool.
    case.coordinator.execute.reset_mock()
    case.coordinator.execute.side_effect = ExecutionClientError("browser.snapshotChanged")
    result = await case.effective.execute(case.call, case.context)
    assert result.metadata["errorCode"] == "browser.snapshotChanged"
    assert case.coordinator.execute.await_count == 1
    assert case.coordinator.execute.await_args.args[-1]["expectedSnapshot"] == "b" * 64


async def test_auto_page_action_dispatches_without_captured_snapshot(browser_case):
    case = browser_case
    case.call.name = "browser_click"
    case.call.arguments = {"tabId": str(uuid4()), "ref": "e1"}
    result = await case.effective.execute(case.call, case.context)
    assert not result.is_error
    case.coordinator.snapshot.assert_not_awaited()
    assert "expectedSnapshot" not in case.coordinator.execute.await_args.args[-1]


@pytest.mark.parametrize("name,source", [
    ("browser_evaluate", {"function": "() => document.title"}),
    ("browser_run_code", {"code": "async def run(page):\n    return await page.title()"}),
])
@pytest.mark.parametrize("observe", [True, False])
@pytest.mark.parametrize("error_code", [None, "browser.actionFailed", "browser.outcomeUnknown"])
async def test_script_results_preserve_bounded_data_and_mark_partial_effect_errors(browser_case, name, source, observe, error_code):
    case = browser_case
    case.call.name = name
    case.call.arguments = {"tabId": str(uuid4()), "observe": observe, **source}
    case.coordinator.execute.side_effect = None
    case.coordinator.execute.return_value = BrowserActionResult(
        script={
            "value": {"changed": True}, "output": "printed output", "outputTruncated": True,
            "errorCode": error_code,
            **({"failureReason": "runtimeError", "failureDetail": "typeError", "line": 3} if error_code else {}),
        },
        **({"text": "- changed document", "data": {"snapshotSha256": "a" * 64}} if observe else {}),
    )
    result = await case.effective.execute(case.call, case.context)
    assert result.is_error == (error_code is not None)
    assert result.metadata == {"privateIntegrationData": True, **({"errorCode": error_code} if error_code else {})}
    assert json.loads(result.content)["script"] == {
        "value": {"changed": True}, "output": "printed output", "outputTruncated": True,
        **({"errorCode": error_code, "failureReason": "runtimeError", "failureDetail": "typeError", "line": 3} if error_code else {}),
    }
    requests = [call.args[0] for call in case.context.ask_handler.await_args_list]
    assert [request.capability for request in requests] == ["browser.use", *(["browser.code"] if name == "browser_run_code" else [])]
    key = next(iter(source))
    assert all(request.approval_details["browser"][key] == source[key] for request in requests)
    case.coordinator.snapshot.assert_not_awaited()
    original_scope = requests[-1].resource_scope
    case.call.arguments[key] += "\n"
    await case.effective.execute(case.call, case.context)
    assert case.context.ask_handler.await_args.args[0].resource_scope != original_scope


@pytest.mark.parametrize("name,arguments,script", [
    ("browser_run_code", {"code": "async def run(page):\n    return 1"}, None),
    ("browser_evaluate", {"function": "() => 1"}, None),
    ("browser_click", {"ref": "e1"}, {"value": "unexpected"}),
])
async def test_script_result_must_match_the_authorized_action(browser_case, name, arguments, script):
    case = browser_case
    case.call.name = name
    case.call.arguments = {"tabId": str(uuid4()), "observe": False, **arguments}
    case.coordinator.execute.side_effect = None
    case.coordinator.execute.return_value = BrowserActionResult(script=script)
    result = await case.effective.execute(case.call, case.context)
    assert result.metadata["errorCode"] == "executionAgent.protocolError"
    assert "unexpected" not in result.content


@pytest.mark.parametrize("code", get_args(BrowserErrorCode))
async def test_known_browser_errors_are_catalogued_and_remote_params_discarded(monkeypatch, code):
    assert describe_error(code).message != "The action failed."
    response = ResponseMessage(type="response", id=uuid4(), ok=False, error={"code": code, "params": {"secret": "token"}})
    monkeypatch.setattr("mato_loop_backend.execution.client.execution_agent_connections.request", AsyncMock(return_value=response))
    with pytest.raises(ExecutionClientError) as error:
        await ExecutionClient(uuid4(), frozenset({"integration.browser.execute"})).request("integration.browser.execute", {}, BrowserActionResult)
    assert error.value.code == code
    assert error.value.params is None


async def test_browser_mutation_disconnect_preserves_unknown_outcome(monkeypatch):
    for operation in ("execute", "setup", "setupwrite", "open", "close", "file"):
        operation = f"integration.browser.{operation}"
        assert operation in SUPPORTED_OPERATIONS
        assert disconnect_error_code(operation, True) == "browser.outcomeUnknown"
        assert disconnect_error_code(operation, False) == "executionAgent.unavailable"
    assert len(SUPPORTED_OPERATIONS) <= 64
    monkeypatch.setattr("mato_loop_backend.execution.client.execution_agent_connections.request", AsyncMock(side_effect=ExecutionAgentError("browser.outcomeUnknown")))
    with pytest.raises(ExecutionClientError, match="browser.outcomeUnknown"):
        await ExecutionClient(uuid4(), frozenset({"integration.browser.execute"})).request("integration.browser.execute", {}, BrowserActionResult)


@pytest.fixture
def browser_api(monkeypatch):
    user = SimpleNamespace(id=uuid4())
    agent = uuid4()
    other_agent = uuid4()
    documents = {(IntegrationSettings, None): IntegrationSettings()}
    db = AsyncMock()

    async def load(_db, settings_type, *, user_id=None, **_kwargs):
        return documents.get((settings_type, user_id), settings_type()).model_copy(deep=True)

    async def save(_db, settings, *, user_id=None):
        documents[type(settings), user_id] = settings.model_copy(deep=True)

    async def load_optional(_db, settings_type, *, user_id=None, **_kwargs):
        if settings_type is UserToolSettings:
            return documents.get(
                (settings_type, user_id), UserToolSettings()
            ).model_copy(deep=True)
        settings = documents.get((settings_type, user_id))
        return settings.model_copy(deep=True) if settings is not None else None

    async def scalar(statement):
        params = statement.compile().params
        return agent if params.get("id_1") == agent and params.get("user_id_1") == user.id else None

    monkeypatch.setattr(SettingsStore, "load", AsyncMock(side_effect=load))
    monkeypatch.setattr(
        SettingsStore, "load_optional", AsyncMock(side_effect=load_optional)
    )
    monkeypatch.setattr(SettingsStore, "save", AsyncMock(side_effect=save))
    db.scalar.side_effect = scalar
    db.scalars.return_value = SimpleNamespace(all=lambda: [agent])
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(api_routes_browser_integrations.router, prefix="/api")
    app.dependency_overrides[require_user] = lambda: user
    app.dependency_overrides[get_db_session] = lambda: db
    return SimpleNamespace(client=TestClient(app), user=user, agent=agent, other_agent=other_agent, db=db, documents=documents)


def test_browser_crud_owner_scoped_empty_config_one_per_agent_and_no_rebind(browser_api):
    case = browser_api
    base = "/api/integrations/browser"
    assert case.client.get(base).json() == {"configured": False, "instances": []}
    assert case.client.post(base, json={"executionAgentId": str(case.other_agent)}).status_code == 404
    assert case.client.post(base, json={"executionAgentId": str(case.agent), "config": {"profilePath": "/tmp"}}).status_code == 422
    created = case.client.post(base, json={"executionAgentId": str(case.agent)})
    assert created.status_code == 201
    item = created.json()["instances"][0]
    assert item["config"] == {} and item["adapterType"] == "browser"
    url = f"{base}/{item['id']}"
    assert case.client.post(base, json={"executionAgentId": str(case.agent)}).status_code == 409
    assert case.client.patch(url, json={"executionAgentId": str(case.other_agent)}).json()["error"]["code"] == "browser.bindingChanged"
    assert case.client.patch(url, json={"enabled": None}).status_code == 422
    assert case.client.patch(url, json={"name": "Privater Browser", "enabled": False}).json()["instances"][0]["enabled"] is False
    assert case.client.get(f"{base}?scope=instance").status_code == 422
    previous_user = case.user.id
    case.user.id = uuid4()
    assert case.client.get(base).json()["configured"] is False
    for method, suffix in (("patch", ""), ("delete", ""), ("get", "/status"), ("post", "/test"), ("post", "/reset")):
        response = case.client.request(method, url + suffix, json={} if method == "patch" else None)
        assert response.status_code == 404
    case.user.id = previous_user
    assert case.client.delete(url).json() == {"configured": False, "instances": []}


@pytest.mark.parametrize("availability,is_admin,status", [
    ("instance_only", True, 201), ("instance_only", False, 503), ("disabled", True, 503),
])
def test_browser_setup_matches_personal_admin_availability(browser_api, availability, is_admin, status):
    case = browser_api
    case.documents[IntegrationSettings, None] = IntegrationSettings(adapter_availability={"browser": availability})
    case.db.get.return_value = SimpleNamespace(is_admin=is_admin)
    response = case.client.post("/api/integrations/browser", json={"executionAgentId": str(case.agent)})
    assert response.status_code == status


def test_global_disable_blocks_use_but_allows_deletion(browser_api):
    case = browser_api
    base = "/api/integrations/browser"
    item = case.client.post(base, json={"executionAgentId": str(case.agent)}).json()["instances"][0]
    case.documents[IntegrationSettings, None] = IntegrationSettings(adapter_availability={"browser": "disabled"})
    assert case.client.post(base, json={"executionAgentId": str(case.agent)}).status_code == 503
    assert case.client.get(f"{base}/{item['id']}/status").status_code == 503
    assert case.client.delete(f"{base}/{item['id']}").status_code == 200


def test_status_uses_only_fixed_owner_bound_rpc_and_has_no_profile_reset(browser_api, monkeypatch):
    case = browser_api
    base = "/api/integrations/browser"
    item = case.client.post(base, json={"executionAgentId": str(case.agent)}).json()["instances"][0]
    url = f"{base}/{item['id']}"
    client = SimpleNamespace(request=AsyncMock(return_value=BrowserStatusResult(state="missing", missing=["chromium"])))
    factory = Mock(return_value=client)
    from mato_loop_backend.api.routes import browser_integrations

    monkeypatch.setattr(browser_integrations, "browser_client", factory)
    for method, suffix in (("get", "status"), ("post", "test")):
        response = case.client.request(method, f"{url}/{suffix}")
        assert response.status_code == 200 and response.json()["state"] == "missing"
        client.request.assert_awaited_with("integration.browser.status", {}, BrowserStatusResult)
    client.request.side_effect = ExecutionClientError("browser.busy", {"secret": "token"})
    response = case.client.post(f"{url}/reset")
    assert response.status_code == 404
    assert "token" not in response.text
    factory.assert_called_with(case.agent)


async def test_global_personal_only_browser_never_becomes_effective(monkeypatch):
    instance = _instance()
    monkeypatch.setattr(SettingsStore, "load", AsyncMock(return_value=IntegrationSettings(instances=[instance])))
    monkeypatch.setattr(SettingsStore, "load_optional", AsyncMock(return_value=None))
    effective = await load_effective_integrations(AsyncMock(), instance.owner_id)
    assert effective.instances == []


def test_browser_client_is_generation_bound(monkeypatch):
    monkeypatch.setattr(browser_module.execution_agent_connections, "snapshot", Mock(return_value=frozenset({"integration.browser.status"})))
    monkeypatch.setattr(browser_module.execution_agent_connections, "generation", Mock(return_value=7))
    assert browser_module.browser_client(uuid4()).expected_generation == 7


async def test_current_tool_context_cannot_bypass_profile(monkeypatch):
    instance = _instance()
    session = ChatSession(id=uuid4(), user_id=instance.owner_id, agent_profile_id=uuid4())
    profile = AgentProfile(id=session.agent_profile_id, name="Browser excluded", instructions="Test profile", integrations_enabled_by_default=False)
    monkeypatch.setattr(mock_tools_context, 'load_effective_integrations', AsyncMock(return_value=IntegrationSettings(instances=[instance])))
    monkeypatch.setattr(mock_tools_context, 'load_agent_profile', AsyncMock(return_value=profile))
    coordinator = SimpleNamespace(current_for=Mock())
    monkeypatch.setitem(sys.modules, "mato_loop_backend.execution.browsers", SimpleNamespace(browser_coordinator=coordinator))
    context = ToolContext(
        db=AsyncMock(), run_id=uuid4(), session=session, tool_call=ToolCall("test", "browser_diagnostics", {}),
        root_files=Mock(), integration_settings=IntegrationSettings(instances=[instance]),
        ask_handler=AsyncMock(), execution_client=ExecutionClient(instance.execution_agent_id, frozenset({"integration.browser.execute"})),
    )
    assert not (await context.current_integrations()).instances
    coordinator.current_for.assert_not_called()


async def test_profile_disabled_browser_actions_are_not_offered():
    instance = _instance()
    settings = effective_execution_integration_settings(
        IntegrationSettings(instances=[instance]), instance.execution_agent_id,
        {"integration.browser.execute"},
    )
    tools = ToolSettings()
    for permissions in (tools.private_permissions, tools.public_permissions):
        permissions.policies["browser.use"] = "disabled"
    effective = mock_tools_catalog.tool_registry.effective(settings, tool_settings=tools)
    assert not set(BROWSER_TOOL_ACTIONS) & effective.tools.keys()
    assert {action.capability for action in mock_tools_catalog.tool_registry.integration_resource_actions(instance)} == {
        "browser.use", "browser.code",
        "filesystem.read", "session.files.read",
    }


@pytest.mark.parametrize("personal", [None, UserToolSettings()])
async def test_stored_browser_family_disable_is_projected_into_effective_settings(monkeypatch, personal):
    from mato_loop_backend.settings.effective import load_effective_tools

    stored = ToolSettings(tool_availability={"browser": False, "browser_run_code": True})
    monkeypatch.setattr(SettingsStore, "load", AsyncMock(return_value=stored))
    monkeypatch.setattr(SettingsStore, "load_optional", AsyncMock(side_effect=lambda _db, model, **_kwargs: personal if model is UserToolSettings else None))
    effective = await load_effective_tools(AsyncMock(), uuid4())
    assert all(effective.tool_availability[name] is False for name in BROWSER_TOOL_ACTIONS)
    assert stored.tool_availability == {"browser": False, "browser_run_code": True}


def test_no_browser_tools_are_offered_with_browser_use_disabled():
    instance = _instance()
    settings = ToolSettings()
    settings.permissions.policies["browser.use"] = "disabled"
    effective = mock_tools_catalog.ToolRegistry([]).effective(IntegrationSettings(instances=[instance]), tool_settings=settings)
    assert not effective.tools
