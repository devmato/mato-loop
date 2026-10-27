"""Collect invalidations for ORM and bulk writes in the committing transaction."""

from __future__ import annotations

from collections import defaultdict
from functools import cache
from uuid import UUID

from sqlalchemy import event, inspect, literal, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import ORMExecuteState, Session

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
        Message,
        Notification,
        PermissionGrant,
        Project,
        QuestionRequest,
        SettingRecord,
    )

    return {
        ChatSession: ("sessions", ChatSession, "id"),
        Message: ("messages", ChatSession, "session_id"),
        ContextSummary: ("messages", ChatSession, "session_id"),
        AgentRun: ("sessions", ChatSession, "session_id"),
        ApprovalRequest: ("approvals", ApprovalRequest, "id"),
        QuestionRequest: ("questions", ChatSession, "session_id"),
        PermissionGrant: ("permissions", ChatSession, "session_id"),
        Notification: ("notifications", Notification, "id"),
        Project: ("projects", Project, "id"),
        Automation: ("automations", Automation, "id"),
        AutomationRun: ("automations", Automation, "automation_id"),
        AutomationSchedule: ("automations", Automation, "automation_id"),
        SettingRecord: ("all", SettingRecord, "id"),
    }


def record_change(session: Session | AsyncSession, user_id: UUID | None, resource: str, session_id: UUID | None = None) -> None:
    pending = session.info.setdefault(_PENDING, defaultdict(set))
    pending[user_id].add((resource, session_id))


@event.listens_for(ChangeTrackingSession, "before_flush")
def _collect_changes(session: Session, _context, _instances) -> None:
    from mato_loop_backend.models import AgentRun, ChatSession, Message

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
        if owner is type(item):
            if item in session.deleted and resource in {"sessions", "projects"}:
                resource = "all"
            record_change(session, item.user_id, resource, item.id if owner is ChatSession else None)
        else:
            related[(resource, owner)].add(getattr(item, key))
    for (resource, owner), ids in related.items():
        for item in session.new:
            if isinstance(item, owner) and item.id in ids:
                record_change(session, item.user_id, resource, item.id if owner is ChatSession else None)
                ids.discard(item.id)
        if not ids:
            continue
        for owner_id, user_id in session.execute(select(owner.id, owner.user_id).where(owner.id.in_(ids))):
            record_change(session, user_id, resource, owner_id if owner is ChatSession else None)


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
    if state.is_delete and resource in {"sessions", "projects"}:
        resource = "all"
    statement = select(owner.user_id, owner.id if owner is ChatSession else literal(None))
    if owner is not model:
        statement = statement.join(model, owner.id == getattr(model, key))
    if state.statement.whereclause is not None:
        statement = statement.where(state.statement.whereclause)
    if model is Message:
        statement = statement.where(Message.role != "tool")
    for user_id, session_id in state.session.execute(statement.distinct()):
        record_change(state.session, user_id, resource, session_id)


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
