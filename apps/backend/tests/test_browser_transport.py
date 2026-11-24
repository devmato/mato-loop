import asyncio
import base64
import errno
import hashlib
import json
from datetime import UTC, datetime, timedelta
from io import BytesIO
from tempfile import TemporaryFile
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest
from browser_helpers import ORIGIN, file_url, open_view, other_session, view_url
from browser_helpers import env as env
from fastapi import WebSocketDisconnect
from PIL import Image

import mato_loop_backend.tools.access as mock_tools_access
import mato_loop_backend.tools.catalog as mock_tools_catalog
from mato_loop_backend.agent.runtime import AgentRuntime
from mato_loop_backend.api.routes import browsers as routes
from mato_loop_backend.api.websocket_access import _BrowserLogin
from mato_loop_backend.execution import browsers as execution
from mato_loop_backend.execution.client import ExecutionClientError
from mato_loop_backend.execution.manager import ExecutionAgentConnectionManager
from mato_loop_backend.execution.protocol import SUPPORTED_OPERATIONS, HelloMessage, ResponseMessage
from mato_loop_backend.models import (
    AgentRun,
    ExecutionAgent,
    LoginSession,
    PermissionGrant,
)
from mato_loop_backend.permissions.policy import (
    PermissionDecision,
    PermissionPolicy,
    integration_resource_scope,
)
from mato_loop_backend.providers.base import ToolCall
from mato_loop_backend.settings import (
    IntegrationSettings,
    ToolSettings,
    UserIntegrationSettings,
)
from mato_loop_backend.settings.models import IntegrationPermissions
from mato_loop_backend.tools.context import ToolContext
from mato_loop_backend.tools.registry import _model_revision


@pytest.fixture
def session_upload(monkeypatch):
    with TemporaryFile() as handle:
        handle.write(b"x")
        handle.seek(0)
        monkeypatch.setattr(routes, "RootFileService", lambda *_args, **_kwargs: SimpleNamespace(
            open_file=lambda *_args: handle,
        ))
        yield handle


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_new_browser_uses_frontend_color_scheme(env, color_scheme):
    with env.client.websocket_connect(
        env.url + f"&colorScheme={color_scheme}", headers=ORIGIN
    ) as control:
        assert control.receive_json()["type"] == "state"
        assert env.calls[0][1]["colorScheme"] == color_scheme


def test_different_chats_have_independent_browser_bindings(env):
    session_b = other_session(env)
    with env.client.websocket_connect(env.url, headers=ORIGIN) as a:
        state_a = a.receive_json()
        with env.client.websocket_connect(env.url.replace(str(env.session.id), str(session_b.id)), headers=ORIGIN) as b:
            state_b = b.receive_json()
            assert state_b["type"] == "state" and state_b["browserId"] != state_a["browserId"]
            assert len(env.coordinator.connections) == 2
            assert env.client.post(file_url(env, state_b, "clipboard/copy")).status_code >= 400


@pytest.fixture
def wire(env, monkeypatch):
    """Exercise real request/cancel frames while keeping agent replies and storage fake."""
    manager = ExecutionAgentConnectionManager()
    request = execution.execution_agent_connections.request
    sent, responders = [], []

    async def send_text(raw):
        message = json.loads(raw)
        sent.append(message)
        if message["type"] == "request":
            async def respond():
                response = await request(env.agent.id, message["operation"], message["arguments"], expected_generation=env.generation)
                response.id = UUID(message["id"])
                await manager.receive(env.agent.id, socket, response.model_dump_json())

            responders.append(asyncio.create_task(respond()))
        # A real WebSocket send can yield, including when sending cancellation frames.
        await asyncio.sleep(0.01 if message["type"] == "cancel" else 0)

    socket = SimpleNamespace(send_text=send_text)
    hello = HelloMessage(type="hello", protocol_version=1, agent_id=env.agent.id, agent_version="test",
                         computer_name="test", platform="linux", operations=list(SUPPORTED_OPERATIONS),
                         home_directory="/home/user")
    env.client.portal.call(manager.register, hello, socket)
    monkeypatch.setattr(execution.execution_agent_connections, "request", manager.request)
    yield sent

    async def cleanup():
        await env.coordinator.shutdown()
        await manager.unregister(env.agent.id, socket)
        for task in responders:
            task.cancel()
        await asyncio.gather(*responders, return_exceptions=True)

    env.client.portal.call(cleanup)


@pytest.mark.parametrize("invalid", [
    "origin", "missing-origin", "login", "expired", "login-owner", "inactive", "missing-session",
    "session-owner", "child", "automation", "agent-owner", "revoked", "binding",
    "integration", "disabled", "global-disabled", "browser-id",
])
def test_websocket_access_denied_before_any_rpc(env, monkeypatch, invalid):
    headers, url = ORIGIN, env.url
    if invalid == "origin":
        headers = {"origin": "https://evil.test"}
    elif invalid == "missing-origin":
        headers = {}
    elif invalid == "login":
        monkeypatch.setattr(routes, "_browser_login", AsyncMock(return_value=None))
    elif invalid == "expired":
        env.login.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    elif invalid == "login-owner":
        env.login.user_id = uuid4()
    elif invalid == "inactive":
        env.user.is_active = False
    elif invalid == "missing-session":
        url = url.split("?")[0]
    elif invalid == "browser-id":
        url += "&expectedBrowserId=invalid"
    elif invalid == "session-owner":
        env.session.user_id = uuid4()
    elif invalid == "child":
        env.session.parent_session_id = uuid4()
    elif invalid == "automation":
        env.session.session_type = "automation"
    elif invalid == "agent-owner":
        env.agent.user_id = uuid4()
    elif invalid == "revoked":
        env.agent.revoked_at = datetime.now(UTC)
    elif invalid == "binding":
        env.session.preferred_execution_agent_id = uuid4()
    elif invalid == "tab":
        env.session.control_center = {"tabs": []}
    elif invalid == "tab-kind":
        env.session.control_center = {"tabs": [{"app_id": "browser", "kind": "file"}]}
    elif invalid == "integration":
        env.documents[UserIntegrationSettings, env.user.id].instances.clear()
    elif invalid == "disabled":
        env.documents[UserIntegrationSettings, env.user.id].instances[0].enabled = False
    else:
        env.documents[IntegrationSettings, None].adapter_availability["browser"] = "disabled"
    with pytest.raises(WebSocketDisconnect) as error, env.client.websocket_connect(url, headers=headers):
        pytest.fail("Unauthorized WebSocket was accepted")
    if invalid in {"origin", "missing-origin", "login"}:
        assert error.value.code == (4401 if invalid == "login" else 4403)
    assert env.calls == []


@pytest.mark.parametrize("unavailable", ["offline", "capability"])
def test_unavailable_agent_never_receives_open(env, unavailable):
    env.operations = None if unavailable == "offline" else frozenset()
    with env.client.websocket_connect(env.url, headers=ORIGIN) as socket:
        event = socket.receive()
        assert event["type"] == "websocket.close" or json.loads(event["text"])["type"] == "error"
    assert env.calls == []
    assert not env.coordinator.connections


def test_views_share_one_runtime_without_takeover_and_survive_disconnect(env):
    with env.client.websocket_connect(env.url, headers=ORIGIN) as control:
        state = control.receive_json()
        with env.client.websocket_connect(env.url, headers=ORIGIN) as duplicate:
            second = duplicate.receive_json()
            assert second["browserId"] == state["browserId"] and second["viewId"] != state["viewId"]
            with open_view(env, state), open_view(env, second):
                assert len(env.coordinator.connections[(env.session.id, env.agent.id)].view_sockets) == 2
        with env.client.websocket_connect(view_url(env, state), headers=ORIGIN) as viewer:
            connection = env.coordinator.connections[(env.session.id, env.agent.id)]
            assert env.coordinator.current_for(env.session.id, env.agent.id) is connection
            viewer.close()
            assert viewer.receive()["type"] == "websocket.close"
    assert env.coordinator.current_for(env.session.id, env.agent.id) is connection
    assert not connection.sockets and not connection.view_sockets
    with env.client.websocket_connect(env.url + f"&expectedBrowserId={state['browserId']}", headers=ORIGIN) as resumed:
        restored = resumed.receive_json()
        assert restored["browserId"] == state["browserId"]
        assert restored["viewId"] != state["viewId"]
        assert env.coordinator.connections[(env.session.id, env.agent.id)] is connection
        assert not connection.closed
        with open_view(env, restored):
            assert env.coordinator.current_for(env.session.id, env.agent.id) is connection
    assert sum(op == "integration.browser.open" for op, _ in env.calls) == 1
    assert not any(op == "integration.browser.setup" for op, _ in env.calls)


@pytest.mark.parametrize("target", ["closed", "different"])
def test_resume_never_opens_a_replacement_browser(env, target):
    with env.client.websocket_connect(env.url, headers=ORIGIN) as control:
        state = control.receive_json()
    if target == "closed":
        connection = env.coordinator.current_for(env.session.id, env.agent.id)
        env.client.portal.call(env.coordinator.close, connection)
    expected = state["browserId"] if target == "closed" else str(uuid4())
    with env.client.websocket_connect(env.url + f"&expectedBrowserId={expected}", headers=ORIGIN) as resumed:
        assert resumed.receive_json()["error"]["code"] == "browser.notConnected"
    assert sum(op == "integration.browser.open" for op, _ in env.calls) == 1


def test_resume_recovers_saved_tabs_after_runtime_loss(env):
    with env.client.websocket_connect(env.url, headers=ORIGIN) as control:
        state = control.receive_json()
    old = env.coordinator.connections[(env.session.id, env.agent.id)]
    env.client.portal.call(lambda: env.coordinator.close(old, reason="watchFailed"))
    env.operations |= {"integration.browser.openview"}

    async def restore(operation, arguments):
        if operation == "integration.browser.openview":
            assert arguments["recoverOnly"] is True
            assert arguments["deferRestore"] is True
            return {"browserId": arguments["browserId"], "protocol": "chat-browser-v1", "restorePending": True}
        if operation == "integration.browser.control" and arguments.get("restoreTabs"):
            return {"ok": True, "restoredTabs": 2}

    env.reply = restore
    for _ in range(2):
        with env.client.websocket_connect(env.url + f"&expectedBrowserId={state['browserId']}", headers=ORIGIN) as resumed:
            recovered = resumed.receive_json()
            assert recovered["browserId"] != state["browserId"] and recovered["restoredTabs"] == 2
    assert sum(op == "integration.browser.openview" for op, _ in env.calls) == 1
    assert sum(bool(args.get("restoreTabs")) for _, args in env.calls) == 1


@pytest.mark.parametrize("decision", [PermissionDecision.DENY, PermissionDecision.ALLOW])
def test_tool_start_defers_restoration_until_authorized(env, monkeypatch, decision):
    integrations = IntegrationSettings(instances=[env.instance])
    monkeypatch.setattr(execution, "browser_coordinator", env.coordinator)
    monkeypatch.setattr(ToolContext, "current_integrations", AsyncMock(return_value=integrations))
    monkeypatch.setattr(mock_tools_access, 'profile_tool_settings', AsyncMock(return_value=ToolSettings()))
    env.operations |= {"integration.browser.openview"}
    events = []

    async def reply(operation, arguments):
        if operation == "integration.browser.openview":
            assert arguments["deferRestore"] is True
            return {"browserId": arguments["browserId"], "protocol": "chat-browser-v1", "restorePending": True}
        if operation == "integration.browser.control" and arguments.get("restoreTabs"):
            events.append("restore")
            return {"ok": True, "restoredTabs": 2}

    async def authorize(*_args, **_kwargs):
        events.append("permission")
        return decision

    env.reply = reply
    call = ToolCall("browser-recover", "browser_tabs", {"action": "list"})
    context = ToolContext(
        db=AsyncMock(), run_id=uuid4(), session=env.session, tool_call=call,
        root_files=Mock(), integration_settings=integrations,
        execution_client=execution.browser_client(env.agent.id), ask_handler=authorize,
    )
    context.db.get.return_value = SimpleNamespace(
        execution_agent_id=env.agent.id, execution_operations=["integration.browser.execute"],
    )
    env.objects[AgentRun, context.run_id] = AgentRun(id=context.run_id, session_id=env.session.id, status="completed")
    registry = mock_tools_catalog.ToolRegistry([]).effective(integrations)
    result = env.client.portal.call(registry.execute, call, context)
    assert result.is_error == (decision == PermissionDecision.DENY), result.content
    assert events == (["permission", "restore"] if decision == PermissionDecision.ALLOW else ["permission"])


@pytest.mark.parametrize("gate", ["default", "explicit", "missing-profile"])
def test_profile_gate_allows_manual_view_but_not_model_actions(env, monkeypatch, gate):
    if gate == "default":
        env.profile.integrations_enabled_by_default = False
    elif gate == "explicit":
        env.instance.profile_permissions[env.profile.id] = IntegrationPermissions(available=False)
    else:
        monkeypatch.setattr(execution, "load_agent_profile", AsyncMock(side_effect=execution.AgentProfileUnavailable))
    with env.client.websocket_connect(env.url, headers=ORIGIN) as control:
        state = control.receive_json()
        with open_view(env, state):
            connection = env.coordinator.current_for(env.session.id, env.agent.id)
            assert not connection.model_available
            with pytest.raises(ExecutionClientError):
                env.client.portal.call(env.coordinator.execute, env.session.id, env.agent.id, env.instance.id,
                                       {"action": "tabs", "expectedBrowserId": state["browserId"]})


