import asyncio
import json
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

import mato_loop_backend.agent.questions as mock_agent_questions
from mato_loop_backend.agent import question_decisions as question_decisions_module
from mato_loop_backend.agent.question_decisions import QuestionDecisionError
from mato_loop_backend.agent.question_decisions import (
    answer_question as answer_question_decision,
)
from mato_loop_backend.agent.question_decisions import (
    reject_question as reject_question_decision,
)
from mato_loop_backend.agent.runtime import AgentRuntime
from mato_loop_backend.api.routes.questions import (
    QuestionAnswerRequest,
    answer_question,
    reject_question,
)
from mato_loop_backend.events import run_events
from mato_loop_backend.models import AgentRun, ChatSession, QuestionRequest, UserAccount
from mato_loop_backend.personal_variables import PersonalVariableError
from mato_loop_backend.providers.base import ToolCall
from mato_loop_backend.settings import ToolSettings
from mato_loop_backend.tools.arguments import (
    ProtectedQuestionArguments,
    ProtectedQuestionResolution,
    QuestionArguments,
    validate_question_answers,
)
from mato_loop_backend.tools.catalog import tool_registry
from mato_loop_backend.tools.context import ToolContext
from mato_loop_backend.tools.contracts import ToolCallSuperseded


def _arguments() -> QuestionArguments:
    return QuestionArguments.model_validate(
        {
            "questions": [
                {
                    "id": "format",
                    "type": "single_choice",
                    "header": "Format",
                    "question": "Welches Format?",
                    "options": [{"label": "Kurz"}, {"label": "Ausführlich"}],
                },
                {
                    "id": "topics",
                    "type": "multiple_choice",
                    "header": "Themen",
                    "question": "Welche Themen?",
                    "options": [{"label": "API"}, {"label": "UI"}],
                },
                {
                    "id": "note",
                    "type": "free_text",
                    "header": "Hinweise",
                    "question": "Weitere Hinweise?",
                    "default_answers": ["Keine"],
                },
            ]
        }
    )


def test_question_answers_match_types_and_options() -> None:
    arguments = _arguments()
    answers = {
        "format": ["Eigener Stil"],
        "topics": ["API", "UI", "Tests"],
        "note": ["   "],
    }

    assert validate_question_answers(arguments, answers) == {
        "format": ["Eigener Stil"],
        "topics": ["API", "UI", "Tests"],
        "note": ["Keine"],
    }
    assert validate_question_answers(arguments, {**answers, "note": [" Hinweis "]})[
        "note"
    ] == ["Hinweis"]
    with pytest.raises(ValueError, match="single-choice"):
        validate_question_answers(arguments, {**answers, "format": ["Kurz", "Ausführlich"]})


def test_question_contract_rejects_invalid_questions_and_oversized_answers() -> None:
    payload = _arguments().model_dump(mode="json")
    payload["questions"][0]["question"] = "   "

    with pytest.raises(ValueError, match="question text must not be blank"):
        QuestionArguments.model_validate(payload)
    invalid_default = _arguments().model_dump(mode="json")
    invalid_default["questions"][0]["allow_custom"] = False
    invalid_default["questions"][0]["default_answers"] = ["Unbekannt"]
    with pytest.raises(ValueError, match="single-choice"):
        QuestionArguments.model_validate(invalid_default)
    with pytest.raises(ValueError, match="at most 13 items"):
        QuestionAnswerRequest(answers={"topics": [str(index) for index in range(14)]})


def test_question_schema_explains_answer_mode_discriminators() -> None:
    properties = QuestionArguments.model_json_schema()["$defs"]["QuestionItem"]["properties"]

    assert properties["type"]["enum"] == ["free_text", "single_choice", "multiple_choice"]
    assert properties["type"]["description"] == (
        "free_text: text; single_choice: exactly one option; multiple_choice: one or more options."
    )
    assert properties["options"]["description"] == "Empty for free_text; 2-12 items for choices."
    assert properties["placeholder"]["description"] == (
        "free_text only. Omit or use null for choices, even with allow_custom=true."
    )


