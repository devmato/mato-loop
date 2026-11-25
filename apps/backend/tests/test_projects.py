from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend.agent.runtime import AgentRuntime
from mato_loop_backend.api.errors import ApiError, register_error_handlers
from mato_loop_backend.api.routes.projects import (
    ProjectCreateRequest,
    ProjectOrderRequest,
    ProjectRequest,
    create_project,
    delete_project,
    list_projects,
    project_delete_impact,
    project_delete_impact_token,
    reorder_projects,
    router,
    update_project,
)
from mato_loop_backend.db.session import get_db_session
from mato_loop_backend.error_contract import describe_error
from mato_loop_backend.models import Automation, ChatSession, Project, UserAccount
from mato_loop_backend.security import require_user
from mato_loop_backend.settings import SettingsStore, UserPreferences

pytestmark = pytest.mark.usefixtures("no_active_voice")


def _user() -> UserAccount:
    return UserAccount(id=uuid4(), username="test", password_hash="hash")


def _project(user_id) -> Project:
    now = datetime(2026, 8, 18, 12, tzinfo=UTC)
    return Project(
        id=uuid4(),
        user_id=user_id,
        name="Project",
        instructions="",
        created_at=now,
        updated_at=now,
    )


def _rows(*items):
    result = MagicMock()
    result.all.return_value = list(items)
    return result


@pytest.fixture(autouse=True)
def no_descendants(monkeypatch):
    monkeypatch.setattr(AgentRuntime, "cancel_descendants", AsyncMock())
    monkeypatch.setattr("mato_loop_backend.api.routes.projects.descendant_sessions", AsyncMock(return_value=[]))


def test_project_model_enforces_owner_binding_and_chat_sessions() -> None:
    project_unique = next(
        constraint
        for constraint in Project.__table__.constraints
        if isinstance(constraint, UniqueConstraint) and constraint.name == "uq_projects_id_user_id"
    )
    project_fk = next(
        constraint
        for constraint in ChatSession.__table__.constraints
        if isinstance(constraint, ForeignKeyConstraint)
        and constraint.name == "fk_chat_sessions_project_owner"
    )
    project_check = next(
        constraint
        for constraint in ChatSession.__table__.constraints
        if isinstance(constraint, CheckConstraint)
        and constraint.name == "ck_chat_sessions_project_chat_sessions_only"
    )

    assert [column.name for column in project_unique.columns] == ["id", "user_id"]
    assert [column.name for column in project_fk.columns] == ["project_id", "user_id"]
    assert str(project_check.sqltext) == "project_id IS NULL OR session_type = 'chat'"
    assert ChatSession.project_id.nullable is True


def test_project_fields_are_trimmed_and_bounded() -> None:
    agent_id = uuid4()
    request = ProjectRequest(name="  Work  ", instructions="  Use Python.  ")
    assert request.name == "Work"
    assert request.instructions == "Use Python."
    assert request.git_fetch_before_actions is True
    assert ProjectRequest(
        name="Work", defaultExecutionAgentId=str(agent_id)
    ).default_execution_agent_id == agent_id
    with pytest.raises(ValidationError):
        ProjectRequest(name="   ")
    with pytest.raises(ValidationError):
        ProjectRequest(name="x" * 241)
    with pytest.raises(ValidationError):
        ProjectRequest(name="Work", instructions="x" * 4001)
    with pytest.raises(ValidationError):
        ProjectCreateRequest(
            name="Work",
            codingService={
                "integrationId": str(uuid4()),
                "projectKey": "project-1",
                "workspaceKey": "repo",
            },
        )


