import asyncio
import json
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import mato_loop_backend.agent.loop as mock_agent_loop
import mato_loop_backend.agent.run_support as mock_agent_run_support
import mato_loop_backend.agent.runtime as mock_agent_runtime
import mato_loop_backend.agent.summarization as mock_agent_summarization
import mato_loop_backend.agent.titles as mock_agent_titles
from mato_loop_backend.agent import subagents
from mato_loop_backend.agent.projection import (
    _model_provider_file_roots,
    _model_tool_calls,
    _projection_messages,
)
from mato_loop_backend.agent.run_contracts import AgentRunError
from mato_loop_backend.agent.run_support import (
    RUN_PHASES,
    _persist_edited_tool_arguments,
    _public_reasoning_payload,
    _public_run_error,
    _publish_run_phase,
    _safe_provider_data,
    _safe_tool_arguments,
)
from mato_loop_backend.agent.run_timing import run_timing_payload, transition_run_status
from mato_loop_backend.agent.runtime import AgentRuntime
from mato_loop_backend.agent.session_settings import RunSessionSettings
from mato_loop_backend.events import RunEventBroker, run_events
from mato_loop_backend.files.service import RootFileService
from mato_loop_backend.files.storage import SessionStorage
from mato_loop_backend.media.attachments import AttachmentInput
from mato_loop_backend.models import (
    AgentRun,
    ApprovalRequest,
    ChatSession,
    Message,
    QuestionRequest,
    TitleGenerationStatus,
)
from mato_loop_backend.permissions.policy import PermissionDecision, PermissionRequest
from mato_loop_backend.providers.base import (
    ProviderError,
    ProviderResult,
    PublicReasoning,
    ToolCall,
)
from mato_loop_backend.sessions.serialization import message_payload
from mato_loop_backend.settings import (
    AgentRuntimeSettings,
    AiSettings,
    IntegrationSettings,
    SettingsStore,
    ToolExecutionPolicy,
    ToolSettings,
    TrustLevel,
)
from mato_loop_backend.settings.models import (
    BUILD_PROFILE_ID,
    LlmProviderConfig,
    ModelConfig,
    build_profile,
)
from mato_loop_backend.tools.arguments import QuestionArguments, TaskArguments


@pytest.fixture(autouse=True)
def prepared_execution(monkeypatch):
    # These tests isolate event delivery; execution prerequisites have their own coverage.
    monkeypatch.setattr(AgentRuntime, "_prepare_run", AsyncMock())


def test_run_timing_sums_active_segments_and_excludes_user_waits() -> None:
    started_at = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    run = AgentRun(id=uuid4(), session_id=uuid4(), status="queued")

    transition_run_status(run, "running", now=started_at)
    transition_run_status(run, "waiting_approval", now=started_at + timedelta(seconds=2))
    transition_run_status(run, "running", now=started_at + timedelta(hours=2))
    assert run_timing_payload(
        run, now=started_at + timedelta(hours=2, seconds=1)
    ) == {
        "activeDurationMs": 3_000,
        "activeDurationRunning": True,
        "activeTimingRevision": 3,
    }
    transition_run_status(run, "waiting_input", now=started_at + timedelta(hours=2, seconds=3))
    transition_run_status(run, "running", now=started_at + timedelta(hours=3))
    transition_run_status(run, "completed", now=started_at + timedelta(hours=3, seconds=4))

    assert run.active_duration_ms == 9_000
    assert run.active_started_at is None
    assert run.active_timing_revision == 6


async def test_run_duration_is_merged_into_result_message_metadata() -> None:
    message = Message(
        id=uuid4(),
        session_id=uuid4(),
        role="assistant",
        provider_data={
            "provider": "private",
            "_matoloop": {"performance": {"providerDurationMs": 400}},
        },
    )
    run = AgentRun(
        id=uuid4(),
        session_id=message.session_id,
        result_message_id=message.id,
        active_duration_ms=9_000,
    )
    db = AsyncMock(spec=AsyncSession)
    db.get.return_value = message

    await AgentRuntime._persist_run_duration(db, run)

    assert message.provider_data == {
        "provider": "private",
        "_matoloop": {
            "performance": {"providerDurationMs": 400, "activeDurationMs": 9_000}
        },
    }


async def test_run_phase_event_has_stable_language_neutral_payload(monkeypatch) -> None:
    published = []

    async def publish(run_id, event) -> None:
        published.append((run_id, event))

    monkeypatch.setattr(run_events, "publish", publish)
    run_id = uuid4()

    await _publish_run_phase(
        run_id,
        "processing_tool",
        tool="read_file",
        toolCallId="call-1",
        index=0,
        total=2,
        toolDisplay={
            "group": "filesystem", "groupLabel": "Dateisystem", "icon": "folder",
            "emoji": "📁", "label": "Pfad lesen", "summary": "notes.txt",
        },
    )

    started_at = published[0][1].pop("startedAt")
    assert isinstance(started_at, str)
    assert started_at.endswith("+00:00")
    assert published == [
        (
            run_id,
                {
                    "type": "run.phase",
                    "phase": "processing_tool",
                    "activityLabel": "Pfad lesen",
                "tool": "read_file",
                "toolDisplay": {
                    "group": "filesystem", "groupLabel": "Dateisystem", "icon": "folder",
                    "emoji": "📁", "label": "Pfad lesen", "summary": "notes.txt",
                },
                "toolCallId": "call-1",
                "index": 0,
                "total": 2,
            },
        )
    ]
    assert RUN_PHASES == {
        "preparing_context",
        "compacting_context",
        "waiting_model",
        "processing_tool",
        "finalizing",
    }


async def test_unknown_run_phase_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown run phase"):
        await _publish_run_phase(uuid4(), "thinking_text")


async def test_runtime_rejects_inactive_user() -> None:
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = False

    with pytest.raises(AgentRunError, match="auth.accountDisabled"):
        await AgentRuntime._ensure_user_active(db, uuid4())


def test_canonical_plan_needs_no_runtime_directory(tmp_path) -> None:
    session_id = uuid4()
    storage = SessionStorage(tmp_path / "sessions")
    storage.provision(session_id)
    root_files = RootFileService(session_id, session_storage=storage)

    assert root_files.run_id is None

    path = "plan.md"
    run_id = uuid4()
    working = RootFileService(session_id, session_storage=storage, run_id=run_id)
    assert working.write_file("session", path, "# Plan") == path
    assert root_files.list_revisions(path) == ()
    working.finalize_run(run_id)
    assert root_files.read_bytes("session", path, limit=100) == b"# Plan"


