from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend.db.session import AsyncSessionFactory
from mato_loop_backend.error_contract import error_payload
from mato_loop_backend.events import run_events
from mato_loop_backend.files.storage import SessionStorage
from mato_loop_backend.models import AgentRun, ChatSession, Message, WebSearchPolicy
from mato_loop_backend.settings import (
    AgentProfileUnavailable,
    IntegrationSettings,
    ToolSettings,
    TrustLevel,
    load_agent_profile_tools,
    load_effective_integrations,
)
from mato_loop_backend.settings.effective import filter_agent_profile_integrations
from mato_loop_backend.settings.models import AgentProfilePermissions
from mato_loop_backend.tools.arguments import TaskArguments
from mato_loop_backend.tools.contracts import ToolResult

if TYPE_CHECKING:
    from mato_loop_backend.agent.runtime import AgentRuntime

MAX_CHILDREN = 8
MAX_RESULT_CHARACTERS = 12_000
def permission_snapshot(
    settings: ToolSettings, tool_names: set[str],
    integration_settings: IntegrationSettings | None = None,
) -> dict[str, Any]:
    from mato_loop_backend.permissions.resolution import tool_default_policy
    from mato_loop_backend.tools.catalog import tool_registry
    from mato_loop_backend.tools.context_search import source_available

    integrations = integration_settings or IntegrationSettings()
    # Only permission data belongs here, never integration configuration or credentials.
    return {
        "permissions": settings.permissions.model_dump(mode="json"),
        "agentPermissions": (
            settings._agent_permissions.model_dump(mode="json")
            if settings._agent_permissions is not None else None
        ),
        "delegationEnabled": bool(
            settings._agent_profile and settings._agent_profile.delegation_enabled
        ),
        "sessionWritePaths": (
            settings._agent_profile.session_write_paths
            if settings._agent_profile is not None else None
        ),
        "temporaryFilesEnabled": settings._temporary_files_enabled,
        "shellPrefixes": settings.shell_prefixes.model_dump(mode="json"),
        "toolNames": sorted(tool_names - {"task"}),
        "contextSources": [source for source in ("memories", "sessions") if source_available(settings, source)],
        "integrationIds": sorted(str(item.id) for item in integrations.instances if item.enabled),
        "actionDefaults": {
            action.capability: tool_default_policy(settings, action.capability, action.default_policy(settings)).value
            for action in tool_registry.policy_actions(integrations)
        },
        "integrationDefaults": {
            str(instance.id): {
                action.capability: action.default_policy(settings).value
                for action in tool_registry.integration_resource_actions(instance)
            }
            for instance in integrations.instances if instance.enabled
        },
    }


def integration_ceiling_ids(snapshot: dict[str, Any] | None) -> list[str] | None:
    if snapshot is None:
        return None
    ids = snapshot.get("integrationIds", [])
    if not isinstance(ids, list):
        return []
    return [value for value in ids if isinstance(value, str)]


def session_write_paths_ceiling(snapshot: dict[str, Any] | None) -> list[str] | None:
    restrictions = []
    while snapshot is not None:
        paths = snapshot.get("sessionWritePaths")
        if paths is not None:
            restrictions.append(set(paths) if isinstance(paths, list) else set())
        snapshot = snapshot.get("offeredCeiling")
    return sorted(set.intersection(*restrictions)) if restrictions else None


def ceiling_settings(snapshot: dict[str, Any], trust: TrustLevel) -> ToolSettings:
    permissions = AgentProfilePermissions.model_validate(snapshot["permissions"])
    settings = ToolSettings(
        private_permissions=permissions,
        public_permissions=permissions,
        shell_prefixes=snapshot["shellPrefixes"],
    ).select_trust(trust)
    settings._frozen_action_defaults = snapshot["actionDefaults"]
    settings._temporary_files_enabled = snapshot.get("temporaryFilesEnabled", False) is True
    if snapshot["agentPermissions"] is not None:
        settings._agent_permissions = AgentProfilePermissions.model_validate(
            snapshot["agentPermissions"]
        )
    return apply_delegation_ceiling(settings, snapshot.get("offeredCeiling"))


