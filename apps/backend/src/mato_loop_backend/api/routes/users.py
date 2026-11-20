from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend.agent.runtime import AgentRuntime
from mato_loop_backend.agent.session_secrets import session_secrets
from mato_loop_backend.api.errors import ApiError
from mato_loop_backend.db.session import get_db_session
from mato_loop_backend.files.storage import SessionStorage
from mato_loop_backend.memory.dreaming import DreamingService
from mato_loop_backend.models import (
    AgentRun,
    Automation,
    ChatSession,
    MemoryDreamRun,
    Project,
    ProjectPlanExecution,
    UserAccount,
)
from mato_loop_backend.projects.coding import ProjectCodingError, ProjectCodingService
from mato_loop_backend.security import (
    hash_password,
    require_admin,
    require_user,
    revoke_login_sessions,
    verify_password,
)
from mato_loop_backend.sessions.state import deleted_session_clause
from mato_loop_backend.settings import SettingsStore, UserPreferences
from mato_loop_backend.settings.models import (
    DEFAULT_SESSION_TABS_LIMIT,
    MAX_SESSION_TABS_LIMIT,
    SessionTabs,
)

router = APIRouter(prefix="/users")
logger = logging.getLogger(__name__)
_ACTIVE_RUN_STATUSES = ("queued", "running", "waiting_approval", "waiting_input")


class UserCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=10, max_length=1024)
    is_admin: bool = False


class UserUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_admin: bool | None = None
    is_active: bool | None = None

    @model_validator(mode="after")
    def require_value(self) -> UserUpdateRequest:
        if self.is_admin is None and self.is_active is None:
            raise ValueError("at least one user setting is required")
        return self


class PasswordResetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: str = Field(min_length=10, max_length=1024)


class PasswordChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=10, max_length=1024)


class UserPreferencesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sessions_position: Literal["aboveProjects", "belowProjects"] = Field(
        default="belowProjects", alias="sessionsPosition",
    )
    session_tabs_enabled: bool = Field(default=False, alias="sessionTabsEnabled")
    sessions_page_size: int = Field(default=8, alias="sessionsPageSize", ge=1, le=100, strict=True)
    session_tabs_position: Literal["top", "bottom"] = Field(default="bottom", alias="sessionTabsPosition")
    session_tabs_order: Literal["project", "added"] = Field(default="project", alias="sessionTabsOrder")
    session_tabs_limit: int = Field(default=DEFAULT_SESSION_TABS_LIMIT, alias="sessionTabsLimit", ge=1, le=MAX_SESSION_TABS_LIMIT, strict=True)
    trash_enabled: bool = Field(default=False, alias="trashEnabled")
    trash_retention_days: int = Field(default=30, alias="trashRetentionDays", ge=1, le=3650)
    preferred_language: str | None = Field(
        default=None, alias="preferredLanguage", min_length=2, max_length=35,
        pattern=r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$",
    )