@pytest.fixture
def delegated_runtime(monkeypatch):
    profile = build_profile()
    session = ChatSession(id=uuid4(), user_id=uuid4(), title="Main", reasoning_effort="high",
                           agent_profile_id=BUILD_PROFILE_ID, web_search_policy="required")
    user = Message(id=uuid4(), session_id=session.id, role="user", content="Work",
                   provider_data={"_matoloop": {"activeFile": {"dirty": True, "path": "/work/code.py"}}})
    parent = AgentRun(
        id=uuid4(), session_id=session.id, input_message_id=user.id, status="running",
        model_config_id=uuid4(), model_name="Model", agent_profile_id=BUILD_PROFILE_ID,
        trust_level="public", pseudonymization_enabled=True, execution_agent_id=uuid4(),
        execution_agent_revision=3, current_directory_snapshot="/work", execution_operations=["shell.run"],
    )
    objects = {(type(item), item.id): item for item in (session, user, parent)}
    databases = []

    def add(item):
        if item.id is None:
            item.id = uuid4()
        item.created_at = datetime.now(UTC)
        if isinstance(item, (ApprovalRequest, QuestionRequest)):
            item.status = "pending"
        objects[type(item), item.id] = item

    def factory():
        db = AsyncMock(spec=AsyncSession)
        db.__aenter__.return_value = db
        db.get.side_effect = lambda model, key, **kw: objects.get((model, key))
        db.add.side_effect = add
        db.add_all.side_effect = lambda items: [add(item) for item in items]
        db.scalar.return_value = None
        async def scalars(statement):
            session_id = statement.compile().params["session_id_1"]
            tools = [item for (model, _key), item in objects.items()
                     if model is Message and item.session_id == session_id and item.role == "tool"
                     and (item.provider_data or {}).get("_matoloop", {}).get("errorCode") not in {
                         "agent.toolCallLimitReached", "agent.outputLimitReached", "agent.repeatedToolCall", "tool.skippedAfterUserDenial",
                     }]
            return Mock(all=lambda: list(reversed(tools))[:6])
        db.scalars.side_effect = scalars
        databases.append(db)
        return db

    db = factory()
    db.scalar.return_value = 0
    monkeypatch.setattr(subagents, "AsyncSessionFactory", factory)
    monkeypatch.setattr(mock_agent_runtime.NotificationService, "deliver_message_after_commit", AsyncMock())
    current_tools = ToolSettings().select_trust(TrustLevel.PUBLIC)
    current_tools._agent_profile = profile
    monkeypatch.setattr(
        subagents, "load_agent_profile_tools",
        AsyncMock(return_value=(current_tools, profile)),
    )
    monkeypatch.setattr(subagents, "load_effective_integrations", AsyncMock(return_value=mock_agent_run_support.IntegrationSettings()))
    _mock_value = factory
    for _mock_target in (mock_agent_runtime, mock_agent_summarization, mock_agent_titles,):
        monkeypatch.setattr(_mock_target, 'AsyncSessionFactory', _mock_value)
    monkeypatch.setattr(SessionStorage, "provision", Mock())
    monkeypatch.setattr(run_events, "register_subagent", Mock(), raising=False)
    monkeypatch.setattr(run_events, "unregister_subagent", Mock(), raising=False)
    monkeypatch.setattr(run_events, "publish", AsyncMock())
    for name in ("_transcribe_voice_messages", "_finalize_run_files", "_persist_run_duration",
                 "_terminalize_incomplete_tool_calls"):
        monkeypatch.setattr(AgentRuntime, name, AsyncMock())

    async def delegate(max_tool_calls=None, *, tool_settings=None, integration_settings=None, tool_names=None):
        selected_tools = tool_settings or ToolSettings().select_trust(TrustLevel.PUBLIC)
        selected_tools._agent_profile = profile
        return await subagents.run_tasks(
            AgentRuntime, db, parent, session,
            TaskArguments(tasks=[{"description": f"Task {i}", "prompt": f"Translate text {i}"} for i in range(2)]),
            tool_call_id="task-1", tool_settings=selected_tools,
            tool_names=tool_names or {"task", "question", "write_file", "web_search"},
            integration_settings=integration_settings,
            max_tool_calls=max_tool_calls,
        )

    return session, parent, objects, databases, delegate


@pytest.mark.parametrize("partial_failure", [False, True])
async def test_delegated_children_parallel_normal_lifecycle(monkeypatch, delegated_runtime, partial_failure):
    session, parent, objects, databases, delegate = delegated_runtime
    barrier = asyncio.Barrier(2)
    loop_databases = []

    async def execute(db, run, _visible):
        assert databases[0].commit.await_count == 1
        assert databases[1].commit.await_count == 1
        loop_databases.append(db)
        await asyncio.wait_for(barrier.wait(), timeout=1)
        child_session = objects[ChatSession, run.session_id]
        if partial_failure and child_session.spawn_index == 1:
            raise AgentRunError("agent.emptyResponse")
        message = Message(id=uuid4(), session_id=run.session_id, role="assistant", content="x" * 12_001)
        db.add(message)
        run.result_message_id = message.id

    monkeypatch.setattr(AgentRuntime, "_execute_loop", execute)
    result = json.loads((await delegate()).content)["tasks"]
    await asyncio.sleep(0)  # Let scheduled task-registry cleanup callbacks run.
    assert result[0]["status"] == "completed"
    assert result[0]["truncated"] is True and len(result[0]["result"]) == 12_000
    assert result[1]["status"] == ("failed" if partial_failure else "completed")
    assert result[1]["errorCode"] == ("agent.emptyResponse" if partial_failure else None)
    assert len(loop_databases) == 2 and loop_databases[0] is not loop_databases[1]
    assert parent.status == "running"
    children = [obj for (model, key), obj in objects.items() if model is AgentRun and key != parent.id]
    for child in children:
        child_session = objects[ChatSession, child.session_id]
        assert child_session.parent_session_id == session.id
        assert child_session.spawned_by_run_id == parent.id
        assert child_session.spawned_by_tool_call_id == "task-1"
        assert child_session.memory_mode == "disabled" and child_session.web_search_policy == "available"
        assert child_session.reasoning_effort == "high"
        assert child.parent_run_id is None and child.session_id != parent.session_id
        assert child.input_message_id is not None and child.result_message_id is not None
        assert child.internal_transcript is None and child.internal_result is None
        for name in ("model_config_id", "model_name", "trust_level", "pseudonymization_enabled",
                     "execution_agent_id", "execution_agent_revision", "current_directory_snapshot", "execution_operations"):
            assert getattr(child, name) == getattr(parent, name)
        metadata = objects[Message, child.input_message_id].provider_data["_matoloop"]
        assert metadata["activeFile"]["dirty"] is True
        assert metadata["maxToolCallsByRun"] == {str(child.id): None}
        assert metadata["delegationCeiling"]["integrationIds"] == []
        assert metadata["delegationCeiling"]["toolNames"] == ["question", "web_search", "write_file"]
        assert child.id not in AgentRuntime._tasks


async def test_delegation_intersects_live_spawn_scope_and_both_permission_ceilings(monkeypatch, delegated_runtime):
    from mato_loop_backend.permissions.resolution import resolve_tool_execution_policy
    from mato_loop_backend.settings import (
        IntegrationInstance,
        IntegrationSettings,
        ToolExecutionPolicy,
    )
    from mato_loop_backend.settings.effective import filter_agent_profile_integrations
    from mato_loop_backend.settings.models import AgentProfile, IntegrationPermissions

    _session, _parent, objects, _databases, delegate = delegated_runtime
    accounts = [IntegrationInstance(
        name=name, integration_type="calendar", adapter_type="caldav",
        config={"base_url": "https://example.org", "username": "user"}, credential_id=uuid4(),
    ) for name in ("A", "B", "New")]
    account_id = accounts[1].id
    offered_integrations = IntegrationSettings(instances=accounts[:2])
    live_integrations = IntegrationSettings(instances=accounts)
    profile = AgentProfile(name="Build", instructions="Follow the task.", integrationsEnabledByDefault=True,
                           delegationEnabled=True)
    accounts[0].profile_permissions[profile.id] = IntegrationPermissions(available=False)
    offered_tools = ToolSettings(public_permissions={
        "policies": {"session.files.write": "ask", "network.fetch": "auto", "calendar.read": "auto"},
        "integration_policies": {str(account_id): {"calendar.read": "auto"}},
        "resource_policies": {str(account_id): {"old": {"calendar.read": "ask"}}},
    }).select_trust(TrustLevel.PUBLIC)
    live_tools = ToolSettings(public_permissions={
        "policies": {"session.files.write": "auto", "network.fetch": "ask", "calendar.read": "auto"},
        "integration_policies": {str(account_id): {"calendar.read": "auto"}},
        "resource_policies": {str(account_id): {"spawn": {"calendar.read": "ask"}}},
    }).select_trust(TrustLevel.PUBLIC)
    live_tools.tool_availability["question"] = False
    monkeypatch.setattr(subagents, "load_agent_profile_tools", AsyncMock(return_value=(live_tools, profile)))
    monkeypatch.setattr(subagents, "load_effective_integrations", AsyncMock(return_value=live_integrations))

    async def execute(db, run, _visible):
        # Reopening settings after persistence must not reopen the child snapshot.
        accounts[0].profile_permissions[profile.id].available = True
        live_tools.tool_availability["question"] = True
        metadata = objects[Message, run.input_message_id].provider_data["_matoloop"]
        snapshot = metadata["delegationCeiling"]
        assert snapshot["integrationIds"] == [str(account_id)]
        assert "question" not in snapshot["toolNames"]
        assert "task" not in snapshot["toolNames"]
        scoped = filter_agent_profile_integrations(live_integrations, profile, allowed_ids=subagents.integration_ceiling_ids(snapshot))
        assert [item.id for item in scoped.instances] == [account_id]
        reopened = ToolSettings(public_permissions={"policies": {
            "session.files.write": "auto", "network.fetch": "auto", "calendar.read": "auto",
        }}).select_trust(TrustLevel.PUBLIC)
        reopened.permissions.integration_policies[str(account_id)] = {"calendar.read": "auto"}
        bounded = subagents.apply_delegation_ceiling(reopened, snapshot)
        for capability in ("session.files.write", "network.fetch"):
            assert resolve_tool_execution_policy(bounded, capability).policy == ToolExecutionPolicy.ASK
        for resource in ("old", "spawn"):
            request = PermissionRequest(capability="calendar.read", resource_type="network_service", resource_scope=resource,
                                        action_kind="read", risk_level="low", title="Read", summary="Read",
                                        policy_integration_id=account_id, policy_resource_key=resource)
            assert mock_agent_run_support._tool_policy(subagents.ceiling_settings(snapshot, TrustLevel.PUBLIC), scoped, request, snapshot=snapshot) == ToolExecutionPolicy.ASK
            assert resolve_tool_execution_policy(bounded, "calendar.read", integration_id=account_id, resource_key=resource).policy == ToolExecutionPolicy.ASK
        assert resolve_tool_execution_policy(bounded, "calendar.read", integration_id=account_id, resource_key="other").policy == ToolExecutionPolicy.AUTO
        message = Message(id=uuid4(), session_id=run.session_id, role="assistant", content="Scoped result")
        db.add(message)
        run.result_message_id = message.id

    monkeypatch.setattr(AgentRuntime, "_execute_loop", execute)
    result = await delegate(tool_settings=offered_tools, integration_settings=offered_integrations,
                            tool_names={"task", "question", "write_file", "webfetch", "calendar_query_events"})
    assert all(task["status"] == "completed" for task in json.loads(result.content)["tasks"])
    subagents.load_agent_profile_tools.assert_awaited_once()
    subagents.load_effective_integrations.assert_awaited_once()


