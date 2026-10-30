from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend.memory.episode_retrieval import EpisodeRetrievalService
from mato_loop_backend.memory.retrieval import MemoryRetrievalStrategy
from mato_loop_backend.models import ChatSession, Project
from mato_loop_backend.sessions.trust import session_visibility_filters
from mato_loop_backend.settings import TrustLevel


@dataclass(frozen=True, slots=True)
class ProjectSearchResult:
    id: UUID
    name: str
    session_count: int


@dataclass(frozen=True, slots=True)
class SessionSearchResult:
    id: UUID
    title: str
    project_id: UUID | None
    project_name: str | None
    match_type: str
    message_id: UUID | None = None
    message_role: str | None = None
    message_excerpt: str | None = None
    matched_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class GlobalSearchResult:
    projects: list[ProjectSearchResult]
    sessions: list[SessionSearchResult]
    strategy: MemoryRetrievalStrategy


def _excerpt(content: str, limit: int = 500) -> str:
    normalized = " ".join(content.split())
    return normalized if len(normalized) <= limit else f"{normalized[:limit].rstrip()}..."


class GlobalSearchService:
    @staticmethod
    async def session_titles(
        db: AsyncSession, *, owner_id: UUID, query: str, limit: int = 10,
        exclude_session_id: UUID | None = None,
        readable_by: TrustLevel = TrustLevel.PRIVATE,
        project_id: UUID | None = None,
    ) -> list[tuple[ChatSession, str | None]]:
        filters = [
            ChatSession.user_id == owner_id,
            ChatSession.session_type == "chat",
            ChatSession.parent_session_id.is_(None),
            ChatSession.title.icontains(query.strip(), autoescape=True),
            *session_visibility_filters(readable_by),
        ]
        if exclude_session_id is not None:
            filters.append(ChatSession.id != exclude_session_id)
        if project_id is not None:
            filters.append(ChatSession.project_id == project_id)
        rows = (await db.execute(
            select(ChatSession, Project.name)
            .outerjoin(Project, (Project.id == ChatSession.project_id) & (Project.user_id == ChatSession.user_id))
            .where(*filters)
            .order_by(ChatSession.updated_at.desc(), ChatSession.id.desc())
            .limit(limit)
        )).all()
        return [(session, project_name) for session, project_name in rows]

    @staticmethod
    async def search(
        db: AsyncSession,
        *,
        owner_id: UUID,
        query: str,
        limit: int = 10,
    ) -> GlobalSearchResult:
        cleaned_query = query.strip()
        if not cleaned_query:
            raise ValueError("global search requires a query")

        project_rows = (
            await db.execute(
                select(Project, func.count(ChatSession.id))
                .outerjoin(
                    ChatSession,
                    (ChatSession.project_id == Project.id)
                    & (ChatSession.user_id == Project.user_id)
                    & ChatSession.parent_session_id.is_(None),
                )
                .where(
                    Project.user_id == owner_id,
                    Project.name.icontains(cleaned_query, autoescape=True),
                )
                .group_by(Project.id)
                .order_by(Project.updated_at.desc(), Project.id.desc())
                .limit(limit)
            )
        ).all()
        projects = [
            ProjectSearchResult(project.id, project.name, session_count)
            for project, session_count in project_rows
        ]

        title_rows = await GlobalSearchService.session_titles(
            db, owner_id=owner_id, query=cleaned_query, limit=limit,
        )

        episodes = await EpisodeRetrievalService.retrieve(
            db,
            owner_id=owner_id,
            current_session_id=None,
            readable_by=TrustLevel.PRIVATE,
            query=cleaned_query,
            limit=limit,
            max_semantic_distance=0.25,
            anchor_only=True,
        )
        project_ids = {
            reference.project_id
            for reference in episodes.references
            if reference.project_id is not None
        }
        project_names = {
            project_id: name
            for project_id, name in (
                (
                    await db.execute(
                        select(Project.id, Project.name).where(
                            Project.user_id == owner_id,
                            Project.id.in_(project_ids),
                        )
                    )
                ).all()
                if project_ids
                else []
            )
        }

        sessions = [
            SessionSearchResult(
                id=session.id,
                title=session.title,
                project_id=session.project_id,
                project_name=project_name,
                match_type="title",
                matched_at=session.updated_at,
            )
            for session, project_name in title_rows
        ]
        seen_session_ids = {item.id for item in sessions}
        for reference in episodes.references:
            if reference.session_id in seen_session_ids or len(sessions) == limit:
                continue
            seen_session_ids.add(reference.session_id)
            sessions.append(
                SessionSearchResult(
                    id=reference.session_id,
                    title=reference.session_title or "Neue Session",
                    project_id=reference.project_id,
                    project_name=project_names.get(reference.project_id),
                    match_type="message",
                    message_id=reference.anchor_message_id,
                    message_role=reference.anchor_role,
                    message_excerpt=_excerpt(reference.anchor_content),
                    matched_at=reference.anchor_created_at,
                )
            )

        return GlobalSearchResult(
            projects=projects,
            sessions=sessions,
            strategy=episodes.strategy,
        )
