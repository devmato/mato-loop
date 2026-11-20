from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

import mato_loop_backend.tools.access as tools_access
from mato_loop_backend.automations import (
    AutomationError,
    AutomationService,
    automation_definition,
    runtime_snapshot,
)
from mato_loop_backend.models import Automation, ChatSession
from mato_loop_backend.permissions.policy import (
    PermissionDisplayItem,
    PermissionRequest,
    automation_collection_scope,
    automation_scope,
)
from mato_loop_backend.sessions.archive import active_session_clause, is_session_archived
from mato_loop_backend.tools.actions import (
    _AUTOMATION_CREATE,
    _AUTOMATION_DELETE,
    _AUTOMATION_READ,
    _AUTOMATION_RUN,
    _AUTOMATION_UPDATE,
)
from mato_loop_backend.tools.arguments import (
    AutomationScheduleArguments,
    CreateAutomationArguments,
    DeleteAutomationArguments,
    EditAutomationArguments,
    GetAutomationArguments,
    ListAutomationsArguments,
    RunAutomationArguments,
)
from mato_loop_backend.tools.context import ToolContext as ToolContext
from mato_loop_backend.tools.contracts import ToolDefinition as ToolDefinition
from mato_loop_backend.tools.contracts import ToolResult as ToolResult


def _automation_collection_permission(
    context: ToolContext,
    capability: str,
    *,
    action_kind: str,
    title: str,
    summary: str,
    display_items: tuple[PermissionDisplayItem, ...] = (),
    approval_details: dict[str, Any] | None = None,
    current_session_only: bool = False,
) -> PermissionRequest:
    return PermissionRequest(
        capability=capability,
        resource_type="session_automation_collection" if current_session_only else "automation_collection",
        resource_scope=automation_collection_scope(
            context.session.user_id, context.session.id
        ),
        action_kind=action_kind,
        risk_level="medium",
        title=title,
        summary=summary,
        resource_display="Eigene Automationen",
        display_items=display_items,
        approval_details=approval_details,
    )


def _automation_permission(
    context: ToolContext,
    automation: Automation,
    capability: str,
    *,
    action_kind: str,
    title: str,
    summary: str,
    risk_level: str = "medium",
    display_items: tuple[PermissionDisplayItem, ...] = (),
    approval_details: dict[str, Any] | None = None,
) -> PermissionRequest:
    return PermissionRequest(
        capability=capability,
        resource_type="automation",
        resource_scope=automation_scope(
            automation.id,
            automation.revision,
            context.session.user_id,
            context.session.id,
        ),
        action_kind=action_kind,
        risk_level=risk_level,
        title=title,
        summary=summary,
        resource_display=f"{automation.name} (Revision {automation.revision})",
        display_items=display_items,
        approval_details=approval_details,
    )


def _automation_schedules(
    schedules: list[AutomationScheduleArguments],
) -> list[dict[str, Any]]:
    return [item.model_dump(exclude_none=True) for item in schedules]


def _automation_schedule_json(schedules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            key: value.isoformat() if isinstance(value, datetime) else value
            for key, value in item.items()
        }
        for item in schedules
    ]


def _automation_schedule_lines(schedules: list[dict[str, Any]]) -> tuple[str, ...]:
    return tuple(
        f'{item.get("run_at") or "{} | {}".format(item["cron"], item["timezone"])} | '
        f'{"aktiv" if item["enabled"] else "inaktiv"}'
        f'{" | gültig bis " + item["valid_until"] if item.get("valid_until") else ""}'
        for item in _automation_schedule_json(schedules)
    ) or ("Keine Zeitpläne",)


_TRIGGER_LABELS = {"agent_response": "Agent hat geantwortet", "agent_error": "Agent-Auftrag fehlgeschlagen"}


async def _automation_definition_payload(
    db: AsyncSession, automation: Automation
) -> dict[str, Any]:
    execution_session = await db.get(ChatSession, automation.execution_session_id)
    if execution_session is None:
        raise AutomationError("automation.executionSessionMissing")
    archived = await is_session_archived(db, execution_session)
    return {
        **automation_definition(automation, archived=archived),
        "sessionArchived": archived,
        "runtime": runtime_snapshot(execution_session),
    }


def _automation_tool_error(exc: AutomationError) -> ToolResult:
    return ToolResult.error(exc.code, field=exc.field, params=exc.params)


