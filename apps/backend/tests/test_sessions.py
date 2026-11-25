import hashlib
import json
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend.agent.runtime import AgentRuntime
from mato_loop_backend.agent.session_settings import apply_run_session_settings
from mato_loop_backend.api.errors import ApiError
from mato_loop_backend.api.routes.sessions import (
    ActiveFileRequest,
    HomeSessionsRequest,
    SendMessageRequest,
    SessionCreateRequest,
    SessionOptionsRequest,
    SessionReadRequest,
    SessionUpdateRequest,
    cancel_run,
    create_session,
    delete_session,
    download_attachment,
    get_run,
    get_session,
    get_session_message_window,
    get_session_messages,
    home_sessions,
    list_sessions,
    mark_session_read,
    message_payload,
    run_payload,
    send_message,
    session_defaults,
    session_payload,
    stream_run_events,
    update_session,
)
from mato_loop_backend.db.session import get_db_session
from mato_loop_backend.files.service import FileRoot, RootFileService
from mato_loop_backend.files.storage import SessionStorage
from mato_loop_backend.media.attachments import AttachmentInput, AttachmentService
from mato_loop_backend.models import (
    AgentRun,
    ChatSession,
    ContextSummary,
    ExecutionAgent,
    Message,
    MessageAttachment,
    PermissionGrant,
    Project,
    TitleGenerationStatus,
    UserAccount,
)
from mato_loop_backend.security import require_user
from mato_loop_backend.sessions.service import (
    SessionServiceError,
    _latest_model_context,
    active_file_payload,
    message_attributions,
    session_message_page,
    session_message_window,
)
from mato_loop_backend.settings import (
    AgentProfilesSettings,
    AiProviderSettings,
    AiSettings,
    GeneralSettings,
    MemorySettings,
    SettingsStore,
    ToolSettings,
    TrustLevel,
    UserPreferences,
)
from mato_loop_backend.settings.models import (
    BUILD_PROFILE_ID,
    GENERAL_PROFILE_ID,
    PLAN_PROFILE_ID,
    AgentProfile,
    AgentProfilePermissions,
    LlmProviderConfig,
    ModelAvailability,
    ModelConfig,
    ModelDefaultsSettings,
)

pytestmark = pytest.mark.usefixtures("no_active_voice")


@pytest.mark.parametrize(("enabled", "stored", "expected"), [
    (True, "public", TrustLevel.PUBLIC),
    (True, "private", TrustLevel.PRIVATE),
    (False, "public", TrustLevel.PRIVATE),
    (False, "private", TrustLevel.PRIVATE),
])
async def test_session_trust_respects_model_distinction(monkeypatch, enabled, stored, expected):
    from mato_loop_backend.sessions.trust import effective_session_trust

    monkeypatch.setattr(SettingsStore, "load", AsyncMock(return_value=GeneralSettings(modelTrustEnabled=enabled)))
    assert await effective_session_trust(AsyncMock(), ChatSession(trust_level=stored)) == expected


async def test_disabled_agent_profile_cannot_be_selected_for_a_new_session(monkeypatch) -> None:
    from mato_loop_backend.sessions import service as session_service

    profile = AgentProfile(name="Paused", instructions="Work.", enabled=False)
    monkeypatch.setattr(session_service, "load_agent_profile", AsyncMock(return_value=profile))

    with pytest.raises(SessionServiceError) as exc_info:
        await session_service.validate_agent_profile(AsyncMock(), uuid4(), profile.id)

    assert exc_info.value.code == "agentProfile.selectedUnavailable"


def _user() -> UserAccount:
    return UserAccount(id=uuid4(), username="test", password_hash="hash")


@pytest.fixture(autouse=True)
def profile_model_defaults(monkeypatch):
    async def load_optional(db, settings_type, **_kwargs):
        if settings_type is AgentProfilesSettings:
            return AgentProfilesSettings()
        if settings_type is not ModelDefaultsSettings:
            return None
        ai = await SettingsStore.load(db, AiProviderSettings)
        return ModelDefaultsSettings(default_model_id=getattr(ai, "default_model_id", None))

    monkeypatch.setattr(SettingsStore, "load_optional", load_optional)


def _stored_app_tab(app_id: str, *, pinned: bool = False) -> dict:
    return {
        "tab_id": app_id,
        "kind": "app",
        "app_id": app_id,
        "pinned": pinned,
        "file_target": None,
    }


def _api_app_tab(app_id: str, *, pinned: bool = False) -> dict:
    return {
        "tabId": app_id,
        "kind": "app",
        "appId": app_id,
        "pinned": pinned,
    }


async def test_workspace_active_file_requires_a_normalized_absolute_posix_path() -> None:
    agent_id = uuid4()
    session = ChatSession(
        id=uuid4(),
        user_id=uuid4(),
        preferred_execution_agent_id=agent_id,
    )
    payload = {
        "root": "workspace",
        "executionAgentId": agent_id,
        "path": "/",
        "sha256": "a" * 64,
        "dirty": True,
    }
    db = AsyncMock(spec=AsyncSession)
    active_file = await active_file_payload(
        ActiveFileRequest.model_validate(payload), session, db
    )
    assert active_file is not None
    assert active_file["path"] == "/"

    for path in (
        "home/test",
        "/home\\test",
        "/home//test",
        "/home/./test",
        "/home/../test",
        "/home/\x1ftest",
        "/home/\x80test",
    ):
        payload["path"] = path
        with pytest.raises(SessionServiceError):
            await active_file_payload(
                ActiveFileRequest.model_validate(payload), session, db
            )


async def test_session_active_file_accepts_owned_same_project_session() -> None:
    user_id, project_id, owner_id = uuid4(), uuid4(), uuid4()
    session = ChatSession(id=uuid4(), user_id=user_id, project_id=project_id)
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = owner_id
    payload = ActiveFileRequest.model_validate(
        {
            "root": "session",
            "rootIdentity": str(owner_id),
            "path": "notes.md",
            "sha256": "a" * 64,
            "dirty": False,
        }
    )

    active_file = await active_file_payload(payload, session, db)

    assert active_file["rootIdentity"] == str(owner_id)
    statement = db.scalar.await_args.args[0]
    assert {owner_id, user_id, project_id}.issubset(statement.compile().params.values())
    assert "FOR UPDATE" in str(statement)


async def test_session_active_file_rejects_session_outside_current_project() -> None:
    session = ChatSession(id=uuid4(), user_id=uuid4(), project_id=uuid4())
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = None
    payload = ActiveFileRequest.model_validate(
        {
            "root": "session",
            "rootIdentity": str(uuid4()),
            "path": "notes.md",
            "sha256": "a" * 64,
            "dirty": False,
        }
    )

    with pytest.raises(SessionServiceError, match="validation.invalid"):
        await active_file_payload(payload, session, db)


def test_send_message_http_response_accepts_nested_message(monkeypatch) -> None:
    from mato_loop_backend.api.routes import sessions as sessions_route

    user = _user()
    session = ChatSession(
        id=uuid4(), user_id=user.id, title="Test", preferred_execution_agent_id=uuid4()
    )
    run = AgentRun(
        id=uuid4(),
        session_id=session.id,
        status="queued",
        model_name=None,
    )
    message = Message(
        id=uuid4(),
        session_id=session.id,
        role="user",
        content="Nachricht",
    )
    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = [session, False, None, message.id, message]
    monkeypatch.setattr(AgentRuntime, "start", AsyncMock(return_value=run))
    provider = LlmProviderConfig(name="Local", adapter_type="openai-compatible")
    model = ModelConfig(provider_id=provider.id, model_id="agent")
    session.model_config_id = model.id
    monkeypatch.setattr(
        SettingsStore,
        "load",
        AsyncMock(
            return_value=AiSettings(
                providers=[provider], models=[model], default_model_id=model.id
            )
        ),
    )

    async def override_db():
        yield db

    app = FastAPI()
    app.include_router(sessions_route.router, prefix="/api")
    app.dependency_overrides[get_db_session] = override_db
    app.dependency_overrides[require_user] = lambda: user

    with TestClient(app) as client:
        response = client.post(
            f"/api/sessions/{session.id}/messages",
            json={
                "content": "Nachricht",
                "active_file": {
                    "root": "workspace",
                    "execution_agent_id": str(session.preferred_execution_agent_id),
                    "path": "/workspace/finish_diagram.py",
                    "sha256": "a" * 64,
                    "dirty": False,
                },
            },
        )

    assert response.status_code == 202
    assert response.json() == {
        "runId": str(run.id),
        "agentProfileId": None,
        "modelName": None,
        "activeDurationMs": 0,
        "activeDurationRunning": False,
        "activeTimingRevision": 0,
        "message": message_payload(message),
    }


@pytest.mark.parametrize("lookup_ids", [False, True])
async def test_session_list_first_page_has_cursor_and_payload_metadata(lookup_ids) -> None:
    user = _user()
    session = ChatSession(
        id=uuid4(),
        user_id=user.id,
        title="Fortgesetzte Session",
        memory_mode="disabled",
        created_at=datetime(2026, 7, 1, 8, 0, tzinfo=UTC),
        updated_at=datetime(2026, 7, 2, 8, 0, tzinfo=UTC),
        last_read_at=datetime(2026, 7, 23, 8, 0, tzinfo=UTC),
    )
    next_session = ChatSession(
        id=uuid4(),
        user_id=user.id,
        title="Nächste Session",
        memory_mode="disabled",
        created_at=datetime(2026, 6, 1, 8, 0, tzinfo=UTC),
        updated_at=datetime(2026, 6, 2, 8, 0, tzinfo=UTC),
    )
    last_message_at = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
    execution_result = MagicMock()
    execution_result.all.return_value = [
        (session, last_message_at, last_message_at, "running", session.created_at, session.created_at),
        (next_session, None, None, None, next_session.created_at, None),
    ]
    db = AsyncMock(spec=AsyncSession)
    db.execute.return_value = execution_result

    result = await list_sessions(
        user, db, page_size=1,
        session_ids=[session.id, next_session.id] if lookup_ids else None,
        include_archived=lookup_ids,
    )

    assert set(result) == {"items", "nextCursor"}
    assert len(result["items"]) == 1
    assert result["nextCursor"] is not None
    assert result["items"][0]["createdAt"] == session.created_at.isoformat()
    assert result["items"][0]["updatedAt"] == session.updated_at.isoformat()
    assert result["items"][0]["lastMessageAt"] == last_message_at.isoformat()
    assert result["items"][0]["lastUserMessageAt"] == session.created_at.isoformat()
    assert result["items"][0]["hasUnreadMessages"] is True
    assert result["items"][0]["activeRunStatus"] == "running"
    statement = db.execute.await_args.args[0]
    sql = str(statement)
    assert "max(messages.created_at)" not in sql
    assert "messages.session_id = chat_sessions.id AND messages.role" in sql
    assert "ORDER BY messages.created_at DESC, messages.id DESC" in sql
    assert "messages.role IN" in sql
    assert "agent_runs.status IN" in sql
    assert "agent_runs.created_at DESC, agent_runs.id DESC" in sql
    assert "LIMIT" in sql
    activity = sql[sql.index("coalesce((SELECT messages.created_at"):sql.index(" AS activity_at")]
    assert f"{activity} DESC" in sql
    assert "chat_sessions.updated_at DESC" not in sql
    assert 2 in statement.compile().params.values()
    assert user.id in statement.compile().params.values()
    assert "chat_sessions.parent_session_id IS NULL" in sql
    assert "chat_sessions.delete_at IS NOT NULL" in sql
    assert "projects.delete_at IS NOT NULL" in sql
    assert ("settings.key" in sql) is not lookup_ids
    if lookup_ids:
        assert [session.id, next_session.id] in statement.compile().params.values()
        assert "chat_sessions.id IN" in sql
        assert "chat_sessions.archived_at IS NULL" not in sql


def test_session_status_view_preserves_navigation_and_list_state_without_full_serialization(monkeypatch):
    from mato_loop_backend.api.routes import sessions as sessions_route

    user = _user()
    now = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
    session = ChatSession(
        id=uuid4(), user_id=user.id, project_id=uuid4(), title="Test", is_pinned=True,
        created_at=now, updated_at=now, title_generation_status="completed",
    )
    db = AsyncMock(spec=AsyncSession)
    result = MagicMock()
    result.all.return_value = [(session, now, now, "running", now, now, True)]
    db.execute.return_value = result
    full = AsyncMock(side_effect=AssertionError("Status view should not build the full payload"))
    monkeypatch.setattr(sessions_route, "_session_list_serializer", full)
    app = FastAPI()
    app.include_router(sessions_route.router, prefix="/api")
    app.dependency_overrides[get_db_session] = lambda: db
    app.dependency_overrides[require_user] = lambda: user

    with TestClient(app) as client:
        response = client.get("/api/sessions", params={
            "sessionIds": str(session.id), "includeArchived": "true", "view": "status",
        })

    assert response.status_code == 200
    assert response.json() == {"nextCursor": None, "items": [{
        "id": str(session.id), "projectId": str(session.project_id), "parentSessionId": None,
        "readOnly": False, "archived": True, "archivedAt": None, "title": "Test",
        "titleGenerationStatus": "completed", "pinned": True,
        "createdAt": now.isoformat(), "updatedAt": now.isoformat(), "lastMessageAt": now.isoformat(),
        "lastUserMessageAt": now.isoformat(), "hasUnreadMessages": True, "activeRunStatus": "running",
    }]}
    db.execute.assert_awaited_once()
    statement = db.execute.await_args.args[0]
    sql = str(statement)
    assert user.id in statement.compile().params.values()
    assert "chat_sessions.parent_session_id IS NULL" in sql
    assert "chat_sessions.delete_at IS NULL" in sql and "projects.delete_at IS NULL" in sql
    assert "WITH RECURSIVE" not in sql
    assert "chat_sessions.control_center" not in sql and "chat_sessions.session_profile" not in sql


@pytest.mark.parametrize("session_ids", [[str(uuid4())] * 101, ["invalid-uuid"]])
def test_session_list_rejects_invalid_session_ids(session_ids):
    from mato_loop_backend.api.routes import sessions as sessions_route

    db = AsyncMock(spec=AsyncSession)
    app = FastAPI()
    app.include_router(sessions_route.router, prefix="/api")
    app.dependency_overrides[get_db_session] = lambda: db
    app.dependency_overrides[require_user] = _user
    with TestClient(app) as client:
        response = client.get("/api/sessions", params=[("sessionIds", item) for item in session_ids])
    assert response.status_code == 422
    db.execute.assert_not_awaited()


@pytest.mark.parametrize("sort", ["pinned", "recent"])
async def test_session_list_next_page_uses_full_keyset_and_preserves_order(sort) -> None:
    user = _user()
    pinned = ChatSession(
        id=uuid4(),
        user_id=user.id,
        title="Angeheftet",
        is_pinned=sort == "pinned",
        memory_mode="disabled",
        created_at=datetime(2026, 7, 20, 8, 0, tzinfo=UTC),
    )
    overflow = ChatSession(
        id=uuid4(),
        user_id=user.id,
        title="Weitere Session",
        memory_mode="disabled",
        created_at=datetime(2026, 7, 19, 8, 0, tzinfo=UTC),
    )
    first_result = MagicMock()
    first_result.all.return_value = [
        (pinned, None, pinned.created_at if sort == "recent" else None, None, pinned.created_at, None),
        (overflow, None, None, None, overflow.created_at, None),
    ]
    first_db = AsyncMock(spec=AsyncSession)
    first_db.execute.return_value = first_result
    first_page = await list_sessions(user, first_db, page_size=1, sort=sort)

    second_result = MagicMock()
    second_result.all.return_value = [
        (overflow, None, None, "waiting_approval", overflow.created_at, None)
    ]
    second_db = AsyncMock(spec=AsyncSession)
    second_db.execute.return_value = second_result
    second_page = await list_sessions(
        user, second_db, page_size=2, cursor=first_page["nextCursor"], sort=sort,
    )

    assert [item["id"] for item in second_page["items"]] == [str(overflow.id)]
    assert second_page["nextCursor"] is None
    statement = second_db.execute.await_args.args[0]
    sql = str(statement)
    activity_start = "greatest(" if sort == "recent" else "coalesce((SELECT messages.created_at"
    activity_sql = sql[sql.index(activity_start):sql.index(" AS activity_at")]
    if sort == "pinned":
        assert "chat_sessions.is_pinned IS false OR chat_sessions.is_pinned IS true" in sql
        assert sql.index("chat_sessions.is_pinned DESC") < sql.index(
            f"{activity_sql} DESC"
        )
    else:
        assert "chat_sessions.is_pinned IS" not in sql
        assert "chat_sessions.is_pinned DESC" not in str(first_db.execute.await_args.args[0]) + sql
        assert "chat_sessions.last_read_at" not in str(statement._order_by_clauses)
        assert "CASE WHEN" not in sql
        assert "greatest(coalesce((SELECT messages.created_at" in sql
        assert "ORDER BY agent_runs.completed_at DESC" in sql
        assert "messages.id = coalesce(agent_runs.result_message_id, (SELECT messages_1.id" in sql
        assert "max(question_requests.created_at)" in sql
        assert "max(approval_requests.created_at)" in sql
        assert "question_requests.status" not in sql
        assert "approval_requests.status" not in sql
        assert second_page["items"][0]["activityAt"] == overflow.created_at.isoformat()
    assert f"{activity_sql} <" in sql
    assert f"{activity_sql} =" in sql
    assert "chat_sessions.id <" in sql
    assert sql.index(f"{activity_sql} DESC") < sql.index("chat_sessions.id DESC")
    assert user.id in statement.compile().params.values()
    assert pinned.created_at in statement.compile().params.values()
    assert pinned.id in statement.compile().params.values()
    with pytest.raises(ApiError) as exc_info:
        await list_sessions(user, second_db, cursor=first_page["nextCursor"], sort="recent" if sort == "pinned" else "pinned")
    assert exc_info.value.field == "cursor"


