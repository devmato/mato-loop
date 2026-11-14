from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend.agent.runtime import AgentRuntime
from mato_loop_backend.events import run_events
from mato_loop_backend.models import AgentRun, ChatSession, QuestionRequest
from mato_loop_backend.personal_variables import (
    PersonalVariableError,
    create_personal_variable,
    update_personal_variable,
)
from mato_loop_backend.settings import (
    AgentProfileUnavailable,
    ToolExecutionPolicy,
    load_agent_profile_tools,
)
from mato_loop_backend.tools.arguments import (
    ProtectedQuestionArguments,
    ProtectedQuestionResolution,
    QuestionArguments,
    validate_protected_question_answers,
    validate_question_answers,
)


class QuestionDecisionError(RuntimeError):
    def __init__(
        self, status_code: int, code: str, *, field: str | None = None
    ) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.field = field


async def answer_question(
    db: AsyncSession,
    request: QuestionRequest,
    answers: dict[str, list[str]],
    *,
    idempotent_same_result: bool = False,
    remember_for_session: list[str] | None = None,
    skipped_question_ids: list[str] | None = None,
) -> dict[str, str]:
    protected = bool(
        request.questions
        and request.questions[0].get("type") in {"value", "password"}
    )
    skipped = frozenset(skipped_question_ids or ())
    if not skipped <= answers.keys() or any(answers[key] for key in skipped):
        raise QuestionDecisionError(
            422, "question.invalidAnswer", field="skipped_question_ids"
        )
    if request.status != "pending":
        if not (idempotent_same_result and request.status == "answered"):
            raise QuestionDecisionError(409, "question.alreadyAnswered")
        if protected:
            return {"status": "answered"}
        try:
            questions = QuestionArguments.model_validate({"questions": request.questions})
            validated_answers = validate_question_answers(questions, answers)
            for key in skipped:
                validated_answers[key] = []
        except ValueError:
            raise QuestionDecisionError(409, "question.alreadyAnswered") from None
        if request.answers == validated_answers:
            return {"status": "answered"}
        raise QuestionDecisionError(409, "question.alreadyAnswered")

    try:
        questions = (
            ProtectedQuestionArguments.model_validate({"questions": request.questions})
            if protected
            else QuestionArguments.model_validate({"questions": request.questions})
        )
        validated_answers = (
            validate_protected_question_answers(questions, answers)
            if isinstance(questions, ProtectedQuestionArguments)
            else validate_question_answers(questions, answers)
        )
    except ValueError as exc:
        raise QuestionDecisionError(
            422, "question.invalidAnswer", field="answers"
        ) from exc

    for key in skipped:
        validated_answers[key] = []

    remembered = frozenset(remember_for_session or ())
    if remembered and (
        not isinstance(questions, ProtectedQuestionArguments)
        or not questions.questions[0].one_time
        or not remembered <= {key for key, value in validated_answers.items() if value}
    ):
        raise QuestionDecisionError(
            422, "question.invalidAnswer", field="remember_for_session"
        )

    if not AgentRuntime.claim_question_resolution(request.id):
        request.status = "expired"
        await db.commit()
        raise QuestionDecisionError(409, "question.runInactive")

    try:
        for question in questions.questions:
            if question.id not in remembered or not question.administrator:
                continue
            run = await db.get(AgentRun, request.run_id)
            session = await db.get(ChatSession, run.session_id) if run else None
            if session is not None:
                from mato_loop_backend.agent.session_settings import apply_run_session_settings

                apply_run_session_settings(session, run)
            if (
                session is None or run.execution_agent_id != question.administrator.agent_id
                or session.preferred_execution_agent_id != question.administrator.agent_id
            ):
                raise QuestionDecisionError(409, "approval.targetChanged")
    except BaseException:
        AgentRuntime.release_question_resolution(request.id)
        raise

    request_id = request.id
    run_id = request.run_id
    resolved_answers = validated_answers
    persisted_answers = validated_answers
    durable_protected = False
    if isinstance(questions, ProtectedQuestionArguments):
        if questions.questions[0].one_time:
            resolved_answers = ProtectedQuestionResolution(
                validated_answers, remembered
            )
            persisted_answers = {
                question.id: ["received"] if validated_answers[question.id] else []
                for question in questions.questions
            }
        elif validated_answers[questions.questions[0].id]:
            durable_protected = True
            question = questions.questions[0]
            assert question.save_proposal is not None
            proposal = question.save_proposal
            try:
                session = await db.get(ChatSession, request.session_id)
                run = await db.get(AgentRun, request.run_id)
                if session is None or run is None:
                    raise QuestionDecisionError(409, "question.runInactive")

                async def policy_changed() -> None:
                    request.status = "expired"
                    request.answered_at = datetime.now(UTC)
                    await db.commit()
                    AgentRuntime.resolve_question(request_id, "rejected")

                try:
                    settings, _profile = await load_agent_profile_tools(
                        db,
                        session.user_id,
                        proposal.agent_profile_id,
                        proposal.trust_level,
                    )
                except AgentProfileUnavailable:
                    await policy_changed()
                    raise QuestionDecisionError(409, "approval.policyChanged") from None
                from mato_loop_backend.permissions.resolution import resolve_tool_execution_policy

                if resolve_tool_execution_policy(
                    settings, "secrets.write", ToolExecutionPolicy.ASK
                ).policy == ToolExecutionPolicy.DISABLED or not settings.tool_availability.get(
                    "variable_write", True
                ):
                    await policy_changed()
                    raise QuestionDecisionError(409, "approval.policyChanged")
                if proposal.action == "create":
                    await create_personal_variable(
                        db,
                        session.user_id,
                        name=proposal.name,
                        value=validated_answers[question.id][0],
                        secret=proposal.secret,
                        description=proposal.description,
                    )
                else:
                    assert proposal.variable_id is not None
                    await update_personal_variable(
                        db,
                        session.user_id,
                        proposal.variable_id,
                        name=proposal.name,
                        value=validated_answers[question.id][0],
                        secret=proposal.secret,
                        description=proposal.description,
                    )
            except PersonalVariableError as exc:
                AgentRuntime.release_question_resolution(request_id)
                raise QuestionDecisionError(409, exc.code, field=exc.field) from exc
            except BaseException:
                AgentRuntime.release_question_resolution(request_id)
                raise
            resolved_answers = {question.id: ["saved"]}
            persisted_answers = resolved_answers
    request.status = "answered"
    request.answers = persisted_answers
    request.answered_at = datetime.now(UTC)
    try:
        await db.commit()
    except BaseException:
        try:
            await db.rollback()
        finally:
            AgentRuntime.release_question_resolution(request_id)
        raise
    if not AgentRuntime.resolve_question(request_id, resolved_answers):
        if durable_protected:
            return {"status": "answered"}
        request.status = "expired"
        request.answers = None
        request.answered_at = None
        await db.commit()
        raise QuestionDecisionError(409, "question.runInactive")
    await run_events.publish(
        run_id,
        {"type": "question.answered", "questionId": str(request_id)},
    )
    return {"status": "answered"}


async def reject_question(
    db: AsyncSession,
    request: QuestionRequest,
    *,
    idempotent_same_result: bool = False,
) -> dict[str, str]:
    if request.status != "pending":
        if idempotent_same_result and request.status == "rejected":
            return {"status": "rejected"}
        raise QuestionDecisionError(409, "question.alreadyAnswered")
    if not AgentRuntime.claim_question_resolution(request.id):
        request.status = "expired"
        await db.commit()
        raise QuestionDecisionError(409, "question.runInactive")

    request_id = request.id
    run_id = request.run_id
    request.status = "rejected"
    request.answered_at = datetime.now(UTC)
    try:
        await db.commit()
    except BaseException:
        try:
            await db.rollback()
        finally:
            AgentRuntime.release_question_resolution(request_id)
        raise
    if not AgentRuntime.resolve_question(request_id, "rejected"):
        request.status = "expired"
        request.answered_at = None
        await db.commit()
        raise QuestionDecisionError(409, "question.runInactive")
    await run_events.publish(
        run_id,
        {"type": "question.rejected", "questionId": str(request_id)},
    )
    return {"status": "rejected"}
