import json
from dataclasses import replace
from typing import Any

from pydantic import Field

from mato_loop_backend.execution import ShellInspectResult
from mato_loop_backend.files.access import filesystem_permission
from mato_loop_backend.permissions.policy import (
    PermissionDecision,
    memory_read_permission,
    session_browse_permission,
    session_search_permission,
    shell_command_permissions,
)
from mato_loop_backend.permissions.resolution import tool_execution_policy
from mato_loop_backend.providers.base import ToolCall
from mato_loop_backend.settings import ToolExecutionPolicy
from mato_loop_backend.tools.access import profile_tool_settings
from mato_loop_backend.tools.actions import ToolAction
from mato_loop_backend.tools.arguments import ToolArguments
from mato_loop_backend.tools.context import ToolContext
from mato_loop_backend.tools.contracts import ToolDefinition, ToolResult


class PermissionTarget(ToolArguments):
    tool: str = Field(
        min_length=1, max_length=200,
        description="Exact offered tool name, such as shell_run. Do not add a functions. prefix.",
    )
    arguments: dict[str, Any] = Field(
        description=(
            "Arguments matching the named tool's schema, as a JSON object, not a JSON string. "
            "If the target tool offers _reason, its optional explanation belongs inside this object."
        ),
    )


class RequestSessionPermissionsArguments(ToolArguments):
    requests: list[PermissionTarget] = Field(
        min_length=1, max_length=8,
        description=(
            "Proposed tool calls. Each item contains only tool and arguments. "
            "An optional explanation for the whole preparation belongs in _reason alongside requests."
        ),
    )


async def builtin_permissions(arguments, context: ToolContext, name: str):
    capabilities = {
        "read": "filesystem.read", "glob": "filesystem.read", "grep": "filesystem.read",
        "write_file": "filesystem.write", "edit_file": "filesystem.write",
        "create_directory": "filesystem.write", "delete_path": "filesystem.delete",
    }
    if name in capabilities and not getattr(arguments, "elevated", False) and not getattr(arguments, "session_id", None):
        capability = capabilities[name]
        if arguments.root == "session" and capability == "filesystem.write":
            capability = "session.files.write"
        return (filesystem_permission(
            context, root=arguments.root, path=arguments.path, capability=capability,
            action_kind=name, action_label="Session-Zugriff vorbereiten",
        ),)
    if name == "session_read":
        from mato_loop_backend.tools.sessions import session_read_permission

        _target, permission = await session_read_permission(context, arguments.session_id or context.session.id)
        return (permission,)
    if name == "shell_run" and not arguments.elevated and not arguments.environment:
        client = context.execution_client
        if client is None or client.current_directory is None:
            return ()
        inspection = await client.request("shell.inspect", {
            "command": arguments.command, "currentDirectory": client.current_directory,
        }, ShellInspectResult)
        return shell_command_permissions(client.agent_id, client.current_directory, arguments.command, inspection)
    if name == "session_search":
        return (session_search_permission(include_private=arguments.include_private, trust_level=context.model_trust_level),)
    if name == "context_search":
        from mato_loop_backend.tools.context_search import (
            requested_sources,
            session_context_permission,
            source_available,
        )

        settings = await profile_tool_settings(context)
        return tuple([
            memory_read_permission(context.session.user_id) if source == "memories" else await session_context_permission(arguments, context)
            for source in requested_sources(arguments)
            if source_available(settings, source, memory_enabled=context.memory_read_enabled)
        ])
    if name == "session_browse":
        return (session_browse_permission(include_private=arguments.include_private, trust_level=context.model_trust_level),)
    return ()


async def authorize_session_requests(context: ToolContext, requests) -> ToolResult:
    if not requests:
        return ToolResult.error("permission.preflightUnavailable")
    for request in requests:
        decision = await context.ask_handler(request, check_only=True)
        if decision == PermissionDecision.ALLOW:
            continue
        if decision != PermissionDecision.ASK:
            return ToolResult.error("permission.denied")
        if request.sensitive_access and tool_execution_policy(
            await profile_tool_settings(context), "sensitive_data.access",
        ) != ToolExecutionPolicy.AUTO:
            return ToolResult.error("permission.preflightUnavailable")
        if "allow_session" not in request.available_decisions or not request.effective_session_grants:
            return ToolResult.error("permission.preflightUnavailable")
        if await context.ask(replace(request, available_decisions=("allow_session", "deny"),
            summary=request.summary + " Es wird jetzt keine Aktion ausgeführt.")) != PermissionDecision.ALLOW:
            return ToolResult.error("permission.denied")
    return ToolResult.json({"permissionsReady": True, "executed": False})


async def request_session_permissions(arguments: RequestSessionPermissionsArguments, context: ToolContext) -> ToolResult:
    from mato_loop_backend.tools.catalog import tool_registry
    from mato_loop_backend.tools.executor import execute_definition

    settings = await profile_tool_settings(context)
    tools = tool_registry.effective(
        await context.current_integrations(), tool_settings=settings,
        memory_enabled=context.memory_read_enabled, memory_write_enabled=context.memory_write_enabled,
        execution_operations=context.execution_client.operations if context.execution_client else frozenset(),
    )
    if settings._delegation_ceiling is not None:
        tools = tools.only(*settings._delegation_ceiling["toolNames"])
    for index, target in enumerate(arguments.requests):
        target_field = f"requests.{index}"
        bound = tools.tools.get(target.tool)
        if bound is None:
            return ToolResult.error(
                "tool.invalidArguments", field=f"{target_field}.tool", params={"reason": "invalid_choice"},
            )
        call = ToolCall(context.tool_call.id, target.tool, target.arguments)
        prepared = replace(context, tool_call=call, tool_settings=settings, bound_options={})
        result = await execute_definition(tool_registry, call, prepared, bound, permissions_only=True)
        context.user_denied = context.user_denied or prepared.user_denied
        if result.is_error:
            error = json.loads(result.content)["error"]
            field = error.get("field")
            return ToolResult.error(
                "permission.userDenied" if context.user_denied else error["code"],
                field=f"{target_field}.arguments.{field}" if field else target_field,
                params=error.get("params"),
            )
    return ToolResult.json({"permissionsReady": True, "executed": False})


REQUEST_SESSION_PERMISSIONS = ToolDefinition(
    "request_session_permissions",
    "Prepare session permissions without execution. Use offered names and arguments. "
    "Supports current-session/workspace files, shell, context search, session read/browse and declared "
    "integrations. No elevation, environment overrides, automation management, notify or silence. "
    "For directory read/write/delete, propose read/create_directory/delete_path at that directory. "
    "Files use exact paths; directories and unknown targets include descendants. Approval can select the parent. "
    "Shell scopes bind command/context or the computer. "
    "Allowed access needs no prompt; disabled stays blocked. Action-bound consent requires execution. "
    "Prepare known automation access. Wait for permissionsReady=true.",
    RequestSessionPermissionsArguments, request_session_permissions,
    label="Session-Freigaben anfragen", icon="verified_user",
    actions=(ToolAction("permissions.request", "Freigaben anfragen", category="write", default=ToolExecutionPolicy.AUTO, configurable=False, allowed_policies=(ToolExecutionPolicy.AUTO,)),),
)