@pytest.mark.parametrize("home", [False, True])
async def test_session_list_batches_payload_inputs_without_changing_payloads(monkeypatch, home) -> None:
    from mato_loop_backend.api.routes import sessions as route

    user = _user()
    project_id, agent_id, revoked_agent_id = uuid4(), uuid4(), uuid4()
    provider = LlmProviderConfig(name="Test", adapter_type="openai-compatible")
    model = ModelConfig(provider_id=provider.id, model_id="test", name="Test")
    denied_model = model.model_copy(update={"id": uuid4(), "memory_policy": "deny"})
    ai = AsyncMock(return_value=AiSettings(
        providers=[provider], models=[model, denied_model], default_model_id=model.id,
    ))
    memory = AsyncMock(return_value=MemorySettings())
    monkeypatch.setattr(route, "load_effective_ai", ai)
    monkeypatch.setattr(SettingsStore, "load", memory)
    monkeypatch.setattr(route.execution_agent_connections, "snapshot", lambda _id: {"filesystem.read": {}})
    sessions = [ChatSession(
        id=uuid4(), user_id=user.id, project_id=project_id,
        preferred_execution_agent_id=selected_agent,
        model_config_id=selected_model, memory_mode=mode,
        created_at=datetime(2026, 7, 1, tzinfo=UTC),
    ) for mode, selected_agent, selected_model in [
        ("full", agent_id, model.id),
        ("read_only", agent_id, denied_model.id),
        ("disabled", revoked_agent_id, None),
    ]]
    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = [
        project_id, SimpleNamespace(id=agent_id),
        project_id, SimpleNamespace(id=agent_id),
        project_id, None,
    ]
    expected = [await route._session_payload(db, item, last_message_at=None, has_unread_messages=False)
                for item in sessions]
    ai.reset_mock()
    memory.reset_mock()
    db.scalar.side_effect = AssertionError("Per-session state query")
    db.scalars.side_effect = [
        MagicMock(all=MagicMock(return_value=[project_id])),
        MagicMock(all=MagicMock(return_value=[agent_id])),
    ]
    db.execute.return_value = MagicMock()
    db.execute.return_value.all.return_value = [
        (item, None, None, None, item.created_at, None) for item in sessions
    ]

    result = (await home_sessions(user, db, HomeSessionsRequest()) if home
              else await list_sessions(user, db, include_archived=True))

    payloads = [{key: value for key, value in item.items()
                 if key not in {"activeRunStatus", "lastUserMessageAt", "activityAt"}}
                for item in result["items"]]
    assert (payloads, ai.await_count, memory.await_count, db.scalars.await_count) == (
        expected, 1, 1, 2,
    )


async def test_home_sessions_include_relevant_and_retained_sessions_without_page_limit() -> None:
    user = _user()
    retained_id = uuid4()
    db = AsyncMock(spec=AsyncSession)
    result = MagicMock()
    result.all.return_value = []
    db.execute.return_value = result
    await home_sessions(user, db, HomeSessionsRequest(retainedIds=[retained_id]))
    statement = db.execute.await_args.args[0]
    sql = str(statement)
    params = statement.compile().params.values()
    assert (
        "row_number() OVER" in sql
        and "home_sessions.home_rank <=" in sql
        and "home_sessions.home_relevant" in sql
        and "approval_requests.status" in sql
        and "question_requests.status" in sql
        and "> chat_sessions.last_read_at" in sql
        and "home_sessions.id IN" in sql
        and statement._limit_clause is None
        and user.id in params and [retained_id] in params and 6 in params
    )


async def test_session_list_rejects_malformed_cursor() -> None:
    db = AsyncMock(spec=AsyncSession)

    with pytest.raises(ApiError) as exc_info:
        await list_sessions(_user(), db, cursor="not-a-cursor")

    assert exc_info.value.status_code == 400
    assert exc_info.value.code == "validation.invalid"
    assert exc_info.value.field == "cursor"
    db.execute.assert_not_awaited()


async def test_session_list_scopes_projects_and_standalone_sessions() -> None:
    user = _user()
    project_id = uuid4()
    result = MagicMock()
    result.all.return_value = []
    project_db = AsyncMock(spec=AsyncSession)
    project_db.scalar.return_value = project_id
    project_db.execute.return_value = result

    await list_sessions(user, project_db, project_id=project_id)

    project_statement = project_db.execute.await_args.args[0]
    assert project_id in project_statement.compile().params.values()
    assert "chat_sessions.project_id = :project_id_1" in str(project_statement)
    owner_statement = project_db.scalar.await_args.args[0]
    assert user.id in owner_statement.compile().params.values()

    standalone_db = AsyncMock(spec=AsyncSession)
    standalone_db.execute.return_value = result
    await list_sessions(user, standalone_db, standalone=True)
    assert "chat_sessions.project_id IS NULL" in str(
        standalone_db.execute.await_args.args[0]
    )
    assert "NOT (EXISTS" in str(standalone_db.execute.await_args.args[0])


async def test_failed_project_move_restores_the_session_tree(monkeypatch) -> None:
    from mato_loop_backend.api.routes import sessions as sessions_route

    user = _user()
    source_project_id = uuid4()
    session = ChatSession(
        id=uuid4(), user_id=user.id, title="Move", project_id=source_project_id
    )
    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = [session.id, None, 0]
    db.scalars.return_value = MagicMock(all=MagicMock(return_value=[]))
    db.commit.side_effect = RuntimeError("commit failed")
    persisted_location = MagicMock()
    persisted_location.one_or_none.return_value = (source_project_id,)
    db.execute.side_effect = [persisted_location]
    storage = MagicMock()
    receipt = MagicMock(
        journal=None,
        source_project_id=source_project_id,
        target_project_id=None,
    )
    storage.stage_session_move.return_value = receipt
    monkeypatch.setattr(sessions_route, "SessionStorage", lambda: storage)
    monkeypatch.setattr(
        sessions_route, "_mutable_session", AsyncMock(return_value=session)
    )

    with pytest.raises(RuntimeError, match="commit failed"):
        await update_session(
            session.id,
            SessionUpdateRequest(project_id=None),
            user,
            db,
        )

    storage.stage_session_move.assert_called_once_with(
        session.id, source_project_id, None
    )
    storage.rollback_session_move.assert_called_once_with(receipt)
    storage.finalize_session_move.assert_not_called()
    assert len(db.execute.await_args_list) == 1
    assert db.rollback.await_count == 2


async def test_standalone_project_move_preserves_managed_file_identity(monkeypatch) -> None:
    from mato_loop_backend.api.routes import sessions as sessions_route

    user = _user()
    project_id = uuid4()
    session = ChatSession(id=uuid4(), user_id=user.id, title="Move")
    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = [session.id, project_id, 0]
    db.scalars.side_effect = [
        MagicMock(all=MagicMock(return_value=[])),
        MagicMock(all=MagicMock(return_value=[])),
        MagicMock(all=MagicMock(return_value=[])),
    ]
    storage = MagicMock()
    receipt = storage.stage_session_move.return_value
    monkeypatch.setattr(sessions_route, "SessionStorage", lambda: storage)
    monkeypatch.setattr(
        sessions_route, "_mutable_session", AsyncMock(return_value=session)
    )
    monkeypatch.setattr(sessions_route, "_session_payload", AsyncMock(return_value={}))

    await update_session(
        session.id,
        SessionUpdateRequest(project_id=project_id),
        user,
        db,
    )

    storage.stage_session_move.assert_called_once_with(
        session.id,
        None,
        project_id,
    )
    storage.finalize_session_move.assert_called_once_with(receipt)
    storage.rollback_session_move.assert_not_called()
    db.execute.assert_not_awaited()
    assert session.project_id == project_id


async def test_project_move_rejects_files_referenced_by_another_session(monkeypatch) -> None:
    from mato_loop_backend.api.routes import sessions as sessions_route

    user = _user()
    session = ChatSession(id=uuid4(), user_id=user.id, title="Move", project_id=uuid4())
    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = [session.id, None, 2]
    db.scalars.return_value = MagicMock(all=lambda: [])
    storage = MagicMock()
    monkeypatch.setattr(sessions_route, "SessionStorage", lambda: storage)
    monkeypatch.setattr(
        sessions_route, "_mutable_session", AsyncMock(return_value=session)
    )

    with pytest.raises(ApiError) as exc_info:
        await update_session(
            session.id, SessionUpdateRequest(project_id=None), user, db
        )

    assert exc_info.value.code == "session.filesReferenced"
    assert exc_info.value.params == {"referenceCount": 2}
    storage.stage_session_move.assert_not_called()


async def test_project_move_rejects_references_to_another_sessions_files(monkeypatch) -> None:
    from mato_loop_backend.api.routes import sessions as sessions_route

    user = _user()
    session = ChatSession(id=uuid4(), user_id=user.id, title="Move", project_id=uuid4())
    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = [session.id, None, 1]
    db.scalars.return_value = MagicMock(all=lambda: [])
    storage = MagicMock()
    monkeypatch.setattr(sessions_route, "SessionStorage", lambda: storage)
    monkeypatch.setattr(
        sessions_route, "_mutable_session", AsyncMock(return_value=session)
    )

    with pytest.raises(ApiError) as exc_info:
        await update_session(
            session.id, SessionUpdateRequest(project_id=None), user, db
        )

    assert exc_info.value.code == "session.filesReferenced"
    storage.stage_session_move.assert_not_called()


@pytest.mark.parametrize("has_message", [True, False])
async def test_mark_session_read_uses_owned_assistant_message_monotonically(has_message) -> None:
    user = _user()
    session = ChatSession(
        id=uuid4(),
        user_id=user.id,
        title="Gelesene Session",
        last_read_at=datetime(2026, 7, 23, 8, 0, tzinfo=UTC),
    )
    message_id = uuid4()
    read_at = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = [session, False, read_at] if has_message else [session, False]

    result = await mark_session_read(
        session.id,
        SessionReadRequest(throughMessageId=message_id if has_message else None),
        user,
        db,
    )

    assert result == {"read": True}
    statements = [call.args[0] for call in db.scalar.await_args_list]
    assert "FOR UPDATE" in str(statements[0])
    if not has_message:
        assert "UPDATE notifications" in str(db.execute.await_args.args[0])
        return
    assert message_id in statements[2].compile().params.values()
    update_statement = db.execute.await_args_list[0].args[0]
    sql = str(update_statement)
    assert "chat_sessions.last_read_at <" in sql
    assert "updated_at=chat_sessions.updated_at" in sql.replace(" ", "")
    assert read_at in update_statement.compile().params.values()
    notification_update = db.execute.await_args_list[1].args[0]
    assert "notifications.created_at <=" in str(notification_update)
    db.commit.assert_awaited_once()


@pytest.mark.parametrize("content,general_template,directory,other_computer", [
    ("Erste Nachricht", False, None, False), ("/git-init", False, None, False),
    ("Erste Nachricht", True, None, False), ("Erste Nachricht", False, "/other", False),
    ("Erste Nachricht", False, None, True),
])
@pytest.mark.parametrize("read_policy,external_policy,trust", [
    ("auto", "ask", "private"), ("ask", "ask", "private"),
    ("disabled", "ask", "private"), ("auto", "disabled", "private"),
    ("auto", "ask", "public"),
])
async def test_session_is_created_with_first_message_and_run(
    monkeypatch, content, general_template, directory, other_computer, read_policy, external_policy, trust,
) -> None:
    from mato_loop_backend.api.routes import sessions as sessions_route
    from mato_loop_backend.settings.session_profiles import CODING_TEMPLATE_ID, DEFAULT_TEMPLATE_ID

    user = _user()
    provider_id = uuid4()
    provider = LlmProviderConfig(
        id=provider_id,
        name="Test",
        adapter_type="openai-compatible",
    )
    model = ModelConfig(
        provider_id=provider_id,
        model_id="test-model",
        name="Test",
        trust_level=TrustLevel(trust),
        discovery_metadata={"reasoning_efforts": ["low", "high"], "default_reasoning_effort": "low"},
    )
    ai_settings = AiSettings(providers=[provider], models=[model], default_model_id=model.id)
    memory_settings = MemorySettings()
    project = Project(
        id=uuid4(),
        user_id=user.id,
        name="Project",
        default_execution_agent_id=uuid4(),
        default_current_directory="/srv/project",
        default_session_template_id=CODING_TEMPLATE_ID,
        git_workspace_mode="worktree" if general_template else "local",
    )
    selected_computer = uuid4() if other_computer else project.default_execution_agent_id

    async def load_settings(_db, settings_type, **_kwargs):
        if settings_type is AiProviderSettings:
            return ai_settings
        if settings_type is UserPreferences:
            return UserPreferences()
        return memory_settings

    run = AgentRun(
        id=uuid4(),
        session_id=uuid4(),
        status="queued",
        model_name="Test",
    )
    start = AsyncMock(return_value=run)
    monkeypatch.setattr(SettingsStore, "load", load_settings)
    monkeypatch.setattr(AgentRuntime, "start", start)
    tools = ToolSettings()
    tools._agent_permissions = AgentProfilePermissions(policies={
        "filesystem.read": read_policy, "working_directory.external": external_policy,
    })
    monkeypatch.setattr("mato_loop_backend.sessions.service.load_agent_profile_tools", AsyncMock(return_value=(tools, None)))
    validate_execution = AsyncMock(return_value=(directory or "/srv/project", None))
    monkeypatch.setattr(
        "mato_loop_backend.sessions.service.validate_execution_preferences",
        validate_execution,
    )
    storage = MagicMock()
    monkeypatch.setattr(sessions_route, "SessionStorage", lambda: storage)
    db = AsyncMock(spec=AsyncSession)
    db.add.side_effect = lambda session: setattr(session, "id", uuid4())
    persisted_message = Message(
        id=uuid4(),
        session_id=run.session_id,
        role="user",
        content=content,
    )
    db.scalar.side_effect = [project, persisted_message, None, None]

    payload = SessionCreateRequest(
        content=f"  {content}  ",
        workspace_mode="worktree" if directory or content == "/git-init" else None,
        **({"current_directory": directory} if directory else {}),
        **({"preferred_execution_agent_id": selected_computer, "current_directory": "/srv/project"} if other_computer else {}),
        project_id=project.id,
        session_template_id=DEFAULT_TEMPLATE_ID if general_template else None,
        reasoning_effort="low",
        control_center={
            "tabs": [
                {"app_id": "files", "pinned": False},
                {"app_id": "notifications", "pinned": True},
            ],
            "active_app_id": "notifications",
        },
    )
    if directory:
        with pytest.raises(ApiError, match="git.projectWorkspaceRequired"):
            await create_session(payload, user, db)
        return
    result = await create_session(payload, user, db)

    session = db.add.call_args_list[0].args[0]
    grants = [call.args[0] for call in db.add.call_args_list if isinstance(call.args[0], PermissionGrant)]
    assert [(grant.session_id, grant.capability, grant.resource_type, json.loads(grant.resource_scope),
             grant.agent_profile_id, grant.trust_level, grant.run_id) for grant in grants] == ([
        (session.id, "category.read", "execution_path",
         {"agentId": str(project.default_execution_agent_id), "path": "/srv/project", "recursive": True},
         session.agent_profile_id, trust, None),
    ] if read_policy == "auto" and external_policy != "disabled" and not other_computer else [])
    assert session.agent_profile_id == (BUILD_PROFILE_ID if content == "/git-init" else GENERAL_PROFILE_ID if general_template else PLAN_PROFILE_ID)
    assert session.workspace_mode == "local"
    assert session.model_config_id is None
    assert session.reasoning_effort == "low"
    assert session.user_id == user.id
    assert session.memory_mode == ("full" if general_template else "read_only")
    assert session.web_search_policy == "available"
    assert session.preferred_execution_agent_id == selected_computer
    assert session.current_directory == "/srv/project"
    assert session.session_profile["template_id"] == str(DEFAULT_TEMPLATE_ID if general_template else CODING_TEMPLATE_ID)
    assert session.control_center == {
        "tabs": [
            _stored_app_tab("session"),
            _stored_app_tab("files"),
            _stored_app_tab("notifications", pinned=True),
        ],
        "active_app_id": "notifications",
        "open": False,
        "file_view": None,
        "file_views": {"session": None, "filesystem": None},
    }
    assert session.title_generation_status == TitleGenerationStatus.PENDING.value
    start.assert_awaited_once_with(
        db,
        session,
        content,
        run_id=None,
        attachments=None,
        active_file=None,
    )
    assert result["session"]["id"] == str(session.id)
    assert result["session"]["titleGenerationStatus"] == "pending"
    assert result["session"]["webSearchPolicy"] == "available"
    assert result["session"]["reasoningEffort"] == "low"
    assert result["session"]["trustLevel"] is None
    assert result["runId"] == str(run.id)
    assert result["modelName"] == "Test"
    assert result["message"]["id"] == str(persisted_message.id)
    validate_execution.assert_awaited_once_with(
        db, user.id, selected_computer, "/srv/project", defer_unavailable=True,
    )
    storage.provision.assert_called_once_with(session.id, project.id, user_id=user.id)


