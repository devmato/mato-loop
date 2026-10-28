from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import mato_loop_backend.agent.run_support as agent_run_support
from mato_loop_backend.agent.activity import run_activity_label
from mato_loop_backend.agent.run_contracts import ApprovalResolution
from mato_loop_backend.agent.run_timing import run_timing_payload, transition_run_status
from mato_loop_backend.agent.subagents import permission_origin, provenance
from mato_loop_backend.events import run_events
from mato_loop_backend.execution import ExecutionClient, ExecutionClientError, WorkspaceStatResult
from mato_loop_backend.execution.manager import execution_agent_connections
from mato_loop_backend.files.service import RootFileError, RootFileService
from mato_loop_backend.messaging.approvals import enqueue_approval_prompt
from mato_loop_backend.models import AgentRun, ApprovalRequest, ChatSession, ExecutionAgent, Message
from mato_loop_backend.notifications import NotificationService
from mato_loop_backend.permissions.coordinator import approval_coordinator
from mato_loop_backend.permissions.policy import (
    PermissionRequest,
    session_grant_options_payload,
    session_grants_display,
    session_grants_fingerprint,
    session_grants_payload,
)
from mato_loop_backend.providers.base import ToolCall
from mato_loop_backend.tools.contracts import PERMISSION_REASON_ARGUMENT, ToolCallSuperseded
from mato_loop_backend.tools.display import tool_display


async def prepare_file_grants(permission: PermissionRequest, root_files: RootFileService) -> PermissionRequest:
    """Resolve required file scopes after hard policy checks."""
    grants = []
    for grant in permission.effective_session_grants:
        if grant.resource_type not in {"execution_path", "session_path"}:
            grants.append(grant)
            continue
        scope = json.loads(grant.resource_scope)
        recursive = permission.action_kind not in {
            "write_file", "edit_file", "apply_patch", "download_file", "browser_download", "browser_upload_source",
        }
        if recursive and permission.action_kind != "create_directory":
            try:
                if grant.resource_type == "execution_path":
                    agent_id = UUID(scope["agentId"])
                    client = ExecutionClient(agent_id, execution_agent_connections.snapshot(agent_id) or frozenset())
                    result = await client.request("filesystem.stat", {"path": scope["path"]}, WorkspaceStatResult)
                    recursive = result.path != scope["path"] or result.kind != "file"
                else:
                    recursive = await asyncio.to_thread(root_files.path_kind, scope["root"], scope["path"]) == "directory"
            except (ExecutionClientError, RootFileError):
                # Unknown targets may be directories; never promise narrower access.
                recursive = True
        scope.pop("recursive", None)
        if recursive:
            scope["recursive"] = True
        grants.append(replace(grant, resource_scope=json.dumps(
            scope, ensure_ascii=True, separators=(",", ":"), sort_keys=True,
        )))
    return replace(permission, session_grants=tuple(grants))


