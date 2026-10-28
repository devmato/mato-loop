from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

import mato_loop_backend.agent.run_support as agent_run_support
from mato_loop_backend.agent.activity import run_activity_label
from mato_loop_backend.agent.run_timing import run_timing_payload, transition_run_status
from mato_loop_backend.agent.subagents import permission_origin, provenance
from mato_loop_backend.events import run_events
from mato_loop_backend.messaging.questions import enqueue_question_prompt
from mato_loop_backend.models import AgentRun, ChatSession, Message, QuestionRequest
from mato_loop_backend.notifications import NotificationService
from mato_loop_backend.permissions.coordinator import question_coordinator
from mato_loop_backend.providers.base import ToolCall
from mato_loop_backend.tools.arguments import (
    ProtectedQuestionResolution,
    QuestionArguments,
    QuestionRequestArguments,
)
from mato_loop_backend.tools.contracts import ToolCallSuperseded


async def request_question(
    cls,
    db: AsyncSession,
    run: AgentRun,
    chat_session: ChatSession,
    call: ToolCall,
    questions: QuestionRequestArguments,
) -> (
    dict[str, list[str]]
    | ProtectedQuestionResolution
    | Literal["rejected"]
    | None
):
    origin = await permission_origin(db, chat_session)
    request = QuestionRequest(
        session_id=origin.id,
        run_id=run.id,
        tool_call_id=call.id,
        questions=questions.model_dump(mode="json")["questions"],
    )
    db.add(request)
    transition_run_status(run, "waiting_input")
    await db.flush()
    notification_id = await NotificationService.create_for_question(db, request)
    if isinstance(questions, QuestionArguments) and run.input_message_id is not None:
        input_message = await db.get(Message, run.input_message_id)
        if (
            input_message is not None
            and input_message.source_type == "endpoint"
            and input_message.source_endpoint_id is not None
        ):
            await enqueue_question_prompt(db, request)
    question_id, run_id = request.id, run.id
    waiter = question_coordinator.register(question_id)
    cls._waiting_interactions[run_id] = (chat_session.spawned_by_run_id, waiter)
    if run_id in cls._pending_continuations:
        waiter.set_result("superseded")
    try:
        if chat_session.spawned_by_run_id in cls._pending_continuations:
            cls.cancel(run_id)
        await db.commit()
        await run_events.publish(
            run.id,
            {"type": "run.timing", "runId": str(run.id), **run_timing_payload(run)},
        )
        await run_events.publish(
            run.id,
            {
                "type": "question.created",
                "question": {
                    **agent_run_support.question_payload(request),
                    **({"payload": provenance(chat_session)} if chat_session.parent_session_id is not None else {}),
                },
                "activityLabel": run_activity_label("waiting_input"),
            },
        )
        if notification_id is not None:
            await NotificationService.deliver_after_commit(notification_id)
        answers = await asyncio.shield(waiter)
    except asyncio.CancelledError:
        await db.rollback()
        current = await db.get(QuestionRequest, question_id)
        if current is not None and current.status == "pending":
            current.status = "expired"
            await db.commit()
            await run_events.publish(run_id, {"type": "question.expired", "questionId": str(question_id)})
        raise
    finally:
        cls._waiting_interactions.pop(run_id, None)
        question_coordinator.discard(question_id)
    await db.refresh(request, with_for_update=True)
    superseded = answers == "superseded" or run_id in cls._pending_continuations
    if superseded and request.status == "pending":
        request.status = "expired"
        request.answered_at = datetime.now(UTC)
    transition_run_status(run, "running")
    await db.commit()
    if superseded:
        await run_events.publish(run_id, {"type": "question.expired", "questionId": str(question_id)})
    await run_events.publish(
        run.id, {"type": "run.timing", "runId": str(run.id), **run_timing_payload(run)}
    )
    if superseded:
        raise ToolCallSuperseded
    return answers