async def test_session_creation_rejects_worktree_with_disabled_template(monkeypatch) -> None:
    monkeypatch.setattr(SettingsStore, "load", AsyncMock(side_effect=lambda _db, kind, **_: kind()))
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = None
    with pytest.raises(ApiError, match="sessionTemplate.worktreesDisabled"):
        await create_session(SessionCreateRequest(content=None, workspace_mode="worktree"), _user(), db)


@pytest.mark.parametrize("with_model", [False, True])
async def test_opening_an_app_creates_an_empty_session_with_storage(monkeypatch, with_model) -> None:
    from mato_loop_backend.api.routes import sessions as sessions_route

    user = _user()
    provider = LlmProviderConfig(name="Test", adapter_type="openai-compatible")
    model = ModelConfig(provider_id=provider.id, model_id="test-model")
    ai_settings = AiSettings(
        providers=[provider] if with_model else [],
        models=[model] if with_model else [],
        default_model_id=model.id if with_model else None,
    )

    async def load_settings(_db, settings_type, **_kwargs):
        return ai_settings if settings_type is AiProviderSettings else MemorySettings()

    monkeypatch.setattr(SettingsStore, "load", load_settings)
    start = AsyncMock()
    monkeypatch.setattr(AgentRuntime, "start", start)
    storage = MagicMock()
    monkeypatch.setattr(sessions_route, "SessionStorage", lambda: storage)
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = None

    result = await create_session(SessionCreateRequest(
        content=None,
        gitFetchBeforeActions=False,
        control_center={
            "tabs": [{"app_id": "session", "pinned": False}, {"app_id": "browser", "pinned": False}],
            "active_app_id": "browser",
            "open": True,
        },
    ), user, db)

    session = db.add.call_args.args[0]
    storage.provision.assert_called_once_with(session.id, None, user_id=user.id)
    db.commit.assert_awaited_once()
    start.assert_not_awaited()
    assert result["session"]["id"] == str(session.id)
    assert result["session"]["controlCenter"]["activeAppId"] == "browser"
    assert result["session"]["titleGenerationStatus"] == "pending"
    assert result["session"]["lastMessageAt"] is None
    assert "message" not in result and "runId" not in result
    assert session.coding_workspace["fetchBeforeActions"] is False


async def test_initial_worktree_receives_fetch_override_and_persists_warning_before_run(monkeypatch):
    from mato_loop_backend.api.routes import sessions as sessions_route
    from mato_loop_backend.settings.session_profiles import CODING_TEMPLATE_ID

    user = _user()
    project = Project(id=uuid4(), user_id=user.id, default_execution_agent_id=uuid4(),
                      default_current_directory="/project", default_session_template_id=CODING_TEMPLATE_ID)
    monkeypatch.setattr(SettingsStore, "load", AsyncMock(return_value=AiSettings()))
    monkeypatch.setattr(sessions_route, "SessionStorage", MagicMock)
    monkeypatch.setattr("mato_loop_backend.sessions.service.validate_execution_preferences",
                        AsyncMock(return_value=("/project", "auto")))

    async def select_workspace(db, session, *_args, **_kwargs):
        assert session.coding_workspace["fetchBeforeActions"] is False
        session.current_directory = "/worktree"
        session.coding_workspace = {**session.coding_workspace, "mode": "worktree", "path": "/worktree",
                                    "baseCommit": "a" * 40, "warningCode": "git.fetchFailed"}
        return "auto"

    monkeypatch.setattr("mato_loop_backend.sessions.coding.select_session_workspace", select_workspace)
    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = [project, None]

    async def start(*_args, **_kwargs):
        assert [item.args[0].role for item in db.add.call_args_list if isinstance(item.args[0], Message)] == ["assistant"]
        raise RuntimeError("Run boundary reached")

    monkeypatch.setattr(AgentRuntime, "start", start)
    with pytest.raises(RuntimeError, match="Run boundary reached"):
        await create_session(SessionCreateRequest(content="Task", project_id=project.id,
            workspace_mode="worktree", gitFetchBeforeActions=False), user, db)


async def test_first_session_retry_returns_canonical_session_and_attachment() -> None:
    user = _user()
    run_id = uuid4()
    session = ChatSession(
        id=uuid4(),
        user_id=user.id,
        title="Erste Session",
        memory_mode="disabled",
    )
    message = Message(
        id=uuid4(),
        session_id=session.id,
        role="user",
        content="Erste Nachricht",
    )
    message.attachments = [
        MessageAttachment(
            id=uuid4(),
            message_id=message.id,
            position=0,
            kind="text",
            mime_type="text/plain",
            size_bytes=7,
            sha256="a" * 64,
            file_root_id=session.id,
            file_id=uuid4(),
            file_revision=1,
            file_path="attachments/note/note.txt",
            filename="note.txt",
        )
    ]
    run = AgentRun(
        id=run_id,
        session_id=session.id,
        input_message_id=message.id,
        status="completed",
        model_name="Test",
    )
    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = [run, session, False, message]

    result = await create_session(
        SessionCreateRequest(content="ignored retry content", run_id=run_id), user, db
    )

    assert result["session"]["id"] == str(session.id)
    assert result["runId"] == str(run_id)
    assert result["message"]["id"] == str(message.id)
    assert result["message"]["attachments"][0]["filename"] == "note.txt"
    db.add.assert_not_called()


@pytest.mark.parametrize("failure_stage", ["insert", "runtime"])
async def test_concurrent_first_session_retry_recovers_winning_run(monkeypatch, failure_stage) -> None:
    from mato_loop_backend.api.routes import sessions as sessions_route

    user = _user()
    run_id = uuid4()
    provider = LlmProviderConfig(name="Local", adapter_type="openai-compatible")
    model = ModelConfig(provider_id=provider.id, model_id="agent")
    winner = ChatSession(
        id=uuid4(),
        user_id=user.id,
        title="Gewinner",
        memory_mode="disabled",
        model_config_id=model.id,
    )
    message = Message(
        id=uuid4(), session_id=winner.id, role="user", content="Nachricht"
    )
    run = AgentRun(
        id=run_id,
        session_id=winner.id,
        input_message_id=message.id,
        status="queued",
    )
    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = [None, run, winner, False, message]
    monkeypatch.setattr(
        SettingsStore,
        "load",
        AsyncMock(
            return_value=AiSettings(
                providers=[provider], models=[model], default_model_id=model.id
            )
        ),
    )
    monkeypatch.setattr(
        AgentRuntime,
        "start",
        AsyncMock(side_effect=IntegrityError("insert", {}, Exception("duplicate"))),
    )
    if failure_stage == "insert":
        db.flush.side_effect = IntegrityError("insert", {}, Exception("duplicate"))
    storage = MagicMock()
    monkeypatch.setattr(sessions_route, "SessionStorage", lambda: storage)

    result = await create_session(
        SessionCreateRequest(content="Nachricht", run_id=run_id), user, db
    )

    assert result["session"]["id"] == str(winner.id)
    assert result["runId"] == str(run_id)
    db.rollback.assert_awaited()
    if failure_stage == "runtime":
        storage.remove_session.assert_called_once()
    else:
        storage.remove_session.assert_not_called()


async def test_session_defaults_report_effective_memory_state(monkeypatch) -> None:
    user = _user()
    provider = LlmProviderConfig(name="Test", adapter_type="openai-compatible")
    model = ModelConfig(
        provider_id=provider.id,
        model_id="test-model",
        supports_tools=False,
    )
    ai_settings = AiSettings(
        providers=[provider], models=[model], default_model_id=model.id
    )
    memory_settings = MemorySettings(enabled=False)

    async def load_settings(_db, settings_type, **_kwargs):
        if settings_type is AiProviderSettings:
            return ai_settings
        if settings_type is UserPreferences:
            return UserPreferences()
        return memory_settings

    monkeypatch.setattr(SettingsStore, "load", load_settings)

    result = await session_defaults(user, AsyncMock(spec=AsyncSession), SessionOptionsRequest())

    assert result["memoryMode"] == "full"
    assert result["effectiveMemoryMode"] == "disabled"
    assert result["memoryToolsAvailable"] is False
    assert result["memoryUnavailableReason"] == "memory.disabled"
    assert result["preferredExecutionAgentId"] is None
    assert "trustLevel" not in result


async def test_session_defaults_use_project_execution_preferences(monkeypatch) -> None:
    user = _user()
    agent = ExecutionAgent(id=uuid4(), user_id=user.id, alias="Workstation")
    project = Project(
        id=uuid4(),
        user_id=user.id,
        name="Project",
        default_execution_agent_id=agent.id,
        default_current_directory="/srv/project",
    )

    async def load_settings(_db, settings_type, **_kwargs):
        if settings_type is AiProviderSettings:
            return AiSettings()
        if settings_type is UserPreferences:
            return UserPreferences()
        return MemorySettings()

    monkeypatch.setattr(SettingsStore, "load", load_settings)
    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = [project, agent]

    result = await session_defaults(user, db, SessionOptionsRequest(project_id=project.id))

    assert result["preferredExecutionAgentId"] == str(agent.id)
    assert result["currentDirectory"] == "/srv/project"


async def test_session_is_not_created_for_blank_first_message() -> None:
    user = _user()
    db = AsyncMock(spec=AsyncSession)

    with pytest.raises(ApiError) as exc_info:
        await create_session(SessionCreateRequest(content="   "), user, db)

    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "validation.required"
    assert exc_info.value.field == "content"
    db.add.assert_not_called()


async def test_uncertain_first_commit_preserves_provisioned_session_tree(
    tmp_path: Path, monkeypatch
) -> None:
    from mato_loop_backend.api.routes import sessions as sessions_route

    user = _user()
    provider = LlmProviderConfig(name="Test", adapter_type="openai-compatible")
    model = ModelConfig(provider_id=provider.id, model_id="test-model")
    ai_settings = AiSettings(
        providers=[provider], models=[model], default_model_id=model.id
    )

    async def load_settings(_db, settings_type, **_kwargs):
        return ai_settings if settings_type is AiProviderSettings else MemorySettings()

    sessions = SessionStorage(tmp_path / "sessions")
    monkeypatch.setattr(SettingsStore, "load", load_settings)
    monkeypatch.setattr(
        AgentRuntime, "start", AsyncMock(side_effect=RuntimeError("commit failed"))
    )
    monkeypatch.setattr(sessions_route, "SessionStorage", lambda: sessions)
    db = AsyncMock(spec=AsyncSession)

    with pytest.raises(RuntimeError, match="commit failed"):
        await create_session(SessionCreateRequest(content="Erste Nachricht"), user, db)

    assert len(
        [item for item in sessions.sessions_dir.iterdir() if not item.name.startswith(".")]
    ) == 1
    db.rollback.assert_awaited()


@pytest.mark.parametrize("todo_status,same_run", [
    ("in_progress", False), ("completed", True), ("completed", False),
])
async def test_session_detail_contains_active_run_and_aggregate_metrics(todo_status, same_run) -> None:
    user = _user()
    session_id = uuid4()
    session = ChatSession(
        id=session_id,
        user_id=user.id,
        title="Test",
        is_pinned=True,
        memory_mode="disabled",
        web_search_policy="available",
    )
    created_at = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    run = AgentRun(
        id=uuid4(),
        session_id=session_id,
        status="waiting_approval",
        model_name="Aktives Modell",
        active_duration_ms=1_500,
        created_at=created_at,
    )
    db = AsyncMock(spec=AsyncSession)
    anchors = MagicMock()
    anchors.all.return_value = []
    aggregate = MagicMock()
    aggregate.one.return_value = (
        None,
        None,
        47,
        {
            "privateProviderValue": "must-not-leak",
            "_matoloop": {
                "performance": {
                    "estimatedInputTokens": 12_345,
                    "inputBudgetTokens": 32_000,
                }
            },
        },
        created_at,
    )
    db.execute.side_effect = [anchors, aggregate]
    db.scalar.side_effect = [session, run, run]
    todo_messages = AsyncMock()
    snapshot = Message(
        session_id=session_id,
        role="tool",
        content='{"todos":[{"content":"Prüfen","status":"' + todo_status + '"}]}',
        provider_data={"_matoloop": {"toolName": "todo_write", "runId": str(run.id if same_run else uuid4())}},
    )

    async def snapshots():
        yield Message(role="tool", content="invalid", provider_data={"_matoloop": {"toolName": "todo_write"}})
        yield Message(role="tool", content=snapshot.content, error_code="tool.failed")
        yield snapshot
        raise AssertionError("Older todo snapshots must not be consumed after a valid snapshot")

    todo_messages.__aiter__.side_effect = snapshots
    db.stream_scalars.return_value = todo_messages

    result = await get_session(session_id, user, db)

    assert result["todos"] == (
        [{"content": "Prüfen", "status": todo_status}]
        if same_run or todo_status == "in_progress" else []
    )
    assert result["latestRunId"] == str(run.id)
    assert result["pinned"] is True
    assert result["controlCenter"] == {
        "tabs": [_api_app_tab("session"), _api_app_tab("files")],
        "activeAppId": "session",
        "open": False,
        "fileView": None,
        "fileViews": {"session": None, "filesystem": None},
    }
    assert result["activeRun"] == {
        "id": str(run.id),
        "status": "waiting_approval",
        "modelName": "Aktives Modell",
        "agentProfileId": None,
        "createdAt": created_at.isoformat(),
        "activeDurationMs": 1_500,
        "activeDurationRunning": False,
        "activeTimingRevision": 0,
    }
    assert result["userRequestCount"] == 47
    assert result["latestModelContext"] == {
        "estimatedInputTokens": 12_345,
        "inputBudgetTokens": 32_000,
    }
    assert "must-not-leak" not in str(result)
    assert db.execute.await_count == 2
    aggregate_statement = db.execute.await_args_list[1].args[0]
    aggregate_sql = str(aggregate_statement)
    assert "count(messages.id) FILTER (WHERE messages.role" in aggregate_sql
    aggregate_params = list(aggregate_statement.compile().params.values())
    assert "silence" not in aggregate_params
    assert "_matoloop" in aggregate_params
    assert "performance" in aggregate_params
    assert "jsonb_build_object" in aggregate_sql
    assert "estimatedInputTokens" in aggregate_params and "inputBudgetTokens" in aggregate_params
    assert "messages_1.created_at DESC, messages_1.id DESC" in aggregate_sql


@pytest.mark.parametrize(
    "performance",
    [
        {"estimatedInputTokens": True, "inputBudgetTokens": 32_000},
        {"estimatedInputTokens": 1, "inputBudgetTokens": False},
        {"estimatedInputTokens": -1, "inputBudgetTokens": 32_000},
        {"estimatedInputTokens": 1, "inputBudgetTokens": 0},
        {"estimatedInputTokens": "1", "inputBudgetTokens": 32_000},
        {},
    ],
)
def test_latest_model_context_rejects_malformed_latest_performance(
    performance: dict,
) -> None:
    provider_data = {
        "privateProviderValue": "must-not-leak",
        "_matoloop": {"performance": performance},
    }

    assert _latest_model_context(provider_data) is None


def test_session_payload_ignores_invalid_persisted_file_view() -> None:
    session = ChatSession(
        id=uuid4(),
        user_id=uuid4(),
        title="Test",
        memory_mode="disabled",
        control_center={
            "tabs": [{"app_id": "files", "pinned": False}],
            "active_app_id": "files",
            "open": True,
            "file_view": {
                "scope": "session",
                "selected_path": "drafts/draft-1/plan.md",
                "viewed_file_path": "drafts/draft-1/plan.md",
            },
        },
    )

    result = session_payload(session)

    assert result["controlCenter"] == {
        "tabs": [_api_app_tab("session"), _api_app_tab("files")],
        "activeAppId": "session",
        "open": False,
        "fileView": None,
        "fileViews": {"session": None, "filesystem": None},
    }