@pytest.mark.parametrize("question_type", ["single_choice", "multiple_choice"])
@pytest.mark.parametrize("allow_custom", [False, True])
async def test_question_placeholder_error_identifies_field_and_allows_repair(
    question_type, allow_custom
) -> None:
    payload = _arguments().model_dump(mode="json")
    payload["questions"][2]["placeholder"] = "Additional notes"
    question = payload["questions"][1]
    question.update(
        type=question_type, allow_custom=allow_custom, placeholder="Additional topics"
    )
    call = ToolCall("question-1", "question", payload)
    context = Mock(spec=ToolContext, tool_settings=ToolSettings(), ask_user=AsyncMock())

    result = await tool_registry.execute(call, context)

    assert result.is_error
    assert json.loads(result.content) == {
        "error": {
            "code": "question.placeholderNotAllowed",
            "message": (
                "placeholder is only allowed for free_text. For choice questions, "
                "omit placeholder or set it to null, including when allow_custom is true."
            ),
            "field": "questions[1].placeholder",
        }
    }
    context.ask_user.assert_not_awaited()

    question["placeholder"] = None
    arguments = QuestionArguments.model_validate(payload)
    assert arguments.questions[1].placeholder is None
    assert arguments.questions[2].placeholder == "Additional notes"
    del question["placeholder"]
    assert QuestionArguments.model_validate(payload) == arguments
    context.ask_user.return_value = {"format": ["Kurz"], "topics": ["API"], "note": []}

    result = await tool_registry.execute(call, context)

    assert not result.is_error
    context.ask_user.assert_awaited_once_with(arguments)


def test_question_placeholder_error_preserves_nested_location() -> None:
    payload = _arguments().model_dump(mode="json")
    payload["questions"][1]["placeholder"] = "Additional topics"

    with pytest.raises(ValidationError) as raised:
        QuestionArguments.model_validate(payload)

    error = raised.value.errors(include_url=False, include_input=False, include_context=False)[0]
    assert error["type"] == "question_placeholder_not_allowed"
    assert error["loc"] == ("questions", 1)


@pytest.mark.parametrize("redirected", [False, True])
async def test_agent_question_push_is_sent_after_commit(monkeypatch, redirected) -> None:
    request_id = uuid4()
    session = ChatSession(id=uuid4(), user_id=uuid4(), title="Test")
    run = AgentRun(id=uuid4(), session_id=session.id)
    db = AsyncMock(spec=AsyncSession)
    created = asyncio.Event()

    async def flush() -> None:
        db.add.call_args.args[0].id = request_id
        db.add.call_args.args[0].status = "pending"

    async def publish(*_args) -> None:
        created.set()

    async def deliver(*_args) -> None:
        assert db.commit.await_count == 1

    db.flush.side_effect = flush
    monkeypatch.setattr(run_events, "publish", publish)
    push = AsyncMock(side_effect=deliver)
    notification_id = uuid4()
    monkeypatch.setattr(mock_agent_questions.NotificationService, "create_for_question", AsyncMock(return_value=notification_id))
    monkeypatch.setattr(mock_agent_questions.NotificationService, "deliver_after_commit", push)
    task = asyncio.create_task(
        AgentRuntime._request_question(
            db,
            run,
            session,
            ToolCall(id="call-1", name="question", arguments={}),
            _arguments(),
        )
    )
    async with asyncio.timeout(1):
        await created.wait()
    if redirected:
        try:
            AgentRuntime.finish_continuation(run.id, asyncio.Event(), accepted=True)
            with pytest.raises(ToolCallSuperseded):
                await asyncio.wait_for(task, timeout=1)
            assert db.add.call_args.args[0].status == "expired"
        finally:
            AgentRuntime._pending_continuations.discard(run.id)
    else:
        assert AgentRuntime.claim_question_resolution(request_id) is True
        assert AgentRuntime.resolve_question(request_id, {"format": ["Kurz"]}) is True
        assert await task == {"format": ["Kurz"]}
    push.assert_awaited_once_with(notification_id)


