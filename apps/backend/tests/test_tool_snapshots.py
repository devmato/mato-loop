import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend.agent.context import ContextManager
from mato_loop_backend.agent.projection import _projection_messages
from mato_loop_backend.agent.tool_round import persist_tool_result
from mato_loop_backend.integrations.registry import integration_adapters
from mato_loop_backend.models import AgentRun, ChatSession, ContextSummary, Message
from mato_loop_backend.permissions.policy import PermissionDecision
from mato_loop_backend.providers.base import ToolCall
from mato_loop_backend.settings import (
    AgentRuntimeSettings,
    InstanceLimits,
    IntegrationInstance,
    IntegrationSettings,
    ToolExecutionPolicy,
    ToolSettings,
    TrustLevel,
)
from mato_loop_backend.settings.models import AgentProfilePermissions, ModelConfig
from mato_loop_backend.tools.catalog import tool_registry
from mato_loop_backend.tools.context import ToolContext
from mato_loop_backend.tools.snapshots import (
    MAX_SNAPSHOT_CHARACTERS,
    SNAPSHOT_BEGIN,
    SNAPSHOT_MARKER,
    ToolSnapshotState,
    collect_tool_snapshots,
    snapshot_context,
    snapshot_revision,
)

NOW = datetime(2026, 9, 28, 12, tzinfo=UTC)


def _tools(settings=None):
    integrations = IntegrationSettings(instances=[IntegrationInstance(
        name="Home", integration_type="infrastructure", adapter_type="arcane",
        config={"base_url": "https://arcane.example.test"},
    )])
    return integrations, tool_registry.effective(integrations, tool_settings=settings)


def _record(session_id, tool, settings, *, observed_at=NOW, items=None, integration_id="one"):
    return Message(
        id=uuid4(), session_id=session_id, role="tool", tool_call_id=str(uuid4()),
        content=json.dumps({"data": items if items is not None else [
            {"id": "0", "name": "Production", "connected": True, "status": "online"},
        ]}),
        provider_data={"_matoloop": {
            "toolName": tool.definition.name,
            "toolSnapshot": ToolSnapshotState(
                arguments={"integration_id": integration_id},
                revision=snapshot_revision(tool, settings), observed_at=observed_at,
            ).model_dump(mode="json"),
        }},
    )


def test_only_the_ten_discovery_tools_opt_in():
    policies = {
        tool.name: tool_registry._contributed_definition(tool).snapshot_policy
        for adapter in integration_adapters()
        for tool in (*adapter.shared_tools, *adapter.execution_tools)
        if tool.snapshot_policy is not None
    }
    assert set(policies) == {
        "arcane_accounts", "arcane_environments", "mealie_list_accounts",
        "mail_list_accounts", "mail_list_folders", "knowledge_list_accounts",
        "calendar_list_accounts", "calendar_list_calendars", "tasks_list_accounts", "tasks_list_lists",
    }
    assert all(policy.max_age_seconds is None for name, policy in policies.items() if "accounts" in name)


async def test_authorized_result_is_persisted_and_reused_after_compaction(monkeypatch):
    settings = ToolSettings()
    integrations, toolset = _tools(settings)
    session = ChatSession(id=uuid4(), user_id=uuid4())
    run = AgentRun(id=uuid4(), session_id=session.id)
    db = AsyncMock(spec=AsyncSession)
    monkeypatch.setattr(ToolContext, "current_integrations", AsyncMock(return_value=integrations))
    monkeypatch.setattr(
        "mato_loop_backend.tools.access.profile_tool_settings", AsyncMock(return_value=settings),
    )
    call = ToolCall(id="accounts", name="arcane_accounts", arguments={})
    context = ToolContext(
        db=db, run_id=run.id, session=session, tool_call=call, root_files=None,
        integration_settings=integrations, tool_settings=settings,
        ask_handler=AsyncMock(return_value=PermissionDecision.ALLOW),
        model_trust_level=TrustLevel.PRIVATE,
    )
    result = await toolset.execute(call, context)
    assert not result.is_error
    record, _, _ = await persist_tool_result(
        db, run, session, call, content=result.content, metadata=result.metadata,
        attachments=(), file_actions=(), trust_level=TrustLevel.PRIVATE,
        ephemeral_output=False, limits=InstanceLimits(), attachment_service=Mock(),
    )
    record.id = uuid4()  # The database assigns this in production.
    user = Message(id=uuid4(), session_id=session.id, role="user", content="Use Home.")
    records = [record, user]
    snapshots = collect_tool_snapshots(records, toolset.tools, settings, session.id)
    assert snapshots[0]["arguments"] == {}
    assert snapshots[0]["data"] == [{"integrationId": str(integrations.instances[0].id), "name": "Home"}]
    assert snapshots[0]["expiresAt"] is None
    assert "base_url" not in json.dumps(record.provider_data)

    db.scalar.return_value = ContextSummary(
        id=uuid4(), session_id=session.id, covered_through_message_id=record.id,
        content="Earlier discovery.",
    )

    async def estimate(messages):
        return len(json.dumps(messages)) // 4

    projection = await ContextManager.build(
        db, chat_session=session, records=records, system_prompt="Host rules.", tool_schemas=[],
        model=ModelConfig(provider_id=uuid4(), model_id="test", context_window_tokens=32_000),
        settings=AgentRuntimeSettings(), summarize=AsyncMock(), estimate_request=estimate,
        tool_snapshots=snapshots,
    )
    pinned = [item for item in projection.messages if item["content"].startswith(SNAPSHOT_BEGIN)]
    assert len(pinned) == 1 and '"name":"Home"' in pinned[0]["content"]
    assert SNAPSHOT_MARKER not in pinned[0]
    context.ask_handler.assert_awaited_once()

    context.ask_handler.return_value = PermissionDecision.DENY
    denied = await toolset.execute(call, context)
    assert denied.is_error and "toolSnapshot" not in denied.metadata


