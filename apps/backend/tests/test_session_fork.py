import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend.agent.context import _model_order
from mato_loop_backend.agent.projection import _projection_messages, _summary_evidence, message_dict
from mato_loop_backend.files.service import RootFileService
from mato_loop_backend.files.storage import SessionStorage
from mato_loop_backend.media.attachments import AttachmentService, latest_image_references
from mato_loop_backend.models import ChatSession, ContextSummary, Message, MessageAttachment
from mato_loop_backend.sessions import fork
from mato_loop_backend.sessions.serialization import message_payload
from mato_loop_backend.sessions.service import SessionServiceError
from mato_loop_backend.settings import InstanceLimits, TrustLevel


@pytest.mark.parametrize("project", [False, True])
@pytest.mark.parametrize("failure", [None, "flush", "commit", "symlink", "quota", "pending"])
async def test_fork_copies_conversation_and_files_and_handles_transaction_failures(tmp_path, monkeypatch, project, failure):
    user_id, session_id, project_id = uuid4(), uuid4(), uuid4() if project else None
    storage = SessionStorage(tmp_path / "sessions")
    root = storage.provision(session_id, project_id, user_id=user_id)
    original_file = root / "files" / "note.txt"
    source_files = RootFileService(session_id, project_id=project_id, session_storage=storage)
    source_files.write_file("session", "note.txt", "original")
    revision = source_files.working_revision("note.txt")
    source_files.write_file("session", "note.txt", "updated", mode="replace", expected_sha256=revision.sha256)
    now = datetime.now(UTC)
    source = ChatSession(id=session_id, user_id=user_id, project_id=project_id, title="Source",
                         trust_level="private", private_integration_data_used=True,
                         preferred_execution_agent_id=uuid4(), current_directory="/worktree",
                         workspace_mode="worktree", coding_workspace={"mode": "worktree"})
    first, second, direct, result = sorted(uuid4() for _ in range(4))
    run_id = str(uuid4())
    messages = [
        Message(id=first, session_id=session_id, role="user", content=f"Keep session {session_id} and message {first} unchanged.", created_at=now,
                generation_status="completed", attachments=[MessageAttachment(
                    id=uuid4(), message_id=first, filename="note.txt", position=0, kind="text", category="user_upload",
                    purpose="attachment", processing_status="ready", mime_type="text/plain", size_bytes=revision.size_bytes,
                    sha256=revision.sha256, file_root_id=session_id, file_id=revision.file_id, file_revision=revision.revision,
                    file_path=revision.path, metadata_json=AttachmentService._companion_metadata(session_id, revision),
                )]),
        Message(id=second, session_id=session_id, role="assistant", content="Done", created_at=now,
                generation_status="completed", attachments=[], tool_calls=[{
                    "id": "call", "name": "shell_run", "arguments": {"command": f"echo {session_id}"},
                }], provider_data={"_matoloop": {"runId": run_id, "fileActions": [{
                    "root": "session", "rootIdentity": str(session_id), "filename": "note.txt", "path": "note.txt",
                }]}}),
        Message(id=direct, session_id=session_id, role="user", content="Keep going", created_at=now,
                generation_status="completed", provider_data={"_matoloop": {"continuationRunId": run_id}}),
        Message(id=result, session_id=session_id, role="tool", content='{"exitCode":0}', created_at=now,
                tool_call_id="call", generation_status="completed", provider_data={"_matoloop": {"runId": run_id}}),
    ]
    summary = ContextSummary(id=uuid4(), session_id=session_id, covered_through_message_id=second,
                             content=f"Facts\nKeep {session_id} unchanged.\n## Source message IDs\n{second}\n", created_at=now)
    fallback_prefix = "Goal\nContinue\n\nFacts and earlier context\n"
    fallback = ContextSummary(id=uuid4(), session_id=session_id, covered_through_message_id=direct,
                              content=fallback_prefix + _summary_evidence(_model_order(messages)),
                              created_at=now, is_fallback=True)
    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = [source, *([None] if project else []), None]
    db.scalars.side_effect = [Mock(all=Mock(return_value=messages)), Mock(all=Mock(return_value=[summary, fallback]))]
    monkeypatch.setattr(fork, "SessionStorage", lambda: storage)
    limits = InstanceLimits(user_quota_mib=1 if failure == "quota" else None)
    monkeypatch.setattr(fork.SettingsStore, "load", AsyncMock(return_value=limits))
    if failure:
        if failure == "flush":
            db.flush.side_effect = [None, RuntimeError("Database transaction failed")]
        elif failure == "commit":
            db.commit.side_effect = RuntimeError("Database transaction failed")
        elif failure == "symlink":
            (root / "files" / "escape").symlink_to(tmp_path)
        elif failure == "quota":
            (root / "files" / "large.bin").write_bytes(b"x" * 1024 * 1024)
        else:
            RootFileService(session_id, project_id=project_id, session_storage=storage, run_id=uuid4()).create_output_file("pending.txt", b"pending")
        error = {"symlink": "file.symlinkForbidden", "quota": "file.userQuotaExceeded", "pending": "file.changed"}.get(failure)
        with pytest.raises(SessionServiceError if error else RuntimeError, match=error or "Database transaction failed"):
            await fork.fork_session(db, session_id, user_id)
        target = db.add.call_args_list[0].args[0]
        assert (storage.session_path(target.id, project_id=project_id).exists(), original_file.read_text()) == (failure == "commit", "updated")
        return
    target = await fork.fork_session(db, session_id, user_id)
    stored = [call.args[0] for call in db.add.call_args_list]
    copied_messages = [item for item in stored if isinstance(item, Message)]
    copied_attachment = next(item for item in stored if isinstance(item, MessageAttachment))
    copied_summary = next(item for item in stored if isinstance(item, ContextSummary))
    copied_fallback = next(item for item in stored if isinstance(item, ContextSummary) and item.is_fallback)
    projection = _projection_messages("System", None, _model_order(copied_messages), TrustLevel.PRIVATE)
    target_files = storage.session_path(target.id, project_id=project_id) / "files"
    assert (
        target.title, target.title_generation_status,
        target.project_id, target.current_directory, target.coding_workspace, target.trust_level,
        target.private_integration_data_used, copied_summary.covered_through_message_id,
        copied_summary.content, copied_messages[0].id < copied_messages[1].id,
        copied_messages[0].content, copied_messages[1].tool_calls,
        copied_messages[1].provider_data["_matoloop"]["fileActions"],
        copied_attachment.file_root_id, copied_attachment.message_id, (target_files / "note.txt").read_text(), original_file.read_text(),
        [item["content"] for item in projection if item["role"] == "tool"], copied_fallback.content,
    ) == ("Kopie: Source", "pending", project_id, "/worktree", {"mode": "local"}, "private", True, copied_messages[1].id,
          f"Facts\nKeep {session_id} unchanged.\n## Source message IDs\n{copied_messages[1].id}\n", True,
           messages[0].content, messages[1].tool_calls,
          [{"root": "session", "rootIdentity": str(target.id), "filename": "note.txt", "path": "note.txt"}],
          target.id, copied_messages[0].id, "updated", "updated", [messages[-1].content],
          fallback_prefix + _summary_evidence(_model_order(copied_messages)))
    storage.remove_session(session_id, project_id)
    assert AttachmentService(storage).read_derived_text(copied_attachment, target.id) == "original"


