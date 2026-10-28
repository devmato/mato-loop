import asyncio
import json
from collections import Counter
from contextlib import aclosing, asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

import mato_loop_backend.agent.runtime as mock_agent_runtime
import mato_loop_backend.agent.summarization as mock_agent_summarization
import mato_loop_backend.agent.titles as mock_agent_titles
import mato_loop_backend.notifications as mock_notifications
import mato_loop_backend.tools.personal_integrations as personal_integrations
import mato_loop_backend.tools.web as mock_tools_web
import mato_loop_backend.web_push as mock_web_push
from mato_loop_backend.agent import subagents
from mato_loop_backend.agent.run_contracts import AgentRunError
from mato_loop_backend.agent.runtime import AgentRuntime
from mato_loop_backend.api.errors import register_error_handlers
from mato_loop_backend.api.routes import approvals, sessions
from mato_loop_backend.config import get_runtime_config
from mato_loop_backend.db.base import Base
from mato_loop_backend.db.session import get_db_session
from mato_loop_backend.events import run_events
from mato_loop_backend.execution import (
    DirectoryResult,
    ExecutionClient,
    execution_agent_connections,
)
from mato_loop_backend.files.storage import SessionStorage
from mato_loop_backend.integrations.caldav.service import CalDavIntegrationService
from mato_loop_backend.models import (
    AgentRun,
    ApprovalRequest,
    ChatSession,
    ExecutionAgent,
    Message,
    Project,
    UserAccount,
)
from mato_loop_backend.permissions.decisions import decide_agent_action
from mato_loop_backend.personal_variables import (
    create_personal_variable,
    personal_variable_value,
    update_personal_variable,
)
from mato_loop_backend.providers.base import ProviderResult, ToolCall
from mato_loop_backend.providers.service import ProviderService
from mato_loop_backend.security import require_user
from mato_loop_backend.settings import AiSettings, SettingsStore, ToolSettings
from mato_loop_backend.settings.models import (
    BUILD_PROFILE_ID,
    PLAN_PROFILE_ID,
    LlmProviderConfig,
    ModelConfig,
    ToolExecutionPolicy,
    UserAgentProfilesSettings,
)
from mato_loop_backend.tools.arguments import PersonalSettingsArguments
from mato_loop_backend.web.fetch import WebFetchResult