@pytest.mark.parametrize("change", ["binding", "disabled", "global-disabled", "generation"])
def test_live_access_revocation_closes_browser(env, monkeypatch, change):
    watch = env.coordinator.watch
    watch_gate = asyncio.Event()

    async def gated_watch(connection):
        await watch_gate.wait()
        await watch(connection)

    monkeypatch.setattr(env.coordinator, "watch", gated_watch)
    monkeypatch.setattr(execution, "ACCESS_INTERVAL", 0.01)
    with env.client.websocket_connect(env.url, headers=ORIGIN) as control:
        state = control.receive_json()
        with open_view(env, state):
            if change == "login":
                env.objects.pop((LoginSession, env.login.id))
            elif change == "expired":
                env.login.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            elif change == "tab":
                env.session.control_center = {"tabs": []}
            elif change == "binding":
                env.session.preferred_execution_agent_id = uuid4()
            elif change == "disabled":
                env.documents[UserIntegrationSettings, env.user.id].instances[0].enabled = False
            elif change == "global-disabled":
                env.documents[IntegrationSettings, None].adapter_availability["browser"] = "disabled"
            elif change == "generation":
                env.generation = 2
            else:
                env.instance.profile_permissions[env.profile.id] = IntegrationPermissions(available=False)
            env.client.portal.call(watch_gate.set)
            event = control.receive_json()
            assert event["type"] == "error"


def test_view_disconnect_during_action_does_not_cancel_rpc(env):
    started, finish = asyncio.Event(), asyncio.Event()
    cancelled = []

    async def reply(operation, arguments):
        if operation == "integration.browser.execute" and arguments["action"] == "navigate":
            started.set()
            try:
                await finish.wait()
            except asyncio.CancelledError:
                cancelled.append(operation)
                raise
            return {}

    env.reply = reply
    with env.client.websocket_connect(env.url, headers=ORIGIN) as control:
        state = control.receive_json()
        with open_view(env, state):
            connection = env.coordinator.connections[(env.session.id, env.agent.id)]
            args = {"action": "navigate", "tabId": str(uuid4()), "url": "https://page.test/",
                    "expectedBrowserId": state["browserId"]}
            task = env.client.portal.start_task_soon(env.coordinator.execute, env.session.id, env.agent.id, env.instance.id, args)
            try:
                env.client.portal.call(asyncio.wait_for, started.wait(), 1)
                assert control.receive_json()["busy"]
                control.send_json({"type": "release"})
                assert control.receive()["type"] == "websocket.close"
                assert not task.done() and not cancelled
            finally:
                env.client.portal.call(finish.set)
            task.result(timeout=2)
            assert not connection.busy and env.manual


def test_session_cancel_interrupts_an_active_browser_command(env, wire, monkeypatch):
    started = asyncio.Event()
    run_id = uuid4()
    monkeypatch.setattr(AgentRuntime, "_tasks", {})
    monkeypatch.setattr(AgentRuntime, "_cancellable_runs", {run_id})
    monkeypatch.setattr(AgentRuntime, "_user_cancelled_runs", set())

    async def reply(operation, arguments):
        if operation == "integration.browser.execute":
            started.set()
            await asyncio.Event().wait()

    env.reply = reply
    with env.client.websocket_connect(env.url, headers=ORIGIN) as control:
        state = control.receive_json()

        async def cancel():
            task = asyncio.create_task(env.coordinator.execute(
                env.session.id, env.agent.id, env.instance.id,
                {"action": "navigate", "tabId": str(uuid4()), "url": "https://page.test/", "expectedBrowserId": state["browserId"]},
            ))
            AgentRuntime._tasks[run_id] = task
            try:
                await asyncio.wait_for(started.wait(), 1)
                AgentRuntime.cancel(run_id)
            finally:
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 2)

        env.client.portal.call(cancel)
        request = next(message for message in wire if message.get("operation") == "integration.browser.execute")
        assert [message["id"] for message in wire if message["type"] == "cancel"] == [request["id"]]


@pytest.mark.parametrize("action", ["click", "inspect"])
def test_access_revoked_during_snapshot_prevents_approved_action_dispatch(env, action):
    started, finish = asyncio.Event(), asyncio.Event()
    with env.client.websocket_connect(env.url, headers=ORIGIN) as control:
        state = control.receive_json()
        with open_view(env, state):
            connection = env.coordinator.connections[(env.session.id, env.agent.id)]
            tab_id = str(uuid4())
            snapshot = env.client.portal.call(env.coordinator.snapshot, env.session.id, env.agent.id, env.instance.id, tab_id)
            control.receive_json()
            control.receive_json()

            async def reply(operation, arguments):
                if operation == "integration.browser.execute" and arguments["action"] == "inspect":
                    started.set()
                    await finish.wait()

            env.reply = reply
            args = {"action": action, "tabId": tab_id, **({"selector": "button"} if action == "click" else {}), "expectedSnapshot": snapshot,
                    "expectedBrowserId": state["browserId"]}
            task = env.client.portal.start_task_soon(env.coordinator.execute, env.session.id, env.agent.id, env.instance.id, args)
            try:
                env.client.portal.call(asyncio.wait_for, started.wait(), 1)
                assert control.receive_json()["busy"]
                connection.model_available = False
            finally:
                env.client.portal.call(finish.set)
            with pytest.raises(ExecutionClientError, match="browser.bindingChanged"):
                task.result(timeout=2)
            detached = control.receive_json()
            assert detached["type"] == "state" and not detached["busy"]
            assert detached["browserId"] == state["browserId"] and env.manual
            assert not any(arguments.get("action") == "click" for _, arguments in env.calls)


@pytest.mark.parametrize("action", ["click", "inspect", "upload"])
@pytest.mark.parametrize("change", ["digest", "url", "tab", "missing", "binding"])
def test_stale_snapshot_after_approval_never_dispatches_action(env, change, action, caplog):
    caplog.set_level("INFO", logger=execution.__name__)
    with env.client.websocket_connect(env.url, headers=ORIGIN) as control:
        state = control.receive_json()
        with open_view(env, state):
            connection = env.coordinator.connections[(env.session.id, env.agent.id)]
            tab_id = str(uuid4())
            snapshot = env.client.portal.call(env.coordinator.snapshot, env.session.id, env.agent.id, env.instance.id, tab_id)
            control.receive_json()
            control.receive_json()
            action_args = ({"selector": "button"} if action == "click" else
                           {"selector": "input", "uploadId": str(uuid4())} if action == "upload" else {})
            args = {"action": action, "tabId": tab_id, **action_args, "expectedSnapshot": snapshot,
                    "expectedBrowserId": state["browserId"]}

            async def reply(operation, arguments):
                if operation == "integration.browser.execute" and arguments["action"] == "inspect":
                    return {"tabId": str(uuid4()) if change == "tab" else tab_id,
                            "url": "https://changed.test/" if change == "url" else "https://page.test/",
                            "data": {} if change == "missing" else {"snapshotSha256": ("b" if change == "digest" else "a") * 64}}

            env.reply = reply
            if change == "binding":
                args["expectedBrowserId"] = str(uuid4())
            code = "browser.snapshotChanged" if change in {"digest", "url"} else "browser.bindingChanged"
            with pytest.raises(ExecutionClientError, match=code):
                env.client.portal.call(env.coordinator.execute, env.session.id, env.agent.id, env.instance.id, args)
            assert not connection.busy
            if action != "inspect":
                assert not any(args.get("action") == action for _, args in env.calls)
            record = [item for item in caplog.records if item.name == execution.__name__][-1]
            assert record.args[0] == "execute" and record.args[2] == "error"
            timings = record.args[-1]
            assert "total_ms" in timings and "action_rpc_ms" not in timings
            assert ("reinspection_rpc_ms" in timings) == (change != "binding")


@pytest.mark.parametrize("action", ["tabs", "inspect", "screenshot", "diagnostics"])
def test_browser_auto_read_from_registry_through_coordinator(env, monkeypatch, action):
    integrations = IntegrationSettings(instances=[env.instance])
    monkeypatch.setattr(execution, "browser_coordinator", env.coordinator)
    monkeypatch.setattr(ToolContext, "current_integrations", AsyncMock(return_value=integrations))
    monkeypatch.setattr(mock_tools_access, 'profile_tool_settings', AsyncMock(return_value=ToolSettings()))
    output = BytesIO()
    Image.new("RGB", (2, 2), "white").save(output, format="PNG")
    image = output.getvalue()

    async def reply(operation, arguments):
        if operation == "integration.browser.execute":
            assert "observe" not in arguments
            assert "snapshotSha256" not in arguments
            if arguments["action"] == "inspect":
                return {"tabId": arguments["tabId"], "text": "- document",
                        "data": {"snapshotSha256": "a" * 64}}
            if arguments["action"] == "screenshot":
                return {"screenshot": {"fileId": env.file_id, "size": len(image)}}
            return {}
        if operation == "integration.browser.file" and arguments["action"] == "read":
            return {"fileId": env.file_id, "size": len(image), "offset": len(image),
                    "complete": True, "data": base64.b64encode(image).decode()}

    env.reply = reply
    with env.client.websocket_connect(env.url, headers=ORIGIN) as control:
        state = control.receive_json()
        with open_view(env, state):
            connection = env.coordinator.connections[(env.session.id, env.agent.id)]
            name = {"tabs": "browser_tabs", "inspect": "browser_snapshot", "screenshot": "browser_take_screenshot", "diagnostics": "browser_diagnostics"}[action]
            arguments = {"tabId": env.navigation["activeTabId"]} if action in {"inspect", "screenshot"} else {"action": "list"} if action == "tabs" else {}
            call = ToolCall("browser-read", name, arguments)
            context = ToolContext(
                db=AsyncMock(), run_id=uuid4(), session=env.session, tool_call=call,
                root_files=Mock(), integration_settings=integrations, execution_client=connection.client,
                ask_handler=AsyncMock(return_value=PermissionDecision.ALLOW),
            )
            context.db.get.return_value = SimpleNamespace(
                execution_agent_id=env.agent.id, execution_operations=["integration.browser.execute"],
            )
            env.objects[AgentRun, context.run_id] = AgentRun(id=context.run_id, session_id=env.session.id, status="completed")
            registry = mock_tools_catalog.ToolRegistry([]).effective(integrations)
            result = env.client.portal.call(registry.execute, call, context)
            assert not result.is_error, result.content
            context.ask_handler.assert_awaited_once()
            assert context.ask_handler.await_args.args[0].capability == "browser.use"
            calls = [args["action"] for op, args in env.calls if op == "integration.browser.execute"]
            assert calls == [action]
            inspections = [args for op, args in env.calls if op == "integration.browser.execute" and args["action"] == "inspect"]
            assert [args["settle"] for args in inspections] == ([True] if inspections else [])
            assert all(args["guard"] is False for args in inspections)
            if action == "screenshot":
                assert result.attachments[0].ephemeral and result.attachments[0].data == image


@pytest.mark.parametrize("root", ["files", "computer"])
@pytest.mark.parametrize("deny_after_transfer", [False, True])
def test_structured_browser_upload_stages_revalidates_executes_and_cleans(
    env, monkeypatch, root, deny_after_transfer,
):
    integrations = IntegrationSettings(instances=[env.instance])
    monkeypatch.setattr(execution, "browser_coordinator", env.coordinator)
    monkeypatch.setattr(ToolContext, "current_integrations", AsyncMock(return_value=integrations))
    monkeypatch.setattr(mock_tools_access, 'profile_tool_settings', AsyncMock(return_value=ToolSettings()))
    root_files = Mock()
    root_files.normalize_relative_path.side_effect = lambda path: path
    root_files.root_identity.return_value = "session-files"

    def open_file(*_args):
        handle = TemporaryFile()
        handle.write(b"file data")
        handle.seek(0)
        return handle

    root_files.open_file.side_effect = open_file

    async def reply(operation, arguments):
        if operation == "integration.browser.execute":
            assert arguments["action"] == "upload"
            assert arguments["selector"] == "aria-ref=e7"
            assert arguments["tabId"] == env.navigation["activeTabId"]
            return {}

    env.reply = reply
    with env.client.websocket_connect(env.url, headers=ORIGIN) as control:
        state = control.receive_json()
        with open_view(env, state):
            connection = env.coordinator.connections[(env.session.id, env.agent.id)]
            connection.client.current_directory = "/home/user"
            call = ToolCall("browser-upload", "browser_upload", {
                "root": root, "path": "sample.txt", "tabId": env.navigation["activeTabId"],
                "ref": "e7", "observe": False,
            })
            decisions = [PermissionDecision.ALLOW] * 2
            decisions += (
                [PermissionDecision.DENY]
                if deny_after_transfer
                else [PermissionDecision.ALLOW] * 2
            )
            context = ToolContext(
                db=AsyncMock(), run_id=uuid4(), session=env.session, tool_call=call,
                root_files=root_files, integration_settings=integrations,
                execution_client=connection.client,
                ask_handler=AsyncMock(side_effect=decisions),
            )
            context.db.get.return_value = SimpleNamespace(
                execution_agent_id=env.agent.id,
                execution_operations=["integration.browser.execute"],
            )
            env.objects[AgentRun, context.run_id] = AgentRun(id=context.run_id, session_id=env.session.id, status="completed")
            result = env.client.portal.call(
                mock_tools_catalog.ToolRegistry([]).effective(integrations).execute, call, context,
            )
            assert result.is_error is deny_after_transfer
            requests = [item.args[0] for item in context.ask_handler.await_args_list]
            expected = ["session.files.read" if root == "files" else "filesystem.read", "browser.use"]
            assert [request.capability for request in requests] == (
                expected + expected[:1] if deny_after_transfer else expected * 2
            )
            assert ("integration.browser.upload" in [op for op, _args in env.calls]) is (root == "computer")
            actions = [args["action"] for op, args in env.calls if op == "integration.browser.file"]
            assert actions == (["begin", "write", "finish", "delete"] if root == "files" else ["delete"])
            executes = [args for op, args in env.calls if op == "integration.browser.execute"]
            assert len(executes) == (0 if deny_after_transfer else 1)


