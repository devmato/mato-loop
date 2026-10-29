import asyncio
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend.api.errors import ApiError
from mato_loop_backend.api.routes import users as user_routes
from mato_loop_backend.models import Project, UserAccount
from mato_loop_backend.settings import SettingsStore, UserPreferences


def _user(*, admin: bool = False, active: bool = True, password_hash: str | None = "hash"):
    return UserAccount(
        id=uuid4(),
        username=f"user-{uuid4().hex}",
        password_hash=password_hash,
        is_admin=admin,
        is_active=active,
    )


def test_update_requires_an_actual_value() -> None:
    with pytest.raises(ValidationError):
        user_routes.UserUpdateRequest()
    with pytest.raises(ValidationError):
        user_routes.UserUpdateRequest(is_admin=None)


async def test_language_preference_is_owner_bound_and_preserves_other_preferences(monkeypatch):
    user = _user()
    settings = UserPreferences(dark_mode=True)
    load = AsyncMock(return_value=settings)
    save = AsyncMock()
    monkeypatch.setattr(SettingsStore, "load", load)
    monkeypatch.setattr(SettingsStore, "save", save)
    db = AsyncMock(spec=AsyncSession)
    result = await user_routes.update_user_preferences(user_routes.UserPreferencesRequest(preferredLanguage="de-DE"), user, db)
    assert (result, settings.dark_mode, load.await_args.kwargs, save.await_args.kwargs) == (
        {"preferredLanguage": "de-DE"}, True, {"user_id": user.id, "for_update": True}, {"user_id": user.id},
    )


@pytest.mark.parametrize("value", ["", "de_DE", "de\r\nX: 1", "x" * 36])
def test_language_preference_rejects_invalid_tags(value):
    with pytest.raises(ValidationError):
        user_routes.UserPreferencesRequest(preferredLanguage=value)


async def test_create_user_maps_duplicate_username(monkeypatch) -> None:
    db = AsyncMock(spec=AsyncSession)
    db.commit.side_effect = IntegrityError("insert", {}, Exception("duplicate"))
    monkeypatch.setattr(user_routes, "hash_password", lambda _password: "hashed")

    with pytest.raises(ApiError) as exc_info:
        await user_routes.create_user(
            user_routes.UserCreateRequest(username="test", password="long-password"),
            _user(admin=True),
            db,
        )

    assert exc_info.value.code == "user.usernameExists"
    assert exc_info.value.field == "username"
    db.rollback.assert_awaited_once()


async def test_update_refuses_removing_last_active_admin() -> None:
    admin = _user(admin=True)
    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = [admin, 0]

    with pytest.raises(ApiError) as exc_info:
        await user_routes.update_user(
            admin.id, user_routes.UserUpdateRequest(is_admin=False), admin, db
        )

    assert exc_info.value.code == "user.lastAdmin"
    assert admin.is_admin
    db.commit.assert_not_awaited()


async def test_update_deactivates_user_and_revokes_sessions(monkeypatch) -> None:
    admin = _user(admin=True)
    target = _user(admin=True)
    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = [target, 1]
    revoke = AsyncMock()
    quiesce = AsyncMock()
    monkeypatch.setattr(user_routes, "revoke_login_sessions", revoke)
    monkeypatch.setattr(user_routes, "_shielded_quiesce", quiesce)

    result = await user_routes.update_user(
        target.id, user_routes.UserUpdateRequest(is_active=False), admin, db
    )

    assert result["isActive"] is False
    revoke.assert_awaited_once_with(db, target.id)
    quiesce.assert_awaited_once_with(db, target.id)
    db.commit.assert_awaited_once()


async def test_update_refuses_self_deactivation() -> None:
    admin = _user(admin=True)
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = admin

    with pytest.raises(ApiError) as exc_info:
        await user_routes.update_user(
            admin.id, user_routes.UserUpdateRequest(is_active=False), admin, db
        )

    assert exc_info.value.code == "user.cannotDeactivateSelf"


async def test_quiesce_cancels_agent_and_dream_runs(monkeypatch) -> None:
    user_id = uuid4()
    run_id = uuid4()
    db = AsyncMock(spec=AsyncSession)
    result = MagicMock()
    result.all.return_value = [run_id]
    db.scalars.return_value = result
    coding_result = MagicMock()
    coding_result.all.return_value = []
    db.execute.side_effect = [MagicMock(), coding_result, MagicMock()]
    cancel = AsyncMock(return_value=True)
    cancel_titles = AsyncMock(return_value=True)
    wait = AsyncMock()
    monkeypatch.setattr(user_routes.AgentRuntime, "cancel_and_wait", cancel)
    monkeypatch.setattr(
        user_routes.AgentRuntime,
        "cancel_and_wait_user_title_tasks",
        cancel_titles,
    )
    monkeypatch.setattr(user_routes.DreamingService, "wait_for_user", wait)

    await user_routes._quiesce_user_jobs(db, user_id)

    cancel.assert_awaited_once_with(run_id)
    cancel_titles.assert_awaited_once_with(user_id)
    wait.assert_awaited_once_with(user_id)
    assert db.execute.await_count == 3
    assert "memory_dream_runs" in str(db.execute.await_args_list[0].args[0])
    assert "project_plan_executions" in str(db.execute.await_args_list[1].args[0])
    assert "agent_runs" in str(db.execute.await_args_list[2].args[0])