async def _list_automations(
    arguments: ListAutomationsArguments, context: ToolContext
) -> ToolResult:
    permission = _automation_collection_permission(
        context,
        "automation.read",
        action_kind="list_automations",
        title="Automationen anzeigen?",
        summary="Der Agent möchte die eigenen Automationen kompakt anzeigen.",
        current_session_only=arguments.scope == "session",
    )
    if not await tools_access.allow(context, permission):
        return ToolResult.error("permission.denied")
    rows = list(
        (
            await context.db.scalars(
                select(Automation)
                .join(ChatSession, ChatSession.id == Automation.execution_session_id)
                .options(selectinload(Automation.schedules))
                .where(
                    Automation.user_id == context.session.user_id,
                    *([Automation.execution_session_id == context.session.id] if arguments.scope == "session" else []),
                    active_session_clause(),
                )
                .order_by(Automation.updated_at.desc(), Automation.id.desc())
                .limit(arguments.limit + 1)
            )
        ).all()
    )
    items = rows[: arguments.limit]
    return ToolResult.json(
        {
            "automations": [
                {
                    "id": str(item.id),
                    "executionSessionId": str(item.execution_session_id),
                    "name": item.name,
                    "modelId": str(item.model_id) if item.model_id else None,
                    "enabled": item.enabled,
                    "triggers": item.triggers or [],
                    "revision": item.revision,
                    "scheduleCount": len(item.schedules),
                    "nextRunAt": (
                        min(
                            schedule.next_run_at
                            for schedule in item.schedules
                            if schedule.enabled
                        ).isoformat()
                        if any(schedule.enabled for schedule in item.schedules)
                        else None
                    ),
                }
                for item in items
            ],
            "limit": arguments.limit,
            "hasMore": len(rows) > arguments.limit,
            "contentIsUntrusted": True,
        }
    )


async def _get_automation(
    arguments: GetAutomationArguments, context: ToolContext
) -> ToolResult:
    try:
        automation = await AutomationService.owned(
            context.db, arguments.automation_id, context.session.user_id
        )
        expected_revision = automation.revision
        if not await tools_access.allow(
            context,
            _automation_permission(
                context,
                automation,
                "automation.read",
                action_kind="get_automation",
                title="Automation anzeigen?",
                summary=(
                    f'Der Agent möchte „{automation.name}“ in Revision '
                    f"{automation.revision} vollständig anzeigen."
                ),
            ),
        ):
            return ToolResult.error("permission.denied")
        automation = await AutomationService.owned(
            context.db, arguments.automation_id, context.session.user_id
        )
        if automation.revision != expected_revision:
            raise AutomationError(
                "automation.changed", params={"currentRevision": automation.revision}
            )
        return ToolResult.json(await _automation_definition_payload(context.db, automation))
    except AutomationError as exc:
        return _automation_tool_error(exc)


async def _create_automation(
    arguments: CreateAutomationArguments, context: ToolContext
) -> ToolResult:
    schedules = _automation_schedules(arguments.schedules)
    try:
        preview = {
            "name": arguments.name,
            "task": arguments.task,
            "modelId": str(arguments.model_id) if arguments.model_id else None,
            "enabled": arguments.enabled,
            "triggers": arguments.triggers,
            "schedules": _automation_schedule_json(schedules),
            "sessionId": str(context.session.id),
        }
        permission = _automation_collection_permission(
            context,
            "automation.create",
            action_kind="create_automation",
            title="Automation erstellen?",
            summary=(
                f'Der Agent möchte die Automation „{arguments.name}“ erstellen und '
                f'{"aktivieren" if arguments.enabled else "deaktiviert speichern"}.'
            ),
            display_items=(
                PermissionDisplayItem("Name", arguments.name),
                PermissionDisplayItem("Status", "Wird aktiviert." if arguments.enabled else "Wird deaktiviert gespeichert."),
                PermissionDisplayItem("Aufgabe", arguments.task),
                PermissionDisplayItem("Modell", str(arguments.model_id) if arguments.model_id else "Aktuelles Sessionmodell"),
                PermissionDisplayItem(
                    "Zeitpläne", _automation_schedule_lines(schedules), "list"
                ),
                PermissionDisplayItem(
                    "Auslöser", tuple(_TRIGGER_LABELS[item] for item in arguments.triggers) or ("Keine Ereignisse",), "list",
                ),
                PermissionDisplayItem(
                    "Ausführung", "Aktuelle Session",
                ),
            ),
            approval_details={"automationPreview": preview},
        )
        if not await tools_access.allow(context, permission):
            return ToolResult.error("permission.denied")
        automation = await AutomationService.create(
            context.db,
            user_id=context.session.user_id,
            session_id=context.session.id,
            name=arguments.name,
            task=arguments.task,
            enabled=arguments.enabled,
            schedules=schedules,
            triggers=arguments.triggers,
            model_id=arguments.model_id,
        )
        await context.db.commit()
        return ToolResult.json(
            {
                "id": str(automation.id),
                "name": automation.name,
                "revision": automation.revision,
                "enabled": automation.enabled,
                "created": True,
            }
        )
    except AutomationError as exc:
        await context.db.rollback()
        return _automation_tool_error(exc)


