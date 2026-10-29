from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select

import mato_loop_backend.files.access as files_access
import mato_loop_backend.tools.access as tools_access
import mato_loop_backend.tools.administrator as administrator
from mato_loop_backend.events import run_events
from mato_loop_backend.execution import (
    DirectoryResult,
    ExecutionClientError,
    ProcessResult,
    ShellInspectResult,
    SystemInfoResult,
)
from mato_loop_backend.execution.manager import execution_agent_connections
from mato_loop_backend.execution.protocol import MAX_REQUEST_BYTES, RequestMessage
from mato_loop_backend.execution.terminals import ShellOutput, terminal_coordinator
from mato_loop_backend.models import AgentRun, ExecutionAgent, Message, UserAccount
from mato_loop_backend.permissions.policy import (
    PermissionDecision,
    administrator_permission,
    execution_agent_select_permission,
    execution_agents_list_permission,
    shell_command_permissions,
    system_info_permission,
)
from mato_loop_backend.tools.actions import (
    _EXECUTION_AGENT_SELECT,
    _EXECUTION_AGENTS_LIST,
    _SHELL_DELETE,
    _SHELL_READ,
    _SHELL_WRITE,
    _SYSTEM_INFO,
    ApprovalEditField,
)
from mato_loop_backend.tools.arguments import (
    ChangeDirectoryArguments,
    ListExecutionAgentsArguments,
    SelectExecutionAgentArguments,
    ShellRunArguments,
    SystemInfoArguments,
)
from mato_loop_backend.tools.context import ToolContext as ToolContext
from mato_loop_backend.tools.contracts import ToolDefinition as ToolDefinition
from mato_loop_backend.tools.contracts import ToolResult as ToolResult
from mato_loop_backend.tools.process_output import redact_process_output
from mato_loop_backend.tools.secrets import (
    discard_sudo_reference,
    effective_sudo_reference,
    resolve_secret_refs,
)


async def _get_system_info(
    _arguments: SystemInfoArguments, context: ToolContext
) -> ToolResult:
    client = files_access.workspace_client(context)
    if not await tools_access.allow(context, system_info_permission(client.agent_id)):
        return ToolResult.error("permission.denied")
    result = await client.request(
        "system.info",
        {"currentDirectory": client.current_directory} if client.current_directory else {},
        SystemInfoResult,
    )
    return ToolResult.json(result.model_dump(mode="json", by_alias=True))


async def _list_execution_agents(
    _arguments: ListExecutionAgentsArguments, context: ToolContext,
) -> ToolResult:
    if not await tools_access.allow(context, execution_agents_list_permission(context.session.user_id)):
        return ToolResult.error("permission.denied")
    agents = (await context.db.scalars(select(ExecutionAgent).where(
        ExecutionAgent.user_id == context.session.user_id,
        ExecutionAgent.revoked_at.is_(None),
    ).order_by(ExecutionAgent.created_at, ExecutionAgent.id))).all()
    integrations = await context.configured_integrations()
    return ToolResult.json({"items": [
        {"id": str(agent.id), "alias": agent.display_name, "platform": agent.platform,
         "connected": execution_agent_connections.is_connected(agent.id),
         "selected": agent.id == context.session.preferred_execution_agent_id,
         "integrations": [
             {"type": item.integration_type, "name": item.name}
             for item in integrations.instances
             if item.enabled and item.execution_agent_id == agent.id
         ]}
        for agent in agents
    ]})


async def _validate_workspace_change(
    context: ToolContext, agent_id: UUID, directory: str | None,
) -> AgentRun:
    from mato_loop_backend.agent.session_settings import apply_run_session_settings
    from mato_loop_backend.sessions.git_commands import check_workspace_admission
    from mato_loop_backend.sessions.service import SessionServiceError

    await context.db.execute(
        select(UserAccount.id).where(UserAccount.id == context.session.user_id).with_for_update(key_share=True)
    )
    await context.db.refresh(context.session, with_for_update=True)
    run = await context.db.get(AgentRun, context.run_id)
    if run is None:
        raise SessionServiceError(409, "tool.unavailable")
    apply_run_session_settings(context.session, run)
    if context.session.workspace_mode == "worktree":
        raise SessionServiceError(409, "git.worktreeChanged")
    message = await context.db.get(Message, run.input_message_id) if run.input_message_id else None
    command = ((message.provider_data or {}).get("_matoloop", {}).get("codingCommand")
               if message is not None else None)
    if command:
        command = {**command, "resources": [*(command.get("resources") or []), directory]}
    binding = context.session.coding_workspace or {}
    await check_workspace_admission(
        context.db, context.session.user_id, agent_id, directory, command,
        exclude_run_id=run.id,
        metadata_directory=(binding.get("commonDirectory")
                            if binding.get("executionAgentId") == str(agent_id) else None),
    )
    return run