async def test_quiesce_continues_after_terminal_coding_race(monkeypatch) -> None:
    user_id = uuid4()
    projects = [
        Project(id=uuid4(), user_id=user_id, name="Project 1"),
        Project(id=uuid4(), user_id=user_id, name="Project 2"),
    ]
    db = AsyncMock(spec=AsyncSession)
    runs = MagicMock()
    runs.all.return_value = []
    db.scalars.return_value = runs
    coding = MagicMock()
    coding.all.return_value = [(uuid4(), projects[0]), (uuid4(), projects[1])]
    db.execute.side_effect = [MagicMock(), coding, MagicMock()]
    cancel = AsyncMock(
        side_effect=[
            user_routes.ProjectCodingError("project.executionNotCancellable"),
            None,
        ]
    )
    monkeypatch.setattr(user_routes.ProjectCodingService, "cancel", cancel)
    monkeypatch.setattr(user_routes.AgentRuntime, "cancel_and_wait_user_title_tasks", AsyncMock())
    monkeypatch.setattr(user_routes.DreamingService, "wait_for_user", AsyncMock())

    await user_routes._quiesce_user_jobs(db, user_id)

    assert cancel.await_count == 2


async def test_change_password_verifies_current_and_revokes_sessions(monkeypatch) -> None:
    user = _user()
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = user
    monkeypatch.setattr(user_routes, "verify_password", lambda password, _hash: password == "old")
    monkeypatch.setattr(user_routes, "hash_password", lambda _password: "new-hash")
    revoke = AsyncMock()
    monkeypatch.setattr(user_routes, "revoke_login_sessions", revoke)

    await user_routes.change_password(
        user_routes.PasswordChangeRequest(current_password="old", new_password="new-password"),
        user,
        db,
    )

    assert user.password_hash == "new-hash"
    revoke.assert_awaited_once_with(db, user.id)
    db.commit.assert_awaited_once()


async def test_change_password_rejects_invalid_current_password(monkeypatch) -> None:
    user = _user()
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = user
    monkeypatch.setattr(user_routes, "verify_password", lambda _password, _hash: False)

    with pytest.raises(ApiError) as exc_info:
        await user_routes.change_password(
            user_routes.PasswordChangeRequest(
                current_password="wrong", new_password="new-password"
            ),
            user,
            db,
        )

    assert exc_info.value.code == "auth.currentPasswordInvalid"
    db.commit.assert_not_awaited()


async def test_admin_password_reset_revokes_sessions(monkeypatch) -> None:
    target = _user(password_hash=None)
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = target
    monkeypatch.setattr(user_routes, "hash_password", lambda _password: "reset-hash")
    revoke = AsyncMock()
    monkeypatch.setattr(user_routes, "revoke_login_sessions", revoke)

    await user_routes.reset_password(
        target.id,
        user_routes.PasswordResetRequest(password="reset-password"),
        _user(admin=True),
        db,
    )

    assert target.password_hash == "reset-hash"
    revoke.assert_awaited_once_with(db, target.id)


async def test_delete_requires_deactivation() -> None:
    admin = _user(admin=True)
    target = _user(active=True)
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = target

    with pytest.raises(ApiError) as exc_info:
        await user_routes.delete_user(target.id, admin, db)

    assert exc_info.value.code == "user.deactivateBeforeDelete"
    db.delete.assert_not_awaited()


async def test_delete_removes_database_and_storage(monkeypatch, tmp_path: Path) -> None:
    admin = _user(admin=True)
    target = _user(active=False)
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = target
    session_result = MagicMock()
    session_result.all.return_value = [(uuid4(), None)]
    db.execute.return_value = session_result
    project_result = MagicMock()
    project_result.all.return_value = []
    db.scalars.return_value = project_result
    session_storage = MagicMock()
    session_storage.trash_session.return_value = tmp_path / "session-trash"
    quiesce = AsyncMock()
    monkeypatch.setattr(user_routes, "SessionStorage", lambda: session_storage)
    monkeypatch.setattr(user_routes, "_shielded_quiesce", quiesce)

    await user_routes.delete_user(target.id, admin, db)

    db.delete.assert_awaited_once_with(target)
    db.commit.assert_awaited_once()
    quiesce.assert_awaited_once_with(db, target.id)
    session_storage.purge_trashed.assert_called_once()
    session_storage.forget_sessions.assert_called_once_with([session_result.all.return_value[0][0]])


