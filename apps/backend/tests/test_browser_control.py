import asyncio
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

from browser_helpers import ORIGIN, open_view, other_session
from browser_helpers import env as env

import mato_loop_backend.tools.access as tools_access
from mato_loop_backend.execution import browsers as execution
from mato_loop_backend.integrations.registry import effective_execution_integration_settings
from mato_loop_backend.models import AgentRun
from mato_loop_backend.permissions.policy import PermissionDecision
from mato_loop_backend.providers.base import ToolCall
from mato_loop_backend.settings import IntegrationSettings, ToolSettings
from mato_loop_backend.tools.catalog import ToolRegistry
from mato_loop_backend.tools.context import ToolContext


def test_browser_tool_starts_without_app_or_viewer(env, monkeypatch):
    env.session.control_center = {"tabs": []}
    run = AgentRun(id=uuid4(), session_id=env.session.id, status="running",
                   execution_agent_id=env.agent.id,
                   execution_operations=["integration.browser.execute"])
    env.objects[AgentRun, run.id] = run
    integrations = effective_execution_integration_settings(
        IntegrationSettings(instances=[env.instance]), env.agent.id, env.operations,
    )
    monkeypatch.setattr(execution, "browser_coordinator", env.coordinator)
    monkeypatch.setattr(ToolContext, "current_integrations", AsyncMock(return_value=integrations))
    monkeypatch.setattr(tools_access, "profile_tool_settings", AsyncMock(return_value=ToolSettings()))
    call = ToolCall("tabs", "browser_tabs", {"action": "list"})
    context = ToolContext(
        db=AsyncMock(), run_id=run.id, session=env.session, tool_call=call,
        root_files=Mock(), integration_settings=integrations,
        execution_client=execution.browser_client(env.agent.id),
        ask_handler=AsyncMock(return_value=PermissionDecision.ALLOW),
    )
    context.db.get.return_value = run
    registry = ToolRegistry([]).effective(integrations)
    result = env.client.portal.call(registry.execute, call, context)
    assert not result.is_error, result.content
    assert any(tab["app_id"] == "browser" for tab in env.session.control_center["tabs"])
    connection = env.coordinator.current_for(env.session.id, env.agent.id)
    assert connection is not None and not connection.sockets
    with env.client.websocket_connect(env.url, headers=ORIGIN) as socket:
        state = socket.receive_json()
        with open_view(env, state):
            assert state["browserId"] == str(connection.browser_id)
    assert env.coordinator.current_for(env.session.id, env.agent.id) is connection


def test_concurrent_starts_share_one_runtime_only_within_the_same_session(env):
    async def scenario():
        session_b = other_session(env)
        connections = await asyncio.gather(
            env.coordinator.open(env.user.id, env.instance.id, env.session.id),
            env.coordinator.open(env.user.id, env.instance.id, env.session.id),
            env.coordinator.open(env.user.id, env.instance.id, session_b.id),
        )
        assert connections[0] is connections[1] and connections[0] is not connections[2]
        assert len({item.browser_id for item in connections}) == 2
        assert sum(op == "integration.browser.open" for op, _ in env.calls) == 2

    env.client.portal.call(scenario)