@pytest.mark.parametrize("input_outside_page", [False, True])
async def test_session_payload_marks_latest_restart_error_retryable(input_outside_page) -> None:
    user = _user()
    session = ChatSession(
        id=uuid4(),
        user_id=user.id,
        title="Test",
        memory_mode="disabled",
    )
    user_message = Message(
        id=uuid4(),
        session_id=session.id,
        role="user",
        content="Fortsetzen",
    )
    failed_run = AgentRun(id=uuid4(), session_id=session.id, status="failed", input_message_id=user_message.id)
    error_message = Message(
        id=uuid4(),
        session_id=session.id,
        role="assistant",
        content="",
        error_code="agent.backendRestarted",
        error_run_id=failed_run.id,
    )
    user_message.attachments = []
    error_message.attachments = []
    created_at = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    user_message.created_at = created_at
    error_message.created_at = created_at
    messages = MagicMock()
    messages.all.return_value = [error_message] if input_outside_page else [user_message, error_message]
    summaries = MagicMock()
    summaries.all.return_value = []
    anchors = MagicMock()
    anchors.all.return_value = [
        SimpleNamespace(created_at=created_at, id=error_message.id, payload_bytes=2_000),
        SimpleNamespace(created_at=created_at, id=user_message.id, payload_bytes=192_000),
    ] if input_outside_page else [SimpleNamespace(created_at=created_at, id=user_message.id)]
    aggregate = MagicMock()
    aggregate.one.return_value = (created_at, created_at, 1, None, created_at)
    direct_links = MagicMock()
    direct_links.all.return_value = [
        SimpleNamespace(
                id=failed_run.id,
                agent_profile_id=None,
                result_message_id=error_message.id,
                session_id=session.id,
            )
    ]
    no_automation_links = MagicMock()
    no_automation_links.all.return_value = []
    db = AsyncMock(spec=AsyncSession)
    db.get.return_value = failed_run
    todo_messages = MagicMock()
    todo_messages.all.return_value = []
    db.scalars.side_effect = [messages, summaries, todo_messages]
    db.scalar.side_effect = [session, None, None, False] if input_outside_page else [session, None, None]
    input_result = MagicMock()
    input_result.one_or_none.return_value = SimpleNamespace(created_at=created_at, id=user_message.id)
    db.execute.side_effect = [anchors, direct_links, no_automation_links] + (
        [input_result] if input_outside_page else []
    ) + [aggregate]

    result = await get_session(session.id, user, db)

    assert [message["retryAvailable"] for message in result["messages"]] == (
        [True] if input_outside_page else [False, True]
    )


@pytest.mark.parametrize(("page_size", "older_bytes", "continuation", "scripted"), [
    (1, 2_000, False, False), (2, 192_000, False, False), (1, 2_000, True, False),
    (1, 2_000, False, True),
])
async def test_session_message_page_keeps_non_tool_anchor_with_results_and_cursor(page_size, older_bytes, continuation, scripted) -> None:
    session_id = uuid4()
    created_at = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    newest = Message(
        id=uuid4(), session_id=session_id, role="user", content="Neu", created_at=created_at - timedelta(seconds=1)
    )
    assistant = Message(
        id=uuid4(), session_id=session_id, role="assistant", content="Antwort", created_at=created_at
    )
    tool_result = Message(
        id=uuid4(), session_id=session_id, role="tool", content="Result", created_at=created_at + timedelta(seconds=2)
    )
    assistant.tool_calls = [{"id": "call-1", "name": "read", "arguments": {}}]
    tool_result.tool_call_id = "call-1"
    if scripted:
        assistant.provider_data = {"_matoloop": {"scriptToolCalls": {
            "step-1": {"id": "step-1", "name": "read", "arguments": {}, "scriptParent": "call-1"},
        }}}
        tool_result.tool_call_id = "step-1"
    for message in (newest, assistant, tool_result):
        message.attachments = []
    anchors = MagicMock()
    anchors.all.return_value = [
        SimpleNamespace(created_at=created_at, id=assistant.id, payload_bytes=2_000),
        SimpleNamespace(created_at=newest.created_at, id=newest.id, payload_bytes=older_bytes),
    ]
    messages = MagicMock()
    messages.all.return_value = [assistant]
    results = MagicMock()
    results.all.return_value = [tool_result]
    summary = ContextSummary(
        id=uuid4(),
        session_id=session_id,
        covered_through_message_id=assistant.id,
        content="Verdichteter Verlauf",
        estimated_tokens=20,
        is_fallback=True,
        created_at=created_at,
    )
    summaries = MagicMock()
    summaries.all.return_value = [summary]
    db = AsyncMock(spec=AsyncSession)
    continuation_id = uuid4()
    cursor_result = MagicMock()
    cursor_result.one_or_none.return_value = SimpleNamespace(
        created_at=created_at + timedelta(seconds=1), id=continuation_id,
    )
    db.execute.side_effect = ([cursor_result] if continuation else []) + [anchors]
    db.scalars.side_effect = [messages, results, summaries]

    items, context_summaries, next_cursor = await session_message_page(
        db,
        session_id,
        page_size=page_size,
        cursor=continuation_id if continuation else None,
    )

    assert [message.id for message in items] == [assistant.id, tool_result.id]
    assert context_summaries == [summary]
    assert next_cursor == str(assistant.id)
    anchor_query = db.execute.await_args.args[0]
    assert list(anchor_query.selected_columns.keys()) == ["created_at", "id", "payload_bytes"]
    assert "chat_sessions" not in str(anchor_query)
    assert "messages.role !=" in str(anchor_query)
    message_query, result_query = [call.args[0] for call in db.scalars.await_args_list[:2]]
    assert [assistant.id] in message_query.compile().params.values()
    assert "messages.role !=" in str(message_query)
    result_sql = str(result_query)
    assert "messages_1.tool_calls @> jsonb_build_array" in result_sql
    assert "messages_1.created_at DESC, messages_1.id DESC" in result_sql
    assert "messages_1.session_id = messages.session_id" in result_sql
    assert "runId" in result_query.compile().params.values()
    assert set(next(value for value in result_query.compile().params.values()
                    if isinstance(value, list) and "call-1" in value)) == (
        {"call-1", "step-1"} if scripted else {"call-1"}
    )
    assert continuation_id not in result_query.compile().params.values()


@pytest.mark.parametrize(("preview", "carrier_bytes", "boundaries", "row_count", "expected_count"), [
    (False, 2_000, (), 21, 20),  # Ordinary pages still count carriers.
    (True, 2_000, range(8), 8, 8),  # An already short session remains whole.
    (True, 2_000, range(121), 121, 6),  # Stop at six sections even below the ordinary page size.
    (True, 6_000, (2, 4, 6, 8, 10, 12, 60), 62, 13),  # Do not add a fragment of the long older section.
    (True, 2_000, (59, 60, 62, 63, 64, 65), 121, 66),  # The newest section alone spans three ordinary pages.
    (True, 3_050, (59, 60, 62, 63, 64, 65), 121, 61),  # Retain the complete boundary before the byte budget.
    (True, 10_000, (59,), 121, 19),  # If the newest section cannot fit, still make progress.
])
async def test_initial_session_preview_pages_by_section(preview, carrier_bytes, boundaries, row_count, expected_count) -> None:
    session_id = uuid4()
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    anchors = [
        SimpleNamespace(created_at=now - timedelta(seconds=index), id=uuid4(),
                        payload_bytes=carrier_bytes, section_start=index in boundaries)
        for index in range(row_count)
    ]
    db = AsyncMock(spec=AsyncSession)
    db.execute.return_value = MagicMock(all=MagicMock(return_value=anchors))
    db.scalars.return_value = MagicMock(all=MagicMock(return_value=[]))

    _, _, cursor = await session_message_page(db, session_id, page_size=20, cursor=None, history_preview=preview)

    anchor_query = db.execute.await_args.args[0]
    assert anchor_query._limit == (121 if preview else 21)
    assert [anchor.id for anchor in anchors[:expected_count]] in db.scalars.await_args_list[0].args[0].compile().params.values()
    assert cursor == (str(anchors[expected_count - 1].id) if expected_count < row_count else None)


async def test_session_message_page_omits_legacy_orphan_results() -> None:
    db = AsyncMock(spec=AsyncSession)
    db.execute.return_value = MagicMock(all=MagicMock(return_value=[]))

    assert await session_message_page(db, uuid4(), page_size=20, cursor=None) == ([], [], None)
    db.scalars.assert_not_awaited()


async def test_message_window_loads_only_the_requested_tool_carrier_section() -> None:
    session_id = uuid4()
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    earlier, user, carrier, answer, next_turn = [
        SimpleNamespace(id=uuid4(), created_at=now + timedelta(seconds=index), section_start=section, payload_bytes=2_000)
        for index, section in enumerate((True, True, False, False, True))
    ]
    tool_id = uuid4()
    db = AsyncMock(spec=AsyncSession)
    db.execute.side_effect = [
        MagicMock(one_or_none=MagicMock(return_value=SimpleNamespace(id=tool_id, role="tool", carrier_id=carrier.id))),
        MagicMock(all=MagicMock(return_value=[carrier, user, earlier])),
        MagicMock(all=MagicMock(return_value=[answer, next_turn])),
    ]
    tool_carrier = Message(id=carrier.id, role="assistant", tool_calls=[{"id": "call-1"}], created_at=carrier.created_at)
    tool_result = Message(id=tool_id, role="tool", tool_call_id="call-1", created_at=now + timedelta(seconds=6))
    db.scalars.side_effect = [MagicMock(all=MagicMock(return_value=[tool_carrier])),
                              MagicMock(all=MagicMock(return_value=[tool_result])),
                              MagicMock(all=MagicMock(return_value=[]))]

    assert await session_message_window(db, session_id, tool_id) == ([tool_carrier, tool_result], [], str(user.id), True)
    assert "messages_1.tool_calls @>" in str(db.execute.await_args_list[0].args[0])
    assert [carrier.id, user.id, answer.id] in db.scalars.await_args_list[0].args[0].compile().params.values()
    assert (next_turn.created_at, next_turn.id) not in db.scalars.await_args_list[1].args[0].compile().params.values()


async def test_session_owner_cannot_read_another_users_session() -> None:
    owner = _user()
    other = _user()
    session = ChatSession(id=uuid4(), user_id=owner.id, title="Privat")
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = None

    with pytest.raises(ApiError) as exc_info:
        await get_session(session.id, other, db, history_preview=True)

    assert exc_info.value.code == "session.notFound"
    db.scalars.assert_not_awaited()
    with pytest.raises(ApiError) as exc_info:
        await get_session_message_window(session.id, uuid4(), other, db)
    assert exc_info.value.code == "session.notFound"
    db.execute.assert_not_awaited()


@pytest.mark.parametrize("redirect", [False, True])
async def test_active_run_cancellation_is_requested_without_waiting(monkeypatch, redirect) -> None:
    user = _user()
    session_id = uuid4()
    session = ChatSession(id=session_id, user_id=user.id, title="Test")
    run = AgentRun(id=uuid4(), session_id=session_id, status="running")
    cancel = MagicMock(return_value=True)
    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = [session, False]
    db.get.return_value = run
    monkeypatch.setattr(AgentRuntime, "cancel", cancel)

    result = await cancel_run(session_id, run.id, user, db, redirect=redirect)

    cancel.assert_called_once_with(run.id, redirected=redirect)
    db.refresh.assert_not_awaited()
    assert result["status"] == "running"


async def test_terminal_run_cancel_is_idempotent(monkeypatch) -> None:
    user = _user()
    session_id = uuid4()
    session = ChatSession(id=session_id, user_id=user.id, title="Test")
    run = AgentRun(id=uuid4(), session_id=session_id, status="completed")
    cancel = MagicMock()
    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = [session, False]
    db.get.return_value = run
    monkeypatch.setattr(AgentRuntime, "cancel", cancel)

    result = await cancel_run(session_id, run.id, user, db)

    cancel.assert_not_called()
    assert result["status"] == "completed"


async def test_run_cancel_checks_session_hierarchy() -> None:
    user = _user()
    run = AgentRun(id=uuid4(), session_id=uuid4(), status="running")
    session_id = uuid4()
    session = ChatSession(id=session_id, user_id=user.id, title="Test")
    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = [session, False]
    db.get.return_value = run

    with pytest.raises(ApiError) as exc_info:
        await cancel_run(session_id, run.id, user, db)

    assert exc_info.value.status_code == 404
    assert exc_info.value.code == "session.runNotFound"


@pytest.mark.parametrize("route", [get_run, cancel_run, stream_run_events])
async def test_internal_child_run_is_not_publicly_accessible(route) -> None:
    user = _user()
    session_id = uuid4()
    session = ChatSession(id=session_id, user_id=user.id, title="Test")
    child = AgentRun(
        id=uuid4(),
        session_id=session_id,
        parent_run_id=uuid4(),
        status="running",
        internal_request="Inspect",
        internal_transcript=[],
    )
    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = [session, False]
    db.get.return_value = child

    with pytest.raises(ApiError) as exc_info:
        await route(session_id, child.id, user, db)

    assert exc_info.value.status_code == 404
    assert exc_info.value.code == "session.runNotFound"


def test_message_payload_exposes_only_public_reasoning() -> None:
    message = Message(
        id=uuid4(),
        session_id=uuid4(),
        role="assistant",
        content="Antwort",
        model_name="Antwortmodell",
        public_reasoning={"kind": "summary", "text": "Freigegebene Begründung"},
        provider_data={"reasoning_encrypted_content": "private-state"},
    )

    payload = message_payload(message)

    assert payload["reasoning"]["text"] == "Freigegebene Begründung"
    assert payload["modelName"] == "Antwortmodell"
    assert payload["generationStatus"] == "completed"
    assert "private-state" not in str(payload)
    assert payload["attachments"] == []


def test_message_payload_enriches_legacy_tool_calls_without_trusting_invalid_display() -> None:
    message = Message(
        id=uuid4(), session_id=uuid4(), role="assistant", content="",
        tool_calls=[{
            "id": "call-1", "name": "browser_navigate",
            "arguments": {"url": "https://example.org/path?token=secret", "content": "secret"},
            "toolDisplay": {"group": "web", "label": "Provider label", "icon": "danger"},
        }],
    )

    display = message_payload(message)["toolCalls"][0]["toolDisplay"]
    assert display["label"] == "Website aufrufen"
    assert display["icon"] == "language"
    assert display["summary"] == "https://example.org/path?token=secret"


def test_assistant_message_and_run_payload_expose_attribution() -> None:
    run_id, profile_id = uuid4(), uuid4()
    message = Message(
        id=uuid4(), session_id=uuid4(), role="assistant", content="Antwort",
        provider_data={"_matoloop": {
            "runId": str(run_id), "agentProfileId": str(profile_id),
        }},
    )

    payload = message_payload(message)
    assert payload["runId"] == str(run_id)
    assert payload["agentProfileId"] == str(profile_id)
    assert run_payload(AgentRun(
        id=run_id, session_id=message.session_id, agent_profile_id=profile_id,
    ))["agentProfileId"] == str(profile_id)
    legacy = Message(
        id=uuid4(), session_id=message.session_id, role="assistant", content="Alt"
    )
    assert message_payload(legacy)["runId"] is None
    assert message_payload(legacy)["agentProfileId"] is None


async def test_legacy_attribution_requires_one_exact_run() -> None:
    profile_id = uuid4()
    direct_run_id, result_run_id, conflicting_run_id, stamped_run_id = (
        uuid4(), uuid4(), uuid4(), uuid4()
    )
    direct = Message(
        id=uuid4(), session_id=uuid4(), role="assistant", content="Direct"
    )
    conflict = Message(
        id=uuid4(), session_id=direct.session_id, role="assistant", content="Conflict",
        error_run_id=conflicting_run_id,
    )
    stamped_conflict = Message(
        id=uuid4(), session_id=direct.session_id, role="assistant", content="Stamped",
        provider_data={"_matoloop": {
            "runId": str(stamped_run_id), "agentProfileId": str(uuid4()),
        }},
    )
    direct_rows = MagicMock()
    direct_rows.all.return_value = [
        SimpleNamespace(
            id=direct_run_id, agent_profile_id=profile_id,
            result_message_id=direct.id, session_id=direct.session_id,
        ),
        SimpleNamespace(
            id=result_run_id, agent_profile_id=uuid4(),
            result_message_id=conflict.id, session_id=conflict.session_id,
        ),
        SimpleNamespace(
            id=conflicting_run_id, agent_profile_id=uuid4(),
            result_message_id=None, session_id=conflict.session_id,
        ),
        SimpleNamespace(
            id=stamped_run_id, agent_profile_id=uuid4(),
            result_message_id=None, session_id=stamped_conflict.session_id,
        ),
    ]
    empty_rows = MagicMock()
    empty_rows.all.return_value = []
    db = AsyncMock(spec=AsyncSession)
    db.execute.side_effect = [direct_rows, empty_rows]

    resolved = await message_attributions(db, [direct, conflict, stamped_conflict])

    assert resolved == {direct.id: (direct_run_id, profile_id)}


async def test_legacy_attribution_resolves_stamped_tool_and_automation_links() -> None:
    session_id, source_session_id = uuid4(), uuid4()
    stamped_run_id, tool_run_id, automation_run_id = uuid4(), uuid4(), uuid4()
    stamped_profile_id, tool_profile_id, automation_profile_id = uuid4(), uuid4(), uuid4()
    stamped = Message(
        id=uuid4(), session_id=session_id, role="assistant", content="Stamped",
        provider_data={"_matoloop": {
            "runId": str(stamped_run_id),
            "agentProfileId": str(stamped_profile_id),
        }},
    )
    tool_linked = Message(
        id=uuid4(), session_id=session_id, role="assistant", content="Tool",
        tool_calls=[{"id": "call-1", "name": "read", "arguments": {}}],
    )
    automated = Message(
        id=uuid4(), session_id=session_id, role="assistant", content="Automation"
    )
    direct_rows = MagicMock()
    direct_rows.all.return_value = [SimpleNamespace(
        id=stamped_run_id, agent_profile_id=stamped_profile_id,
        result_message_id=None, session_id=session_id,
    )]
    automation_rows = MagicMock()
    automation_rows.all.return_value = [SimpleNamespace(
        output_message_id=automated.id, id=automation_run_id,
        agent_profile_id=automation_profile_id, session_id=source_session_id,
    )]
    tool_rows = MagicMock()
    tool_rows.all.return_value = [SimpleNamespace(
        session_id=session_id, tool_call_id="call-1",
        provider_data={"_matoloop": {"runId": str(tool_run_id)}},
    )]
    profile_rows = MagicMock()
    profile_rows.all.return_value = [SimpleNamespace(
        id=tool_run_id, agent_profile_id=tool_profile_id, session_id=session_id,
    )]
    db = AsyncMock(spec=AsyncSession)
    db.execute.side_effect = [direct_rows, automation_rows, tool_rows, profile_rows]

    resolved = await message_attributions(db, [stamped, tool_linked, automated])

    assert resolved == {
        stamped.id: (stamped_run_id, stamped_profile_id),
        tool_linked.id: (tool_run_id, tool_profile_id),
        automated.id: (automation_run_id, automation_profile_id),
    }


