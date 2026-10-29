import asyncio
import json
import logging
import wave
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import Headers, UploadFile

import mato_loop_backend.agent.loop as mock_agent_loop
import mato_loop_backend.agent.runtime as mock_agent_runtime
import mato_loop_backend.agent.titles as mock_agent_titles
from mato_loop_backend.agent.projection import message_dict
from mato_loop_backend.agent.runtime import AgentRuntime
from mato_loop_backend.api.errors import ApiError
from mato_loop_backend.api.routes import sessions as sessions_route
from mato_loop_backend.api.routes.media import create_transcription
from mato_loop_backend.files.service import RootFileService
from mato_loop_backend.files.storage import SessionStorage
from mato_loop_backend.media.attachments import AttachmentError, AttachmentInput, AttachmentService
from mato_loop_backend.models import AgentRun, ChatSession, Message, UserAccount
from mato_loop_backend.providers import azure_speech, lemonade
from mato_loop_backend.providers.base import ProviderError
from mato_loop_backend.providers.media import MediaService, TranscriptionResult
from mato_loop_backend.settings import InstanceLimits, SettingsStore, ToolSettings, UserPreferences


def wav_bytes() -> bytes:
    buffer = BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8_000)
        audio.writeframes(b"\x00\x00" * 80)
    return buffer.getvalue()


def upload() -> UploadFile:
    return UploadFile(
        BytesIO(wav_bytes()),
        filename="aufnahme.wav",
        headers=Headers({"content-type": "audio/wav"}),
    )


@pytest.mark.usefixtures("mock_db_settings")
async def test_direct_transcription_uses_timeout_and_returns_text(monkeypatch) -> None:
    settings = ToolSettings.model_validate(
        {"transcribeAudio": {"timeoutSeconds": 175}}
    )
    monkeypatch.setattr(
        SettingsStore,
        "load",
        AsyncMock(side_effect=[settings, InstanceLimits()]),
    )
    transcribe = AsyncMock(return_value=TranscriptionResult("Gesprochener Text"))
    monkeypatch.setattr(MediaService, "transcribe_audio", transcribe)
    db = AsyncMock(spec=AsyncSession)
    user = UserAccount(id=uuid4(), username="test", password_hash="hash")

    result = await create_transcription(upload(), db, user, "de")

    assert result == {"text": "Gesprochener Text"}
    assert transcribe.await_args.kwargs["audio"] == wav_bytes()
    assert transcribe.await_args.kwargs["language"] == "de"
    assert transcribe.await_args.kwargs["timeout"] == 175


@pytest.mark.usefixtures("mock_db_settings")
async def test_direct_transcription_returns_structured_timeout(monkeypatch) -> None:
    settings = ToolSettings.model_validate(
        {"transcribeAudio": {"timeoutSeconds": 480}}
    )
    monkeypatch.setattr(
        SettingsStore,
        "load",
        AsyncMock(side_effect=[settings, InstanceLimits()]),
    )
    monkeypatch.setattr(
        MediaService,
        "transcribe_audio",
        AsyncMock(
            side_effect=ProviderError(
                "timed out",
                code="provider.timeout",
                params={"timeoutSeconds": 480, "retryable": True},
            )
        ),
    )

    with pytest.raises(ApiError) as exc_info:
        await create_transcription(
            upload(),
            AsyncMock(spec=AsyncSession),
            UserAccount(id=uuid4(), username="test", password_hash="hash"),
        )

    assert exc_info.value.status_code == 504
    assert exc_info.value.code == "provider.timeout"
    assert exc_info.value.params == {"timeoutSeconds": 480, "retryable": True}


