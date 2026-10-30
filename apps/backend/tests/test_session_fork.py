from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend.agent.context import _model_order
from mato_loop_backend.agent.projection import _projection_messages, _summary_evidence
from mato_loop_backend.agent.prompts import general_system_prompt
from mato_loop_backend.files.storage import SessionStorage
from mato_loop_backend.models import ChatSession, ContextSummary, Message, MessageAttachment
from mato_loop_backend.sessions import fork
from mato_loop_backend.sessions.serialization import message_payload
from mato_loop_backend.sessions.service import SessionServiceError
from mato_loop_backend.settings import TrustLevel


@pytest.mark.parametrize("project", [False, True])
@pytest.mark.parametrize("failure", [None, "flush", "commit"])
async def test_fork_copies_only_conversation_and_handles_transaction_failures(tmp_path, monkeypatch, project, failure):
    user_id, session_id, project_id = uuid4(), uuid4(), uuid4() if project else None
    storage = SessionStorage(tmp_path / "sessions")
    root = storage.provision(session_id, project_id, user_id=user_id)
    original_file = root / "files" / "note.txt"
    original_file.write_text("original")
    now = datetime.now(UTC)
    source = ChatSession(id=session_id, user_id=user_id, project_id=project_id, title="Source",
                         trust_level="private", private_integration_data_used=True,
                         preferred_execution_agent_id=uuid4(), current_directory="/worktree",
                         workspace_mode="worktree", coding_workspace={"mode": "worktree"})
    first, second, direct, result = sorted(uuid4() for _ in range(4))
    run_id = str(uuid4())
    messages = [
        Message(id=first, session_id=session_id, role="user", content=f"Keep session {session_id} and message {first} unchanged.", created_at=now,
                generation_status="completed", attachments=[MessageAttachment(id=uuid4(), filename="note.txt")]),
        Message(id=second, session_id=session_id, role="assistant", content="Done", created_at=now,
                generation_status="completed", attachments=[], tool_calls=[{
                    "id": "call", "name": "shell_run", "arguments": {"command": f"echo {session_id}"},
                }], provider_data={"_matoloop": {"runId": run_id, "fileActions": [{
                    "root": "session", "rootIdentity": str(uuid4()), "filename": "report.csv",
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
    if failure:
        if failure == "flush":
            db.flush.side_effect = [None, RuntimeError("Database transaction failed")]
        else:
            db.commit.side_effect = RuntimeError("Database transaction failed")
        with pytest.raises(RuntimeError, match="Database transaction failed"):
            await fork.fork_session(db, session_id, user_id)
        target = db.add.call_args_list[0].args[0]
        assert (storage.session_path(target.id, project_id=project_id).exists(), original_file.read_text()) == (failure == "commit", "original")
        return
    target = await fork.fork_session(db, session_id, user_id)
    stored = [call.args[0] for call in db.add.call_args_list]
    copied_messages = [item for item in stored if isinstance(item, Message)]
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
        "This conversation was copied without internal files or attachments." in general_system_prompt(target),
        copied_messages[1].provider_data["_matoloop"]["fileActions"],
        any(isinstance(item, MessageAttachment) for item in stored), list(target_files.iterdir()), original_file.read_text(),
        [item["content"] for item in projection if item["role"] == "tool"], copied_fallback.content,
    ) == ("Kopie: Source", "pending", project_id, "/worktree", {"mode": "local"}, "private", True, copied_messages[1].id,
          f"Facts\nKeep {session_id} unchanged.\n## Source message IDs\n{copied_messages[1].id}\n", True,
          messages[0].content, messages[1].tool_calls, True, [], False, [], "original",
          [messages[-1].content], fallback_prefix + _summary_evidence(_model_order(copied_messages)))


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
