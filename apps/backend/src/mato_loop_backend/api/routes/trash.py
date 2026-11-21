from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend.api.errors import ApiError
from mato_loop_backend.db.session import get_db_session
from mato_loop_backend.models import ChatSession, Project, UserAccount
from mato_loop_backend.security import require_user
from mato_loop_backend.sessions.service import SessionServiceError
from mato_loop_backend.sessions.trash import restore_from_trash

router = APIRouter(prefix="/trash", dependencies=[Depends(require_user)])


def _payload(item):
    return {"id": str(item.id), "deleteAt": item.delete_at.isoformat(), **(
        {"name": item.name} if isinstance(item, Project) else {
            "title": item.title, "projectId": str(item.project_id) if item.project_id else None,
        }
    )}


@router.get("")
async def list_trash(user: Annotated[UserAccount, Depends(require_user)],
                     db: Annotated[AsyncSession, Depends(get_db_session)]):
    projects = await db.scalars(select(Project).where(
        Project.user_id == user.id, Project.delete_at.is_not(None),
    ).order_by(Project.delete_at, Project.id))
    sessions = (await db.execute(select(ChatSession, Project.name).outerjoin(Project).where(
        ChatSession.user_id == user.id, ChatSession.parent_session_id.is_(None),
        ChatSession.delete_at.is_not(None), Project.delete_at.is_(None),
    ).order_by(ChatSession.delete_at, ChatSession.id))).all()
    return {"projects": [_payload(item) for item in projects],
            "sessions": [{**_payload(item), "projectName": name} for item, name in sessions]}


@router.get("/projects/{project_id}/sessions")
async def project_sessions(project_id: UUID,
                           user: Annotated[UserAccount, Depends(require_user)],
                           db: Annotated[AsyncSession, Depends(get_db_session)],
                           cursor: UUID | None = None):
    project = await db.scalar(select(Project).where(
        Project.id == project_id, Project.user_id == user.id, Project.delete_at.is_not(None),
    ))
    if project is None:
        raise ApiError(404, "trash.notFound")
    query = select(ChatSession).where(
        ChatSession.project_id == project.id, ChatSession.user_id == user.id,
        ChatSession.parent_session_id.is_(None),
    )
    if cursor:
        query = query.where(ChatSession.id > cursor)
    items = list(await db.scalars(query.order_by(ChatSession.id).limit(21)))
    return {"items": [{"id": str(item.id), "title": item.title,
                       "deleteAt": item.delete_at.isoformat() if item.delete_at else None}
                      for item in items[:20]],
            "nextCursor": str(items[19].id) if len(items) > 20 else None}


@router.post("/{kind}/{item_id}/restore", status_code=status.HTTP_204_NO_CONTENT,
             responses={200: {"description": "Restored session with worktree recovery status"}})
async def restore(kind: Literal["sessions", "projects"], item_id: UUID,
                  user: Annotated[UserAccount, Depends(require_user)],
                  db: Annotated[AsyncSession, Depends(get_db_session)]):
    model = ChatSession if kind == "sessions" else Project
    item = await db.scalar(select(model).where(model.id == item_id, model.user_id == user.id))
    if item is None or isinstance(item, ChatSession) and item.parent_session_id is not None:
        raise ApiError(404, "trash.notFound")
    try:
        worktree_restored = await restore_from_trash(db, item)
    except SessionServiceError as exc:
        raise ApiError(exc.status_code, exc.code) from exc
    if worktree_restored is not None:
        return JSONResponse({"worktreeRestored": worktree_restored})
    return Response(status_code=status.HTTP_204_NO_CONTENT)