def _automation_edit_diff(
    automation: Automation,
    arguments: EditAutomationArguments,
    schedules: list[dict[str, Any]],
) -> tuple[str, ...]:
    before_schedules = [
        {
            **(
                {"run_at": item.next_run_at.isoformat()}
                if item.cron_expression is None
                else {
                    "cron": item.cron_expression,
                    "timezone": item.timezone,
                    **(
                        {"valid_until": item.valid_until.isoformat()}
                        if item.valid_until
                        else {}
                    ),
                }
            ),
            "enabled": item.enabled,
        }
        for item in automation.schedules
    ]
    changes = []
    comparisons = (
        ("Name", automation.name, arguments.name),
        ("Aufgabe", automation.task, arguments.task),
        ("Modell", str(automation.model_id) if automation.model_id else "Aktuelles Sessionmodell",
         str(arguments.model_id) if arguments.model_id else "Aktuelles Sessionmodell"),
        ("Aktiv", str(automation.enabled), str(arguments.enabled)),
        (
            "Auslöser",
            ", ".join(_TRIGGER_LABELS[item] for item in automation.triggers or []) or "Keine Ereignisse",
            ", ".join(_TRIGGER_LABELS[item] for item in arguments.triggers) or "Keine Ereignisse",
        ),
        (
            "Zeitpläne",
            json.dumps(before_schedules) if before_schedules else "Keine Zeitpläne",
            json.dumps(_automation_schedule_json(schedules)) if schedules else "Keine Zeitpläne",
        ),
    )
    for label, before, after in comparisons:
        if before != after:
            changes.append(f"{label}: {before} -> {after}")
    return tuple(changes or ("Keine inhaltliche Änderung",))


async def _edit_automation(
    arguments: EditAutomationArguments, context: ToolContext
) -> ToolResult:
    schedules = _automation_schedules(arguments.schedules)
    try:
        automation = await AutomationService.owned(
            context.db, arguments.automation_id, context.session.user_id
        )
        if automation.revision != arguments.expected_revision:
            raise AutomationError(
                "automation.changed", params={"currentRevision": automation.revision}
            )
        changes = _automation_edit_diff(
            automation, arguments, schedules
        )
        if not await tools_access.allow(
            context,
            _automation_permission(
                context,
                automation,
                "automation.update",
                action_kind="edit_automation",
                title="Automation ändern?",
                summary=(
                    f'Der Agent möchte „{automation.name}“ in Revision '
                    f"{automation.revision} ändern."
                ),
                display_items=(PermissionDisplayItem("Änderungen", changes, "list"),),
                approval_details={
                    "automationChange": {
                        "automationId": str(automation.id),
                        "expectedRevision": automation.revision,
                        "changes": list(changes),
                    }
                },
            ),
        ):
            return ToolResult.error("permission.denied")
        automation = await AutomationService.owned(
            context.db,
            arguments.automation_id,
            context.session.user_id,
            for_update=True,
        )
        automation = await AutomationService.replace(
            context.db,
            automation,
            expected_revision=arguments.expected_revision,
            name=arguments.name,
            task=arguments.task,
            enabled=arguments.enabled,
            schedules=schedules,
            triggers=arguments.triggers,
            model_id=arguments.model_id,
        )
        await context.db.commit()
        return ToolResult.json(
            {
                "id": str(automation.id),
                "name": automation.name,
                "revision": automation.revision,
                "enabled": automation.enabled,
                "updated": True,
            }
        )
    except AutomationError as exc:
        await context.db.rollback()
        return _automation_tool_error(exc)


async def _run_automation(
    arguments: RunAutomationArguments, context: ToolContext
) -> ToolResult:
    try:
        automation = await AutomationService.owned(
            context.db, arguments.automation_id, context.session.user_id
        )
        if automation.revision != arguments.expected_revision:
            raise AutomationError(
                "automation.changed", params={"currentRevision": automation.revision}
            )
        if not await tools_access.allow(
            context,
            _automation_permission(
                context,
                automation,
                "automation.run",
                action_kind="run_automation",
                title="Automation jetzt starten?",
                summary=(
                    f'Der Agent möchte „{automation.name}“ in Revision '
                    f"{automation.revision} manuell einreihen."
                ),
                display_items=(
                    PermissionDisplayItem("Auswirkung", "Ein Lauf wird eingereiht."),
                ),
            ),
        ):
            return ToolResult.error("permission.denied")
        run = await AutomationService.enqueue_manual(
            context.db,
            automation.id,
            context.session.user_id,
            expected_revision=arguments.expected_revision,
        )
        return ToolResult.json(
            {
                "automationId": str(automation.id),
                "revision": run.automation_revision,
                "runId": str(run.id),
                "status": run.status,
                "queued": True,
            }
        )
    except AutomationError as exc:
        await context.db.rollback()
        return _automation_tool_error(exc)