def test_snapshots_keep_latest_per_scope_and_expire_without_refreshing_time():
    settings = ToolSettings()
    _, toolset = _tools(settings)
    tool = toolset.tools["arcane_environments"]
    session_id = uuid4()
    first = _record(session_id, tool, settings, observed_at=NOW - timedelta(minutes=10))
    latest = _record(session_id, tool, settings)
    other = _record(session_id, tool, settings, integration_id="two")
    failure = Message(
        id=uuid4(), session_id=session_id, role="tool", content='{"error":{}}',
        provider_data={"_matoloop": {"toolName": tool.definition.name, "errorCode": "integration.unavailable"}},
    )
    records = [first, latest, other, failure]
    snapshots = collect_tool_snapshots(records, toolset.tools, settings, session_id, now=NOW + timedelta(minutes=29))
    assert {item["messageId"] for item in snapshots} == {str(latest.id), str(other.id)}
    assert all(item["data"] == [{"id": "0", "name": "Production"}] for item in snapshots)
    assert all(item["observedAt"] == NOW.isoformat() for item in snapshots)
    assert all(item["expiresAt"] == (NOW + timedelta(minutes=30)).isoformat() for item in snapshots)
    assert collect_tool_snapshots(records, toolset.tools, settings, session_id, now=NOW + timedelta(minutes=30)) == []


@pytest.mark.parametrize("change", ["binding", "policy", "profile_policy", "unavailable", "session", "trust"])
def test_changed_binding_or_access_does_not_reinject_snapshot(change):
    settings = ToolSettings()
    _, toolset = _tools(settings)
    tool = toolset.tools["arcane_environments"]
    session_id = uuid4()
    record = _record(session_id, tool, settings)
    if change == "binding":
        toolset.tools[tool.definition.name] = replace(tool, integration_revisions=(("changed", "revision"),))
    elif change == "policy":
        settings.private_permissions.policies["arcane.read"] = ToolExecutionPolicy.ASK
    elif change == "profile_policy":
        settings._agent_permissions = AgentProfilePermissions(policies={"arcane.read": ToolExecutionPolicy.DISABLED})
    elif change == "unavailable":
        toolset.tools.clear()
    elif change == "session":
        session_id = uuid4()
    else:
        settings.select_trust(TrustLevel.PUBLIC)
    assert collect_tool_snapshots([record], toolset.tools, settings, session_id, now=NOW) == []


def test_oversized_latest_result_is_not_truncated_or_replaced_with_older_result():
    settings = ToolSettings()
    _, toolset = _tools(settings)
    tool = toolset.tools["arcane_environments"]
    session_id = uuid4()
    old = _record(session_id, tool, settings)
    large = _record(session_id, tool, settings, items=[{"id": "0", "name": "x" * MAX_SNAPSHOT_CHARACTERS}])
    assert collect_tool_snapshots([old, large], toolset.tools, settings, session_id, now=NOW) == []


def test_current_result_is_not_duplicated_and_pin_budget_preserves_whole_entries():
    settings = ToolSettings()
    _, toolset = _tools(settings)
    tool = toolset.tools["arcane_environments"]
    session_id = uuid4()
    user = Message(id=uuid4(), session_id=session_id, role="user", content="List environments.")
    record = _record(session_id, tool, settings)
    snapshots = collect_tool_snapshots([record], toolset.tools, settings, session_id, now=NOW)
    call = Message(
        id=uuid4(), session_id=session_id, role="assistant", content="",
        tool_calls=[{"id": record.tool_call_id, "name": tool.definition.name, "arguments": {"integration_id": "one"}}],
    )
    messages = _projection_messages("Host rules.", None, [user, call, record], TrustLevel.PRIVATE, tool_snapshots=snapshots)
    assert not any(message.get(SNAPSHOT_MARKER) for message in messages)
    assert any(message["role"] == "tool" and "Production" in message["content"] for message in messages)
    followup = Message(id=uuid4(), session_id=session_id, role="user", content="Next request")
    assert not any(message.get(SNAPSHOT_MARKER) for message in _projection_messages(
        "Host rules.", None, [user, call, record, followup], TrustLevel.PRIVATE, tool_snapshots=snapshots,
    ))
    single = snapshot_context(snapshots, current_message_ids=set(), max_characters=8_000)
    assert single is not None
    budget = len(single["content"])
    assert snapshot_context(snapshots, current_message_ids=set(), max_characters=budget - 1) is None
    extra = {**snapshots[0], "messageId": str(uuid4())}
    assert snapshot_context([*snapshots, extra], current_message_ids=set(), max_characters=budget) == single