async def _select_execution_agent(
    arguments: SelectExecutionAgentArguments, context: ToolContext,
) -> ToolResult:
    if context.session.session_type != "chat" or context.session.parent_session_id is not None:
        return ToolResult.error("tool.unavailable")
    agent = await context.db.scalar(select(ExecutionAgent).where(
        ExecutionAgent.id == arguments.agent_id,
        ExecutionAgent.user_id == context.session.user_id,
        ExecutionAgent.revoked_at.is_(None),
    ))
    if agent is None:
        return ToolResult.error("executionAgent.notFound")
    if agent.id == context.session.preferred_execution_agent_id:
        return ToolResult.json({"id": str(agent.id), "alias": agent.display_name, "selected": True})
    if not await tools_access.allow(context, execution_agent_select_permission(agent.id, agent.display_name)):
        return ToolResult.error("permission.denied")
    from mato_loop_backend.sessions.service import (
        SessionServiceError,
        validate_execution_preferences,
    )

    try:
        directory, _policy = await validate_execution_preferences(
            context.db, context.session.user_id, agent.id, None,
        )
    except SessionServiceError as exc:
        return ToolResult.error(exc.code)
    # Revalidate after approval; the target or its connection may have changed while waiting.
    agent = await context.db.scalar(select(ExecutionAgent).where(
        ExecutionAgent.id == arguments.agent_id,
        ExecutionAgent.user_id == context.session.user_id,
        ExecutionAgent.revoked_at.is_(None),
    ).execution_options(populate_existing=True))
    snapshot = execution_agent_connections.snapshot(arguments.agent_id)
    if agent is None or snapshot is None:
        return ToolResult.error("executionAgent.unavailable")
    try:
        run = await _validate_workspace_change(context, agent.id, directory)
    except SessionServiceError as exc:
        return ToolResult.error(exc.code)
    run.execution_agent_id = agent.id
    run.execution_agent_revision = agent.revision
    run.current_directory_snapshot = directory
    run.execution_operations = sorted(snapshot)
    from mato_loop_backend.agent.session_settings import apply_run_session_settings

    if not apply_run_session_settings(context.session, run):
        context.session.preferred_execution_agent_id = agent.id
        context.session.control_center = {
            key: value for key, value in (context.session.control_center or {}).items()
            if key != "terminal_auto_attach"
        }
        context.session.current_directory = directory
    await context.db.flush()
    return ToolResult.json({
        "id": str(agent.id), "alias": agent.display_name, "currentDirectory": directory,
        "selected": True,
        "instruction": "The selected computer changed. Files on the previous computer were not transferred. Use their recorded computer binding.",
    }, metadata={"executionAgentChanged": True})


async def _change_directory(
    arguments: ChangeDirectoryArguments, context: ToolContext
) -> ToolResult:
    client = files_access.workspace_client(context)
    result = await client.request(
        "filesystem.resolve",
        {"path": files_access.execution_path(context, arguments.path)},
        DirectoryResult,
    )
    assert isinstance(result, DirectoryResult)
    if result.path == client.current_directory:
        return ToolResult.json({"currentDirectory": result.path})
    if not await files_access.directory_allowed(
        context,
        result.path,
        ask=True,
        changes_directory=True,
    ):
        return ToolResult.error("permission.denied")
    from mato_loop_backend.sessions.service import SessionServiceError

    try:
        run = await _validate_workspace_change(context, client.agent_id, result.path)
    except SessionServiceError as exc:
        return ToolResult.error(exc.code)
    run.current_directory_snapshot = result.path
    from mato_loop_backend.agent.session_settings import apply_run_session_settings

    if not apply_run_session_settings(context.session, run):
        context.session.current_directory = result.path
    client.current_directory = result.path
    await context.db.flush()
    return ToolResult.json({"currentDirectory": result.path})