@pytest.mark.parametrize("limited", [False, True])
async def test_child_failed_or_limited_handoff_keeps_bounded_tool_references(monkeypatch, delegated_runtime, limited):
    _session, _parent, objects, _databases, delegate = delegated_runtime

    async def execute(db, run, _visible):
        for index in range(7):
            db.add(Message(
                id=uuid4(), session_id=run.session_id, role="tool", tool_call_id=f"write-{index}",
                content=f"result-{index}:" + "x" * 1_500,
                provider_data={"_matoloop": {"runId": str(run.id), "toolName": "write_file", "fileActions": [{"operation": "write", "root": "session", "path": f"{index}.txt"}]}},
            ))
        db.add(Message(id=uuid4(), session_id=run.session_id, role="tool", content="Rejected batch.",
                       provider_data={"_matoloop": {"errorCode": "agent.outputLimitReached"}}))
        if not limited:
            raise AgentRunError("agent.outputLimitReached")
        message = Message(id=uuid4(), session_id=run.session_id, role="assistant", content="Teilweise erledigt.",
                          provider_data={"_matoloop": {"completionCause": "agent.toolCallLimitReached"}})
        db.add(message)
        run.result_message_id = message.id

    monkeypatch.setattr(AgentRuntime, "_execute_loop", execute)
    results = json.loads((await delegate(max_tool_calls=7)).content)["tasks"]
    for result in results:
        assert result["status"] == ("completed" if limited else "failed")
        assert result["cause"]["code"] == ("agent.toolCallLimitReached" if limited else "agent.outputLimitReached")
        evidence = result["lastToolResults"]
        assert len(evidence) == 6
        assert [item["toolCallId"] for item in evidence] == [f"write-{index}" for index in range(1, 7)]
        assert all(len(item["result"]) == 1_200 and item["truncated"] for item in evidence)
        assert evidence[-1]["fileActions"] == [{"operation": "write", "root": "files", "path": "6.txt"}]
        assert all(str(objects[Message, UUID(item["messageId"])].session_id) == result["sessionId"] for item in evidence)


@pytest.mark.parametrize("during_startup", [False, True])
async def test_delegated_parent_cancellation_joins_children(monkeypatch, delegated_runtime, during_startup):
    _session, parent, objects, databases, delegate = delegated_runtime
    entered = asyncio.Event()
    release = asyncio.Event()

    async def execute(_db, _run, _visible):
        entered.set()
        await asyncio.Event().wait()

    if during_startup:
        factory = subagents.AsyncSessionFactory

        def delayed_factory():
            db = factory()
            if len(databases) == 2:
                async def commit():
                    entered.set()
                    await release.wait()
                db.commit.side_effect = commit
            return db

        monkeypatch.setattr(subagents, "AsyncSessionFactory", delayed_factory)
    monkeypatch.setattr(AgentRuntime, "_execute_loop", execute)
    task = asyncio.create_task(delegate())
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)
    children = [obj for (model, key), obj in objects.items() if model is AgentRun and key != parent.id]
    assert len(children) == 2
    assert all(child.status == "cancelled" and child.id not in AgentRuntime._tasks for child in children)
    assert parent.status == "running"


async def test_delegation_limit_and_recursion_are_host_enforced(delegated_runtime):
    session, _parent, _objects, databases, delegate = delegated_runtime
    databases[0].scalar.return_value = 7
    result = await delegate()
    assert result.is_error
    assert len(databases) == 1
    session.parent_session_id = uuid4()
    assert (await delegate()).is_error
    assert len(databases) == 1


@pytest.mark.parametrize("shutdown", [False, True])
async def test_cancel_before_scheduled_run_starts_persists_terminal_state(monkeypatch, delegated_runtime, shutdown):
    _session, parent, _objects, _databases, _delegate = delegated_runtime
    parent.status = "queued"
    monkeypatch.setattr(AgentRuntime, "_session_settings", {parent.id: RunSessionSettings(
        session_profile={}, memory_mode="disabled", web_search_policy="required",
    )})
    execute = AsyncMock(side_effect=AssertionError("Cancelled work must never execute"))
    monkeypatch.setattr(AgentRuntime, "_execute_loop", execute)
    task = AgentRuntime._schedule_run(parent)
    if shutdown:
        await AgentRuntime.shutdown()
        assert parent.status == "failed" and parent.error == "agent.backendRestarted"
    else:
        assert AgentRuntime.cancel(parent.id)
        assert task.cancelling() == 0
        await asyncio.gather(task, return_exceptions=True)
        assert parent.status == "cancelled"
    assert parent.result_message_id is not None
    assert parent.id not in AgentRuntime._session_settings
    execute.assert_not_awaited()


async def test_cancel_descendants_uses_recursive_sessions_and_never_replays_orphans(monkeypatch):
    root_id = uuid4()
    runs = [AgentRun(id=uuid4(), session_id=uuid4(), status="queued", trust_level="public") for _ in range(2)]
    profile_id = uuid4()
    sessions = {
        run.session_id: ChatSession(id=run.session_id, agent_profile_id=profile_id)
        for run in runs
    }
    read_db, write_db = AsyncMock(spec=AsyncSession), AsyncMock(spec=AsyncSession)
    for db in (read_db, write_db):
        db.__aenter__.return_value = db
    read_db.execute.return_value = Mock(all=lambda: [(run.session_id, None) for run in runs])
    read_db.scalars.return_value = Mock(all=lambda: [run.id for run in runs])
    write_db.get.side_effect = lambda model, key: (
        next(run for run in runs if run.id == key)
        if model is AgentRun
        else sessions[key]
    )
    write_db.add.side_effect = lambda message: setattr(message, "id", uuid4())
    factory = Mock(side_effect=[read_db, write_db])
    _mock_value = factory
    for _mock_target in (mock_agent_runtime, mock_agent_summarization, mock_agent_titles,):
        monkeypatch.setattr(_mock_target, 'AsyncSessionFactory', _mock_value)

    async def cancel(_run_id):
        read_db.__aexit__.assert_awaited_once()
        return False

    monkeypatch.setattr(AgentRuntime, "cancel_and_wait", AsyncMock(side_effect=cancel))
    schedule = Mock(side_effect=AssertionError("Orphans must not be replayed"))
    monkeypatch.setattr(AgentRuntime, "_schedule_run", schedule)
    for name in ("_terminalize_incomplete_tool_calls", "_finalize_run_files", "_persist_run_duration"):
        monkeypatch.setattr(AgentRuntime, name, AsyncMock())
    monkeypatch.setattr(run_events, "publish", AsyncMock())
    await AgentRuntime.cancel_descendants(root_id)
    assert "WITH RECURSIVE" in str(read_db.execute.await_args.args[0])
    assert "agent_runs.parent_run_id IS NULL" in str(read_db.scalars.await_args.args[0])
    assert all(run.status == "cancelled" and run.result_message_id is not None for run in runs)
    assert all(run.agent_profile_id == profile_id for run in runs)
    assert all(
        call.args[0].provider_data["_matoloop"] == {
            "runId": str(run.id), "agentProfileId": str(profile_id),
        }
        for call, run in zip(write_db.add.call_args_list, runs, strict=True)
    )
    assert AgentRuntime.cancel_and_wait.await_count == 2
    schedule.assert_not_called()


