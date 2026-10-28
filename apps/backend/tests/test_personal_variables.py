from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError

import mato_loop_backend.personal_variables as personal_variables
from mato_loop_backend.models import Credential
from mato_loop_backend.personal_variables import (
    PersonalVariableError,
    normalize_personal_variable_description,
    personal_variable_payload,
)
from mato_loop_backend.tools.arguments import (
    ProtectedQuestionArguments,
    ShellRunArguments,
    validate_protected_question_answers,
)


def test_personal_variable_payload_never_reads_or_returns_secret(monkeypatch) -> None:
    variable = Credential(
        id=uuid4(),
        user_id=uuid4(),
        kind="personal_variable",
        label="API_TOKEN",
        encrypted_payload=b"encrypted",
        metadata_json={"secret": True, "description": "Token for deployments"},
    )
    def read(_variable):
        raise AssertionError("secret decrypted")

    monkeypatch.setattr("mato_loop_backend.personal_variables.read_credential", read)

    assert personal_variable_payload(variable) == {
        "id": str(variable.id),
        "name": "API_TOKEN",
        "description": "Token for deployments",
        "secret": True,
    }


def test_personal_variable_description_is_optional_and_normalized() -> None:
    assert normalize_personal_variable_description(None) is None
    assert normalize_personal_variable_description("   ") is None
    assert normalize_personal_variable_description("  Deployment token  ") == (
        "Deployment token"
    )
    with pytest.raises(PersonalVariableError):
        normalize_personal_variable_description("x" * 501)


@pytest.mark.parametrize("stored_secret", [False, True])
async def test_variable_rename_preserves_value_without_implicitly_revealing_secrets(monkeypatch, stored_secret):
    variable = Credential(id=uuid4(), user_id=uuid4(), label="OLD", metadata_json={"secret": stored_secret})
    db = AsyncMock()
    db.scalar.side_effect = [variable.user_id, variable, None]
    monkeypatch.setattr(personal_variables, "read_credential", lambda _variable: {"value": "existing-value"})
    store = AsyncMock()
    monkeypatch.setattr(personal_variables, "store_credential", store)
    if stored_secret:
        with pytest.raises(PersonalVariableError, match="personalVariable.valueRequired"):
            await personal_variables.update_personal_variable(
                db, variable.user_id, variable.id, name="NEW", value=None, secret=False,
            )
    else:
        await personal_variables.update_personal_variable(
            db, variable.user_id, variable.id, name="NEW", value=None, secret=False,
        )
        assert (store.await_args.kwargs["label"], store.await_args.kwargs["payload"]) == (
            "NEW", {"value": "existing-value"},
        )


def test_protected_answers_preserve_exact_value() -> None:
    questions = ProtectedQuestionArguments.model_validate(
        {
            "questions": [
                {
                    "id": "value",
                    "header": "Secret",
                    "question": "Enter the value",
                    "type": "password",
                    "save_proposal": {
                        "name": "API_TOKEN",
                        "description": "Token for deployments",
                        "secret": True,
                        "action": "create",
                        "trust_level": "public",
                    },
                }
            ]
        }
    )

    assert validate_protected_question_answers(
        questions, {"value": ["  exact value  "]}
    ) == {"value": ["  exact value  "]}


def test_one_time_protected_answers_support_visible_and_masked_values() -> None:
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
                },
            ]
        }
    )

    assert validate_protected_question_answers(
        questions,
        {"SSH_USERNAME": [" user "], "SSH_PASSWORD": [" password "]},
    ) == {"SSH_USERNAME": [" user "], "SSH_PASSWORD": [" password "]}

    with pytest.raises(ValueError, match="one answer per item"):
        validate_protected_question_answers(
            questions, {"SSH_PASSWORD": [" password "]}
        )


def test_shell_environment_accepts_references_but_rejects_host_names() -> None:
    reference = uuid4()
    assert ShellRunArguments(
        command="env", environment={"API_TOKEN": reference}
    ).environment == {"API_TOKEN": reference}
    session_reference = f"session-secret:{uuid4()}"
    assert ShellRunArguments(
        command="env", environment={"API_TOKEN": session_reference}
    ).environment == {"API_TOKEN": session_reference}

    for name in ("PATH", "LD_PRELOAD", "GIT_CONFIG_COUNT"):
        with pytest.raises(ValidationError):
            ShellRunArguments(command="env", environment={name: reference})