async def _run_process(
    context: ToolContext,
    operation: str,
    arguments: dict[str, Any],
    *,
    requested_arguments: dict[str, Any],
    executed_arguments: dict[str, Any],
    redacted_values: tuple[str, ...] = (),
) -> ToolResult:
    output = {"stdout": "", "stderr": ""}
    published_output = dict(output)
    consumed_output = {"stdout": 0, "stderr": 0}
    client = files_access.workspace_client(context)
    execution = ShellOutput(str(uuid4()), datetime.now(UTC))
    async with terminal_coordinator.capture_shell_output(
        user_id=context.session.user_id,
        session_id=context.session.id,
        agent_id=client.agent_id,
        tool_call_id=context.tool_call.id,
        execution=execution,
    ) as display:
        command = arguments["arguments"]["command"] if operation == "administrator.run" else arguments["command"]
        for secret in sorted(set(redacted_values), key=len, reverse=True):
            if secret:
                command = command.replace(secret, "[redacted]")
        prefix = "sudo " if operation == "administrator.run" else "$ "
        await display(f"\n{prefix}{command}\n")

        async def publish(event: Any) -> None:
            channel = event.event
            output[channel] += event.data
            text = redact_process_output(
                {channel: output[channel][consumed_output[channel]:]}, redacted_values, complete=False,
            )[channel]
            if text:
                consumed_output[channel] = max(output[channel].rfind("\n"), output[channel].rfind("\r")) + 1
                await display(text)
                await run_events.publish(context.run_id, {
                    "type": "process.output", "toolCallId": context.tool_call.id,
                    "stream": channel, "delta": text,
                })
                published_output[channel] += text

        result = None
        try:
            result = await client.request(
                operation, arguments, ProcessResult, timeout=3_610, on_event=publish,
            )
            assert isinstance(result, ProcessResult)
        finally:
            truncated = (result.stdout_truncated or result.stderr_truncated) if result else bool(redacted_values)
            safe_output = redact_process_output(output, redacted_values, truncated=truncated)
            if safe_output != published_output:
                for channel, text in safe_output.items():
                    delta = text[len(published_output[channel]):]
                    if delta:
                        await display(delta)
                        await run_events.publish(context.run_id, {
                            "type": "process.output", "toolCallId": context.tool_call.id,
                            "stream": channel, "delta": delta,
                        })
    tool_result = ToolResult.json(
        {
            **result.model_dump(by_alias=True),
            "requestedArguments": requested_arguments,
            "executedArguments": executed_arguments,
            "userEdited": requested_arguments != executed_arguments,
            "output": {
                **safe_output,
                "truncated": truncated,
            },
        },
        metadata={"shellExecution": {"id": execution.id, "startedAt": execution.started_at.isoformat()}},
    )
    if result.timed_out or result.exit_code != 0:
        tool_result.is_error = True
        tool_result.metadata["errorCode"] = (
            "process.timedOut" if result.timed_out else "process.commandFailed"
        )
    return tool_result


async def _authorize_shell(context: ToolContext, requests, reference=None) -> bool:
    edited_approval = context.approval_resolution.get("argument_edit_approved")
    try:
        if len(requests) > 1:
            for request in requests:
                if await context.ask_handler(request, check_only=True) in {PermissionDecision.DENY, PermissionDecision.USER_DENIED}:
                    return False
        # Approve writing before reading so its included read right avoids a redundant prompt.
        for request in sorted(requests, key=lambda item: item.capability == "shell.read"):
            # The user's edit consent covers the whole revalidated command, not only its first category.
            if edited_approval is not None:
                context.approval_resolution["argument_edit_approved"] = edited_approval
            allowed = (
                await administrator.authorize(context, request, reference)
                if request.resource_type == "execution_administrator"
                else await tools_access.allow(context, request)
            )
            if not allowed:
                return False
        if len(requests) > 1:
            for request in requests:
                if await context.ask_handler(request, check_only=True) != PermissionDecision.ALLOW:
                    return False
        return True
    finally:
        if edited_approval is not None:
            context.approval_resolution.pop("argument_edit_approved", None)