@pytest.fixture
def browser_tool(env, monkeypatch, wire):
    integrations = IntegrationSettings(instances=[env.instance])
    monkeypatch.setattr(execution, "browser_coordinator", env.coordinator)
    monkeypatch.setattr(ToolContext, "current_integrations", AsyncMock(return_value=integrations))
    monkeypatch.setattr(mock_tools_access, 'profile_tool_settings', AsyncMock(return_value=ToolSettings()))

    async def reply(operation, arguments):
        if operation != "integration.browser.execute":
            return None
        if arguments["action"] == "inspect":
            return {"tabId": arguments["tabId"], "url": "https://page.test/", "text": "- document",
                    "data": {"snapshotSha256": "a" * 64}}
        assert arguments["action"] == "fill_form"
        return {"batch": {"completed": len(arguments["fields"]), "total": len(arguments["fields"])},
                **({"url": "https://page.test/", "text": "- document after fill",
                    "data": {"snapshotSha256": "b" * 64}} if arguments["observe"] else {})}

    env.reply = reply
    with env.client.websocket_connect(env.url, headers=ORIGIN) as control:
        state = control.receive_json()
        with open_view(env, state):
            connection = env.coordinator.connections[(env.session.id, env.agent.id)]
            fields = [
                {"name": "Name", "ref": "e1", "type": "textbox", "value": "Ada"},
                {"name": "Active", "ref": "e2", "type": "checkbox", "value": "false"},
                {"name": "Choice", "ref": "f1e3", "type": "radio", "value": "true"},
                {"name": "Country", "ref": "e4", "type": "combobox", "value": "de"},
            ]
            call = ToolCall("browser-form", "browser_fill_form", {
                "tabId": env.navigation["activeTabId"], "fields": fields,
            })
            context = ToolContext(
                db=AsyncMock(), run_id=uuid4(), session=env.session, tool_call=call,
                root_files=Mock(), integration_settings=integrations, execution_client=connection.client,
                ask_handler=AsyncMock(return_value=PermissionDecision.ALLOW),
            )
            context.db.get.return_value = SimpleNamespace(
                execution_agent_id=env.agent.id, execution_operations=["integration.browser.execute"],
            )
            env.objects[AgentRun, context.run_id] = AgentRun(id=context.run_id, session_id=env.session.id, status="completed")
            yield SimpleNamespace(
                env=env, call=call, context=context, connection=connection, frames=wire,
                registry=mock_tools_catalog.ToolRegistry([]).effective(integrations),
            )


@pytest.mark.parametrize("observe,observation_failed", [(False, False), (True, False), (True, True)])
@pytest.mark.parametrize("error_code", [None, "browser.snapshotChanged", "browser.outcomeUnknown"])
def test_fill_form_permission_wire_and_partial_prefix(browser_tool, observe, error_code, observation_failed):
    case = browser_tool
    case.call.arguments["observe"] = observe
    reply = case.env.reply
    lines = [
        "await page.locator('aria-ref=e1').fill('Ada', timeout=1000)",
        "await page.locator('aria-ref=e2').set_checked(False, timeout=1000)",
        "await page.locator('aria-ref=f1e3').set_checked(True, timeout=1000)",
        "await page.locator('aria-ref=e4').select_option(label='de', timeout=1000)",
    ]
    feedback = {
        "completed": "\n".join(lines[:1] if error_code else lines), "truncated": False,
        **({"errorCode": error_code} if error_code else {}),
        **({"attempted": lines[1]} if error_code == "browser.outcomeUnknown" else {}),
    }

    async def result_reply(operation, arguments):
        result = await reply(operation, arguments)
        if operation == "integration.browser.execute" and arguments["action"] == "fill_form":
            result["playwrightCode"] = feedback
            if error_code:
                result["batch"].update(completed=1, errorCode=error_code)
            if observe and observation_failed:
                result.pop("text")
                result["data"] = {"observationFailed": True}
            elif observe:
                result["data"]["settled"] = False
        return result

    case.env.reply = result_reply
    result = case.env.client.portal.call(case.registry.execute, case.call, case.context)
    assert result.is_error == (error_code is not None), result.content
    payload = json.loads(result.content)
    assert payload["playwrightCode"] == feedback
    assert payload["batch"] == {
        "completed": 1 if error_code else 4, "total": 4,
        **({"errorCode": error_code} if error_code else {}),
    }
    assert result.metadata == {"privateIntegrationData": True, **({"errorCode": error_code} if error_code else {})}
    if observe:
        assert payload["data"] == ({"observationFailed": True} if observation_failed else {
            "snapshotSha256": hashlib.sha256(f"https://page.test/\n{'b' * 64}".encode()).hexdigest(),
            "settled": False,
        })
    else:
        assert "data" not in payload and "text" not in payload
    requests = [call.args[0] for call in case.context.ask_handler.await_args_list]
    assert [request.capability for request in requests] == ["browser.use"]
    for request in requests:
        arguments = case.registry.tools[case.call.name].definition.validate_arguments(case.call.arguments).to_action().model_dump(mode="json")
        operation = {
            **arguments, "browserId": str(case.connection.browser_id),
            "capability": request.capability,
        }
        key = json.loads(request.resource_scope)["operationKey"]
        assert key == hashlib.sha256(json.dumps(operation, sort_keys=True).encode()).hexdigest()
        assert request.approval_details["browser"]["fields"] == case.call.arguments["fields"]
        for fields in (list(reversed(operation["fields"])), [{**operation["fields"][0], "value": "Changed"}, *operation["fields"][1:]]):
            assert key != hashlib.sha256(json.dumps({**operation, "fields": fields}, sort_keys=True).encode()).hexdigest()
    calls = [frame["arguments"] for frame in case.frames if frame.get("operation") == "integration.browser.execute"]
    assert [call["action"] for call in calls] == ["fill_form"]
    assert [call["settle"] for call in calls] == [False]
    assert all("snapshotSha256" not in call for call in calls)
    assert calls[-1] == {
        **case.registry.tools[case.call.name].definition.validate_arguments(case.call.arguments).to_action().model_dump(mode="json", exclude={"code", "function", "slowly", "submit"}),
        "browserId": str(case.connection.browser_id), "settle": False, "guard": False,
    }


@pytest.mark.parametrize("change,code", [
    ("source", "tool.invalidArguments"), ("digest", "browser.snapshotChanged"),
    ("url", "browser.snapshotChanged"), ("access", "browser.notConnected"),
    ("binding", "browser.bindingChanged"), ("deny-use", "permission.denied"),
])
def test_fill_form_stale_or_denied_never_dispatches(browser_tool, change, code):
    case = browser_tool
    reply = case.env.reply
    if change == "source":
        case.call.arguments["snapshotSha256"] = "0" * 64

    async def approve(request):
        from mato_loop_backend.integrations.browser.adapter import capture_browser_approval

        await capture_browser_approval(
            request, case.call, case.context.session,
            SimpleNamespace(execution_agent_id=case.env.agent.id), case.context.approval_resolution,
        )
        if change == "access":
            case.connection.model_available = False
        elif change == "binding":
            case.connection.browser_id = uuid4()
        if change == f"deny-{request.capability.split('.')[-1]}":
            return PermissionDecision.USER_DENIED
        return PermissionDecision.ALLOW

    async def changed_reply(operation, arguments):
        result = await reply(operation, arguments)
        if operation == "integration.browser.execute" and arguments["action"] == "inspect" and not arguments["settle"]:
            if change == "digest":
                result["data"]["snapshotSha256"] = "b" * 64
            elif change == "url":
                result["url"] = "https://changed.test/"
        return result

    case.context.ask_handler.side_effect = approve
    case.env.reply = changed_reply
    result = case.env.client.portal.call(case.registry.execute, case.call, case.context)
    assert result.is_error and result.metadata["errorCode"] == code
    calls = [frame["arguments"] for frame in case.frames if frame.get("operation") == "integration.browser.execute"]
    assert all(call["action"] == "inspect" for call in calls)
    assert len(calls) == (0 if change == "source" else 2 if change in {"digest", "url"} else 1)
    assert all(call["guard"] is True for call in calls)
    if change == "source":
        case.context.ask_handler.assert_not_awaited()


@pytest.mark.parametrize("action,batch", [
    ("fill_form", None), ("fill_form", {"completed": 1, "total": 1}),
    ("tabs", {"completed": 1, "total": 1}), ("inspect", {"completed": 1, "total": 1}),
])
def test_browser_wire_rejects_batch_for_wrong_action_or_total(browser_tool, action, batch):
    case = browser_tool
    if action != "fill_form":
        case.call.name = "browser_snapshot" if action == "inspect" else "browser_tabs"
        case.call.arguments = {"tabId": case.call.arguments["tabId"]} if action == "inspect" else {"action": "list"}
    reply = case.env.reply

    async def invalid_reply(operation, arguments):
        if operation == "integration.browser.execute" and arguments["action"] == action:
            return {"batch": batch} if action != "inspect" else {
                **await reply(operation, arguments), "batch": batch,
            }
        return await reply(operation, arguments)

    case.env.reply = invalid_reply
    result = case.env.client.portal.call(case.registry.execute, case.call, case.context)
    assert result.is_error and result.metadata["errorCode"] == "executionAgent.protocolError"


@pytest.mark.parametrize("name,action,arguments", [
    ("browser_run_code", "run_code", {"code": "async def run(page):\n    return await page.title()"}),
    ("browser_evaluate", "evaluate", {"function": "element => element.textContent", "ref": "e1"}),
    ("browser_type", "fill", {"ref": "e1", "text": "Ada", "slowly": True, "submit": True}),
    ("browser_tabs", "select", {"action": "select"}),
])
def test_slim_browser_tools_convert_to_one_unguarded_rpc(browser_tool, name, action, arguments):
    case = browser_tool
    case.call.name = name
    case.call.arguments = {"tabId": case.env.navigation["activeTabId"], "observe": False, **arguments}
    script = action in {"run_code", "evaluate"}

    async def reply(operation, wire):
        if operation == "integration.browser.execute":
            assert wire["action"] == action
            assert wire["guard"] is False and wire["settle"] is False
            assert "snapshotSha256" not in wire and "ref" not in wire
            assert ("code" in wire) == (action == "run_code")
            assert ("function" in wire) == (action == "evaluate")
            assert ("slowly" in wire) == (action == "fill")
            assert ("submit" in wire) == (action == "fill")
            if "ref" in arguments:
                assert wire["selector"] == "aria-ref=e1"
            if action == "fill":
                assert wire["slowly"] is True and wire["submit"] is True
            return {"script": {"value": {"changed": True}, "output": "bounded",
                               "errorCode": "browser.actionFailed", "failureReason": "runtimeError",
                               "line": 2}} if script else {}

    case.env.reply = reply
    result = case.env.client.portal.call(case.registry.execute, case.call, case.context)
    assert result.is_error == script, result.content
    if script:
        assert json.loads(result.content)["script"]["value"] == {"changed": True}
        assert result.metadata == {"errorCode": "browser.actionFailed", "privateIntegrationData": True}
    calls = [frame["arguments"] for frame in case.frames if frame.get("operation") == "integration.browser.execute"]
    assert len(calls) == 1


def test_auto_inspection_still_checks_returned_tab_binding(browser_tool):
    case = browser_tool
    case.call.name = "browser_snapshot"
    case.call.arguments = {"tabId": case.env.navigation["activeTabId"]}

    async def reply(operation, _arguments):
        if operation == "integration.browser.execute":
            return {"tabId": str(uuid4()), "text": "wrong page", "data": {"snapshotSha256": "a" * 64}}

    case.env.reply = reply
    result = case.env.client.portal.call(case.registry.execute, case.call, case.context)
    assert result.metadata["errorCode"] == "browser.bindingChanged"
    assert "wrong page" not in result.content
    calls = [frame["arguments"] for frame in case.frames if frame.get("operation") == "integration.browser.execute"]
    assert len(calls) == 1 and calls[0]["guard"] is False


@pytest.mark.parametrize("action", ["inspect", "click"])
def test_browser_inspection_reuse_and_latency_phases(env, monkeypatch, caplog, action):
    caplog.set_level("INFO", logger=execution.__name__)
    clock, inspections = [0.0], []
    monkeypatch.setattr(execution, "monotonic", lambda: clock[0])
    check_access = execution.BrowserConnection.check_access

    async def access(connection, **kwargs):
        await check_access(connection, **kwargs)
        clock[0] += 0.2

    monkeypatch.setattr(execution.BrowserConnection, "check_access", access)

    async def reply(operation, arguments):
        if operation != "integration.browser.execute":
            return None
        if arguments["action"] == "inspect":
            clock[0] += 0.4
            result = {"tabId": arguments["tabId"], "url": "https://private.test/",
                      "text": f"private-inspection-{len(inspections) + 1}",
                      "tabs": [{"id": arguments["tabId"], "url": "https://private.test/", "title": "private title"}],
                      "data": {"snapshotSha256": "a" * 64}}
            inspections.append(result)
            return result
        assert arguments["action"] == "click"
        clock[0] += 0.5
        return {}

    env.reply = reply
    with env.client.websocket_connect(env.url, headers=ORIGIN) as control:
        state = control.receive_json()
        with open_view(env, state):
            connection = env.coordinator.connections[(env.session.id, env.agent.id)]
            tab_id = str(uuid4())
            snapshot = env.client.portal.call(env.coordinator.snapshot, env.session.id, env.agent.id, env.instance.id, tab_id)
            control.receive_json()
            control.receive_json()
            args = {"action": action, "tabId": tab_id, "expectedSnapshot": snapshot,
                    **({"selector": "#private-selector"} if action == "click" else {}),
                    "expectedBrowserId": state["browserId"]}

            async def execute_after_lock_wait():
                await connection.action_lock.acquire()
                task = asyncio.create_task(env.coordinator.execute(env.session.id, env.agent.id, env.instance.id, args))
                try:
                    await asyncio.sleep(0)
                    assert not task.done()
                    clock[0] += 1
                finally:
                    connection.action_lock.release()
                return await task

            result = env.client.portal.call(execute_after_lock_wait)
            assert control.receive_json()["busy"]
            assert not control.receive_json()["busy"]
            assert len(inspections) == 2
            calls = [args["action"] for op, args in env.calls if op == "integration.browser.execute"]
            assert calls == (["inspect", "inspect"] if action == "inspect" else ["inspect", "inspect", "click"])
            if action == "inspect":
                expected = execution.BrowserActionResult.model_validate(inspections[-1])
                assert result == expected.model_copy(update={"data": {"snapshotSha256": snapshot}})
                assert result.text == "private-inspection-2"
            assert not connection.busy
            records = [item for item in caplog.records if item.name == execution.__name__]
            assert len(records) == 2
            authorization, executed = records
            assert authorization.args[:3] == ("snapshot", env.session.id, "ok")
            assert executed.args[:3] == ("execute", env.session.id, "ok")
            assert authorization.args[-1]["total_ms"] == pytest.approx(600)
            timings = executed.args[-1]
            assert timings["lock_wait_ms"] == pytest.approx(1000)
            assert timings["access_ms"] == pytest.approx(200)
            assert timings["reinspection_rpc_ms"] == pytest.approx(400)
            assert timings["total_ms"] == pytest.approx(1600 if action == "inspect" else 2100)
            if action == "inspect":
                assert "action_rpc_ms" not in timings
            else:
                assert timings["action_rpc_ms"] == pytest.approx(500)
            assert "private" not in caplog.text and snapshot not in caplog.text