async def test_automation_attribution_survives_normal_target_session_retrieval(
    monkeypatch,
) -> None:
    from mato_loop_backend.api.routes import sessions as sessions_route

    user = _user()
    target = ChatSession(id=uuid4(), user_id=user.id, title="Target")
    source_session_id = uuid4()
    historical_run_id, stamped_run_id, actual_run_id = uuid4(), uuid4(), uuid4()
    historical_profile_id, stamped_profile_id, actual_profile_id = uuid4(), uuid4(), uuid4()
    historical = Message(
        id=uuid4(), session_id=target.id, role="assistant", content="Historical"
    )
    stamped = Message(
        id=uuid4(), session_id=target.id, role="assistant", content="Stamped",
        provider_data={"_matoloop": {
            "runId": str(stamped_run_id),
            "agentProfileId": str(stamped_profile_id),
        }},
    )
    mismatched = Message(
        id=uuid4(), session_id=target.id, role="assistant", content="Mismatched",
        provider_data={"_matoloop": {
            "runId": str(uuid4()),
            "agentProfileId": str(uuid4()),
        }},
    )
    direct_rows = MagicMock()
    direct_rows.all.return_value = []
    automation_rows = MagicMock()
    automation_rows.all.return_value = [
        SimpleNamespace(
            output_message_id=historical.id, id=historical_run_id,
            agent_profile_id=historical_profile_id, session_id=source_session_id,
        ),
        SimpleNamespace(
            output_message_id=stamped.id, id=stamped_run_id,
            agent_profile_id=stamped_profile_id, session_id=source_session_id,
        ),
        SimpleNamespace(
            output_message_id=mismatched.id, id=actual_run_id,
            agent_profile_id=actual_profile_id, session_id=source_session_id,
        ),
    ]
    db = AsyncMock(spec=AsyncSession)
    db.get.return_value = target
    db.execute.side_effect = [direct_rows, automation_rows]
    monkeypatch.setattr(
        sessions_route.session_service,
        "session_message_page",
        AsyncMock(return_value=([historical, stamped, mismatched], [], None)),
    )

    result = await get_session_messages(target.id, user, db)

    assert [(item["runId"], item["agentProfileId"]) for item in result["items"]] == [
        (str(historical_run_id), str(historical_profile_id)),
        (str(stamped_run_id), str(stamped_profile_id)),
        (None, None),
    ]


async def test_valid_stamp_is_authoritative_over_reused_fallback_links() -> None:
    session_id = uuid4()
    stamped_run_id, fallback_run_id = uuid4(), uuid4()
    stamped_profile_id = uuid4()
    message = Message(
        id=uuid4(), session_id=session_id, role="assistant", content="Stamped",
        error_run_id=fallback_run_id,
        tool_calls=[{"id": "reused-call"}],
        provider_data={"_matoloop": {
            "runId": str(stamped_run_id),
            "agentProfileId": str(stamped_profile_id),
        }},
    )
    direct_rows = MagicMock()
    direct_rows.all.return_value = [
        SimpleNamespace(
            id=stamped_run_id, agent_profile_id=stamped_profile_id,
            result_message_id=None, session_id=session_id,
        ),
        SimpleNamespace(
            id=fallback_run_id, agent_profile_id=uuid4(),
            result_message_id=message.id, session_id=session_id,
        ),
    ]
    automation_rows = MagicMock()
    automation_rows.all.return_value = []
    db = AsyncMock(spec=AsyncSession)
    db.execute.side_effect = [direct_rows, automation_rows]

    assert await message_attributions(db, [message]) == {
        message.id: (stamped_run_id, stamped_profile_id)
    }
    assert db.execute.await_count == 2


async def test_legacy_tool_attribution_requires_one_unique_run() -> None:
    session_id = uuid4()
    duplicate_run_id, first_run_id, second_run_id = uuid4(), uuid4(), uuid4()
    duplicate = Message(
        id=uuid4(), session_id=session_id, role="assistant", content="Duplicate",
        tool_calls=[{"id": "duplicate-call"}],
    )
    ambiguous = Message(
        id=uuid4(), session_id=session_id, role="assistant", content="Ambiguous",
        tool_calls=[{"id": "ambiguous-call"}],
    )
    direct_rows = MagicMock()
    direct_rows.all.return_value = []
    automation_rows = MagicMock()
    automation_rows.all.return_value = []
    tool_rows = MagicMock()
    tool_rows.all.return_value = [
        SimpleNamespace(
            session_id=session_id, tool_call_id="duplicate-call",
            provider_data={"_matoloop": {"runId": str(duplicate_run_id)}},
        ),
        SimpleNamespace(
            session_id=session_id, tool_call_id="duplicate-call",
            provider_data={"_matoloop": {"runId": str(duplicate_run_id)}},
        ),
        SimpleNamespace(
            session_id=session_id, tool_call_id="ambiguous-call",
            provider_data={"_matoloop": {"runId": str(first_run_id)}},
        ),
        SimpleNamespace(
            session_id=session_id, tool_call_id="ambiguous-call",
            provider_data={"_matoloop": {"runId": str(second_run_id)}},
        ),
    ]
    profile_rows = MagicMock()
    profile_rows.all.return_value = [
        SimpleNamespace(id=run_id, agent_profile_id=uuid4(), session_id=session_id)
        for run_id in (duplicate_run_id, first_run_id, second_run_id)
    ]
    db = AsyncMock(spec=AsyncSession)
    db.execute.side_effect = [direct_rows, automation_rows, tool_rows, profile_rows]

    resolved = await message_attributions(db, [duplicate, ambiguous])

    assert resolved == {
        duplicate.id: (duplicate_run_id, profile_rows.all.return_value[0].agent_profile_id)
    }


async def test_legacy_attribution_rejects_cross_session_and_malformed_links() -> None:
    session_id, foreign_session_id = uuid4(), uuid4()
    foreign_run_id, malformed_run_id, partial_run_id = uuid4(), uuid4(), uuid4()
    stamped = Message(
        id=uuid4(), session_id=session_id, role="assistant", content="Stamped",
        provider_data={"_matoloop": {
            "runId": str(foreign_run_id), "agentProfileId": str(uuid4()),
        }},
    )
    malformed = Message(
        id=uuid4(), session_id=session_id, role="assistant", content="Malformed",
        provider_data={"_matoloop": {
            "runId": "not-a-uuid", "agentProfileId": "also-invalid",
        }},
    )
    partial = Message(
        id=uuid4(), session_id=session_id, role="assistant", content="Partial",
        error_run_id=partial_run_id,
        provider_data={"_matoloop": {"runId": str(partial_run_id)}},
    )
    error = Message(
        id=uuid4(), session_id=session_id, role="assistant", content="Error",
        error_run_id=foreign_run_id,
    )
    tool = Message(
        id=uuid4(), session_id=session_id, role="assistant", content="Tool",
        tool_calls=[{"id": "call-foreign"}],
    )
    automated = Message(
        id=uuid4(), session_id=session_id, role="assistant", content="Automation"
    )
    direct_rows = MagicMock()
    direct_rows.all.return_value = [
        SimpleNamespace(
            id=foreign_run_id, agent_profile_id=uuid4(),
            result_message_id=error.id, session_id=foreign_session_id,
        ),
        SimpleNamespace(
            id=malformed_run_id, agent_profile_id=uuid4(),
            result_message_id=malformed.id, session_id=session_id,
        ),
        SimpleNamespace(
            id=partial_run_id, agent_profile_id=uuid4(),
            result_message_id=partial.id, session_id=session_id,
        ),
    ]
    automation_profile_id = uuid4()
    automation_rows = MagicMock()
    automation_rows.all.return_value = [SimpleNamespace(
        output_message_id=automated.id, id=foreign_run_id,
        agent_profile_id=automation_profile_id, session_id=foreign_session_id,
    )]
    tool_rows = MagicMock()
    tool_rows.all.return_value = [SimpleNamespace(
        session_id=session_id, tool_call_id="call-foreign",
        provider_data={"_matoloop": {"runId": str(foreign_run_id)}},
    )]
    profile_rows = MagicMock()
    profile_rows.all.return_value = []
    db = AsyncMock(spec=AsyncSession)
    db.execute.side_effect = [direct_rows, automation_rows, tool_rows, profile_rows]

    resolved = await message_attributions(
        db, [stamped, malformed, partial, error, tool, automated]
    )

    assert resolved == {
        automated.id: (foreign_run_id, automation_profile_id)
    }
    for message in (stamped, malformed, partial, error, tool):
        payload = message_payload(
            message, attribution=resolved.get(message.id), attribution_resolved=True
        )
        assert payload["runId"] is None
        assert payload["agentProfileId"] is None


@pytest.mark.parametrize("duration", [False, -1, 1.5, "1000"])
def test_message_payload_exposes_only_valid_active_duration(duration) -> None:
    message = Message(
        id=uuid4(),
        session_id=uuid4(),
        role="assistant",
        content="Antwort",
        provider_data={
            "_matoloop": {"performance": {"activeDurationMs": duration}}
        },
    )

    assert message_payload(message)["activeDurationMs"] is None


def test_message_payload_exposes_active_duration() -> None:
    message = Message(
        id=uuid4(),
        session_id=uuid4(),
        role="assistant",
        content="Antwort",
        provider_data={
            "privateProviderValue": "must-not-leak",
            "_matoloop": {"performance": {"activeDurationMs": 9_000}},
        },
    )

    payload = message_payload(message)

    assert payload["activeDurationMs"] == 9_000
    assert "must-not-leak" not in str(payload)


def test_message_payload_canonicalizes_historical_assistant_markdown() -> None:
    message = Message(
        id=uuid4(),
        session_id=uuid4(),
        role="assistant",
        content="````markdown\n# Document\n\n```mermaid\ngraph TD\nA --> B\n```\n````",
    )

    assert message_payload(message)["content"] == (
        "# Document\n\n```mermaid\ngraph TD\nA --> B\n```\n"
    )


def test_message_payload_exposes_only_known_runtime_warnings() -> None:
    message = Message(
        id=uuid4(),
        session_id=uuid4(),
        role="assistant",
        content="Antwort",
        provider_data={
            "_matoloop": {
                "warnings": [
                    "privacy.placeholderRestorationIncomplete",
                    "privacy.placeholderRestorationIncomplete",
                    "provider.secretWarning",
                    {"code": "privacy.placeholderRestorationIncomplete"},
                ]
            }
        },
    )

    assert message_payload(message)["warnings"] == [
        "privacy.placeholderRestorationIncomplete"
    ]


def test_message_payload_exposes_only_validated_research_snapshot() -> None:
    message = Message(
        id=uuid4(),
        session_id=uuid4(),
        role="assistant",
        content="Antwort",
        provider_data={
            "secret": "private-state",
            "_matoloop": {
                "research": {
                    "policy": "available",
                    "used": True,
                    "linkedSources": [
                        {"title": "Quelle", "url": "HTTPS://EXAMPLE.TEST/source#part"},
                        {"title": "Unsicher", "url": "javascript:alert(1)"},
                    ],
                    "internal": "not-public",
                }
            },
        },
    )

    payload = message_payload(message)

    assert payload["research"] == {
        "policy": "available",
        "used": True,
        "linkedSources": [
            {"title": "Quelle", "url": "https://example.test/source#part"}
        ],
    }
    assert "private-state" not in str(payload)
    assert "not-public" not in str(payload)


def test_message_payload_exposes_only_validated_automation_source() -> None:
    message = Message(
        id=uuid4(),
        session_id=uuid4(),
        role="assistant",
        content="Erinnerung",
        provider_data={
            "secret": "private-state",
            "_matoloop": {
                "source": {
                    "type": "automationRun",
                    "automationId": str(uuid4()),
                    "runId": str(uuid4()),
                }
            },
        },
    )

    payload = message_payload(message)

    assert payload["sourceType"] == "automationRun"
    assert "private-state" not in str(payload)
    assert "automationId" not in str(payload)


def test_message_payload_validates_file_actions_and_derives_download_url() -> None:
    session_id = uuid4()
    execution_agent_id = str(uuid4())
    stored_action = {
        "operation": "copy",
        "root": "workspace",
        "path": "/home/user/project/reports/result.txt",
        "filename": "result.txt",
        "sizeBytes": 7,
        "executionAgentId": execution_agent_id,
        "currentDirectory": "/home/user/project",
    }
    message = Message(
        id=uuid4(),
        session_id=session_id,
        role="tool",
        content="{}",
        provider_data={"_matoloop": {"fileActions": [stored_action]}},
    )

    payload = message_payload(message)

    action = payload["fileActions"][0]
    query = parse_qs(urlparse(action.pop("downloadUrl")).query)
    assert action == stored_action
    assert query == {
        "path": ["/home/user/project/reports/result.txt"],
        "scope": ["filesystem"],
        "session_id": [str(session_id)],
        "execution_agent_id": [execution_agent_id],
        "current_directory": ["/home/user/project"],
    }
    assert "downloadUrl" not in str(message.provider_data)


def test_message_payload_hides_internal_file_actions() -> None:
    session_id = uuid4()
    message = Message(
        id=uuid4(), session_id=session_id, role="tool", content="{}",
        provider_data={"_matoloop": {"fileActions": [{
            "operation": "write", "root": "session", "rootIdentity": str(session_id),
            "path": "research.txt", "filename": "research.txt", "sizeBytes": 7,
            "presentation": "internal",
        }]}},
    )

    assert message_payload(message)["fileActions"] == []


def test_message_payload_classifies_tool_attachments_for_presentation() -> None:
    session_id = uuid4()
    attachments = [
        MessageAttachment(
            id=uuid4(), kind="image", mime_type="image/png", size_bytes=7,
            file_id=uuid4(), file_root_id=session_id, file_revision=1,
            file_path="result.png", filename="result.png",
            metadata_json={"toolOutputPresentation": "internal", "pageNumber": 1},
        ),
        MessageAttachment(
            id=uuid4(), kind="file", mime_type="application/pdf", size_bytes=7,
            file_id=uuid4(), file_root_id=session_id, file_revision=1,
            file_path="result.pdf", filename="result.pdf", metadata_json={},
        ),
    ]
    message = Message(
        id=uuid4(), session_id=session_id, role="tool", content="{}",
        provider_data={"_matoloop": {"toolName": "webfetch"}},
        attachments=attachments,
    )

    payload = message_payload(message)

    assert [item["presentation"] for item in payload["attachments"]] == ["internal", "result"]
    assert payload["attachments"][0]["metadata"] == {"pageNumber": 1}
    assert "toolOutputPresentation" not in str(payload)


def test_message_payload_leaves_unknown_legacy_tool_attachment_unclassified() -> None:
    session_id = uuid4()
    message = Message(
        id=uuid4(),
        session_id=session_id,
        role="tool",
        content="{}",
        attachments=[
            MessageAttachment(
                id=uuid4(),
                kind="image",
                mime_type="image/png",
                size_bytes=7,
                file_id=uuid4(),
                file_root_id=session_id,
                file_revision=1,
                file_path="browser.png",
                filename="browser.png",
                metadata_json={},
            )
        ],
    )

    assert message_payload(message)["attachments"][0]["presentation"] is None


def test_message_payload_validates_prepared_replies() -> None:
    prepared_reply = {
        "markdown": "[Kunde 123](#reply-customer-123)",
        "options": [{"id": "customer-123", "content": "Verwende ERP-Kunde 123."}],
    }
    message = Message(
        id=uuid4(), session_id=uuid4(), role="tool", content="{}",
        provider_data={"_matoloop": {"preparedReply": prepared_reply}},
    )

    assert message_payload(message)["preparedReply"] == prepared_reply

    message.provider_data["_matoloop"]["preparedReply"]["options"][0]["id"] = "../invalid"
    assert message_payload(message)["preparedReply"] is None


def test_message_payload_keeps_session_file_action_url_exact() -> None:
    session_id = uuid4()
    stored_action = {
        "operation": "write",
        "root": "session",
        "rootIdentity": str(session_id),
        "path": "result.txt",
        "filename": "result.txt",
        "sizeBytes": 7,
    }
    message = Message(
        id=uuid4(),
        session_id=session_id,
        role="tool",
        content="{}",
        provider_data={"_matoloop": {"fileActions": [stored_action]}},
    )

    action = message_payload(message)["fileActions"][0]

    query = parse_qs(urlparse(action.pop("downloadUrl")).query)
    assert action == stored_action
    assert query == {
        "path": ["result.txt"],
        "scope": ["session"],
        "session_id": [str(session_id)],
        "root_identity": [str(session_id)],
    }