@pytest.fixture
async def native_database(request, tmp_path, monkeypatch):
    if not request.config.getoption("--subagent-db"):
        pytest.skip("requires the development PostgreSQL database with pgvector")
    config = get_runtime_config()
    schema = f"native_subagent_{uuid4().hex}"
    admin = create_async_engine(config.database_url, poolclass=NullPool)
    engine = create_async_engine(
        config.database_url,
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": f"{schema},public"}},
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    owned_runs = set()
    cleanup_before = set(run_events._cleanup_tasks)
    monkeypatch.setattr(config, "data_dir", tmp_path)
    monkeypatch.setattr(config, "embedding_enabled", False)
    monkeypatch.setattr(config, "web_push_enabled", False)
    _mock_value = factory
    for _mock_target in (mock_agent_runtime, mock_agent_summarization, mock_agent_titles, mock_web_push, mock_notifications):
        monkeypatch.setattr(_mock_target, 'AsyncSessionFactory', _mock_value)
    monkeypatch.setattr(subagents, "AsyncSessionFactory", factory)
    async with admin.begin() as db:
        await db.execute(CreateSchema(schema))
    try:
        async with engine.begin() as db:
            assert await db.scalar(text("SELECT current_schema()")) == schema
            # checkfirst would see public tables through search_path and skip isolation.
            await db.run_sync(lambda connection: Base.metadata.create_all(connection, checkfirst=False))
        async with factory() as db:
            user = UserAccount(username=f"native-{uuid4().hex}", password_hash="test")
            db.add(user)
            await db.flush()
            agent = ExecutionAgent(user_id=user.id, alias="Test computer", home_directory="/workspace")
            db.add(agent)
            await db.flush()
            provider = LlmProviderConfig(name="Scripted", adapter_type="openai-compatible", pseudonymization_enabled=False)
            model = ModelConfig(provider_id=provider.id, model_id="scripted-native")
            await SettingsStore.save(db, AiSettings(providers=[provider], models=[model], default_model_id=model.id))
            await SettingsStore.save(db, ToolSettings(public_permissions={"policies": {"session.read": "ask"}}))
            await db.commit()

        operations = frozenset({"filesystem.resolve", "filesystem.read", "filesystem.glob",
                                "filesystem.grep", "filesystem.write"})
        monkeypatch.setattr(execution_agent_connections, "snapshot", lambda agent_id: operations if agent_id == agent.id else None)

        async def resolve_directory(_client, operation, arguments, _result_model, **_kwargs):
            assert operation == "filesystem.resolve"
            return DirectoryResult(path=arguments["path"])

        monkeypatch.setattr(ExecutionClient, "request", resolve_directory)

        app = FastAPI()
        app.include_router(sessions.router, prefix="/api")
        app.include_router(approvals.router, prefix="/api")
        register_error_handlers(app)
        app.dependency_overrides[require_user] = lambda: user

        async def database():
            async with factory() as db:
                yield db

        app.dependency_overrides[get_db_session] = database
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            yield SimpleNamespace(factory=factory, user=user, model=model, agent=agent, client=client, owned_runs=owned_runs)
    finally:
        try:
            # Stop and join only this fixture's runs before dropping its committed data.
            await asyncio.gather(*(AgentRuntime.cancel_and_wait(run_id) for run_id in owned_runs))
            cleanup = set(run_events._cleanup_tasks) - cleanup_before
            for task in cleanup:
                task.cancel()
            await asyncio.gather(*cleanup, return_exceptions=True)
            for run_id in owned_runs:
                run_events._history.pop(run_id, None)
                run_events._checkpoints.pop(run_id, None)
                run_events._subscribers.pop(run_id, None)
                run_events.unregister_subagent(run_id)
        finally:
            await engine.dispose()
            async with admin.begin() as db:
                await db.execute(DropSchema(schema, cascade=True))
            await admin.dispose()


async def _events(run_id, event_type, count=1):
    found = []
    async with asyncio.timeout(20), aclosing(run_events.subscribe(run_id)) as stream:
        async for event in stream:
            if event["type"] == event_type:
                found.append(event)
                if len(found) == count:
                    return found
            if event["type"] == "run.failed":
                pytest.fail(f"Run failed before {event_type}: {event}")


async def test_admission_account_lock_allows_first_personal_settings_insert(native_database):
    fixture = native_database
    async with fixture.factory() as admission, fixture.factory() as settings:
        await AgentRuntime._ensure_user_active(admission, fixture.user.id, for_update=True)
        await settings.execute(text("SET LOCAL lock_timeout = '2s'"))
        record = await SettingsStore.save(settings, UserAgentProfilesSettings(), user_id=fixture.user.id)
        assert record.user_id == fixture.user.id


async def test_personal_variable_rename_keeps_concurrent_value(native_database):
    fixture = native_database
    async with fixture.factory() as stale, fixture.factory() as editor:
        variable = await create_personal_variable(
            stale, fixture.user.id, name="OLD", value="old-value", secret=False,
        )
        await stale.commit()
        await update_personal_variable(
            editor, fixture.user.id, variable.id, name="OLD", value="new-value", secret=False,
        )
        await editor.commit()
        renamed = await update_personal_variable(
            stale, fixture.user.id, variable.id, name="NEW", value=None, secret=False,
        )
        await stale.commit()
        assert personal_variable_value(renamed) == "new-value"


async def test_customize_rejects_connection_changed_before_service_save(native_database, monkeypatch):
    fixture = native_database
    async with fixture.factory() as db, fixture.factory() as editor:
        saved = await CalDavIntegrationService.create(
            db, owner_id=fixture.user.id, name="Calendar", enabled=True,
            base_url="https://old.example.com", username="ada", password="password",
        )
        item = saved.instances[0]

        @asynccontextmanager
        async def changed_service_session():
            await CalDavIntegrationService.update(
                editor, item.id, owner_id=fixture.user.id, name=item.name, enabled=True,
                base_url="https://new.example.com", username="ada", password=None,
                expected_instance=item,
            )
            yield editor

        monkeypatch.setattr(personal_integrations, "AsyncSessionFactory", changed_service_session)
        result = await personal_integrations.manage_integrations(
            PersonalSettingsArguments(
                action="update", area="integrations", integration_id=item.id,
                changes={"name": "Renamed calendar"},
            ),
            SimpleNamespace(db=db, session=SimpleNamespace(user_id=fixture.user.id)),
            AsyncMock(return_value=None),
        )
        assert result.metadata["errorCode"] == "integration.changed"


@pytest.mark.parametrize("command_name", ["git-merge", "git_merge"])
async def test_coding_command_excludes_new_runs_in_nested_directories(native_database, monkeypatch, command_name):
    fixture = native_database
    monkeypatch.setattr(AgentRuntime, "_schedule_run", lambda run: fixture.owned_runs.add(run.id))
    async with fixture.factory() as db:
        agent = ExecutionAgent(user_id=fixture.user.id, alias="Coding", home_directory="/workspace")
        db.add(agent)
        await db.flush()
        owner = ChatSession(user_id=fixture.user.id, title="Publication", agent_profile_id=BUILD_PROFILE_ID,
                            model_config_id=fixture.model.id, preferred_execution_agent_id=agent.id,
                            current_directory="/worktree", memory_mode="disabled")
        other = ChatSession(user_id=fixture.user.id, title="Other task", agent_profile_id=BUILD_PROFILE_ID,
                            model_config_id=fixture.model.id, preferred_execution_agent_id=agent.id,
                            current_directory="/project/src", memory_mode="disabled")
        db.add_all([owner, other])
        await db.flush()
        message = Message(session_id=owner.id, role="user", content=f"/{command_name}", provider_data={
            "_matoloop": {"codingCommand": {"name": command_name, "resources": ["/project", "/worktree"]}},
        })
        db.add(message)
        await db.flush()
        run = AgentRun(session_id=owner.id, input_message_id=message.id, status="running",
                       execution_agent_id=agent.id, current_directory_snapshot="/worktree")
        db.add(run)
        await db.commit()
        other_id = other.id
    async with fixture.factory() as db:
        other = await db.get(ChatSession, other_id)
        with pytest.raises(AgentRunError, match="git.projectBusy"):
            await AgentRuntime.start(db, other, "Continue normal coding.")


@pytest.mark.parametrize("with_project", [False, True], ids=["without-project", "with-project"])
async def test_direct_message_during_model_response(native_database, monkeypatch, with_project):
    fixture = native_database
    session_id, run_id, message_id = uuid4(), uuid4(), uuid4()
    fixture.owned_runs.add(run_id)
    started = asyncio.Event()
    requests = []

    async def complete(db, **kwargs):
        requests.append(kwargs)
        if len(requests) == 1:
            await kwargs["on_delta"]("Partial answer")
            started.set()
            await asyncio.Future()
        return ProviderResult(content="Updated answer", finish_reason="stop")

    monkeypatch.setattr(ProviderService, "complete", complete)
    async with fixture.factory() as db:
        project = None
        if with_project:
            project = Project(user_id=fixture.user.id, name="Continuation regression")
            db.add(project)
            await db.flush()
        session = ChatSession(
            id=session_id, user_id=fixture.user.id,
            project_id=project.id if project else None,
            title="Direct message regression", title_generation_status="skipped",
            agent_profile_id=BUILD_PROFILE_ID, model_config_id=fixture.model.id,
            memory_mode="disabled", web_search_policy="available",
            preferred_execution_agent_id=fixture.agent.id, current_directory="/workspace",
        )
        db.add(session)
        await db.flush()
        SessionStorage().provision(session_id, session.project_id)
        await AgentRuntime.start(db, session, "Prepare a PDF.", run_id=run_id)

    await asyncio.wait_for(started.wait(), timeout=20)
    response = await fixture.client.post(
        f"/api/sessions/{session_id}/runs/{run_id}/messages",
        json={"message_id": str(message_id), "content": "Keep the PDF file small."},
    )
    assert response.status_code == 202, response.text
    assert response.json()["runId"] == str(run_id)
    async with asyncio.timeout(20), aclosing(run_events.subscribe(run_id)) as stream:
        async for event in stream:
            if event["type"] in {"run.completed", "run.failed"}:
                break

    async with fixture.factory() as db:
        message = await db.get(Message, message_id)
        assert message.content == "Keep the PDF file small."
        assert message.provider_data["_matoloop"]["continuationRunId"] == str(run_id)
        run = await db.get(AgentRun, run_id)
        assert run.status == "completed", run.error
        assert (await db.get(Message, run.result_message_id)).content == "Updated answer"
    assert len(requests) == 2
    assert "Keep the PDF file small." in str(requests[-1]["messages"])


@pytest.mark.parametrize("profile_id", [PLAN_PROFILE_ID, BUILD_PROFILE_ID], ids=["plan", "build"])
@pytest.mark.parametrize("cancel", [False, True], ids=["approve", "cancel"])
async def test_native_delegation_pipeline(native_database, monkeypatch, profile_id, cancel):
    fixture = native_database
    parent_id, parent_run_id = uuid4(), uuid4()
    fixture.owned_runs.add(parent_run_id)
    barrier = asyncio.Barrier(2)
    calls = Counter()
    provider_databases = {}
    joined_results = []

    async def complete(db, **kwargs):
        session_id = UUID(kwargs["prompt_cache_key"])
        session = await db.get(ChatSession, session_id)
        assert session is not None
        calls[session_id] += 1
        schemas = {item["name"] for item in kwargs["tools"]}
        assert kwargs["model"].id == fixture.model.id
        assert kwargs["reasoning_effort"] == "high"
        assert "delegationCeiling" not in str(kwargs["messages"])
        if session_id == parent_id:
            assert "task" in schemas
            if calls[session_id] == 1:
                return ProviderResult(tool_calls=[ToolCall("delegate-batch", "task", {"tasks": [
                    {"description": "First task", "prompt": "Read this child session and report first result."},
                    {"description": "Second task", "prompt": "Report second result independently."},
                ]})], finish_reason="tool_calls")
            assert not cancel
            tools = [item for item in kwargs["messages"] if item["role"] == "tool"]
            result = json.loads(tools[-1]["content"])
            joined_results.extend(result["tasks"])
            assert [item["status"] for item in joined_results] == ["completed", "completed"]
            assert [item["result"] for item in joined_results] == ["first result", "second result"]
            return ProviderResult(content="Both delegated tasks completed.", finish_reason="stop")

        assert session.parent_session_id == parent_id
        assert session.memory_mode == "disabled"
        assert "task" not in schemas and "session_read" in schemas
        run = await db.scalar(select(AgentRun).where(AgentRun.session_id == session_id))
        fixture.owned_runs.add(run.id)
        if calls[session_id] == 1:
            provider_databases[session_id] = db
            # Neither child can pass until both normal runtimes reach their provider request.
            async with asyncio.timeout(5):
                await barrier.wait()
            if session.spawn_index == 0 or cancel:
                return ProviderResult(tool_calls=[ToolCall("read-own-session", "session_read", {
                    "session_id": str(session_id),
                })], finish_reason="tool_calls")
        if session.spawn_index == 0:
            assert any(item["role"] == "tool" for item in kwargs["messages"])
        return ProviderResult(content="first result" if session.spawn_index == 0 else "second result", finish_reason="stop")

    monkeypatch.setattr(ProviderService, "complete", complete)
    async with fixture.factory() as db:
        parent = ChatSession(id=parent_id, user_id=fixture.user.id, title="Native integration",
                             title_generation_status="skipped", agent_profile_id=profile_id,
                             model_config_id=fixture.model.id, memory_mode="disabled",
                             reasoning_effort="high", web_search_policy="available")
        parent.preferred_execution_agent_id = fixture.agent.id
        parent.current_directory = "/workspace"
        db.add(parent)
        await db.flush()
        SessionStorage().provision(parent.id)
        await AgentRuntime.start(db, parent, "Delegate the two independent tasks.", run_id=parent_run_id)

    forwarded = await _events(parent_run_id, "approval.created", count=2 if cancel else 1)
    assert len(provider_databases) == 2
    assert len({id(db) for db in provider_databases.values()}) == 2
    assert calls[parent_id] == 1
    response = await fixture.client.get(f"/api/sessions/{parent_id}/children")
    assert response.status_code == 200, response.text
    children = sorted(response.json(), key=lambda item: item["spawnIndex"])
    assert len(children) == 2
    child_ids = [UUID(child["id"]) for child in children]
    child_run_ids = [UUID(child["run"]["id"]) for child in children]
    fixture.owned_runs.update(child_run_ids)
    pending_response = await fixture.client.get("/api/approvals/pending")
    assert pending_response.status_code == 200, pending_response.text
    assert {item["id"] for item in pending_response.json()} == {event["approval"]["id"] for event in forwarded}
    assert all(item["sourceSession"]["id"] == str(parent_id) for item in pending_response.json())

    async with fixture.factory() as db:
        assert (await db.get(AgentRun, parent_run_id)).status == "running"
        pending = list((await db.scalars(select(ApprovalRequest).where(ApprovalRequest.status == "pending"))).all())
        assert len(pending) == (2 if cancel else 1)
        for approval in pending:
            assert approval.session_id == parent_id and approval.run_id in child_run_ids
            assert approval.payload["subagentSessionId"] in {str(child_id) for child_id in child_ids}
        for event in forwarded:
            assert event["approval"]["sessionId"] == str(parent_id)
            assert event["approval"]["payload"]["subagentSessionId"] == event["subagentSessionId"]
            assert event["approval"]["payload"]["subagentTitle"] == event["subagentTitle"]

    # The ordinary transcript endpoint works while the child is still waiting for approval.
    live = await fixture.client.get(f"/api/sessions/{child_ids[0]}")
    assert live.status_code == 200, live.text
    assert "delegationCeiling" not in live.text
    assert live.json()["readOnly"] is True
    assert live.json()["activeRun"]["status"] == "waiting_approval"
    assert [item["role"] for item in live.json()["messages"]] == ["user", "assistant"]
    child_events = await _events(child_run_ids[0], "approval.created")
    assert child_events[0]["approval"]["id"] in {event["approval"]["id"] for event in forwarded}
    await _events(parent_run_id, "subagent.updated")

    if cancel:
        assert await AgentRuntime.cancel_and_wait(parent_run_id)
        await _events(parent_run_id, "run.cancelled")
        await _events(parent_run_id, "approval.expired", count=2)
        assert all(calls[child_id] == 1 for child_id in child_ids)
        assert calls[parent_id] == 1
    else:
        await _events(child_run_ids[1], "run.completed")
        assert calls[child_ids[1]] == 1 and calls[child_ids[0]] == 1 and calls[parent_id] == 1
        async with fixture.factory() as db:
            approval = await db.get(ApprovalRequest, UUID(forwarded[0]["approval"]["id"]))
            result = await decide_agent_action(db, approval, "allow_once")
            assert result == {"status": "approved", "decision": "allow_once"}
        await _events(parent_run_id, "approval.resolved")
        await _events(parent_run_id, "run.completed")
        # The independent child is not restarted when its sibling's permission resolves.
        assert calls[child_ids[0]] == 2 and calls[child_ids[1]] == 1 and calls[parent_id] == 2
        assert {item["sessionId"] for item in joined_results} == {str(child_id) for child_id in child_ids}

    async with fixture.factory() as db:
        expected = "cancelled" if cancel else "completed"
        for run_id in [parent_run_id, *child_run_ids]:
            run = await db.get(AgentRun, run_id)
            assert run.status == expected and run.parent_run_id is None
            assert run.result_message_id is not None
            assert run.internal_transcript is None and run.internal_result is None
        approvals = list((await db.scalars(select(ApprovalRequest))).all())
        assert all(item.status == ("expired" if cancel else "approved") for item in approvals)
        if not cancel:
            for item in joined_results:
                run = await db.get(AgentRun, UUID(item["runId"]))
                assert run.session_id == UUID(item["sessionId"])
                assert str(run.result_message_id) == item["resultMessageId"]
            records = list((await db.scalars(select(Message).where(Message.session_id == child_ids[0]).order_by(Message.created_at, Message.id))).all())
            assert [item.role for item in records] == ["user", "assistant", "tool", "assistant"]
            assert records[-1].content == "first result"
    for child_id in child_ids:
        response = await fixture.client.get(f"/api/sessions/{child_id}/messages")
        assert response.status_code == 200 and response.json()["items"]
    assert not any(run_id in run_events._subagents for run_id in child_run_ids)
    assert (await fixture.client.get("/api/approvals/pending")).json() == []


async def test_webfetch_dispatch_cannot_bypass_spawn_ask_after_live_auto(native_database, monkeypatch):
    fixture = native_database
    parent_id, parent_run_id = uuid4(), uuid4()
    fixture.owned_runs.add(parent_run_id)
    calls = Counter()
    network_reads = []

    async def fetch(url, **kwargs):
        await kwargs["authorize_url"](url)
        network_reads.append(url)
        return WebFetchResult(final_url=url, content_type="text/plain", content="Fetched",
                              status_code=200, output_format="text")

    async def complete(db, **kwargs):
        session = await db.get(ChatSession, UUID(kwargs["prompt_cache_key"]))
        calls[session.id] += 1
        if session.id == parent_id:
            if calls[session.id] == 1:
                return ProviderResult(tool_calls=[ToolCall("delegate", "task", {"tasks": [
                    {"description": "Fetch", "prompt": "Fetch the requested page."},
                ]})], finish_reason="tool_calls")
            return ProviderResult(content="The child reported the denied fetch.", finish_reason="stop")
        child_run = await db.scalar(select(AgentRun).where(AgentRun.session_id == session.id))
        fixture.owned_runs.add(child_run.id)
        if calls[session.id] == 1:
            assert "webfetch" in {schema["name"] for schema in kwargs["tools"]}
            live = await SettingsStore.load(db, ToolSettings)
            live.public_permissions.policies["network.fetch"] = ToolExecutionPolicy.AUTO
            await SettingsStore.save(db, live)
            await db.commit()
            return ProviderResult(tool_calls=[ToolCall("fetch", "webfetch", {
                "url": "https://example.test/page",
            })], finish_reason="tool_calls")
        return ProviderResult(content="No fetch performed.", finish_reason="stop")

    monkeypatch.setattr(ProviderService, "complete", complete)
    monkeypatch.setattr(mock_tools_web, 'fetch_web', fetch)
    async with fixture.factory() as db:
        await SettingsStore.save(db, ToolSettings(public_permissions={"policies": {"network.fetch": "ask"}}))
        parent = ChatSession(id=parent_id, user_id=fixture.user.id, title="Ceiling regression",
                             title_generation_status="skipped", agent_profile_id=BUILD_PROFILE_ID,
                             model_config_id=fixture.model.id, memory_mode="disabled")
        parent.preferred_execution_agent_id = fixture.agent.id
        parent.current_directory = "/workspace"
        db.add(parent)
        await db.flush()
        SessionStorage().provision(parent_id)
        await AgentRuntime.start(db, parent, "Delegate fetching the page.", run_id=parent_run_id)
    forwarded = (await _events(parent_run_id, "approval.created"))[0]
    assert network_reads == []
    async with fixture.factory() as db:
        assert (await SettingsStore.load(db, ToolSettings)).public_permissions.policies["network.fetch"] == ToolExecutionPolicy.AUTO
        approval = await db.get(ApprovalRequest, UUID(forwarded["approval"]["id"]))
        assert approval.session_id == parent_id and approval.capability == "network.fetch"
        assert (await db.get(AgentRun, approval.run_id)).status == "waiting_approval"
        await decide_agent_action(db, approval, "deny")
    await _events(parent_run_id, "run.completed")
    assert network_reads == []
