from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend.models import (
    Automation,
    AutomationRun,
    AutomationSchedule,
    ChatSession,
    Project,
)


class ArchiveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    archived: bool


def active_session_clause():
    return ChatSession.archived_at.is_(None) & ~select(Project.id).where(
        Project.id == ChatSession.project_id, Project.archived_at.is_not(None),
    ).exists()


async def is_session_archived(db: AsyncSession, session: ChatSession) -> bool:
    if session.archived_at is not None:
        return True
    if session.project_id is None:
        return False
    return await db.scalar(select(Project.id).where(
        Project.id == session.project_id, Project.archived_at.is_not(None),
    )) is not None


async def set_archived(db: AsyncSession, item: ChatSession | Project, archived: bool) -> None:
    from mato_loop_backend.automations import advance_schedule

    if (item.archived_at is not None) == archived:
        return
    now = datetime.now(UTC)
    item.archived_at = now if archived else None
    sessions = select(ChatSession.id).where(
        ChatSession.user_id == item.user_id,
        ChatSession.project_id == item.id if isinstance(item, Project) else ChatSession.id == item.id,
    )
    automation_ids = select(Automation.id).where(Automation.execution_session_id.in_(sessions))
    # An archived conversation never accumulates work to replay on restoration.
    await db.execute(update(AutomationRun).where(
        AutomationRun.automation_id.in_(automation_ids), AutomationRun.status == "queued",
    ).values(status="cancelled", completed_at=now))
    if not archived:
        schedules = (await db.scalars(select(AutomationSchedule).where(
            AutomationSchedule.automation_id.in_(automation_ids),
            AutomationSchedule.enabled.is_(True), AutomationSchedule.next_run_at <= now,
        ).with_for_update())).all()
        for schedule in schedules:
            advance_schedule(schedule, now)
    await db.flush()
    await db.refresh(item, attribute_names=["updated_at"])