async def test_answer_is_persisted_before_agent_resumes(monkeypatch) -> None:
    arguments = _arguments()
    request = QuestionRequest(
        id=uuid4(),
        session_id=uuid4(),
        run_id=uuid4(),
        tool_call_id="call-1",
        questions=arguments.model_dump(mode="json")["questions"],
        status="pending",
    )
    user = UserAccount(id=uuid4(), username="test", password_hash="hash")
    session = ChatSession(id=request.session_id, user_id=user.id, title="Test")
    db = AsyncMock(spec=AsyncSession)
    db.get.side_effect = [request, session]
    claim = Mock(return_value=True)
    resolve = Mock(return_value=True)
    monkeypatch.setattr(AgentRuntime, "claim_question_resolution", claim)
    monkeypatch.setattr(AgentRuntime, "resolve_question", resolve)
    monkeypatch.setattr(run_events, "publish", AsyncMock())
    answers = {"format": ["Kurz"], "topics": ["API"], "note": []}
    resolved_answers = {**answers, "note": ["Keine"]}

    result = await answer_question(
        request.id, QuestionAnswerRequest(answers=answers), user, db
    )

    assert request.status == "answered"
    assert request.answers == resolved_answers
    db.commit.assert_awaited_once()
    resolve.assert_called_once_with(request.id, resolved_answers)
    assert result == {"status": "answered"}


@pytest.mark.parametrize(("filled", "action"), [(True, "create"), (False, "update")])
async def test_protected_answer_is_saved_but_never_persisted_or_resumed(
    monkeypatch, filled, action,
) -> None:
    variable_id = uuid4() if action == "update" else None
    questions = ProtectedQuestionArguments.model_validate(
        {
            "questions": [
                {
                    "id": "value",
                    "header": "Secret",
                    "question": "Enter token",
                    "type": "password",
                    "save_proposal": {
                        "name": "API_TOKEN",
                        "description": "Token for deployments",
                        "secret": True,
                        "action": action,
                        "variable_id": variable_id,
                        "trust_level": "public",
                    },
                }
            ]
        }
    )
    request = QuestionRequest(
        id=uuid4(),
        session_id=uuid4(),
        run_id=uuid4(),
        tool_call_id="call-1",
        questions=questions.model_dump(mode="json")["questions"],
        status="pending",
    )
    session = ChatSession(id=request.session_id, user_id=uuid4(), title="Test")
    db = AsyncMock(spec=AsyncSession)
    db.get.return_value = session
    save = AsyncMock()
    update = AsyncMock()
    resolve = Mock(return_value=True)
    monkeypatch.setattr(question_decisions_module, "create_personal_variable", save)
    monkeypatch.setattr(question_decisions_module, "update_personal_variable", update)
    monkeypatch.setattr(
        question_decisions_module,
        "load_agent_profile_tools",
        AsyncMock(return_value=(ToolSettings(), None)),
    )
    monkeypatch.setattr(
        AgentRuntime, "claim_question_resolution", Mock(return_value=True)
    )
    monkeypatch.setattr(AgentRuntime, "resolve_question", resolve)
    monkeypatch.setattr(run_events, "publish", AsyncMock())

    result = await answer_question_decision(
        db, request, {"value": ["  exact secret  "] if filled else []}
    )

    if not filled:
        save.assert_not_awaited()
        update.assert_not_awaited()
        resolve.assert_called_once_with(request.id, {"value": []})
        return
    save.assert_awaited_once_with(
        db,
        session.user_id,
        name="API_TOKEN",
        value="  exact secret  ",
        secret=True,
        description="Token for deployments",
    )
    assert request.answers == {"value": ["saved"]}
    assert "exact secret" not in json.dumps(request.answers)
    resolve.assert_called_once_with(request.id, {"value": ["saved"]})
    assert result == {"status": "answered"}


