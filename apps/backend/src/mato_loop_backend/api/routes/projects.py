from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend.agent.runtime import AgentRuntime
from mato_loop_backend.agent.session_secrets import session_secrets
from mato_loop_backend.api.errors import ApiError
from mato_loop_backend.automations import ACTIVE_STATUSES, automation_impact_token
from mato_loop_backend.db.session import get_db_session
from mato_loop_backend.files.storage import SessionStorage, SessionStorageError
from mato_loop_backend.models import (
    AgentRun,
    Automation,
    ChatSession,
    Message,
    MessageAttachment,
    Project,
    ProjectPlanExecution,
    UserAccount,
)
from mato_loop_backend.security import require_user
from mato_loop_backend.sessions.archive import ArchiveRequest, set_archived
from mato_loop_backend.sessions.cleanup import descendant_sessions
from mato_loop_backend.sessions.coding import (
    remove_session_worktree,
    validate_git_branch,
    validate_git_remote,
)
from mato_loop_backend.sessions.service import (
    SessionServiceError,
    unread_response_clause,
    validate_execution_preferences,
)
from mato_loop_backend.sessions.state import deleted_session_clause
from mato_loop_backend.sessions.templates import load_template
from mato_loop_backend.sessions.trash import move_to_trash
from mato_loop_backend.settings import SettingsStore, UserPreferences

router = APIRouter(prefix="/projects", dependencies=[Depends(require_user)])
logger = logging.getLogger(__name__)
ProjectIconColor = Literal["blue", "teal", "green", "lime", "ochre", "orange", "red", "pink", "violet", "slate"]


class ProjectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str = Field(min_length=1, max_length=240)
    icon_initials: str | None = Field(default=None, alias="iconInitials", pattern=r"^[\p{L}\p{N}]{1,2}$")
    icon_color: ProjectIconColor | None = Field(default=None, alias="iconColor")
    instructions: str = Field(default="", max_length=4000)
    default_session_template_id: UUID | None = Field(
        default=None, strict=False,
        validation_alias=AliasChoices("defaultSessionTemplateId", "default_session_template_id"),
    )
    default_execution_agent_id: UUID | None = Field(
        default=None,
        strict=False,
        validation_alias=AliasChoices("defaultExecutionAgentId", "default_execution_agent_id"),
    )
    default_current_directory: str | None = Field(
        default=None,
        max_length=4096,
        validation_alias=AliasChoices("defaultCurrentDirectory", "default_current_directory"),
    )
    git_remote: str | None = Field(default=None, validation_alias=AliasChoices("gitRemote", "git_remote"))
    git_target_branch: str | None = Field(default=None, validation_alias=AliasChoices("gitTargetBranch", "git_target_branch"))
    git_workspace_mode: Literal["local", "worktree"] = Field(
        default="local", validation_alias=AliasChoices("gitWorkspaceMode", "git_workspace_mode")
    )
    git_fetch_before_actions: bool = Field(
        default=True,
        validation_alias=AliasChoices("gitFetchBeforeActions", "git_fetch_before_actions"),
    )

    _remote = field_validator("git_remote")(validate_git_remote)
    _branch = field_validator("git_target_branch")(validate_git_branch)

    @field_validator("icon_initials", mode="before")
    @classmethod
    def normalize_icon_initials(cls, value: object) -> object:
        return (value.strip() or None) if isinstance(value, str) else value

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("instructions", mode="before")
    @classmethod
    def normalize_instructions(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class ProjectCreateRequest(ProjectRequest):
    pass


class ProjectResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    name: str
    icon_initials: str | None = Field(default=None, alias="iconInitials")
    icon_color: str | None = Field(default=None, alias="iconColor")
    archived_at: datetime | None = Field(default=None, alias="archivedAt")
    instructions: str
    default_session_template_id: UUID | None = Field(default=None, alias="defaultSessionTemplateId")
    default_execution_agent_id: UUID | None = Field(alias="defaultExecutionAgentId")
    default_current_directory: str | None = Field(alias="defaultCurrentDirectory")
    git_remote: str | None = Field(alias="gitRemote")
    git_target_branch: str | None = Field(alias="gitTargetBranch")
    git_workspace_mode: Literal["local", "worktree"] = Field(alias="gitWorkspaceMode")
    git_fetch_before_actions: bool = Field(default=True, alias="gitFetchBeforeActions")
    session_count: int = Field(alias="sessionCount")
    has_unread_messages: bool = Field(default=False, alias="hasUnreadMessages")
    created_at: datetime = Field(alias="createdAt")
    updated_at: datetime = Field(alias="updatedAt")
    coding_service: dict | None = Field(alias="codingService")


class ProjectListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ProjectResponse]


