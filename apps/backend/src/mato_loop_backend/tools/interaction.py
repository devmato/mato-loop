from __future__ import annotations

from uuid import UUID, uuid4

from sqlalchemy import select

import mato_loop_backend.permissions.resolution as permissions_resolution
import mato_loop_backend.tools.access as tools_access
from mato_loop_backend.agent.session_secrets import session_secrets
from mato_loop_backend.agent.session_settings import apply_run_session_settings
from mato_loop_backend.agent.subagents import permission_origin
from mato_loop_backend.models import AgentRun, ExecutionAgent
from mato_loop_backend.notifications import NotificationDraft, NotificationService
from mato_loop_backend.permissions.policy import personal_variables_permission
from mato_loop_backend.personal_variables import (
    get_personal_variable,
    get_personal_variable_by_name,
    list_personal_variables,
    personal_variable_payload,
)
from mato_loop_backend.settings import ToolExecutionPolicy
from mato_loop_backend.tools.actions import _SECRETS_ACCESS, _SECRETS_WRITE
from mato_loop_backend.tools.arguments import (
    AdministratorQuestionTarget,
    NotifyArguments,
    ProtectedQuestionArguments,
    ProtectedQuestionItem,
    ProtectedQuestionResolution,
    QuestionArguments,
    SecretInputArguments,
    TaskArguments,
    TodoWriteArguments,
    ToolArguments,
    VariableReadArguments,
    VariableWriteArguments,
)
from mato_loop_backend.tools.context import ToolContext as ToolContext
from mato_loop_backend.tools.contracts import ToolDefinition as ToolDefinition
from mato_loop_backend.tools.contracts import ToolResult as ToolResult

SECRET_INPUT_USAGE = (
    "Continue the requested work in this run where the available inputs are sufficient; "
    "do not finish with an acknowledgement. "
    "Inputs listed in unanswered were skipped by the user; do not assume their values or "
    "repeat those questions unless requested. Explain work blocked by missing inputs. "
    "Copy references exactly. run-secret references are reusable until this run ends; "
    "a final answer ends the run. session-secret references last until removed or the backend restarts."
)


async def _question(arguments: QuestionArguments, context: ToolContext) -> ToolResult:
    answers = await context.ask_user(arguments)
    if answers is None:
        return ToolResult.error("question.expired")
    if answers == "rejected":
        return ToolResult.error("question.rejected")
    return ToolResult.json(
        {
            "answers": [
                {
                    "questionId": question.id,
                    "question": question.question,
                    "answers": answers[question.id],
                    "answered": bool(answers[question.id]),
                }
                for question in arguments.questions
            ]
        }
    )


async def _variable_read(
    arguments: VariableReadArguments, context: ToolContext
) -> ToolResult:
    if not await tools_access.allow(
        context, personal_variables_permission(context.session.user_id)
    ):
        return ToolResult.error("permission.denied")
    if arguments.name is not None:
        variable = await get_personal_variable_by_name(
            context.db, context.session.user_id, arguments.name
        )
        if variable is None:
            return ToolResult.error("personalVariable.notFound")
        variables = [variable]
    else:
        variables = await list_personal_variables(context.db, context.session.user_id)
    return ToolResult.json(
        {
            "variables": [
                {
                    **personal_variable_payload(variable),
                    "valueRef": str(variable.id),
                }
                for variable in variables
            ]
        }
    )