@pytest.mark.parametrize("failure", [None, "missing", "id", "size", "empty", "offset", "complete", "revoked", "revoked-delete", "revoked-state"])
def test_screenshot_chunks_are_private_bounded_and_deleted(env, failure, monkeypatch):
    image = b"temporary screenshot bytes" * 30000
    screenshot_id = str(uuid4())
    with env.client.websocket_connect(env.url, headers=ORIGIN) as control:
        state = control.receive_json()
        with open_view(env, state):
            connection = env.coordinator.connections[(env.session.id, env.agent.id)]
            tab_id = str(uuid4())
            snapshot = env.client.portal.call(env.coordinator.snapshot, env.session.id, env.agent.id, env.instance.id, tab_id)
            send_state = execution.BrowserConnection.state

            async def changed_state(current):
                if failure == "revoked-state" and current is connection and not current.busy:
                    env.profile.integrations_enabled_by_default = False
                await send_state(current)

            monkeypatch.setattr(execution.BrowserConnection, "state", changed_state)

            async def reply(operation, arguments):
                if operation == "integration.browser.execute" and arguments["action"] == "screenshot":
                    assert "snapshotSha256" not in arguments and "observe" not in arguments
                    return {} if failure == "missing" else {
                        "tabId": tab_id, "screenshot": {"fileId": screenshot_id, "size": len(image)},
                    }
                if operation == "integration.browser.file" and arguments["action"] == "read":
                    assert connection.busy and arguments["fileId"] == screenshot_id
                    offset = arguments["offset"]
                    data = image[offset:offset + arguments["maxBytes"]]
                    assert len(data) <= 65536
                    if failure == "revoked":
                        env.profile.integrations_enabled_by_default = False
                    return {
                        "fileId": str(uuid4()) if failure == "id" else screenshot_id,
                        "size": len(image) + (failure == "size"),
                        "offset": offset + len(data) + (failure == "offset"),
                        "complete": True if failure == "complete" else offset + len(data) == len(image),
                        "data": base64.b64encode(b"" if failure == "empty" else data).decode(),
                    }
                if operation == "integration.browser.file" and arguments["action"] == "delete" and failure == "revoked-delete":
                    env.profile.integrations_enabled_by_default = False

            env.reply = reply
            args = {"action": "screenshot", "tabId": tab_id, "expectedSnapshot": snapshot,
                    "expectedBrowserId": state["browserId"]}
            if failure is None:
                result = env.client.portal.call(env.coordinator.execute, env.session.id, env.agent.id, env.instance.id, args)
                assert len(result._screenshot_bytes) > 500000
                assert result._screenshot_bytes == image
                assert "temporary screenshot bytes" not in result.model_dump_json()
                assert len(result.model_dump_json()) < 1000
            else:
                code = "browser.bindingChanged" if failure in {"revoked", "revoked-delete", "revoked-state"} else "executionAgent.protocolError"
                with pytest.raises(ExecutionClientError, match=code):
                    env.client.portal.call(env.coordinator.execute, env.session.id, env.agent.id, env.instance.id, args)
            deletions = [args for op, args in env.calls if op == "integration.browser.file" and args["action"] == "delete"]
            assert [args["fileId"] for args in deletions] == ([] if failure == "missing" else [screenshot_id])
            assert not connection.busy


def test_snapshot_file_is_streamed_and_deleted(env):
    content = ("- document\n  - button \"Target\"\n" * 20000).encode()
    file_id = str(uuid4())
    with env.client.websocket_connect(env.url, headers=ORIGIN) as control:
        state = control.receive_json()
        with open_view(env, state):
            tab_id = str(uuid4())

            async def reply(operation, arguments):
                if operation == "integration.browser.execute":
                    return {
                        "tabId": tab_id,
                        "url": "https://example.test/",
                        "snapshot": {"fileId": file_id, "size": len(content)},
                        "data": {
                            "snapshotSha256": "a" * 64,
                            "snapshotCharacters": len(content.decode()),
                        },
                    }
                if operation == "integration.browser.file" and arguments["action"] == "read":
                    offset = arguments["offset"]
                    data = content[offset:offset + arguments["maxBytes"]]
                    return {
                        "fileId": file_id,
                        "size": len(content),
                        "offset": offset + len(data),
                        "complete": offset + len(data) == len(content),
                        "data": base64.b64encode(data).decode(),
                    }

            env.reply = reply
            result = env.client.portal.call(
                env.coordinator.execute,
                env.session.id,
                env.agent.id,
                env.instance.id,
                {
                    "action": "inspect",
                    "tabId": tab_id,
                    "expectedBrowserId": state["browserId"],
                },
            )
            with result._snapshot_stream as stream:
                assert stream.read() == content
            assert result.text is None
            assert [
                args["fileId"]
                for op, args in env.calls
                if op == "integration.browser.file" and args["action"] == "delete"
            ] == [file_id]


@pytest.mark.parametrize("phase", ["capture", "read"])
def test_cancelled_screenshot_drains_capture_and_deletes_staging(env, phase):
    started, finish = asyncio.Event(), asyncio.Event()
    screenshot_id = str(uuid4())
    with env.client.websocket_connect(env.url, headers=ORIGIN) as control:
        state = control.receive_json()
        with open_view(env, state):
            connection = env.coordinator.connections[(env.session.id, env.agent.id)]
            tab_id = str(uuid4())
            snapshot = env.client.portal.call(env.coordinator.snapshot, env.session.id, env.agent.id, env.instance.id, tab_id)

            async def reply(operation, arguments):
                capture = operation == "integration.browser.execute" and arguments["action"] == "screenshot"
                read = operation == "integration.browser.file" and arguments["action"] == "read"
                if capture and phase == "capture" or read and phase == "read":
                    started.set()
                    await finish.wait()
                if capture:
                    return {"screenshot": {"fileId": screenshot_id, "size": 3}}
                if read:
                    return {"fileId": screenshot_id, "size": 3, "offset": 3, "complete": True, "data": "YWJj"}

            env.reply = reply
            args = {"action": "screenshot", "tabId": tab_id, "expectedSnapshot": snapshot,
                    "expectedBrowserId": state["browserId"]}

            async def cancel():
                task = asyncio.create_task(env.coordinator.execute(env.session.id, env.agent.id, env.instance.id, args))
                try:
                    await asyncio.wait_for(started.wait(), 2)
                    task.cancel()
                    await asyncio.sleep(0)
                    assert not task.done() and connection.busy
                finally:
                    finish.set()
                    with pytest.raises(asyncio.CancelledError):
                        await task

            env.client.portal.call(cancel)
            deletions = [args for op, args in env.calls if op == "integration.browser.file" and args["action"] == "delete"]
            assert [args["fileId"] for args in deletions] == [screenshot_id]
            assert not connection.busy


@pytest.mark.parametrize("digest", [None, 123, "not-a-digest"])
def test_malformed_snapshot_is_rejected_before_becoming_approval_binding(env, digest):
    async def reply(operation, arguments):
        if operation == "integration.browser.execute":
            return {"tabId": arguments["tabId"], "url": "https://page.test/", "data": {"snapshotSha256": digest}}

    env.reply = reply
    with env.client.websocket_connect(env.url, headers=ORIGIN) as control:
        state = control.receive_json()
        with open_view(env, state):
            with pytest.raises(ExecutionClientError, match="executionAgent.protocolError"):
                env.client.portal.call(env.coordinator.snapshot, env.session.id, env.agent.id, env.instance.id, str(uuid4()))


@pytest.mark.parametrize("phase", ["lock", "reinspection"])
def test_cancelled_inspect_never_returns_content_and_records_partial_latency(env, caplog, phase):
    caplog.set_level("INFO", logger=execution.__name__)
    started = asyncio.Event()
    with env.client.websocket_connect(env.url, headers=ORIGIN) as control:
        state = control.receive_json()
        with open_view(env, state):
            connection = env.coordinator.connections[(env.session.id, env.agent.id)]
            tab_id = str(uuid4())
            snapshot = env.client.portal.call(env.coordinator.snapshot, env.session.id, env.agent.id, env.instance.id, tab_id)

            async def reply(operation, arguments):
                if operation == "integration.browser.execute":
                    assert arguments["action"] == "inspect"
                    started.set()
                    await asyncio.Event().wait()

            env.reply = reply
            args = {"action": "inspect", "tabId": tab_id, "expectedSnapshot": snapshot,
                    "expectedBrowserId": state["browserId"]}

            async def cancel():
                if phase == "lock":
                    await connection.action_lock.acquire()
                task = asyncio.create_task(env.coordinator.execute(env.session.id, env.agent.id, env.instance.id, args))
                try:
                    if phase == "lock":
                        await asyncio.sleep(0)
                    else:
                        await asyncio.wait_for(started.wait(), 1)
                finally:
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                    if phase == "lock":
                        connection.action_lock.release()

            env.client.portal.call(cancel)
            assert not connection.busy and not connection.action_lock.locked()
            assert len([op for op, _ in env.calls if op == "integration.browser.execute"]) == (1 if phase == "lock" else 2)
            record = [item for item in caplog.records if item.name == execution.__name__][-1]
            assert record.args[:3] == ("execute", env.session.id, "cancelled")
            timings = record.args[-1]
            assert "total_ms" in timings and "lock_wait_ms" in timings and "action_rpc_ms" not in timings
            assert ("reinspection_rpc_ms" in timings) == (phase == "reinspection")


def test_setup_only_explicit_start_and_viewer_close_does_not_cancel_installer(env):
    cancelled = []

    async def reply(operation, _arguments):
        if operation == "integration.browser.setupread":
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.append(operation)
                raise

    env.reply = reply
    env.documents[UserIntegrationSettings, env.user.id].instances[0].enabled = False
    with env.client.websocket_connect(env.setup_url, headers=ORIGIN) as setup:
        env.client.portal.call(asyncio.sleep, 0)
        assert not env.calls
        setup.send_json({"type": "status"})
        assert setup.receive_json()["state"] == "missing"
        assert [op for op, _ in env.calls] == ["integration.browser.status"]
        with pytest.raises(WebSocketDisconnect), env.client.websocket_connect(env.setup_url, headers=ORIGIN):
            pytest.fail("A second setup viewer was accepted")
        setup.send_json({"type": "start"})
        assert setup.receive_json()["state"] == "installing"
        setup.send_json({"type": "input", "data": base64.b64encode(b"y\r").decode()})
        setup.send_json({"type": "resize", "rows": 30, "columns": 100})
        setup.send_json({"type": "status"})
        setup.receive_json()
        setup.close()
        assert setup.receive()["type"] == "websocket.close"
    assert not routes._setup_viewers
    assert cancelled == ["integration.browser.setupread"]
    assert [args for op, args in env.calls if op == "integration.browser.setup"] == [{"ownerId": str(env.user.id)}]
    assert any(op == "integration.browser.setupwrite" and args["setupId"] == str(env.setup_id) for op, args in env.calls)
    assert not any("cancel" in op or op == "integration.browser.close" for op, _ in env.calls)


def test_closing_viewer_during_setup_start_never_sends_wire_cancel(env, monkeypatch):
    manager = ExecutionAgentConnectionManager()
    sent, started = [], asyncio.Event()

    async def send_text(raw):
        message = json.loads(raw)
        sent.append(message)
        if message.get("operation") == "integration.browser.setup":
            started.set()

    agent_socket = SimpleNamespace(send_text=send_text)
    hello = HelloMessage(type="hello", protocol_version=1, agent_id=env.agent.id, agent_version="test",
                         computer_name="test", platform="linux", operations=list(SUPPORTED_OPERATIONS),
                         home_directory="/home/user")
    env.client.portal.call(manager.register, hello, agent_socket)
    monkeypatch.setattr(execution.execution_agent_connections, "request", manager.request)
    try:
        with env.client.websocket_connect(env.setup_url, headers=ORIGIN) as setup:
            setup.send_json({"type": "start"})
            env.client.portal.call(asyncio.wait_for, started.wait(), 1)
        request_id = next(item["id"] for item in sent if item.get("operation") == "integration.browser.setup")
        assert not any(item["type"] == "cancel" and item["id"] == request_id for item in sent)
        task = routes._setup_starts[env.agent.id]
        assert not task.done()

        async def complete_start():
            response = ResponseMessage(type="response", id=UUID(request_id), ok=True,
                                       result={"state": "installing", "setupId": str(env.setup_id)})
            await manager.receive(env.agent.id, agent_socket, response.model_dump_json())
            await asyncio.wait_for(asyncio.shield(task), 1)
            await asyncio.sleep(0)
            assert not routes._setup_starts

        env.client.portal.call(complete_start)
    finally:
        env.client.portal.call(manager.unregister, env.agent.id, agent_socket)