@pytest.mark.usefixtures("mock_db_settings")
async def test_direct_transcription_rejects_unavailable_tool(monkeypatch) -> None:
    monkeypatch.setattr(
        SettingsStore,
        "load",
        AsyncMock(return_value=ToolSettings(tool_availability={"transcribe_audio": False})),
    )
    transcribe = AsyncMock()
    monkeypatch.setattr(MediaService, "transcribe_audio", transcribe)

    with pytest.raises(ApiError) as exc_info:
        await create_transcription(
            upload(),
            AsyncMock(spec=AsyncSession),
            UserAccount(id=uuid4(), username="test", password_hash="hash"),
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "tool.unavailable"
    transcribe.assert_not_awaited()


@pytest.mark.parametrize("project_id", [None, str(uuid4())])
async def test_session_multipart_routes_forward_attachments_without_text(monkeypatch, project_id) -> None:
    user = UserAccount(id=uuid4(), username="test", password_hash="hash")
    db = AsyncMock(spec=AsyncSession)
    create = AsyncMock(return_value={"created": True})
    send = AsyncMock(return_value={"runId": str(uuid4()), "modelName": "Test"})
    monkeypatch.setattr(sessions_route, "_create_session", create)
    monkeypatch.setattr(sessions_route, "_send_message", send)
    monkeypatch.setattr(sessions_route, "_mutable_session", AsyncMock(return_value=object()))
    monkeypatch.setattr(
        SettingsStore, "load", AsyncMock(return_value=InstanceLimits())
    )
    create_payload = json.dumps({
        "content": "", "voiceAttachmentIndices": [0], "project_id": project_id,
    })
    send_payload = json.dumps({"content": "", "voiceAttachmentIndices": [0]})

    result = await sessions_route.create_session_with_attachments(
        create_payload, [upload()], user, db
    )
    session_id = uuid4()
    sent = await sessions_route.send_message_with_attachments(
        session_id, send_payload, [upload()], user, db
    )

    assert result == {"created": True}
    assert sent["modelName"] == "Test"
    assert create.await_args.args[0].content == ""
    assert send.await_args.args[1].content == ""
    created_audio = create.await_args.args[3][0]
    sent_audio = send.await_args.args[4][0]
    assert isinstance(created_audio, AttachmentInput)
    assert created_audio.kind == "audio"
    assert created_audio.mime_type == "audio/wav"
    assert sent_audio.filename == "aufnahme.wav"
    assert created_audio.metadata == sent_audio.metadata == {"voiceMessage": True}


@pytest.mark.parametrize("indices", [[0, 0], [-1], [1]])
async def test_voice_indices_reject_duplicates_and_out_of_range(indices) -> None:
    audio = upload()
    with pytest.raises(ApiError) as error:
        await sessions_route._attachment_uploads([audio], voice_attachment_indices=indices)
    assert error.value.code == "validation.invalid"
    assert error.value.field == "voiceAttachmentIndices"
    assert audio.file.closed


@pytest.mark.parametrize("indices", [[True], ["0"], [0.5], None])
@pytest.mark.parametrize("model", [sessions_route.SessionCreateRequest, sessions_route.SendMessageRequest])
def test_voice_indices_require_integer_list(indices, model) -> None:
    with pytest.raises(ApiError) as error:
        sessions_route._multipart_payload(
            json.dumps({"content": "", "voiceAttachmentIndices": indices}), model
        )
    assert error.value.code == "validation.invalid"
    assert error.value.field == "voiceAttachmentIndices"


async def test_voice_marker_rejects_non_audio_and_leaves_regular_audio_unmarked() -> None:
    text = UploadFile(BytesIO(b"hello"), filename="note.txt")
    with pytest.raises(ApiError) as error:
        await sessions_route._attachment_uploads([text], voice_attachment_indices=[0])
    assert error.value.code == "validation.invalid"
    assert error.value.field == "voiceAttachmentIndices"
    assert (await sessions_route._attachment_uploads([upload()]))[0].metadata is None


@pytest.mark.parametrize("outcome", [
    "success", "empty", "failure", "unavailable", "no_model", "attachment_error",
    "os_error", "timeout", "runtime_error", "cancelled", "database_error",
    "owner_disabled_before", "owner_disabled_during", "status_commit_error",
])
async def test_common_voice_transcription_preserves_persisted_audio_and_is_idempotent(
    tmp_path, monkeypatch, caplog, outcome,
) -> None:
    session = ChatSession(id=uuid4(), user_id=uuid4(), title="Test")
    storage = SessionStorage(tmp_path / "sessions")
    storage.provision(session.id)
    service = AttachmentService(storage)
    message = Message(id=uuid4(), session_id=session.id, role="user", content="Bitte")
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = session
    voice = await service.create_attachment(
        db, message, wav_bytes(), "audio/wav", kind="audio", filename="voice.wav",
        metadata={"voiceMessage": True}, position=0,
    )
    regular = await service.create_attachment(
        db, message, wav_bytes(), "audio/wav", kind="audio", filename="upload.wav", position=1,
    )
    message.attachments = [voice, regular]
    await db.commit()
    assert voice.metadata_json["voiceMessage"] is True
    original_path = await service.verified_path(voice, session.id)
    run = AgentRun(id=uuid4(), session_id=session.id, input_message_id=message.id)

    async def transcribe(_db, **kwargs):
        assert db.commit.await_count >= 2
        assert kwargs["audio"] == await service.read_audio(voice, session.id)
        assert kwargs["user_id"] == session.user_id
        assert original_path.read_bytes() == wav_bytes()
        errors = {
            "attachment_error": AttachmentError("attachment.notFound"),
            "os_error": OSError("secret-provider-response"),
            "timeout": TimeoutError("secret-provider-response"),
            "runtime_error": RuntimeError("secret-provider-response"),
            "cancelled": asyncio.CancelledError(),
            "database_error": SQLAlchemyError("secret-provider-response"),
        }
        if outcome in errors:
            raise errors[outcome]
        if outcome == "owner_disabled_during":
            db.scalar.return_value = False
            return TranscriptionResult("Must not be persisted")
        if outcome in {"failure", "no_model"}:
            raise ProviderError(
                "secret-provider-response", code=(
                    "provider.timeout" if outcome == "failure"
                    else "provider.modelCapabilityUnavailable"
                ),
            )
        return TranscriptionResult("  Gesprochener Text  " if outcome == "success" else "  ")

    db.get.return_value = session
    db.scalar.return_value = outcome != "owner_disabled_before"
    db.scalar.side_effect = lambda statement: (
        message if statement.column_descriptions[0]["entity"] is Message else db.scalar.return_value
    )
    def _mock_value():
        return service
    for _mock_target in (mock_agent_runtime, mock_agent_loop, mock_agent_titles,):
        monkeypatch.setattr(_mock_target, 'AttachmentService', _mock_value)
    monkeypatch.setattr(mock_agent_runtime, 'load_effective_tools', AsyncMock(return_value=ToolSettings(
        tool_availability={"transcribe_audio": outcome != "unavailable"},
    )))
    enqueue = AsyncMock()
    monkeypatch.setattr(mock_agent_runtime.EpisodeIndexService, "enqueue", enqueue)
    transcription = AsyncMock(side_effect=transcribe)
    monkeypatch.setattr(MediaService, "transcribe_audio", transcription)
    events = []

    async def publish(run_id, event):
        assert db.commit.await_count == 3
        assert voice.metadata_json["voiceTranscriptionStatus"]
        events.append((run_id, event))

    monkeypatch.setattr(mock_agent_runtime.run_events, "publish", publish)
    if outcome == "status_commit_error":
        db.commit.side_effect = [None, SQLAlchemyError("commit failed")]

    terminal_errors = {
        "cancelled": asyncio.CancelledError,
        "database_error": SQLAlchemyError,
        "owner_disabled_before": mock_agent_runtime.AgentRunError,
        "owner_disabled_during": mock_agent_runtime.AgentRunError,
        "status_commit_error": SQLAlchemyError,
    }
    if outcome in terminal_errors:
        with pytest.raises(terminal_errors[outcome]) as error:
            await AgentRuntime._transcribe_voice_messages(db, run)
        if outcome.startswith("owner_disabled"):
            assert str(error.value) == "auth.accountDisabled"
        assert transcription.await_count == (0 if outcome == "owner_disabled_before" else 1)
        assert original_path.read_bytes() == wav_bytes()
        assert message.content == "Bitte"
        if outcome != "status_commit_error":
            assert "voiceTranscriptionStatus" not in voice.metadata_json
        assert events == []
        return

    await AgentRuntime._transcribe_voice_messages(db, run)
    await AgentRuntime._transcribe_voice_messages(db, run)

    assert transcription.await_count == (0 if outcome == "unavailable" else 1)
    assert original_path.read_bytes() == wav_bytes()
    assert "voiceMessage" not in regular.metadata_json
    assert "voiceTranscriptionStatus" not in regular.metadata_json
    assert message.content == ("Bitte\n\nGesprochener Text" if outcome == "success" else "Bitte")
    assert voice.metadata_json["voiceTranscriptionStatus"] == (
        "completed" if outcome == "success" else "unavailable" if outcome == "unavailable" else "failed"
    )
    projected = message_dict(message)["content"]
    assert "root=files" in projected
    assert voice.file_path in projected
    assert ("transcript included in user text" in projected) == (outcome == "success")
    assert enqueue.await_count == (1 if outcome == "success" else 0)
    assert "secret-provider-response" not in caplog.text
    assert events == [(run.id, {"type": "message.updated", "messageId": str(message.id)})]


@pytest.mark.parametrize("adapter", [lemonade, azure_speech])
@pytest.mark.parametrize(("explicit", "preferred"), [(None, None), (None, "de-DE"), ("en-US", "de-DE")])
async def test_transcription_releases_model_lookup_transaction_before_provider_io(monkeypatch, adapter, explicit, preferred) -> None:
    db = AsyncMock(spec=AsyncSession)
    resolved = SimpleNamespace(
        base_url="http://media", api_key=None, model=SimpleNamespace(model_id="stt"),
        provider=SimpleNamespace(adapter_type="azure-speech" if adapter is azure_speech else "lemonade"),
    )
    monkeypatch.setattr(MediaService, "resolve_model", AsyncMock(return_value=resolved))
    monkeypatch.setattr(SettingsStore, "load", AsyncMock(return_value=UserPreferences(preferred_language=preferred)))

    async def transcribe(**_kwargs):
        db.commit.assert_awaited_once()
        assert _kwargs["language"] == (explicit or preferred)
        return "Spoken text"

    monkeypatch.setattr(adapter, "transcribe_audio", transcribe)
    result = await MediaService.transcribe_audio(db, audio=wav_bytes(), language=explicit, user_id=uuid4())
    assert result.text == "Spoken text"


async def test_session_multipart_retries_skip_attachment_classification(monkeypatch) -> None:
    user = UserAccount(id=uuid4(), username="test", password_hash="hash")
    db = AsyncMock(spec=AsyncSession)
    run_id = uuid4()
    duplicate_response = {"runId": str(run_id), "message": {"attachments": [{"id": "old"}]}}
    duplicate = AsyncMock(return_value=duplicate_response)
    load = AsyncMock()
    uploads = AsyncMock()
    monkeypatch.setattr(sessions_route, "_duplicate_submission_response", duplicate)
    monkeypatch.setattr(sessions_route, "_mutable_session", AsyncMock(return_value=object()))
    monkeypatch.setattr(SettingsStore, "load", load)
    monkeypatch.setattr(sessions_route, "_attachment_uploads", uploads)
    payload = json.dumps({"content": "retry", "run_id": str(run_id)})

    created = await sessions_route.create_session_with_attachments(
        payload, [upload()], user, db
    )
    session_id = uuid4()
    sent = await sessions_route.send_message_with_attachments(
        session_id, payload, [upload()], user, db
    )

    assert created is duplicate_response
    assert sent is duplicate_response
    load.assert_not_awaited()
    uploads.assert_not_awaited()


async def test_session_attachment_processing_limit_returns_413(monkeypatch) -> None:
    monkeypatch.setattr(sessions_route, "_mutable_session", AsyncMock(return_value=object()))
    monkeypatch.setattr(
        SettingsStore, "load", AsyncMock(return_value=InstanceLimits())
    )
    monkeypatch.setattr(
        sessions_route,
        "_send_message",
        AsyncMock(side_effect=AttachmentError("attachment.textTooLarge")),
    )

    with pytest.raises(ApiError) as exc_info:
        await sessions_route.send_message_with_attachments(
            uuid4(),
            json.dumps({"content": "Analysiere das Log"}),
            [upload()],
            UserAccount(id=uuid4(), username="test", password_hash="hash"),
            AsyncMock(spec=AsyncSession),
        )

    assert exc_info.value.status_code == 413
    assert exc_info.value.code == "attachment.textTooLarge"


async def test_large_attachment_is_streamed_without_media_processing(tmp_path, monkeypatch):
    class Chunked(BytesIO):
        def read(self, size=-1):
            assert 0 < size <= 1024 * 1024
            return super().read(size)

    session = ChatSession(id=uuid4(), user_id=uuid4())
    storage = SessionStorage(tmp_path / "sessions")
    storage.provision(session.id, user_id=session.user_id)
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = session
    service = AttachmentService(storage, settings=InstanceLimits())
    source = Chunked(b"raw file")
    monkeypatch.setattr(sessions_route, "_mutable_session", AsyncMock(return_value=session))
    monkeypatch.setattr(SettingsStore, "load", AsyncMock(return_value=InstanceLimits()))

    async def send(_id, _payload, _user, db, inputs):
        item = inputs[0]
        result = await service.create_attachment(db, Message(id=uuid4(), session_id=session.id), item.data, item.mime_type, kind=item.kind, filename=item.filename, position=0)
        return {"path": result.file_path}

    monkeypatch.setattr(sessions_route, "_send_message", send)
    result = await sessions_route.send_message_with_attachments(
        session.id, json.dumps({"content": "Datei"}), [UploadFile(source, filename="large.bin")],
        UserAccount(id=session.user_id), db,
    )
    assert (RootFileService(session.id, session_storage=storage).read_bytes("session", result["path"], limit=100), source.closed) == (b"raw file", True)


async def test_attachment_processing_has_no_additional_file_size_ceiling(tmp_path, monkeypatch):
    monkeypatch.setattr("mato_loop_backend.media.attachments.MAX_FILE_ATTACHMENT_BYTES", 4)
    settings = InstanceLimits()
    inputs = await sessions_route._attachment_uploads([
        upload(),
    ], settings)
    session = ChatSession(id=uuid4(), user_id=uuid4())
    storage = SessionStorage(tmp_path / "sessions")
    storage.provision(session.id, user_id=session.user_id)
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = session
    item = inputs[0]
    result = await AttachmentService(storage, settings=settings).create_attachment(
        db, Message(id=uuid4(), session_id=session.id),
        item.data, item.mime_type, kind=item.kind, filename=item.filename, position=0,
    )
    assert result.kind == "audio"


async def test_attachment_upload_count_is_bounded() -> None:
    files = [upload() for _ in range(9)]

    with pytest.raises(ApiError) as exc_info:
        await sessions_route._attachment_uploads(files)

    assert exc_info.value.code == "validation.tooManyItems"
    assert exc_info.value.params == {"maxItems": 8}


async def test_invalid_image_upload_logs_safe_decoder_context(caplog) -> None:
    data = b"\xff\xd8\xffprivate-image-bytes"
    image = UploadFile(
        BytesIO(data),
        filename="private-location-name.jpeg",
        headers=Headers({"content-type": "image/jpeg"}),
    )
    caplog.set_level(logging.WARNING, logger=sessions_route.__name__)

    with pytest.raises(ApiError) as exc_info:
        await sessions_route._attachment_uploads([image])

    assert exc_info.value.code == "attachment.invalidImage"
    assert f"size_bytes={len(data)}" in caplog.text
    assert "content_type='image/jpeg'" in caplog.text
    assert "detected_type='image/jpeg'" in caplog.text
    assert "suffix='.jpeg'" in caplog.text
    assert "diagnostic='verify:" in caplog.text
    assert "private-location-name" not in caplog.text
    assert "private-image-bytes" not in caplog.text


async def test_runtime_preserves_stored_audio_when_commit_outcome_is_uncertain(
    monkeypatch,
) -> None:
    pass
    from mato_loop_backend.models import MessageAttachment
    from mato_loop_backend.settings import AiSettings
    from mato_loop_backend.settings.models import LlmProviderConfig, ModelConfig

    provider = LlmProviderConfig(name="Test", adapter_type="openai-compatible")
    model = ModelConfig(provider_id=provider.id, model_id="test")
    settings = AiSettings(
        providers=[provider], models=[model], default_model_id=model.id
    )
    monkeypatch.setattr(
        SettingsStore,
        "load",
        AsyncMock(
            side_effect=lambda _db, settings_type, **_kwargs: (
                settings if settings_type.settings_key == "ai" else InstanceLimits()
            )
        ),
    )
    monkeypatch.setattr(SettingsStore, "load_optional", AsyncMock(return_value=None))
    attachment = MessageAttachment(
        id=uuid4(),
        message_id=uuid4(),
        position=0,
        kind="audio",
        mime_type="audio/wav",
        size_bytes=100,
        sha256="a" * 64,
        file_path=(
            "attachments/00000000-0000-4000-8000-000000000000/audio.wav"
        ),
    )
    service = AsyncMock()
    service.create_attachment.return_value = attachment
    def _mock_value(**_kwargs):
        return service
    for _mock_target in (mock_agent_runtime, mock_agent_loop, mock_agent_titles,):
        monkeypatch.setattr(_mock_target, 'AttachmentService', _mock_value)
    db = AsyncMock(spec=AsyncSession)
    db.commit.side_effect = RuntimeError("commit failed")
    session = ChatSession(
        id=uuid4(),
        user_id=uuid4(),
        title="Test",
        model_config_id=model.id,
    )
    db.scalar.side_effect = [session, None, True, True]

    with pytest.raises(RuntimeError, match="commit failed"):
        await AgentRuntime.start(
            db,
            session,
            "Nachricht",
            attachments=[AttachmentInput(wav_bytes(), "audio/wav", "audio.wav", "audio")],
        )

    service.create_attachment.assert_awaited_once()
    service.discard.assert_not_awaited()
    db.rollback.assert_awaited_once()


async def test_runtime_waits_for_commit_before_propagating_cancellation(
    monkeypatch,
) -> None:
    pass
    from mato_loop_backend.models import MessageAttachment
    from mato_loop_backend.settings import AiSettings
    from mato_loop_backend.settings.models import LlmProviderConfig, ModelConfig

    provider = LlmProviderConfig(name="Test", adapter_type="openai-compatible")
    model = ModelConfig(provider_id=provider.id, model_id="test")
    settings = AiSettings(
        providers=[provider], models=[model], default_model_id=model.id
    )
    monkeypatch.setattr(
        SettingsStore,
        "load",
        AsyncMock(
            side_effect=lambda _db, settings_type, **_kwargs: (
                settings if settings_type.settings_key == "ai" else InstanceLimits()
            )
        ),
    )
    monkeypatch.setattr(SettingsStore, "load_optional", AsyncMock(return_value=None))
    attachment = MessageAttachment(
        id=uuid4(),
        message_id=uuid4(),
        position=0,
        kind="audio",
        mime_type="audio/wav",
        size_bytes=100,
        sha256="a" * 64,
        file_path=(
            "attachments/00000000-0000-4000-8000-000000000000/audio.wav"
        ),
    )
    service = AsyncMock()
    service.create_attachment.return_value = attachment
    def _mock_value(**_kwargs):
        return service
    for _mock_target in (mock_agent_runtime, mock_agent_loop, mock_agent_titles,):
        monkeypatch.setattr(_mock_target, 'AttachmentService', _mock_value)
    commit_started = asyncio.Event()
    finish_commit = asyncio.Event()
    committed = False

    async def commit() -> None:
        nonlocal committed
        commit_started.set()
        await finish_commit.wait()
        committed = True

    db = AsyncMock(spec=AsyncSession)
    db.commit.side_effect = commit
    session = ChatSession(
        id=uuid4(),
        user_id=uuid4(),
        title="Test",
        model_config_id=model.id,
    )
    db.scalar.side_effect = [session, None, True, True]
    schedule = MagicMock()
    monkeypatch.setattr(AgentRuntime, "_schedule_run", schedule)
    task = asyncio.create_task(
        AgentRuntime.start(
            db,
            session,
            "Nachricht",
            attachments=[AttachmentInput(wav_bytes(), "audio/wav", "audio.wav", "audio")],
        )
    )
    await commit_started.wait()

    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    finish_commit.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert committed is True
    schedule.assert_called_once()
    service.discard.assert_not_awaited()
    db.rollback.assert_not_awaited()