class ProjectStatusResponse(BaseModel):
    id: UUID
    session_count: int = Field(alias="sessionCount")
    has_unread_messages: bool = Field(alias="hasUnreadMessages")


class ProjectStatusListResponse(BaseModel):
    items: list[ProjectStatusResponse]


class ProjectOrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_ids: list[UUID] = Field(alias="projectIds", min_length=1, max_length=10000)

    @field_validator("project_ids")
    @classmethod
    def unique_projects(cls, value: list[UUID]) -> list[UUID]:
        if len(value) != len(set(value)):
            raise ValueError("project IDs must be unique")
        return value


def _payload(project: Project, session_count: int, has_unread_messages: bool = False) -> ProjectResponse:
    return ProjectResponse(
        id=project.id,
        name=project.name,
        iconInitials=project.icon_initials,
        iconColor=project.icon_color,
        archivedAt=project.archived_at,
        instructions=project.instructions,
        defaultSessionTemplateId=project.default_session_template_id,
        defaultExecutionAgentId=project.default_execution_agent_id,
        defaultCurrentDirectory=project.default_current_directory,
        gitRemote=project.git_remote,
        gitTargetBranch=project.git_target_branch,
        gitWorkspaceMode=project.git_workspace_mode or "local",
        gitFetchBeforeActions=(
            project.git_fetch_before_actions
            if project.git_fetch_before_actions is not None
            else True
        ),
        sessionCount=session_count,
        hasUnreadMessages=has_unread_messages,
        createdAt=project.created_at,
        updatedAt=project.updated_at,
        codingService=(
            {
                "adapterType": "opencode",
                "integrationId": str(project.coding_integration_id),
                "projectKey": project.coding_project_key,
                "workspaceKey": project.coding_workspace_key,
                "analysisModelKey": project.coding_analysis_model_key,
                "analysisModelVariant": project.coding_analysis_model_variant,
                "executionModelKey": project.coding_execution_model_key,
                "executionModelVariant": project.coding_execution_model_variant,
                "queuePauseReason": project.coding_queue_pause_reason,
            }
            if project.coding_integration_id is not None
            else None
        ),
    )


async def _owned_project(
    db: AsyncSession, project_id: UUID, user_id: UUID, *,
    for_update: bool = False, include_deleted: bool = False,
) -> Project:
    statement = select(Project).where(Project.id == project_id, Project.user_id == user_id)
    if not include_deleted:
        statement = statement.where(Project.delete_at.is_(None))
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    project = await db.scalar(statement)
    if project is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "project.notFound")
    return project


async def _session_count(db: AsyncSession, project: Project) -> int:
    count = await db.scalar(
        select(func.count(ChatSession.id)).where(
            ChatSession.project_id == project.id,
            ChatSession.user_id == project.user_id,
            ChatSession.parent_session_id.is_(None),
            ~deleted_session_clause(),
        )
    )
    return count or 0


def _project_session_ids(project_id: UUID, user_id: UUID):
    return select(ChatSession.id).where(
        ChatSession.project_id == project_id,
        ChatSession.user_id == user_id,
        ChatSession.parent_session_id.is_(None),
    )