@pytest.mark.parametrize("channel", ["setup", "view"])
@pytest.mark.parametrize("invalid", ["origin", "login"])
def test_other_websocket_channels_require_origin_and_login(env, monkeypatch, channel, invalid):
    headers = {"origin": "https://evil.test"} if invalid == "origin" else ORIGIN
    if invalid == "login":
        monkeypatch.setattr(routes, "_browser_login", AsyncMock(return_value=None))
    url = env.setup_url if channel == "setup" else f"/browsers/{env.instance.id}/view"
    with pytest.raises(WebSocketDisconnect) as error, env.client.websocket_connect(url, headers=headers):
        pytest.fail("Unauthorized viewer was accepted")
    assert error.value.code == (4403 if invalid == "origin" else 4401)
    assert not env.calls


@pytest.mark.parametrize("invalid", ["session", "integration", "browser", "view", "missing-view", "owner", "login", "duplicate"])
def test_view_socket_is_bound_to_control_viewer_and_exclusive(env, monkeypatch, invalid):
    with env.client.websocket_connect(env.url, headers=ORIGIN) as control:
        state = control.receive_json()
        with open_view(env, state):
            url = view_url(env, state)
            if invalid == "session":
                url = url.replace(str(env.session.id), str(uuid4()))
            elif invalid == "integration":
                url = url.replace(str(env.instance.id), str(uuid4()))
            elif invalid == "browser":
                url = url.replace(state["browserId"], str(uuid4()))
            elif invalid == "view":
                url = url.replace(state["viewId"], str(uuid4()))
            elif invalid == "missing-view":
                url = url.split("&viewId=")[0]
            elif invalid == "owner":
                monkeypatch.setattr(routes, "_browser_login", AsyncMock(return_value=_BrowserLogin(
                    SimpleNamespace(id=uuid4()), env.login.id, env.login.expires_at,
                )))
            elif invalid == "login":
                monkeypatch.setattr(routes, "_browser_login", AsyncMock(return_value=_BrowserLogin(
                    env.user, uuid4(), env.login.expires_at,
                )))
            with pytest.raises(WebSocketDisconnect), env.client.websocket_connect(url, headers=ORIGIN):
                pytest.fail("Unbound or second view socket was accepted")
            assert len(env.coordinator.relays) == 1
            assert sum(op == "integration.browser.view" for op, _ in env.calls) == 1


@pytest.mark.parametrize("payload", ['{"type":"start","command":"touch /tmp/unsafe"}', '{', 'x' * 100001])
def test_malformed_setup_controls_never_start_installation(env, payload):
    with env.client.websocket_connect(env.setup_url, headers=ORIGIN) as setup:
        setup.send_text(payload)
        assert setup.receive_json()["error"]["code"] == "browser.invalidTarget"
    assert not env.calls


@pytest.mark.parametrize("invalid", ["token", "reused", "ended", "closed"])
def test_relay_token_is_one_use_and_bound_to_live_connection(env, invalid):
    with env.client.websocket_connect(env.url, headers=ORIGIN) as control:
        control.receive_json()
        connection = env.coordinator.connections[(env.session.id, env.agent.id)]
        relay = execution.BrowserRelay(connection)
        env.coordinator.relays[relay.relay_id] = relay
        token = relay.token
        if invalid == "token":
            token += "wrong"
        elif invalid == "reused":
            relay.accepted = True
        elif invalid == "ended":
            relay.ended.set()
        else:
            connection.closed = True
        try:
            with pytest.raises(WebSocketDisconnect) as error, env.client.websocket_connect(
                f"/browser-relay/{relay.relay_id}", headers={"authorization": f"Bearer {token}"},
            ):
                pytest.fail("Invalid relay was accepted")
            assert error.value.code == 4403
        finally:
            connection.closed = False


def test_real_binary_relay_bidirectional_bounded_and_one_use(env):
    async def reply(operation, arguments):
        if operation == "integration.browser.view":
            relay = env.coordinator.relays[UUID(arguments["relayId"])]
            async with asyncio.timeout(2):
                while not relay.accepted:
                    await asyncio.sleep(0.001)
            return {"ok": True, "protocol": "page-stream-v1"}

    env.reply = reply
    with env.client.websocket_connect(env.url, headers=ORIGIN) as control:
        state = control.receive_json()
        with env.client.websocket_connect(view_url(env, state), headers=ORIGIN) as viewer:
            env.client.portal.call(asyncio.sleep, 0)
            relay = next(iter(env.coordinator.relays.values()))
            assert relay.incoming.maxsize == relay.outgoing.maxsize == 4
            headers = {"authorization": f"Bearer {relay.token}"}
            url = f"/browser-relay/{relay.relay_id}"
            with env.client.websocket_connect(url, headers=headers) as agent:
                agent.send_bytes(b"page-stream-frame")
                assert viewer.receive_bytes() == b"page-stream-frame"
                viewer.send_bytes(b"\x05\x01\x00\x01\x00\x02")
                assert agent.receive_bytes() == b"\x05\x01\x00\x01\x00\x02"
                with pytest.raises(WebSocketDisconnect) as error, env.client.websocket_connect(url, headers=headers):
                    pytest.fail("Relay token was reused")
                assert error.value.code == 4403
                agent.send_bytes(b"x" * (1024 * 1024))
                assert len(viewer.receive_bytes()) == 1024 * 1024
                agent.close()
                assert viewer.receive()["type"] == "websocket.close"
    assert not env.coordinator.relays


def test_relay_backpressure_blocks_until_queue_space_is_available(env):
    with env.client.websocket_connect(env.url, headers=ORIGIN) as control:
        control.receive_json()
        relay = execution.BrowserRelay(env.coordinator.connections[(env.session.id, env.agent.id)])
        env.coordinator.relays[relay.relay_id] = relay
        with env.client.websocket_connect(f"/browser-relay/{relay.relay_id}",
                                          headers={"authorization": f"Bearer {relay.token}"}) as agent:
            for index in range(6):
                agent.send_bytes(bytes([index]))

            async def full():
                async with asyncio.timeout(1):
                    while not relay.incoming.full():
                        await asyncio.sleep(0.001)

            env.client.portal.call(full)
            assert relay.incoming.qsize() == 4

            async def drain():
                async with asyncio.timeout(1):
                    return [await relay.incoming.get() for _ in range(6)]

            assert env.client.portal.call(drain) == [bytes([index]) for index in range(6)]


async def test_page_stream_input_passes_transport_while_browser_action_lock_is_held():
    release = asyncio.Event()

    async def receive():
        if not release.is_set():
            release.set()
            return {"type": "websocket.receive", "bytes": b"typed-input"}
        await asyncio.Event().wait()

    connection = SimpleNamespace(action_lock=asyncio.Lock(), closed=False)
    await connection.action_lock.acquire()
    outgoing = asyncio.Queue(maxsize=4)
    task = asyncio.create_task(
        routes._pump_socket(
            SimpleNamespace(receive=receive, send_bytes=AsyncMock()),
            asyncio.Queue(maxsize=4), outgoing, connection=connection,
        )
    )
    try:
        assert await asyncio.wait_for(outgoing.get(), 1) == b"typed-input"
        assert connection.action_lock.locked()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        connection.action_lock.release()


@pytest.mark.parametrize("payload", ["text-not-binary", b"x" * (1024 * 1024 + 1)])
def test_viewer_rejects_nonbinary_and_oversized_frames_without_relaying(env, payload):
    with env.client.websocket_connect(env.url, headers=ORIGIN) as control:
        state = control.receive_json()
        with env.client.websocket_connect(view_url(env, state), headers=ORIGIN) as viewer:
            env.client.portal.call(asyncio.sleep, 0)
            relay = next(iter(env.coordinator.relays.values()))
            (viewer.send_text if isinstance(payload, str) else viewer.send_bytes)(payload)
            assert control.receive_json()["error"]["code"] == "browser.invalidTarget"
            assert viewer.receive()["type"] == "websocket.close"
            assert relay.outgoing.empty()


@pytest.mark.parametrize("text", [
    "", "Plain text\nsecond line", "Gr\u00fc\u00dfe \u65e5\u672c\u8a9e \U0001f642 e\u0301",
    "x" * 16384, "\u00e4" * 8192, "\U0001f642" * 4096,
], ids=["empty", "ascii", "unicode", "ascii-limit", "two-byte-limit", "four-byte-limit"])
def test_clipboard_round_trip_preserves_text_and_utf8_byte_limit(env, text):
    with env.client.websocket_connect(env.url, headers=ORIGIN) as control:
        state = control.receive_json()
        url = file_url(env, state, "clipboard")
        written = env.client.put(url, json={"text": text})
        assert written.status_code == 200 and written.json() == {"text": text}
        read = env.client.post(file_url(env, state, "clipboard/copy"))
        assert read.status_code == 200 and read.json() == {"text": text}
        assert [args for op, args in env.calls if op == "integration.browser.clipboard"] == [
            {"browserId": state["browserId"], "action": "write", "text": text},
            {"browserId": state["browserId"], "action": "copy"},
        ]


@pytest.mark.parametrize("method", ["POST", "PUT"])
@pytest.mark.parametrize("invalid, code", [
    ("unauthenticated", "auth.notAuthenticated"), ("owner", "browser.notConnected"),
    ("session", "browser.notConnected"), ("browser", "browser.notConnected"),
    ("integration", "browser.notConnected"), ("expired", "auth.sessionExpired"),
    ("inactive", "auth.sessionExpired"), ("binding", "browser.bindingChanged"),
    ("agent-owner", "executionAgent.notFound"),
    ("disabled", "browser.unavailable"), ("worker-locked", "browser.busy"), ("busy", "browser.busy"),
    ("view", "browser.notConnected"), ("login", "browser.notConnected"),
    ("global-disabled", "browser.unavailable"),
])
def test_clipboard_requires_live_authorized_manual_browser(env, monkeypatch, method, invalid, code):
    with env.client.websocket_connect(env.url, headers=ORIGIN) as control:
        state = control.receive_json()
        with open_view(env, state):
            url = file_url(env, state, "clipboard" if method == "PUT" else "clipboard/copy")
            connection = env.coordinator.connections[(env.session.id, env.agent.id)]
            if invalid == "unauthenticated":
                monkeypatch.setattr(routes, "_browser_login", AsyncMock(return_value=None))
            elif invalid == "owner":
                monkeypatch.setattr(routes, "_browser_login", AsyncMock(return_value=_BrowserLogin(
                    SimpleNamespace(id=uuid4()), env.login.id, env.login.expires_at,
                )))
            elif invalid == "login":
                monkeypatch.setattr(routes, "_browser_login", AsyncMock(return_value=_BrowserLogin(
                    env.user, uuid4(), env.login.expires_at,
                )))
            elif invalid == "view":
                url = url.replace(state["viewId"], str(uuid4()))
            elif invalid == "session":
                url = url.replace(str(env.session.id), str(uuid4()))
            elif invalid == "browser":
                url = url.replace(state["browserId"], str(uuid4()))
            elif invalid == "integration":
                url = url.replace(str(env.instance.id), str(uuid4()))
            elif invalid == "expired":
                env.login.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            elif invalid == "inactive":
                env.user.is_active = False
            elif invalid == "binding":
                env.session.preferred_execution_agent_id = uuid4()
            elif invalid == "agent-owner":
                env.agent.user_id = uuid4()
            elif invalid == "tab":
                env.session.control_center = {"tabs": []}
            elif invalid == "disabled":
                env.documents[UserIntegrationSettings, env.user.id].instances[0].enabled = False
            elif invalid == "global-disabled":
                env.documents[IntegrationSettings, None].adapter_availability["browser"] = "disabled"
            elif invalid == "worker-locked":
                connection.manual_enabled = False
            else:
                connection.busy = True
            response = env.client.request(method, url, **({"json": {"text": "private clipboard"}} if method == "PUT" else {}))
            assert response.status_code >= 400 and response.json()["error"]["code"] == code
            if invalid == "unauthenticated":
                assert response.status_code == 401
            elif invalid in {"worker-locked", "busy"}:
                assert response.status_code == 409
            assert "private clipboard" not in response.text
            assert not any(op == "integration.browser.clipboard" for op, _ in env.calls)


@pytest.mark.parametrize("payload", [
    {}, {"text": None}, {"text": 1}, {"text": []}, {"text": "private clipboard", "action": "read"},
    {"text": "x" * 16385}, {"text": "\u00e4" * 8192 + "x"}, {"text": "\U0001f642" * 4096 + "x"},
    {"text": "\ud800"},
], ids=["missing", "null", "number", "list", "host-field", "ascii-overflow", "two-byte-overflow", "four-byte-overflow", "surrogate"])
def test_invalid_clipboard_input_is_localized_and_never_dispatched(env, payload):
    with env.client.websocket_connect(env.url, headers=ORIGIN) as control:
        state = control.receive_json()
        response = env.client.put(file_url(env, state, "clipboard"), content=json.dumps(payload),
                                  headers={"content-type": "application/json"})
        assert response.status_code == 422
        assert response.json()["error"]["code"].startswith("validation.")
        assert "private clipboard" not in response.text and "surrogates" not in response.text
        assert not any(op == "integration.browser.clipboard" for op, _ in env.calls)


@pytest.mark.parametrize("method", ["POST", "PUT"])
@pytest.mark.parametrize("payload", [
    {}, {"text": 123}, {"text": "\u00e4" * 8193}, {"text": "private clipboard", "unexpected": "secret"},
], ids=["missing", "wrong-type", "unicode-overflow", "extra-field"])
def test_malformed_remote_clipboard_result_is_sanitized(env, method, payload):
    async def reply(operation, _arguments):
        if operation == "integration.browser.clipboard":
            return payload

    env.reply = reply
    with env.client.websocket_connect(env.url, headers=ORIGIN) as control:
        state = control.receive_json()
        response = env.client.request(method, file_url(env, state, "clipboard" if method == "PUT" else "clipboard/copy"),
                                      **({"json": {"text": "value"}} if method == "PUT" else {}))
        assert response.status_code >= 400
        assert response.json()["error"]["code"] == "executionAgent.protocolError"
        assert "private clipboard" not in response.text and "secret" not in response.text