@pytest.mark.parametrize("kind", ["session", "project"])
async def test_explicit_soft_delete_rejects_disabled_trash(monkeypatch, kind) -> None:
    from mato_loop_backend.api.routes import projects, sessions

    user = _user()
    item = ChatSession(id=uuid4(), user_id=user.id) if kind == "session" else _project(user.id)
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = item
    monkeypatch.setattr(SettingsStore, "load", AsyncMock(return_value=UserPreferences(trash_enabled=False)))
    if kind == "session":
        monkeypatch.setattr(sessions, "_deletable_session", AsyncMock(return_value=item))
        route = sessions.delete_session
        permanent_delete = AsyncMock()
        monkeypatch.setattr(sessions.session_service, "delete_session", permanent_delete)
    else:
        monkeypatch.setattr(projects, "_owned_project", AsyncMock(return_value=item))
        route = projects.delete_project
        permanent_delete = AsyncMock()
        monkeypatch.setattr(projects, "purge_project", permanent_delete)

    with pytest.raises(ApiError) as exc:
        await route(item.id, user, db, permanent=False)

    assert exc.value.code == "trash.disabled"
    permanent_delete.assert_not_awaited()


@pytest.mark.parametrize("targeted", [False, True])
async def test_project_list_is_owner_scoped_and_counts_sessions(monkeypatch, targeted) -> None:
    user = _user()
    project = _project(user.id)
    project.coding_integration_id = uuid4()
    project.coding_project_key = "project-1"
    project.coding_workspace_key = "repo"
    project.coding_analysis_model_key = "provider/analyze"
    project.coding_analysis_model_variant = "high"
    project.coding_execution_model_key = "provider/build"
    project.coding_execution_model_variant = "max"
    result = MagicMock()
    result.all.return_value = [(project, 3, True)]
    db = AsyncMock(spec=AsyncSession)
    db.execute.return_value = result
    monkeypatch.setattr(SettingsStore, "load", AsyncMock(return_value=UserPreferences()))

    project_ids = [project.id, uuid4()] if targeted else None
    response = await list_projects(user, db, project_ids=project_ids)

    assert response.items[0].session_count == 3
    assert response.items[0].has_unread_messages is True
    assert response.items[0].git_fetch_before_actions is True
    assert response.items[0].coding_service["analysisModelVariant"] == "high"
    assert response.items[0].coding_service["executionModelVariant"] == "max"
    statement = db.execute.await_args.args[0]
    assert user.id in statement.compile().params.values()
    assert "LEFT OUTER JOIN chat_sessions" in str(statement)
    assert "projects.user_id =" in str(statement.whereclause)
    assert "projects.archived_at IS NULL" in str(statement.whereclause)
    if targeted:
        assert project_ids in statement.compile().params.values()
        assert "projects.id IN" in str(statement.whereclause)


def test_project_status_view_is_owner_scoped_and_skips_full_settings(monkeypatch):
    user = _user()
    project = _project(user.id)
    db = AsyncMock(spec=AsyncSession)
    db.execute.side_effect = [_rows((project.id, 3, True)), _rows((project, 3, True))]
    preferences = AsyncMock(return_value=UserPreferences())
    monkeypatch.setattr(SettingsStore, "load", preferences)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db_session] = lambda: db
    app.dependency_overrides[require_user] = lambda: user

    with TestClient(app) as client:
        status_response = client.get("/projects", params={"view": "status", "projectIds": str(project.id)})
        full_response = client.get("/projects", params={"projectIds": str(project.id)})

    assert status_response.json() == {"items": [{"id": str(project.id), "sessionCount": 3, "hasUnreadMessages": True}]}
    assert full_response.json()["items"][0]["instructions"] == project.instructions
    preferences.assert_awaited_once()
    statement = db.execute.await_args_list[0].args[0]
    sql = str(statement)
    assert user.id in statement.compile().params.values()
    assert "projects.archived_at IS NULL" in sql and "projects.delete_at IS NULL" in sql
    assert "chat_sessions.parent_session_id IS NULL" in sql and "chat_sessions.delete_at IS NULL" in sql
    assert "WITH RECURSIVE" not in sql and "projects.instructions" not in sql


@pytest.mark.parametrize("project_ids", [["invalid"], [str(uuid4())] * 101])
def test_project_list_rejects_invalid_or_unbounded_targets(project_ids) -> None:
    db = AsyncMock(spec=AsyncSession)

    async def override_db():
        yield db

    app = FastAPI()
    register_error_handlers(app)
    app.include_router(router)
    app.dependency_overrides[require_user] = _user
    app.dependency_overrides[get_db_session] = override_db

    response = TestClient(app).get("/projects", params=[("projectIds", value) for value in project_ids])

    assert response.status_code == 422
    db.execute.assert_not_awaited()