@router.get("/me/preferences")
async def get_user_preferences(
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict:
    settings = await SettingsStore.load(db, UserPreferences, user_id=user.id)
    return {
        "preferredLanguage": settings.preferred_language,
        "sessionsPosition": settings.sessions_position,
        "sessionsPageSize": settings.sessions_page_size,
        "sessionTabsEnabled": settings.session_tabs_enabled,
        "sessionTabsPosition": settings.session_tabs_position,
        "sessionTabsOrder": settings.session_tabs_order,
        "sessionTabsLimit": settings.session_tabs_limit,
        "trashEnabled": settings.trash_enabled,
        "trashRetentionDays": settings.trash_retention_days,
    }


@router.put("/me/preferences")
async def update_user_preferences(
    payload: UserPreferencesRequest,
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict:
    settings = await SettingsStore.load(db, UserPreferences, user_id=user.id, for_update=True)
    for field in payload.model_fields_set:
        setattr(settings, field, getattr(payload, field))
    await SettingsStore.save(db, settings, user_id=user.id)
    await db.commit()
    return {
        "preferredLanguage": settings.preferred_language,
        "sessionsPosition": settings.sessions_position,
        "sessionsPageSize": settings.sessions_page_size,
        "sessionTabsEnabled": settings.session_tabs_enabled,
        "sessionTabsPosition": settings.session_tabs_position,
        "sessionTabsOrder": settings.session_tabs_order,
        "sessionTabsLimit": settings.session_tabs_limit,
        "trashEnabled": settings.trash_enabled,
        "trashRetentionDays": settings.trash_retention_days,
    }


class SessionTabUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: UUID | None = Field(default=None, alias="sessionId")
    project_id: UUID | None = Field(default=None, alias="projectId")
    last_used_at: int = Field(alias="lastUsedAt", ge=0, le=9_007_199_254_740_991, strict=True)

    def identity(self) -> tuple[str, UUID | None]:
        return ("session", self.session_id) if self.session_id else ("draft", self.project_id)


class WorkspaceStateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_tabs: SessionTabs = Field(default_factory=list, alias="sessionTabs")
    session_tab_usage: list[SessionTabUsage] = Field(default_factory=list, alias="sessionTabUsage", max_length=100)
    control_center_open: StrictBool = Field(default=False, alias="controlCenterOpen")


async def _workspace_tabs(
    db: AsyncSession, user_id: UUID, tabs: SessionTabs,
) -> SessionTabs:
    session_ids = {tab.session_id for tab in tabs if tab.session_id is not None}
    project_ids = {tab.project_id for tab in tabs if tab.session_id is None and tab.project_id is not None}
    sessions = {item.id: item for item in (await db.scalars(select(ChatSession).where(
        ChatSession.id.in_(session_ids), ChatSession.user_id == user_id,
        ChatSession.parent_session_id.is_(None), ~deleted_session_clause(),
    ))).all()} if session_ids else {}
    projects = set((await db.scalars(select(Project.id).where(
        Project.id.in_(project_ids), Project.user_id == user_id,
        Project.archived_at.is_(None), Project.delete_at.is_(None),
    ))).all()) if project_ids else set()
    result = []
    for tab in tabs:
        if tab.session_id is not None:
            session = sessions.get(tab.session_id)
            if session is None:
                continue
            tab = tab.model_copy(update={"title": session.title, "project_id": session.project_id})
        elif tab.project_id is not None and tab.project_id not in projects:
            continue
        result.append(tab)
    return result


def _limit_workspace_tabs(tabs: SessionTabs) -> SessionTabs:
    if len(tabs) <= MAX_SESSION_TABS_LIMIT:
        return tabs
    recent = sorted(range(len(tabs)), key=lambda index: tabs[index].last_used_at, reverse=True)[:MAX_SESSION_TABS_LIMIT]
    return [tabs[index] for index in sorted(recent)]


@router.get("/me/workspace-state")
async def get_workspace_state(
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict:
    settings = await SettingsStore.load(db, UserPreferences, user_id=user.id)
    tabs = _limit_workspace_tabs(await _workspace_tabs(db, user.id, settings.session_tabs))
    return {
        "sessionTabs": [tab.model_dump(mode="json", by_alias=True) for tab in tabs],
        "controlCenterOpen": settings.control_center_open,
    }


@router.put("/me/workspace-state")
async def update_workspace_state(
    payload: WorkspaceStateRequest,
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict:
    settings = await SettingsStore.load(db, UserPreferences, user_id=user.id, for_update=True)
    usage = {}
    if "session_tabs" in payload.model_fields_set:
        # A list change may carry an older snapshot of another device's usage.
        for tab in settings.session_tabs:
            key = ("session", tab.session_id) if tab.session_id else ("draft", tab.project_id)
            usage[key] = tab.last_used_at
    for touch in payload.session_tab_usage:
        key = touch.identity()
        usage[key] = max(touch.last_used_at, usage.get(key, 0))
    replaces_tabs = "session_tabs" in payload.model_fields_set
    tabs = await _workspace_tabs(db, user.id, payload.session_tabs if replaces_tabs else settings.session_tabs)
    if replaces_tabs or payload.session_tab_usage:
        tabs = [
            tab.model_copy(update={"last_used_at": max(tab.last_used_at, usage.get(
                ("session", tab.session_id) if tab.session_id else ("draft", tab.project_id), 0,
            ))})
            for tab in tabs
        ]
    tabs = _limit_workspace_tabs(tabs)
    if replaces_tabs or payload.session_tab_usage:
        settings.session_tabs = tabs
    if "control_center_open" in payload.model_fields_set:
        settings.control_center_open = payload.control_center_open
    await SettingsStore.save(db, settings, user_id=user.id)
    await db.commit()
    return {
        "sessionTabs": [tab.model_dump(mode="json", by_alias=True) for tab in tabs],
        "controlCenterOpen": settings.control_center_open,
    }


def _payload(user: UserAccount) -> dict[str, str | bool]:
    return {
        "id": str(user.id),
        "username": user.username,
        "isAdmin": user.is_admin,
        "isActive": user.is_active,
        "hasLocalPassword": user.password_hash is not None,
        "createdAt": user.created_at.isoformat() if user.created_at else "",
    }


async def _lock_administration(db: AsyncSession) -> None:
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended('mato-loop-users', 0))")
    )


async def _user(db: AsyncSession, user_id: UUID, *, for_update: bool = False) -> UserAccount:
    statement = select(UserAccount).where(UserAccount.id == user_id)
    if for_update:
        statement = statement.with_for_update()
    user = await db.scalar(statement)
    if user is None:
        raise ApiError(404, "user.notFound")
    return user


async def _ensure_another_admin(db: AsyncSession, target: UserAccount) -> None:
    if not target.is_admin or not target.is_active:
        return
    count = await db.scalar(
        select(func.count(UserAccount.id)).where(
            UserAccount.id != target.id,
            UserAccount.is_admin.is_(True),
            UserAccount.is_active.is_(True),
        )
    )
    if not count:
        raise ApiError(409, "user.lastAdmin")


async def _quiesce_user_jobs(db: AsyncSession, user_id: UUID) -> None:
    session_ids = select(ChatSession.id).where(ChatSession.user_id == user_id)
    run_ids = list(
        (
            await db.scalars(
                select(AgentRun.id).where(
                    AgentRun.session_id.in_(session_ids),
                    AgentRun.parent_run_id.is_(None),
                    AgentRun.status.in_(_ACTIVE_RUN_STATUSES),
                )
            )
        ).all()
    )
    await db.execute(
        update(MemoryDreamRun)
        .where(
            MemoryDreamRun.session_id.in_(session_ids),
            MemoryDreamRun.status.in_(("queued", "running")),
        )
        .values(
            status="cancelled",
            error="auth.accountDisabled",
            completed_at=datetime.now(UTC),
            next_attempt_at=None,
        )
    )
    await db.commit()
    coding_executions = list(
        (
            await db.execute(
                select(ProjectPlanExecution.id, Project)
                .join(Project, Project.id == ProjectPlanExecution.project_id)
                .where(
                    Project.user_id == user_id,
                    ProjectPlanExecution.status.in_(
                        ("queued", "running", "waiting_permission")
                    ),
                )
                .order_by(ProjectPlanExecution.queue_position)
            )
        ).all()
    )
    coding_error: ProjectCodingError | None = None
    for execution_id, project in coding_executions:
        try:
            await ProjectCodingService.cancel(
                db, user_id, project, execution_id
            )
        except ProjectCodingError as exc:
            if exc.code not in {
                "project.executionNotCancellable",
                "project.executionNotFound",
            } and coding_error is None:
                coding_error = exc
    if coding_error is not None:
        status_code = 503 if coding_error.code.startswith("opencode.") else 409
        raise ApiError(
            status_code, coding_error.code, params=coding_error.params
        ) from coding_error
    for run_id in run_ids:
        await AgentRuntime.cancel_and_wait(run_id)
    await AgentRuntime.cancel_and_wait_user_title_tasks(user_id)
    await DreamingService.wait_for_user(user_id)
    await db.execute(
        update(AgentRun)
        .where(
            AgentRun.id.in_(run_ids),
            AgentRun.status.in_(_ACTIVE_RUN_STATUSES),
        )
        .values(
            status="cancelled",
            error="auth.accountDisabled",
            completed_at=datetime.now(UTC),
        )
    )
    await db.commit()


async def _shielded_quiesce(db: AsyncSession, user_id: UUID) -> None:
    task = asyncio.create_task(_quiesce_user_jobs(db, user_id))
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


@router.get("")
async def list_users(
    _admin: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, list[dict[str, str | bool]]]:
    users = list((await db.scalars(select(UserAccount).order_by(UserAccount.username))).all())
    return {"items": [_payload(item) for item in users]}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_user(
    payload: UserCreateRequest,
    _admin: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, str | bool]:
    username = payload.username.strip()
    if not username:
        raise ApiError(422, "validation.required", field="username")
    user = UserAccount(
        username=username,
        password_hash=hash_password(payload.password),
        is_admin=payload.is_admin,
        is_active=True,
    )
    db.add(user)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise ApiError(409, "user.usernameExists", field="username") from exc
    return _payload(user)


@router.patch("/{user_id}")
async def update_user(
    user_id: UUID,
    payload: UserUpdateRequest,
    admin: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, str | bool]:
    await _lock_administration(db)
    user = await _user(db, user_id, for_update=True)
    if user.id == admin.id and payload.is_active is False:
        raise ApiError(409, "user.cannotDeactivateSelf")
    if (
        payload.is_admin is False and user.is_admin
        or payload.is_active is False and user.is_active
    ):
        await _ensure_another_admin(db, user)
    if payload.is_admin is not None:
        user.is_admin = payload.is_admin
    if payload.is_active is not None:
        user.is_active = payload.is_active
        if not payload.is_active:
            await revoke_login_sessions(db, user.id)
    await db.commit()
    if payload.is_active is False:
        await _shielded_quiesce(db, user.id)
    return _payload(user)


@router.put("/me/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    payload: PasswordChangeRequest,
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> None:
    current = await _user(db, user.id, for_update=True)
    if current.password_hash is None or not verify_password(
        payload.current_password, current.password_hash
    ):
        raise ApiError(422, "auth.currentPasswordInvalid", field="currentPassword")
    if payload.current_password == payload.new_password:
        raise ApiError(422, "auth.passwordUnchanged", field="newPassword")
    current.password_hash = hash_password(payload.new_password)
    await revoke_login_sessions(db, current.id)
    await db.commit()


@router.put("/{user_id}/password", status_code=status.HTTP_204_NO_CONTENT)
async def reset_password(
    user_id: UUID,
    payload: PasswordResetRequest,
    _admin: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> None:
    user = await _user(db, user_id, for_update=True)
    user.password_hash = hash_password(payload.password)
    await revoke_login_sessions(db, user.id)
    await db.commit()


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: UUID,
    admin: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> None:
    if user_id == admin.id:
        raise ApiError(409, "user.cannotDeleteSelf")
    await _lock_administration(db)
    user = await _user(db, user_id, for_update=True)
    if user.is_active:
        raise ApiError(409, "user.deactivateBeforeDelete")
    await _ensure_another_admin(db, user)
    await _shielded_quiesce(db, user.id)
    await _lock_administration(db)
    user = await _user(db, user_id, for_update=True)
    if user.is_active:
        raise ApiError(409, "user.deactivateBeforeDelete")
    sessions = list(
        (
            await db.execute(
                select(ChatSession.id, ChatSession.project_id).where(
                    ChatSession.user_id == user.id
                )
            )
        ).all()
    )
    project_ids = list(
        (await db.scalars(select(Project.id).where(Project.user_id == user.id))).all()
    )
    storage = SessionStorage()
    standalone_sessions = [session_id for session_id, project_id in sessions if project_id is None]
    trashed_sessions: list[tuple[UUID, Path | None]] = []
    trashed_projects: list[tuple[UUID, Path | None]] = []

    async def restore_storage() -> None:
        for project_id, path in reversed(trashed_projects):
            if path is None:
                continue
            try:
                await asyncio.to_thread(storage.restore_project, project_id, path)
            except Exception:
                logger.exception("User project storage could not be restored")
        for session_id, path in reversed(trashed_sessions):
            if path is not None:
                try:
                    await asyncio.to_thread(storage.restore_session, session_id, path)
                except Exception:
                    logger.exception("User session storage could not be restored")

    async def purge_storage() -> None:
        await asyncio.to_thread(storage.forget_sessions, [session_id for session_id, _ in sessions])
        for _session_id, path in trashed_sessions:
            try:
                await asyncio.to_thread(storage.purge_trashed, path)
            except Exception:
                logger.exception("Deleted user session storage could not be purged")
        for _project_id, path in trashed_projects:
            try:
                await asyncio.to_thread(storage.purge_trashed, path)
            except Exception:
                logger.exception("Deleted user project storage could not be purged")

    commit_task: asyncio.Task[None] | None = None
    try:
        for project_id in project_ids:
            trash_task = asyncio.create_task(
                asyncio.to_thread(storage.trash_project, project_id)
            )
            try:
                trashed = await asyncio.shield(trash_task)
            except asyncio.CancelledError:
                trashed_projects.append((project_id, await trash_task))
                raise
            trashed_projects.append((project_id, trashed))
        for session_id in standalone_sessions:
            trash_task = asyncio.create_task(
                asyncio.to_thread(storage.trash_session, session_id, None)
            )
            try:
                trashed = await asyncio.shield(trash_task)
            except asyncio.CancelledError:
                trashed_sessions.append((session_id, await trash_task))
                raise
            trashed_sessions.append((session_id, trashed))
        await db.execute(delete(Automation).where(Automation.user_id == user.id))
        await db.delete(user)
        commit_task = asyncio.create_task(db.commit())
        await asyncio.shield(commit_task)
    except asyncio.CancelledError:
        if commit_task is None:
            await db.rollback()
            await restore_storage()
            raise
        try:
            await commit_task
        except BaseException:
            await db.rollback()
            await restore_storage()
        else:
            session_secrets.clear_sessions([session_id for session_id, _ in sessions])
            await purge_storage()
        raise
    except BaseException:
        await db.rollback()
        await restore_storage()
        raise
    session_secrets.clear_sessions([session_id for session_id, _ in sessions])
    await purge_storage()
