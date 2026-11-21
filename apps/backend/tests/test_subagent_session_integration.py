import json
from contextlib import asynccontextmanager, nullcontext
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call
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
from mato_loop_backend.api.routes import approvals, projects, sessions, trash, workspace
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
    ExecutionAgent,
    LoginSession,
    Message,
    PermissionGrant,
    Project,
    QueuedAttachment,
    QueuedMessage,
    UserAccount,
    VoiceConnection,
)
from mato_loop_backend.security import require_user
from mato_loop_backend.sessions import cleanup
from mato_loop_backend.settings import SettingsStore, UserPreferences
from mato_loop_backend.settings.models import (
    PLAN_PROFILE_ID,
    AiSettings,
    LlmProviderConfig,
    ModelConfig,
)


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


async def test_message_queue_persists_deduplicates_and_dispatches_fifo_with_uploads(tree, client, monkeypatch):
    from mato_loop_backend.sessions import message_queue

    db = tree.db
    session, _, _ = tree.records[0]
    schedule = Mock()
    monkeypatch.setattr(message_queue, "schedule_delivery", schedule)
    request_id, second_id = uuid4(), uuid4()
    response = await client.post(f"/api/sessions/{session.id}/messages/with-attachments", data={"payload": json.dumps({
        "content": "First", "run_id": str(request_id), "enqueue": True,
        "agent_profile_id": str(PLAN_PROFILE_ID),
    })}, files=[("attachments", ("note.txt", b"saved upload", "text/plain"))])
    response.raise_for_status()
    followup = await client.post(f"/api/sessions/{session.id}/messages", json={
        "content": "Second", "run_id": str(second_id), "enqueue": True, "after_run_id": str(request_id),
    })
    followup.raise_for_status()
    duplicate = await client.post(f"/api/sessions/{session.id}/messages", json={"content": "First", "run_id": str(request_id), "enqueue": True})
    duplicate.raise_for_status()
    assert schedule.call_args_list == [call(session.id), call(session.id)]
    listing = (await client.get("/api/sessions/message-queue")).json()["items"]
    assert [item["id"] for item in listing] == [str(request_id), str(second_id)]
    assert await message_queue.list_entries(db, tree.foreign.id) == []
    download = await client.get(listing[0]["attachments"][0]["url"])
    assert download.content == b"saved upload"

    async def accept(db, session, content, **kwargs):
        message = Message(session_id=session.id, role="user", content=content)
        db.add(message)
        await db.flush()
        db.add(AgentRun(id=kwargs["run_id"], session_id=session.id, status="completed", input_message_id=message.id))
        await db.commit()

    start = AsyncMock(side_effect=accept)
    monkeypatch.setattr(AgentRuntime, "start", start)
    monkeypatch.setattr(message_queue, "AsyncSessionFactory", lambda: nullcontext(db))
    db.expunge_all()
    await message_queue.deliver_next(session.id)
    assert start.await_args.kwargs["agent_profile_id_override"] == PLAN_PROFILE_ID
    assert (start.await_args.args[2], start.await_args.kwargs["attachments"][0].data) == ("First", b"saved upload")
    assert await db.get(QueuedAttachment, (request_id, 0)) is None
    assert [item["id"] for item in await message_queue.list_entries(db, tree.user.id)] == [str(second_id)]
    admitted = await db.get(AgentRun, request_id)
    removed = await client.delete(f"/api/sessions/{session.id}/queue/{request_id}", params={"require_present": True})
    download = await client.get(listing[0]["attachments"][0]["url"])
    assert [(response.status_code, response.json()["error"]["params"]["messageId"]) for response in (removed, download)] == [
        (409, str(admitted.input_message_id)), (409, str(admitted.input_message_id)),
    ]
    missing = await client.delete(f"/api/sessions/{session.id}/queue/{uuid4()}", params={"require_present": True})
    assert (missing.status_code, missing.json()["error"]["code"]) == (404, "session.messageNotFound")