@pytest.mark.parametrize("kind", ["approval", "question"])
@pytest.mark.parametrize("direct_message", [False, True])
async def test_delegated_waits_keep_parent_active_and_expire_on_cancel(monkeypatch, delegated_runtime, kind, direct_message):
    session, parent, objects, _databases, delegate = delegated_runtime
    entered = asyncio.Event()
    created = []

    async def publish(_run_id, event):
        if event["type"] == f"{kind}.created":
            created.append(event[kind])
            if len(created) == 2:
                entered.set()

    async def execute(db, run, _visible):
        child = objects[ChatSession, run.session_id]
        call = ToolCall("call", kind, {})
        if kind == "approval":
            await AgentRuntime._request_approval(db, run, child, call, PermissionRequest(
                capability="network.fetch", resource_type="url", resource_scope="https://example.org",
                action_kind="fetch", risk_level="low", title="Read", summary="Read",
            ))
        else:
            await AgentRuntime._request_question(db, run, child, call, QuestionArguments(questions=[{
                "id": "detail", "type": "free_text", "header": "Detail", "question": "Which detail?",
            }]))

    monkeypatch.setattr("mato_loop_backend.notifications.NotificationService.create_for_approval", AsyncMock(return_value=None))
    monkeypatch.setattr("mato_loop_backend.notifications.NotificationService.create_for_question", AsyncMock(return_value=None))
    monkeypatch.setattr(run_events, "publish", publish)
    monkeypatch.setattr(AgentRuntime, "_execute_loop", execute)
    task = asyncio.create_task(delegate())
    await asyncio.wait_for(entered.wait(), timeout=1)
    assert parent.status == "running"
    for event in created:
        assert event["sessionId"] == str(session.id)
        assert event["runId"] != str(parent.id)
        assert event["payload"]["subagentTitle"].startswith("Task ")
        assert event["payload"]["subagentSessionId"] != str(session.id)
    if direct_message:
        try:
            AgentRuntime.finish_continuation(parent.id, asyncio.Event(), accepted=True)
            await asyncio.wait_for(task, timeout=1)
        finally:
            AgentRuntime._pending_continuations.discard(parent.id)
    else:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
    request_type = ApprovalRequest if kind == "approval" else QuestionRequest
    requests = [obj for (model, _key), obj in objects.items() if model is request_type]
    assert len(requests) == 2 and all(request.status == "expired" for request in requests)
    assert parent.status == "running"


async def test_child_authorization_uses_main_grants_child_roots_and_snapshot(monkeypatch):
    parent = ChatSession(id=uuid4(), user_id=uuid4())
    child = ChatSession(id=uuid4(), user_id=parent.user_id, parent_session_id=parent.id,
                        agent_profile_id=BUILD_PROFILE_ID)
    run = AgentRun(id=uuid4(), session_id=child.id, input_message_id=uuid4(), trust_level="public")
    settings = ToolSettings(public_permissions={"policies": {"network.fetch": "ask"}}).select_trust(TrustLevel.PUBLIC)
    message = Message(id=run.input_message_id, provider_data={"_matoloop": {
        "delegationCeiling": subagents.permission_snapshot(settings, {"webfetch"})
    }})
    db = AsyncMock(spec=AsyncSession)
    db.get.side_effect = lambda model, key: parent if model is ChatSession else message
    _mock_value = AsyncMock(return_value=(ToolSettings(), None))
    for _mock_target in (mock_agent_runtime, mock_agent_loop,):
        monkeypatch.setattr(_mock_target, 'load_agent_profile_tools', _mock_value)
    _mock_value = AsyncMock(return_value=IntegrationSettings())
    for _mock_target in (mock_agent_runtime, mock_agent_loop,):
        monkeypatch.setattr(_mock_target, 'load_effective_integrations', _mock_value)
    evaluate = AsyncMock(side_effect=[PermissionDecision.ASK, PermissionDecision.ALLOW])
    monkeypatch.setattr(mock_agent_runtime.PermissionPolicy, "evaluate", evaluate)
    approve = AsyncMock(return_value="allow_once")
    monkeypatch.setattr(AgentRuntime, "_request_approval", approve)
    request = PermissionRequest(capability="network.fetch", resource_type="url", resource_scope="https://example.org",
                                action_kind="fetch", risk_level="low", title="Read", summary="Read")
    roots = RootFileService(child.id)
    call = ToolCall("fetch-1", "webfetch", {"url": "https://example.org"})
    decision = await AgentRuntime._authorize(db, run, child, call, request, roots)
    assert decision == PermissionDecision.ALLOW
    for evaluated in evaluate.await_args_list:
        assert evaluated.args[1] is child and evaluated.args[3] is roots
        assert evaluated.args[4] == ToolExecutionPolicy.ASK
        assert evaluated.kwargs["permission_session_id"] == parent.id
    assert approve.await_args.args[1] is run
    assert (await AgentRuntime._authorize(db, run, child, ToolCall("task", "task", {}), request, roots)) == PermissionDecision.DENY


@pytest.mark.parametrize(
    ("provider_trust", "model_trust", "expected_trust", "expected_pseudonymization"),
    [
        (TrustLevel.PRIVATE, TrustLevel.PUBLIC, TrustLevel.PUBLIC, True),
        (TrustLevel.PRIVATE, None, TrustLevel.PRIVATE, True),
    ],
)
@pytest.mark.parametrize("tool_limit", [25, None])
@pytest.mark.parametrize("session_trust", [None, "public", "private"])
@pytest.mark.usefixtures("mock_db_settings")
async def test_start_persists_effective_run_policy_snapshot(
    monkeypatch,
    provider_trust: TrustLevel,
    model_trust: TrustLevel | None,
    expected_trust: TrustLevel,
    expected_pseudonymization: bool,
    tool_limit: int | None,
    session_trust: str | None,
) -> None:
    from mato_loop_backend.settings.models import AgentProfile

    profile = AgentProfile(name="Custom", instructions="Follow the task.", maxToolCalls=tool_limit)
    _mock_value = AsyncMock(return_value=profile)
    for _mock_target in (mock_agent_runtime, mock_agent_loop,):
        monkeypatch.setattr(_mock_target, 'load_agent_profile', _mock_value)
    provider = LlmProviderConfig(
        name="Provider",
        adapter_type="openai-compatible",
        trust_level=provider_trust,
        pseudonymization_enabled=True,
    )
    model = ModelConfig(
        provider_id=provider.id,
        model_id="model",
        name="Anzeigename",
        trust_level=model_trust,
    )
    settings = AiSettings(
        providers=[provider], models=[model], default_model_id=model.id
    )
    monkeypatch.setattr(SettingsStore, "load", AsyncMock(return_value=settings))
    task = Mock()

    def create_task(coroutine):
        coroutine.close()
        return task

    monkeypatch.setattr(asyncio, "create_task", create_task)
    session = ChatSession(
        id=uuid4(),
        user_id=uuid4(),
        title="Test",
        model_config_id=model.id,
        agent_profile_id=profile.id,
        trust_level=session_trust,
    )
    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = [session, None, True, True]

    requested_run_id = uuid4()
    run = await AgentRuntime.start(db, session, "Nachricht", run_id=requested_run_id)

    message, persisted_run = db.add_all.call_args.args[0]
    assert message.provider_data["_matoloop"]["maxToolCallsByRun"] == {str(run.id): tool_limit}
    assert message.trust_level == expected_trust.value
    assert persisted_run is run
    assert run.id == requested_run_id
    assert run.model_config_id == model.id
    assert run.model_name == "Anzeigename"
    assert run.trust_level == expected_trust.value
    assert session.trust_level == (session_trust or expected_trust.value)
    assert run.pseudonymization_enabled is expected_pseudonymization
    db.commit.assert_awaited_once()
    task.add_done_callback.assert_called_once()
    AgentRuntime._tasks.pop(run.id, None)


@pytest.mark.usefixtures("mock_db_settings")
async def test_start_accepts_unavailable_model_for_persisted_host_failure(monkeypatch) -> None:
    missing_model_id = uuid4()
    settings = AiSettings(models=[], default_model_id=missing_model_id)
    monkeypatch.setattr(SettingsStore, "load", AsyncMock(return_value=settings))
    task = Mock()

    def create_task(coroutine):
        coroutine.close()
        return task

    monkeypatch.setattr(asyncio, "create_task", create_task)
    session = ChatSession(
        id=uuid4(),
        user_id=uuid4(),
        title="Test",
        model_config_id=missing_model_id,
    )
    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = [session, None, True, True]
    monkeypatch.setattr(mock_agent_runtime.EpisodeIndexService, "enqueue", AsyncMock())
    run = await AgentRuntime.start(db, session, "Nachricht")
    assert run.model_config_id is None
    db.commit.assert_awaited_once()