async def _delete_automation(
    arguments: DeleteAutomationArguments, context: ToolContext
) -> ToolResult:
    try:
        automation = await AutomationService.owned(
            context.db, arguments.automation_id, context.session.user_id
        )
        if automation.revision != arguments.expected_revision:
            raise AutomationError(
                "automation.changed", params={"currentRevision": automation.revision}
            )
        if not await tools_access.allow(
            context,
            _automation_permission(
                context,
                automation,
                "automation.delete",
                action_kind="delete_automation",
                title="Automation endgültig löschen?",
                summary=(
                    f'Der Agent möchte „{automation.name}“ in Revision '
                    f"{automation.revision} samt Laufhistorie löschen."
                ),
                risk_level="high",
                display_items=(
                    PermissionDisplayItem(
                        "Auswirkung",
                        "Auftrag, Zeitpläne und Laufhistorie werden gelöscht. Die Session bleibt erhalten.",
                    ),
                ),
            ),
        ):
            return ToolResult.error("permission.denied")
        await AutomationService.delete(
            context.db,
            automation.id,
            context.session.user_id,
            expected_revision=arguments.expected_revision,
        )
        return ToolResult.json(
            {
                "id": str(arguments.automation_id),
                "revision": arguments.expected_revision,
                "deleted": True,
            }
        )
    except AutomationError as exc:
        await context.db.rollback()
        return _automation_tool_error(exc)


LIST_AUTOMATIONS = ToolDefinition(
            "list_automations",
            "List automations in the current session. Use scope=all only for an owner-wide list across sessions.",
            ListAutomationsArguments,
            _list_automations,
            group="automations",
            label="Automationen anzeigen",
            icon="view_list",
            actions=(_AUTOMATION_READ,),
        )

GET_AUTOMATION = ToolDefinition(
            "get_automation",
            "Get by ID.",
            GetAutomationArguments,
            _get_automation,
            group="automations",
            label="Automation anzeigen",
            icon="schedule",
            actions=(_AUTOMATION_READ,),
        )

CREATE_AUTOMATION = ToolDefinition(
            "create_automation",
             "Create an automation in the current session if asked. Reuse an existing automation for the same task. Calendar event reminders use the calendar event tools. Use cron+timezone or offset run_at; valid_until is cron-only.",
            CreateAutomationArguments,
            _create_automation,
            group="automations",
            label="Automation erstellen",
            icon="add_task",
             actions=(_AUTOMATION_CREATE,),
             prompt_fragments=(
                 "Offer supported automation for later checks, continuation or recurring work, subject to "
                 "the user's preferences. State timing and scope briefly.",
                 "Automations run in their session with its environment and permission grants. "
                 "Triggers agent_response and agent_error observe the owner's ordinary agent runs; "
                 "automation-origin work never emits these triggers. Put optional event conditions in task. "
                 "The triggered agent checks those conditions and may take no action. Name skills in task; "
                 "load them with load_skill. Use "
                 "request_session_permissions for known required or recommended access before enabling work; "
                 "never execute a mutation just to obtain permission. Wait for the result. Correct invalid target names "
                 "or arguments. If preparation is unavailable, keep the automation disabled until question confirms "
                 "the user accepts possible approval waits during execution. Do not enable work that depends on "
                 "denied access. Archived sessions and projects do not run automations.",
             ),
        )

EDIT_AUTOMATION = ToolDefinition(
            "edit_automation",
            "Edit exact revision. Use cron+timezone or offset run_at; valid_until is cron-only.",
            EditAutomationArguments,
            _edit_automation,
            group="automations",
            label="Automation ändern",
            icon="edit_calendar",
            actions=(_AUTOMATION_UPDATE,),
        )

RUN_AUTOMATION = ToolDefinition(
            "run_automation",
            "Run an exact revision only if latest asks.",
            RunAutomationArguments,
            _run_automation,
            group="automations",
            label="Automation starten",
            icon="play_arrow",
            actions=(_AUTOMATION_RUN,),
        )

DELETE_AUTOMATION = ToolDefinition(
            "delete_automation",
            "Delete with history only if latest asks; exact revision.",
            DeleteAutomationArguments,
            _delete_automation,
            group="automations",
            label="Automation löschen",
            icon="delete_forever",
            actions=(_AUTOMATION_DELETE,),
        )
