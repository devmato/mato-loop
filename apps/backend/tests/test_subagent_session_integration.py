from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload
from sqlalchemy.pool import NullPool

import mato_loop_backend.agent.loop as mock_agent_loop
import mato_loop_backend.agent.runtime as mock_agent_runtime
import mato_loop_backend.agent.summarization as mock_agent_summarization
import mato_loop_backend.agent.titles as mock_agent_titles
import mato_loop_backend.execution.terminals as execution_terminals
from mato_loop_backend.agent.runtime import AgentRuntime
from mato_loop_backend.api import workspace_service
from mato_loop_backend.api.errors import register_error_handlers
from mato_loop_backend.api.routes import approvals, projects, sessions, workspace
from mato_loop_backend.automations import AutomationError, AutomationService
from mato_loop_backend.config import get_runtime_config
from mato_loop_backend.db.session import get_db_session
from mato_loop_backend.execution import ExecutionClientError
from mato_loop_backend.files.service import FileRoot, RootFileService
from mato_loop_backend.files.storage import SessionStorage
from mato_loop_backend.models import (
    AgentRun,
    Automation,
    AutomationRun,
    ChatSession,
    LoginSession,
    Message,
    PermissionGrant,
    Project,
    UserAccount,
)
from mato_loop_backend.security import require_user
from mato_loop_backend.sessions import cleanup
from mato_loop_backend.settings.models import AiSettings, LlmProviderConfig, ModelConfig


@pytest.fixture
async def tree(request, tmp_path, monkeypatch):
    if not request.config.getoption("--subagent-db"):
        pytest.skip("requires the migrated development PostgreSQL database")
    engine = create_async_engine(get_runtime_config().database_url, poolclass=NullPool)
    _mock_value = async_sessionmaker(engine, expire_on_commit=False)
    for _mock_target in (mock_agent_runtime, mock_agent_summarization, mock_agent_titles,):
        monkeypatch.setattr(_mock_target, 'AsyncSessionFactory', _mock_value)
    storage = SessionStorage(tmp_path / "sessions")
    monkeypatch.setattr(workspace_service, "RootFileService", lambda *args, **kwargs: RootFileService(
        *args, session_storage=storage, **kwargs
    ))
    for module in (sessions, projects, cleanup):
        monkeypatch.setattr(module, "SessionStorage", lambda: storage)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            async with AsyncSession(connection, join_transaction_mode="create_savepoint", expire_on_commit=False) as db:
                user = UserAccount(username=f"subagent-{uuid4().hex}", password_hash="test")
                foreign = UserAccount(username=f"subagent-{uuid4().hex}", password_hash="test")
                db.add_all([user, foreign])
                await db.flush()
                parent = None
                records = []
                now = datetime.now(UTC)
                for index in range(3):
                    session = ChatSession(user_id=user.id, title=f"Session {index}", memory_mode="disabled")
                    if parent is not None:
                        session.parent_session_id = parent.id
                        session.spawned_by_run_id = records[-1][1].id
                        session.spawned_by_tool_call_id = "spawn"
                        session.spawn_index = 0
                    db.add(session)
                    await db.flush()
                    message = Message(session_id=session.id, role="user", content="Task", created_at=now)
                    db.add(message)
                    await db.flush()
                    run = AgentRun(session_id=session.id, status="completed", input_message_id=message.id)
                    db.add(run)
                    await db.flush()
                    answer = Message(session_id=session.id, role="assistant", content="Result", created_at=now + timedelta(seconds=1))
                    db.add(answer)
                    await db.flush()
                    run.result_message_id = answer.id
                    records.append((session, run, message))
                    parent = session
                    storage.provision(session.id)
                    RootFileService(session.id, session_storage=storage).write_file(FileRoot.SESSION, "note.txt", "original")
                await db.commit()
                yield SimpleNamespace(db=db, user=user, foreign=foreign, records=records, storage=storage)
        finally:
            await transaction.rollback()
    await engine.dispose()