async def test_start_rejects_disabled_profile_before_persisting(monkeypatch) -> None:
    from mato_loop_backend.settings.models import AgentProfile

    profile = AgentProfile(name="Paused", instructions="Work.", enabled=False)
    monkeypatch.setattr(mock_agent_runtime, "load_agent_profile", AsyncMock(return_value=profile))
    session = ChatSession(id=uuid4(), user_id=uuid4(), agent_profile_id=profile.id)
    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = [session, None, True]

    with pytest.raises(AgentRunError) as exc_info:
        await AgentRuntime.start(db, session, "Work")

    assert exc_info.value.code == "agentProfile.selectedUnavailable"
    db.add_all.assert_not_called()
    db.commit.assert_not_awaited()


@pytest.mark.usefixtures("mock_db_settings")
async def test_start_switches_profile_with_run_and_discards_session_grants(monkeypatch) -> None:
    from mato_loop_backend.settings.models import AgentProfile

    profile = AgentProfile(name="Next", instructions="Follow the task.")
    load_profile = AsyncMock(return_value=profile)
    _mock_value = load_profile
    for _mock_target in (mock_agent_runtime, mock_agent_loop,):
        monkeypatch.setattr(_mock_target, 'load_agent_profile', _mock_value)
    provider = LlmProviderConfig(name="Provider", adapter_type="openai-compatible")
    model = ModelConfig(provider_id=provider.id, model_id="model")
    monkeypatch.setattr(
        SettingsStore, "load", AsyncMock(return_value=AiSettings(
            providers=[provider], models=[model], default_model_id=model.id
        )),
    )

    def create_task(coroutine):
        coroutine.close()
        return Mock()

    monkeypatch.setattr(asyncio, "create_task", create_task)
    session = ChatSession(
        id=uuid4(), user_id=uuid4(), agent_profile_id=uuid4(), model_config_id=model.id
    )
    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = [session, None, True, True]

    run = await AgentRuntime.start(
        db, session, "Next message", agent_profile_id_override=profile.id
    )

    load_profile.assert_awaited_once_with(db, session.user_id, profile.id)
    assert any(
        "DELETE FROM permission_grants" in str(call.args[0])
        for call in db.execute.await_args_list
    )
    assert session.agent_profile_id == run.agent_profile_id == profile.id
    db.commit.assert_awaited_once()
    AgentRuntime._tasks.pop(run.id, None)


@pytest.mark.usefixtures("mock_db_settings")
@pytest.mark.parametrize(("title", "expected_title"), [
    ("Neue Session", "urlaub.png"), ("Kopie: Urlaub", "Kopie: Urlaub"),
])
async def test_start_schedules_title_generation_after_agent_run(monkeypatch, title, expected_title) -> None:
    provider = LlmProviderConfig(name="Provider", adapter_type="openai-compatible")
    model = ModelConfig(provider_id=provider.id, model_id="model")
    settings = AiSettings(
        providers=[provider], models=[model], default_model_id=model.id
    )
    monkeypatch.setattr(SettingsStore, "load", AsyncMock(return_value=settings))
    run_worker = AsyncMock()
    title_worker = AsyncMock()
    monkeypatch.setattr(AgentRuntime, "_run", run_worker)
    monkeypatch.setattr(AgentRuntime, "_generate_title", title_worker)
    attachment_service = Mock()
    attachment_service.create_attachment = AsyncMock(
        return_value=Mock(id=uuid4(), filename="urlaub.png")
    )
    attachment_service.discard = AsyncMock()
    def _mock_value(**_kwargs):
        return attachment_service
    for _mock_target in (mock_agent_runtime, mock_agent_loop, mock_agent_titles,):
        monkeypatch.setattr(_mock_target, 'AttachmentService', _mock_value)
    scheduled = []
    tasks = []

    def create_task(coroutine):
        scheduled.append(coroutine)
        coroutine.close()
        task = Mock()
        tasks.append(task)
        return task

    monkeypatch.setattr(asyncio, "create_task", create_task)
    session = ChatSession(
        id=uuid4(),
        user_id=uuid4(),
        title=title,
        title_generation_status=TitleGenerationStatus.PENDING.value,
        model_config_id=model.id,
    )
    run_id = uuid4()
    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = [session, None, True, True]

    def assign_ids(items):
        next(item for item in items if isinstance(item, AgentRun)).id = run_id
        next(item for item in items if isinstance(item, Message)).id = uuid4()

    db.add_all.side_effect = assign_ids

    await AgentRuntime.start(
        db,
        session,
        "",
        attachments=[AttachmentInput(b"image", "image/png", "urlaub.png", "image")],
    )

    message = next(item for item in db.add_all.call_args.args[0] if isinstance(item, Message))
    assert len(scheduled) == 2
    assert session.title == expected_title
    assert session.title_generation_status == TitleGenerationStatus.RUNNING.value
    run_worker.assert_called_once_with(run_id)
    title_worker.assert_called_once_with(
        run_id,
        session.id,
        message.id,
        expected_title,
        after=tasks[0],
    )
    assert list(AgentRuntime._title_tasks.values()) == [session.user_id]
    AgentRuntime._tasks.pop(run_id, None)
    AgentRuntime._title_tasks.clear()


async def test_repeated_run_cancel_does_not_interrupt_cleanup() -> None:
    run_id = uuid4()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    cancellations = 0

    async def worker() -> None:
        nonlocal cancellations
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancellations += 1
            cleanup_started.set()
            try:
                await release_cleanup.wait()
            except asyncio.CancelledError:
                cancellations += 1
            raise

    task = asyncio.create_task(worker())
    AgentRuntime._tasks[run_id] = task
    AgentRuntime._cancellable_runs.add(run_id)
    try:
        first = asyncio.create_task(AgentRuntime.cancel_and_wait(run_id))
        await cleanup_started.wait()
        second = asyncio.create_task(AgentRuntime.cancel_and_wait(run_id))
        await asyncio.sleep(0)
        release_cleanup.set()

        assert await asyncio.gather(first, second) == [True, True]
        assert cancellations == 1
    finally:
        AgentRuntime._tasks.pop(run_id, None)
        AgentRuntime._cancellable_runs.discard(run_id)
        AgentRuntime._user_cancelled_runs.discard(run_id)
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_cancel_waits_when_run_is_already_finalizing() -> None:
    run_id = uuid4()
    release = asyncio.Event()

    async def worker() -> None:
        await release.wait()

    task = asyncio.create_task(worker())
    AgentRuntime._tasks[run_id] = task
    try:
        cancellation = asyncio.create_task(AgentRuntime.cancel_and_wait(run_id))
        await asyncio.sleep(0)

        assert task.cancelling() == 0
        assert not cancellation.done()

        release.set()
        assert await cancellation is True
    finally:
        AgentRuntime._tasks.pop(run_id, None)
        AgentRuntime._user_cancelled_runs.discard(run_id)
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_cancel_and_wait_user_title_tasks_only_cancels_owned_tasks() -> None:
    user_id = uuid4()
    other_user_id = uuid4()
    owned = asyncio.create_task(asyncio.Event().wait())
    other = asyncio.create_task(asyncio.Event().wait())
    AgentRuntime._title_tasks[owned] = user_id
    AgentRuntime._title_tasks[other] = other_user_id
    try:
        assert await AgentRuntime.cancel_and_wait_user_title_tasks(user_id) is True
        assert owned.cancelled()
        assert not other.done()
    finally:
        AgentRuntime._title_tasks.clear()
        other.cancel()
        await asyncio.gather(other, return_exceptions=True)


async def test_title_generation_rechecks_user_before_each_provider_request(
    monkeypatch,
) -> None:
    user_id = uuid4()
    provider = LlmProviderConfig(name="Provider", adapter_type="openai-compatible")
    model = ModelConfig(provider_id=provider.id, model_id="model")
    settings = AiSettings(
        providers=[provider], models=[model], title_generation_model_id=model.id
    )
    session = ChatSession(
        id=uuid4(),
        user_id=user_id,
        title="Initial title",
        title_generation_status=TitleGenerationStatus.RUNNING.value,
        model_config_id=model.id,
    )
    db = AsyncMock(spec=AsyncSession)
    message = Message(id=uuid4(), session_id=session.id, role="user", content="Message")
    db.get.side_effect = [session, message]
    ensure_active = AsyncMock(
        side_effect=[None, AgentRunError("auth.accountDisabled")]
    )
    complete = AsyncMock(return_value=ProviderResult())
    fail = AsyncMock()
    def _mock_value():
        return nullcontext(db)
    for _mock_target in (mock_agent_runtime, mock_agent_summarization, mock_agent_titles,):
        monkeypatch.setattr(_mock_target, 'AsyncSessionFactory', _mock_value)
    _mock_value = AsyncMock(return_value=settings)
    for _mock_target in (mock_agent_runtime, mock_agent_loop, mock_agent_titles,):
        monkeypatch.setattr(_mock_target, 'load_effective_ai', _mock_value)
    monkeypatch.setattr(
        SettingsStore, "load", AsyncMock(return_value=AgentRuntimeSettings())
    )
    monkeypatch.setattr(AgentRuntime, "_ensure_user_active", ensure_active)
    monkeypatch.setattr(mock_agent_titles.ProviderService, "complete", complete)
    monkeypatch.setattr(AgentRuntime, "_fail_title_generation", fail)

    await AgentRuntime._generate_title(
        uuid4(), session.id, message.id, session.title
    )

    assert ensure_active.await_count == 2
    assert all(call.args == (db, user_id) for call in ensure_active.await_args_list)
    complete.assert_awaited_once()
    fail.assert_awaited_once_with(db, session.id)