async def collect_secret_input(
    arguments: SecretInputArguments, context: ToolContext
) -> ToolResult:
    # Reuse run-local answers across direct-message continuations without extending their lifetime.
    known = {
        **{name: ref for name, ref in context.run_secret_refs.items()
           if context.one_time_secrets.get(UUID(ref.removeprefix("run-secret:"))) is not None},
        **session_secrets.references(context.session.id),
    }
    missing = [item for item in arguments.inputs if item.refresh or item.name not in known]
    if not missing:
        return ToolResult.json({
            "environment": {item.name: known[item.name] for item in arguments.inputs},
            "unanswered": [],
            "instruction": SECRET_INPUT_USAGE,
        })
    administrator = None
    if any(item.administrator for item in missing):
        client = context.execution_client
        if client is None or client.agent_id != context.session.preferred_execution_agent_id:
            return ToolResult.error("permission.denied")
        agent = await context.db.scalar(select(ExecutionAgent).where(
            ExecutionAgent.id == client.agent_id,
            ExecutionAgent.user_id == context.session.user_id,
            ExecutionAgent.revoked_at.is_(None),
        ))
        if agent is None:
            return ToolResult.error("permission.denied")
        administrator = AdministratorQuestionTarget(agent_id=agent.id, alias=agent.display_name)
    answers = await context.ask_user(
        ProtectedQuestionArguments(
            questions=[
                ProtectedQuestionItem(
                    id=item.name,
                    header=item.label,
                    question=item.question,
                    type="password" if item.masked else "value",
                    one_time=True,
                    administrator=administrator if item.administrator else None,
                )
                for item in missing
            ]
        )
    )
    if answers is None:
        return ToolResult.error("question.expired")
    if answers == "rejected":
        return ToolResult.error("question.rejected")
    assert isinstance(answers, ProtectedQuestionResolution)
    provided = [item for item in missing if answers.answers[item.name]]
    if administrator is not None and any(item.administrator for item in provided):
        await context.db.refresh(context.session)
        run = await context.db.get(AgentRun, context.run_id)
        if run is None:
            return ToolResult.error("permission.denied")
        apply_run_session_settings(context.session, run)
        if context.session.preferred_execution_agent_id != administrator.agent_id:
            return ToolResult.error("permission.denied")
    for item in missing:
        if item.refresh:
            reference = known.pop(item.name, None)
            if reference is not None:
                session_secrets.discard(context.session.id, item.name, reference)
            run_reference = context.run_secret_refs.pop(item.name, None)
            if run_reference is not None:
                context.one_time_secrets.pop(UUID(run_reference.removeprefix("run-secret:")), None)
    remembered = {
        item.name: answers.answers[item.name][0]
        for item in provided
        if item.name in answers.remember_for_session
    }
    session_environment = session_secrets.put(
        context.session.id,
        remembered,
        {
            item.name: item.description or f"Administrator password for {administrator.alias}"
            for item in provided
            if (item.description or item.administrator) and item.name in answers.remember_for_session
        },
    )
    environment = {item.name: known[item.name] for item in arguments.inputs if item.name in known}
    for item in provided:
        if item.name in session_environment:
            environment[item.name] = session_environment[item.name]
        else:
            secret_id = uuid4()
            context.one_time_secrets[secret_id] = answers.answers[item.name][0]
            environment[item.name] = f"run-secret:{secret_id}"
            context.run_secret_refs[item.name] = environment[item.name]
    return ToolResult.json({
        "environment": environment,
        "unanswered": [item.name for item in missing if not answers.answers[item.name]],
        "instruction": SECRET_INPUT_USAGE,
    })


async def _variable_write(
    arguments: VariableWriteArguments, context: ToolContext
) -> ToolResult:
    context.tool_settings = await tools_access.profile_tool_settings(context)
    if permissions_resolution.tool_execution_policy(
        context.tool_settings, "secrets.write", ToolExecutionPolicy.ASK
    ) == ToolExecutionPolicy.DISABLED or not context.tool_settings.tool_availability.get(
        "variable_write", True
    ):
        return ToolResult.error("tool.unavailable")
    action = "update" if arguments.variable_id is not None else "create"
    if arguments.variable_id is not None:
        variable = await get_personal_variable(
            context.db, context.session.user_id, arguments.variable_id
        )
        if variable is None:
            return ToolResult.error("personalVariable.notFound")
    questions = ProtectedQuestionArguments(
        questions=[
            ProtectedQuestionItem(
                id="value",
                header="Secret" if arguments.secret else "Variable",
                question=f"Welcher Wert soll als {arguments.name} gespeichert werden?",
                type="password" if arguments.secret else "value",
                save_proposal={
                    "name": arguments.name,
                    "description": arguments.description,
                    "secret": arguments.secret,
                    "action": action,
                    "variable_id": arguments.variable_id,
                    "agent_profile_id": context.agent_profile_id,
                    "trust_level": context.model_trust_level,
                },
            )
        ]
    )
    answers = await context.ask_user(questions)
    if answers is None:
        return ToolResult.error("question.expired")
    if answers == "rejected":
        return ToolResult.error("question.rejected")
    if not answers["value"]:
        return ToolResult.json({"saved": False, "name": arguments.name, "unanswered": ["value"]})
    saved = await get_personal_variable_by_name(
        context.db, context.session.user_id, arguments.name
    )
    if saved is None:
        return ToolResult.error("personalVariable.notFound")
    return ToolResult.json(
        {
            "saved": True,
            "name": arguments.name,
            "description": arguments.description,
            "secret": arguments.secret,
            "valueRef": str(saved.id),
        }
    )


async def _task(arguments: TaskArguments, context: ToolContext) -> ToolResult:
    if context.task_handler is None:
        return ToolResult.error("tool.unavailable")
    return await context.task_handler(arguments)


async def _todo_write(arguments: TodoWriteArguments, _context: ToolContext) -> ToolResult:
    return ToolResult.json(arguments.model_dump(mode="json"))