async def test_project_order_is_saved_and_used_by_subsequent_lists(monkeypatch) -> None:
    user = _user()
    first, second = _project(user.id), _project(user.id)
    archived_id, deleted_id = uuid4(), uuid4()
    preferences = UserPreferences(project_order=[first.id, second.id, archived_id, deleted_id])
    monkeypatch.setattr(SettingsStore, "load", AsyncMock(return_value=preferences))
    monkeypatch.setattr(SettingsStore, "save", AsyncMock())
    db = AsyncMock(spec=AsyncSession)
    db.scalars.return_value = [first.id, second.id, archived_id]
    db.execute.return_value = _rows((first, 0, False), (second, 0, False))

    await reorder_projects(ProjectOrderRequest(projectIds=[second.id, first.id]), user, db)
    response = await list_projects(user, db)

    assert [item.id for item in response.items] == [second.id, first.id]
    assert preferences.project_order == [second.id, first.id, archived_id]
    SettingsStore.save.assert_awaited_once_with(db, preferences, user_id=user.id)
    db.commit.assert_awaited_once()


async def test_project_order_rejects_unowned_or_missing_projects(monkeypatch) -> None:
    user = _user()
    db = AsyncMock(spec=AsyncSession)
    db.scalars.return_value = []
    save = AsyncMock()
    monkeypatch.setattr(SettingsStore, "save", save)

    with pytest.raises(ApiError):
        await reorder_projects(ProjectOrderRequest(projectIds=[uuid4()]), user, db)

    assert user.id in db.scalars.await_args.args[0].compile().params.values()
    save.assert_not_awaited()


def test_project_order_rejects_duplicates() -> None:
    project_id = uuid4()
    with pytest.raises(ValidationError):
        ProjectOrderRequest(projectIds=[project_id, project_id])


@pytest.mark.parametrize("fetch_before_actions", [True, False])
async def test_create_and_update_project_commit_owner_scoped_changes(fetch_before_actions) -> None:
    user = _user()
    now = datetime(2026, 8, 18, 12, tzinfo=UTC)
    db = AsyncMock(spec=AsyncSession)

    def persist(project: Project) -> None:
        project.id = uuid4()
        project.created_at = now
        project.updated_at = now

    db.add.side_effect = persist
    created = await create_project(
        ProjectCreateRequest(
            name=" New ", instructions=" Use Python. ", gitFetchBeforeActions=fetch_before_actions,
            iconInitials=" Ä2 ", iconColor="teal",
        ), user, db
    )

    project = _project(user.id)
    db.scalar.side_effect = [project, 2, True]
    updated = await update_project(
        project.id,
        ProjectRequest(
            name=" Renamed ", instructions=" Be concise. ", git_fetch_before_actions=fetch_before_actions,
            iconInitials="  ", iconColor=None,
        ),
        user,
        db,
    )

    assert created.name == "New"
    assert created.instructions == "Use Python."
    assert (created.icon_initials, created.icon_color) == ("Ä2", "teal")
    assert created.coding_service is None
    assert created.model_dump(by_alias=True)["gitFetchBeforeActions"] is fetch_before_actions
    assert db.add.call_args.args[0].git_fetch_before_actions is fetch_before_actions
    assert updated.name == "Renamed"
    assert updated.instructions == "Be concise."
    assert updated.icon_initials is updated.icon_color is None
    assert updated.session_count == 2
    assert updated.model_dump(by_alias=True)["gitFetchBeforeActions"] is fetch_before_actions
    assert project.git_fetch_before_actions is fetch_before_actions
    owner_query = db.scalar.await_args_list[0].args[0]
    assert user.id in owner_query.compile().params.values()
    assert db.commit.await_count == 2


async def test_update_project_preserves_omitted_instructions() -> None:
    user = _user()
    project = _project(user.id)
    project.instructions = "Keep this."
    project.icon_initials = "AB"
    project.icon_color = "violet"
    project.git_fetch_before_actions = False
    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = [project, 0, False]

    updated = await update_project(
        project.id, ProjectRequest(name="Renamed"), user, db
    )

    assert updated.instructions == "Keep this."
    assert (updated.icon_initials, updated.icon_color) == ("AB", "violet")
    assert updated.git_fetch_before_actions is False