async def test_preloaded_text_message_can_pass_voice_transcription(tree):
    _, run, message = tree.records[0]
    await AgentRuntime._transcribe_voice_messages(tree.db, run)
    assert message.attachments == []


async def test_database_spawn_constraints_and_message_relationships(tree):
    db, user = tree.db, tree.user
    parent, parent_run, _ = tree.records[0]
    child, _, _ = tree.records[1]
    self_id = uuid4()
    base = dict(user_id=user.id, parent_session_id=parent.id, spawned_by_run_id=parent_run.id,
                spawned_by_tool_call_id="spawn", spawn_index=1)
    for overrides in (
        {"spawn_index": None}, {"spawn_index": -1}, {"spawn_index": 0},
        {"parent_session_id": self_id, "id": self_id},
        {"user_id": tree.foreign.id}, {"spawned_by_run_id": uuid4()},
    ):
        with pytest.raises(IntegrityError):
            async with db.begin_nested():
                db.add(ChatSession(**(base | overrides)))
                await db.flush()
    loaded = await db.scalar(select(ChatSession).where(ChatSession.id == child.id).options(selectinload(ChatSession.messages)))
    assert len(loaded.messages) == 2
    assert all(message.session_id == child.id for message in loaded.messages)
    child_id = child.id
    await db.delete(parent)
    await db.flush()
    assert await db.scalar(select(ChatSession.id).where(ChatSession.id == child_id)) is None
    assert await db.scalar(select(AgentRun.id).where(AgentRun.session_id == child_id)) is None
    assert await db.scalar(select(Message.id).where(Message.session_id == child_id)) is None


@pytest.fixture
async def client(tree):
    app = FastAPI()
    for module in (sessions, workspace, approvals):
        app.include_router(module.router, prefix="/api")
    register_error_handlers(app)
    app.dependency_overrides[require_user] = lambda: tree.user

    async def db_override():
        yield tree.db

    app.dependency_overrides[get_db_session] = db_override
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        yield client


async def test_http_repeat_retry_preserves_failed_runs_and_original_input(tree, client, monkeypatch):
    db = tree.db
    parent, run, message = tree.records[0]
    session_id, message_id = parent.id, message.id
    original_message = (message_id, message.role, message.content, message.created_at)
    provider = LlmProviderConfig(name="Provider", adapter_type="openai-compatible")
    model = ModelConfig(provider_id=provider.id, model_id="model", name="Renamed model")
    settings = AiSettings(providers=[provider], models=[model], default_model_id=model.id)
    parent.model_config_id = uuid4()
    run.model_config_id = model.id
    run.model_name = "Original model"
    run.trust_level = "private"
    run.pseudonymization_enabled = False
    schedule = Mock()
    monkeypatch.setattr(AgentRuntime, "_schedule_run", schedule)
    _mock_value = AsyncMock(return_value=settings)
    for _mock_target in (mock_agent_runtime, mock_agent_loop, mock_agent_titles,):
        monkeypatch.setattr(_mock_target, 'load_effective_ai', _mock_value)
    # Descendant cancellation uses a separate connection outside the fixture transaction.
    monkeypatch.setattr(AgentRuntime, "cancel_descendants", AsyncMock())
    run_ids = [run.id]
    failures = {}

    for attempt, error_code in enumerate(("agent.backendRestarted", "provider.requestFailed"), start=1):
        run.status = "failed"
        run.error = error_code
        run.completed_at = original_message[3] + timedelta(seconds=attempt + 1)
        error = Message(
            session_id=session_id, role="assistant", content="", error_code=error_code,
            error_run_id=run.id, created_at=run.completed_at,
        )
        db.add(error)
        await db.flush()
        run.result_message_id = error.id
        failures[run.id] = error_code
        error_id = error.id
        await db.commit()
        await db.refresh(tree.user)

        response = await client.post(f"/api/sessions/{session_id}/messages/{error_id}/retry")

        assert response.status_code == 202, response.text
        assert response.json()["modelName"] == "Original model"
        retry_id = UUID(response.json()["runId"])
        assert retry_id not in run_ids
        run_ids.append(retry_id)
        runs = list((await db.scalars(
            select(AgentRun).where(AgentRun.session_id == session_id)
            .execution_options(populate_existing=True)
        )).all())
        assert {item.id for item in runs} == set(run_ids)
        assert all(item.input_message_id == message_id for item in runs)
        assert all(
            (item.model_config_id, item.model_name, item.trust_level, item.pseudonymization_enabled)
            == (model.id, "Original model", "private", False)
            for item in runs
        )
        assert {item.id: item.error for item in runs if item.status == "failed"} == failures
        assert all(item.result_message_id is None for item in runs)
        assert [tuple(row) for row in (await db.execute(
            select(Message.id, Message.role, Message.content, Message.created_at)
            .where(Message.session_id == session_id)
        )).all()] == [original_message]
        run = next(item for item in runs if item.id == retry_id)
        assert run.status == "queued" and run.error is None
        assert schedule.call_count == attempt
        assert schedule.call_args.args == (run,)