@pytest.mark.parametrize("text", [None, "value"], ids=["copy", "write"])
def test_clipboard_revalidates_login_after_waiting_for_action_lock(env, monkeypatch, text):
    checked, release = asyncio.Event(), asyncio.Event()
    original = env.coordinator.manual

    async def manual(*args):
        connection = await original(*args)
        checked.set()
        return connection

    monkeypatch.setattr(env.coordinator, "manual", manual)
    with env.client.websocket_connect(env.url, headers=ORIGIN) as control:
        state = control.receive_json()
        connection = env.coordinator.connections[(env.session.id, env.agent.id)]

        async def hold_lock():
            async with connection.action_lock:
                await release.wait()

        holder = env.client.portal.start_task_soon(hold_lock)
        env.client.portal.call(asyncio.sleep, 0)
        assert connection.action_lock.locked()
        async def clipboard():
            authorized = await env.coordinator.manual(env.user.id, env.instance.id, env.session.id,
                                                       UUID(state["browserId"]), UUID(state["viewId"]), env.login.id)
            return await routes._clipboard(authorized, text)

        task = env.client.portal.start_task_soon(clipboard)
        try:
            env.client.portal.call(asyncio.wait_for, checked.wait(), 1)
            env.objects.pop((LoginSession, env.login.id))
        finally:
            env.client.portal.call(release.set)
            holder.result(timeout=2)
        with pytest.raises(routes.ApiError) as error:
            task.result(timeout=2)
        assert error.value.code == "auth.sessionExpired"
        assert not any(op == "integration.browser.clipboard" for op, _ in env.calls)


def test_pending_manual_mutation_serializes_model_action(env):
    started, finish = asyncio.Event(), asyncio.Event()

    async def reply(op, arguments):
        if op == "integration.browser.clipboard" and arguments["action"] == "write":
            started.set()
            await finish.wait()

    env.reply = reply
    with env.client.websocket_connect(env.url, headers=ORIGIN) as control:
        state = control.receive_json()
        with open_view(env, state):
            connection = env.coordinator.connections[(env.session.id, env.agent.id)]
            task = env.client.portal.start_task_soon(routes._clipboard, connection, "value")
            try:
                env.client.portal.call(asyncio.wait_for, started.wait(), 1)
                model = env.client.portal.start_task_soon(env.coordinator.execute,
                    env.session.id, env.agent.id, env.instance.id,
                    {"action": "tabs", "expectedBrowserId": state["browserId"]})
                assert not model.done() and env.manual
            finally:
                env.client.portal.call(finish.set)
            task.result(timeout=2)
            model.result(timeout=2)
            assert env.manual


def test_upload_chunks_are_bounded_and_finish_precedes_choose(env, session_upload):
    with env.client.websocket_connect(env.url, headers=ORIGIN) as control:
        state = control.receive_json()
        data = b"x" * 65536 + b"end"
        session_upload.write(data)
        session_upload.seek(0)
        response = env.client.post(file_url(env, state, "upload-source"), json={
            "source": {"scope": "session", "path": "sample.txt"},
        })
        assert response.status_code == 200 and response.json() == {"uploadId": env.file_id, "chosen": True}
        calls = [args for op, args in env.calls if op == "integration.browser.file"]
        assert [args["action"] for args in calls] == ["begin", "write", "write", "finish", "choose"]
        writes = [args for args in calls if args["action"] == "write"]
        assert [args["offset"] for args in writes] == [0, 65536]
        assert b"".join(base64.b64decode(args["data"], validate=True) for args in writes) == data


def test_existing_computer_file_uses_agent_upload_and_same_choose_flow(env):
    with env.client.websocket_connect(env.url, headers=ORIGIN) as control:
        state = control.receive_json()
        response = env.client.post(file_url(env, state, "upload-source"), json={
            "source": {"scope": "filesystem", "path": "/home/user/sample.txt"},
        })
        assert response.status_code == 200
        assert response.json() == {"uploadId": env.file_id, "chosen": True, "size": 1}
        upload, choose = [(op, args) for op, args in env.calls if op in {
            "integration.browser.upload", "integration.browser.file",
        }]
        assert upload == ("integration.browser.upload", {
            "browserId": state["browserId"], "path": "/home/user/sample.txt",
        })
        assert choose[0] == "integration.browser.file"
        assert choose[1]["action"] == "choose" and choose[1]["fileId"] == env.file_id


def test_existing_session_file_uses_bounded_upload_flow(env, monkeypatch):
    def open_file(scope, path):
        assert scope == routes.FileRoot.SESSION and path == "notes/sample.txt"
        handle = TemporaryFile()
        handle.write(b"session data")
        handle.seek(0)
        return handle

    monkeypatch.setattr(routes, "RootFileService", lambda *_args, **_kwargs: SimpleNamespace(open_file=open_file))
    with env.client.websocket_connect(env.url, headers=ORIGIN) as control:
        state = control.receive_json()
        response = env.client.post(file_url(env, state, "upload-source"), json={
            "source": {"scope": "session", "path": "notes/sample.txt"},
        })
        assert response.status_code == 200
        writes = [args for op, args in env.calls if op == "integration.browser.file" and args["action"] == "write"]
        assert b"".join(base64.b64decode(args["data"]) for args in writes) == b"session data"


def test_upload_source_rejects_redundant_body_binding_before_agent_rpc(env):
    with env.client.websocket_connect(env.url, headers=ORIGIN) as control:
        state = control.receive_json()
        before = list(env.calls)
        response = env.client.post(file_url(env, state, "upload-source"), json={
            "sessionId": str(env.session.id), "browserId": str(uuid4()),
            "viewId": state["viewId"], "source": {"scope": "filesystem", "path": "/home/user/sample.txt"},
        })
        assert response.status_code == 422
        assert response.json()["error"]["code"].startswith("validation.")
        assert env.calls == before


def test_human_download_is_saved_in_session_files_before_deleting_staging(env):
    async def reply(operation, arguments):
        if operation == "integration.browser.files":
            origin = next(args for op, args in env.calls if op == "integration.browser.control" and args["manual"])
            return {"downloads": [{
                "id": env.file_id, "filename": "sample.txt", "status": "ready", "size": 1,
                "sha256": hashlib.sha256(b"x").hexdigest(), "origin": origin["origin"], "controlEpoch": origin["controlEpoch"],
            }]}

    env.reply = reply
    with env.client.websocket_connect(env.url, headers=ORIGIN) as control:
        state = control.receive_json()
        changed = env.client.portal.call(env.coordinator.poll_downloads, env.coordinator.connections[(env.session.id, env.agent.id)])
        response = env.client.get(file_url(env, state, "downloads"))
        assert response.status_code == 200
        assert response.json()["downloads"][0]["status"] == "saved"
        saved = response.json()["downloads"][0]
        assert changed and saved["root"] == "files" and saved["path"].startswith("sample-")
        assert execution.RootFileService(env.session.id).read_bytes("session", saved["path"], limit=10) == b"x"
        assert [args["action"] for op, args in env.calls if op == "integration.browser.file"] == ["read", "delete"]


def test_saved_download_remains_after_worker_removes_staging_record(env):
    polls = 0

    async def reply(operation, arguments):
        nonlocal polls
        if operation == "integration.browser.files":
            polls += 1
            if polls > 1:
                return {"downloads": []}
            origin = next(args for op, args in env.calls if op == "integration.browser.control" and args["manual"])
            return {"downloads": [{
                "id": env.file_id, "filename": "sample.txt", "status": "ready", "size": 1,
                "sha256": hashlib.sha256(b"x").hexdigest(), "origin": origin["origin"], "controlEpoch": origin["controlEpoch"],
            }]}

    env.reply = reply
    with env.client.websocket_connect(env.url, headers=ORIGIN) as control:
        state = control.receive_json()
        first = env.client.get(file_url(env, state, "downloads")).json()["downloads"]
        second = env.client.get(file_url(env, state, "downloads")).json()["downloads"]
        assert second == first and second[0]["status"] == "saved"
        assert sum(op == "integration.browser.file" and args["action"] == "read" for op, args in env.calls) == 1


async def test_cancelled_download_save_remembers_result_and_does_not_duplicate_it(env, monkeypatch):
    from mato_loop_backend.execution.browser_contracts import BrowserDownloadState
    from mato_loop_backend.files import access

    connection = SimpleNamespace(
        session_id=env.session.id, owner_id=env.user.id, browser_id=uuid4(), downloads=[],
        check_access=AsyncMock(), request=AsyncMock(return_value=execution.BrowserControlResult(ok=True)),
    )
    download = BrowserDownloadState(id=env.file_id, filename="result.txt", status="ready", size=1, sha256=hashlib.sha256(b"x").hexdigest())
    monkeypatch.setattr(env.coordinator, "_read_browser_file", AsyncMock(return_value=BytesIO(b"x")))
    started, release = asyncio.Event(), asyncio.Event()
    create = access.create_output_stream

    async def paused(*args):
        started.set()
        await release.wait()
        return await create(*args)

    monkeypatch.setattr(access, "create_output_stream", paused)
    task = asyncio.create_task(env.coordinator.save_download(connection, download, authorize=AsyncMock()))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    saved = await env.coordinator.save_download(connection, download, authorize=AsyncMock())
    files = execution.RootFileService(env.session.id)
    assert (len(files.list_entries("session", ".").entries), files.read_bytes("session", saved["path"], limit=10), connection.request.await_count) == (1, b"x", 1)


def test_download_hash_mismatch_retains_staging(env):
    async def reply(operation, arguments):
        if operation == "integration.browser.files":
            origin = next(args for op, args in env.calls if op == "integration.browser.control" and args["manual"])
            return {"downloads": [{
                "id": env.file_id, "filename": "sample.txt", "status": "ready", "size": 1,
                "sha256": "a" * 64, "origin": origin["origin"], "controlEpoch": origin["controlEpoch"],
            }]}

    env.reply = reply
    with env.client.websocket_connect(env.url, headers=ORIGIN) as control:
        state = control.receive_json()
        response = env.client.get(file_url(env, state, "downloads"))
        assert response.status_code == 200
        assert response.json()["downloads"][0]["errorCode"] == "executionAgent.protocolError"
        assert not any(op == "integration.browser.file" and args["action"] == "delete" for op, args in env.calls)


def test_model_download_ask_stays_staged_for_foreground_tool(env, monkeypatch):
    async def reply(operation, _arguments):
        if operation == "integration.browser.files":
            return {"downloads": [{
                "id": env.file_id, "filename": "sample.txt", "status": "ready", "size": 1,
                "sha256": "a" * 64, "origin": issued["origin"], "controlEpoch": issued["controlEpoch"],
            }]}

    env.reply = reply
    with env.client.websocket_connect(env.url, headers=ORIGIN) as control:
        state = control.receive_json()
        connection = env.coordinator.connections[(env.session.id, env.agent.id)]
        issued = connection.origin("model", run_id=uuid4())
        monkeypatch.setattr(env.coordinator, "_download_decision", AsyncMock(return_value=PermissionDecision.ASK))
        response = env.client.get(file_url(env, state, "downloads"))
        assert response.status_code == 200
        assert response.json()["downloads"][0]["status"] == "permission_required"
        assert not any(op == "integration.browser.file" for op, _ in env.calls)


@pytest.mark.parametrize("browser_decision,status", [
    (PermissionDecision.ASK, "permission_required"),
    (PermissionDecision.DENY, "failed"),
    (PermissionDecision.ALLOW, "saved"),
])
def test_model_download_requires_browser_use_for_background_publish(
    env, monkeypatch, browser_decision, status,
):
    run = AgentRun(
        id=uuid4(), session_id=env.session.id, trust_level="public",
        execution_agent_id=env.agent.id, execution_operations=list(env.operations),
    )
    env.objects[(AgentRun, run.id)] = run
    checked = []

    async def authorize(_db, _run, _session, _call, request, _root_files, *, check_only):
        assert check_only
        checked.append(request)
        if request.capability == "browser.use" and browser_decision == PermissionDecision.ALLOW:
            return PermissionDecision.ALLOW if PermissionPolicy.grant_matches(grant, request, None) else PermissionDecision.ASK
        return browser_decision if request.capability == "browser.use" else PermissionDecision.ALLOW

    monkeypatch.setattr(AgentRuntime, "_authorize", authorize)

    issued = {}

    async def reply(operation, arguments):
        if operation == "integration.browser.control" and "origin" in arguments:
            issued.update(arguments)
        if operation == "integration.browser.files":
            return {"downloads": [{
                "id": env.file_id, "filename": "sample.txt", "status": "ready", "size": 1,
                "sha256": hashlib.sha256(b"x").hexdigest(), "origin": issued["origin"], "controlEpoch": issued["controlEpoch"],
            }]}

    env.reply = reply
    with env.client.websocket_connect(env.url, headers=ORIGIN) as control:
        state = control.receive_json()
        connection = env.coordinator.connections[(env.session.id, env.agent.id)]
        grant = PermissionGrant(
            capability="category.write", resource_type="integration_resource",
            resource_scope=integration_resource_scope(env.instance.id, _model_revision(env.instance), f"browser:{state['browserId']}"),
        )
        async def model_action():
            async with env.coordinator._model_action(connection, run.id):
                pass

        env.client.portal.call(model_action)
        response = env.client.get(file_url(env, state, "downloads"))
        assert response.status_code == 200
        assert response.json()["downloads"][0]["status"] == status
        assert [request.capability for request in checked] == ["browser.use"] * (2 if browser_decision == PermissionDecision.ALLOW else 1)
        assert checked[0].policy_integration_id == env.instance.id
        assert any(op == "integration.browser.file" and args["action"] == "delete" for op, args in env.calls) is (browser_decision == PermissionDecision.ALLOW)