@pytest.mark.parametrize("fields", [{"iconInitials": "ABC"}, {"iconInitials": "A!"}, {"iconColor": "#ffffff"}])
def test_project_icon_rejects_invalid_values(fields) -> None:
    with pytest.raises(ValidationError):
        ProjectRequest(name="Work", **fields)


async def test_update_project_validates_changed_session_defaults(monkeypatch) -> None:
    from mato_loop_backend.api.routes import projects as projects_route

    user = _user()
    project = _project(user.id)
    agent_id = uuid4()
    validate = AsyncMock(return_value=("/srv/project", "ask"))
    monkeypatch.setattr(projects_route, "validate_execution_preferences", validate)
    template_id = uuid4()
    load_template = AsyncMock()
    monkeypatch.setattr(projects_route, "load_template", load_template)
    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = [project, 0, False]

    updated = await update_project(
        project.id,
        ProjectRequest(
            name="Project",
            defaultExecutionAgentId=agent_id,
            defaultCurrentDirectory="/srv/project",
            defaultSessionTemplateId=template_id,
        ),
        user,
        db,
    )

    validate.assert_awaited_once_with(db, user.id, agent_id, "/srv/project")
    assert updated.default_execution_agent_id == agent_id
    assert updated.default_current_directory == "/srv/project"
    load_template.assert_awaited_once_with(db, user.id, template_id)
    assert updated.default_session_template_id == template_id


async def test_project_delete_impact_counts_sessions_and_linked_automations() -> None:
    user = _user()
    project = _project(user.id)
    automations = [
        Automation(
            id=uuid4(),
            user_id=user.id,
            execution_session_id=uuid4(),
            name="Automation",
            task="Task",
            revision=index,
        )
        for index in (1, 3)
    ]
    session_ids = [uuid4(), uuid4(), uuid4(), uuid4()]
    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = [project, None, 2]
    db.scalars.side_effect = [
        _rows(*reversed(session_ids)),
        _rows(*session_ids),
        _rows(*automations),
    ]

    result = await project_delete_impact(project.id, user, db)

    assert result == {
        "sessionCount": 4,
        "sessionIds": [str(item) for item in sorted(session_ids)],
        "automationCount": 2,
        "requiredAutomationCount": 2,
        "fileReferenceCount": 2,
        "automationImpact": project_delete_impact_token(session_ids, automations),
    }
    linked_query = db.scalars.await_args_list[2].args[0]
    assert user.id in linked_query.compile().params.values()
    assert project.id in linked_query.compile().params.values()
    assert project_delete_impact_token(session_ids[:-1], automations) != result[
        "automationImpact"
    ]


async def test_delete_project_with_sessions_trashes_tree_and_database_rows(
    tmp_path, monkeypatch
) -> None:
    from mato_loop_backend.api.routes import projects as projects_route
    from mato_loop_backend.files.storage import SessionStorage

    user = _user()
    project = _project(user.id)
    storage = SessionStorage(tmp_path / "sessions")
    session = ChatSession(
        id=uuid4(), user_id=user.id, project_id=project.id, title="Session"
    )
    storage.provision(session.id, project.id, user_id=user.id)
    shared_file = storage.logical_root_path(session.id, project.id) / "files" / "shared.txt"
    shared_file.write_text("shared", encoding="utf-8")
    monkeypatch.setattr(projects_route, "SessionStorage", lambda: storage)
    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = [project, project, project, 0, None]
    db.scalars.side_effect = [
        _rows(session.id),
        _rows(),
        _rows(),
        _rows(session),
        _rows(),
        _rows(),
    ]
    db.execute.return_value = MagicMock(all=lambda: [])

    await delete_project(
        project.id,
        user,
        db,
        permanent=True,
        automation_impact=project_delete_impact_token([session.id], []),
    )

    assert [call.args[0] for call in db.delete.await_args_list] == [project]
    db.commit.assert_awaited_once()
    assert not shared_file.exists()
    assert "DELETE FROM chat_sessions" in str(db.execute.await_args.args[0])
    assert list(storage.trash_dir.iterdir()) == []
    assert storage._owners == {}
    locked_automation_query = db.scalars.await_args_list[4].args[0]
    assert locked_automation_query.get_execution_options()["populate_existing"] is True
    assert "FOR UPDATE" in str(locked_automation_query)


