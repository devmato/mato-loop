from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend.agent.question_decisions import QuestionDecisionError
from mato_loop_backend.agent.question_decisions import (
    answer_question as answer_question_decision,
)
from mato_loop_backend.agent.question_decisions import (
    reject_question as reject_question_decision,
)
from mato_loop_backend.agent.run_support import question_payload
from mato_loop_backend.api.errors import ApiError
from mato_loop_backend.db.session import get_db_session
from mato_loop_backend.models import AgentRun, ChatSession, QuestionRequest, UserAccount
from mato_loop_backend.security import require_user

router = APIRouter(prefix="/questions", dependencies=[Depends(require_user)])


class QuestionAnswerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    answers: dict[
        str,
        Annotated[list[Annotated[str, Field(max_length=20_000)]], Field(max_length=13)],
    ] = Field(max_length=5)
    remember_for_session: list[
        Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,79}$")]
    ] = Field(default_factory=list, max_length=5)
    skipped_question_ids: list[
        Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,79}$")]
    ] = Field(default_factory=list, max_length=5)


@router.get("/pending")
async def pending_questions(
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[dict]:
    items = list(
        (
            await db.scalars(
                select(QuestionRequest)
                .join(ChatSession, ChatSession.id == QuestionRequest.session_id)
                .where(QuestionRequest.status == "pending")
                .where(ChatSession.user_id == user.id)
                .order_by(QuestionRequest.created_at)
            )
        ).all()
    )
    if not items:
        return []
    children = (await db.execute(
        select(AgentRun.id, ChatSession.id, ChatSession.title, ChatSession.parent_session_id)
        .join(ChatSession, ChatSession.id == AgentRun.session_id)
        .where(
            AgentRun.id.in_([item.run_id for item in items]),
            ChatSession.user_id == user.id,
            ChatSession.parent_session_id.is_not(None),
        )
    )).all()
    origins = {
        run_id: (parent_id, {"subagentSessionId": str(session_id), "subagentTitle": title})
        for run_id, session_id, title, parent_id in children
    }
    result = []
    for item in items:
        payload = question_payload(item)
        origin = origins.get(item.run_id)
        if origin is not None and origin[0] == item.session_id:
            payload["payload"] = origin[1]
        result.append(payload)
    return result


@router.post("/{question_id}/answers")
async def answer_question(
    question_id: UUID,
    payload: QuestionAnswerRequest,
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, str]:
    request = await db.get(QuestionRequest, question_id, with_for_update=True)
    if request is None:
        raise ApiError(404, "question.notFound")
    session = await db.get(ChatSession, request.session_id)
    if session is None or session.user_id != user.id:
        raise ApiError(404, "question.notFound")
    try:
        return await answer_question_decision(
            db,
            request,
            payload.answers,
            remember_for_session=payload.remember_for_session,
            skipped_question_ids=payload.skipped_question_ids,
        )
    except QuestionDecisionError as exc:
        raise ApiError(exc.status_code, exc.code, field=exc.field) from exc


@router.post("/{question_id}/reject")
async def reject_question(
    question_id: UUID,
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, str]:
    request = await db.get(QuestionRequest, question_id, with_for_update=True)
    if request is None:
        raise ApiError(404, "question.notFound")
    session = await db.get(ChatSession, request.session_id)
    if session is None or session.user_id != user.id:
        raise ApiError(404, "question.notFound")
    try:
        return await reject_question_decision(db, request)
    except QuestionDecisionError as exc:
        raise ApiError(exc.status_code, exc.code, field=exc.field) from exc