async def _shell_run(arguments: ShellRunArguments, context: ToolContext) -> ToolResult:
    client = files_access.workspace_client(context)
    if client.current_directory is None:
        return ToolResult.error("permission.denied")
    if arguments.elevated:
        if not client.supports("administrator.run") or not client.supports("shell.classify"):
            return ToolResult.error("executionAgent.operationUnavailable")
        shell_arguments = {"command": arguments.command, "currentDirectory": client.current_directory}
        inspection = await client.request("shell.classify", shell_arguments, ShellInspectResult)
        permissions = tuple(administrator_permission(
            client.agent_id, client.current_directory, capability=f"shell.{category}",
            command=arguments.command, inspection=inspection,
            environment=arguments.model_dump(mode="json").get("environment"),
        ) for category in inspection.required_categories)
        if not await _authorize_shell(context, permissions, arguments.sudo_password_ref):
            return ToolResult.error("permission.denied")
        inspection = await administrator.invoke(context, "shell.inspect", shell_arguments, ShellInspectResult, arguments.sudo_password_ref)
        if isinstance(inspection, ToolResult):
            return inspection
        if arguments.environment:
            inspection = inspection.model_copy(update={"analysis_complete": False})
        permissions = tuple(administrator_permission(
            client.agent_id, client.current_directory, capability=f"shell.{category}",
            command=arguments.command, inspection=inspection,
            environment=arguments.model_dump(mode="json").get("environment"),
        ) for category in inspection.required_categories)
        if not await _authorize_shell(context, permissions, arguments.sudo_password_ref):
            return ToolResult.error("permission.denied")
        resolved = await resolve_secret_refs(context, arguments.environment)
        if isinstance(resolved, ToolResult):
            return resolved
        reference = await effective_sudo_reference(
            context.db, context.session, context.run_secret_refs, client.agent_id, arguments.sudo_password_ref,
        )
        requested = arguments.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
        payload = await administrator.request_arguments(context, "shell.run", {
            **shell_arguments, "expectedInspectionRevision": inspection.inspection_revision,
            **({"timeoutSeconds": arguments.timeout} if arguments.timeout is not None else {}),
            **({"environment": resolved} if resolved else {}),
        }, reference)
        if isinstance(payload, ToolResult):
            return payload
        password = payload.get("sudoPassword")
        try:
            return await _run_process(
                context, "administrator.run", payload,
                requested_arguments=requested, executed_arguments=requested,
                redacted_values=(*resolved.values(), *((password,) if password else ())),
            )
        except ExecutionClientError as exc:
            if exc.code in {"process.sudoAuthFailed", "process.sudoAuthRequired"}:
                discard_sudo_reference(context, reference)
            raise
    requested = arguments.model_dump(
        mode="json", exclude_none=True, exclude_defaults=True
    )
    executed = requested
    inspection = await client.request(
        "shell.inspect",
        {"command": executed["command"], "currentDirectory": client.current_directory},
        ShellInspectResult,
    )
    assert isinstance(inspection, ShellInspectResult)
    if arguments.environment:
        inspection = inspection.model_copy(update={"analysis_complete": False})
    permissions = shell_command_permissions(
        client.agent_id, client.current_directory, executed["command"], inspection,
        environment=arguments.model_dump(mode="json").get("environment"),
    )
    if not await _authorize_shell(context, permissions):
        return ToolResult.error("permission.denied")
    resolved = await resolve_secret_refs(context, arguments.environment)
    if isinstance(resolved, ToolResult):
        return resolved
    environment = resolved
    process_arguments = {
        "command": executed["command"],
        "currentDirectory": client.current_directory,
        "timeoutSeconds": arguments.timeout,
        "expectedInspectionRevision": inspection.inspection_revision,
        **({"environment": environment} if environment else {}),
    }
    info = execution_agent_connections.system_info(client.agent_id)
    if info is not None and info.temporary_directory is not None and info.session_temporary_directory_supported:
        from mato_loop_backend.sessions.service import execution_filesystem_policy

        settings = await tools_access.profile_tool_settings(context)
        if settings._temporary_files_enabled:
            agent = await context.db.get(ExecutionAgent, client.agent_id)
            if agent is not None and execution_filesystem_policy(agent, info.temporary_directory) != "deny":
                process_arguments["temporarySessionId"] = str(context.session.id)
    request = RequestMessage(
        id=uuid4(), operation="shell.run", arguments=process_arguments
    )
    if len(request.model_dump_json(by_alias=True).encode()) > MAX_REQUEST_BYTES:
        return ToolResult.error("executionAgent.requestTooLarge")
    return await _run_process(
        context,
        "shell.run",
        process_arguments,
        requested_arguments=requested,
        executed_arguments=executed,
        redacted_values=tuple(environment.values()),
    )