async def test_foreign_project_is_not_found() -> None:
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = None

    with pytest.raises(ApiError) as exc_info:
        await update_project(
            uuid4(), ProjectRequest(name="Hidden"), _user(), db
        )

    assert exc_info.value.code == "project.notFound"
    assert describe_error("project.notFound").message == "The project was not found."
    assert describe_error("session.automationOutputLinked").message


async def test_delete_project_enforces_automation_mode_and_impact() -> None:
    user = _user()
    project = _project(user.id)
    session_id = uuid4()
    automation = Automation(
        id=uuid4(),
        user_id=user.id,
        execution_session_id=session_id,
        name="Required",
        task="Task",
        revision=2,
    )

    missing_mode_db = AsyncMock(spec=AsyncSession)
    missing_mode_db.scalar.return_value = project
    missing_mode_db.scalars.side_effect = [_rows(session_id), _rows(automation)]
    with pytest.raises(ApiError) as missing_mode:
        await delete_project(
            project.id,
            user,
            missing_mode_db,
            permanent=True,
            automation_impact=project_delete_impact_token([session_id], [automation]),
        )
    assert missing_mode.value.code == "session.automationOutputRequired"

    stale_db = AsyncMock(spec=AsyncSession)
    stale_db.scalar.return_value = project
    stale_db.scalars.side_effect = [_rows(session_id), _rows(automation)]
    with pytest.raises(ApiError) as stale:
        await delete_project(
            project.id,
            user,
            stale_db,
            automation_mode="deleteAutomations",
            permanent=True,
            automation_impact="0" * 64,
        )
    assert stale.value.code == "session.automationImpactChanged"

    unlink_db = AsyncMock(spec=AsyncSession)
    unlink_db.scalar.return_value = project
    unlink_db.scalars.side_effect = [_rows(session_id), _rows(automation)]
    with pytest.raises(ApiError) as unlink:
        await delete_project(
            project.id,
            user,
            unlink_db,
            automation_mode="unlink",
            permanent=True,
            automation_impact=project_delete_impact_token([session_id], [automation]),
        )
    assert unlink.value.code == "session.automationOutputRequired"


async def test_delete_project_revalidates_locked_session_membership() -> None:
    user = _user()
    project = _project(user.id)
    original_session = ChatSession(
        id=uuid4(), user_id=user.id, project_id=project.id, title="Original"
    )
    late_session = ChatSession(
        id=uuid4(), user_id=user.id, project_id=project.id, title="Late"
    )
    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = [project, project, project, None]
    db.scalars.side_effect = [
        _rows(original_session.id),
        _rows(),
        _rows(),
        _rows(original_session, late_session),
        _rows(),
    ]

    with pytest.raises(ApiError) as exc_info:
        await delete_project(
            project.id,
            user,
            db,
            permanent=True,
            automation_impact=project_delete_impact_token(
                [original_session.id], []
            ),
        )

    assert exc_info.value.code == "session.automationImpactChanged"
    db.delete.assert_not_awaited()


async def test_delete_project_rejects_files_referenced_outside_project(
    monkeypatch,
) -> None:
    user = _user()
    project = _project(user.id)
    child_id = uuid4()
    session = ChatSession(
        id=uuid4(), user_id=user.id, project_id=project.id, title="Referenced"
    )
    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = [project, project, project, 3]
    db.scalars.side_effect = [
        _rows(session.id),
        _rows(),
        _rows(),
        _rows(session),
        _rows(),
        _rows(),
    ]
    monkeypatch.setattr(
        "mato_loop_backend.api.routes.projects.descendant_sessions",
        AsyncMock(return_value=[(child_id, None)]),
    )

    with pytest.raises(ApiError) as exc_info:
        await delete_project(
            project.id,
            user,
            db,
            permanent=True,
            automation_impact=project_delete_impact_token([session.id], []),
        )

    assert exc_info.value.code == "session.filesReferenced"
    assert exc_info.value.params == {"referenceCount": 3}
    query = db.scalar.await_args_list[3].args[0].compile()
    assert {session.id, child_id} in [
        set(value) for value in query.params.values() if isinstance(value, list)
    ]
    db.delete.assert_not_awaited()