async def test_child_http_mutations_block_real_storage_and_grants(tree, client):
    child, run, message = tree.records[1]
    child_id = child.id
    grant = PermissionGrant(session_id=child_id, capability="file.write", resource_type="root_path", resource_scope="test")
    tree.db.add(grant)
    await tree.db.commit()
    grant_id = grant.id
    query = f"?scope=session&session_id={child_id}"
    mutations = [
        ("POST", "/api/workspace/directories" + query, {"parent": ".", "name": "new"}),
        ("PUT", "/api/workspace/files" + query, {"path": "note.txt", "content": "changed", "expected_revision": "a" * 64}),
        ("PATCH", "/api/workspace/entries" + query, {"path": "note.txt", "name": "renamed.txt"}),
        ("DELETE", "/api/workspace/entries" + query + "&path=note.txt", None),
        ("POST", "/api/workspace/files/openscad/render" + query, {"path": "note.scad"}),
        ("POST", "/api/workspace/files/revisions/1/activate" + query, {"path": "note.txt", "fileId": str(uuid4()), "expectedCurrentRevision": 1}),
        ("POST", "/api/workspace/entries/move" + query, {"path": "note.txt", "destination": "moved.txt", "destination_scope": "session"}),
        ("POST", "/api/workspace/entries/copy" + query, {"path": "note.txt", "destination": "copy.txt", "destination_scope": "session"}),
        ("POST", f"/api/sessions/{child_id}/messages", {"content": "manual"}),
        ("PATCH", f"/api/sessions/{child_id}", {"title": "changed"}),
        ("DELETE", f"/api/sessions/{child_id}", None),
        ("POST", f"/api/sessions/{child_id}/runs/{run.id}/cancel", None),
        ("POST", f"/api/sessions/{child_id}/messages/{message.id}/undo", None),
        ("POST", f"/api/sessions/{child_id}/messages/{message.id}/retry", None),
        ("DELETE", f"/api/approvals/sessions/{child_id}/permissions", None),
        ("DELETE", f"/api/approvals/sessions/{child_id}/permissions/{grant_id}", None),
    ]
    for method, url, payload in mutations:
        response = await client.request(method, url, json=payload)
        assert (response.status_code, response.json()["error"]["code"]) == (403, "session.readOnly"), (url, response.text)
    response = await client.post("/api/workspace/files" + query, files={"file": ("upload.txt", b"upload")})
    assert response.status_code == 403 and response.json()["error"]["code"] == "session.readOnly"
    response = await client.post(
        f"/api/workspace/directories?scope=filesystem&session_id={child_id}&execution_agent_id={uuid4()}",
        json={"parent": "/", "name": "blocked"},
    )
    assert response.status_code == 403 and response.json()["error"]["code"] == "session.readOnly"
    files = RootFileService(child_id, session_storage=tree.storage)
    assert files.read_bytes(FileRoot.SESSION, "note.txt", limit=100) == b"original"
    assert await tree.db.get(PermissionGrant, grant_id) is not None
    assert [entry.name for entry in files.list_entries(FileRoot.SESSION, ".").entries] == ["note.txt"]