def apply_delegation_ceiling(settings: ToolSettings, snapshot: dict[str, Any] | None) -> ToolSettings:
    settings._delegation_ceiling = snapshot
    settings._temporary_files_enabled &= temporary_files_ceiling(snapshot)
    if snapshot is not None:
        # Aggregate integrations enumerate scoped keys before checking each resource policy.
        # Retain those keys, not old values that could override stricter current defaults.
        for ceiling in (snapshot, snapshot.get("offeredCeiling")):
            if ceiling is None:
                continue
            for permissions in (ceiling["permissions"], ceiling["agentPermissions"] or {}):
                for integration_id, resources in permissions.get("resource_policies", {}).items():
                    current = settings.permissions.resource_policies.setdefault(integration_id, {})
                    for resource_key in resources:
                        current.setdefault(resource_key, {})
                for integration_id, resources in permissions.get("resource_availability", {}).items():
                    current = settings.permissions.resource_policies.setdefault(integration_id, {})
                    for resource_key in resources:
                        current.setdefault(resource_key, {})
    return settings


def temporary_files_ceiling(snapshot: dict[str, Any] | None) -> bool:
    while snapshot is not None:
        if snapshot.get("temporaryFilesEnabled") is not True:
            return False
        snapshot = snapshot.get("offeredCeiling")
    return True


async def delegation_snapshot(
    db: AsyncSession, run: AgentRun, session: ChatSession,
) -> dict[str, Any] | None:
    if session.parent_session_id is None:
        return None
    message = await db.get(Message, run.input_message_id) if run.input_message_id else None
    metadata = (message.provider_data or {}).get("_matoloop", {}) if message else {}
    snapshot = metadata.get("delegationCeiling")
    if not isinstance(snapshot, dict):
        from mato_loop_backend.agent.run_contracts import AgentRunError

        raise AgentRunError("tool.unavailable")
    return snapshot


async def permission_origin(db: AsyncSession, session: ChatSession) -> ChatSession:
    if session.parent_session_id is None:
        return session
    parent = await db.get(ChatSession, session.parent_session_id)
    if parent is None or parent.user_id != session.user_id or parent.parent_session_id is not None:
        from mato_loop_backend.agent.run_contracts import AgentRunError

        raise AgentRunError("session.notFound")
    return parent


def provenance(session: ChatSession) -> dict[str, str]:
    return (
        {"subagentSessionId": str(session.id), "subagentTitle": session.title}
        if session.parent_session_id is not None else {}
    )