async def request_approval(
    cls,
    db: AsyncSession,
    run: AgentRun,
    chat_session: ChatSession,
    call: ToolCall,
    permission: PermissionRequest,
    *,
    display: dict[str, str] | None = None,
    argument_edit: dict[str, Any] | None = None,
) -> str | ApprovalResolution:
    if run.id in cls._pending_continuations:
        raise ToolCallSuperseded
    origin = await permission_origin(db, chat_session)
    if permission.resource_type in {"execution_administrator", "execution_directory", "shell_command"}:
        agent = await db.get(ExecutionAgent, UUID(json.loads(permission.resource_scope)["agentId"]))
        if isinstance(agent, ExecutionAgent) and agent.user_id == chat_session.user_id:
            permission = replace(
                permission, title=f"Administratorzugriff auf „{agent.display_name}“ erlauben?" if permission.resource_type == "execution_administrator" else permission.title,
                display_items=tuple(replace(item, value=agent.display_name) if item.label == "Computer" else item for item in permission.display_items),
            )
    resource_display = permission.resource_display
    if (
        resource_display is None
        and permission.capability == "session.read"
        and permission.resource_type == "session"
    ):
        resource_display = await db.scalar(
            select(ChatSession.title).where(
                ChatSession.id == UUID(permission.resource_scope),
                ChatSession.user_id == chat_session.user_id,
                ChatSession.session_type == "chat",
            )
        )
        permission = replace(permission, resource_display=resource_display)
    grant_payload = session_grants_payload(permission)
    available_decisions = [
        decision for decision in permission.available_decisions
        if decision != "allow_session" or grant_payload
    ]
    reason = call.arguments.get(PERMISSION_REASON_ARGUMENT)
    if not isinstance(reason, str) or not (reason := reason.strip()):
        reason = "Keine Begründung angegeben"
    approval = ApprovalRequest(
        request_type="agent_action",
        user_id=chat_session.user_id,
        session_id=origin.id,
        run_id=run.id,
        action_kind=permission.action_kind,
        risk_level=permission.risk_level,
        title=permission.title,
        reason=reason,
        summary=permission.summary,
        capability=permission.capability,
        resource_type=permission.resource_type,
        resource_scope=permission.resource_scope,
        payload={
            "toolCallId": call.id,
            "tool": call.name,
            "arguments": agent_run_support._safe_tool_arguments(call.name, call.arguments),
            "toolDisplay": display
            or tool_display(call.name, agent_run_support._safe_tool_arguments(call.name, call.arguments)),
            "resourceDisplay": resource_display,
            "policyIntegrationId": (
                str(permission.policy_integration_id)
                if permission.policy_integration_id is not None
                else None
            ),
            "policyResourceKey": permission.policy_resource_key,
            "agentProfileId": (
                str(run.agent_profile_id or chat_session.agent_profile_id)
                if run.agent_profile_id is not None
                or chat_session.agent_profile_id is not None
                else None
            ),
            "trustLevel": run.trust_level,
            "sessionGrants": grant_payload,
            "grantOptions": session_grant_options_payload(permission) if "allow_session" in available_decisions else [],
            "runActionScope": agent_run_support._run_action_scope(permission, call),
            "runActionOnly": "allow_session" not in permission.available_decisions,
            "sessionGrantsFingerprint": session_grants_fingerprint(
                permission.capability,
                permission.resource_type,
                permission.resource_scope,
                grant_payload,
            ),
            **(permission.approval_details or {}),
            **provenance(chat_session),
            **({"argumentEdit": argument_edit} if argument_edit else {}),
            "displayItems": [
                {
                    "label": item.label,
                    "value": list(item.value) if isinstance(item.value, tuple) else item.value,
                    "format": item.format,
                }
                for item in permission.display_items
            ] + ([{
                "label": "Freigegebener Bereich",
                "value": session_grants_display(permission),
                "format": "list",
            }] if "allow_session" in available_decisions else []),
        },
        available_decisions=available_decisions,
    )
    db.add(approval)
    transition_run_status(run, "waiting_approval")
    await db.flush()
    notification_id = await NotificationService.create_for_approval(db, approval)
    if run.input_message_id is not None:
        input_message = await db.get(Message, run.input_message_id)
        if (
            input_message is not None
            and input_message.source_type == "endpoint"
            and input_message.source_endpoint_id is not None
        ):
            await enqueue_approval_prompt(db, approval)
    approval_id, run_id = approval.id, run.id
    waiter = approval_coordinator.register(approval_id)
    cls._waiting_interactions[run_id] = (chat_session.spawned_by_run_id, waiter)
    if run_id in cls._pending_continuations:
        waiter.set_result("superseded")
    try:
        if chat_session.spawned_by_run_id in cls._pending_continuations:
            cls.cancel(run_id)
        await db.commit()
        await run_events.publish(
            run.id, {"type": "run.timing", "runId": str(run.id), **run_timing_payload(run)}
        )
        await run_events.publish(
            run.id,
            {
                "type": "approval.created",
                "approval": agent_run_support.approval_payload(approval),
                "activityLabel": run_activity_label("waiting_approval"),
            },
        )
        if notification_id is not None:
            await NotificationService.deliver_after_commit(notification_id)
        decision = await asyncio.shield(waiter)
    except asyncio.CancelledError:
        await db.rollback()
        current = await db.get(ApprovalRequest, approval_id)
        if current is not None and current.status == "pending":
            current.status = "expired"
            current.resolved_at = datetime.now(UTC)
            await db.commit()
            await run_events.publish(run_id, {"type": "approval.expired", "approvalId": str(approval_id)})
        raise
    finally:
        cls._waiting_interactions.pop(run_id, None)
        approval_coordinator.discard(approval_id)
    await db.refresh(approval, with_for_update=True)
    superseded = decision == "superseded" or run_id in cls._pending_continuations
    if superseded and approval.status == "pending":
        approval.status = "expired"
        approval.resolved_at = datetime.now(UTC)
    transition_run_status(run, "running")
    await db.commit()
    if superseded:
        await run_events.publish(run_id, {"type": "approval.expired", "approvalId": str(approval_id)})
    await run_events.publish(
        run.id, {"type": "run.timing", "runId": str(run.id), **run_timing_payload(run)}
    )
    if superseded:
        raise ToolCallSuperseded
    return decision