@pytest.mark.parametrize("failure,code", [
    (routes.RootFileError("file.tooLarge", "Storage limit exceeded"), "file.tooLarge"),
    (OSError(errno.ENOSPC, "No space left on device"), "browser.actionFailed"),
    (TimeoutError(), "browser.actionFailed"),
])
def test_backend_storage_failure_does_not_delete_download(env, monkeypatch, failure, code):
    async def reply(operation, arguments):
        if operation == "integration.browser.files":
            return {"downloads": [{
                "id": env.file_id, "filename": "sample.txt", "status": "ready", "size": 1,
                "sha256": hashlib.sha256(b"x").hexdigest(), "origin": issued["origin"], "controlEpoch": issued["controlEpoch"],
            }]}

    env.reply = reply
    with env.client.websocket_connect(env.url, headers=ORIGIN) as control:
        state = control.receive_json()
        connection = env.coordinator.connections[(env.session.id, env.agent.id)]
        issued = connection.origin("model", run_id=uuid4())
        decision = AsyncMock(return_value=PermissionDecision.ALLOW)
        monkeypatch.setattr(env.coordinator, "_download_decision", decision)
        monkeypatch.setattr("mato_loop_backend.files.access.create_output_stream", AsyncMock(side_effect=failure))
        response = env.client.get(file_url(env, state, "downloads"))
        assert response.status_code == 200
        assert response.json()["downloads"][0]["errorCode"] == code
        assert not any(op == "integration.browser.file" and args["action"] == "delete" for op, args in env.calls)


def test_foreground_download_revalidates_metadata_after_approval(env, monkeypatch):
    integrations = IntegrationSettings(instances=[env.instance])
    monkeypatch.setattr(execution, "browser_coordinator", env.coordinator)
    monkeypatch.setattr(ToolContext, "current_integrations", AsyncMock(return_value=integrations))
    monkeypatch.setattr(mock_tools_access, 'profile_tool_settings', AsyncMock(return_value=ToolSettings()))
    run_id = uuid4()
    env.objects[AgentRun, run_id] = AgentRun(id=run_id, session_id=env.session.id, status="completed")

    polls = 0

    async def reply(operation, arguments):
        nonlocal polls
        if operation == "integration.browser.files":
            polls += 1
            return {"downloads": [{
                "id": env.file_id, "filename": "sample.txt", "status": "ready", "size": 1,
                "sha256": ("a" if polls == 1 else "b") * 64, "origin": issued["origin"], "controlEpoch": issued["controlEpoch"],
            }]}

    env.reply = reply
    with env.client.websocket_connect(env.url, headers=ORIGIN) as control:
        state = control.receive_json()
        with open_view(env, state):
            connection = env.coordinator.connections[(env.session.id, env.agent.id)]
            issued = connection.origin("model", run_id=run_id)
            call = ToolCall("download", "browser_downloads", {"downloadId": env.file_id})
            context = ToolContext(
                db=AsyncMock(), run_id=run_id, session=env.session, tool_call=call,
                root_files=Mock(), integration_settings=integrations,
                execution_client=connection.client,
                ask_handler=AsyncMock(side_effect=lambda request, **_: (
                    PermissionDecision.ALLOW if request.capability != "browser.use"
                    or PermissionPolicy.grant_matches(grant, request, None) else PermissionDecision.DENY
                )),
            )
            grant = PermissionGrant(
                capability="category.write", resource_type="integration_resource",
                resource_scope=integration_resource_scope(env.instance.id, _model_revision(env.instance), f"browser:{state['browserId']}"),
            )

            async def get(model, _key):
                if model is ExecutionAgent:
                    return env.agent
                if model is AgentRun:
                    return SimpleNamespace(
                        execution_agent_id=env.agent.id,
                        execution_operations=list(connection.client.operations),
                    )

            context.db.get.side_effect = get
            result = env.client.portal.call(
                mock_tools_catalog.ToolRegistry([]).effective(integrations).execute, call, context,
            )
            assert result.is_error and result.metadata["errorCode"] == "browser.bindingChanged"
            assert not any(op == "integration.browser.file" for op, _ in env.calls)


def test_oversized_upload_rejected_before_rpc(env, monkeypatch, session_upload):
    monkeypatch.setattr(execution, "MAX_BROWSER_FILE_BYTES", 0)
    with env.client.websocket_connect(env.url, headers=ORIGIN) as control:
        state = control.receive_json()
        response = env.client.post(file_url(env, state, "upload-source"), json={
            "source": {"scope": "session", "path": "sample.txt"},
        })
        assert response.status_code >= 400
        assert response.json()["error"]["code"] == "browser.fileTooLarge"
        assert not any(op == "integration.browser.file" for op, _ in env.calls)


@pytest.mark.parametrize("invalid", ["file-id", "offset", "size", "complete", "malformed", "finish"])
def test_malformed_upload_reply_aborts_and_deletes_staging(env, invalid, session_upload):
    async def reply(operation, arguments):
        if operation == "integration.browser.file" and arguments["action"] == ("finish" if invalid == "finish" else "write"):
            return {"fileId": str(uuid4()) if invalid == "file-id" else env.file_id,
                    "offset": 0 if invalid == "offset" else 1, "size": 2 if invalid == "size" else 1,
                    "complete": invalid == "complete", **({"data": "!"} if invalid == "malformed" else {})}

    env.reply = reply
    with env.client.websocket_connect(env.url, headers=ORIGIN) as control:
        state = control.receive_json()
        response = env.client.post(file_url(env, state, "upload-source"), json={
            "source": {"scope": "session", "path": "sample.txt"},
        })
        assert response.status_code >= 400
        assert response.json()["error"]["code"] == "executionAgent.protocolError"
        calls = [args for op, args in env.calls if op == "integration.browser.file"]
        assert calls[-1]["action"] == "delete" and calls[-1]["fileId"] == env.file_id
        assert not any(args["action"] == "choose" for args in calls)


@pytest.mark.parametrize("change", ["login", "binding"])
def test_upload_revalidates_access_after_finish_before_choosing_file(env, change, session_upload):
    async def reply(operation, arguments):
        if operation == "integration.browser.file" and arguments["action"] == "finish":
            if change == "login":
                env.objects.pop((LoginSession, env.login.id))
            else:
                env.session.preferred_execution_agent_id = uuid4()

    env.reply = reply
    with env.client.websocket_connect(env.url, headers=ORIGIN) as control:
        state = control.receive_json()
        with open_view(env, state):
            response = env.client.post(file_url(env, state, "upload-source"), json={
                "source": {"scope": "session", "path": "sample.txt"},
            })
            assert not any(args.get("action") == "choose" for _, args in env.calls)
            assert response.status_code >= 400


@pytest.mark.parametrize("invalid", ["file-id", "base64", "oversize", "offset", "filename"])
def test_malformed_download_rejected_before_streaming(env, invalid):
    download_id = str(uuid4())

    async def reply(operation, _arguments):
        if operation == "integration.browser.file":
            return {"fileId": str(uuid4()) if invalid == "file-id" else download_id,
                    "offset": 0 if invalid == "offset" else 1, "size": 1, "complete": invalid != "offset",
                    "filename": "../secret" if invalid == "filename" else "sample.txt",
                    "data": "!" if invalid == "base64" else base64.b64encode(b"x" * (65537 if invalid == "oversize" else 1)).decode()}

    env.reply = reply
    with env.client.websocket_connect(env.url, headers=ORIGIN) as control:
        state = control.receive_json()
        response = env.client.get(file_url(env, state, f"download/{download_id}"))
        assert response.status_code >= 400
        assert response.json()["error"]["code"] == "executionAgent.protocolError"


async def test_browser_file_checks_binding_per_chunk_without_reloading_access(monkeypatch):
    monkeypatch.setattr(execution, "monotonic", lambda: 0.0)
    file_id = str(uuid4())
    connection = SimpleNamespace(
        browser_id=uuid4(), agent_id=uuid4(), generation=None, closed=False,
        check_access=AsyncMock(),
        request=AsyncMock(side_effect=[
            execution.BrowserFileResult(file_id=file_id, offset=offset, size=3, complete=offset == 3, data="eA==")
            for offset in (1, 2, 3)
        ]),
    )
    binding = Mock()
    descriptor = execution.BrowserFileResult(file_id=file_id, offset=3, size=3, complete=True)
    with await execution.BrowserCoordinator()._read_browser_file(connection, descriptor, binding, delete=False):
        pass
    assert (connection.check_access.await_count, binding.call_count) == (2, 4)


def test_download_stream_uses_exact_offsets_and_safe_headers(env):
    download_id = str(uuid4())

    async def reply(operation, arguments):
        if operation == "integration.browser.file":
            offset = arguments["offset"]
            return {"fileId": download_id, "offset": offset + 2, "size": 4, "complete": offset == 2,
                    "filename": "a b.txt", "data": base64.b64encode(b"ab" if offset == 0 else b"cd").decode()}

    env.reply = reply
    with env.client.websocket_connect(env.url, headers=ORIGIN) as control:
        state = control.receive_json()
        response = env.client.get(file_url(env, state, f"download/{download_id}"))
        assert response.status_code == 200 and response.content == b"abcd"
        assert response.headers["content-length"] == "4"
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["content-disposition"] == "attachment; filename*=UTF-8''a%20b.txt"
        assert [args["offset"] for op, args in env.calls if op == "integration.browser.file"] == [0, 2]


@pytest.mark.parametrize("invalid", ["offset", "size", "filename", "empty", "base64"])
def test_download_aborts_on_malformed_later_chunk(env, invalid):
    download_id = str(uuid4())

    async def reply(operation, arguments):
        if operation == "integration.browser.file":
            later = arguments["offset"] != 0
            return {"fileId": download_id, "offset": 1 if not later or invalid in {"empty", "offset"} else 2,
                    "size": 4 if later and invalid == "size" else 3, "complete": False,
                    "filename": "changed.txt" if later and invalid == "filename" else "sample.txt",
                    "data": ("" if invalid == "empty" else "!") if later and invalid in {"empty", "base64"} else "eA=="}

    env.reply = reply
    with env.client.websocket_connect(env.url, headers=ORIGIN) as control:
        state = control.receive_json()
        with pytest.raises(ExecutionClientError, match="executionAgent.protocolError"):
            env.client.get(file_url(env, state, f"download/{download_id}"))
        assert [args["offset"] for op, args in env.calls if op == "integration.browser.file"] == [0, 1]


def test_desktop_resize_preserves_runtime(env):
    with env.client.websocket_connect(env.url, headers=ORIGIN) as control:
        state = control.receive_json()
        with open_view(env, state):
            control.send_json({"type": "resize", "width": 1001, "height": 701})
            assert control.receive_json() == {
                "type": "resized", "browserId": state["browserId"], "viewId": state["viewId"], "width": 1001, "height": 701,
            }
            connection = env.coordinator.connections[(env.session.id, env.agent.id)]
            assert str(connection.browser_id) == state["browserId"]
            assert sum(op == "integration.browser.open" for op, _ in env.calls) == 1


@pytest.mark.parametrize("field,value", [("width", True), ("width", "640"), ("height", 400.5),
                                        ("height", 0), ("width", 3841), ("height", 2161), ("display", ":0")])
def test_invalid_desktop_size_never_reaches_agent(env, field, value):
    with env.client.websocket_connect(env.url, headers=ORIGIN) as control:
        control.receive_json()
        control.send_json({"type": "resize", "width": 640, "height": 480, field: value})
        assert control.receive_json()["error"]["code"] == "browser.invalidTarget"
        assert not any(op == "integration.browser.resize" for op, _ in env.calls)


def test_resize_requests_keep_only_latest_pending_geometry(env):
    started, finish = asyncio.Event(), asyncio.Event()

    async def reply(op, args):
        if op == "integration.browser.resize" and args["width"] == 640:
            started.set()
            await finish.wait()

    env.reply = reply
    with env.client.websocket_connect(env.url, headers=ORIGIN) as control:
        control.receive_json()
        control.send_json({"type": "resize", "width": 640, "height": 480})
        env.client.portal.call(asyncio.wait_for, started.wait(), 1)
        try:
            for width in range(700, 750):
                control.send_json({"type": "resize", "width": width, "height": 500})
            env.client.portal.call(asyncio.sleep, 0.1)
        finally:
            env.client.portal.call(finish.set)
        assert control.receive_json()["width"] == 640
        assert control.receive_json()["width"] == 749
        assert [args["width"] for op, args in env.calls if op == "integration.browser.resize"] == [640, 749]


def test_resize_failure_is_scoped_and_does_not_close_browser(env):
    async def reply(op, _args):
        if op == "integration.browser.resize":
            raise ExecutionClientError("browser.actionFailed")

    env.reply = reply
    with env.client.websocket_connect(env.url, headers=ORIGIN) as control:
        control.receive_json()
        control.send_json({"type": "resize", "width": 640, "height": 480})
        event = control.receive_json()
        assert event["operation"] == "resize" and event["error"]["code"] == "browser.actionFailed"
        assert not env.coordinator.connections[(env.session.id, env.agent.id)].closed


@pytest.mark.parametrize("blocked", [None, "busy", "login"])
def test_copy_clipboard_uses_fixed_host_action_and_live_authorization(env, blocked):
    env.clipboard = "Grüße\n世界\n"
    with env.client.websocket_connect(env.url, headers=ORIGIN) as control:
        state = control.receive_json()
        with open_view(env, state):
            if blocked == "busy":
                env.coordinator.connections[(env.session.id, env.agent.id)].busy = True
            elif blocked == "login":
                env.objects.pop((LoginSession, env.login.id))
            response = env.client.post(file_url(env, state, "clipboard/copy"))
            calls = [args for op, args in env.calls if op == "integration.browser.clipboard"]
            if blocked:
                assert response.status_code >= 400 and calls == []
            else:
                assert response.json() == {"text": env.clipboard}
                assert calls == [{"browserId": state["browserId"], "action": "copy"}]


@pytest.mark.parametrize("action", ["state", "new", "select", "close", "navigate", "back", "forward", "reload", "stop"])
def test_navigation_commands_are_bound_and_acknowledged(env, action):
    fields = {} if action in {"state", "new"} else {"tabId": env.navigation["activeTabId"]}
    if action == "navigate":
        fields["url"] = "https://example.test/"
    with env.client.websocket_connect(env.url, headers=ORIGIN) as control:
        state = control.receive_json()
        with open_view(env, state):
            control.send_json({"type": "navigation", "action": action, **fields})
            response = control.receive_json()
            assert response == {"type": "navigation", "browserId": state["browserId"],
                                "viewId": state["viewId"], "completed": True, **env.navigation}
            calls = [args for op, args in env.calls if op == "integration.browser.navigation"]
            assert calls == [{"browserId": state["browserId"], "action": action, **fields}]


