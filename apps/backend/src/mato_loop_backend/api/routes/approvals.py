from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend.agent.run_support import approval_payload
from mato_loop_backend.agent.session_secrets import session_secrets
from mato_loop_backend.api.errors import ApiError
from mato_loop_backend.db.session import get_db_session
from mato_loop_backend.memory.service import MemoryApprovalEdit, MemoryService
from mato_loop_backend.models import (
    AgentRun,
    ApprovalRequest,
    ChatSession,
    ExecutionAgent,
    PermissionGrant,
    UserAccount,
)
from mato_loop_backend.permissions.decisions import ApprovalDecisionError, decide_agent_action
from mato_loop_backend.permissions.policy import (
    PermissionDecision,
    PermissionPolicy,
    SessionGrantScope,
    execution_directory_permission,
    temporary_file_grants,
)
from mato_loop_backend.security import require_user
from mato_loop_backend.sessions.service import (
    ACTIVE_RUN_STATUSES,
    SessionServiceError,
    get_owned_session,
)
from mato_loop_backend.settings import (
    AgentProfileUnavailable,
    TrustLevel,
    effective_model_trust,
    load_agent_profile_tools,
    load_effective_ai,
)
from mato_loop_backend.settings.effective import apply_session_ai

router = APIRouter(prefix="/approvals", dependencies=[Depends(require_user)])


class ApprovalArgumentEdit(BaseModel):
    path: list[Annotated[str, Field(min_length=1, max_length=80)]] = Field(
        min_length=1, max_length=4
    )
    value: str = Field(max_length=256 * 1024)


class ApprovalDecisionRequest(BaseModel):
    decision: Literal["allow_once", "allow_session", "approve", "deny"]
    grant_option: str | None = Field(default=None, min_length=1, max_length=80)
    memory_edit: MemoryApprovalEdit | None = None
    argument_edits: list[ApprovalArgumentEdit] | None = Field(
        default=None, min_length=1, max_length=8
    )
    argument_revision: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    expected_revision: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )


async def _permission_payload(
    db: AsyncSession, user: UserAccount, grant: PermissionGrant, source: ApprovalRequest | None,
    session: ChatSession, binding: tuple[UUID | None, TrustLevel],
) -> dict:
    resource_display = None
    shell_details = {}
    applicable = True
    if grant.source_approval_id is not None:
        if grant.resource_type == "skill_collection":
            resource_display = "Persönliche Skills"
        elif source and isinstance(source.payload.get("resourceDisplay"), str):
            resource_display = source.payload["resourceDisplay"]
        if source is not None and isinstance(source.payload.get("grantedScopes", source.payload.get("sessionGrants")), list):
            matching_scope = next(
                (
                    item for item in source.payload.get("grantedScopes", source.payload.get("sessionGrants", []))
                    if isinstance(item, dict)
                    and item.get("resourceType") == grant.resource_type
                    and item.get("resourceScope") == grant.resource_scope
                    and SessionGrantScope(
                        item.get("category"), item.get("resourceType"), item.get("resourceScope"),
                    ).capability == grant.capability
                ),
                None,
            )
            if matching_scope is not None and isinstance(
                matching_scope.get("resourceDisplay"), str
            ):
                resource_display = matching_scope["resourceDisplay"]
        if grant.resource_type == "shell_command" and source is not None:
            shell_details = {
                "shellClassification": source.payload.get("shellClassification"),
                "shellPrefixes": source.payload.get("shellPrefixes"),
            }
    if grant.resource_type == "execution_administrator":
        computer = await db.get(ExecutionAgent, UUID(grant.resource_scope))
        if computer is not None and computer.user_id == user.id:
            resource_display = computer.display_name
        applicable = False
    profile_id, trust = binding
    applicable = applicable and grant.agent_profile_id == profile_id and grant.trust_level == trust.value
    target = source if grant.resource_type == "run_action" and source is not None else grant
    try:
        scope = json.loads(target.resource_scope)
        if not isinstance(scope, dict):
            scope = {}
        if target.resource_type == "shell_command" and isinstance(scope.get("action"), str):
            scope = json.loads(scope["action"])
            if not isinstance(scope, dict):
                scope = {}
    except (ValueError, TypeError):
        scope = {}
    if target.resource_type == "shell_computer":
        scope = {"agentId": target.resource_scope}
        resource_display = "Shell"
    if grant.resource_type in {"root_path", "execution_directory"} and grant.capability in {"resource.read", "resource.write"}:
        applicable = False
    if grant.resource_type == "shell_command":
        applicable = False
    if target.resource_type in {"execution_directory", "execution_path", "administrator_path", "shell_action", "shell_command", "execution_administrator", "shell_computer"}:
        try:
            computer_id = UUID(scope["agentId"])
            computer = await db.get(ExecutionAgent, computer_id)
        except (KeyError, ValueError, TypeError):
            computer = None
        if computer is not None and computer.user_id == user.id:
            shell_details.update(executionAgentId=str(computer.id), executionAgentName=computer.display_name)
            applicable = applicable and computer.revoked_at is None and computer.id == session.preferred_execution_agent_id
        else:
            applicable = False
        resource_display = scope.get("path") or scope.get("currentDirectory") or resource_display
        if target.capability in {"shell.read", "shell.write", "shell.delete"} or target.resource_type == "shell_action":
            shell_details.update(command=scope.get("command"), currentDirectory=scope.get("currentDirectory"), elevated=scope.get("elevated", target.resource_type == "execution_administrator"))
            applicable = applicable and scope.get("currentDirectory") == session.current_directory
    elif target.resource_type in {"root_path", "session_path"}:
        resource_display = f"{scope.get('root', 'session')}:{scope.get('path', '')}"
    if target.resource_type in {"administrator_path", "execution_administrator"}:
        shell_details["elevated"] = True
    return {
        "id": str(grant.id),
        "runId": str(grant.run_id) if grant.run_id is not None else None,
        "capability": grant.capability,
        "resourceType": grant.resource_type,
        "resourceScope": grant.resource_scope,
        "resourceDisplay": resource_display,
        "recursive": grant.capability.startswith("category.") and scope.get("recursive") is True,
        "agentProfileId": (
            str(grant.agent_profile_id)
            if grant.agent_profile_id is not None
            else None
        ),
        "trustLevel": grant.trust_level,
        "applicable": applicable,
        **shell_details,
        "createdAt": grant.created_at.isoformat(),
    }