def test_unknown_runtime_exception_has_safe_descriptor() -> None:
    secret = "secret upstream response"

    error = _public_run_error(RuntimeError(secret))

    assert error.payload() == {
        "code": "agent.failed",
        "message": "The agent run failed.",
    }
    assert secret not in str(error.payload())


@pytest.mark.parametrize("preflight,previously_started", [(False, False), (True, False), (True, True)])
async def test_run_failure_persists_structured_message_before_terminal_event(monkeypatch, preflight, previously_started) -> None:
    monkeypatch.setattr(AgentRuntime, "_finalize_run_files", AsyncMock())
    run = AgentRun(
        id=uuid4(),
        session_id=uuid4(),
        status="queued",
        model_name="Antwortmodell",
        trust_level="private",
    )
    db = AsyncMock(spec=AsyncSession)
    session = ChatSession(id=run.session_id, title_generation_status="running")
    input_message = Message(id=uuid4(), session_id=run.session_id, role="user", content="Work", provider_data={"_matoloop": {"executionStarted": previously_started}})
    run.input_message_id = input_message.id
    db.get.side_effect = lambda model, key, **_kw: session if model is ChatSession else input_message if key == input_message.id else run
    db.scalar.return_value = None
    db.add.side_effect = lambda message: setattr(message, "id", uuid4())
    events = []

    async def fail(*_args) -> None:
        raise ProviderError(
            "secret provider response",
            code="provider.rawSecretResponse",
        )

    async def publish(_run_id, event) -> None:
        events.append(event)

    def _mock_value():
        return nullcontext(db)
    for _mock_target in (mock_agent_runtime, mock_agent_summarization, mock_agent_titles,):
        monkeypatch.setattr(_mock_target, 'AsyncSessionFactory', _mock_value)
    monkeypatch.setattr(AgentRuntime, "_execute_loop", fail)
    if preflight:
        monkeypatch.setattr(AgentRuntime, "_prepare_run", AsyncMock(side_effect=AgentRunError("session.computerRequired")))
    monkeypatch.setattr(AgentRuntime, "_terminalize_incomplete_tool_calls", AsyncMock())
    monkeypatch.setattr(mock_agent_runtime.NotificationService, "deliver_message_after_commit", AsyncMock())
    monkeypatch.setattr(run_events, "publish", publish)

    await AgentRuntime._run(run.id)

    message = db.add.call_args.args[0]
    assert message.error_run_id == run.id
    assert message.error_code == ("session.computerRequired" if preflight else "provider.requestFailed")
    assert message.error_params is None
    assert message.content == ""
    assert run.status == "failed"
    assert run.error == message.error_code
    assert message_payload(message)["origin"] == "host"
    assert events[1]["message"]["state"] == "error"
    assert (message.provider_data["_matoloop"].get("beforeExecution") is True) is (preflight and not previously_started)
    assert [event["type"] for event in events] == [
        "run.started",
        "message.completed",
        "run.failed",
    ]
    assert events[-1]["runId"] == str(run.id)
    assert events[1]["message"]["content"] == message_payload(message)["content"]
    assert events[1]["message"]["createdAt"] == message.created_at.isoformat()
    assert "secret" not in str(events)


@pytest.mark.parametrize(
    ("failed", "expected_title", "expected_body"),
    [
        (False, "Neue Antwort: Test", "Fertige Antwort"),
        (True, "Antwort fehlgeschlagen: Test", "Der Agent-Auftrag konnte nicht abgeschlossen werden. Details stehen in der Session."),
    ],
)
async def test_normal_run_push_uses_canonical_session_target(
    monkeypatch, failed: bool, expected_title: str, expected_body: str
) -> None:
    from mato_loop_backend.models import Notification
    from mato_loop_backend.web_push import WebPushService

    session = ChatSession(id=uuid4(), user_id=uuid4(), title="Test", session_type="chat")
    message = Message(
        id=uuid4(), session_id=session.id, role="assistant", content="Fertige Antwort",
        created_at=datetime.now(UTC), error_code="agent.failed" if failed else None,
    )
    db = AsyncMock(spec=AsyncSession)
    notification_id = uuid4()
    db.scalar.side_effect = [session, session.id, notification_id]
    deliver = AsyncMock()
    monkeypatch.setattr(WebPushService, "deliver_after_commit", deliver)

    await mock_agent_runtime.NotificationService.create_for_message(db, message)
    delivery_db = AsyncMock(spec=AsyncSession)
    delivery_db.__aenter__.return_value = delivery_db
    delivery_db.scalar.return_value = notification_id
    delivery_db.get.return_value = Notification(**db.scalar.await_args.args[0].compile().params)
    monkeypatch.setattr("mato_loop_backend.notifications.AsyncSessionFactory", lambda: delivery_db)
    await db.commit()
    await mock_agent_runtime.NotificationService.deliver_message_after_commit(message.id)

    user_id, payload = deliver.await_args.args
    assert user_id == session.user_id
    assert payload.notification.title == expected_title
    assert payload.notification.body == expected_body
    assert payload.notification.target.session_id == session.id


@pytest.mark.parametrize("redirected", [False, True])
async def test_user_cancel_persists_visible_response_before_terminal_event(monkeypatch, redirected) -> None:
    monkeypatch.setattr(AgentRuntime, "_finalize_run_files", AsyncMock())
    run = AgentRun(
        id=uuid4(),
        session_id=uuid4(),
        status="running",
        model_name="Antwortmodell",
        trust_level="private",
    )
    db = AsyncMock(spec=AsyncSession)
    db.get.return_value = run
    events = []

    async def execute_loop(_db, _run, visible_response) -> None:
        visible_response.content_parts.extend(["Teil", "antwort"])
        visible_response.reasoning = {
            "kind": "content",
            "text": "Zwischenstand",
            "status": "streaming",
        }
        AgentRuntime._user_cancelled_runs.add(run.id)
        if redirected:
            AgentRuntime._redirected_runs.add(run.id)
        raise asyncio.CancelledError

    async def publish(_run_id, event) -> None:
        events.append(event)

    def _mock_value():
        return nullcontext(db)
    for _mock_target in (mock_agent_runtime, mock_agent_summarization, mock_agent_titles,):
        monkeypatch.setattr(_mock_target, 'AsyncSessionFactory', _mock_value)
    monkeypatch.setattr(AgentRuntime, "_execute_loop", execute_loop)
    monkeypatch.setattr(AgentRuntime, "_terminalize_incomplete_tool_calls", AsyncMock())
    monkeypatch.setattr(run_events, "publish", publish)
    with pytest.raises(asyncio.CancelledError):
        await AgentRuntime._run(run.id)

    interrupted = db.add.call_args.args[0]
    assert isinstance(interrupted, Message)
    assert interrupted.content == "Teilantwort"
    assert interrupted.generation_status == "interrupted"
    assert message_payload(interrupted)["redirected"] is redirected
    assert interrupted.model_name == "Antwortmodell"
    assert interrupted.trust_level == "private"
    assert interrupted.public_reasoning == {
        "kind": "content",
        "text": "Zwischenstand",
        "status": "interrupted",
    }
    assert [event["type"] for event in events] == [
        "run.started",
        "message.interrupted",
        "run.cancelled",
    ]
    assert events[1]["message"]["content"] == "Teilantwort"
    assert events[1]["message"]["createdAt"] == interrupted.created_at.isoformat()
    assert events[1]["message"]["modelName"] == "Antwortmodell"
    assert events[1]["message"]["redirected"] is redirected
    assert run.status == "cancelled"


