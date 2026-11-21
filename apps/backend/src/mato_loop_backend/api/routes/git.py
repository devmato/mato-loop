from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import AliasChoices, BaseModel, Field, StrictBool, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend.api.errors import ApiError
from mato_loop_backend.db.session import get_db_session
from mato_loop_backend.execution.client import ExecutionClientError, ExecutionResult
from mato_loop_backend.models import Project, UserAccount
from mato_loop_backend.security import require_user
from mato_loop_backend.sessions.coding import (
    RepositoryResult,
    coding_client,
    grant_session_workspace,
    inspect_repository,
    persist_worktree_warning,
    remove_worktree,
    resolve_git_target,
    select_session_workspace,
    validate_git_branch,
    validate_git_remote,
)
from mato_loop_backend.sessions.service import (
    SessionServiceError,
    get_owned_session,
)

router = APIRouter(prefix="/coding", dependencies=[Depends(require_user)])


class RepositoryRequest(BaseModel):
    execution_agent_id: UUID
    current_directory: str = Field(min_length=1, max_length=4096)
    target_branch: str | None = Field(default=None, max_length=300)
    worktree_details: bool = False

    _target = field_validator("target_branch")(validate_git_branch)


@router.post("/repository", response_model=RepositoryResult)
async def repository(
    payload: RepositoryRequest,
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> RepositoryResult:
    try:
        return await inspect_repository(db, user.id, payload.execution_agent_id, payload.current_directory,
                                        payload.target_branch, worktree_details=payload.worktree_details)
    except SessionServiceError as exc:
        raise ApiError(exc.status_code, exc.code, field=exc.field, params=exc.params) from exc


class GitTargetRequest(BaseModel):
    remote: str | None = Field(default=None, min_length=1, max_length=200)
    remote_identity: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    target_branch: str | None = Field(default=None, min_length=1, max_length=300)
    fetch_before_actions: StrictBool | None = Field(
        default=None, validation_alias=AliasChoices("fetchBeforeActions", "fetch_before_actions")
    )

    _remote = field_validator("remote")(validate_git_remote)
    _target = field_validator("target_branch")(validate_git_branch)


async def git_session(db, user, session_id):
    from mato_loop_backend.sessions.archive import is_session_archived

    await db.execute(select(UserAccount.id).where(UserAccount.id == user.id).with_for_update(key_share=True))
    session = await get_owned_session(db, session_id, user.id, for_update=True)
    if session is None:
        raise SessionServiceError(404, "session.notFound")
    if await is_session_archived(db, session):
        raise SessionServiceError(409, "session.archived")
    return session


@router.put("/sessions/{session_id}/target")
async def set_target(
    session_id: UUID,
    payload: GitTargetRequest,
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict:
    try:
        session = await git_session(db, user, session_id)
        info = await inspect_repository(db, user.id, session.preferred_execution_agent_id, session.current_directory)
        if info.repository is None:
            raise SessionServiceError(409, "git.notRepository")
        if payload.remote is not None and payload.remote not in info.remotes:
            raise SessionServiceError(409, "git.remoteRequired")
        if payload.remote is not None and info.remote_identities.get(payload.remote) != payload.remote_identity:
            raise SessionServiceError(409, "git.worktreeChanged")
        binding = dict(resolve_git_target(session, None, info)[0])
        if binding.get("commonDirectory") and binding["commonDirectory"] != info.common_directory:
            raise SessionServiceError(409, "git.worktreeChanged")
        project = await db.scalar(select(Project).where(Project.id == session.project_id, Project.user_id == user.id)) if session.project_id else None
        configured = (project is not None and project.default_execution_agent_id == session.preferred_execution_agent_id
                      and (project.default_current_directory == info.repository
                           or any(item.path == project.default_current_directory for item in info.worktrees)))
        if configured and payload.remote is not None:
            project.git_remote = payload.remote
        if not binding:
            binding = {"path": info.repository, "repository": info.repository,
                       "executionAgentId": str(session.preferred_execution_agent_id)}
        if payload.remote is not None:
            binding.update(remote=payload.remote, remoteIdentity=info.remote_identities[payload.remote])
        if "fetch_before_actions" in payload.model_fields_set:
            if payload.fetch_before_actions is None:
                binding.pop("fetchBeforeActions", None)
            else:
                binding["fetchBeforeActions"] = payload.fetch_before_actions
        binding.update(targetBranch=payload.target_branch or binding.get("targetBranch") or (project.git_target_branch if configured else None) or info.default_branch,
                       commonDirectory=info.common_directory)
        session.coding_workspace = binding
        await db.commit()
        return {"codingWorkspace": binding, "projectId": str(project.id) if configured else None}
    except SessionServiceError as exc:
        raise ApiError(exc.status_code, exc.code, field=exc.field, params=exc.params) from exc


class GitActionRequest(BaseModel):
    action: Literal["remove", "branch-create", "branch-switch", "branch-rename", "branch-delete"]
    directory: str | None = Field(default=None, max_length=4096)
    name: str | None = Field(default=None, min_length=1, max_length=300)
    target_branch: str | None = Field(default=None, min_length=1, max_length=300)
    force: bool = False

    _name = field_validator("name", "target_branch")(validate_git_branch)


async def workspace_session(db, user, session_id):
    session = await git_session(db, user, session_id)
    project = await db.scalar(select(Project).where(Project.id == session.project_id, Project.user_id == user.id)) if session.project_id else None
    if project is None:
        raise SessionServiceError(409, "git.projectWorkspaceRequired")
    if project.archived_at is not None:
        raise SessionServiceError(409, "project.archived")
    return session, project


@router.post("/sessions/{session_id}/git")
async def git_action(session_id: UUID, payload: GitActionRequest,
    user: Annotated[UserAccount, Depends(require_user)], db: Annotated[AsyncSession, Depends(get_db_session)]):
    try:
        session, _ = await workspace_session(db, user, session_id)
        if payload.action != "remove" and payload.directory != session.current_directory:
            raise SessionServiceError(409, "git.worktreeChanged")
        info = await inspect_repository(db, user.id, session.preferred_execution_agent_id, session.current_directory)
        if payload.action == "remove":
            await remove_worktree(db, session, info, payload.directory, force=payload.force)
        else:
            root = next((item.path for item in info.worktrees if item.main), None)
            directory = info.repository
            if not root or not directory or not any(item.path == directory for item in info.worktrees):
                raise SessionServiceError(409, "git.worktreeChanged")
            client = await coding_client(db, user.id, session.preferred_execution_agent_id)
            if not payload.name:
                raise SessionServiceError(422, "validation.required", field="name")
            await client.request("git.branch", {
                "path": directory, "action": payload.action.removeprefix("branch-"),
                "name": payload.name, "targetBranch": payload.target_branch,
            }, ExecutionResult)
        await db.commit()
        return {"ok": True}
    except ExecutionClientError as exc:
        raise ApiError(409, exc.code) from exc
    except SessionServiceError as exc:
        raise ApiError(exc.status_code, exc.code, field=exc.field, params=exc.params) from exc


class SessionWorkspaceRequest(BaseModel):
    directory: str | None = Field(default=None, max_length=4096)
    create: bool = False
    worktree_id: UUID | None = None
    restore: bool = False


@router.put("/sessions/{session_id}/workspace")
async def switch_workspace(session_id: UUID, payload: SessionWorkspaceRequest,
    user: Annotated[UserAccount, Depends(require_user)], db: Annotated[AsyncSession, Depends(get_db_session)]):
    try:
        session, project = await workspace_session(db, user, session_id)
        policy = await select_session_workspace(db, session, project, payload.directory, create=payload.create,
                                                worktree_id=payload.worktree_id, restore=payload.restore)
        await persist_worktree_warning(db, session)
        await grant_session_workspace(db, session, policy)
        await db.commit()
        return {"currentDirectory": session.current_directory, "workspaceMode": session.workspace_mode,
                "codingWorkspace": session.coding_workspace}
    except SessionServiceError as exc:
        raise ApiError(exc.status_code, exc.code, field=exc.field, params=exc.params) from exc