async def test_message_submission_lookup_only_returns_owned_root_messages(tree, client):
    session, run, message = tree.records[0]
    child, child_run, _ = tree.records[1]
    run.status = "queued"
    child_run.status = "queued"
    await tree.db.commit()

    found = await client.get(f"/api/sessions/message-submissions/{run.id}")
    hidden = await client.get(f"/api/sessions/message-submissions/{child_run.id}")
    foreign = await sessions.session_service.find_message_submission(tree.db, tree.foreign.id, run.id)
    assert (found.status_code, found.json(), hidden.status_code, foreign) == (
        200, {"sessionId": str(session.id), "messageId": str(message.id)}, 404, None,
    )
    assert child.parent_session_id == session.id


@pytest.mark.parametrize("blocker", ["workspace", "predecessor"])
async def test_message_queue_pauses_unsafe_followups_without_losing_the_entry(tree, monkeypatch, blocker):
    from mato_loop_backend.sessions import message_queue

    db = tree.db
    session, predecessor, _ = tree.records[0]
    request_id = uuid4()
    await message_queue.enqueue(db, session, request_id, {"content": "Follow up"}, after_run_id=predecessor.id)
    if blocker == "workspace":
        session.current_directory = "/changed"
    else:
        predecessor.status = "failed"
    await db.commit()
    start = AsyncMock()
    monkeypatch.setattr(AgentRuntime, "start", start)
    monkeypatch.setattr(message_queue, "AsyncSessionFactory", lambda: nullcontext(db))
    await message_queue.deliver_next(session.id)
    entry = await db.get(QueuedMessage, request_id)
    assert (entry.paused, entry.error_code, start.await_count) == (
        True, "session.queueContextChanged" if blocker == "workspace" else "session.queuePredecessorFailed", 0,
    )


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
    for module in (sessions, workspace, approvals, projects, trash):
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
        survivor = ChatSession(user_id=tree.user.id, project_id=project_id, title="Preserved session")
        db.add(survivor)
        await db.flush()
        survivor_id = survivor.id
        storage.provision(survivor_id, project_id)
        RootFileService(survivor_id, project_id=project_id, session_storage=storage).write_file(
            FileRoot.SESSION, "retained.txt", "preserve"
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
        assert await db.get(ChatSession, survivor_id) is not None
        assert (storage.logical_root_path(survivor_id, project_id) / "files" / "retained.txt").read_text() == "preserve"


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
    survivor = ChatSession(user_id=user_id, project_id=other.id, title="Preserved session")
    db.add(survivor)
    await db.flush()
    survivor_id = survivor.id
    for session in (parent, child, survivor):
        storage.remove_session(session.id)
        storage.provision(session.id, session.project_id)
    other_project_id = other.id
    other_root = storage.logical_root_path(survivor_id, other.id)
    RootFileService(survivor_id, project_id=other.id, session_storage=storage).write_file(FileRoot.SESSION, "retained.txt", "preserve")
    await db.commit()
    impact = await projects.project_delete_impact(project.id, tree.user, db)
    assert impact["sessionCount"] == 1
    await projects.delete_project(project.id, tree.user, db, automation_impact=impact["automationImpact"])
    assert not (await db.scalars(select(ChatSession.id).where(ChatSession.id.in_(ids)))).all()
    assert await db.get(Project, other_project_id) is not None
    assert await db.get(ChatSession, survivor_id) is not None
    assert (other_root / "files" / "retained.txt").read_text() == "preserve"
    assert not storage.session_path(ids[0], project_id=project.id).exists()
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


@pytest.mark.parametrize("retry", [False, True], ids=["undo", "retry"])
async def test_removed_parent_turn_trims_reused_child_history(tree, monkeypatch, retry):
    db = tree.db
    parent, _, initial = tree.records[0]
    child = tree.records[1][0]
    child_id = child.id
    request = Message(session_id=parent.id, role="user", content="Follow up", created_at=initial.created_at + timedelta(seconds=2))
    db.add(request)
    await db.flush()
    run = AgentRun(session_id=parent.id, input_message_id=request.id, status="failed")
    db.add(run)
    await db.flush()
    instruction = Message(session_id=child.id, role="user", content="Discard this instruction",
                          created_at=request.created_at, provider_data={"_matoloop": {"delegationSource": {
                              "sessionId": str(parent.id), "runId": str(run.id),
                          }}})
    error = Message(session_id=parent.id, role="assistant", content="", error_code="provider.requestFailed", error_run_id=run.id,
                    created_at=request.created_at + timedelta(seconds=1))
    db.add_all([instruction, error])
    await db.flush()
    child_run = AgentRun(session_id=child.id, input_message_id=instruction.id, status="completed")
    db.add(child_run)
    db.add(Message(session_id=child.id, role="assistant", content="Discard this result", created_at=error.created_at))
    await db.commit()
    if retry:
        monkeypatch.setattr(AgentRuntime, "retry", AsyncMock(return_value=run))
        await sessions.retry_failed_message(parent.id, error.id, tree.user, db)
    else:
        await sessions.undo_user_message(parent.id, request.id, tree.user, db)
    assert (await db.scalars(select(Message.content).where(Message.session_id == child_id)
                             .order_by(Message.created_at, Message.id))).all() == ["Task", "Result"]


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


@pytest.fixture
async def trash_tree(tree, monkeypatch):
    await SettingsStore.save(tree.db, UserPreferences(trash_enabled=True, trash_retention_days=7), user_id=tree.user.id)
    await tree.db.commit()
    # All fixture runs are completed; cancellation otherwise opens an unrelated transaction.
    monkeypatch.setattr(AgentRuntime, "cancel_descendants", AsyncMock())
    return tree


async def test_trash_session_preserves_history_files_and_fixed_deadline(trash_tree, client):
    tree = trash_tree
    db = tree.db
    parent, child, _ = [row[0] for row in tree.records]
    ids = [row[0].id for row in tree.records]
    history = set(await db.execute(select(Message.id, Message.content).where(Message.session_id.in_(ids))))
    voice = VoiceConnection(session_id=child.id, user_id=tree.user.id, provider_id=uuid4(), model_id=uuid4(),
                            status="active", lease_until=datetime.now(UTC) + timedelta(minutes=5))
    db.add(voice)
    await db.commit()
    blocked = await client.delete(f"/api/sessions/{parent.id}")
    assert (blocked.status_code, blocked.json()["error"]["code"]) == (409, "voice.busy")
    await db.refresh(parent)
    assert parent.delete_at is None
    voice.status = "ended"
    await db.commit()
    before = datetime.now(UTC)
    response = await client.delete(f"/api/sessions/{parent.id}")
    assert response.status_code == 204, response.text
    await db.refresh(parent)
    deadline = parent.delete_at
    assert before + timedelta(days=7) <= deadline <= datetime.now(UTC) + timedelta(days=7)
    await SettingsStore.save(db, UserPreferences(trash_enabled=False, trash_retention_days=1), user_id=tree.user.id)
    await db.commit()
    listing = (await client.get("/api/trash")).json()
    assert [(item["id"], datetime.fromisoformat(item["deleteAt"])) for item in listing["sessions"]] == [(str(parent.id), deadline)]
    for session_id in ids:
        assert (await client.get(f"/api/sessions/{session_id}")).status_code == 404
        assert (tree.storage.logical_root_path(session_id, None) / "files" / "note.txt").read_text() == "original"
    assert set(await db.execute(select(Message.id, Message.content).where(Message.session_id.in_(ids)))) == history
    assert set(await db.scalars(select(AgentRun.id).where(AgentRun.session_id.in_(ids)))) == {row[1].id for row in tree.records}
    assert (await client.post(f"/api/trash/sessions/{parent.id}/restore")).status_code == 204
    assert (await client.get(f"/api/sessions/{child.id}")).status_code == 200


async def test_trash_project_restore_preserves_archive_and_individual_trash(trash_tree, client):
    tree = trash_tree
    db = tree.db
    parent = tree.records[0][0]
    archived_at = datetime.now(UTC) - timedelta(days=1)
    project = Project(user_id=tree.user.id, name="Recoverable project", archived_at=archived_at)
    db.add(project)
    await db.flush()
    parent.project_id = project.id
    parent.archived_at = archived_at
    independent = ChatSession(user_id=tree.user.id, project_id=project.id, title="Individually deleted")
    db.add(independent)
    await db.commit()
    assert (await client.delete(f"/api/sessions/{independent.id}")).status_code == 204
    deadline = independent.delete_at
    assert (await client.delete(f"/api/projects/{project.id}")).status_code == 204
    listing = (await client.get("/api/trash")).json()
    assert [item["id"] for item in listing["projects"]] == [str(project.id)]
    assert listing["sessions"] == []
    contents = (await client.get(f"/api/trash/projects/{project.id}/sessions")).json()["items"]
    assert {item["id"] for item in contents} == {str(parent.id), str(independent.id)}
    blocked = await client.post(f"/api/trash/sessions/{independent.id}/restore")
    assert (blocked.status_code, blocked.json()["error"]["code"]) == (409, "trash.parentDeleted")
    assert (await client.post(f"/api/trash/projects/{project.id}/restore")).status_code == 204
    await db.refresh(project)
    await db.refresh(parent)
    await db.refresh(independent)
    assert (project.delete_at, project.archived_at, parent.archived_at, independent.delete_at) == (
        None, archived_at, archived_at, deadline,
    )
    assert [item["id"] for item in (await client.get("/api/trash")).json()["sessions"]] == [str(independent.id)]


async def test_trash_restore_rejects_expired_and_foreign_entries(trash_tree, client):
    tree = trash_tree
    session = tree.records[0][0]
    session.delete_at = datetime.now(UTC) - timedelta(seconds=1)
    deadline = session.delete_at
    foreign = Project(user_id=tree.foreign.id, name="Private trash", delete_at=datetime.now(UTC) + timedelta(days=1))
    foreign_session = ChatSession(user_id=tree.foreign.id, title="Private session", delete_at=foreign.delete_at)
    tree.db.add_all([foreign, foreign_session])
    await tree.db.commit()
    assert (await client.get(f"/api/trash/projects/{foreign.id}/sessions")).status_code == 404
    expired = await client.post(f"/api/trash/sessions/{session.id}/restore")
    assert (expired.status_code, expired.json()["error"]["code"]) == (409, "trash.expired")
    for kind, item in (("projects", foreign), ("sessions", foreign_session)):
        response = await client.post(f"/api/trash/{kind}/{item.id}/restore")
        assert (response.status_code, response.json()["error"]["code"]) == (404, "trash.notFound")
    listing = (await client.get("/api/trash")).json()
    assert listing["projects"] == []
    assert [item["id"] for item in listing["sessions"]] == [str(session.id)]
    await tree.db.refresh(session)
    assert session.delete_at == deadline


async def test_trash_automatic_purge_only_due_and_rechecks_after_cancellation(trash_tree, monkeypatch):
    from mato_loop_backend.api import trash_retention

    tree = trash_tree
    db = tree.db
    ids = [row[0].id for row in tree.records]
    past = datetime.now(UTC) - timedelta(days=1)
    tree.records[0][0].delete_at = past
    rescued = ChatSession(user_id=tree.user.id, title="Restored during purge", delete_at=past)
    future = ChatSession(user_id=tree.user.id, title="Not due", delete_at=datetime.now(UTC) + timedelta(days=1))
    db.add_all([rescued, future])
    await db.commit()
    rescued_id, future_id = rescued.id, future.id
    for session_id in (rescued_id, future_id):
        tree.storage.provision(session_id)

    async def restore_before_final_lock(session_id):
        if session_id == rescued_id:
            item = await db.get(ChatSession, rescued_id)
            item.delete_at = None
            await db.commit()

    monkeypatch.setattr(AgentRuntime, "cancel_descendants", restore_before_final_lock)
    monkeypatch.setattr(trash_retention, "AsyncSessionFactory", lambda: nullcontext(db))
    monkeypatch.setattr(trash_retention, "SessionStorage", lambda: tree.storage)
    await trash_retention.purge_expired()
    assert not list(await db.scalars(select(ChatSession.id).where(ChatSession.id.in_(ids))))
    assert all(not tree.storage.session_path(session_id).exists() for session_id in ids)
    assert set(await db.scalars(select(ChatSession.id).where(ChatSession.id.in_((rescued_id, future_id))))) == {rescued_id, future_id}
    assert all(tree.storage.session_path(session_id).exists() for session_id in (rescued_id, future_id))


@pytest.mark.parametrize("path_state,mode", [
    (state, mode) for state in ("present", "missing", "unavailable", "replaced")
    for mode in ("manual", "automatic")
] + [("unavailable", "keep"), ("missing", "project")])
async def test_trash_purge_honors_existing_worktree_selection(trash_tree, client, monkeypatch, path_state, mode):
    from mato_loop_backend.api import trash_retention
    from mato_loop_backend.sessions import coding

    db = trash_tree.db
    item = trash_tree.records[0][0]
    agent = ExecutionAgent(id=uuid4(), user_id=trash_tree.user.id, alias="Test workstation")
    db.add(agent)
    await db.flush()
    item.preferred_execution_agent_id = agent.id
    item.current_directory = "/worktree"
    if mode == "project":
        project = Project(user_id=trash_tree.user.id, name="Project in trash")
        db.add(project)
        await db.flush()
        item.project_id = project.id
    await db.commit()
    session_id = item.id
    info = coding.RepositoryResult(repository="/worktree", commonDirectory="/project/.git",
        gitDirectory="/project/.git/worktrees/worktree", remotes=[], defaultRemote=None, defaultBranch="main", branch="task",
        worktrees=[{"path": "/project", "main": True, "branch": "main", "locked": False},
                   {"path": "/worktree", "main": False, "branch": "task", "locked": False}])
    token = coding.worktree_removal_token(item, info, [], 1.0)
    monkeypatch.setattr(coding, "session_worktree", AsyncMock(return_value=info))
    monkeypatch.setattr(coding, "sessions_in_worktree", AsyncMock(return_value=[]))
    remove = AsyncMock(return_value=coding.WorktreeRemovedResult(removed=True))

    async def request(operation, arguments, result_type, **kwargs):
        if operation == "git.worktree.remove":
            return await remove(arguments)
        path = arguments["path"]
        if path == "/worktree" and path_state in {"missing", "unavailable"}:
            raise ExecutionClientError("file.pathNotFound" if path_state == "missing" else "executionAgent.unavailable")
        return result_type(path=path, kind="directory" if path == "/worktree" else "file", size=None,
                           modifiedAt=2 if path_state == "replaced" else 1)

    monkeypatch.setattr(coding, "coding_client", AsyncMock(return_value=SimpleNamespace(request=request)))
    response = await client.delete(f"/api/sessions/{session_id}", params={"permanent": False})
    assert response.status_code == 204
    await db.refresh(item)
    item.trash_worktree_removal = {"agentId": str(agent.id), "path": "/worktree", "impact": token}
    await db.commit()
    remove.assert_not_awaited()

    blocked = path_state in {"unavailable", "replaced"} and mode != "keep"
    if mode == "automatic":
        item.delete_at = datetime.now(UTC) - timedelta(days=1)
        await db.commit()
        monkeypatch.setattr(trash_retention, "AsyncSessionFactory", lambda: nullcontext(db))
        monkeypatch.setattr(trash_retention, "SessionStorage", lambda: trash_tree.storage)
        await trash_retention.purge_expired()
    elif mode == "project":
        project_url = f"/api/projects/{project.id}"
        assert (await client.delete(project_url, params={"permanent": False})).status_code == 204
        preview = (await client.get(f"{project_url}/delete-impact")).json()
        response = await client.delete(project_url, params={"permanent": True, "automationImpact": preview["automationImpact"]})
        assert response.status_code == 204
    else:
        response = await client.delete(f"/api/sessions/{session_id}", params={"permanent": True, "keepWorktree": mode == "keep"})
        assert response.status_code == (409 if blocked else 204)

    remains = await db.scalar(select(ChatSession.id).where(ChatSession.id == session_id)) is not None
    assert remains == blocked
    assert remove.await_count == int(path_state == "present")
    if path_state == "present":
        assert remove.await_args.args[0]["expectedRegistration"] == 1.0


@pytest.mark.parametrize("outcome", ["restored", "offline", "blocked"])
async def test_trash_removes_worktree_immediately_and_restores_session(trash_tree, client, monkeypatch, outcome):
    from mato_loop_backend.sessions import coding, service

    db = trash_tree.db
    item = trash_tree.records[0][0]
    agent = ExecutionAgent(user_id=item.user_id, alias="Workstation")
    db.add(agent)
    await db.flush()
    project = Project(user_id=item.user_id, name="Worktree project",
                      default_execution_agent_id=agent.id, default_current_directory="/repo")
    db.add(project)
    await db.flush()
    item.project_id = project.id
    item.preferred_execution_agent_id = agent.id
    item.current_directory = "/worktree"
    item.workspace_mode = "worktree"
    item.coding_workspace = {"baseCommit": "b" * 40}
    item.session_profile = {"worktrees_enabled": True}
    session_id = item.id
    await db.commit()

    info = coding.RepositoryResult(repository="/worktree", commonDirectory="/repo/.git", remotes=[],
        defaultRemote=None, defaultBranch="main", branch=None, worktrees=[
            {"path": "/repo", "main": True, "branch": "main", "locked": False},
            {"path": "/worktree", "main": False, "branch": None, "locked": False},
        ])
    token = coding.worktree_removal_token(item, info, [item], 1.0)
    monkeypatch.setattr(coding, "session_worktree", AsyncMock(return_value=info))
    monkeypatch.setattr(coding, "worktree_registration", AsyncMock(return_value=1.0))
    monkeypatch.setattr(service, "validate_execution_preferences", AsyncMock(side_effect=lambda db, user, agent, path: (path, "auto")))
    commit = "a" * 40

    async def request(operation, arguments, result_type, **kwargs):
        if operation == "git.worktree.remove":
            if outcome == "blocked":
                raise ExecutionClientError("git.worktreeLocked")
            return result_type(removed=True, headCommit=commit)
        if outcome == "offline":
            raise ExecutionClientError("executionAgent.unavailable")
        assert operation == "git.worktree" and arguments["restoreCommit"] == commit
        return result_type(repository="/repo", commonDirectory="/repo/.git", path=arguments["destinations"][0],
                           gitDirectory="/repo/.git/worktrees/new", branch=None, baseCommit=commit, targetBranch="main")

    execution = AsyncMock(side_effect=request)
    monkeypatch.setattr(coding, "coding_client", AsyncMock(return_value=SimpleNamespace(request=execution)))
    monkeypatch.setattr(coding, "inspect_repository", AsyncMock(return_value=info.model_copy(update={
        "repository": "/repo", "worktree_root": "/managed", "project_worktree_directory": "/managed/project",
    })))
    response = await client.delete(f"/api/sessions/{session_id}", params={"permanent": False, "worktreeImpact": token})
    assert execution.await_args.args[0] == "git.worktree.remove"
    if outcome == "blocked":
        assert response.status_code == 409
        await db.rollback()
        await db.refresh(item)
        assert item.delete_at is None and item.current_directory == "/worktree"
        return
    assert response.status_code == 204
    assert item.delete_at is not None and item.current_directory == "/repo"
    response = await client.post(f"/api/trash/sessions/{session_id}/restore")
    assert response.status_code == 200
    assert response.json() == {"worktreeRestored": outcome == "restored"}
    assert item.delete_at is None
    assert item.workspace_mode == ("worktree" if outcome == "restored" else "local")
    if outcome == "restored":
        assert item.coding_workspace["baseCommit"] == "b" * 40


async def test_trash_pauses_automations_and_restore_skips_missed_schedules(trash_tree, client):
    tree = trash_tree
    db = tree.db
    session = tree.records[0][0]
    past = datetime.now(UTC) - timedelta(days=1)
    automation = await AutomationService.create(db, user_id=tree.user.id, session_id=session.id,
        name="Report", task="Report", enabled=True, schedules=[{"run_at": past, "enabled": True}], now=past - timedelta(seconds=1))
    queued = await AutomationService.enqueue_manual(db, automation.id, tree.user.id)
    assert (await client.delete(f"/api/sessions/{session.id}")).status_code == 204
    session.trash_worktree_removal = {"impact": "a" * 64}
    await db.commit()
    await db.refresh(queued)
    assert queued.status == "cancelled"
    with pytest.raises(AutomationError, match="session.inTrash"):
        await AutomationService.enqueue_manual(db, automation.id, tree.user.id)
    assert await AutomationService.enqueue_due(db) == 0
    assert (await client.post(f"/api/trash/sessions/{session.id}/restore")).status_code == 204
    await db.refresh(session)
    assert session.trash_worktree_removal is None
    assert automation.enabled
    assert await AutomationService.enqueue_due(db) == 0
    assert list(await db.scalars(select(AutomationRun.id).where(AutomationRun.automation_id == automation.id))) == [queued.id]


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