async def _external_file_reference_count(
    db: AsyncSession, session_ids: list[UUID]
) -> int:
    if not session_ids:
        return 0
    return int(
        await db.scalar(
            select(func.count())
            .select_from(MessageAttachment)
            .join(Message, Message.id == MessageAttachment.message_id)
            .where(
                MessageAttachment.file_root_id.in_(session_ids),
                Message.session_id.not_in(session_ids),
            )
        )
        or 0
    )


async def _linked_automations(
    db: AsyncSession,
    project_id: UUID,
    user_id: UUID,
    *,
    for_update: bool = False,
) -> list[Automation]:
    statement = select(Automation).where(
        Automation.user_id == user_id,
        Automation.execution_session_id.in_(_project_session_ids(project_id, user_id)),
    )
    if for_update:
        statement = statement.execution_options(populate_existing=True).with_for_update()
    return list((await db.scalars(statement)).all())


def project_delete_impact_token(
    session_ids: list[UUID], automations: list[Automation]
) -> str:
    value = "|".join(str(item) for item in sorted(session_ids))
    return hashlib.sha256(
        f"sessions:{value}\nautomations:{automation_impact_token(automations)}".encode()
    ).hexdigest()


def _validate_automation_policy(
    automations: list[Automation],
    automation_mode: Literal["unlink", "deleteAutomations"] | None,
) -> None:
    required_count = len(automations)
    if required_count and automation_mode != "deleteAutomations":
        raise ApiError(
            409,
            "session.automationOutputRequired",
            params={
                "automationCount": required_count,
                "totalAutomationCount": len(automations),
            },
        )


@router.get("", response_model=ProjectListResponse | ProjectStatusListResponse)
async def list_projects(
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    archived: bool = False,
    project_ids: Annotated[list[UUID] | None, Query(alias="projectIds", max_length=100)] = None,
    view: Literal["full", "status"] = "full",
) -> ProjectListResponse | ProjectStatusListResponse:
    filters = (
        Project.user_id == user.id,
        Project.delete_at.is_(None),
        Project.archived_at.is_not(None) if archived else Project.archived_at.is_(None),
        Project.id.in_(project_ids) if project_ids is not None else True,
    )
    order = (Project.updated_at.desc(), Project.id.desc())
    if view == "status":
        # The owning project is already known to be live. Count only its own
        # live root sessions, without walking an unrelated deletion tree.
        session_count = select(func.count()).select_from(ChatSession).where(
            ChatSession.project_id == Project.id, ChatSession.user_id == Project.user_id,
            ChatSession.parent_session_id.is_(None), ChatSession.delete_at.is_(None),
        ).correlate(Project).scalar_subquery()
        rows = (await db.execute(
            select(Project.id, session_count, _unread_project_sessions(live_project=True))
            .where(*filters).order_by(*order)
        )).all()
        return ProjectStatusListResponse(items=[
            ProjectStatusResponse(id=project_id, sessionCount=count, hasUnreadMessages=unread)
            for project_id, count, unread in rows
        ])

    rows = (
        await db.execute(
            select(Project, func.count(ChatSession.id), _unread_project_sessions())
            .outerjoin(
                ChatSession,
                (ChatSession.project_id == Project.id)
                & (ChatSession.user_id == Project.user_id)
                & ChatSession.parent_session_id.is_(None)
                & ~deleted_session_clause(),
            )
            .where(*filters)
            .group_by(Project.id)
            .order_by(*order)
        )
    ).all()
    if not archived:
        preferences = await SettingsStore.load(db, UserPreferences, user_id=user.id)
        positions = {project_id: index for index, project_id in enumerate(preferences.project_order)}
        rows = sorted(rows, key=lambda row: positions.get(row[0].id, len(positions)))
    return ProjectListResponse(
        items=[_payload(project, session_count, unread) for project, session_count, unread in rows]
    )