async def test_fork_remaps_stored_resource_references(tmp_path, monkeypatch):
    user_id, session_id = uuid4(), uuid4()
    result_id, call_id = sorted(uuid4() for _ in range(2))
    arguments = {"message_token": f"mail:{result_id}:0"}
    storage = SessionStorage(tmp_path / "sessions")
    storage.provision(session_id, user_id=user_id)
    messages = [
        Message(id=result_id, session_id=session_id, role="tool", attachments=[], created_at=datetime.now(UTC),
                content='{"messages":[{"messageToken":"encrypted-token"}]}',
                provider_data={"_matoloop": {"toolName": "mail_search"}}),
        Message(id=call_id, session_id=session_id, role="assistant", content="", attachments=[], created_at=datetime.now(UTC),
                tool_calls=[{"id": "read", "name": "mail_get_message", "arguments": arguments}],
                provider_data={"_matoloop": {"userEditedToolCalls": {"read": arguments}}, "responses_output_items": [{
                    "type": "function_call", "name": "mail_get_message", "arguments": json.dumps(arguments),
                }]}),
    ]
    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = [ChatSession(id=session_id, user_id=user_id, title="Mail"), None]
    db.scalars.side_effect = [Mock(all=Mock(return_value=messages)), Mock(all=Mock(return_value=[]))]
    monkeypatch.setattr(fork, "SessionStorage", lambda: storage)
    monkeypatch.setattr(fork.SettingsStore, "load", AsyncMock(return_value=InstanceLimits()))

    await fork.fork_session(db, session_id, user_id)

    copied = [call.args[0] for call in db.add.call_args_list if isinstance(call.args[0], Message)]
    expected = {"message_token": f"mail:{copied[0].id}:0"}
    assert (
        copied[1].tool_calls[0]["arguments"],
        copied[1].provider_data["_matoloop"]["userEditedToolCalls"]["read"],
        json.loads(copied[1].provider_data["responses_output_items"][0]["arguments"]),
    ) == (expected, expected, expected)


