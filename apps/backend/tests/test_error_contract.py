from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from mato_loop_backend.api.errors import register_error_handlers
from mato_loop_backend.error_contract import describe_error
from mato_loop_backend.providers.base import ProviderError


def test_http_error_uses_safe_english_message_instead_of_raw_detail() -> None:
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/failure")
    async def failure() -> None:
        raise HTTPException(418, detail="secret upstream response")

    response = TestClient(app).get("/failure")

    assert response.status_code == 418
    assert response.json() == {
        "error": {
            "code": "http.requestFailed",
            "message": "The request failed.",
        }
    }
    assert "secret upstream response" not in response.text


def test_unknown_error_code_and_provider_code_fall_back_without_raw_text() -> None:
    unknown = describe_error("secret.upstreamResponse")
    provider = ProviderError(
        "raw provider exception",
        code="provider.rawSecretResponse",
    )

    assert unknown.payload() == {
        "code": "secret.upstreamResponse",
        "message": "The action failed.",
    }
    assert provider.public_code == "provider.requestFailed"
    assert "raw" not in str(unknown.payload())


def test_chat_history_errors_have_controlled_fallbacks() -> None:
    assert describe_error("file.sessionQuotaExceeded").message == "The session storage limit has been reached."
    assert describe_error("attachment.sessionQuotaExceeded").message == "The session storage limit has been reached."
    assert describe_error("session.messageNotFound").message == "The chat message was not found."
    assert (
        describe_error("session.retryUnavailable").message
        == "The failed response can no longer be retried."
    )
    assert (
        describe_error("tool.executionInterrupted").message
        == "The tool execution was interrupted and its outcome is unknown. Do not infer success or "
        "failure without verification."
    )
    assert (
        describe_error("tool.cursorInvalid").message
        == "The session cursor is unavailable. Retry session_read with "
        "error.params.retryArguments exactly."
    )
    assert describe_error("tool.invalidArguments").message == (
        "The tool arguments are invalid. Inspect error.field and error.params.reason and retry with arguments "
        "that match the tool schema."
    )


def test_changed_file_error_has_controlled_fallback() -> None:
    assert describe_error("file.changed").message == "The file changed since it was opened."


def test_file_access_errors_have_controlled_fallbacks() -> None:
    assert describe_error("file.accessDenied").message == "Access to the file was denied."
    assert describe_error("file.unavailable").message == "The file is unavailable."


def test_execution_and_output_limits_have_controlled_fallbacks() -> None:
    assert describe_error("process.commandFailed").message == "The command exited with a nonzero status."
    assert describe_error("agent.outputLimitReached").message == "The model response was interrupted at its output limit."
    assert describe_error("process.invalidSyntax").message == "The shell command has invalid syntax."
    assert describe_error("process.inspectionLimitExceeded").message == "The shell command exceeds the inspection limits."
    assert describe_error("process.unsupportedTargetQuoting").message == "The shell path target uses unsupported quoting. Use a literal path with ordinary quotes."


def test_provider_context_capacity_error_preserves_controlled_code() -> None:
    from mato_loop_backend.providers.base import ProviderError

    error = ProviderError("private request detail", code="agent.contextWindowExceeded")
    assert error.public_code == "agent.contextWindowExceeded"
    assert describe_error(error.public_code).message == "The request does not fit in the model's context window."


def test_incomplete_patch_error_requires_rereading_even_the_failed_operation() -> None:
    assert describe_error("file.patchIncomplete").message == (
        "The patch did not complete. Files may have changed, including in the "
        "failed operation. Reread the affected paths before retrying."
    )


def test_memory_approval_conflicts_have_controlled_fallbacks() -> None:
    assert (
        describe_error("approval.targetChanged").message
        == "The approval target changed before the decision was applied."
    )
    assert (
        describe_error("approval.policyChanged").message
        == "The approval policy changed before the decision was applied."
    )


def test_tls_error_has_controlled_fallback() -> None:
    assert (
        describe_error("webfetch.tlsFailed").message
        == "The web server's TLS certificate could not be verified."
    )


def test_integration_permission_error_has_controlled_fallback() -> None:
    assert (
        describe_error("integration.permissionDenied").message
        == "The integration does not permit this operation."
    )


def test_user_denial_errors_have_controlled_fallbacks() -> None:
    assert (
        describe_error("permission.userDenied").message
        == "The user explicitly denied this action."
    )
    assert (
        describe_error("tool.skippedAfterUserDenial").message
        == "The tool call was not run because the user denied another action in the same batch."
    )


def test_video_processing_errors_have_controlled_fallbacks() -> None:
    assert (
        describe_error("video.frameExtractionFailed").message
        == "The video frames could not be extracted."
    )
    assert (
        describe_error("video.audioExtractionFailed").message
        == "The video audio track could not be extracted."
    )


def test_image_limit_error_has_controlled_fallback() -> None:
    assert (
        describe_error("provider.imageLimitExceeded", params={"maxImages": 4}).payload()
        == {
            "code": "provider.imageLimitExceeded",
            "message": "The request contains too many images.",
            "params": {"maxImages": 4},
        }
    )


def test_execution_agent_pairing_errors_have_controlled_fallbacks() -> None:
    assert (
        describe_error("executionAgent.alreadyPaired").message
        == "The MatoLoop Agent is already paired."
    )
    assert (
        describe_error("executionAgent.pairingTokenInvalid").message
        == "The MatoLoop Agent pairing token is invalid."
    )
    assert (
        describe_error("executionAgent.pairingTokenExpired").message
        == "The MatoLoop Agent pairing token has expired."
    )


def test_notification_errors_have_controlled_fallbacks() -> None:
    assert describe_error("notification.notFound").message == "The notification was not found."
    assert (
        describe_error("notification.unknown").message
        == "The notification request could not be completed."
    )