@router.put("/order", status_code=status.HTTP_204_NO_CONTENT)
async def reorder_projects(
    payload: ProjectOrderRequest,
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> None:
    owned_ids = set(await db.scalars(select(Project.id).where(
        Project.user_id == user.id,
        Project.delete_at.is_(None),
    )))
    requested_ids = set(payload.project_ids)
    if not requested_ids <= owned_ids:
        raise ApiError(status.HTTP_404_NOT_FOUND, "project.notFound")
    preferences = await SettingsStore.load(db, UserPreferences, user_id=user.id, for_update=True)
    preferences.project_order = payload.project_ids + [
        project_id for project_id in preferences.project_order
        if project_id in owned_ids and project_id not in requested_ids
    ]
    await SettingsStore.save(db, preferences, user_id=user.id)
    await db.commit()


def _unread_project_sessions(*, live_project: bool = False):
    return select(ChatSession.id).join(Message, Message.session_id == ChatSession.id).where(
        ChatSession.project_id == Project.id, ChatSession.user_id == Project.user_id,
        ChatSession.parent_session_id.is_(None), ChatSession.archived_at.is_(None),
        ChatSession.delete_at.is_(None) if live_project else ~deleted_session_clause(),
        unread_response_clause(),
        or_(ChatSession.last_read_at.is_(None), Message.created_at > ChatSession.last_read_at),
    ).correlate(Project).exists()


@router.post("/{project_id}/archive", response_model=ProjectResponse)
async def archive_project(
    project_id: UUID, payload: ArchiveRequest,
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> ProjectResponse:
    project = await _owned_project(db, project_id, user.id, for_update=True)
    try:
        await set_archived(db, project, payload.archived)
    except SessionServiceError as exc:
        raise ApiError(exc.status_code, exc.code) from exc
    await db.commit()
    return _payload(project, await _session_count(db, project))


@router.post("", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
async def create_project(
    payload: ProjectCreateRequest,
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> ProjectResponse:
    try:
        if payload.default_session_template_id is not None:
            await load_template(db, user.id, payload.default_session_template_id)
        current_directory, _ = await validate_execution_preferences(
            db,
            user.id,
            payload.default_execution_agent_id,
            payload.default_current_directory,
        )
    except SessionServiceError as exc:
        raise ApiError(exc.status_code, exc.code, field=exc.field, params=exc.params) from exc
    project = Project(
        user_id=user.id,
        name=payload.name,
        icon_initials=payload.icon_initials,
        icon_color=payload.icon_color,
        instructions=payload.instructions,
        default_session_template_id=payload.default_session_template_id,
        default_execution_agent_id=payload.default_execution_agent_id,
        default_current_directory=current_directory,
        git_remote=payload.git_remote,
        git_target_branch=payload.git_target_branch,
        git_workspace_mode=payload.git_workspace_mode,
        git_fetch_before_actions=payload.git_fetch_before_actions,
    )
    db.add(project)
    await db.commit()
    return _payload(project, 0)


@router.patch("/{project_id}", response_model=ProjectResponse)
async def update_project(
    project_id: UUID,
    payload: ProjectRequest,
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> ProjectResponse:
    project = await _owned_project(db, project_id, user.id)
    session_count = await _session_count(db, project)
    if "default_session_template_id" in payload.model_fields_set:
        if payload.default_session_template_id is not None and payload.default_session_template_id != project.default_session_template_id:
            try:
                await load_template(db, user.id, payload.default_session_template_id)
            except SessionServiceError as exc:
                raise ApiError(exc.status_code, exc.code, field="defaultSessionTemplateId") from exc
        project.default_session_template_id = payload.default_session_template_id
    project.name = payload.name
    if "icon_initials" in payload.model_fields_set:
        project.icon_initials = payload.icon_initials
    if "icon_color" in payload.model_fields_set:
        project.icon_color = payload.icon_color
    if "instructions" in payload.model_fields_set:
        project.instructions = payload.instructions
    if "git_remote" in payload.model_fields_set:
        project.git_remote = payload.git_remote
    if "git_target_branch" in payload.model_fields_set:
        project.git_target_branch = payload.git_target_branch
    if "git_workspace_mode" in payload.model_fields_set:
        project.git_workspace_mode = payload.git_workspace_mode
    if "git_fetch_before_actions" in payload.model_fields_set:
        project.git_fetch_before_actions = payload.git_fetch_before_actions
    execution_fields = {
        "default_execution_agent_id",
        "default_current_directory",
    }
    if payload.model_fields_set & execution_fields:
        agent_id = (
            payload.default_execution_agent_id
            if "default_execution_agent_id" in payload.model_fields_set
            else project.default_execution_agent_id
        )
        current_directory = (
            payload.default_current_directory
            if "default_current_directory" in payload.model_fields_set
            else (
                None
                if agent_id != project.default_execution_agent_id
                else project.default_current_directory
            )
        )
        if (
            agent_id != project.default_execution_agent_id
            or current_directory != project.default_current_directory
        ):
            try:
                current_directory, _ = await validate_execution_preferences(
                    db, user.id, agent_id, current_directory
                )
            except SessionServiceError as exc:
                raise ApiError(
                    exc.status_code, exc.code, field=exc.field, params=exc.params
                ) from exc
            project.default_execution_agent_id = agent_id
            project.default_current_directory = current_directory
    project.updated_at = datetime.now(UTC)
    await db.commit()
    unread = await db.scalar(select(_unread_project_sessions()).select_from(Project).where(Project.id == project.id))
    return _payload(project, session_count, bool(unread))


@router.get("/{project_id}/delete-impact")
async def project_delete_impact(
    project_id: UUID,
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, int | str | list[str]]:
    project = await _owned_project(db, project_id, user.id, include_deleted=True)
    project_db_id = project.id
    owner_id = project.user_id
    if await db.scalar(
        select(ProjectPlanExecution.id)
        .where(
            ProjectPlanExecution.project_id == project_db_id,
            ProjectPlanExecution.status.in_(("queued", "running", "waiting_permission")),
        )
        .limit(1)
    ) is not None:
        raise ApiError(409, "project.codingExecutionsActive")
    session_ids = list(
        (await db.scalars(_project_session_ids(project_db_id, owner_id))).all()
    )
    storage_session_ids = list(
        (
            await db.scalars(
                select(ChatSession.id).where(
                    ChatSession.project_id == project_db_id,
                    ChatSession.user_id == owner_id,
                )
            )
        ).all()
    )
    linked = await _linked_automations(db, project_db_id, owner_id)
    sorted_session_ids = sorted(session_ids)
    return {
        "sessionCount": len(sorted_session_ids),
        "sessionIds": [str(item) for item in sorted_session_ids],
        "automationCount": len(linked),
        "requiredAutomationCount": len(linked),
        "fileReferenceCount": await _external_file_reference_count(
            db, storage_session_ids
        ),
        "automationImpact": project_delete_impact_token(session_ids, linked),
    }


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
    project_id: UUID,
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    automation_impact: Annotated[
        str | None,
        Query(alias="automationImpact", min_length=64, max_length=64),
    ] = None,
    automation_mode: Annotated[
        Literal["unlink", "deleteAutomations"] | None,
        Query(alias="automationMode"),
    ] = None,
    permanent: bool | None = None,
) -> None:
    project = await _owned_project(db, project_id, user.id, include_deleted=True)
    if not permanent:
        preferences = await SettingsStore.load(db, UserPreferences, user_id=user.id)
        if permanent is False or preferences.trash_enabled:
            try:
                await move_to_trash(db, project)
            except SessionServiceError as exc:
                raise ApiError(exc.status_code, exc.code, field=exc.field, params=exc.params) from exc
            return
        if project.delete_at is not None:
            raise ApiError(409, "project.inTrash")
    await purge_project(
        project_id, user.id, db,
        automation_impact=automation_impact, automation_mode=automation_mode,
        allow_deleted=bool(permanent),
    )


async def purge_project(
    project_id: UUID,
    user_id: UUID,
    db: AsyncSession,
    *,
    automation_impact: str | None,
    automation_mode: Literal["unlink", "deleteAutomations"] | None = None,
    expired_only: bool = False,
    allow_deleted: bool = True,
) -> None:
    project = await _owned_project(db, project_id, user_id, include_deleted=True)
    if not allow_deleted and project.delete_at is not None:
        raise ApiError(409, "project.inTrash")
    if expired_only and (project.delete_at is None or project.delete_at > datetime.now(UTC)):
        return
    project_db_id = project.id
    owner_id = project.user_id
    session_ids = list(
        (await db.scalars(_project_session_ids(project_db_id, owner_id))).all()
    )
    linked_automations = await _linked_automations(db, project_db_id, owner_id)
    if automation_impact != project_delete_impact_token(
        session_ids, linked_automations
    ):
        raise ApiError(409, "session.automationImpactChanged")
    _validate_automation_policy(linked_automations, automation_mode)

    async def active_run_ids(current_session_ids: list[UUID]) -> list[UUID]:
        return list(
            (
                await db.scalars(
                    select(AgentRun.id).where(
                        AgentRun.session_id.in_(current_session_ids),
                        AgentRun.parent_run_id.is_(None),
                        AgentRun.status.in_(ACTIVE_STATUSES),
                    )
                )
            ).all()
        )

    async def cancel_runs(run_ids: list[UUID]) -> None:
        not_local = []
        for run_id in run_ids:
            if not await AgentRuntime.cancel_and_wait(run_id):
                not_local.append(run_id)
        if not_local and await db.scalar(
            select(AgentRun.id).where(
                AgentRun.id.in_(not_local),
                AgentRun.status.in_(ACTIVE_STATUSES),
            )
        ):
            raise ApiError(409, "session.runAlreadyActive")

    cancellation_roots = [*session_ids]
    if run_ids := await active_run_ids(session_ids):
        await db.rollback()
        await cancel_runs(run_ids)
    await db.rollback()
    for session_id in cancellation_roots:
        await AgentRuntime.cancel_descendants(session_id)

    for attempt in range(2):
        await db.execute(select(UserAccount.id).where(UserAccount.id == owner_id).with_for_update(key_share=True))
        project = await _owned_project(
            db, project_db_id, owner_id, for_update=True, include_deleted=True
        )
        if not allow_deleted and project.delete_at is not None:
            raise ApiError(409, "project.inTrash")
        if expired_only and (project.delete_at is None or project.delete_at > datetime.now(UTC)):
            return
        sessions = list(
            (
                await db.scalars(
                    select(ChatSession)
                    .where(
                        ChatSession.project_id == project_db_id,
                        ChatSession.user_id == owner_id,
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).all()
        )
        from mato_loop_backend.voice.admission import active_connection

        for session in sessions:
            if await active_connection(db, session.id):
                raise ApiError(409, "voice.busy")
        linked_automations = await _linked_automations(
            db, project_db_id, owner_id, for_update=True
        )
        session_ids = [item.id for item in sessions if item.parent_session_id is None]
        if automation_impact != project_delete_impact_token(
            session_ids, linked_automations
        ):
            raise ApiError(409, "session.automationImpactChanged")
        _validate_automation_policy(linked_automations, automation_mode)
        deletion_roots = [item.id for item in sessions]
        descendants = await descendant_sessions(db, deletion_roots)
        storage_session_ids = [
            *deletion_roots,
            *(child_id for child_id, _ in descendants),
        ]
        await db.execute(
            select(ChatSession.id)
            .where(ChatSession.id.in_(storage_session_ids))
            .with_for_update()
        )
        reference_count = await _external_file_reference_count(db, storage_session_ids)
        if reference_count:
            raise ApiError(
                409,
                "session.filesReferenced",
                params={"referenceCount": reference_count},
            )
        late_run_ids = await active_run_ids(storage_session_ids)
        coding_execution_id = await db.scalar(
            select(ProjectPlanExecution.id)
            .where(
                ProjectPlanExecution.project_id == project_db_id,
                ProjectPlanExecution.status.in_(
                    ("queued", "running", "waiting_permission")
                ),
            )
            .limit(1)
        )
        if coding_execution_id is not None:
            raise ApiError(409, "project.codingExecutionsActive")
        if not late_run_ids:
            break
        await db.rollback()
        await cancel_runs(late_run_ids)
        if attempt == 1:
            raise ApiError(409, "session.runAlreadyActive")

    extra_storage = dict(
        [(child_id, child_project) for child_id, child_project in descendants
           if child_project != project_db_id]
    )

    storage = SessionStorage()
    project_trashed: Path | None = None
    execution_trashed: list[tuple[UUID, UUID | None, Path | None]] = []

    async def trash_storage() -> None:
        nonlocal project_trashed
        project_trashed = await asyncio.to_thread(
            storage.trash_project, project_db_id
        )
        for execution_session_id, storage_project_id in extra_storage.items():
            trashed = await asyncio.to_thread(
                storage.trash_session, execution_session_id, storage_project_id
            )
            execution_trashed.append((execution_session_id, storage_project_id, trashed))

    async def restore_storage() -> None:
        for execution_session_id, storage_project_id, trashed in reversed(execution_trashed):
            if trashed is None:
                continue
            try:
                await asyncio.to_thread(
                    storage.restore_session, execution_session_id, trashed, storage_project_id
                )
            except Exception:
                logger.exception("Automation session storage could not be restored")
        if project_trashed is not None:
            try:
                await asyncio.to_thread(
                    storage.restore_project, project_db_id, project_trashed
                )
            except Exception:
                logger.exception("Project storage could not be restored")

    async def restore_and_rollback() -> None:
        try:
            await restore_storage()
        finally:
            await db.rollback()

    async def purge_storage() -> None:
        await asyncio.to_thread(storage.forget_sessions, storage_session_ids)
        for _execution_session_id, _storage_project_id, trashed in execution_trashed:
            try:
                await asyncio.to_thread(storage.purge_trashed, trashed)
            except Exception:
                logger.exception("Deleted automation session storage could not be purged")
        try:
            await asyncio.to_thread(storage.purge_trashed, project_trashed)
        except Exception:
            logger.exception("Deleted project storage could not be purged")

    trash_task = asyncio.create_task(trash_storage())
    try:
        await asyncio.shield(trash_task)
    except asyncio.CancelledError:
        try:
            await trash_task
        except BaseException:
            pass
        await restore_and_rollback()
        raise
    except (OSError, SessionStorageError) as exc:
        await restore_and_rollback()
        logger.exception("Project storage could not be moved to trash")
        raise ApiError(500, "session.storageUnavailable") from exc
    except BaseException:
        await restore_and_rollback()
        raise

    commit_task: asyncio.Task[None] | None = None
    try:
        for session in sessions:
            if session.trash_worktree_removal:
                try:
                    await remove_session_worktree(db, session, scheduled=session.trash_worktree_removal)
                except SessionServiceError as exc:
                    raise ApiError(exc.status_code, exc.code, field=exc.field, params=exc.params) from exc
        if automation_mode == "deleteAutomations":
            for automation in linked_automations:
                await db.delete(automation)
            await db.flush()
        if deletion_roots:
            await db.execute(
                delete(ChatSession)
                .where(ChatSession.id.in_(deletion_roots), ChatSession.user_id == owner_id)
                .execution_options(synchronize_session=False)
            )
        await db.flush()
        await db.delete(project)
        commit_task = asyncio.create_task(db.commit())
        await asyncio.shield(commit_task)
    except asyncio.CancelledError:
        if commit_task is None:
            await restore_and_rollback()
            raise
        try:
            await commit_task
        except BaseException:
            await restore_and_rollback()
        else:
            session_secrets.clear_sessions(storage_session_ids)
            await purge_storage()
        raise
    except BaseException:
        await restore_and_rollback()
        raise
    session_secrets.clear_sessions(storage_session_ids)
    await purge_storage()