@pytest.mark.parametrize("pending,hidden", [(True, False), (False, True), (True, True), (False, False)])
def test_message_payload_exposes_only_final_surviving_run_outputs(pending, hidden) -> None:
    session_id, run_id = uuid4(), uuid4()
    message = Message(
        id=uuid4(), session_id=session_id, role="tool", content="{}",
        provider_data={"_matoloop": {
            "runId": str(run_id), "fileOutputsPending": pending, "fileOutputsHidden": hidden,
            "pendingAttachments": [{"private": "never-public"}],
            "fileActions": [{
                "operation": "write", "root": "session", "rootIdentity": str(session_id),
                "path": "result.txt", "filename": "result.txt", "sizeBytes": 7,
            }],
        }},
        attachments=[MessageAttachment(
            id=uuid4(), kind="file", file_id=uuid4(), file_root_id=session_id,
            file_revision=1, file_path="result.txt", filename="result.txt",
        )],
    )
    payload = message_payload(message)
    assert payload["runId"] == str(run_id)
    assert bool(payload["fileActions"]) is (not pending and not hidden)
    assert bool(payload["attachments"]) is (not pending and not hidden)
    assert "pendingAttachments" not in str(payload)
    assert "never-public" not in str(payload)
    message.provider_data["_matoloop"]["runId"] = "invalid"
    assert "runId" not in message_payload(message)


def test_message_payload_rejects_workspace_snapshot_fields_on_session_actions() -> None:
    session_id = uuid4()
    action = {
        "operation": "write",
        "root": "session",
        "rootIdentity": str(session_id),
        "path": "result.txt",
        "filename": "result.txt",
        "sizeBytes": 7,
        "executionAgentId": str(uuid4()),
        "currentDirectory": "/home/user",
    }
    message = Message(
        id=uuid4(),
        session_id=session_id,
        role="tool",
        content="{}",
        provider_data={"_matoloop": {"fileActions": [action]}},
    )

    assert message_payload(message)["fileActions"] == []


@pytest.mark.parametrize(
    "file_actions",
    [
        [
            {
                "operation": "write",
                "root": "session",
                "rootIdentity": "not-a-uuid",
                "path": "result.txt",
                "filename": "result.txt",
                "sizeBytes": 7,
            }
        ],
        [
            {
                "operation": "write",
                "root": "workspace",
                "rootIdentity": "project",
                "path": "../result.txt",
                "filename": "result.txt",
                "sizeBytes": 7,
            }
        ],
        [
            {
                "operation": "write",
                "root": "workspace",
                "rootIdentity": "project",
                "path": "result.txt",
                "filename": "result.txt",
                "sizeBytes": "7",
            }
        ],
        [
            {
                "operation": "write",
                "root": "workspace",
                "rootIdentity": "project",
                "path": "result.txt",
                "filename": "result.txt",
                "sizeBytes": 7,
                "downloadUrl": "https://evil.test/file",
            }
        ],
        [{}, {}],
    ],
)
def test_message_payload_rejects_untrusted_file_action_metadata(file_actions) -> None:
    message = Message(
        id=uuid4(),
        session_id=uuid4(),
        role="tool",
        content="{}",
        provider_data={"_matoloop": {"fileActions": file_actions}},
    )

    assert message_payload(message)["fileActions"] == []


def test_message_payload_recreates_safe_english_runtime_error() -> None:
    secret = "secret upstream response"
    message = Message(
        id=uuid4(),
        session_id=uuid4(),
        role="assistant",
        content=secret,
        error_code="provider.requestFailed",
        error_params={"retryable": True},
        error_run_id=uuid4(),
    )

    payload = message_payload(message)

    assert payload["kind"] == "runtime_error"
    assert payload["content"] == "The model provider request failed."
    assert payload["error"] == {
        "code": "provider.requestFailed",
        "message": "The model provider request failed.",
        "params": {"retryable": True},
    }
    assert payload["retryAvailable"] is False
    assert secret not in str(payload)


async def test_terminal_failure_is_serialized_before_sse_transaction_ends(
    monkeypatch,
) -> None:
    from mato_loop_backend.api.routes import sessions as sessions_route

    user = _user()
    session = ChatSession(id=uuid4(), user_id=user.id, title="Test")
    run = AgentRun(
        id=uuid4(),
        session_id=session.id,
        status="failed",
        error="provider.requestFailed",
        agent_profile_id=uuid4(),
    )
    error_message = Message(
        id=uuid4(),
        session_id=session.id,
        role="assistant",
        content="secret upstream response",
        error_code="provider.requestFailed",
        error_params={"retryable": True},
        error_run_id=run.id,
    )
    error_message.attachments = []
    db = AsyncMock(spec=AsyncSession)
    db.get.return_value = run
    db.scalar.side_effect = [session, error_message]
    original_message_payload = message_payload

    def serialize_before_rollback(message, **kwargs):
        assert db.rollback.await_count == 0
        return original_message_payload(message, **kwargs)

    monkeypatch.setattr(sessions_route, "message_payload", serialize_before_rollback)

    response = await stream_run_events(session.id, run.id, user, db)
    chunks = [chunk async for chunk in response.body_iterator]
    body = "".join(
        chunk.decode() if isinstance(chunk, bytes) else chunk for chunk in chunks
    )

    assert db.rollback.await_count == 1
    assert body.index("event: message.completed") < body.index("event: run.failed")
    assert '"retryable": true' in body
    assert f'"runId": "{run.id}"' in body
    assert f'"agentProfileId": "{run.agent_profile_id}"' in body
    assert "secret upstream response" not in body


def test_message_payload_contains_public_attachment_metadata_only() -> None:
    session_id = uuid4()
    message_id = uuid4()
    attachment_id = uuid4()
    file_id = uuid4()
    message = Message(
        id=message_id,
        session_id=session_id,
        role="assistant",
        content="Bild",
    )
    message.attachments = [
        MessageAttachment(
            id=attachment_id,
            message_id=message_id,
            position=0,
            kind="image",
            category="generated",
            purpose="artifact",
            processing_status="ready",
            mime_type="image/png",
            size_bytes=123,
            sha256="a" * 64,
            file_root_id=session_id,
            file_id=file_id,
            file_revision=3,
            file_path="bild.png",
            filename="bild.png",
            width=10,
            height=20,
        )
    ]

    payload = message_payload(message)

    assert payload["attachments"] == [
        {
            "id": str(attachment_id),
            "kind": "image",
            "category": "generated",
            "purpose": "artifact",
            "presentation": "result",
            "processingStatus": "ready",
            "mimeType": "image/png",
            "sizeBytes": 123,
            "filename": "bild.png",
            "width": 10,
            "height": 20,
            "durationMs": None,
            "metadata": {},
            "fileRootId": str(session_id),
            "fileId": str(file_id),
            "fileRevision": 3,
            "filePath": "bild.png",
            "url": (
                f"/api/sessions/{session_id}/messages/{message_id}"
                f"/attachments/{attachment_id}"
            ),
        }
    ]
    assert "storage" not in str(payload).lower()
    assert "sha256" not in str(payload)


async def test_attachment_download_has_secure_headers_and_checks_hierarchy(
    tmp_path: Path, monkeypatch
) -> None:
    from mato_loop_backend.api.routes import sessions as sessions_route

    session_id = uuid4()
    user = _user()
    message_id = uuid4()
    attachment_id = uuid4()
    sessions = SessionStorage(tmp_path / "sessions")
    sessions.provision(session_id)
    data = b"image-data"
    root_files = RootFileService(session_id, session_storage=sessions)
    root_files.create_directory(FileRoot.SESSION, "attachments")
    root_files.create_directory(FileRoot.SESSION, f"attachments/{attachment_id}")
    file_path = f"attachments/{attachment_id}/bild.png"
    root_files.create_file(
        FileRoot.SESSION, file_path, BytesIO(data), max_bytes=100
    )
    revision = root_files.list_revisions(file_path)[0]
    attachment = MessageAttachment(
        id=attachment_id,
        message_id=message_id,
        position=0,
        kind="image",
        category="user_upload",
        mime_type="image/png",
        size_bytes=10,
        sha256=hashlib.sha256(data).hexdigest(),
        file_root_id=session_id,
        file_id=revision.file_id,
        file_revision=revision.revision,
        file_path=file_path,
        filename="bild.png",
    )
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = attachment
    monkeypatch.setattr(
        sessions_route, "AttachmentService", lambda: AttachmentService(sessions)
    )

    response = await download_attachment(
        session_id, message_id, attachment_id, user, db
    )

    statement = db.scalar.await_args.args[0]
    assert {session_id, message_id, attachment_id}.issubset(set(statement.compile().params.values()))
    assert Path(response.path).read_bytes() == b"image-data"
    assert response.media_type == "image/png"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cross-origin-resource-policy"] == "same-origin"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["content-disposition"].startswith("inline;")


@pytest.mark.parametrize("computer", ["online", "offline", "slow", "permission"])
async def test_session_delete_impact_discovers_the_worktree_without_waiting_for_an_unavailable_computer(monkeypatch, computer):
    import asyncio

    from mato_loop_backend.sessions import coding
    from mato_loop_backend.sessions.service import SessionServiceError, session_delete_impact

    session = ChatSession(id=uuid4(), user_id=uuid4(), project_id=uuid4(),
        preferred_execution_agent_id=uuid4(), current_directory="/worktree/src", workspace_mode="local")
    other = ChatSession(id=uuid4(), title="Other work", current_directory="/worktree")
    child = ChatSession(id=uuid4(), parent_session_id=session.id, current_directory="/worktree")
    grandchild = ChatSession(id=uuid4(), parent_session_id=child.id, current_directory="/worktree")
    affected = [session, child, grandchild, other]
    info = coding.RepositoryResult(repository="/worktree", commonDirectory="/project/.git", remotes=[],
        defaultRemote=None, defaultBranch="main", branch="task", removalChecked=True,
        removalError={"code": "git.worktreePermissionDenied", "params": {"path": "/worktree/cache"}} if computer == "permission" else None,
        worktrees=[{"path": "/worktree", "branch": "task", "main": False, "locked": False}])
    async def inspect(*args, **kwargs):
        if computer == "offline":
            raise SessionServiceError(409, "executionAgent.unavailable")
        if computer == "slow":
            await asyncio.sleep(1)
        return info

    monkeypatch.setattr(coding, "inspect_repository", inspect)
    monkeypatch.setattr(coding, "worktree_registration", AsyncMock(return_value=1.0))
    timeout = asyncio.timeout
    monkeypatch.setattr(asyncio, "timeout", lambda seconds: timeout(0.01))
    db = AsyncMock(spec=AsyncSession)
    db.scalars.side_effect = [MagicMock(all=lambda: []), affected]
    db.scalar.return_value = 0
    db.execute.return_value = MagicMock(all=lambda: [(child.id, session.project_id), (grandchild.id, session.project_id)])
    result = await asyncio.wait_for(session_delete_impact(
        session.id, session.user_id, db, owned_session_query=AsyncMock(return_value=session),
    ), timeout=0.5)
    assert result["worktree"] == (None if computer not in {"online", "permission"} else {
        "path": "/worktree", "impact": coding.worktree_removal_token(session, info, affected, 1.0),
        "removalError": info.removal_error,
        "sessions": [{"id": str(other.id), "title": other.title}],
    })


@pytest.mark.parametrize("worktree", [None, "remove", "changed", "blocked", "joined", "permission", "trash"])
async def test_session_delete_trashes_commits_and_purges_tree(
    tmp_path: Path, monkeypatch, worktree
) -> None:
    from mato_loop_backend.api.routes import sessions as sessions_route
    from mato_loop_backend.sessions import coding

    session_id = uuid4()
    user = _user()
    session = ChatSession(id=session_id, user_id=user.id, title="Test", preferred_execution_agent_id=uuid4(),
        current_directory="/missing/worktree", workspace_mode="worktree")
    sessions = SessionStorage(tmp_path / "sessions")
    session_path = sessions.provision(session_id, user_id=user.id)
    (session_path / "files" / "notiz.txt").write_text("Inhalt", encoding="utf-8")
    active_runs_result = MagicMock()
    active_runs_result.all.return_value = []
    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = [session, 0, session, 0]
    db.scalars.return_value = active_runs_result
    db.execute.return_value = MagicMock(all=lambda: [])
    monkeypatch.setattr(AgentRuntime, "cancel_descendants", AsyncMock(), raising=False)
    monkeypatch.setattr(sessions_route, "SessionStorage", lambda: sessions)

    token = None
    if worktree:
        info = coding.RepositoryResult(repository="/missing/worktree", commonDirectory="/project/.git",
            gitDirectory="/project/.git/worktrees/worktree",
            remotes=[], defaultRemote=None, defaultBranch="main", branch="task", worktrees=[
                {"path": "/project", "branch": "main", "main": True, "locked": False},
                {"path": "/missing/worktree", "branch": "task", "main": False, "locked": False},
            ])
        token = coding.worktree_removal_token(session, info, [], 1.0)
        monkeypatch.setattr(coding, "worktree_registration", AsyncMock(return_value=1.0))
        monkeypatch.setattr(coding, "session_worktree", AsyncMock(return_value=info))
        affected = [ChatSession(id=uuid4())] if worktree == "joined" else []
        monkeypatch.setattr(coding, "sessions_in_worktree", AsyncMock(return_value=affected))
        if worktree == "blocked":
            db.scalars.side_effect = lambda statement: ["/missing/worktree/src"] if str(statement).startswith(
                "SELECT agent_runs.current_directory_snapshot"
            ) else active_runs_result
        client = AsyncMock()
        client.request.return_value = coding.WorktreeRemovedResult(removed=True, headCommit="a" * 40)
        monkeypatch.setattr(coding, "coding_client", AsyncMock(return_value=client))
        if worktree == "permission":
            from mato_loop_backend.execution.client import ExecutionClientError

            client.request.side_effect = ExecutionClientError("git.worktreePermissionDenied", {"path": "/missing/worktree/cache"})
            with pytest.raises(ApiError) as error:
                await delete_session(session_id, user, db, worktree_impact=token, permanent=True)
            assert (error.value.code, error.value.params, session_path.exists(), db.delete.await_count) == (
                "git.worktreePermissionDenied", {"path": "/missing/worktree/cache"}, True, 0,
            )
            return
        if worktree == "changed":
            session.current_directory = "/another/worktree"
        if worktree in {"changed", "blocked", "joined"}:
            with pytest.raises(ApiError, match="git.worktreeInUse" if worktree == "blocked" else "git.worktreeChanged"):
                await delete_session(session_id, user, db, worktree_impact=token, permanent=True)
            assert (session_path.exists(), db.delete.await_count, client.request.await_count) == (True, 0, 0)
            return

    if worktree == "trash":
        monkeypatch.setattr(sessions_route, "_deletable_session", AsyncMock(return_value=session))
        monkeypatch.setattr(SettingsStore, "load", AsyncMock(return_value=UserPreferences(trash_enabled=True)))
        db.scalar.side_effect = lambda statement: session if str(statement).startswith("SELECT chat_sessions.id,") else None
        db.scalars.return_value = []
        await delete_session(session_id, user, db, worktree_impact=token, permanent=False)
        assert (session.delete_at is not None, client.request.await_count, session_path.exists(), db.delete.await_count) == (True, 1, True, 0)
        assert session.coding_workspace["worktreeRecovery"]["commit"] == "a" * 40
        return

    await delete_session(session_id, user, db, worktree_impact=token, permanent=True)

    if worktree:
        assert client.request.await_args.args[1]["force"] is True
    db.delete.assert_awaited_once_with(session)
    db.commit.assert_awaited_once()
    assert not session_path.exists()
    assert list(sessions.trash_dir.iterdir()) == []
    assert sessions._owners == {}


async def test_session_delete_rejects_files_referenced_by_another_session(
    tmp_path: Path, monkeypatch
) -> None:
    from mato_loop_backend.api.routes import sessions as sessions_route

    session_id = uuid4()
    child_id = uuid4()
    user = _user()
    session = ChatSession(id=session_id, user_id=user.id, title="Owner")
    sessions = SessionStorage(tmp_path / "sessions")
    session_path = sessions.provision(session_id)
    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = [session, 1]
    db.execute.return_value = MagicMock(all=lambda: [(child_id, None)])
    monkeypatch.setattr(sessions_route, "SessionStorage", lambda: sessions)

    with pytest.raises(ApiError) as exc_info:
        await delete_session(session_id, user, db, permanent=True)

    assert exc_info.value.code == "session.filesReferenced"
    assert exc_info.value.params == {"referenceCount": 1}
    query = db.scalar.await_args_list[1].args[0].compile()
    assert {session_id, child_id} in [
        set(value) for value in query.params.values() if isinstance(value, list)
    ]
    assert session_path.is_dir()
    db.delete.assert_not_awaited()


async def test_session_delete_restores_tree_when_database_commit_fails(
    tmp_path: Path, monkeypatch
) -> None:
    from mato_loop_backend.api.routes import sessions as sessions_route

    session_id = uuid4()
    user = _user()
    session = ChatSession(id=session_id, user_id=user.id, title="Test")
    sessions = SessionStorage(tmp_path / "sessions")
    session_path = sessions.provision(session_id, user_id=user.id)
    stored_file = session_path / "files" / "notiz.txt"
    stored_file.write_text("Inhalt", encoding="utf-8")
    active_runs_result = MagicMock()
    active_runs_result.all.return_value = []
    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = [session, 0, session, 0]
    db.scalars.return_value = active_runs_result
    db.execute.return_value = MagicMock(all=lambda: [])
    monkeypatch.setattr(AgentRuntime, "cancel_descendants", AsyncMock(), raising=False)
    db.commit.side_effect = RuntimeError("commit failed")
    monkeypatch.setattr(sessions_route, "SessionStorage", lambda: sessions)

    with pytest.raises(RuntimeError, match="commit failed"):
        await delete_session(session_id, user, db, permanent=True)

    assert stored_file.read_text(encoding="utf-8") == "Inhalt"
    assert sessions._owners == {session_id: user.id}
    assert list(sessions.trash_dir.iterdir()) == []
    assert db.rollback.await_count == 2