async def test_child_reads_exports_and_copy_out_remain_available(tree, client):
    parent_id, child_id = tree.records[0][0].id, tree.records[1][0].id
    response = await client.get(f"/api/sessions/{parent_id}/children")
    assert response.status_code == 200 and response.json()[0]["id"] == str(child_id)
    response = await client.get(f"/api/workspace/files/download?scope=session&session_id={child_id}&path=note.txt")
    assert response.status_code == 200 and response.content == b"original"
    response = await client.post(f"/api/workspace/entries/copy?scope=session&session_id={child_id}", json={
        "path": "note.txt", "destination": "export.txt", "destination_scope": "session", "destination_session_id": str(parent_id),
    })
    assert response.status_code == 200, response.text
    assert RootFileService(parent_id, session_storage=tree.storage).read_bytes(FileRoot.SESSION, "export.txt", limit=100) == b"original"
    response = await client.post(f"/api/workspace/directories?scope=session&session_id={parent_id}", json={"parent": ".", "name": "allowed"})
    assert response.status_code == 201
    tree.user = tree.foreign
    response = await client.get(f"/api/workspace/files/download?scope=session&session_id={child_id}&path=note.txt")
    assert response.status_code == 404
    response = await client.post(f"/api/workspace/directories?scope=session&session_id={child_id}", json={"parent": ".", "name": "hidden"})
    assert response.status_code == 404


@pytest.mark.parametrize("in_project", [False, True])
async def test_session_delete_with_persistent_user_after_rollback(tree, client, in_project):
    db, storage = tree.db, tree.storage
    parent = tree.records[0][0]
    ids = [item[0].id for item in tree.records]
    project_id = None
    if in_project:
        project = Project(user_id=tree.user.id, name="Preserved project")
        db.add(project)
        await db.flush()
        project_id = parent.project_id = project.id
        storage.remove_session(parent.id)
        storage.provision(parent.id, project_id)
        RootFileService(parent.id, project_id=project_id, session_storage=storage).write_file(
            FileRoot.SESSION, "shared.txt", "preserve"
        )
        await db.commit()

    response = await client.delete(f"/api/sessions/{ids[0]}")

    assert response.status_code == 204, response.text
    assert not (await db.scalars(select(ChatSession.id).where(ChatSession.id.in_(ids)))).all()
    assert not storage.session_path(ids[0], project_id=project_id).exists()
    assert all(not storage.session_path(item_id).exists() for item_id in ids[1:])
    assert list(storage.trash_dir.iterdir()) == []
    if in_project:
        assert await db.get(Project, project_id) is not None
        assert (storage.logical_root_path(ids[0], project_id) / "files" / "shared.txt").read_text() == "preserve"