async def run_tasks(
    runtime: type[AgentRuntime],
    db: AsyncSession,
    parent: AgentRun,
    session: ChatSession,
    arguments: TaskArguments,
    *,
    tool_call_id: str,
    tool_settings: ToolSettings,
    tool_names: set[str],
    integration_settings: IntegrationSettings | None = None,
    max_tool_calls: int | None,
) -> ToolResult:
    if session.parent_session_id is not None or (
        tool_settings._agent_profile is None
        or not tool_settings._agent_profile.delegation_enabled
    ):
        return ToolResult.error("tool.unavailable")
    count = await db.scalar(
        select(func.count()).select_from(ChatSession).where(
            ChatSession.spawned_by_run_id == parent.id
        )
    ) or 0
    if count + len(arguments.tasks) > MAX_CHILDREN:
        return ToolResult.error("agent.subagentLimitReached")
    offered_snapshot = permission_snapshot(tool_settings, tool_names, integration_settings)
    input_message = await db.get(Message, parent.input_message_id) if parent.input_message_id else None
    input_metadata = (input_message.provider_data or {}).get("_matoloop", {}) if input_message else {}
    active_file = deepcopy(input_metadata.get("activeFile"))
    # Flush dirty parent input/tool metadata and release locks before children can ask for grants.
    commit = asyncio.create_task(db.commit())
    try:
        await asyncio.shield(commit)
    except asyncio.CancelledError:
        await commit
        raise

    children: list[tuple[UUID, UUID, str, asyncio.Task[None]]] = []

    async def start_children() -> None:
        async with AsyncSessionFactory() as child_db:
            current_tools, profile = await load_agent_profile_tools(
                child_db, session.user_id, parent.agent_profile_id or session.agent_profile_id,
                TrustLevel(parent.trust_level),
            )
            if not current_tools.tool_availability.get("task", True):
                raise AgentProfileUnavailable
            if profile is None or not profile.delegation_enabled:
                raise AgentProfileUnavailable
            current_integrations = filter_agent_profile_integrations(
                await load_effective_integrations(child_db, session.user_id), profile,
                allowed_ids=integration_ceiling_ids(offered_snapshot),
            )
            snapshot = permission_snapshot(
                current_tools,
                {name for name in tool_names if current_tools.tool_availability.get(name, True)},
                current_integrations,
            )
            # Keep both policy layers: scoped overrides and shell prefixes cannot widen the old offer.
            snapshot["offeredCeiling"] = offered_snapshot
            prepared = []
            from mato_loop_backend.settings import apply_session_ai, load_effective_ai

            session_ai = apply_session_ai(await load_effective_ai(child_db, session.user_id), session, parent.agent_profile_id or session.agent_profile_id)
            storage = SessionStorage()
            try:
                for index, task in enumerate(arguments.tasks, start=count):
                    child_run_id = uuid4()
                    child_session = ChatSession(
                        id=uuid4(), user_id=session.user_id,
                        session_type="chat", title=task.description,
                        title_generation_status="skipped", project_id=session.project_id,
                        parent_session_id=session.id, spawned_by_run_id=parent.id,
                        spawned_by_tool_call_id=tool_call_id, spawn_index=index,
                        agent_profile_id=parent.agent_profile_id or session.agent_profile_id,
                        model_config_id=parent.model_config_id,
                        trust_level=session.trust_level,
                        reasoning_effort=session_ai.reasoning_effort,
                        preferred_execution_agent_id=parent.execution_agent_id,
                        current_directory=parent.current_directory_snapshot,
                        memory_mode="disabled",
                        web_search_policy=(
                            WebSearchPolicy.DISABLED.value
                            if session.web_search_policy == WebSearchPolicy.DISABLED
                            else WebSearchPolicy.AVAILABLE.value
                        ),
                    )
                    message = Message(
                        id=uuid4(), session_id=child_session.id, role="user", content=task.prompt,
                        trust_level=parent.trust_level, source_type="app",
                        provider_data={"_matoloop": {
                            "delegationCeiling": deepcopy(snapshot),
                            "maxToolCallsByRun": {str(child_run_id): max_tool_calls},
                            **({"activeFile": deepcopy(active_file)} if active_file is not None else {}),
                        }},
                    )
                    child = AgentRun(
                        id=child_run_id, session_id=child_session.id, parent_run_id=None,
                        automation_origin=bool(parent.automation_origin),
                        input_message_id=message.id, status="queued",
                        agent_profile_id=child_session.agent_profile_id,
                        model_config_id=parent.model_config_id, model_name=parent.model_name,
                        trust_level=parent.trust_level,
                        pseudonymization_enabled=parent.pseudonymization_enabled,
                        execution_agent_id=parent.execution_agent_id,
                        execution_agent_revision=parent.execution_agent_revision,
                        current_directory_snapshot=parent.current_directory_snapshot,
                        execution_operations=list(parent.execution_operations or []),
                    )
                    child_db.add(child_session)
                    await child_db.flush()
                    prepared.append((child_session, child))
                    await asyncio.to_thread(
                        storage.provision, child_session.id, child_session.project_id,
                        user_id=child_session.user_id,
                    )
                    child_db.add(message)
                    await child_db.flush()
                    child_db.add(child)
                await child_db.commit()
            except BaseException:
                await child_db.rollback()
                for child_session, _child in prepared:
                    await asyncio.to_thread(
                        storage.remove_session, child_session.id, child_session.project_id
                    )
                raise
            # No await between the durable checkpoint and registering every scheduled child.
            for child_session, child in prepared:
                run_events.register_subagent(child.id, parent.id, {
                    "sessionId": str(child_session.id), "runId": str(child.id),
                    "title": child_session.title, "status": "queued",
                    "spawnIndex": child_session.spawn_index, "toolCallId": tool_call_id,
                })
                children.append((child_session.id, child.id, child_session.title, runtime._schedule_run(child)))

    startup = asyncio.create_task(start_children())
    try:
        try:
            await asyncio.shield(startup)
        except asyncio.CancelledError:
            await startup
            raise
        await asyncio.shield(asyncio.gather(*(task for _, _, _, task in children), return_exceptions=True))
        results = []
        private_data_used = False
        async with AsyncSessionFactory() as result_db:
            for session_id, run_id, title, _task in children:
                child = await result_db.get(AgentRun, run_id)
                child_session = await result_db.get(ChatSession, session_id)
                private_data_used |= bool(child_session and child_session.private_integration_data_used)
                message = (
                    await result_db.get(Message, child.result_message_id)
                    if child is not None and child.result_message_id else None
                )
                status = child.status if child is not None else "failed"
                content = message.content if message is not None else ""
                metadata = (message.provider_data or {}).get("_matoloop", {}) if message else {}
                cause = (child.error if child is not None else "agent.failed") or metadata.get("completionCause")
                if status != "completed" and not cause:
                    cause = "agent.failed"
                evidence = []
                if cause:
                    tools = list((await result_db.scalars(
                        select(Message).where(
                            Message.session_id == session_id,
                            Message.role == "tool",
                            func.coalesce(Message.provider_data["_matoloop"]["errorCode"].as_string(), "").not_in((
                                "agent.toolCallLimitReached", "agent.outputLimitReached",
                                "agent.repeatedToolCall", "tool.skippedAfterUserDenial",
                            )),
                        ).order_by(Message.created_at.desc(), Message.id.desc()).limit(6)
                    )).all())
                    for tool in reversed(tools):
                        tool_metadata = (tool.provider_data or {}).get("_matoloop", {})
                        evidence.append({
                            "messageId": str(tool.id), "toolCallId": tool.tool_call_id,
                            "tool": tool_metadata.get("toolName"),
                            "result": tool.content[:1_200], "truncated": len(tool.content) > 1_200,
                            "fileActions": tool_metadata.get("fileActions", [])[:6],
                            "errorCode": tool_metadata.get("errorCode"),
                        })
                results.append({
                    "sessionId": str(session_id), "runId": str(run_id), "description": title,
                    "status": status,
                    "resultMessageId": str(message.id) if message is not None else None,
                    "result": content[:MAX_RESULT_CHARACTERS],
                    "truncated": len(content) > MAX_RESULT_CHARACTERS,
                    "errorCode": cause,
                    **({"cause": error_payload(cause)["error"], "lastToolResults": evidence} if cause else {}),
                })
        return ToolResult.json({"tasks": results}, metadata={"privateIntegrationData": private_data_used})
    except AgentProfileUnavailable:
        return ToolResult.error("agentProfile.selectedUnavailable")
    finally:
        for _, run_id, _, task in children:
            if not task.done():
                runtime.cancel(run_id)
        if children:
            await asyncio.shield(asyncio.gather(*(task for _, _, _, task in children), return_exceptions=True))
        for _, run_id, _, _ in children:
            run_events.unregister_subagent(run_id)