@pytest.mark.parametrize("password", [["  exact secret  "], []])
async def test_one_time_secret_is_delivered_but_never_persisted(monkeypatch, password) -> None:
    questions = ProtectedQuestionArguments.model_validate(
        {
            "questions": [
                {
                    "id": "SSH_USERNAME",
                    "header": "Username",
                    "question": "Enter username",
                    "type": "value",
                    "one_time": True,
                },
                {
                    "id": "SSH_PASSWORD",
                    "header": "Password",
                    "question": "Enter password",
                    "type": "password",
                    "one_time": True,
                    "administrator": {"agent_id": str(uuid4()), "alias": "AI-PC"},
                }
            ]
        }
    )
    request = QuestionRequest(
        id=uuid4(),
        session_id=uuid4(),
        run_id=uuid4(),
        tool_call_id="call-1",
        questions=questions.model_dump(mode="json")["questions"],
        status="pending",
    )
    db = AsyncMock(spec=AsyncSession)
    resolve = Mock(return_value=True)
    monkeypatch.setattr(
        AgentRuntime, "claim_question_resolution", Mock(return_value=True)
    )
    monkeypatch.setattr(AgentRuntime, "resolve_question", resolve)
    monkeypatch.setattr(run_events, "publish", AsyncMock())

    if not password:
        with pytest.raises(QuestionDecisionError, match="question.invalidAnswer"):
            await answer_question_decision(
                db, request, {"SSH_USERNAME": ["user"], "SSH_PASSWORD": []},
                remember_for_session=["SSH_PASSWORD"],
            )

    result = await answer_question_decision(
        db,
        request,
        {
            "SSH_USERNAME": ["  exact user  "],
            "SSH_PASSWORD": password,
        },
        remember_for_session=["SSH_USERNAME"],
    )

    assert request.answers == {
        "SSH_USERNAME": ["received"],
        "SSH_PASSWORD": ["received"] if password else [],
    }
    assert "exact user" not in json.dumps(request.answers)
    assert "exact secret" not in json.dumps(request.answers)
    db.get.assert_not_awaited()
    db.add.assert_not_called()
    db.commit.assert_awaited_once()
    resolve.assert_called_once_with(
        request.id,
        ProtectedQuestionResolution(
            {
                "SSH_USERNAME": ["  exact user  "],
                "SSH_PASSWORD": password,
            },
            frozenset({"SSH_USERNAME"}),
        ),
    )
    assert result == {"status": "answered"}


async def test_protected_answer_failure_releases_resolution(monkeypatch) -> None:
    questions = ProtectedQuestionArguments.model_validate(
        {
            "questions": [
                {
                    "id": "value",
                    "header": "Secret",
                    "question": "Enter token",
                    "type": "password",
                    "save_proposal": {
                        "name": "API_TOKEN",
                        "secret": True,
                        "action": "create",
                        "trust_level": "public",
                    },
                }
            ]
        }
    )
    request = QuestionRequest(
        id=uuid4(),
        session_id=uuid4(),
        run_id=uuid4(),
        tool_call_id="call-1",
        questions=questions.model_dump(mode="json")["questions"],
        status="pending",
    )
    session = ChatSession(id=request.session_id, user_id=uuid4(), title="Test")
    db = AsyncMock(spec=AsyncSession)
    db.get.return_value = session
    release = Mock()
    monkeypatch.setattr(
        question_decisions_module,
        "load_agent_profile_tools",
        AsyncMock(return_value=(ToolSettings(), None)),
    )
    monkeypatch.setattr(
        question_decisions_module,
        "create_personal_variable",
        AsyncMock(side_effect=PersonalVariableError("personalVariable.duplicate")),
    )
    monkeypatch.setattr(
        AgentRuntime, "claim_question_resolution", Mock(return_value=True)
    )
    monkeypatch.setattr(AgentRuntime, "release_question_resolution", release)

    with pytest.raises(QuestionDecisionError, match="personalVariable.duplicate"):
        await answer_question_decision(db, request, {"value": ["secret"]})

    release.assert_called_once_with(request.id)


async def test_rejection_is_persisted_before_agent_resumes(monkeypatch) -> None:
    request = QuestionRequest(
        id=uuid4(),
        session_id=uuid4(),
        run_id=uuid4(),
        tool_call_id="call-1",
        questions=_arguments().model_dump(mode="json")["questions"],
        status="pending",
    )
    user = UserAccount(id=uuid4(), username="test", password_hash="hash")
    session = ChatSession(id=request.session_id, user_id=user.id, title="Test")
    db = AsyncMock(spec=AsyncSession)
    db.get.side_effect = [request, session]
    monkeypatch.setattr(
        AgentRuntime, "claim_question_resolution", Mock(return_value=True)
    )
    resolve = Mock(return_value=True)
    monkeypatch.setattr(AgentRuntime, "resolve_question", resolve)
    monkeypatch.setattr(run_events, "publish", AsyncMock())

    result = await reject_question(request.id, user, db)

    assert request.status == "rejected"
    db.commit.assert_awaited_once()
    resolve.assert_called_once_with(request.id, "rejected")
    assert result == {"status": "rejected"}