async def test_project_delete_cleans_cross_project_descendants_and_preserves_other_project(tree):
    db, storage = tree.db, tree.storage
    user_id = tree.user.id
    parent, child, grandchild = [item[0] for item in tree.records]
    ids = [parent.id, child.id, grandchild.id]
    project = Project(user_id=user_id, name="Parent project")
    other = Project(user_id=user_id, name="Child snapshot project")
    db.add_all([project, other])
    await db.flush()
    parent.project_id = child.project_id = project.id
    await db.flush()
    assert await projects._session_count(db, project) == 1
    child.project_id = other.id
    for session in (parent, child):
        storage.remove_session(session.id)
        storage.provision(session.id, session.project_id)
    other_project_id = other.id
    other_root = storage.logical_root_path(child.id, other.id)
    RootFileService(child.id, project_id=other.id, session_storage=storage).write_file(FileRoot.SESSION, "shared.txt", "preserve")
    await db.commit()
    impact = await projects.project_delete_impact(project.id, tree.user, db)
    assert impact["sessionCount"] == 1
    await projects.delete_project(project.id, tree.user, db, automation_impact=impact["automationImpact"])
    assert not (await db.scalars(select(ChatSession.id).where(ChatSession.id.in_(ids)))).all()
    assert await db.get(Project, other_project_id) is not None
    assert (other_root / "files" / "shared.txt").read_text() == "preserve"
    assert not storage.session_path(ids[1], project_id=other_project_id).exists()
    assert not storage.session_path(ids[2]).exists()
    assert list(storage.trash_dir.iterdir()) == []


async def test_first_message_undo_keeps_parent_and_storage_but_deletes_children(tree):
    parent, _, message = tree.records[0]
    ids = [item[0].id for item in tree.records]
    response = await sessions.undo_user_message(parent.id, message.id, tree.user, tree.db)
    assert response["draft"]["content"] == message.content
    assert (await tree.db.scalars(select(ChatSession.id).where(ChatSession.id.in_(ids)))).all() == [parent.id]
    assert tree.storage.session_path(parent.id).exists()
    assert all(not tree.storage.session_path(item_id).exists() for item_id in ids[1:])


async def test_suffix_undo_preserves_earlier_children_and_removes_child_run_cycle(tree):
    db, storage = tree.db, tree.storage
    parent, _, initial = tree.records[0]
    parent_id = parent.id
    retained_ids = [item[0].id for item in tree.records]
    request = Message(session_id=parent_id, role="user", content="Remove this turn", created_at=initial.created_at + timedelta(seconds=2))
    db.add(request)
    await db.flush()
    run = AgentRun(session_id=parent_id, input_message_id=request.id, status="completed")
    db.add(run)
    await db.flush()
    child = ChatSession(user_id=tree.user.id, parent_session_id=parent_id,
                        spawned_by_run_id=run.id, spawned_by_tool_call_id="suffix", spawn_index=0)
    db.add(child)
    await db.flush()
    child_input = Message(session_id=child.id, role="user", content="Child task")
    db.add(child_input)
    await db.flush()
    child_run = AgentRun(session_id=child.id, input_message_id=child_input.id, status="failed")
    db.add(child_run)
    await db.flush()
    error = Message(session_id=child.id, role="assistant", content="", error_code="provider.requestFailed", error_run_id=child_run.id)
    db.add(error)
    await db.flush()
    child_run.result_message_id = error.id
    child_id, run_id, child_run_id, request_id = child.id, run.id, child_run.id, request.id
    storage.provision(child_id)
    await db.commit()
    result = await sessions.undo_user_message(parent_id, request_id, tree.user, db)
    assert result["draft"]["content"] == "Remove this turn"
    assert set((await db.scalars(select(ChatSession.id).where(ChatSession.id.in_(retained_ids)))).all()) == set(retained_ids)
    assert await db.scalar(select(ChatSession.id).where(ChatSession.id == child_id)) is None
    assert await db.scalar(select(AgentRun.id).where(AgentRun.id == child_run_id)) is None
    assert await db.scalar(select(AgentRun.input_message_id).where(AgentRun.id == run_id)) is None
    assert not storage.session_path(child_id).exists()
    assert all(storage.session_path(item_id).exists() for item_id in retained_ids)