@pytest.mark.parametrize("action", ["state", "new", "select", "navigate"])
def test_idle_browser_allows_manual_navigation(env, action):
    fields = {"tabId": env.navigation["activeTabId"]} if action in {"select", "navigate"} else {}
    if action == "navigate":
        fields["url"] = "https://example.test/"
    with env.client.websocket_connect(env.url, headers=ORIGIN) as control:
        state = control.receive_json()
        with open_view(env, state):
            control.send_json({"type": "navigation", "action": action, **fields})
            response = control.receive_json()
            assert response["type"] == "navigation" and response["completed"]


@pytest.mark.parametrize("fields", [
    {"action": "state", "tabId": None}, {"action": "new", "nativeChrome": True},
    {"action": "chrome", "nativeChrome": "true"}, {"action": "select"},
    {"action": "evaluate", "text": "alert(1)"},
    {"action": "new", "url": "javascript:alert(1)"}, {"action": "new", "url": "file:///etc/passwd"},
    {"action": "new", "url": "https://secret:password@example.test/"},
])
def test_invalid_navigation_never_dispatches(env, fields):
    with env.client.websocket_connect(env.url, headers=ORIGIN) as control:
        control.receive_json()
        control.send_json({"type": "navigation", **fields})
        assert control.receive_json()["error"]["code"] == "browser.invalidTarget"
        assert not any(op == "integration.browser.navigation" for op, _ in env.calls)


def test_navigation_observation_publishes_changes_without_command_ack(env, monkeypatch):
    monkeypatch.setattr(execution, "ACCESS_INTERVAL", 0.05)
    with env.client.websocket_connect(env.url, headers=ORIGIN) as control:
        control.receive_json()
        control.send_json({"type": "navigation", "action": "state"})
        assert control.receive_json()["completed"]
        env.navigation["tabs"][0]["title"] = "After redirect"
        response = control.receive_json()
        assert response["type"] == "navigation" and response["completed"] is False
        assert response["tabs"][0]["title"] == "After redirect"


def test_navigation_error_does_not_end_healthy_browser(env):
    async def reply(operation, _arguments):
        if operation == "integration.browser.navigation":
            return {**env.navigation, "activeTabId": str(uuid4())}

    env.reply = reply
    with env.client.websocket_connect(env.url, headers=ORIGIN) as control:
        control.receive_json()
        control.send_json({"type": "navigation", "action": "state"})
        response = control.receive_json()
        assert response["operation"] == "navigation" and response["error"]["code"] == "executionAgent.protocolError"
        assert not env.coordinator.connections[(env.session.id, env.agent.id)].closed


@pytest.mark.parametrize("suffix,method", [("clipboard", "PUT"), ("clipboard/copy", "POST"),
                                          ("downloads", "GET"), ("upload-source", "POST"),
                                          ("upload/{file_id}/choose", "POST"), ("download/{file_id}", "GET")])
def test_all_manual_http_routes_require_view_id_before_rpc(env, suffix, method):
    with env.client.websocket_connect(env.url, headers=ORIGIN) as control:
        state = control.receive_json()
        before = list(env.calls)
        kwargs = {"json": {"source": {"scope": "session", "path": "sample.txt"}}} if suffix == "upload-source" else {}
        if method == "PUT":
            kwargs = {"json": {"text": "private clipboard"}}
        response = env.client.request(method, file_url(env, state, suffix.format(file_id=env.file_id)).split("&viewId=")[0], **kwargs)
        assert response.status_code == 422 and response.json()["error"]["code"].startswith("validation.")
        assert "private clipboard" not in response.text and env.calls == before


@pytest.mark.parametrize("change", ["expired", "global-disabled"])
def test_view_periodic_access_check_revokes_only_requester(env, monkeypatch, change):
    monkeypatch.setattr(routes, "ACCESS_INTERVAL", 0.01)
    session_b = env.session
    login_b = LoginSession(id=uuid4(), user_id=env.user.id, expires_at=env.login.expires_at)
    env.objects[LoginSession, login_b.id] = login_b
    with env.client.websocket_connect(env.url, headers=ORIGIN) as control:
        control.receive_json()
        old = env.coordinator.connections[(env.session.id, env.agent.id)]
        monkeypatch.setattr(routes, "_browser_login", AsyncMock(return_value=_BrowserLogin(env.user, login_b.id, login_b.expires_at)))
        with env.client.websocket_connect(env.url.replace(str(env.session.id), str(session_b.id)), headers=ORIGIN) as candidate:
            assert candidate.receive_json()["type"] == "state"
            if change == "expired":
                login_b.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            else:
                env.documents[IntegrationSettings, None].adapter_availability["browser"] = "disabled"
            assert candidate.receive_json()["error"]["code"] == ("auth.sessionExpired" if change == "expired" else "browser.unavailable")
            assert candidate.receive()["type"] == "websocket.close"
        assert env.coordinator.connections[(env.session.id, env.agent.id)] is old and not old.closed
        assert [op for op, _ in env.calls] == ["integration.browser.open", "integration.browser.control"]


@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("operation", ["copy", "upload-begin", "upload", "choose", "download", "download-later"])
def test_native_helpers_drain_before_close_and_never_send_wire_cancel(env, wire, operation, cancelled, session_upload):
    async def scenario():
        started, finish = asyncio.Event(), asyncio.Event()
        old = await env.coordinator.open(env.user.id, env.instance.id, env.session.id, env.login.id, AsyncMock())

        async def reply(op, arguments):
            target = (op == "integration.browser.clipboard" if operation == "copy" else
                      op == "integration.browser.file" and arguments["action"] ==
                      ("begin" if operation == "upload-begin" else "write" if operation == "upload" else
                       "choose" if operation == "choose" else "read"))
            if target and (operation != "download-later" or arguments["offset"] == 1):
                started.set()
                await finish.wait()
            if op == "integration.browser.file" and arguments["action"] == "read":
                offset = arguments["offset"]
                return {"fileId": env.file_id, "filename": "sample.txt", "size": 2,
                        "offset": offset + 1, "complete": offset == 1, "data": "eA=="}

        env.reply = reply
        async def operation_task():
            if operation == "copy":
                return await routes._clipboard(old)
            if operation in {"upload", "upload-begin"}:
                return await routes.browser_upload_source(old, routes.BrowserUploadRequest(
                    source={"scope": "session", "path": "sample.txt"},
                ))
            if operation == "choose":
                return await routes.browser_choose(UUID(env.file_id), old)
            response = await routes.browser_download(UUID(env.file_id), old)
            if operation == "download-later":
                assert await anext(response.body_iterator) == b"x"
                return await anext(response.body_iterator)
            return response

        active = asyncio.create_task(operation_task())
        await asyncio.wait_for(started.wait(), 1)
        if cancelled:
            active.cancel()
        await asyncio.sleep(0)
        closing = asyncio.create_task(env.coordinator.close(old))
        try:
            async with asyncio.timeout(1):
                while not old.closed:
                    await asyncio.sleep(0)
            assert not active.done() and not closing.done() and old.action_lock.locked()
            assert [args["manual"] for op, args in env.calls if op == "integration.browser.control"] == [True]
            assert not any(message["type"] == "cancel" for message in wire)
        finally:
            finish.set()
        with pytest.raises(asyncio.CancelledError if cancelled else (routes.ApiError, ExecutionClientError)) as error:
            await asyncio.wait_for(active, 2)
        if not cancelled:
            assert error.value.code == "browser.notConnected"
        await asyncio.wait_for(closing, 2)
        assert old.closed and not old.action_lock.locked()
        assert not any(message["type"] == "cancel" for message in wire)
        if operation in {"upload", "upload-begin"}:
            assert session_upload.closed
        if operation in {"upload", "upload-begin"}:
            expected = ["begin", "write", "delete"] if operation == "upload" else ["begin", "delete"]
            assert [args["action"] for op, args in env.calls if op == "integration.browser.file"] == expected
            assert next(i for i, (op, args) in enumerate(env.calls) if args.get("action") == "delete") < next(
                i for i, (op, args) in enumerate(env.calls)
                if op == "integration.browser.close"
            )

    env.client.portal.call(scenario)


@pytest.mark.parametrize("operation", ["copy", "upload", "choose", "download", "downloads", "resize", "navigation"])
def test_queued_manual_operations_fail_after_close_without_dispatch(env, operation, session_upload):
    async def scenario():
        old = await env.coordinator.open(env.user.id, env.instance.id, env.session.id, env.login.id, AsyncMock())
        entered = asyncio.Event()

        async def queued():
            entered.set()
            if operation == "copy":
                return await routes._clipboard(old)
            if operation == "upload":
                return await routes.browser_upload_source(old, routes.BrowserUploadRequest(
                    source={"scope": "session", "path": "sample.txt"},
                ))
            if operation == "choose":
                return await routes.browser_choose(UUID(env.file_id), old)
            if operation == "download":
                return await routes.browser_download(UUID(env.file_id), old)
            if operation == "downloads":
                return await routes.browser_downloads(old)
            if operation == "resize":
                return await env.coordinator.resize(old, 800, 600)
            return await env.coordinator.navigate(old, routes.BrowserNavigationArguments(action="new"))

        await old.action_lock.acquire()
        pending = asyncio.create_task(queued())
        await asyncio.wait_for(entered.wait(), 1)
        closing = asyncio.create_task(env.coordinator.close(old))
        try:
            async with asyncio.timeout(1):
                while not old.closed:
                    await asyncio.sleep(0)
        finally:
            old.action_lock.release()
        with pytest.raises((routes.ApiError, ExecutionClientError)) as error:
            await asyncio.wait_for(pending, 1)
        assert error.value.code == "browser.notConnected"
        await asyncio.wait_for(closing, 2)
        assert [op for op, _ in env.calls] == [
            "integration.browser.open", "integration.browser.control",
            "integration.browser.close",
        ]
        if operation == "upload":
            assert session_upload.closed

    env.client.portal.call(scenario)


def test_close_drains_cancelled_metadata_watch_without_publishing_navigation(env, wire, monkeypatch):
    monkeypatch.setattr(execution, "ACCESS_INTERVAL", 0.01)

    async def scenario():
        started, finish = asyncio.Event(), asyncio.Event()
        socket = AsyncMock()
        old = await env.coordinator.open(env.user.id, env.instance.id, env.session.id, env.login.id, socket)
        old.navigation = execution.BrowserNavigationResult.model_validate(env.navigation)
        old_watch = old.task

        async def reply(operation, _arguments):
            if operation == "integration.browser.navigation":
                started.set()
                await finish.wait()
                return {**env.navigation, "loading": True}

        env.reply = reply
        await asyncio.wait_for(started.wait(), 1)
        closing = asyncio.create_task(env.coordinator.close(old))
        try:
            async with asyncio.timeout(1):
                while not old.closed:
                    await asyncio.sleep(0)
            assert not old_watch.done() and old.action_lock.locked() and not closing.done()
            assert not any(message["type"] == "cancel" for message in wire)
        finally:
            finish.set()
        await asyncio.wait_for(closing, 2)
        assert old_watch.cancelled() and old.closed
        assert not any(call.args[0]["type"] == "navigation" for call in socket.send_json.await_args_list)
        assert not any(message["type"] == "cancel" for message in wire)

    env.client.portal.call(scenario)


def test_old_access_check_rechecks_closed_binding_after_await(env, monkeypatch):
    async def scenario():
        started, finish = asyncio.Event(), asyncio.Event()
        old = await env.coordinator.open(env.user.id, env.instance.id, env.session.id, env.login.id, AsyncMock())
        validate = execution.validate_browser_access

        async def delayed(*args, **kwargs):
            result = await validate(*args, **kwargs)
            if asyncio.current_task() is checking:
                started.set()
                await finish.wait()
            return result

        monkeypatch.setattr(execution, "validate_browser_access", delayed)
        checking = asyncio.create_task(old.check_access())
        await asyncio.wait_for(started.wait(), 1)
        try:
            await env.coordinator.close(old)
        finally:
            finish.set()
        with pytest.raises(ExecutionClientError, match="browser.bindingChanged"):
            await asyncio.wait_for(checking, 1)
        assert old.closed

    env.client.portal.call(scenario)


def test_setup_start_remains_independent_of_views_and_browser_close(env, wire):
    started, finish = asyncio.Event(), asyncio.Event()

    async def reply(operation, _arguments):
        if operation == "integration.browser.setup":
            started.set()
            await finish.wait()

    env.reply = reply
    session_b = env.session
    with env.client.websocket_connect(env.setup_url, headers=ORIGIN) as setup:
        setup.send_json({"type": "start"})
        env.client.portal.call(asyncio.wait_for, started.wait(), 1)
        installer = routes._setup_starts[env.agent.id]
        try:
            with env.client.websocket_connect(env.url, headers=ORIGIN) as a:
                state_a = a.receive_json()
                with env.client.websocket_connect(env.url.replace(str(env.session.id), str(session_b.id)), headers=ORIGIN) as b:
                    assert b.receive_json()["browserId"] == state_a["browserId"]
                    b.send_json({"type": "close"})
                    assert b.receive()["type"] == "websocket.close"
            assert not installer.done() and not env.coordinator.connections
            assert env.agent.id in routes._setup_viewers
            assert not any(message["type"] == "cancel" for message in wire)
            assert sum(op == "integration.browser.setup" for op, _ in env.calls) == 1
            assert sum(op == "integration.browser.open" for op, _ in env.calls) == 1
        finally:
            env.client.portal.call(finish.set)
        assert setup.receive_json()["state"] == "installing"
        env.client.portal.call(installer.result)
        assert not routes._setup_starts
    assert env.agent.id not in routes._setup_viewers