async def test_failed_answer_commit_releases_resolution(monkeypatch) -> None:
    request = QuestionRequest(
        id=uuid4(),
        session_id=uuid4(),
        run_id=uuid4(),
        tool_call_id="call-1",
        questions=_arguments().model_dump(mode="json")["questions"],
        status="pending",
    )
    user = UserAccount(id=uuid4(), username="test", password_hash="hash")
    session = ChatSession(id=request.session_id, user_id=user.id, title="Test")
    db = AsyncMock(spec=AsyncSession)
    db.get.side_effect = [request, session]
    db.commit.side_effect = RuntimeError("commit failed")
    db.rollback.side_effect = RuntimeError("rollback failed")
    monkeypatch.setattr(
        AgentRuntime, "claim_question_resolution", Mock(return_value=True)
    )
    release = Mock()
    monkeypatch.setattr(AgentRuntime, "release_question_resolution", release)

    with pytest.raises(RuntimeError, match="rollback failed"):
        await answer_question(
            request.id,
            QuestionAnswerRequest(
                answers={"format": ["Kurz"], "topics": ["API"], "note": []}
            ),
            user,
            db,
        )

    db.rollback.assert_awaited_once()
    release.assert_called_once_with(request.id)


async def test_question_decisions_are_idempotent_only_for_same_final_result(
    monkeypatch,
) -> None:
    arguments = _arguments()
    answers = {"format": ["Kurz"], "topics": ["API"], "note": ["Keine"]}
    request = QuestionRequest(
        id=uuid4(),
        session_id=uuid4(),
        run_id=uuid4(),
        tool_call_id="call-1",
        questions=arguments.model_dump(mode="json")["questions"],
        answers=answers,
        status="answered",
    )
    db = AsyncMock(spec=AsyncSession)
    claim = Mock()
    resolve = Mock()
    monkeypatch.setattr(AgentRuntime, "claim_question_resolution", claim)
    monkeypatch.setattr(AgentRuntime, "resolve_question", resolve)

    assert await answer_question_decision(
        db, request, answers, idempotent_same_result=True
    ) == {"status": "answered"}
    with pytest.raises(QuestionDecisionError) as exc_info:
        await answer_question_decision(
            db,
            request,
            {**answers, "format": ["Ausführlich"]},
            idempotent_same_result=True,
        )
    assert (exc_info.value.status_code, exc_info.value.code) == (
        409,
        "question.alreadyAnswered",
    )
    with pytest.raises(QuestionDecisionError, match="question.alreadyAnswered"):
        await reject_question_decision(db, request, idempotent_same_result=True)
    claim.assert_not_called()
    resolve.assert_not_called()
    db.commit.assert_not_awaited()

    request.status = "rejected"
    request.answers = None
    assert await reject_question_decision(
        db, request, idempotent_same_result=True
    ) == {"status": "rejected"}
    claim.assert_not_called()


async def test_cancelled_commit_rolls_back_and_releases_resolution(monkeypatch) -> None:
    request = QuestionRequest(
        id=uuid4(),
        session_id=uuid4(),
        run_id=uuid4(),
        tool_call_id="call-1",
        questions=_arguments().model_dump(mode="json")["questions"],
        status="pending",
    )
    db = AsyncMock(spec=AsyncSession)
    db.commit.side_effect = asyncio.CancelledError
    monkeypatch.setattr(
        AgentRuntime, "claim_question_resolution", Mock(return_value=True)
    )
    release = Mock()
    monkeypatch.setattr(AgentRuntime, "release_question_resolution", release)

    with pytest.raises(asyncio.CancelledError):
        await reject_question_decision(db, request)

    db.rollback.assert_awaited_once()
    release.assert_called_once_with(request.id)