async def test_delete_restores_earlier_storage_when_later_trash_fails(
    monkeypatch, tmp_path: Path
) -> None:
    admin = _user(admin=True)
    target = _user(active=False)
    original_user_id = target.id
    sessions = [(uuid4(), uuid4()), (uuid4(), None)]
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = target
    session_result = MagicMock()
    session_result.all.return_value = sessions
    db.execute.return_value = session_result
    project_result = MagicMock()
    project_result.all.return_value = [sessions[0][1]]
    db.scalars.return_value = project_result
    session_storage = MagicMock()
    first_trash = tmp_path / "first-trash"
    session_storage.trash_project.return_value = first_trash
    session_storage.trash_session.side_effect = OSError("storage failed")
    monkeypatch.setattr(user_routes, "SessionStorage", lambda: session_storage)
    monkeypatch.setattr(user_routes, "_shielded_quiesce", AsyncMock())

    async def expire_user() -> None:
        target.id = None

    db.rollback.side_effect = expire_user

    with pytest.raises(OSError, match="storage failed"):
        await user_routes.delete_user(original_user_id, admin, db)

    db.rollback.assert_awaited_once()
    session_storage.restore_project.assert_called_once_with(sessions[0][1], first_trash)


async def test_delete_does_not_restore_storage_when_cancelled_commit_succeeds(
    monkeypatch, tmp_path: Path
) -> None:
    admin = _user(admin=True)
    target = _user(active=False)
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = target
    session_result = MagicMock()
    session_result.all.return_value = []
    db.execute.return_value = session_result
    project_result = MagicMock()
    project_result.all.return_value = []
    db.scalars.return_value = project_result
    commit_started = asyncio.Event()
    finish_commit = asyncio.Event()

    async def commit() -> None:
        commit_started.set()
        await finish_commit.wait()

    db.commit.side_effect = commit
    session_storage = MagicMock()
    monkeypatch.setattr(user_routes, "SessionStorage", lambda: session_storage)
    monkeypatch.setattr(user_routes, "_shielded_quiesce", AsyncMock())

    task = asyncio.create_task(user_routes.delete_user(target.id, admin, db))
    await commit_started.wait()
    task.cancel()
    finish_commit.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    session_storage.restore_session.assert_not_called()


async def test_delete_restores_project_when_cancelled_during_trash(
    monkeypatch, tmp_path: Path
) -> None:
    admin = _user(admin=True)
    target = _user(active=False)
    project_id = uuid4()
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = target
    session_result = MagicMock()
    session_result.all.return_value = []
    db.execute.return_value = session_result
    project_result = MagicMock()
    project_result.all.return_value = [project_id]
    db.scalars.return_value = project_result
    started, finish = threading.Event(), threading.Event()
    trashed = tmp_path / "project-trash"
    session_storage = MagicMock()

    def trash_project(_project_id):
        started.set()
        finish.wait()
        return trashed

    session_storage.trash_project.side_effect = trash_project
    monkeypatch.setattr(user_routes, "SessionStorage", lambda: session_storage)
    monkeypatch.setattr(user_routes, "_shielded_quiesce", AsyncMock())

    task = asyncio.create_task(user_routes.delete_user(target.id, admin, db))
    await asyncio.to_thread(started.wait)
    task.cancel()
    finish.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    session_storage.restore_project.assert_called_once_with(project_id, trashed)
    db.rollback.assert_awaited_once()


async def test_delete_restores_standalone_session_when_cancelled_during_trash(
    monkeypatch, tmp_path: Path
) -> None:
    admin = _user(admin=True)
    target = _user(active=False)
    session_id = uuid4()
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = target
    session_result = MagicMock()
    session_result.all.return_value = [(session_id, None)]
    db.execute.return_value = session_result
    project_result = MagicMock()
    project_result.all.return_value = []
    db.scalars.return_value = project_result
    started, finish = threading.Event(), threading.Event()
    trashed = tmp_path / "session-trash"
    session_storage = MagicMock()

    def trash_session(_session_id, _project_id):
        started.set()
        finish.wait()
        return trashed

    session_storage.trash_session.side_effect = trash_session
    monkeypatch.setattr(user_routes, "SessionStorage", lambda: session_storage)
    monkeypatch.setattr(user_routes, "_shielded_quiesce", AsyncMock())

    task = asyncio.create_task(user_routes.delete_user(target.id, admin, db))
    await asyncio.to_thread(started.wait)
    task.cancel()
    finish.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    session_storage.restore_session.assert_called_once_with(session_id, trashed)
    db.rollback.assert_awaited_once()


def test_static_password_route_precedes_dynamic_route() -> None:
    paths = [route.path for route in user_routes.router.routes]
    assert paths.index("/users/me/password") < paths.index("/users/{user_id}/password")