async def test_attachment_download_rejects_incomplete_hierarchy() -> None:
    user = _user()
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = None

    with pytest.raises(ApiError) as exc_info:
        await download_attachment(uuid4(), uuid4(), uuid4(), user, db)

    assert exc_info.value.status_code == 404
    assert exc_info.value.code == "attachment.notFound"


@pytest.mark.parametrize("selection", ["default", "session", "agent"])
async def test_session_accepts_unavailable_model_for_host_failure(monkeypatch, selection) -> None:
    from mato_loop_backend.settings.models import GENERAL_PROFILE_ID
    from mato_loop_backend.settings.session_profiles import (
        ChatModelOverride,
        SessionProfileSnapshot,
    )

    user = _user()
    provider = LlmProviderConfig(name="Local", adapter_type="openai-compatible")
    model = ModelConfig(
        provider_id=provider.id,
        model_id="missing",
        availability=ModelAvailability.MISSING,
    )
    settings = AiSettings(providers=[provider], models=[model], default_model_id=model.id)

    async def load_settings(_db, settings_type, **_kwargs):
        return settings if settings_type is AiProviderSettings else MemorySettings()

    monkeypatch.setattr(SettingsStore, "load", load_settings)
    run = AgentRun(
        id=uuid4(),
        session_id=uuid4(),
        status="queued",
        model_config_id=model.id,
    )
    start = AsyncMock(return_value=run)
    monkeypatch.setattr(AgentRuntime, "start", start)
    from mato_loop_backend.api.routes import sessions as sessions_route

    storage = MagicMock()
    monkeypatch.setattr(sessions_route, "SessionStorage", lambda: storage)
    persisted_message = Message(
        id=uuid4(),
        session_id=run.session_id,
        role="user",
        content="Test",
    )
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = persisted_message

    options = {"model_id": model.id} if selection == "session" else {
        "session_profile": SessionProfileSnapshot(agent_models={GENERAL_PROFILE_ID: ChatModelOverride(model_id=model.id)})
    } if selection == "agent" else {}
    result = await create_session(SessionCreateRequest(content="Test", **options), user, db)
    assert result["runId"] == str(run.id)
    start.assert_awaited_once()


async def test_required_search_unavailability_does_not_reject_user_message(monkeypatch) -> None:
    user = _user()
    session = ChatSession(
        id=uuid4(),
        user_id=user.id,
        title="Test",
        web_search_policy="required",
    )
    run = AgentRun(id=uuid4(), session_id=session.id, status="queued")
    user_message = Message(
        id=uuid4(), session_id=session.id, role="user", content="Aktuelle Frage"
    )
    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = [session, False, None, None, user_message]
    start = AsyncMock(return_value=run)
    monkeypatch.setattr(AgentRuntime, "start", start)
    provider = LlmProviderConfig(name="Local", adapter_type="openai-compatible")
    model = ModelConfig(provider_id=provider.id, model_id="agent")
    session.model_config_id = model.id
    load = AsyncMock(
        return_value=AiSettings(
            providers=[provider], models=[model], default_model_id=model.id
        )
    )
    monkeypatch.setattr(SettingsStore, "load", load)

    request = SendMessageRequest.model_validate(
        {
            "content": "Aktuelle Frage",
            "activeFile": {
                "root": "session",
                "rootIdentity": str(session.id),
                "path": "notes.txt",
                "sha256": "a" * 64,
                "dirty": True,
            },
        }
    )
    result = await send_message(session.id, request, user, db)

    start.assert_awaited_once_with(
        db,
        session,
        "Aktuelle Frage",
        run_id=None,
        attachments=None,
        active_file={
            "root": "session",
            "rootIdentity": str(session.id),
            "path": "notes.txt",
            "sha256": "a" * 64,
            "dirty": True,
        },
    )
    assert load.await_count >= 1
    assert result["runId"] == str(run.id)


async def test_existing_session_retry_reschedules_queued_run(monkeypatch) -> None:
    user = _user()
    session = ChatSession(id=uuid4(), user_id=user.id, title="Test")
    message = Message(
        id=uuid4(), session_id=session.id, role="user", content="Original"
    )
    run = AgentRun(
        id=uuid4(),
        session_id=session.id,
        input_message_id=message.id,
        status="queued",
        model_name="Test",
    )
    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = [session, False, run, session, False, message]
    schedule = MagicMock()
    monkeypatch.setattr(AgentRuntime, "_schedule_run", schedule)

    result = await send_message(
        session.id,
        SendMessageRequest(content="retry", run_id=run.id),
        user,
        db,
    )

    assert result["runId"] == str(run.id)
    assert result["message"]["content"] == "Original"
    assert len(db.scalar.await_args_list) == 6
    schedule.assert_called_once_with(run)


async def test_existing_session_message_passes_profile_to_next_run(monkeypatch) -> None:
    from mato_loop_backend.sessions import service as session_service

    user = _user()
    selected_profile = uuid4()
    session = ChatSession(id=uuid4(), user_id=user.id, agent_profile_id=uuid4())
    run = AgentRun(id=uuid4(), session_id=session.id, agent_profile_id=selected_profile)
    message = Message(id=uuid4(), session_id=session.id, role="user", content="Next")
    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = [session, False, None, None, message]
    provider = LlmProviderConfig(name="Local", adapter_type="openai-compatible")
    model = ModelConfig(provider_id=provider.id, model_id="model")
    ai = AiSettings(providers=[provider], models=[model], default_model_id=model.id)
    load_ai = AsyncMock(return_value=ai)
    monkeypatch.setattr(session_service, "load_effective_ai", load_ai)
    monkeypatch.setattr(session_service, "validate_agent_profile", AsyncMock())
    grant = AsyncMock()
    monkeypatch.setattr(session_service, "add_directory_grant", grant)
    start = AsyncMock(return_value=run)
    monkeypatch.setattr(AgentRuntime, "start", start)

    result = await send_message(
        session.id,
        SendMessageRequest.model_validate({
            "content": "Next", "agent_profile_id": str(selected_profile)
        }),
        user,
        db,
    )

    load_ai.assert_awaited_once_with(db, user.id)
    assert grant.await_args.args[:2] == (db, session.id)
    assert grant.await_args.args[5] == selected_profile
    assert start.await_args.kwargs["agent_profile_id_override"] == selected_profile
    assert result["agentProfileId"] == str(selected_profile)


async def test_direct_message_is_persisted_once_on_active_run(monkeypatch) -> None:
    from mato_loop_backend.sessions import service as session_service

    user = _user()
    session = ChatSession(id=uuid4(), user_id=user.id)
    run = AgentRun(id=uuid4(), session_id=session.id, status="running")
    message_id = uuid4()
    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = [None, run]
    reservation = object()
    reserve = MagicMock(return_value=reservation)
    finish = MagicMock()
    monkeypatch.setattr(AgentRuntime, "reserve_continuation", reserve)
    monkeypatch.setattr(AgentRuntime, "finish_continuation", finish)
    monkeypatch.setattr(session_service.EpisodeIndexService, "enqueue", AsyncMock())
    monkeypatch.setattr(SettingsStore, "load", AsyncMock(return_value=session_service.InstanceLimits()))
    payload = SimpleNamespace(content="  Another thought  ", message_id=message_id, active_file=None)

    result = await session_service.continue_run(
        session.id, run.id, payload, user, db,
        attachments=None,
        owned_session_query=AsyncMock(return_value=session),
        message_serializer=lambda message: {"id": str(message.id), "content": message.content},
    )

    message = db.add.call_args.args[0]
    assert message.content == "Another thought"
    assert message.provider_data["_matoloop"]["continuationRunId"] == str(run.id)
    assert result == {"message": {"id": str(message_id), "content": "Another thought"}, "runId": str(run.id)}
    db.commit.assert_awaited_once()
    finish.assert_called_once_with(run.id, reservation, accepted=True)

    db.scalar.side_effect = [message]
    duplicate = await session_service.continue_run(
        session.id, run.id, payload, user, db,
        attachments=None,
        owned_session_query=AsyncMock(return_value=session),
        message_serializer=lambda item: {"id": str(item.id), "content": item.content},
    )
    assert duplicate == result
    reserve.assert_called_once_with(run.id, queued=False)
    db.commit.assert_awaited_once()