@pytest.mark.parametrize("shared_companion", [False, True])
async def test_fork_imports_foreign_attachment_and_companion_without_sharing_storage(tmp_path, monkeypatch, shared_companion):
    storage = SessionStorage(tmp_path / "sessions")
    project_id, user_id = uuid4(), uuid4()
    source_id, foreign_id = uuid4(), uuid4()
    for session_id in (source_id, foreign_id):
        storage.provision(session_id, project_id, user_id=user_id)
    files = RootFileService(foreign_id, project_id=project_id, session_storage=storage)
    files.write_file("session", "report.pdf", "source")
    files.write_file("session", "report.txt", "extracted")
    source = files.working_revision("report.pdf")
    companion = files.working_revision("report.txt")
    revisions = [source]
    if shared_companion:
        files.write_file("session", "second.pdf", "other source")
        revisions.append(files.working_revision("second.pdf"))
    else:
        files.register_derived_file(source.file_id, companion.file_id, source_revision=source.revision, derived_revision=companion.revision)
    message = Message(id=uuid4(), session_id=source_id, role="tool", content="Done", created_at=datetime.now(UTC), attachments=[
        MessageAttachment(
            id=uuid4(), filename=revision.path, kind="document", processing_status="ready",
            file_root_id=foreign_id, file_id=revision.file_id, file_revision=revision.revision,
            file_path=revision.path, size_bytes=revision.size_bytes, sha256=revision.sha256,
            metadata_json=AttachmentService._companion_metadata(foreign_id, companion),
        ) for revision in revisions
    ], provider_data={"_matoloop": {"fileActions": [
        {"operation": "provide", "root": "session", "rootIdentity": str(foreign_id),
         "fileId": str(revision.file_id), "fileRevision": revision.revision,
         "path": revision.path, "filename": revision.path, "sizeBytes": revision.size_bytes}
        for revision in revisions
    ]}})
    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = [ChatSession(id=source_id, user_id=user_id, project_id=project_id, title="Source"), None, None]
    db.scalars.side_effect = [Mock(all=Mock(return_value=[message])), Mock(all=Mock(return_value=[]))]
    monkeypatch.setattr(fork, "SessionStorage", lambda: storage)
    monkeypatch.setattr(fork.SettingsStore, "load", AsyncMock(return_value=InstanceLimits()))
    target = await fork.fork_session(db, source_id, user_id)
    stored = [call.args[0] for call in db.add.call_args_list]
    copied = [item for item in stored if isinstance(item, MessageAttachment)]
    copied_message = next(item for item in stored if isinstance(item, Message))
    storage.remove_session(foreign_id, project_id)
    contents = [AttachmentService(storage).read_derived_text(item, target.id) for item in copied]
    target_root = storage.session_path(target.id, project_id=project_id)
    RootFileService(target.id, project_id=project_id, session_storage=storage).delete_path("session", copied[0].file_path, recursive=False)
    assert (contents, [item["filename"] for item in message_payload(copied_message)["fileActions"]],
            (target_root / "files" / copied[0].metadata_json["companionFilePath"]).exists()) == (
        ["extracted"] * len(revisions), [item.path for item in revisions], shared_companion,
    )


def test_foreign_image_revisions_keep_only_the_current_image_visible(tmp_path):
    storage = SessionStorage(tmp_path / "sessions")
    project_id = uuid4()
    source_id, foreign_id, target_id = uuid4(), uuid4(), uuid4()
    for session_id in (source_id, foreign_id, target_id):
        storage.provision(session_id, project_id)
    files = RootFileService(foreign_id, project_id=project_id, session_storage=storage)
    files.write_file("session", "image.png", "old")
    old = files.working_revision("image.png")
    files.write_file("session", "image.png", "new", mode="replace", expected_sha256=old.sha256)
    service, imported = AttachmentService(storage), {}
    target = RootFileService(target_id, project_id=project_id, session_storage=storage)
    copied = [service.copy_attachment(MessageAttachment(
        id=uuid4(), kind="image", file_root_id=foreign_id, file_id=item.file_id, file_revision=item.revision,
        file_path=item.path, size_bytes=item.size_bytes, sha256=item.sha256,
    ), source_id, target, uuid4(), imported) for item in (old, files.working_revision("image.png"))]
    projected = latest_image_references([
        message_dict(Message(session_id=target_id, role="user", content="Image", attachments=[item])) for item in copied
    ])
    by_id = {str(item.id): item for item in copied}
    assert [service.read_source(by_id[item["id"]], target_id, current_only=True)
            for message in projected for item in message["attachments"]] == [b"new"]


@pytest.mark.parametrize(("state", "code"), [
    ("missing", "session.notFound"), ("child", "session.readOnly"),
    ("busy", "session.runAlreadyActive"), ("archived", "session.archived"),
])
async def test_fork_rejects_unavailable_sources_without_creating_data(state, code):
    user_id, session_id = uuid4(), uuid4()
    source = ChatSession(id=session_id, user_id=user_id,
                         parent_session_id=uuid4() if state == "child" else None,
                         archived_at=datetime.now(UTC) if state == "archived" else None)
    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = [None if state == "missing" else source, uuid4() if state == "busy" else None]
    with pytest.raises(SessionServiceError) as error:
        await fork.fork_session(db, session_id, user_id)
    assert (error.value.code, db.add.call_count) == (code, 0)


def test_historical_errors_remain_localizable_without_a_run():
    message = Message(id=uuid4(), role="assistant", content="", attachments=[], provider_data={
        "_matoloop": {"historicalError": {"code": "session.runAlreadyActive", "params": None}},
    })
    assert message_payload(message)["error"]["code"] == "session.runAlreadyActive"