LIST_EXECUTION_AGENTS = ToolDefinition(
            "list_execution_agents",
            "List computers, session selection and enabled profile-visible integrations. Configuration only, not readiness or permission.",
            ListExecutionAgentsArguments, _list_execution_agents,
            group="agent", label="Computer anzeigen", icon="computer",
            actions=(_EXECUTION_AGENTS_LIST,),
        )

SELECT_EXECUTION_AGENT = ToolDefinition(
            "select_execution_agent",
            "Select one listed connected MatoLoop Agent for this session. Call alone. Configured policies and grants determine approval. The next model step uses the selected agent and its directory.",
            SelectExecutionAgentArguments, _select_execution_agent,
            group="agent", label="Computer übernehmen", icon="computer",
            actions=(_EXECUTION_AGENT_SELECT,),
        )

GET_SYSTEM_INFO = ToolDefinition(
            "get_system_info",
            "Get bounded basic information about the selected computer. This does not require a current directory.",
            SystemInfoArguments,
            _get_system_info,
            tool_type="computer",
            required_operation="system.info",
            group="process",
            label="Systeminformationen",
            icon="computer",
            actions=(_SYSTEM_INFO,),
        )

CHANGE_DIRECTORY = ToolDefinition(
            "change_directory",
            "Resolve a directory on the selected computer and make it the persistent current directory. "
            "File and shell tools already use the current directory; call this only to select a different directory. "
            "Use this instead of shell cd; shell cd applies only to that shell call.",
            ChangeDirectoryArguments,
            _change_directory,
            tool_type="computer",
            required_operation="filesystem.resolve",
            group="process",
            label="Verzeichnis wechseln",
            icon="drive_file_move",
            requires_permission_reason=True,
        )

SHELL_RUN = ToolDefinition(
            "shell_run",
             "Run a command in a fresh host-configured shell in the session directory; use PowerShell on Windows. "
             "State and cd do not persist; use change_directory to change the session directory. "
             "Inspection checks required read/write/delete categories before execution. Read means no known "
             "mutation, not absence of side effects; incomplete analysis is separate. This is not an OS sandbox. "
             "Normal consent is computer/category-bound. The user can edit staged commands; results report "
             "requested and executed arguments. Optional timeout: seconds, 0 < timeout <= 3600; null uses the "
             "agent default, capped locally. Nonzero exits/timeouts return errors with bounded output. "
             "elevated=true runs as administrator with the same category checks. Use environment references; "
             "sudo preserves only explicitly named variables. Omit sudo_password_ref to reuse authentication "
             "or request missing administrator input directly. Pass an existing matching reference explicitly; "
             "use secret_input with administrator=true for advance or grouped input. "
             "Passwords authenticate only. Elevated/explicit-environment consent binds exact command/context. "
             "Never put sudo, password pipes, or passwords in the command.",
            ShellRunArguments,
            _shell_run,
            tool_type="computer",
            required_operation="shell.run",
            group="process",
            label="Shell-Befehl ausführen",
            icon="terminal",
             actions=(_SHELL_READ, _SHELL_WRITE, _SHELL_DELETE),
             alternative_action_groups=(frozenset({"shell.read", "shell.write", "shell.delete"}),),
            permission_reason_instruction=(
                "Explain the command's purpose and relevant effects in plain language. Keep it short and do not merely repeat the command."
            ),
            approval_edit_fields=(
                ApprovalEditField(("command",), "Befehl", "textarea", True, 256 * 1024),
            ),
        )
