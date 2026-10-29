import asyncio
import json
import wave
import zipfile
from copy import deepcopy
from datetime import UTC, datetime
from io import BytesIO
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from context_helpers import _build_context
from PIL import Image
from runtime_helpers import _runtime_setup
from sqlalchemy.ext.asyncio import AsyncSession

import mato_loop_backend.agent.loop as mock_agent_loop
import mato_loop_backend.agent.run_support as mock_agent_run_support
import mato_loop_backend.agent.runtime as mock_agent_runtime
import mato_loop_backend.agent.summarization as mock_agent_summarization
import mato_loop_backend.agent.titles as mock_agent_titles
import mato_loop_backend.agent.tool_round as mock_tool_round
import mato_loop_backend.files.access as mock_files_access
import mato_loop_backend.tools.access as mock_tools_access
import mato_loop_backend.tools.catalog as mock_tools_catalog
import mato_loop_backend.tools.files as mock_tools_files
import mato_loop_backend.tools.media as mock_tools_media
import mato_loop_backend.tools.sessions as mock_tools_sessions
from mato_loop_backend.agent.context import ContextManager
from mato_loop_backend.agent.projection import ContextProjection, message_dict
from mato_loop_backend.agent.runtime import AgentRuntime
from mato_loop_backend.files.service import RootFileError, RootFileService
from mato_loop_backend.files.storage import SessionStorage
from mato_loop_backend.integrations.openscad.adapter import _zip_project
from mato_loop_backend.media.attachments import (
    AttachmentError,
    AttachmentService,
    latest_image_references,
    pending_attachments,
)
from mato_loop_backend.models import (
    AgentRun,
    ChatSession,
    Message,
    MessageAttachment,
    WebSearchPolicy,
)
from mato_loop_backend.permissions.policy import PermissionDecision
from mato_loop_backend.providers import service as provider_service
from mato_loop_backend.providers.base import (
    ProviderError,
    ProviderImageInput,
    ProviderResult,
    ToolCall,
)
from mato_loop_backend.providers.media import MediaResult
from mato_loop_backend.settings import (
    AgentRuntimeSettings,
    InstanceLimits,
    IntegrationSettings,
    SettingsStore,
    ToolSettings,
    TrustLevel,
)
from mato_loop_backend.settings.models import IntegrationInstance, LlmProviderConfig, ModelConfig
from mato_loop_backend.tools.arguments import ProvideFileArguments
from mato_loop_backend.tools.contracts import ToolAttachment, ToolResult


def png(color):
    output = BytesIO()
    Image.new("RGB", (3, 2), color).save(output, format="PNG")
    return output.getvalue()


def tool_message(session, run_id):
    return Message(
        id=uuid4(),
        session_id=session.id,
        role="tool",
        content="Image generated.",
        trust_level="public",
        created_at=datetime.now(UTC),
        attachments=[],
        provider_data={"_matoloop": {"runId": str(run_id), "fileOutputsPending": True}},
    )


@pytest.mark.parametrize("with_action", [False, True])
async def test_empty_provided_final_file_never_creates_invalid_attachment(tmp_path, with_action):
    session = ChatSession(id=uuid4(), user_id=uuid4())
    storage = SessionStorage(tmp_path / "sessions")
    storage.provision(session.id)
    public = RootFileService(session.id, session_storage=storage)
    public.write_file("session", "notes.txt", "nonempty")
    original = public.working_revision("notes.txt")
    run_id = uuid4()
    message = tool_message(session, run_id)
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = session
    service = AttachmentService(storage, run_id=run_id)
    await service.create_reference(
        db,
        message,
        file_root_id=session.id,
        file_id=original.file_id,
        file_revision=original.revision,
        file_path=original.path,
        filename="notes.txt",
        position=0,
        category="downloaded",
    )
    if with_action:
        message.provider_data["_matoloop"]["fileActions"] = [
            {
                "root": "session",
                "rootIdentity": str(session.id),
                "operation": "write",
                "path": "notes.txt",
                "filename": "notes.txt",
                "sizeBytes": original.size_bytes,
            }
        ]
    RootFileService(session.id, session_storage=storage, run_id=run_id).replace_text_file(
        "session", "notes.txt", "", original.sha256
    )
    db.scalars.return_value = Mock(all=Mock(return_value=[message]))
    await service.finalize_run(db, session, run_id)
    await service.finalize_run(db, session, run_id)
    db.add.assert_not_called()
    assert message.attachments == []
    state = message.provider_data["_matoloop"]
    assert "pendingAttachments" not in state
    if with_action:
        assert state["fileOutputsPending"] is False
        assert state["fileActions"][0]["sizeBytes"] == 0
    else:
        assert state["fileOutputsHidden"] is True


async def test_provided_published_identity_survives_rename_and_old_path_reuse(tmp_path):
    session = ChatSession(id=uuid4(), user_id=uuid4())
    storage = SessionStorage(tmp_path / "sessions")
    storage.provision(session.id)
    public = RootFileService(session.id, session_storage=storage)
    public.write_file("session", "notes.txt", "original")
    original = public.working_revision("notes.txt")
    run_id = uuid4()
    message = tool_message(session, run_id)
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = session
    service = AttachmentService(storage, run_id=run_id)
    await service.create_reference(
        db,
        message,
        file_root_id=session.id,
        file_id=original.file_id,
        file_revision=original.revision,
        file_path=original.path,
        filename="notes.txt",
        position=0,
        category="downloaded",
        metadata={"toolOutputPresentation": "internal", "pageNumber": 1},
    )
    public.rename_path("session", "notes.txt", "renamed.txt")
    public.write_file("session", "notes.txt", "unrelated replacement")
    RootFileService(session.id, session_storage=storage, run_id=run_id).replace_text_file(
        "session", "renamed.txt", "updated original", original.sha256
    )
    db.scalars.return_value = Mock(all=Mock(return_value=[message]))
    await service.finalize_run(db, session, run_id)
    await service.finalize_run(db, session, run_id)
    db.add.assert_called_once()
    attachment = db.add.call_args.args[0]
    assert attachment.file_id == original.file_id
    assert attachment.file_path == "renamed.txt"
    assert attachment.file_revision == 2
    assert attachment.metadata_json == {"toolOutputPresentation": "internal"}
    assert service.read_source(attachment, session.id) == b"updated original"
    assert public.open_file("session", "notes.txt").read() == b"unrelated replacement"


@pytest.mark.parametrize("existing", [False, True])
async def test_cancelled_managed_image_write_keeps_last_completed_run_bytes(
    tmp_path, monkeypatch, existing
):
    session = ChatSession(id=uuid4(), user_id=uuid4())
    storage = SessionStorage(tmp_path / "sessions")
    storage.provision(session.id)
    public = RootFileService(session.id, session_storage=storage)
    original = public.create_output_file("image.png", png("red")) if existing else None
    run_id = uuid4()
    service = AttachmentService(storage, run_id=run_id)
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = session
    completed, release = Event(), Event()
    written = []
    method = "_write_run_file" if existing else "_create_run_file_stream"
    write = getattr(RootFileService, method)

    def paused(files, *args, **kwargs):
        revision = write(files, *args, **kwargs)
        written.append(revision if existing else files.working_revision(revision.path))
        completed.set()
        assert release.wait(10)
        return revision

    monkeypatch.setattr(RootFileService, method, paused)
    task = asyncio.create_task(
        service.create_image(
            db,
            tool_message(session, run_id),
            png("blue"),
            "image/png",
            filename="image.png",
            position=0,
            category="generated",
            purpose="artifact",
            source_file_root_id=session.id if original else None,
            file_id=original.file_id if original else None,
        )
    )
    try:
        assert await asyncio.to_thread(completed.wait, 10)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    finalized = public.finalize_run(run_id)
    assert len(finalized) == 1
    assert finalized[0].file_id == written[0].file_id
    assert public.open_revision_by_id(finalized[0].file_id, finalized[0].revision).read() == png(
        "blue"
    )
    if original:
        assert finalized[0].file_id == original.file_id
        assert public.open_revision_by_id(original.file_id, original.revision).read() == png("red")
    db.add.assert_not_called()