async def test_automation_and_terminal_attach_reject_real_child(tree, monkeypatch):
    child = tree.records[1][0]
    with pytest.raises(AutomationError, match="session.readOnly"):
        await AutomationService.normal_session(tree.db, child.id, tree.user.id, field="sessionId")

    @asynccontextmanager
    async def database():
        yield tree.db

    monkeypatch.setattr(execution_terminals, "AsyncSessionFactory", database)
    login = LoginSession(
        user_id=tree.user.id, token_hash=uuid4().hex,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    tree.db.add(login)
    await tree.db.flush()
    with pytest.raises(ExecutionClientError, match="session.notFound"):
        await execution_terminals.validate_terminal_access(tree.user.id, child.id, uuid4(), login.id)


async def test_multiple_automations_share_session_and_deletion_preserves_its_tree(tree):
    parent = tree.records[0][0]
    ids = [item[0].id for item in tree.records]
    automation, other = [await AutomationService.create(
        tree.db, user_id=tree.user.id, session_id=parent.id, name=name,
        task="Task", enabled=True, schedules=[],
    ) for name in ("Daily report", "Weekly report")]
    await tree.db.commit()
    await AutomationService.delete(tree.db, automation.id, tree.user.id, expected_revision=automation.revision)
    assert set((await tree.db.scalars(select(ChatSession.id).where(ChatSession.id.in_(ids)))).all()) == set(ids)
    assert await tree.db.scalar(select(Automation.id).where(Automation.execution_session_id == parent.id)) == other.id
    assert all(tree.storage.session_path(item_id).exists() for item_id in ids)
    assert list(tree.storage.trash_dir.iterdir()) == []


async def test_archive_restoration_keeps_individual_session_state_and_drops_queued_work(tree):
    from datetime import UTC, datetime, timedelta

    from mato_loop_backend.sessions.archive import is_session_archived, set_archived

    db = tree.db
    session = tree.records[0][0]
    project = Project(user_id=tree.user.id, name="Archive test")
    db.add(project)
    await db.flush()
    session.project_id = project.id
    past = datetime.now(UTC) - timedelta(days=1)
    automation = await AutomationService.create(db, user_id=tree.user.id, session_id=session.id,
        name="Report", task="Report", enabled=True, schedules=[{"run_at": past, "enabled": True}], now=past - timedelta(seconds=1))
    queued = await AutomationService.enqueue_manual(db, automation.id, tree.user.id)
    await set_archived(db, session, True)
    await set_archived(db, project, True)
    assert session.updated_at is not None and project.updated_at is not None
    await set_archived(db, project, False)
    assert await is_session_archived(db, session)
    await db.refresh(queued)
    assert queued.status == "cancelled"
    await set_archived(db, session, False)
    assert not await is_session_archived(db, session)
    assert automation.enabled and session.project_id == project.id
    assert await AutomationService.enqueue_due(db) == 0


@pytest.mark.parametrize("trigger", ["agent_response", "agent_error"])
async def test_event_triggers_are_owner_bound_deduplicated_and_do_not_chain(tree, trigger):
    db = tree.db
    session, source, _message = tree.records[0]
    automation = await AutomationService.create(db, user_id=tree.user.id, session_id=session.id,
        name="Listener", task="Act only when needed", enabled=True, schedules=[], triggers=[trigger])
    foreign_session = ChatSession(user_id=tree.foreign.id, title="Foreign")
    db.add(foreign_session)
    await db.flush()
    await AutomationService.create(db, user_id=tree.foreign.id, session_id=foreign_session.id,
        name="Foreign listener", task="Do work", enabled=True, schedules=[], triggers=[trigger])
    await AutomationService.enqueue_event(db, source, trigger)
    await db.flush()
    await AutomationService.enqueue_event(db, source, trigger)
    automated = AgentRun(id=uuid4(), session_id=session.id, automation_origin=True, status="completed")
    await AutomationService.enqueue_event(db, automated, trigger)
    rows = (await db.scalars(select(AutomationRun).where(AutomationRun.source_run_id == source.id))).all()
    assert [row.automation_id for row in rows] == [automation.id]
    assert not (await db.scalars(select(AutomationRun).where(AutomationRun.source_run_id == automated.id))).all()