@pytest.mark.parametrize("redirected", [False, True])
async def test_immediate_user_cancel_persists_empty_interruption_marker(monkeypatch, redirected) -> None:
    monkeypatch.setattr(AgentRuntime, "_finalize_run_files", AsyncMock())
    run = AgentRun(id=uuid4(), session_id=uuid4(), status="queued")
    db = AsyncMock(spec=AsyncSession)
    db.get.return_value = run
    events = []

    async def publish(_run_id, event) -> None:
        events.append(event)

    execute_loop = AsyncMock()
    def _mock_value():
        return nullcontext(db)
    for _mock_target in (mock_agent_runtime, mock_agent_summarization, mock_agent_titles,):
        monkeypatch.setattr(_mock_target, 'AsyncSessionFactory', _mock_value)
    monkeypatch.setattr(AgentRuntime, "_execute_loop", execute_loop)
    monkeypatch.setattr(AgentRuntime, "_terminalize_incomplete_tool_calls", AsyncMock())
    monkeypatch.setattr(run_events, "publish", publish)
    AgentRuntime._user_cancelled_runs.add(run.id)
    if redirected:
        AgentRuntime._redirected_runs.add(run.id)

    with pytest.raises(asyncio.CancelledError):
        await AgentRuntime._run(run.id)

    interrupted = db.add.call_args.args[0]
    assert interrupted.content == ""
    assert interrupted.generation_status == "interrupted"
    assert message_payload(interrupted)["redirected"] is redirected
    assert [event["type"] for event in events] == [
        "message.interrupted",
        "run.cancelled",
    ]
    execute_loop.assert_not_awaited()


async def test_shutdown_cancel_is_persisted_as_backend_restart(monkeypatch) -> None:
    monkeypatch.setattr(mock_agent_runtime.NotificationService, "deliver_message_after_commit", AsyncMock())
    run = AgentRun(id=uuid4(), session_id=uuid4(), status="running")
    db = AsyncMock(spec=AsyncSession)
    messages = []

    async def get(model_type, _item_id):
        return run if model_type is AgentRun else messages[0] if messages else None

    def add(message) -> None:
        message.id = uuid4()
        messages.append(message)

    db.get.side_effect = get
    db.scalar.return_value = None
    db.add.side_effect = add
    events = []

    async def execute_loop(_db, _run, _visible_response) -> None:
        raise asyncio.CancelledError

    async def publish(_run_id, event) -> None:
        events.append(event)

    def _mock_value():
        return nullcontext(db)
    for _mock_target in (mock_agent_runtime, mock_agent_summarization, mock_agent_titles,):
        monkeypatch.setattr(_mock_target, 'AsyncSessionFactory', _mock_value)
    monkeypatch.setattr(AgentRuntime, "_execute_loop", execute_loop)
    monkeypatch.setattr(AgentRuntime, "_terminalize_incomplete_tool_calls", AsyncMock())
    monkeypatch.setattr(run_events, "publish", publish)

    with pytest.raises(asyncio.CancelledError):
        await AgentRuntime._run(run.id)

    assert len(messages) == 1
    assert messages[0].error_code == "agent.backendRestarted"
    assert run.status == "failed"
    assert run.error == "agent.backendRestarted"
    assert [event["type"] for event in events] == [
        "run.started",
        "message.completed",
        "run.failed",
    ]
    assert events[-1]["runId"] == str(run.id)


async def test_cancel_after_completed_response_does_not_add_interruption(monkeypatch) -> None:
    monkeypatch.setattr("mato_loop_backend.automations.AutomationService.enqueue_event", AsyncMock())
    monkeypatch.setattr(AgentRuntime, "_finalize_run_files", AsyncMock())
    run = AgentRun(id=uuid4(), session_id=uuid4(), status="running")
    db = AsyncMock(spec=AsyncSession)
    db.get.return_value = run
    events = []

    async def publish(_run_id, event) -> None:
        events.append(event)

    def _mock_value():
        return nullcontext(db)
    for _mock_target in (mock_agent_runtime, mock_agent_summarization, mock_agent_titles,):
        monkeypatch.setattr(_mock_target, 'AsyncSessionFactory', _mock_value)
    monkeypatch.setattr(AgentRuntime, "_execute_loop", AsyncMock())

    async def cancel_after_completion(run_id, event) -> None:
        await publish(run_id, event)
        if event["type"] == "run.completed":
            AgentRuntime._user_cancelled_runs.add(run.id)
            raise asyncio.CancelledError

    monkeypatch.setattr(run_events, "publish", cancel_after_completion)

    with pytest.raises(asyncio.CancelledError):
        await AgentRuntime._run(run.id)

    db.add.assert_not_called()
    assert run.status == "completed"
    assert [event["type"] for event in events] == ["run.started", "run.completed"]


async def test_reasoning_snapshot_keeps_only_latest_state() -> None:
    broker = RunEventBroker()
    run_id = uuid4()

    await broker.publish(run_id, {"type": "reasoning.updated", "reasoning": {"text": "A"}})
    await broker.publish(run_id, {"type": "message.delta", "delta": "Antwort"})
    await broker.publish(run_id, {"type": "reasoning.updated", "reasoning": {"text": "AB"}})

    assert broker._snapshot(run_id)["content"] == "Antwort"
    assert broker._snapshot(run_id)["events"] == [
        {"type": "reasoning.updated", "reasoning": {"text": "AB"}},
    ]


async def test_context_summary_snapshot_keeps_only_latest_state() -> None:
    broker = RunEventBroker()
    run_id = uuid4()

    await broker.publish(
        run_id,
        {"type": "context.summary.updated", "summary": {"content": "A"}},
    )
    await broker.publish(run_id, {"type": "run.phase", "phase": "compacting_context"})
    await broker.publish(
        run_id,
        {"type": "context.summary.updated", "summary": {"content": "AB"}},
    )

    assert broker._snapshot(run_id)["events"] == [
        {"type": "run.phase", "phase": "compacting_context"},
        {"type": "context.summary.updated", "summary": {"content": "AB"}},
    ]


def test_public_reasoning_payload_preserves_long_content() -> None:
    text = "x" * 150_000

    payload = _public_reasoning_payload(PublicReasoning(text=text))

    assert payload is not None
    assert payload["text"] == text
    assert "truncated" not in payload


def test_webfetch_url_query_is_preserved_before_persistence() -> None:
    arguments = _safe_tool_arguments(
        "webfetch", {"url": "https://example.com/page?token=secret", "format": "text"}
    )

    assert arguments == {
        "url": "https://example.com/page?token=secret",
        "format": "text",
    }


def test_redacted_webfetch_query_is_marked_unusable_in_model_history() -> None:
    projected = _model_tool_calls(
        [
            {
                "id": "fetch-1",
                "name": "webfetch",
                "arguments": {"url": "https://example.com/page?REDACTED"},
            }
        ]
    )

    assert projected is not None
    assert projected[0]["arguments"]["url"] == (
        "https://example.com/page?QUERY_VALUES_OMITTED_RECONSTRUCT_BEFORE_REUSE"
    )


def test_write_file_content_is_fully_redacted_before_persistence() -> None:
    secret = "vertraulicher Dateiinhalt"

    arguments = _safe_tool_arguments(
        "write_file",
        {"root": "session", "path": "files/note.txt", "content": secret},
    )

    assert arguments["content"] == {"characters": len(secret), "redacted": True}
    assert secret not in str(arguments)


def test_edit_file_text_is_fully_redacted_before_persistence() -> None:
    arguments = _safe_tool_arguments(
        "edit_file",
        {
            "root": "session",
            "path": "note.txt",
            "oldText": "private old text",
            "newText": "private new text",
        },
    )

    assert arguments["oldText"] == {"characters": 16, "redacted": True}
    assert arguments["newText"] == {"characters": 16, "redacted": True}
    assert "private" not in str(arguments)


def test_apply_patch_content_is_fully_redacted_before_persistence() -> None:
    patch = "*** Begin Patch\n*** Add File: secret.txt\n+secret\n*** End Patch\n"

    arguments = _safe_tool_arguments(
        "apply_patch", {"patch": patch, "expectedSha256": {}}
    )

    assert arguments["patch"] == {"characters": len(patch), "redacted": True}
    assert "secret" not in str(arguments)