async def test_superseded_pending_images_do_not_exhaust_context_or_provider_budget(
    tmp_path, monkeypatch
):
    session = ChatSession(id=uuid4(), user_id=uuid4())
    run_id = uuid4()
    storage = SessionStorage(tmp_path / "sessions")
    storage.provision(session.id)
    service = AttachmentService(storage, run_id=run_id)
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = session
    records = [
        Message(
            id=uuid4(),
            session_id=session.id,
            role="user",
            content="Inspect Y and X.",
            attachments=[],
        )
    ]
    outputs = []
    x = None
    for index in range(9):
        message = tool_message(session, run_id)
        message.tool_call_id = str(message.id)
        records.append(
            Message(
                id=uuid4(),
                session_id=session.id,
                role="assistant",
                content="",
                tool_calls=[
                    {"id": message.tool_call_id, "name": "generate_image", "arguments": {}}
                ],
            )
        )
        output = await service.create_image(
            db,
            message,
            png((index * 20, 20, 30)),
            "image/png",
            filename="Y.png" if index == 0 else "X.png",
            position=0,
            category="generated",
            purpose="artifact",
            source_file_root_id=session.id if x else None,
            file_id=x.file_id if x else None,
        )
        if index > 0:
            x = output
        outputs.append(output)
        records.append(message)
    raw = [message_dict(message) for message in records]
    assert sum(len(message["attachments"]) for message in raw) == 9
    db.scalar.return_value = None
    monkeypatch.setattr(provider_service, "AttachmentService", lambda: service)
    context = await _build_context(
        db,
        chat_session=session,
        records=records,
        system_prompt="Inspect both current images.",
        tool_schemas=[],
        model=ModelConfig(provider_id=uuid4(), model_id="test", supports_image_input=True),
        settings=AgentRuntimeSettings(),
        summarize=AsyncMock(),
        attachment_service=service,
    )
    expected_ids = {str(outputs[0].id), str(outputs[-1].id)}
    assert {
        item["id"] for message in context.messages for item in message.get("attachments", [])
    } == expected_ids
    expected_urls = {
        provider_service._read_image_data_url(message_dict(records[index])["attachments"][0])
        for index in (2, len(records) - 1)
    }
    for messages in (raw, context.messages):
        _, media = await provider_service._provider_projection(
            messages, supports_image_input=True, target_trust_level=TrustLevel.PUBLIC
        )
        assert len(media) == 2
        assert {item.data_url for item in media} == expected_urls
        assert all(item.status == "ready" for item in media)
    assert sum(len(message["attachments"]) for message in raw) == 9
    raw[-1]["attachments"][0]["sha256"] = "0" * 64
    _, media = await provider_service._provider_projection(
        raw, supports_image_input=True, target_trust_level=TrustLevel.PUBLIC
    )
    assert sorted(item.status for item in media) == ["ready", "unavailable"]


def test_latest_image_filter_uses_source_order_for_compacted_references():
    identity = {
        "kind": "image",
        "session_id": str(uuid4()),
        "file_root_id": str(uuid4()),
        "file_id": str(uuid4()),
    }
    messages = [
        {
            "role": "system",
            "attachments": [{**identity, "id": "new", "source_order": 9, "historical": True}],
        },
        {"role": "tool", "attachments": [{**identity, "id": "old", "source_order": 3}]},
    ]
    filtered = latest_image_references(messages)
    assert filtered[0]["attachments"][0]["id"] == "new"
    assert filtered[1]["attachments"] == []
    assert messages[1]["attachments"][0]["id"] == "old"


def test_inactive_run_draft_cannot_displace_published_image_reference():
    identity = {
        "kind": "image",
        "session_id": str(uuid4()),
        "file_root_id": str(uuid4()),
        "file_id": str(uuid4()),
    }
    messages = [
        {"role": "user", "attachments": [{**identity, "id": "published", "file_revision": 1}]},
        {
            "role": "tool",
            "attachments": [
                {**identity, "id": "inactive", "file_revision": None, "run_id": str(uuid4())}
            ],
        },
    ]
    filtered = latest_image_references(messages, run_id=uuid4())
    assert filtered[0]["attachments"][0]["id"] == "published"
    assert filtered[1]["attachments"] == []


async def test_pending_images_edit_in_place_project_and_finalize_once(tmp_path, monkeypatch):
    session = ChatSession(id=uuid4(), user_id=uuid4(), title="Test")
    run_id = uuid4()
    storage = SessionStorage(tmp_path / "sessions")
    storage.provision(session.id)
    service = AttachmentService(storage, run_id=run_id)
    files = RootFileService(session.id, session_storage=storage, run_id=run_id)
    public_files = RootFileService(session.id, session_storage=storage)
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = session
    first_message = tool_message(session, run_id)
    first = await service.create_image(
        db,
        first_message,
        png("red"),
        "image/png",
        filename="image.png",
        position=0,
        category="generated",
        purpose="artifact",
    )
    assert not isinstance(first, MessageAttachment)
    assert first.file_revision is None
    assert first_message.attachments == []
    db.add.assert_not_called()
    json.dumps(first_message.provider_data)
    assert public_files.list_revisions(first.file_path) == ()
    assert await service.read_image(first, session.id, current_only=True) == png("red")
    projected = message_dict(first_message)
    assert first.file_path in projected["content"]
    assert projected["attachments"][0]["run_id"] == str(run_id)
    monkeypatch.setattr(provider_service, "AttachmentService", lambda: service)
    assert provider_service._read_image_data_url(projected["attachments"][0]).startswith(
        "data:image/png;base64,"
    )
    transported, media = await provider_service._provider_projection(
        [{"role": "user", "content": "Inspect the image."}, projected],
        supports_image_input=True,
        target_trust_level=TrustLevel.PUBLIC,
    )
    assert "attachments" not in transported[1]
    assert media[0].status == "ready"

    db.scalar.return_value = None
    db.scalars.return_value = Mock(all=Mock(return_value=[first_message]))
    assert (await service.resolve_path(db, files, first.file_path)).id == first.id
    db.scalar.return_value = session
    second_message = tool_message(session, run_id)
    second = await service.create_image(
        db,
        second_message,
        png("blue"),
        "image/png",
        filename="image.png",
        position=0,
        category="generated",
        purpose="artifact",
        source_file_root_id=first.file_root_id,
        file_id=first.file_id,
    )
    assert second.file_id == first.file_id
    assert second.file_revision is None
    assert await service.read_image(second, session.id, current_only=True) == png("blue")
    with pytest.raises(AttachmentError, match="attachment.notFound"):
        await service.read_image(first, session.id, current_only=True)
    with pytest.raises(AttachmentError):
        provider_service._read_image_data_url(projected["attachments"][0])
    with pytest.raises(AttachmentError):
        await service.read_image(second, uuid4(), current_only=True)
    wrong_run = SimpleNamespace(**{**vars(second), "run_id": uuid4()})
    with pytest.raises(AttachmentError):
        await service.read_image(wrong_run, session.id, current_only=True)
    assert files.working_revision(second.file_path).sha256 == second.sha256

    db.scalars.return_value = Mock(all=Mock(return_value=[first_message, second_message]))
    await service.finalize_run(db, session, run_id)
    public_attachment = db.add.call_args.args[0]
    assert isinstance(public_attachment, MessageAttachment)
    assert public_attachment.id == second.id
    assert public_attachment.file_revision == 1
    assert public_attachment.sha256 == second.sha256
    assert first.file_revision is None
    assert first.sha256 != second.sha256
    assert first_message.provider_data["_matoloop"]["fileOutputsHidden"] is True
    assert first_message.provider_data["_matoloop"]["fileOutputsPending"] is False
    assert second_message.provider_data["_matoloop"]["fileOutputsPending"] is False
    assert pending_attachments(second_message) == []
    assert await AttachmentService(storage).read_image(public_attachment, session.id) == png("blue")
    await service.finalize_run(db, session, run_id)
    db.add.assert_called_once()
    assert len(public_files.list_revisions(second.file_path)) == 1


