"""Collect invalidations for ORM and bulk writes in the committing transaction."""

from __future__ import annotations

from collections import defaultdict
from functools import cache
from uuid import UUID

from sqlalchemy import event, inspect, literal, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import ORMExecuteState, Session
from sqlalchemy.sql.elements import BindParameter, Null

from mato_loop_backend.change_events import change_events

_PENDING = "change_events"


class ChangeTrackingSession(Session):
    pass


@cache
def _sources():
    # Imported lazily because model registration also imports database services.
    from mato_loop_backend.models import (
        AgentRun,
        ApprovalRequest,
        Automation,
        AutomationRun,
        AutomationSchedule,
        ChatSession,
        ContextSummary,
        MemoryRecord,
        MemorySourceMessage,
        Message,
        Notification,
        PermissionGrant,
        Project,
        QuestionRequest,
        QueuedMessage,
        SettingRecord,
    )

    return {
        ChatSession: ("sessions", ChatSession, "id"),
        Message: ("messages", ChatSession, "session_id"),
        ContextSummary: ("messages", ChatSession, "session_id"),
        AgentRun: ("sessions", ChatSession, "session_id"),
        QueuedMessage: ("message_queue", ChatSession, "session_id"),
        ApprovalRequest: ("approvals", ApprovalRequest, "id"),
        QuestionRequest: ("questions", ChatSession, "session_id"),
        PermissionGrant: ("permissions", ChatSession, "session_id"),
        Notification: ("notifications", Notification, "id"),
        MemoryRecord: ("approvals", MemoryRecord, "id"),
        MemorySourceMessage: ("approvals", MemoryRecord, "memory_id"),
        Project: ("projects", Project, "id"),
        Automation: ("automations", Automation, "id"),
        AutomationRun: ("automations", Automation, "automation_id"),
        AutomationSchedule: ("automations", Automation, "automation_id"),
        SettingRecord: ("all", SettingRecord, "id"),
    }


def record_change(session: Session | AsyncSession, user_id: UUID | None, resource: str, session_id: UUID | None = None) -> None:
    pending = session.info.setdefault(_PENDING, defaultdict(set))
    pending[user_id].add((resource, session_id))


def _record_project(session: Session, user_id: UUID, project_id: UUID | None) -> None:
    if project_id is not None:
        record_change(session, user_id, "project_summaries", project_id)


@event.listens_for(ChangeTrackingSession, "before_flush")
def _collect_changes(session: Session, _context, _instances) -> None:
    from mato_loop_backend.models import AgentRun, ChatSession, Message, SettingRecord

    related = defaultdict(set)
    for item in set(session.new) | set(session.dirty) | set(session.deleted):
        source = _sources().get(type(item))
        if source is None or (item in session.dirty and not session.is_modified(item, include_collections=False)):
            continue
        if isinstance(item, AgentRun) and item in session.dirty and not inspect(item).attrs.status.history.has_changes():
            continue
        if isinstance(item, Message) and item.role == "tool":
            continue
        resource, owner, key = source
        if isinstance(item, SettingRecord) and item.key == "user-preferences" and item in session.dirty:
            previous = inspect(item).attrs.data.history.deleted
            if previous:
                fields = {"session_tabs", "control_center_open"}
                if {k: v for k, v in previous[0].items() if k not in fields} == {
                    k: v for k, v in item.data.items() if k not in fields
                }:
                    resource = "workspace"
        if owner is type(item):
            if item in session.deleted and resource in {"sessions", "projects"}:
                resource = "all"
            record_change(session, item.user_id, resource, item.id if owner is ChatSession else None)
            if owner is ChatSession and item not in session.deleted:
                _record_project(session, item.user_id, item.project_id)
                history = inspect(item).attrs.project_id.history
                if history.has_changes() and item not in session.new:
                    # Assignment to an unloaded attribute does not retain its old value.
                    old_project_id = history.deleted[0] if history.deleted else session.scalar(
                        select(ChatSession.project_id).where(
                            ChatSession.id == item.id, ChatSession.user_id == item.user_id,
                        )
                    )
                    _record_project(session, item.user_id, old_project_id)
        else:
            related[(resource, owner)].add(getattr(item, key))
    for (resource, owner), ids in related.items():
        for item in session.new:
            if isinstance(item, owner) and item.id in ids:
                record_change(session, item.user_id, resource, item.id if owner is ChatSession else None)
                if owner is ChatSession:
                    _record_project(session, item.user_id, item.project_id)
                ids.discard(item.id)
        if not ids:
            continue
        statement = select(owner.id, owner.user_id, owner.project_id if owner is ChatSession else literal(None))
        for owner_id, user_id, project_id in session.execute(statement.where(owner.id.in_(ids))):
            record_change(session, user_id, resource, owner_id if owner is ChatSession else None)
            if owner is ChatSession:
                _record_project(session, user_id, project_id)


@event.listens_for(ChangeTrackingSession, "do_orm_execute")
def _collect_bulk_changes(state: ORMExecuteState) -> None:
    if not (state.is_update or state.is_delete) or state.bind_mapper is None:
        return
    model = state.bind_mapper.class_
    source = _sources().get(model)
    if source is None:
        return
    from mato_loop_backend.models import ChatSession, Message

    resource, owner, key = source
    if model is ChatSession and state.is_update and state.execution_options.get("matoloop_session_read"):
        resource = "session_read"
    if state.is_delete and resource in {"sessions", "projects"}:
        resource = "all"
    statement = select(
        owner.user_id, owner.id if owner is ChatSession else literal(None),
        owner.project_id if owner is ChatSession else literal(None),
    )
    if owner is not model:
        statement = statement.join(model, owner.id == getattr(model, key))
    if state.statement.whereclause is not None:
        statement = statement.where(state.statement.whereclause)
    if model is Message:
        statement = statement.where(Message.role != "tool")
    parameters = state.parameters if state.is_executemany else [state.parameters or {}]
    for params in parameters:
        affected = statement
        # Bulk-by-primary-key writes carry their predicates in the parameter list.
        if state.is_executemany and "id" in params:
            affected = affected.where(model.id == params["id"])
        destination = None
        unknown_destination = False
        if model is ChatSession and state.is_update:
            values = dict(state.statement._values or state.statement._ordered_values or ())
            for column, value in values.items():
                if getattr(column, "key", column) != "project_id":
                    continue
                if isinstance(value, BindParameter) and value.callable is None:
                    if value.key in params:
                        destination = params[value.key]
                    elif not value.required:
                        destination = value.value
                    else:
                        unknown_destination = True
                elif not isinstance(value, Null):
                    unknown_destination = True
            if "project_id" in params:
                destination = params["project_id"]
            if destination is not None and not isinstance(destination, UUID):
                unknown_destination = True
        for user_id, session_id, project_id in state.session.execute(affected.distinct(), params):
            record_change(state.session, user_id, resource, session_id)
            if owner is ChatSession and resource != "all":
                _record_project(state.session, user_id, project_id)
                if unknown_destination:
                    record_change(state.session, user_id, "projects")
                else:
                    _record_project(state.session, user_id, destination)


@event.listens_for(ChangeTrackingSession, "after_commit")
def _publish_changes(session: Session) -> None:
    if session.in_nested_transaction():
        return
    for user_id, changes in session.info.pop(_PENDING, {}).items():
        change_events.publish(user_id, changes)


@event.listens_for(ChangeTrackingSession, "after_rollback")
def _discard_changes(session: Session) -> None:
    if not session.in_nested_transaction():
        session.info.pop(_PENDING, None)