async def _notify(arguments: NotifyArguments, context: ToolContext) -> ToolResult:
    session = await permission_origin(context.db, context.session)
    notification_id = await NotificationService.create(context.db, NotificationDraft(
        user_id=session.user_id, session_id=session.id, kind="agent.notice",
        title=arguments.title, body=arguments.body,
        deduplication_key=f"tool:{context.run_id}:{context.tool_call.id}",
    ))
    await context.db.commit()
    if notification_id is not None:
        await NotificationService.deliver_after_commit(notification_id)
    return ToolResult.json(
        {"created": notification_id is not None}, metadata={"suppressResponseNotification": True},
    )


async def _silence(_arguments: ToolArguments, _context: ToolContext) -> ToolResult:
    return ToolResult.json(
        {"silenced": True}, metadata={"suppressResponseNotification": True},
    )


NOTIFY = ToolDefinition(
    "notify",
    "Create a persistent notification for the current user, linked to this session. "
    "Use for requested alerts or important findings, including during automations. "
    "A successful call replaces this run's automatic completion notification. "
    "Use a short plain-text title and body in the user's language.",
    NotifyArguments, _notify, group="agent", label="Benachrichtigung erstellen", icon="notifications",
)


SILENCE = ToolDefinition(
    "silence",
    "Suppress this run's automatic completion notification, but keep the final response in the session. "
    "Use for automation checks with no reportable change, or when the user asks for a quiet response. "
    "Do not silence a normal chat response merely because it is short. Still provide a final response. "
    "Errors, approvals, questions, and explicit notify notices are unaffected.",
    ToolArguments, _silence, group="agent", label="Abschluss ohne Benachrichtigung", icon="notifications_off",
)


QUESTION = ToolDefinition(
            "question",
            "Ask 1-5 questions; call alone. Copy every requested type exactly. Examples are not "
            "options. allow_custom replaces Other; default_answers are fallbacks. "
            "Never use for commands, output, or permissions.",
            QuestionArguments,
            _question,
            group="agent",
            label="Rückfrage stellen",
            icon="help_outline",
        )

TASK = ToolDefinition(
            "task",
            "Join scoped self-contained tasks with bounded results; avoid write conflicts; 8/run; no recursion.",
            TaskArguments,
            _task,
            group="agent",
            label="Aufgaben delegieren",
            icon="account_tree",
        )

TODO_WRITE = ToolDefinition(
            "todo_write",
            "Replace the current session checklist with the complete updated list. Each item has content "
            "and status: pending, in_progress, completed, or cancelled. Omitted items are removed. An "
            "empty list clears the checklist. Use at most one in_progress item. This updates tracking "
            "only; it does not perform or verify the listed work.",
            TodoWriteArguments,
            _todo_write,
            group="agent",
            label="Aufgaben aktualisieren",
            icon="checklist",
        )

VARIABLE_READ = ToolDefinition(
            "variable_read",
            "List personal variables or read one by name. Secret values are never returned; use valueRef "
            "with shell_run.environment to provide a value directly to an isolated process.",
            VariableReadArguments,
            _variable_read,
            group="agent",
            label="Variable lesen",
            icon="key",
            actions=(_SECRETS_ACCESS,),
            releases_private_data=True,
        )

VARIABLE_WRITE = ToolDefinition(
            "variable_write",
            "Ask for and durably save one personal variable or secret only when the user explicitly requests "
            "storage; otherwise use secret_input. Never put the value in tool arguments. Include variable_id "
            "only when updating an exact existing variable. Add description when its purpose is known.",
            VariableWriteArguments,
            _variable_write,
            group="agent",
            label="Variable speichern",
            icon="password",
            actions=(_SECRETS_WRITE,),
        )

SECRET_INPUT = ToolDefinition(
            "secret_input",
            "Collect usernames and secrets; return references, never values. "
            "Reuse references by default. After rejected authentication, set refresh=true on affected fields; keep their names. "
            "Submitting refresh invalidates old references, including skipped fields; cancellation keeps them. "
            "References last for this run unless remembered for this session. "
            "Call alone; continue work before answering. Pass references to protected text parts, "
            "shell_run.environment, or customize_matoloop. Use masked=false for visible input only. "
            "For selected-computer administrator input, set administrator=true, masked=true; "
            "pass the reference as sudo_password_ref. Elevated tools request missing passwords directly. "
            "Input grants no action rights. "
            "Durable storage via customize_matoloop requires an explicit user request.",
            SecretInputArguments,
            collect_secret_input,
            group="agent",
            label="Secret einmalig abfragen",
            icon="password",
        )