async def test_provided_working_file_and_actions_publish_only_final_output(tmp_path, monkeypatch):
    session = ChatSession(id=uuid4(), user_id=uuid4(), title="Test")
    run_id = uuid4()
    storage = SessionStorage(tmp_path / "sessions")
    storage.provision(session.id)
    files = RootFileService(session.id, session_storage=storage, run_id=run_id)
    files.write_file("session", "model.scad", "cube(1);")
    working = files.working_revision("model.scad")
    monkeypatch.setattr(mock_files_access, 'project_read_files', AsyncMock(return_value=(files, session.id)))
    monkeypatch.setattr(mock_files_access, 'filesystem_permission', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(mock_tools_access, 'allow', AsyncMock(return_value=True))
    provided = await mock_tools_files._provide_file(
        ProvideFileArguments(root="session", path="model.scad"),
        SimpleNamespace(run_id=run_id, root_files=files, session=session),
    )
    assert provided.file_actions[0].file_revision is None
    assert provided.file_actions[0].file_id == str(working.file_id)
    assert "downloadUrl" not in json.loads(provided.content)
    service = AttachmentService(storage, run_id=run_id)
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = session
    messages = [tool_message(session, run_id) for _ in range(2)]
    for message in messages:
        message.provider_data["_matoloop"]["fileActions"] = [
            {
                "operation": "write",
                "root": "session",
                "rootIdentity": str(session.id),
                "path": "model.scad",
                "filename": "model.scad",
                "sizeBytes": 8,
            }
        ]
        await service.create_reference(
            db,
            message,
            file_root_id=session.id,
            file_id=working.file_id,
            file_revision=None,
            file_path=working.path,
            filename="model.scad",
            position=0,
            category="downloaded",
        )
    db.add.assert_not_called()
    files.append_output_revision(working.file_id, b"cube(20);")
    archive, _, _ = _zip_project(files, ".")
    with archive, zipfile.ZipFile(archive) as project:
        assert project.read("model.scad") == b"cube(20);"
    db.scalars.return_value = Mock(all=Mock(return_value=messages))
    await service.finalize_run(db, session, run_id)
    db.add.assert_called_once()
    attachment = db.add.call_args.args[0]
    assert attachment.sha256 == files.working_revision("model.scad").sha256
    assert attachment.size_bytes == len(b"cube(20);")
    assert attachment.file_revision == 1
    assert messages[0].provider_data["_matoloop"]["fileOutputsHidden"] is True
    assert messages[1].provider_data["_matoloop"]["fileOutputsPending"] is False
    assert messages[1].provider_data["_matoloop"]["fileActions"] == []


async def test_provided_reference_retains_final_revision_without_creating_attachment(tmp_path, monkeypatch):
    session = ChatSession(id=uuid4(), user_id=uuid4())
    run_id = uuid4()
    storage = SessionStorage(tmp_path / "sessions")
    storage.provision(session.id)
    files = RootFileService(session.id, session_storage=storage, run_id=run_id)
    files.create_directory("session", "reports")
    files.write_file("session", "reports/notes.txt", "original")
    source = files.working_revision("reports/notes.txt")
    monkeypatch.setattr(mock_files_access, 'project_read_files', AsyncMock(return_value=(files, session.id)))
    monkeypatch.setattr(mock_files_access, 'filesystem_permission', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(mock_tools_access, 'allow', AsyncMock(return_value=True))
    provided = await mock_tools_files._provide_file(
        ProvideFileArguments(root="session", path="reports/notes.txt"),
        SimpleNamespace(run_id=run_id, root_files=files, session=session),
    )
    assert not provided.is_error
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = session
    service = AttachmentService(storage, run_id=run_id)
    message, _, _ = await mock_tool_round.persist_tool_result(
        db, SimpleNamespace(id=run_id), session, ToolCall("provide", "provide_file", {}),
        content=provided.content, metadata=provided.metadata, attachments=provided.attachments,
        file_actions=provided.file_actions, trust_level=TrustLevel.PUBLIC,
        ephemeral_output=False, limits=InstanceLimits(), attachment_service=lambda **_kwargs: service,
    )
    files.append_output_revision(source.file_id, b"edited")
    db.scalars.return_value = Mock(all=Mock(return_value=[message]))
    await service.finalize_run(db, session, run_id)
    db.add.assert_called_once()
    action, = message.provider_data["_matoloop"]["fileActions"]
    assert (action["fileId"], action["fileRevision"], action["sizeBytes"]) == (str(source.file_id), 1, 6)


@pytest.mark.parametrize("quota_mib,used_mib", [(1, 2), (1000, 600)])
@pytest.mark.parametrize("recovery", [False, True])
async def test_runtime_finalization_preserves_reserved_outputs_after_quota_reduction(
    tmp_path, monkeypatch, quota_mib, used_mib, recovery
):
    session = ChatSession(id=uuid4(), user_id=uuid4(), title="Test")
    run = AgentRun(id=uuid4(), session_id=session.id, status="failed")
    limits = InstanceLimits(user_quota_mib=quota_mib, max_upload_mib=1)
    storage = SessionStorage(tmp_path / "sessions")
    storage.provision(session.id, user_id=session.user_id)
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = session
    message = tool_message(session, run.id)
    await AttachmentService(storage, run_id=run.id, settings=limits).create_image(
        db, message, png("red"), "image/png", position=0, category="generated", purpose="artifact"
    )
    db.get.return_value = session
    db.scalar.return_value = SimpleNamespace(data=limits.model_dump())
    db.scalars.return_value = Mock(all=Mock(return_value=[message]))
    def _mock_value(**kwargs):
        return AttachmentService(storage, **kwargs)
    for _mock_target in (mock_agent_runtime, mock_agent_loop, mock_agent_titles,):
        monkeypatch.setattr(_mock_target, 'AttachmentService', _mock_value)
    if recovery:
        db.get.side_effect = lambda model, _id: session if model is ChatSession else run
        db.scalars.side_effect = lambda statement: Mock(
            all=Mock(
                return_value={
                    AgentRun: [],
                    ChatSession: [session],
                    Message: [message],
                }[statement.column_descriptions[0]["entity"]]
            )
        )
        def _mock_value(*args, **kwargs):
            return RootFileService(*args, session_storage=storage, **kwargs)
        for _mock_target in (mock_agent_runtime, mock_agent_loop,):
            monkeypatch.setattr(_mock_target, 'RootFileService', _mock_value)
    # Simulate existing usage without allocating hundreds of MiB in the test container.
    monkeypatch.setattr(RootFileService, "_session_usage", lambda _self: used_mib * 1024 * 1024)
    if recovery:
        await AgentRuntime.recover(db)
        assert message.provider_data["_matoloop"]["fileOutputsPending"] is False
        assert db.add.call_count == 1
    else:
        await AgentRuntime._finalize_run_files(db, run)
        assert db.add.call_args.args[0].file_revision == 1
        assert message.provider_data["_matoloop"]["fileOutputsPending"] is False


@pytest.mark.parametrize("bound", [False, True])
async def test_actual_context_projection_does_not_excerpt_replaced_upload(tmp_path, bound):
    session = ChatSession(id=uuid4(), user_id=uuid4(), title="Test")
    storage = SessionStorage(tmp_path / "sessions")
    storage.provision(session.id)
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = session
    message = Message(
        id=uuid4(), session_id=session.id, role="user", content="Read my notes.", attachments=[]
    )
    service = AttachmentService(storage)
    attachment = await service.create_attachment(
        db, message, b"OLD_SECRET", "text/plain", kind="text", filename="notes.txt", position=0
    )
    message.attachments.append(attachment)
    run_id = uuid4()
    files = RootFileService(session.id, session_storage=storage, run_id=run_id)
    files.append_output_revision(attachment.file_id, b"NEW_TEXT")
    db.scalar.return_value = None
    projection = await _build_context(
        db,
        chat_session=session,
        records=[message],
        system_prompt="Help with notes.",
        tool_schemas=[],
        model=ModelConfig(provider_id=uuid4(), model_id="test"),
        settings=AgentRuntimeSettings(),
        summarize=AsyncMock(),
        attachment_service=AttachmentService(storage, run_id=run_id if bound else None),
    )
    assert "OLD_SECRET" not in json.dumps(projection.messages)
    assert service.read_derived_text(attachment, session.id) == "OLD_SECRET"


async def test_other_project_session_reads_its_active_session_root(tmp_path):
    project_id, user_id, run_id = uuid4(), uuid4(), uuid4()
    session = ChatSession(id=uuid4(), user_id=user_id, project_id=project_id)
    sibling = ChatSession(id=uuid4(), user_id=user_id, project_id=project_id)
    storage = SessionStorage(tmp_path / "sessions")
    storage.provision(session.id, project_id)
    storage.provision(sibling.id, project_id)
    public = RootFileService(session.id, project_id=project_id, session_storage=storage)
    public.write_file("session", "notes.txt", "OLD_SECRET")
    files = RootFileService(
        session.id, project_id=project_id, session_storage=storage, run_id=run_id
    )
    sibling_files = RootFileService(
        sibling.id, project_id=project_id, session_storage=storage, run_id=run_id
    )
    sibling_files.write_file("session", "notes.txt", "NEW_TEXT")
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = sibling
    selected, target_id = await mock_files_access.project_read_files(
        SimpleNamespace(session_id=sibling.id, root="session"),
        SimpleNamespace(session=session, root_files=files, db=db),
    )
    assert target_id == sibling.id
    assert selected.run_id == run_id
    assert selected.open_file("session", "notes.txt").read() == b"NEW_TEXT"
    assert files.open_file("session", "notes.txt").read() == b"OLD_SECRET"
    assert public.open_file("session", "notes.txt").read() == b"OLD_SECRET"


def test_hidden_artifact_references_are_omitted_without_redacting_technical_history():
    session = ChatSession(id=uuid4(), user_id=uuid4())
    message = tool_message(session, uuid4())
    message.content = '{"diagnostics":"Exact technical history remains available."}'
    message.provider_data["_matoloop"]["fileOutputsHidden"] = True
    message.attachments.append(
        MessageAttachment(
            id=uuid4(),
            kind="image",
            purpose="artifact",
            filename="internal-views.png",
            file_path="internal-views.png",
            file_id=uuid4(),
            file_revision=1,
        )
    )
    projected = message_dict(message)
    assert projected["attachments"] == []
    assert projected["content"] == message.content
    item, _ = mock_tools_sessions._message_item(
        message,
        1000,
        {message.id: "render_openscad"},
        TrustLevel.PUBLIC,
        reference_tool_results=True,
    )
    assert "attachments" not in item
    assert "Exact technical history remains available." in item["content"]


@pytest.mark.parametrize("terminal,vision", [
    ("completed", True), ("cancelled", True), ("failed", True), ("completed", False),
    ("bounded-count", True), ("bounded-bytes", True),
    ("reused-one-image", True), ("reused-two-images", True),
])
async def test_runtime_ephemeral_images_survive_requests_without_files(
    tmp_path, monkeypatch, terminal, vision,
):

    reused_ids = terminal.startswith("reused-")
    db, run, requests, _, _ = await _runtime_setup(
        monkeypatch,
        [
            ProviderResult(tool_calls=[ToolCall("capture", "webfetch", {"url": "https://example.test/one"})]),
            ProviderResult(tool_calls=[ToolCall("capture" if reused_ids else "next", "webfetch", {"url": "https://example.test/two"})]),
            ProviderResult(content="Finished.", finish_reason="stop"),
        ],
        search_policy=WebSearchPolicy.AVAILABLE, active_supports_image_input=vision,
    )
    session = db.get.return_value
    storage = SessionStorage(tmp_path / "sessions")
    storage.provision(session.id)
    records = await AgentRuntime._records(db, session.id)
    if reused_ids:
        records[:0] = [
            Message(
                id=uuid4(), session_id=session.id, role="assistant", content="",
                tool_calls=[{"id": "capture", "name": "webfetch", "arguments": {"url": "https://example.test/old"}}],
            ),
            Message(
                id=uuid4(), session_id=session.id, role="tool", tool_call_id="capture",
                content='{"observation":"historical"}', provider_data={},
            ),
        ]
    attachments = []

    def add(item):
        if isinstance(item, Message):
            item.id, item.created_at = uuid4(), datetime.now(UTC)
            records.append(item)
        elif isinstance(item, MessageAttachment):
            attachments.append(item)

    db.add.side_effect = add
    if terminal == "bounded-count":
        _mock_value = 1
        for _mock_target in (mock_agent_run_support, mock_tool_round, mock_agent_titles,):
            monkeypatch.setattr(_mock_target, 'MAX_PROVIDER_IMAGES', _mock_value)
    elif terminal == "bounded-bytes":
        _mock_value = max(len(png("red")), len(png("blue")))
        for _mock_target in (mock_agent_run_support, mock_tool_round, mock_agent_titles,):
            monkeypatch.setattr(_mock_target, 'MAX_PROVIDER_IMAGE_BYTES', _mock_value)
    def _mock_value(*args, **kwargs):
        return RootFileService(*args, session_storage=storage, **kwargs)
    for _mock_target in (mock_agent_runtime, mock_agent_loop,):
        monkeypatch.setattr(_mock_target, 'RootFileService', _mock_value)
    def _mock_value(**kwargs):
        return AttachmentService(storage, **kwargs)
    for _mock_target in (mock_agent_runtime, mock_agent_loop, mock_agent_titles,):
        monkeypatch.setattr(_mock_target, 'AttachmentService', _mock_value)

    async def execute(call, _context, _tool):
        capture = call.arguments["url"].endswith("/one")
        return ToolResult(
            json.dumps({"observation": "capture" if capture else "inspect"}), {}, trust_level=TrustLevel.PRIVATE,
            attachments=(ToolAttachment(
                png("red" if capture else "blue"), "image/png", "browser.png", ephemeral=True,
            ),) if capture or terminal.startswith("bounded-") or terminal == "reused-two-images" else (),
        )

    monkeypatch.setattr(mock_tools_catalog.tool_registry, "_execute_definition", execute)
    estimates = []
    prepare = provider_service.ProviderService.prepare_request

    async def estimate(**kwargs):
        estimates.append((deepcopy(kwargs["messages"]), deepcopy(kwargs["image_inputs"])))
        return await prepare(**kwargs)

    monkeypatch.setattr(provider_service.ProviderService, "prepare_request", staticmethod(estimate))
    build = ContextManager.build

    async def compacted_build(*args, **kwargs):
        if len(requests) < 2 or reused_ids:
            return await build(*args, **kwargs)
        # Simulate compaction removing the source tool exchange, not the active run.
        messages = [
            {"role": "system", "content": "Earlier tool exchanges were summarized."},
            message_dict(records[0]),
        ]
        return ContextProjection(
            messages=messages, estimated_input_tokens=await kwargs["estimate_request"](messages),
            input_budget_tokens=1000,
        )

    monkeypatch.setattr(ContextManager, "build", compacted_build)
    complete = provider_service.ProviderService.complete
    observed = []
    observed_messages = []
    direct_references = []

    async def observed_complete(*args, **kwargs):
        # Optional context candidates may be estimated and rejected before this request.
        assert (kwargs["messages"], kwargs["image_inputs"]) in estimates
        for image in kwargs["image_inputs"]:
            carrier = kwargs["messages"][image.message_index]
            assert carrier["role"] == "assistant" and not carrier.get("tool_calls")
        assert all(
            "Ephemeral tool-result image" not in item["content"]
            for item in kwargs["messages"] if item["role"] == "tool"
        )
        observed.append(deepcopy(kwargs["image_inputs"]))
        observed_messages.append(deepcopy(kwargs["messages"]))
        direct_references.extend(kwargs["image_inputs"])
        if len(observed) == 3 and terminal in {"cancelled", "failed"}:
            if terminal == "cancelled":
                raise asyncio.CancelledError
            raise ProviderError("Test failure", code="provider.requestFailed")
        return await complete(*args, **kwargs)

    monkeypatch.setattr(provider_service.ProviderService, "complete", staticmethod(observed_complete))
    if terminal not in {"cancelled", "failed"}:
        await AgentRuntime._execute_loop(db, run)
    else:
        with pytest.raises(asyncio.CancelledError if terminal == "cancelled" else ProviderError):
            await AgentRuntime._execute_loop(db, run)
    assert [len(images) for images in observed] == (
        [0, 1, 2] if terminal == "reused-two-images" else [0, 1, 1] if vision else [0, 0, 0]
    )
    if vision:
        assert observed[1][0].data == png("red")
        assert observed[-1][0].data == png(
            "blue" if terminal.startswith("bounded-") or terminal == "reused-two-images" else "red"
        )
        assert all(images[0].trust_level == "private" for images in observed[1:])
        carrier = observed_messages[-1][observed[-1][0].message_index]
        assert carrier["role"] == "assistant" and "Ephemeral tool-result image" in carrier["content"]
        assert all(image.data == b"" for image in direct_references)
        if reused_ids:
            sources = [item for item in records if item.role == "assistant" and item.tool_calls][1:]
            expected = [(1, sources[0], png("red"))]
            if terminal == "reused-two-images":
                expected.insert(0, (2, sources[1], png("blue")))
            for image, (request_number, source, data) in zip(observed[-1], expected, strict=True):
                label = observed_messages[-1][image.message_index]["content"]
                assert observed_messages[-1][image.message_index - 2]["source_message_id"] == str(source.id)
                assert image.data == data
                assert f"model request {request_number}, assistant message {source.id}, call \"capture\"" in label
    else:
        assert "not sent to this model and not saved" in next(
            item.content for item in records if item.tool_call_id == "capture"
        )
    assert attachments == []
    assert all(pending_attachments(item) == [] for item in records)
    assert all(not item.attachments for item in records)
    assert next(item for item in records if item.tool_call_id == "capture" and item.provider_data).provider_data[
        "_matoloop"
    ]["fileOutputsPending"] is False
    assert "base64" not in json.dumps([message_dict(item) for item in records])
    assert "Image saved" not in json.dumps([message_dict(item) for item in records])
    assert not any(path.is_file() for path in storage.logical_root_path(session.id, None).rglob("*"))
    restarted = AsyncMock(return_value=ProviderResult(content="Restarted.", finish_reason="stop"))
    monkeypatch.setattr(provider_service.ProviderService, "complete", restarted)
    await AgentRuntime._execute_loop(db, run)
    assert restarted.await_args.kwargs["image_inputs"] == []


@pytest.mark.parametrize("persisted_count", [0, 1, 8])
def test_ephemeral_projection_keeps_user_references_and_newest_images(persisted_count):
    data = png("red")
    messages = [{"role": "user", "content": "Compare my references.", "attachments": [
        {"id": str(index), "kind": "image", "size_bytes": len(data)}
        for index in range(persisted_count)
    ]}]
    original = deepcopy(messages)
    images = {
        (uuid4(), index + 1, "reused-call", 0): ProviderImageInput(0, data, "private", "image/png")
        for index in range(8)
    }
    model = ModelConfig(provider_id=uuid4(), model_id="vision", supports_image_input=True)
    projected, direct = mock_agent_run_support._project_ephemeral_images(
        messages, images, model=model, target_trust_level=TrustLevel.PUBLIC,
    )
    assert messages == original
    assert len(direct) == 8 - persisted_count
    assert direct == list(reversed(images.values()))[:8 - persisted_count]
    assert all(projected[image.message_index]["role"] == "assistant" for image in direct)
    assert "base64" not in json.dumps(projected)
    assert mock_agent_run_support._project_ephemeral_images(
        messages, images, model=model, target_trust_level=TrustLevel.PUBLIC,
    ) == (projected, direct)


@pytest.mark.parametrize("serialize,history_key", [(provider_service._chat_body, "messages"), (provider_service._responses_body, "input")])
@pytest.mark.parametrize("source", ["pdf", "tool"])
async def test_ephemeral_images_preserve_the_provider_prefix_across_tool_rounds(serialize, history_key, source):
    provider = LlmProviderConfig(name="Test", adapter_type="openai-compatible", pseudonymization_enabled=False)
    model = ModelConfig(provider_id=provider.id, model_id="vision", supports_image_input=True)
    messages = [{"role": "system", "content": "Stable instructions"}, {"role": "user", "content": "Inspect the PDF"}]
    assistant_id = uuid4()
    if source == "tool":
        messages.extend([
            {"role": "assistant", "source_message_id": str(assistant_id), "content": "", "tool_calls": [{"id": "capture", "name": "browser_take_screenshot", "arguments": {}}]},
            {"role": "tool", "tool_call_id": "capture", "content": "Screenshot captured"},
        ])
    images = {(assistant_id, 0, "pdf-upload:test" if source == "pdf" else "capture", 1): ProviderImageInput(0, png("red"), "private", "image/png")}
    bodies = []
    for index in range(2):
        projected, direct = mock_agent_run_support._project_ephemeral_images(messages, images, model=model, target_trust_level=TrustLevel.PRIVATE)
        prepared = await provider_service.ProviderService.prepare_request(
            provider=provider, model=model, messages=projected, image_inputs=direct, tools=[],
        )
        bodies.append(serialize(prepared.request))
        messages.extend([
            {"role": "assistant", "content": "", "tool_calls": [{"id": str(index), "name": "read", "arguments": {"path": "notes.txt"}}]},
            {"role": "tool", "tool_call_id": str(index), "content": "Notes"},
        ])
    assert {**bodies[1], history_key: bodies[1][history_key][:len(bodies[0][history_key])]} == bodies[0]


@pytest.mark.parametrize("export_stl", [False, True])
async def test_runtime_two_previews_are_internal_and_explicit_stl_is_retained(
    tmp_path, monkeypatch, export_stl
):

    finalize = AgentRuntime._finalize_run_files
    agent_id = uuid4()
    integration = IntegrationInstance(
        name="OpenSCAD",
        integration_type="openscad",
        adapter_type="openscad",
        execution_agent_id=agent_id,
        config={"binary_path": "/usr/bin/openscad"},
    )
    results = []
    for index in range(2):
        results.extend(
            [
                ProviderResult(
                    tool_calls=[
                        ToolCall(
                            f"preview-{index}",
                            "render_openscad",
                            {"root": "session", "path": "model.scad"},
                        )
                    ]
                ),
                ProviderResult(
                    content=json.dumps({"verdict": "pass", "findings": [], "uncertainty": []})
                ),
            ]
        )
    if export_stl:
        results.append(
            ProviderResult(
                tool_calls=[
                    ToolCall(
                        "export",
                        "render_openscad",
                        {"root": "session", "path": "model.scad", "output": "stl"},
                    )
                ]
            )
        )
    results.append(ProviderResult(content="SCAD source ready.", finish_reason="stop"))
    db, run, requests, projections, _ = await _runtime_setup(
        monkeypatch,
        results,
        search_policy=WebSearchPolicy.AVAILABLE,
        active_supports_image_input=True,
        integration_settings=IntegrationSettings(instances=[integration]),
        execution_agent_id=agent_id,
        execution_operations=("integration.openscad.render", "cache.export"),
    )
    monkeypatch.setattr(AgentRuntime, "_finalize_run_files", finalize)
    session = db.get.return_value
    storage = SessionStorage(tmp_path / "sessions")
    storage.provision(session.id)
    files = RootFileService(session.id, session_storage=storage, run_id=run.id)
    files.write_file("session", "model.scad", "cube(1);")
    records = await AgentRuntime._records(db, session.id)
    source = tool_message(session, run.id)
    source.provider_data["_matoloop"]["fileActions"] = [
        {
            "root": "session",
            "rootIdentity": str(session.id),
            "path": "model.scad",
            "filename": "model.scad",
            "operation": "write",
            "sizeBytes": 8,
        }
    ]
    records.append(source)
    attachments = []

    def add(item):
        if isinstance(item, Message):
            item.id, item.created_at = uuid4(), datetime.now(UTC)
            records.append(item)
        else:
            attachments.append(item)

    async def scalars(statement):
        return Mock(
            all=Mock(
                return_value=[item for item in records if item.role == "tool"]
                if statement.column_descriptions[0]["entity"] is Message
                else [agent_id]
            )
        )

    async def execute(call, _context, _tool):
        if call.id == "export":
            return ToolResult(
                '{"output":"stl","file":"model.stl"}',
                {},
                attachments=(ToolAttachment(b"stl", "model/stl", "model.stl", kind="file"),),
            )
        return ToolResult(
            '{"output":"preview","file":"model.off","previewFile":"model-views.png","geometryMetadata":{"triangleCount":12}}',
            {},
            attachments=(
                ToolAttachment(b"OFF", "model/off", "model.off", kind="file"),
                ToolAttachment(png("red"), "image/png", "model-views.png"),
            ),
        )

    db.add.side_effect = add
    db.scalar.return_value = session
    db.scalars.side_effect = scalars
    def _mock_value(*args, **kwargs):
        return RootFileService(*args, session_storage=storage, **kwargs)
    for _mock_target in (mock_agent_runtime, mock_agent_loop,):
        monkeypatch.setattr(_mock_target, 'RootFileService', _mock_value)
    def _mock_value(**kwargs):
        return AttachmentService(storage, **kwargs)
    for _mock_target in (mock_agent_runtime, mock_agent_loop, mock_agent_titles,):
        monkeypatch.setattr(_mock_target, 'AttachmentService', _mock_value)
    monkeypatch.setattr(mock_tools_catalog.tool_registry, "_execute_definition", execute)
    await AgentRuntime._execute_loop(db, run)
    public = RootFileService(session.id, session_storage=storage)
    assert public.list_revisions("model.scad")[0].revision == 1
    assert source.provider_data["_matoloop"]["fileOutputsPending"] is False
    assert [item.filename for item in attachments] == (["model.stl"] if export_stl else [])
    assert len([item for item in requests if item.get("image_inputs")]) == 2
    assert projections[0]["attachment_service"].run_id == run.id
    for message in records:
        if message.role == "tool" and (message.tool_call_id or "").startswith("preview-"):
            assert pending_attachments(message) == []
            assert "model.off" not in message.content and "model-views.png" not in message.content
            assert json.loads(message.content)["visualVerification"]["status"] == "completed"
    paths = [
        path for path in storage.logical_root_path(session.id, None).rglob("*") if path.is_file()
    ]
    assert not any(path.suffix in {".off", ".png"} for path in paths)


async def test_busy_file_rejects_concurrent_write_before_finalization(tmp_path):
    session = ChatSession(id=uuid4(), user_id=uuid4(), title="Test")
    storage = SessionStorage(tmp_path / "sessions")
    storage.provision(session.id)
    files = RootFileService(session.id, session_storage=storage)
    original = files.create_output_file("image.png", png("red"))
    run_id = uuid4()
    service = AttachmentService(storage, run_id=run_id)
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = session
    message = tool_message(session, run_id)
    await service.create_image(
        db,
        message,
        png("blue"),
        "image/png",
        filename="image.png",
        position=0,
        category="generated",
        purpose="artifact",
        source_file_root_id=session.id,
        file_id=original.file_id,
    )
    for writer in (files, RootFileService(session.id, session_storage=storage, run_id=uuid4())):
        with pytest.raises(RootFileError) as error:
            writer.append_output_revision(original.file_id, png("green"))
        assert error.value.code == "file.changed"
    assert files.open_file("session", original.path).read() == png("blue")
    db.add.assert_not_called()
    assert message.provider_data["_matoloop"]["fileOutputsPending"] is True
    assert len(pending_attachments(message)) == 1
    assert files.pending_run_ids() == (run_id,)
    db.scalars.return_value = Mock(all=Mock(return_value=[message]))
    await service.finalize_run(db, session, run_id)
    assert service.read_source(db.add.call_args.args[0], session.id) == png("blue")
    assert message.provider_data["_matoloop"]["fileOutputsPending"] is False
    assert files.pending_run_ids() == ()


async def test_runtime_finalizer_uses_session_and_preserves_conflicts(monkeypatch):
    session = ChatSession(id=uuid4(), user_id=uuid4(), title="Test")
    run = AgentRun(id=uuid4(), session_id=session.id)
    db = AsyncMock(spec=AsyncSession)
    db.get.return_value = session
    db.scalar.return_value = None
    finalize = AsyncMock()
    monkeypatch.setattr(AttachmentService, "finalize_run", finalize)
    assert await AgentRuntime._finalize_run_files(db, run) is True
    finalize.assert_awaited_once_with(db, session, run.id)
    finalize.side_effect = RootFileError("file.changed", "File changed during publication")
    with pytest.raises(RootFileError):
        await AgentRuntime._finalize_run_files(db, run)
    assert await AgentRuntime._finalize_run_files(db, run, tolerate_conflict=True) is False
    db.begin_nested.assert_called_once_with()
    assert db.begin_nested.return_value.__aexit__.await_args.args[0] is RootFileError

    finalize.reset_mock(side_effect=True)
    session.parent_session_id = uuid4()
    assert await AgentRuntime._finalize_run_files(db, run) is True
    finalize.assert_not_awaited()


async def test_recovery_retries_failed_active_run_file_finalization(monkeypatch):
    session = ChatSession(id=uuid4(), user_id=uuid4(), title="Test")
    run = AgentRun(id=uuid4(), session_id=session.id, status="running")
    db = AsyncMock(spec=AsyncSession)

    async def scalars(statement):
        entity = statement.column_descriptions[0]["entity"]
        values = {
            AgentRun: [run] if run.status == "running" else [],
            ChatSession: [session],
            Message: [],
        }
        return Mock(all=Mock(return_value=values[entity]))

    db.scalars.side_effect = scalars
    db.get.side_effect = lambda entity, _id: session if entity is ChatSession else run
    files = Mock(pending_run_ids=Mock(return_value=(run.id,)))
    finalize = AsyncMock(return_value=False)
    def _mock_value(*_args, **_kwargs):
        return files
    for _mock_target in (mock_agent_runtime, mock_agent_loop,):
        monkeypatch.setattr(_mock_target, 'RootFileService', _mock_value)
    monkeypatch.setattr(AgentRuntime, "_finalize_run_files", finalize)
    monkeypatch.setattr(AgentRuntime, "_terminalize_incomplete_tool_calls", AsyncMock())
    monkeypatch.setattr(AgentRuntime, "_persist_error_message", AsyncMock())
    monkeypatch.setattr(AgentRuntime, "_persist_run_duration", AsyncMock())

    await AgentRuntime.recover(db)
    assert finalize.await_count == 2
    await AgentRuntime.recover(db)
    assert finalize.await_count == 3


@pytest.mark.parametrize("later_finalized", [False, True])
async def test_recovery_after_db_interruption_uses_own_snapshot_not_later_run_bytes(
    tmp_path, later_finalized
):
    session = ChatSession(id=uuid4(), user_id=uuid4())
    storage = SessionStorage(tmp_path / "sessions")
    storage.provision(session.id)
    run_id, later_run_id = uuid4(), uuid4()
    service = AttachmentService(storage, run_id=run_id)
    message = tool_message(session, run_id)
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = session
    pending = await service.create_image(
        db, message, png("red"), "image/png", filename="image.png", position=0,
        category="generated", purpose="artifact",
    )
    saved = json.dumps(message.provider_data)
    db.scalars.return_value = Mock(all=Mock(return_value=[message]))
    db.flush.side_effect = RuntimeError("Database interrupted after storage publication")
    with pytest.raises(RuntimeError, match="Database interrupted"):
        await service.finalize_run(db, session, run_id)
    files = RootFileService(session.id, session_storage=storage)
    original = files.finalize_run(run_id)[0]
    later = RootFileService(session.id, session_storage=storage, run_id=later_run_id)
    later.append_output_revision(pending.file_id, png("blue"))
    if later_finalized:
        files.finalize_run(later_run_id)
    recovered = files.finalize_run(run_id)
    assert [(item.file_id, item.revision, item.sha256) for item in recovered] == [
        (original.file_id, original.revision, original.sha256)
    ]
    message.provider_data = json.loads(saved)
    db.add.reset_mock()
    db.flush.side_effect = None
    await service.finalize_run(db, session, run_id)
    await service.finalize_run(db, session, run_id)
    db.add.assert_called_once()
    published = db.add.call_args.args[0]
    assert published.file_revision == original.revision == 1
    assert published.sha256 == pending.sha256
    assert published.kind == "image"
    assert AttachmentService(storage).read_source(published, session.id) == png("red")
    assert files.open_file("session", pending.file_path).read() == png("blue")
    for reader in (service, AttachmentService(storage)):
        with pytest.raises(AttachmentError):
            reader.read_source(published, session.id, current_only=True)
    assert message.provider_data["_matoloop"]["fileOutputsPending"] is False


async def test_foreign_live_reference_cannot_be_read_or_published(tmp_path):
    session = ChatSession(id=uuid4(), user_id=uuid4())
    storage = SessionStorage(tmp_path / "sessions")
    storage.provision(session.id)
    owner_id, other_id = uuid4(), uuid4()
    files = RootFileService(session.id, session_storage=storage, run_id=owner_id)
    files.write_file("session", "notes.txt", "owned live bytes")
    revision = files.working_revision("notes.txt")
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = session
    message = tool_message(session, owner_id)
    pending = await AttachmentService(storage, run_id=owner_id).create_reference(
        db, message, file_root_id=session.id, file_id=revision.file_id,
        file_revision=None, file_path=revision.path, filename="notes.txt",
        position=0, category="generated",
    )
    service = AttachmentService(storage, run_id=other_id)
    with pytest.raises(AttachmentError):
        await service.create_reference(
            db, tool_message(session, other_id), file_root_id=session.id,
            file_id=revision.file_id, file_revision=None, file_path=revision.path,
            filename="notes.txt", position=0, category="generated",
        )
    forged = SimpleNamespace(**{**vars(pending), "run_id": other_id})
    with pytest.raises(AttachmentError):
        service.read_source(forged, session.id, current_only=True)
    state = message.provider_data["_matoloop"]
    state["runId"] = state["pendingAttachments"][0]["run_id"] = str(other_id)
    db.scalars.return_value = Mock(all=Mock(return_value=[message]))
    await service.finalize_run(db, session, other_id)
    db.add.assert_not_called()
    assert message.provider_data["_matoloop"]["fileOutputsPending"] is False
    assert message.provider_data["_matoloop"]["fileOutputsHidden"] is True


async def test_pending_audio_and_image_tools_use_owned_live_sources(tmp_path, monkeypatch):
    session = ChatSession(id=uuid4(), user_id=uuid4(), title="Test")
    run_id = uuid4()
    storage = SessionStorage(tmp_path / "sessions")
    storage.provision(session.id)
    service = AttachmentService(storage, run_id=run_id)
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = session
    message = tool_message(session, run_id)
    audio_output = BytesIO()
    with wave.open(audio_output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\0\0" * 80)
    attachment = await service.create_audio(
        db,
        message,
        audio_output.getvalue(),
        "audio/wav",
        filename="voice.wav",
        position=0,
        category="generated",
        purpose="artifact",
    )
    image = await service.create_image(
        db,
        message,
        png("red"),
        "image/png",
        filename="image.png",
        position=1,
        category="downloaded",
        purpose="artifact",
    )
    assert attachment.file_revision is None
    assert (
        await service.read_audio(attachment, session.id, current_only=True)
        == audio_output.getvalue()
    )
    db.scalar.return_value = None
    db.scalars.return_value = Mock(all=Mock(return_value=[message]))
    def _mock_value(**_kwargs):
        return service
    for _mock_target in (mock_tools_files, mock_tools_media,):
        monkeypatch.setattr(_mock_target, 'AttachmentService', _mock_value)
    target = SimpleNamespace(
        model=SimpleNamespace(id=uuid4(), trust_level=TrustLevel.PUBLIC),
        provider=SimpleNamespace(trust_level=TrustLevel.PUBLIC),
    )
    monkeypatch.setattr(mock_tools_media.MediaService, "resolve_model", AsyncMock(return_value=target))
    monkeypatch.setattr(mock_tools_media, '_media_target_matches_binding', lambda *_args: True)
    monkeypatch.setattr(mock_tools_media, '_authorize_media', AsyncMock(return_value=None))
    edit = AsyncMock(return_value=MediaResult(png("blue"), "image/png", "image.png"))
    monkeypatch.setattr(mock_tools_media.MediaService, "edit_image", edit)
    context = SimpleNamespace(
        db=db,
        session=session,
        run_id=run_id,
        agent_profile_id=None,
        model_id=target.model.id,
        tool_settings=ToolSettings(),
        root_files=RootFileService(session.id, session_storage=storage, run_id=run_id),
        tool_call=ToolCall("edit", "edit_image", {}),
        ask=AsyncMock(return_value=PermissionDecision.ALLOW),
        model_trust_level=TrustLevel.PUBLIC,
    )
    result = await mock_tools_media._edit_image(
        SimpleNamespace(images=[SimpleNamespace(root="session", path=image.file_path)], prompt="Make it blue"), context
    )
    assert result.attachments[0].file_root_id is None
    assert result.attachments[0].file_id is None
    assert edit.await_args.kwargs["images"] == ((png("red"), "image/png"),)
    transcribe = AsyncMock(return_value=SimpleNamespace(text="transcribed"))
    monkeypatch.setattr(mock_tools_media.MediaService, "transcribe_audio", transcribe)
    await mock_tools_media._transcribe_audio(
        SimpleNamespace(root="session", path=attachment.file_path, language=None), context,
    )
    assert transcribe.await_args.kwargs["audio"] == audio_output.getvalue()
    context.run_id = uuid4()
    monkeypatch.setattr(mock_tools_media, "AttachmentService", AttachmentService)
    with pytest.raises(AttachmentError):
        await mock_tools_media._transcribe_audio(
            SimpleNamespace(root="session", path=attachment.file_path, language=None), context,
        )
    db.add.assert_not_called()


@pytest.mark.parametrize("terminal", ["completed", "cancelled", "failed"])
async def test_terminal_events_follow_file_finalization(monkeypatch, terminal):
    monkeypatch.setattr(AgentRuntime, "_prepare_run", AsyncMock())
    run = AgentRun(id=uuid4(), session_id=uuid4(), status="queued")
    db = AsyncMock(spec=AsyncSession)
    db.get.side_effect = lambda model, _id: run if model is AgentRun else None
    db.scalar.return_value = None
    order = []

    async def execute(*_args):
        if terminal == "cancelled":
            AgentRuntime._user_cancelled_runs.add(run.id)
            raise asyncio.CancelledError
        if terminal == "failed":
            raise ProviderError("Failure", code="provider.requestFailed")

    async def finalize(*_args, **_kwargs):
        order.append("finalize")

    async def publish(_run_id, event):
        order.append(event["type"])
        if event["type"] in {"message.completed", "message.interrupted", f"run.{terminal}"}:
            assert "finalize" in order

    db.__aenter__.return_value = db
    def _mock_value():
        return db
    for _mock_target in (mock_agent_runtime, mock_agent_summarization, mock_agent_titles,):
        monkeypatch.setattr(_mock_target, 'AsyncSessionFactory', _mock_value)
    monkeypatch.setattr(AgentRuntime, "_execute_loop", execute)
    monkeypatch.setattr(AgentRuntime, "_finalize_run_files", finalize)
    monkeypatch.setattr(AgentRuntime, "_terminalize_incomplete_tool_calls", AsyncMock())
    monkeypatch.setattr(AgentRuntime, "_persist_run_duration", AsyncMock())
    monkeypatch.setattr(mock_agent_runtime.NotificationService, "deliver_message_after_commit", AsyncMock())
    monkeypatch.setattr(mock_agent_runtime.run_events, "publish", publish)
    if terminal == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await AgentRuntime._run(run.id)
    else:
        await AgentRuntime._run(run.id)
    assert f"run.{terminal}" in order


@pytest.mark.parametrize(
    "status,storage_finalized", [("running", False), ("failed", False), ("failed", True)]
)
async def test_recovery_finalizes_live_files_and_pending_json_before_errors(
    tmp_path, monkeypatch, status, storage_finalized
):
    session = ChatSession(id=uuid4(), user_id=uuid4(), title="Test")
    run = AgentRun(id=uuid4(), session_id=session.id, status=status)
    storage = SessionStorage(tmp_path / "sessions")
    storage.provision(session.id)
    service = AttachmentService(storage, run_id=run.id)
    files = RootFileService(session.id, session_storage=storage)
    message = tool_message(session, run.id)
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = session
    pending = await service.create_image(
        db,
        message,
        png("red"),
        "image/png",
        filename="image.png",
        position=0,
        category="generated",
        purpose="artifact",
    )
    if storage_finalized:
        files.finalize_run(run.id)
        assert files.pending_run_ids() == ()

    async def scalars(statement):
        entity = statement.column_descriptions[0]["entity"]
        values = {
            AgentRun: [run] if status == "running" else [],
            ChatSession: [session],
            Message: [message],
        }
        return Mock(all=Mock(return_value=values[entity]))

    async def persist_error(*_args):
        assert message.provider_data["_matoloop"]["fileOutputsPending"] is False
        assert files.list_revisions(pending.file_path)[0].revision == 1

    db.scalars.side_effect = scalars
    db.get.side_effect = lambda entity, _id: session if entity is ChatSession else run
    def _mock_value(**_kwargs):
        return service
    for _mock_target in (mock_agent_runtime, mock_agent_loop, mock_agent_titles,):
        monkeypatch.setattr(_mock_target, 'AttachmentService', _mock_value)
    monkeypatch.setattr(SettingsStore, "load", AsyncMock(return_value=InstanceLimits()))
    def _mock_value(*_args, **_kwargs):
        return files
    for _mock_target in (mock_agent_runtime, mock_agent_loop,):
        monkeypatch.setattr(_mock_target, 'RootFileService', _mock_value)
    monkeypatch.setattr(AgentRuntime, "_terminalize_incomplete_tool_calls", AsyncMock())
    monkeypatch.setattr(AgentRuntime, "_persist_error_message", persist_error)
    await AgentRuntime.recover(db)
    assert db.add.call_args.args[0].file_revision == 1
    assert message.provider_data["_matoloop"]["fileOutputsPending"] is False
    assert files.pending_run_ids() == ()


async def test_runtime_publishes_attachment_rows_only_before_final_assistant(tmp_path, monkeypatch):

    finalize = AgentRuntime._finalize_run_files
    db, run, requests, _, events = await _runtime_setup(
        monkeypatch,
        [
            ProviderResult(
                tool_calls=[ToolCall("first", "webfetch", {"url": "https://example.test/first"})]
            ),
            ProviderResult(
                tool_calls=[ToolCall("second", "webfetch", {"url": "https://example.test/second"})]
            ),
            ProviderResult(content="Final image.", finish_reason="stop"),
        ],
        search_policy=WebSearchPolicy.AVAILABLE,
        active_supports_image_input=True,
    )
    monkeypatch.setattr(AgentRuntime, "_finalize_run_files", finalize)
    session = db.get.return_value
    storage = SessionStorage(tmp_path / "sessions")
    storage.provision(session.id)
    public_files = RootFileService(session.id, session_storage=storage)
    def _mock_value(*args, **kwargs):
        return RootFileService(*args, session_storage=storage, **kwargs)
    for _mock_target in (mock_agent_runtime, mock_agent_loop,):
        monkeypatch.setattr(_mock_target, 'RootFileService', _mock_value)
    def _mock_value(**kwargs):
        return AttachmentService(storage, **kwargs)
    for _mock_target in (mock_agent_runtime, mock_agent_loop, mock_agent_titles,):
        monkeypatch.setattr(_mock_target, 'AttachmentService', _mock_value)
    records = await AgentRuntime._records(db, session.id)
    public_attachments = []

    def add(item):
        if isinstance(item, Message):
            item.id = uuid4()
            item.created_at = datetime.now(UTC)
            records.append(item)
        elif isinstance(item, MessageAttachment):
            public_attachments.append(item)

    db.add.side_effect = add
    db.scalar.return_value = session
    db.scalars.side_effect = lambda _statement: Mock(
        all=Mock(return_value=[item for item in records if item.role == "tool"])
    )

    async def execute(call, context, _tool):
        assert context.root_files.run_id == run.id
        if call.id == "first":
            return ToolResult(
                "Downloaded image.",
                {},
                attachments=(
                    ToolAttachment(png("red"), "image/png", "image.png", category="downloaded"),
                ),
            )
        assert public_attachments == []
        source = next(item for message in records for item in pending_attachments(message))
        assert public_files.list_revisions(source.file_path) == ()
        assert public_files.open_file("session", source.file_path).read() == png("red")
        assert requests[-1]["messages"][-1]["attachments"][0]["file_revision"] is None
        assert context.root_files.open_file("session", source.file_path).read() == png("red")
        return ToolResult(
            "Updated image.",
            {},
            attachments=(
                ToolAttachment(
                    png("blue"),
                    "image/png",
                    "image.png",
                    file_root_id=source.file_root_id,
                    file_id=source.file_id,
                ),
            ),
        )

    async def publish(_run_id, event):
        if event["type"] == "message.completed" and not event["message"]["toolCalls"]:
            assert len(public_attachments) == 1
            assert public_attachments[0].file_revision == 1
        events.append(event)

    monkeypatch.setattr(mock_tools_catalog.tool_registry, "_execute_definition", execute)
    monkeypatch.setattr(mock_agent_runtime.run_events, "publish", publish)
    await AgentRuntime._execute_loop(db, run)
    assert len(public_attachments) == 1
    assert await AttachmentService(storage).read_image(public_attachments[0], session.id) == png(
        "blue"
    )
    messages = [item for item in records if item.role == "tool"]
    assert messages[0].provider_data["_matoloop"]["fileOutputsHidden"] is True
    assert messages[1].provider_data["_matoloop"]["fileOutputsPending"] is False