async def test_delete_project_rejects_uncancellable_active_database_run(
    monkeypatch,
) -> None:
    user = _user()
    project = _project(user.id)
    session_id = uuid4()
    run_id = uuid4()
    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = [project, project, run_id]
    db.scalars.side_effect = [
        _rows(session_id),
        _rows(),
        _rows(run_id),
    ]
    cancel = AsyncMock(return_value=False)
    monkeypatch.setattr(AgentRuntime, "cancel_and_wait", cancel)

    with pytest.raises(ApiError) as exc_info:
        await delete_project(
            project.id,
            user,
            db,
            permanent=True,
            automation_impact=project_delete_impact_token([session_id], []),
        )

    assert exc_info.value.code == "session.runAlreadyActive"
    cancel.assert_awaited_once_with(run_id)
    db.rollback.assert_awaited_once()


def test_delete_project_rejects_unknown_automation_mode() -> None:
    user = _user()
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = _project(user.id)
    db.scalars.return_value = _rows()

    async def override_db():
        yield db

    app = FastAPI()
    register_error_handlers(app)
    app.include_router(router)
    app.dependency_overrides[require_user] = lambda: user
    app.dependency_overrides[get_db_session] = override_db

    client = TestClient(app)
    missing_impact = client.delete(f"/projects/{uuid4()}?permanent=true")
    response = client.delete(
        f"/projects/{uuid4()}?automationMode=invalid&automationImpact={'0' * 64}"
    )

    assert missing_impact.status_code == 409
    assert missing_impact.json()["error"]["code"] == "session.automationImpactChanged"
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation.invalidChoice"
    assert response.json()["error"]["field"] == "automationMode"
    assert db.scalar.await_count == 2


async def test_delete_project_restores_storage_when_commit_fails(
    tmp_path, monkeypatch
) -> None:
    from mato_loop_backend.api.routes import projects as projects_route
    from mato_loop_backend.files.storage import SessionStorage

    user = _user()
    project = _project(user.id)
    storage = SessionStorage(tmp_path / "sessions")
    session = ChatSession(
        id=uuid4(), user_id=user.id, project_id=project.id, title="Session"
    )
    automation = Automation(
        id=uuid4(),
        user_id=user.id,
        execution_session_id=session.id,
        name="Automation",
        task="Task",
        revision=1,
    )
    storage.provision(session.id, project.id)
    shared_file = storage.logical_root_path(session.id, project.id) / "files" / "shared.txt"
    shared_file.write_text("shared", encoding="utf-8")
    execution_file = storage.logical_root_path(session.id, project.id) / "files" / "run.txt"
    execution_file.write_text("history", encoding="utf-8")
    monkeypatch.setattr(projects_route, "SessionStorage", lambda: storage)
    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = [project, project, project, 0, None]
    db.scalars.side_effect = [
        _rows(session.id),
        _rows(automation),
        _rows(),
        _rows(session),
        _rows(automation),
        _rows(),
    ]
    db.commit.side_effect = RuntimeError("commit failed")

    async def rollback_after_restore() -> None:
        assert shared_file.read_text(encoding="utf-8") == "shared"
        assert execution_file.read_text(encoding="utf-8") == "history"

    db.rollback.side_effect = rollback_after_restore

    with pytest.raises(RuntimeError, match="commit failed"):
        await delete_project(
            project.id,
            user,
            db,
            automation_mode="deleteAutomations",
            permanent=True,
            automation_impact=project_delete_impact_token(
                [session.id], [automation]
            ),
        )

    assert db.rollback.await_count == 2
    assert shared_file.read_text(encoding="utf-8") == "shared"
    assert execution_file.read_text(encoding="utf-8") == "history"
    assert list(storage.trash_dir.iterdir()) == []