def test_qr_code_content_is_fully_redacted_before_persistence() -> None:
    secret = "WIFI:T:WPA;S:Guest;P:not-for-tool-history;;"

    arguments = _safe_tool_arguments("generate_qr_code", {"content": secret})

    assert arguments["content"] == {"characters": len(secret), "redacted": True}
    assert secret not in str(arguments)

    wifi = _safe_tool_arguments("generate_qr_code", {"wifi": {
        "ssid": "Guest", "password": "private value", "security": "WPA",
    }})
    assert wifi["wifi"] == {"redacted": True}
    assert "private value" not in str(wifi)

    protected_file = _safe_tool_arguments("write_file", {
        "content": {"parts": ["sensitive literal", {"value_ref": f"run-secret:{uuid4()}"}]},
    })
    assert protected_file["content"] == {"redacted": True}
    assert "sensitive literal" not in str(protected_file)
    protected_edit = _safe_tool_arguments("edit_file", {
        "newText": {"parts": ["sensitive literal", {"value_ref": f"run-secret:{uuid4()}"}]},
    })
    assert protected_edit["newText"] == {"redacted": True}


@pytest.mark.parametrize(
    ("tool_name", "field", "value"),
    [
        ("tasks_update", "task_token", "encrypted-secret"),
        ("mail_get_message", "message_token", "encrypted-secret"),
        ("mail_get_message", "message_token", f"task:{uuid4()}:0"),
        ("mail_get_message", "password", f"mail:{uuid4()}:0"),
        ("unknown_tool", "message_token", f"mail:{uuid4()}:0"),
    ],
)
def test_mutation_tokens_are_redacted_before_persistence(tool_name, field, value) -> None:
    arguments = _safe_tool_arguments(tool_name, {field: value, "title": "Neu"})

    assert arguments == {field: "[Redacted]", "title": "Neu"}


@pytest.mark.parametrize("replay", [False, True])
@pytest.mark.parametrize("successful_read", [False, True])
def test_legacy_mail_reference_is_restored_or_marked_omitted(replay, successful_read) -> None:
    call = {
        "id": "read",
        "name": "mail_get_message",
        "arguments": {"integration_id": str(uuid4()), "message_token": "[Redacted]"},
    }
    result = Message(
        id=uuid4(), role="tool", tool_call_id="read", attachments=[],
        content='{"message":{"messageToken":"encrypted-token"}}',
        provider_data={"_matoloop": {"toolName": "mail_get_message"}},
    )
    tool_results = {"read": result} if successful_read else {}
    if replay:
        call["arguments"] = json.dumps(call["arguments"])
        projected = _model_provider_file_roots({"responses_output_items": [call]}, tool_results)
        arguments = json.loads(projected["responses_output_items"][0]["arguments"])
    else:
        messages = [Message(id=uuid4(), role="assistant", content="", attachments=[], tool_calls=[call])]
        if successful_read:
            messages.append(result)
        projected = _projection_messages("System", None, messages, TrustLevel.PRIVATE)
        arguments = projected[1]["tool_calls"][0]["arguments"]

    assert arguments["message_token"] == (f"mail:{result.id}:0" if successful_read else (
        "[Historical reference omitted. Do not copy this value. For a new call, "
        "obtain a current reference from a tool result and pass it as message_token.]"
    ))


def test_memory_write_content_is_preserved_for_user_review() -> None:
    content = "Stabiler Selbstkontext " * 1000

    arguments = _safe_tool_arguments(
        "memory_write",
        {"action": "create", "content": content, "category": "context"},
    )

    assert arguments["content"] == content


def test_responses_replay_write_file_content_is_fully_redacted() -> None:
    secret = "nicht persistieren"
    call = ToolCall(
        id="write-1",
        name="write_file",
        arguments={
            "root": "session",
            "path": "files/note.txt",
            "content": secret,
        },
    )

    result = _safe_provider_data(
        {
            "responses_output_items": [
                {
                    "type": "function_call",
                    "call_id": "write-1",
                    "name": "write_file",
                    "arguments": (
                        '{"root":"session","path":"files/note.txt",'
                        f'"content":"{secret}"}}'
                    ),
                }
            ]
        },
        [call],
    )

    persisted = result["responses_output_items"][0]["arguments"]
    assert secret not in persisted
    assert '"redacted": true' in persisted


def test_responses_replay_webfetch_arguments_preserve_query_values() -> None:
    call = ToolCall(
        id="call-1",
        name="webfetch",
        arguments={"url": "https://example.com/page?token=secret"},
    )

    result = _safe_provider_data(
        {
            "responses_output_items": [
                {
                    "type": "function_call",
                    "call_id": "call-1",
                    "name": "webfetch",
                    "arguments": '{"url":"https://example.com/page?token=secret"}',
                }
            ]
        },
        [call],
    )

    assert result["responses_output_items"][0]["arguments"] == (
        '{"url": "https://example.com/page?token=secret"}'
    )


@pytest.mark.parametrize("value", ["encrypted-secret", f"task:{uuid4()}:0"])
def test_responses_replay_preserves_only_short_resource_references(value) -> None:
    call = ToolCall(
        id="task-1",
        name="tasks_update",
        arguments={"task_token": value, "title": "Neu"},
    )

    result = _safe_provider_data(
        {
            "responses_output_items": [
                {
                    "type": "function_call",
                    "call_id": call.id,
                    "name": call.name,
                    "arguments": json.dumps(call.arguments),
                }
            ]
        },
        [call],
    )

    persisted = json.loads(result["responses_output_items"][0]["arguments"])
    assert persisted["task_token"] == ("[Redacted]" if value == "encrypted-secret" else value)


def test_agent_identity_instructions_are_redacted_and_reprojected_as_text() -> None:
    instructions = "Sei direkt. " * 100
    call = ToolCall(
        id="identity-1",
        name="update_agent_identity",
        arguments={"instructions": instructions},
    )

    safe_arguments = _safe_tool_arguments(call.name, call.arguments)
    replay = _safe_provider_data(
        {
            "responses_output_items": [
                {
                    "type": "function_call",
                    "call_id": call.id,
                    "name": call.name,
                    "arguments": {"instructions": instructions},
                }
            ]
        },
        [call],
    )
    projected = _model_tool_calls(
        [{"id": call.id, "name": call.name, "arguments": safe_arguments}]
    )

    assert instructions not in replay["responses_output_items"][0]["arguments"]
    assert projected is not None
    assert isinstance(projected[0]["arguments"]["instructions"], str)
    assert "were truncated" in projected[0]["arguments"]["instructions"]


def test_object_responses_replay_webfetch_arguments_preserve_query_values() -> None:
    result = _safe_provider_data(
        {
            "responses_output_items": [
                {
                    "type": "function_call",
                    "call_id": "call-1",
                    "name": "webfetch",
                    "arguments": {"url": "https://example.com/?secret"},
                }
            ]
        },
        [ToolCall(id="call-1", name="webfetch", arguments={})],
    )

    assert result["responses_output_items"][0]["arguments"] == (
        '{"url": "https://example.com/?secret"}'
    )


def test_provider_cannot_set_internal_runtime_state() -> None:
    result = _safe_provider_data(
        {
            "_matoloop": {
                "memoryOverview": {"hasMore": True},
                "fileActions": [{"downloadUrl": "https://evil.test"}],
            }
        },
        [],
    )

    assert "_matoloop" not in result


async def test_persisted_edited_tool_call_keeps_final_arguments_and_provenance(
    monkeypatch,
) -> None:
    session_id = uuid4()
    run_id = uuid4()
    assistant = Message(
        id=uuid4(),
        session_id=session_id,
        role="assistant",
        content="",
        tool_calls=[{
            "id": "call-1",
            "name": "calendar_create_event",
            "arguments": {"summary": "Meeting"},
        }],
        provider_data={"finish_reason": "tool_calls"},
    )
    serialized_calls = list(assistant.tool_calls)
    live_calls = [{
        "id": "call-1",
        "name": "calendar_create_event",
        "arguments": {"summary": "Meeting"},
    }]
    call = ToolCall(
        "call-1",
        "calendar_create_event",
        {"summary": "Meeting mit Hans"},
    )
    db = AsyncMock(spec=AsyncSession)
    publish = AsyncMock()
    monkeypatch.setattr(mock_agent_runtime.run_events, "publish", publish)

    await _persist_edited_tool_arguments(
        db,
        run_id,
        assistant,
        serialized_calls,
        live_calls,
        0,
        call,
        None,
        call.arguments,
    )

    assert assistant.tool_calls[0]["arguments"]["summary"] == "Meeting mit Hans"
    assert live_calls[0]["arguments"]["summary"] == "Meeting mit Hans"
    assert assistant.provider_data["_matoloop"]["userEditedToolCalls"] == {
        "call-1": {"summary": "Meeting mit Hans"}
    }
    db.execute.assert_awaited_once()
    db.commit.assert_awaited_once()
    publish.assert_awaited_once_with(
        run_id,
        {"type": "message.updated", "messageId": str(assistant.id)},
    )