async def test_existing_session_retry_rejects_owned_run_from_another_session() -> None:
    user = _user()
    requested = ChatSession(id=uuid4(), user_id=user.id, title="Requested")
    run = AgentRun(id=uuid4(), session_id=uuid4(), status="completed")
    original = ChatSession(id=run.session_id, user_id=user.id)
    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = [requested, False, run, original, False]

    with pytest.raises(ApiError) as exc_info:
        await send_message(
            requested.id,
            SendMessageRequest(content="retry", run_id=run.id),
            user,
            db,
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "http.conflict"
    ownership_statement = db.scalar.await_args_list[2].args[0]
    assert user.id in ownership_statement.compile().params.values()


async def test_run_id_collision_with_another_owner_is_a_controlled_conflict(monkeypatch) -> None:
    user = _user()
    session = ChatSession(id=uuid4(), user_id=user.id, title="Test")
    run_id = uuid4()
    provider = LlmProviderConfig(name="Local", adapter_type="openai-compatible")
    model = ModelConfig(provider_id=provider.id, model_id="agent")
    session.model_config_id = model.id
    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = [session, False, None, None, None, None]
    monkeypatch.setattr(
        SettingsStore,
        "load",
        AsyncMock(
            return_value=AiSettings(
                providers=[provider], models=[model], default_model_id=model.id
            )
        ),
    )
    monkeypatch.setattr(
        AgentRuntime,
        "start",
        AsyncMock(side_effect=IntegrityError("insert", {}, Exception("duplicate"))),
    )

    with pytest.raises(ApiError) as exc_info:
        await send_message(
            session.id,
            SendMessageRequest(content="Nachricht", run_id=run_id),
            user,
            db,
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "http.conflict"
    db.rollback.assert_awaited_once()


async def test_attachment_only_message_starts_run(monkeypatch) -> None:
    from mato_loop_backend.api.routes import sessions as sessions_route

    user = _user()
    session = ChatSession(id=uuid4(), user_id=user.id, title="Test")
    run = AgentRun(id=uuid4(), session_id=session.id, status="queued")
    user_message = Message(id=uuid4(), session_id=session.id, role="user", content="")
    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = [session, False, None, None, user_message]
    start = AsyncMock(return_value=run)
    monkeypatch.setattr(AgentRuntime, "start", start)
    provider = LlmProviderConfig(name="Local", adapter_type="openai-compatible")
    model = ModelConfig(provider_id=provider.id, model_id="agent")
    session.model_config_id = model.id
    monkeypatch.setattr(
        SettingsStore,
        "load",
        AsyncMock(
            return_value=AiSettings(
                providers=[provider], models=[model], default_model_id=model.id
            )
        ),
    )
    attachment = AttachmentInput(b"Bericht", "text/plain", "bericht.txt")

    result = await sessions_route._send_message(
        session.id,
        SendMessageRequest(content="   "),
        user,
        db,
        attachments=[attachment],
    )

    start.assert_awaited_once_with(
        db,
        session,
        "",
        run_id=None,
        attachments=[attachment],
        active_file=None,
    )
    assert result["message"]["content"] == ""


async def test_session_can_switch_to_public_model(monkeypatch) -> None:
    user = _user()
    provider = LlmProviderConfig(name="Cloud", adapter_type="openai-compatible")
    public_model = ModelConfig(provider_id=provider.id, model_id="public")
    settings = AiSettings(providers=[provider], models=[public_model])
    session = ChatSession(
        id=uuid4(),
        user_id=user.id,
        title="Privat gestartet",
        model_config_id=None,
        memory_mode="disabled",
    )
    monkeypatch.setattr(
        SettingsStore,
        "load",
        AsyncMock(return_value=settings),
    )
    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = [session.id, session, False, None]
    db.scalars.return_value = MagicMock(all=lambda: [])

    await update_session(
        session.id,
        SessionUpdateRequest(model_id=public_model.id),
        user,
        db,
    )

    assert session.model_config_id == public_model.id


async def test_session_can_override_reasoning_effort() -> None:
    user = _user()
    session = ChatSession(
        id=uuid4(),
        user_id=user.id,
        title="Test",
        memory_mode="disabled",
    )
    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = [session.id, session, False, None]
    db.scalars.return_value = MagicMock(all=lambda: [])

    result = await update_session(
        session.id,
        SessionUpdateRequest(reasoning_effort="ultra"),
        user,
        db,
    )

    assert session.reasoning_effort == "ultra"
    assert result["reasoningEffort"] == "ultra"


async def test_template_switch_replaces_session_overrides(monkeypatch) -> None:
    from mato_loop_backend.settings.session_profiles import (
        DEFAULT_TEMPLATE_ID,
        SessionTemplatesSettings,
    )

    user = _user()
    provider = LlmProviderConfig(name="Cloud", adapter_type="openai-compatible")
    model = ModelConfig(provider_id=provider.id, model_id="chat")
    ai = AiSettings(providers=[provider], models=[model], default_model_id=model.id)
    monkeypatch.setattr(SettingsStore, "load", AsyncMock(side_effect=lambda _db, kind, **_: ai if kind is AiProviderSettings else kind()))
    session = ChatSession(id=uuid4(), user_id=user.id, title="Test", model_config_id=uuid4(), reasoning_effort="high", memory_mode="disabled", session_profile={"agent_models": {str(uuid4()): {"model_id": str(uuid4())}}}, web_search_policy="disabled", workspace_mode="worktree")
    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = [session.id, session, False, None]
    db.scalars.return_value = MagicMock(all=lambda: [])

    result = await update_session(session.id, SessionUpdateRequest(session_template_id=DEFAULT_TEMPLATE_ID), user, db)

    template = SessionTemplatesSettings().templates[0]
    assert {key: result[key] for key in ("sessionProfile", "agentProfileId", "modelId", "reasoningEffort", "memoryMode", "webSearchPolicy")} == {
        "sessionProfile": template.snapshot().model_dump(mode="json", by_alias=True),
        "agentProfileId": str(template.start_agent_id), "modelId": None,
        "reasoningEffort": None, "memoryMode": "full", "webSearchPolicy": "disabled",
    }


@pytest.mark.parametrize("field,revokes", [("message_size", False), ("agent_ids", True), ("agent_models", False), ("model_id", False), ("assistant_context", False), ("keyboard_agent_ids", False)])
async def test_session_changes_only_revoke_grants_for_policy_changes(monkeypatch, field, revokes) -> None:
    from mato_loop_backend.settings.session_profiles import (
        ChatModelOverride,
        SessionTemplatesSettings,
    )

    user = _user()
    provider = LlmProviderConfig(name="Cloud", adapter_type="openai-compatible")
    model = ModelConfig(provider_id=provider.id, model_id="chat")
    ai = AiSettings(providers=[provider], models=[model], default_model_id=model.id)
    monkeypatch.setattr(SettingsStore, "load", AsyncMock(side_effect=lambda _db, kind, **_: ai if kind is AiProviderSettings else kind()))
    template = SessionTemplatesSettings().templates[1]
    profile = template.snapshot()
    session = ChatSession(id=uuid4(), user_id=user.id, title="Test", agent_profile_id=template.start_agent_id, model_config_id=model.id, memory_mode="disabled", session_profile=profile.model_dump(mode="json"))
    session.session_profile["context_state"] = {"revision": 3, "blocks": [{"key": "keep", "text": "durable"}], "trust_level": "private"}
    payload = SessionUpdateRequest(model_id=model.id)
    if field == "model_id":
        session.model_config_id = None
    else:
        updates = {field: {
            "message_size": "standard",
            "agent_ids": [template.start_agent_id],
            "agent_models": {template.start_agent_id: ChatModelOverride(model_id=model.id)},
            "assistant_context": "disabled",
            "keyboard_agent_ids": [template.start_agent_id],
        }[field]}
        if field == "agent_ids":
            updates["keyboard_agent_ids"] = [template.start_agent_id]
        payload = SessionUpdateRequest(session_profile=profile.model_copy(update=updates))
    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = [session.id, session, False, None]
    db.scalars.return_value = MagicMock(all=lambda: [])

    await update_session(session.id, payload, user, db)

    assert db.execute.await_count == int(revokes)
    assert session.session_profile["context_state"]["revision"] == 3


async def test_session_model_switch_ignores_stale_model_references(monkeypatch) -> None:
    from mato_loop_backend.settings.effective import apply_session_ai
    from mato_loop_backend.settings.models import BUILD_PROFILE_ID, PLAN_PROFILE_ID
    from mato_loop_backend.settings.session_profiles import (
        ChatModelOverride,
        SessionProfileSnapshot,
    )

    user = _user()
    provider = LlmProviderConfig(name="Local", adapter_type="openai-compatible")
    plain_chat = ModelConfig(provider_id=provider.id, model_id="plain-chat", supports_tools=False)
    fallback = ModelConfig(provider_id=provider.id, model_id="Qwen3.8-27B")
    replacement = ModelConfig(provider_id=provider.id, model_id="replacement")
    ai = AiSettings(providers=[provider], models=[plain_chat, fallback, replacement], default_model_id=fallback.id)
    monkeypatch.setattr(SettingsStore, "load", AsyncMock(side_effect=lambda _db, kind, **_: ai if kind is AiProviderSettings else kind()))
    profile = SessionProfileSnapshot(agent_models={PLAN_PROFILE_ID: ChatModelOverride(model_id=uuid4(), reasoning_effort="high"), BUILD_PROFILE_ID: ChatModelOverride(model_id=uuid4())})
    session = ChatSession(id=uuid4(), user_id=user.id, title="Test", agent_profile_id=PLAN_PROFILE_ID, model_config_id=plain_chat.id, memory_mode="disabled", session_profile=profile.model_dump(mode="json"))
    before = apply_session_ai(ai, session)
    profile.agent_models[PLAN_PROFILE_ID] = ChatModelOverride(model_id=replacement.id)
    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = [session.id, session, False, None]
    db.scalars.return_value = MagicMock(all=lambda: [])

    await update_session(session.id, SessionUpdateRequest(session_profile=profile), user, db)

    assert (before.default_model_id, before.reasoning_effort, apply_session_ai(ai, session).default_model_id) == (fallback.id, None, replacement.id)


async def test_manual_title_change_skips_automatic_generation(monkeypatch) -> None:
    user = _user()
    session = ChatSession(
        id=uuid4(),
        user_id=user.id,
        title="Automatischer Entwurf",
        title_generation_status=TitleGenerationStatus.RUNNING.value,
        memory_mode="full",
        web_search_policy="available",
    )
    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = [session, False]
    monkeypatch.setattr(
        SettingsStore,
        "load",
        AsyncMock(
            side_effect=lambda _db, settings_type, **_kwargs: settings_type()
        ),
    )

    await update_session(
        session.id,
        SessionUpdateRequest(title="Manueller Titel"),
        user,
        db,
    )

    assert session.title == "Manueller Titel"
    assert session.title_generation_status == TitleGenerationStatus.SKIPPED.value
    db.commit.assert_awaited_once()


async def test_session_can_be_pinned_during_active_run() -> None:
    user = _user()
    session_id = uuid4()
    session = ChatSession(
        id=session_id,
        user_id=user.id,
        title="Test",
        is_pinned=False,
        memory_mode="disabled",
        web_search_policy="available",
    )
    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = [session, False]

    result = await update_session(
        session_id,
        SessionUpdateRequest(pinned=True),
        user,
        db,
    )

    assert session.is_pinned is True
    assert result["pinned"] is True
    assert db.scalar.await_count == 2


async def test_control_center_state_can_be_replaced() -> None:
    user = _user()
    session = ChatSession(
        id=uuid4(),
        user_id=user.id,
        title="Test",
        memory_mode="disabled",
        control_center={
            "tabs": [{"app_id": "files", "pinned": False}],
            "active_app_id": "files",
            "conversation_fork": True,
        },
    )
    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = [session.id, session, False]
    state = {
        "tabs": [
            {"app_id": "files", "pinned": True},
            {"app_id": "terminal_2", "pinned": False},
        ],
        "active_app_id": "terminal_2",
        "open": True,
        "file_view": {
            "scope": "session",
            "selected_path": "drafts/draft-id/plan.md",
            "selected_kind": "file",
            "viewed_file_path": "drafts/draft-id/plan.md",
            "root_identity": None,
            "execution_agent_id": None,
            "current_directory": None,
        },
    }

    result = await update_session(
        session.id, SessionUpdateRequest(control_center=state), user, db
    )

    assert session.control_center == {
        "tabs": [
            _stored_app_tab("session"),
            _stored_app_tab("files", pinned=True),
            _stored_app_tab("terminal_2"),
        ],
        "active_app_id": "terminal_2",
        "open": True,
        "file_view": state["file_view"],
        "file_views": {"session": None, "filesystem": None},
    }
    assert result["controlCenter"] == {
        "tabs": [
            _api_app_tab("session"),
            _api_app_tab("files", pinned=True),
            _api_app_tab("terminal_2"),
        ],
        "activeAppId": "terminal_2",
        "open": True,
        "fileView": {
            "scope": "session",
            "selectedPath": "drafts/draft-id/plan.md",
            "selectedKind": "file",
            "viewedFilePath": "drafts/draft-id/plan.md",
            "rootIdentity": None,
            "executionAgentId": None,
            "currentDirectory": None,
        },
        "fileViews": {"session": None, "filesystem": None},
    }
    db.commit.assert_awaited_once()


async def test_todo_collapse_state_round_trips_and_survives_layout_updates() -> None:
    user = _user()
    session = ChatSession(
        id=uuid4(), user_id=user.id, title="Test", memory_mode="disabled",
        control_center={"tabs": [{"app_id": "files", "pinned": False}], "active_app_id": "files"},
    )
    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = [session.id, session, False] * 3
    assert session_payload(session)["todosCollapsed"] is False

    result = await update_session(session.id, SessionUpdateRequest(todos_collapsed=True), user, db)
    assert result["todosCollapsed"] is True
    assert result["controlCenter"]["activeAppId"] == "files"
    assert session.control_center["todos_collapsed"] is True
    assert db.scalar.await_count == 3  # Lock, ownership and trash availability.
    assert "FOR UPDATE" in str(db.scalar.await_args_list[0].args[0])

    result = await update_session(
        session.id, SessionUpdateRequest(control_center={"tabs": [], "active_app_id": None}), user, db,
    )
    assert result["todosCollapsed"] is True
    result = await update_session(session.id, SessionUpdateRequest(todos_collapsed=False), user, db)
    assert result["todosCollapsed"] is False
    with pytest.raises(ValidationError):
        SessionUpdateRequest(todos_collapsed="false")


async def test_changing_computer_preserves_terminal_preference(monkeypatch) -> None:
    from mato_loop_backend.sessions import service

    user = _user()
    session = ChatSession(
        id=uuid4(), user_id=user.id, title="Test", memory_mode="disabled",
        preferred_execution_agent_id=uuid4(),
        control_center={"terminal_auto_attach": True, "tabs": [{"app_id": "terminal", "pinned": False}],
                        "active_app_id": "terminal"},
    )
    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = [session.id, session, False, None]
    db.scalars.return_value = MagicMock(all=lambda: [])
    monkeypatch.setattr(service, "load_effective_ai", AsyncMock(return_value=AiSettings()))
    monkeypatch.setattr(service, "validate_execution_preferences", AsyncMock(return_value=(None, None)))
    monkeypatch.setattr(service, "add_directory_grant", AsyncMock())
    await update_session(
        session.id, SessionUpdateRequest(preferred_execution_agent_id=uuid4()), user, db,
    )
    assert session.control_center["terminal_auto_attach"] is True
    assert session.control_center["tabs"][0]["app_id"] == "terminal"


@pytest.mark.parametrize("preference", [None, False, True])
async def test_control_center_layout_discards_legacy_terminal_preference(preference) -> None:
    user = _user()
    session = ChatSession(
        id=uuid4(), user_id=user.id, title="Test", memory_mode="disabled",
        control_center={
            "tabs": [{"app_id": "terminal", "pinned": False}],
            "active_app_id": "terminal",
            **({"terminal_auto_attach": preference} if preference is not None else {}),
        },
    )
    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = [session.id, session, False] * 2
    assert session_payload(session)["controlCenter"]["activeAppId"] == "terminal"
    result = await update_session(
        session.id, SessionUpdateRequest(control_center={
            "tabs": [{"app_id": "terminal", "pinned": False}],
            "active_app_id": "terminal", "open": True,
        }), user, db,
    )
    assert "terminal_auto_attach" not in session.control_center
    assert result["controlCenter"]["activeAppId"] == "terminal"
    assert "FOR UPDATE" in str(db.scalar.await_args_list[0].args[0])

    await update_session(
        session.id, SessionUpdateRequest(control_center={"tabs": [], "active_app_id": None}), user, db,
    )
    assert "terminal_auto_attach" not in session.control_center


def test_control_center_state_accepts_a_session_file_tab() -> None:
    root_id = uuid4()
    tab_id = f"file-{uuid4()}"

    request = SessionUpdateRequest(
        control_center={
            "tabs": [
                {
                    "tab_id": tab_id,
                    "kind": "file",
                    "app_id": "file-viewer",
                    "pinned": False,
                    "file_target": {
                        "scope": "session",
                        "path": "notes/plan.md",
                        "name": "plan.md",
                        "root_identity": str(root_id),
                    },
                }
            ],
            "active_app_id": tab_id,
        }
    )

    assert request.control_center is not None
    assert request.control_center.tabs[0].app_id == "session"
    assert request.control_center.tabs[1].file_target is not None
    assert request.control_center.tabs[1].file_target.path == "notes/plan.md"


def test_historical_file_targets_require_read_only_identity_and_revision() -> None:
    from mato_loop_backend.api.routes.sessions import ControlCenterFileTarget
    from mato_loop_backend.sessions.serialization import _control_center_tab_payload

    target = {
        "scope": "session", "path": "notes.md", "name": "notes.md",
        "root_identity": str(uuid4()), "file_id": str(uuid4()), "revision": 3,
        "read_only": True,
    }
    parsed = ControlCenterFileTarget.model_validate(target)
    serialized = _control_center_tab_payload({
        "tab_id": f"file-{uuid4()}", "app_id": "file-viewer", "kind": "file",
        "pinned": False, "file_target": parsed.model_dump(),
    })["fileTarget"]
    assert serialized["fileId"] == target["file_id"]
    assert serialized["revision"] == 3
    assert serialized["readOnly"] is True
    for invalid in ({"read_only": False}, {"file_id": None}, {"revision": None}, {"revision": 0}):
        with pytest.raises(ValueError):
            ControlCenterFileTarget.model_validate({**target, **invalid})


def test_control_center_state_preserves_twenty_legacy_tabs_when_adding_session() -> None:
    request = SessionUpdateRequest(
        control_center={
            "tabs": [
                {"app_id": f"app_{index}", "pinned": False}
                for index in range(20)
            ],
            "active_app_id": "app_0",
        }
    )

    assert request.control_center is not None
    assert len(request.control_center.tabs) == 21
    assert request.control_center.tabs[0].app_id == "session"
    assert request.control_center.active_app_id == "app_0"


@pytest.mark.parametrize(
    "state",
    [
        {
            "tabs": [
                {"app_id": "files", "pinned": False},
                {"app_id": "files", "pinned": True},
            ],
            "active_app_id": "files",
        },
        {
            "tabs": [{"app_id": "files", "pinned": False}],
            "active_app_id": "files",
            "file_view": {
                "scope": "session",
                "selected_path": "drafts/../secret.txt",
                "selected_kind": "file",
            },
        },
        {
            "tabs": [{"app_id": "files", "pinned": False}],
            "active_app_id": "files",
            "file_view": {
                "scope": "filesystem",
                "selected_path": "report.txt",
                "selected_kind": "file",
                "viewed_file_path": "other.txt",
            },
        },
        {
            "tabs": [{"app_id": "files", "pinned": False}],
            "active_app_id": "files",
            "file_view": {
                "scope": "filesystem",
                "selected_path": "/work/../secret.txt",
                "selected_kind": "file",
                "execution_agent_id": "550e8400-e29b-41d4-a716-446655440001",
                "current_directory": "/work",
            },
        },
        {
            "tabs": [{"app_id": "../files", "pinned": False}],
            "active_app_id": "../files",
        },
        {"tabs": [], "active_app_id": "files"},
        {
            "tabs": [
                {"app_id": f"app_{index}", "pinned": False}
                for index in range(21)
            ],
            "active_app_id": "app_0",
        },
        {
            "tabs": [
                {
                    "tab_id": "file-x",
                    "kind": "file",
                    "app_id": "file-viewer",
                    "pinned": False,
                    "file_target": {
                        "scope": "session",
                        "path": "plan.md",
                        "name": "plan.md",
                        "root_identity": str(uuid4()),
                    },
                }
            ],
            "active_app_id": "file-x",
        },
        {
            "tabs": [
                {
                    "tab_id": "file-550e8400-e29b-41d4-a716-446655440000",
                    "kind": "file",
                    "app_id": "file-viewer",
                    "pinned": False,
                    "file_target": {
                        "scope": "filesystem",
                        "path": "/work/../secret.txt",
                        "name": "secret.txt",
                        "execution_agent_id": "550e8400-e29b-41d4-a716-446655440001",
                        "current_directory": "/work",
                    },
                }
            ],
            "active_app_id": "file-550e8400-e29b-41d4-a716-446655440000",
        },
        None,
    ],
)
def test_control_center_state_rejects_invalid_full_state(state) -> None:
    with pytest.raises(ValidationError):
        SessionUpdateRequest(control_center=state)


async def test_control_center_state_keeps_the_fixed_session_tab() -> None:
    user = _user()
    session = ChatSession(
        id=uuid4(),
        user_id=user.id,
        title="Test",
        memory_mode="disabled",
    )
    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = [session.id, session, False]

    result = await update_session(
        session.id,
        SessionUpdateRequest(control_center={"tabs": [], "active_app_id": None}),
        user,
        db,
    )

    assert session.control_center == {
        "tabs": [_stored_app_tab("session")],
        "active_app_id": "session",
        "open": False,
        "file_view": None,
        "file_views": {"session": None, "filesystem": None},
    }
    assert result["controlCenter"] == {
        "tabs": [_api_app_tab("session")],
        "activeAppId": "session",
        "open": False,
        "fileView": None,
        "fileViews": {"session": None, "filesystem": None},
    }


async def test_control_center_state_can_change_during_active_run() -> None:
    user = _user()
    session = ChatSession(
        id=uuid4(),
        user_id=user.id,
        title="Test",
        memory_mode="disabled",
    )
    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = [session.id, session, False]

    await update_session(
        session.id,
        SessionUpdateRequest(
            control_center={
                "tabs": [{"app_id": "files", "pinned": True}],
                "active_app_id": "files",
            }
        ),
        user,
        db,
    )

    assert session.control_center["tabs"][1]["pinned"] is True
    assert db.scalar.await_count == 3
    assert "FOR UPDATE" in str(db.scalar.await_args_list[0].args[0])


async def test_control_center_update_is_owner_scoped() -> None:
    owner = _user()
    other = _user()
    session = ChatSession(id=uuid4(), user_id=owner.id, title="Privat")
    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = [None, None]

    with pytest.raises(ApiError) as exc_info:
        await update_session(
            session.id,
            SessionUpdateRequest(control_center={"tabs": [], "active_app_id": None}),
            other,
            db,
        )

    assert exc_info.value.code == "session.notFound"
    db.flush.assert_not_awaited()


async def test_project_cannot_change_during_active_run() -> None:
    user = _user()
    session_id = uuid4()
    session = ChatSession(
        id=session_id,
        user_id=user.id,
        title="Test",
        memory_mode="disabled",
        web_search_policy="available",
    )
    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = [session.id, session, False]
    db.scalars.return_value = MagicMock(all=lambda: [uuid4()])

    with pytest.raises(ApiError) as exc_info:
        await update_session(
            session_id,
            SessionUpdateRequest(project_id=uuid4()),
            user,
            db,
        )

    assert exc_info.value.code == "session.runAlreadyActive"


async def test_model_change_preserves_active_run_settings(monkeypatch) -> None:
    from mato_loop_backend.api.routes import sessions as routes
    from mato_loop_backend.sessions import service

    user = _user()
    session_id = uuid4()
    provider = LlmProviderConfig(name="Cloud", adapter_type="openai-compatible")
    model = ModelConfig(provider_id=provider.id, model_id="chat")
    session = ChatSession(
        id=session_id,
        user_id=user.id,
        title="Test",
        memory_mode="disabled",
        web_search_policy="available",
        session_profile={},
    )
    run = AgentRun(id=uuid4(), session_id=session_id, status="running")
    waiting = AgentRun(id=uuid4(), session_id=session_id, status="waiting_approval")
    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = [session.id, session, False] * 2
    db.scalars.return_value = MagicMock(all=lambda: [run.id, waiting.id])
    monkeypatch.setattr(AgentRuntime, "_session_settings", {})
    monkeypatch.setattr(AgentRuntime, "_tasks", {key: SimpleNamespace(done=lambda: False) for key in (run.id, waiting.id)})
    monkeypatch.setattr(service, "load_effective_ai", AsyncMock(return_value=AiSettings(providers=[provider], models=[model])))
    monkeypatch.setattr(routes, "_session_payload", AsyncMock(return_value={}))

    await update_session(session_id, SessionUpdateRequest(model_id=model.id), user, db)
    await update_session(session_id, SessionUpdateRequest(reasoning_effort="high"), user, db)
    saved_model = session.model_config_id
    apply_run_session_settings(session, waiting)
    assert session.model_config_id is None
    apply_run_session_settings(session, run)

    assert (saved_model, session.model_config_id, session.reasoning_effort, inspect(session).attrs.model_config_id.history.has_changes()) == (model.id, None, None, False)