async def _permission_binding(db: AsyncSession, session: ChatSession) -> tuple[UUID | None, TrustLevel]:
    run = await db.scalar(select(AgentRun).where(
        AgentRun.session_id == session.id, AgentRun.status.in_(ACTIVE_RUN_STATUSES),
    ).order_by(AgentRun.created_at.desc()).limit(1))
    if run is not None:
        from mato_loop_backend.agent.session_settings import apply_run_session_settings

        apply_run_session_settings(session, run)
        return run.agent_profile_id or session.agent_profile_id, TrustLevel(run.trust_level or "public")
    ai = apply_session_ai(await load_effective_ai(db, session.user_id), session)
    model = next((item for item in ai.models if item.id == ai.default_model_id), None)
    provider = next((item for item in ai.providers if model and item.id == model.provider_id), None)
    return session.agent_profile_id, effective_model_trust(provider, model) if provider and model else TrustLevel.PRIVATE


@router.get("/pending")
async def pending_approvals(
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[dict]:
    rows = (
        await db.execute(
            select(ApprovalRequest, ChatSession)
            .outerjoin(ChatSession, ChatSession.id == ApprovalRequest.session_id)
            .where(ApprovalRequest.status == "pending")
            .where(ApprovalRequest.user_id == user.id)
            .where(ApprovalRequest.action_kind != "terminal_command")
            .order_by(ApprovalRequest.created_at)
        )
    ).all()
    return [
        {
            **approval_payload(approval),
            "sourceSession": (
                {"id": str(session.id), "title": session.title} if session else None
            ),
        }
        for approval, session in rows
    ]


@router.post("/{approval_id}/decisions")
async def decide_approval(
    approval_id: UUID,
    payload: ApprovalDecisionRequest,
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, str]:
    approval = await db.get(ApprovalRequest, approval_id)
    if approval is None or approval.user_id != user.id:
        raise ApiError(404, "approval.notFound")
    if payload.memory_edit is not None and (
        payload.decision != "approve"
        or approval.request_type != "memory_change"
        or approval.action_kind not in {"memory_update", "memory_merge"}
    ):
        raise ApiError(400, "validation.invalid", field="memory_edit")
    if (payload.argument_edits is None) != (payload.argument_revision is None):
        raise ApiError(400, "validation.invalid", field="argument_edits")
    if payload.argument_edits is not None and (
        payload.decision != "allow_once" or payload.memory_edit is not None
    ):
        raise ApiError(400, "approval.decisionUnavailable")
    if approval.request_type == "memory_change":
        if payload.grant_option is not None:
            raise ApiError(400, "validation.invalid", field="grant_option")
        await db.refresh(approval, with_for_update=True)
        if approval.status != "pending":
            raise ApiError(409, "approval.alreadyDecided")
        if approval.action_kind == "terminal_command":
            raise ApiError(404, "approval.notFound")
        if payload.decision not in approval.available_decisions:
            raise ApiError(400, "approval.decisionUnavailable")
        if payload.decision not in {"approve", "deny"}:
            raise ApiError(400, "approval.decisionUnavailable")
        if payload.expected_revision is not None:
            raise ApiError(400, "validation.invalid", field="expected_revision")
        return await _decide_memory_change(
            approval, payload.decision, user.id, db, memory_edit=payload.memory_edit
        )

    try:
        return await decide_agent_action(
            db,
            approval,
            payload.decision,
            expected_revision=payload.expected_revision,
            argument_edits=(
                tuple((tuple(edit.path), edit.value) for edit in payload.argument_edits)
                if payload.argument_edits is not None
                else ()
            ),
            argument_revision=payload.argument_revision,
            grant_option=payload.grant_option,
        )
    except ApprovalDecisionError as exc:
        raise ApiError(
            exc.status_code, exc.code, field=exc.field, params=exc.params
        ) from exc


async def _decide_memory_change(
    approval: ApprovalRequest,
    decision: str,
    user_id: UUID,
    db: AsyncSession,
    *,
    memory_edit: MemoryApprovalEdit | None = None,
) -> dict[str, str]:
    now = datetime.now(UTC)
    if decision == "deny":
        approval.status = "denied"
        approval.decision = decision
        approval.resolved_at = now
        await db.commit()
        return {"status": approval.status, "decision": decision}

    resolver = getattr(MemoryService, "apply_approved_change", None)
    if resolver is None:
        raise ApiError(503, "memory.unavailable")
    outcome = await resolver(
        db, approval=approval, owner_id=user_id, memory_edit=memory_edit
    )
    error_code = {
        "target_changed": "approval.targetChanged",
        "policy_changed": "approval.policyChanged",
    }.get(outcome)
    if error_code is not None:
        approval.status = "expired"
        approval.resolved_at = now
        await db.commit()
        raise ApiError(409, error_code)
    if outcome != "applied":
        raise ApiError(503, "memory.unavailable")

    if memory_edit is not None:
        approval.payload = {
            **approval.payload,
            "ownerEdit": memory_edit.model_dump(mode="json"),
        }
    approval.status = "approved"
    approval.decision = decision
    approval.resolved_at = now
    await db.commit()
    return {"status": approval.status, "decision": decision}


@router.get("/sessions/{session_id}/permissions")
async def list_permissions(
    session_id: UUID,
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[dict]:
    session = await db.get(ChatSession, session_id)
    if session is None or session.user_id != user.id:
        raise ApiError(404, "session.notFound")
    grants = list(
        (
            await db.scalars(
                select(PermissionGrant)
                .where(PermissionGrant.session_id == session_id)
                .where(PermissionGrant.run_id.is_(None) | PermissionGrant.run_id.in_(
                    select(AgentRun.id).where(AgentRun.status.in_(ACTIVE_RUN_STATUSES))
                ))
                .order_by(PermissionGrant.created_at)
            )
        ).all()
    )
    payloads = [
        {
            "id": reference, "sessionId": str(session.id),
            "resourceType": "session_secret", "resourceDisplay": name,
            "source": "session_input", "scope": "session", "readOnly": True,
        }
        for name, reference in session_secrets.references(session.id).items()
    ]
    binding = await _permission_binding(db, session)
    for item in grants:
        source = (
            await db.get(ApprovalRequest, item.source_approval_id)
            if item.source_approval_id is not None
            else None
        )
        payloads.append({**await _permission_payload(db, user, item, source, session, binding),
                         "sessionId": str(session.id), "source": "approval", "scope": "session"})
    from mato_loop_backend.execution.manager import execution_agent_connections
    from mato_loop_backend.files.service import RootFileService

    agent = await db.get(ExecutionAgent, session.preferred_execution_agent_id) if session.preferred_execution_agent_id else None
    info = execution_agent_connections.system_info(agent.id) if agent is not None else None
    if agent is not None and agent.user_id == user.id and agent.revoked_at is None and info is not None and info.temporary_directory:
        try:
            settings, _ = await load_agent_profile_tools(db, user.id, binding[0], binding[1])
        except AgentProfileUnavailable:
            return payloads
        for grant in temporary_file_grants(session, agent.id, info.temporary_directory, writable=settings._temporary_files_enabled):
            category = grant.capability.removeprefix("category.")
            path = json.loads(grant.resource_scope)["path"]
            request = execution_directory_permission(agent.id, path, capability=f"filesystem.{category}", action_kind="temporary_files")
            decision = await PermissionPolicy.evaluate(
                db, session, request, RootFileService(session.id, project_id=session.project_id),
                temporary_files_enabled=settings._temporary_files_enabled,
            )
            payloads.append({
                "id": f"temporary:{session.id}:{category}", "sessionId": str(session.id),
                "capability": grant.capability, "resourceType": grant.resource_type,
                "resourceScope": grant.resource_scope, "resourceDisplay": path, "recursive": True,
                "executionAgentId": str(agent.id), "executionAgentName": agent.display_name,
                "source": "default" if category == "read" else "profile", "scope": "session",
                "derived": True, "applicable": decision == PermissionDecision.ALLOW,
            })
    return payloads


@router.get("/permissions")
async def all_permissions(
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[dict]:
    rows = (
        await db.execute(
            select(PermissionGrant, ChatSession, ApprovalRequest)
            .join(ChatSession, ChatSession.id == PermissionGrant.session_id)
            .outerjoin(
                ApprovalRequest,
                ApprovalRequest.id == PermissionGrant.source_approval_id,
            )
            .where(ChatSession.user_id == user.id, ChatSession.parent_session_id.is_(None))
            .where(PermissionGrant.run_id.is_(None) | PermissionGrant.run_id.in_(
                select(AgentRun.id).where(AgentRun.status.in_(ACTIVE_RUN_STATUSES))
            ))
            .order_by(PermissionGrant.created_at)
        )
    ).all()
    bindings = {session.id: session for _, session, _ in rows}
    bindings = {key: await _permission_binding(db, session) for key, session in bindings.items()}
    return [
        {
            **await _permission_payload(db, user, grant, source, session, bindings[session.id]),
            "sessionId": str(session.id),
            "sourceSession": {"id": str(session.id), "title": session.title},
        }
        for grant, session, source in rows
    ]


@router.delete(
    "/sessions/{session_id}/permissions", status_code=status.HTTP_204_NO_CONTENT
)
async def revoke_permissions(
    session_id: UUID,
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> None:
    try:
        session = await get_owned_session(db, session_id, user.id)
    except SessionServiceError as exc:
        raise ApiError(exc.status_code, exc.code) from exc
    if session is None or session.user_id != user.id:
        raise ApiError(404, "approval.notFound")
    await db.execute(
        delete(PermissionGrant).where(
            PermissionGrant.session_id == session_id,
            PermissionGrant.run_id.is_(None),
        )
    )
    await db.commit()


@router.delete(
    "/sessions/{session_id}/permissions/{grant_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def revoke_permission(
    session_id: UUID,
    grant_id: UUID,
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> None:
    try:
        session = await get_owned_session(db, session_id, user.id)
    except SessionServiceError as exc:
        raise ApiError(exc.status_code, exc.code) from exc
    if session is None or session.user_id != user.id:
        raise ApiError(404, "approval.notFound")
    grant = await db.get(PermissionGrant, grant_id)
    if grant is None or grant.session_id != session_id:
        raise ApiError(404, "approval.notFound")
    await db.delete(grant)
    await db.commit()
