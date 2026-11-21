import asyncio
import json
import runpy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from pydantic import ValidationError
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from mato_loop_backend import notifications
from mato_loop_backend.api.errors import ApiError
from mato_loop_backend.api.routes.notifications import (
    NotificationPresenceRequest,
    _decode_cursor,
    _encode_cursor,
    _notification_payload,
    delete_notification,
    list_notifications,
    mark_all_notifications_read,
    mark_notification_presence,
    mark_notification_read,
)
from mato_loop_backend.config import get_runtime_config
from mato_loop_backend.models import (
    ApprovalRequest,
    ChatSession,
    Message,
    Notification,
    QuestionRequest,
    UserAccount,
)
from mato_loop_backend.notifications import (
    NotificationDraft,
    NotificationError,
    NotificationService,
)
from mato_loop_backend.web_push import WebPushMemoryApprovalTarget, web_push_payload


def _user() -> UserAccount:
    return UserAccount(id=uuid4(), username="test", password_hash="hash")


def _notification(user_id, *, created_at: datetime | None = None) -> Notification:
    return Notification(
        id=uuid4(),
        user_id=user_id,
        kind="automation.completed",
        title="Automation abgeschlossen",
        body="Der Lauf wurde abgeschlossen.",
        deduplication_key=f"automation-run:{uuid4()}:completed",
        created_at=created_at or datetime(2026, 8, 14, 8, 0, tzinfo=UTC),
    )


async def test_mark_notification_presence_is_user_scoped(monkeypatch) -> None:
    from mato_loop_backend.api.routes import notifications as notifications_route

    user = _user()
    mark = MagicMock()
    monkeypatch.setattr(notifications_route, "mark_user_present", mark)

    response = await mark_notification_presence(user, AsyncMock(spec=AsyncSession))

    assert response.status_code == 204
    mark.assert_called_once_with(user.id)


@pytest.mark.parametrize("refocus", [False, True])
async def test_session_response_push_waits_for_grace_and_batches_unread(monkeypatch, refocus) -> None:
    user = _user()
    unread = [_notification(user.id), _notification(user.id)]
    ids = [item.id for item in unread] + [uuid4()]
    db = AsyncMock(spec=AsyncSession)
    db.__aenter__.return_value = db
    db.execute.return_value = MagicMock(one_or_none=MagicMock(side_effect=[(user.id, notice_id) for notice_id in ids]))
    db.scalars.return_value = MagicMock(all=MagicMock(return_value=unread))
    deliver = AsyncMock()
    resume = asyncio.Event()

    async def wait(delay):
        assert delay == 120
        await resume.wait()

    monkeypatch.setattr(notifications, "AsyncSessionFactory", lambda: db)
    monkeypatch.setattr(notifications, "response_push_delay", lambda _: 120)
    monkeypatch.setattr(notifications, "sleep", wait)
    monkeypatch.setattr(notifications.WebPushService, "deliver_after_commit", deliver)
    for _ in ids:
        await NotificationService.deliver_message_after_commit(uuid4())
    deliver.assert_not_awaited()

    task = notifications._pending_response_pushes[user.id][1]
    if refocus:
        await mark_notification_presence(user, db, NotificationPresenceRequest(clientId=uuid4()))
    resume.set()
    if refocus:
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await task
    if refocus:
        deliver.assert_not_awaited()
    else:
        assert "notifications.read_at IS NULL" in str(db.scalars.await_args.args[0])
        deliver.assert_awaited_once()
        payload = deliver.await_args.args[1].notification
        assert (payload.title, payload.target.type, payload.notification_id) == ("2 neue Session-Hinweise", "actions", None)


@pytest.mark.parametrize("trashed", [False, True])
async def test_deferred_response_push_checks_session_trash(request, monkeypatch, trashed) -> None:
    if not request.config.getoption("--memory-db"):
        pytest.skip("requires the migrated development PostgreSQL database")
    engine = create_async_engine(get_runtime_config().database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            await connection.begin()
            session_factory = async_sessionmaker(connection, expire_on_commit=False)
            async with session_factory() as db:
                user = _user()
                user.username = f"trash-push-{user.id}"
                db.add(user)
                await db.flush()
                session = ChatSession(user_id=user.id)
                db.add(session)
                await db.flush()
                notice = _notification(user.id)
                notice.kind, notice.session_id = "session.response", session.id
                db.add(notice)
                if trashed:
                    session.delete_at = datetime.now(UTC) + timedelta(days=1)
                await db.flush()
            deliver = AsyncMock()
            monkeypatch.setattr(notifications, "AsyncSessionFactory", session_factory)
            monkeypatch.setattr(notifications.WebPushService, "deliver_after_commit", deliver)
            await notifications._deliver_response_push_batch(user.id, {notice.id}, 0)
            assert deliver.await_count == (0 if trashed else 1)
    finally:
        await engine.dispose()


def test_notification_model_deletes_notices_when_session_is_deleted() -> None:
    foreign_keys = {column.name: next(iter(column.foreign_keys)) for column in Notification.__table__.columns if column.foreign_keys}

    assert foreign_keys["user_id"].ondelete == "CASCADE"
    assert foreign_keys["session_id"].ondelete == "CASCADE"
    assert any(
        constraint.name == "uq_notifications_user_deduplication_key"
        for constraint in Notification.__table__.constraints
    )


async def test_session_deletion_and_migration_remove_only_session_notices(request) -> None:
    if not request.config.getoption("--memory-db"):
        pytest.skip("requires the migrated development PostgreSQL database")
    engine = create_async_engine(get_runtime_config().database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            await connection.begin()
            async with AsyncSession(connection, join_transaction_mode="create_savepoint", expire_on_commit=False) as db:
                user = _user()
                user.username = f"deleted-notice-{user.id}"
                db.add(user)
                await db.flush()
                session = ChatSession(user_id=user.id)
                db.add(session)
                await db.flush()
                linked, orphan, independent = [_notification(user.id) for _ in range(3)]
                linked.session_id = session.id
                linked.kind = orphan.kind = "agent.notice"
                independent.kind = "memory.approval"
                db.add_all([linked, orphan, independent])
                await db.flush()
                path = Path(__file__).parents[1] / "alembic/versions/d99f4a6c781b_cascade_session_notifications.py"
                upgrade = runpy.run_path(path)["upgrade"]

                def migrate(connection):
                    with Operations.context(MigrationContext.configure(connection)):
                        upgrade()

                await connection.run_sync(migrate)
                await db.delete(session)
                await db.flush()
                assert list(await db.scalars(select(Notification.id).where(
                    Notification.user_id == user.id,
                ))) == [independent.id]
    finally:
        await engine.dispose()


def test_notification_draft_normalizes_text_and_rejects_blanks() -> None:
    draft = NotificationDraft(
        user_id=uuid4(),
        kind="automation.completed",
        title="  Fertig  ",
        body="  Ergebnis  ",
        deduplication_key="  run:1  ",
    )

    assert (draft.title, draft.body, draft.deduplication_key) == (
        "Fertig",
        "  Ergebnis",
        "run:1",
    )
    with pytest.raises(ValidationError):
        NotificationDraft(
            user_id=uuid4(),
            kind="automation.completed",
            title="   ",
            deduplication_key="run:2",
        )


async def test_notification_creation_is_idempotent_and_does_not_commit() -> None:
    created_id = uuid4()
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = created_id
    draft = NotificationDraft(
        user_id=uuid4(),
        kind="automation.completed",
        title="Fertig",
        deduplication_key="automation-run:1:completed",
    )

    result = await NotificationService.create(db, draft)

    assert result == created_id
    statement = db.scalar.await_args.args[0]
    sql = str(statement)
    assert "INSERT INTO notifications" in sql
    assert "ON CONFLICT ON CONSTRAINT uq_notifications_user_deduplication_key DO NOTHING" in sql
    db.commit.assert_not_awaited()


@pytest.mark.parametrize("target", ["session_id", "approval_id"])
async def test_notification_creation_rejects_foreign_target(target) -> None:
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = None
    draft = NotificationDraft(
        user_id=uuid4(),
        **{target: uuid4()},
        kind="automation.completed",
        title="Fertig",
        deduplication_key="automation-run:1:completed",
    )

    with pytest.raises(NotificationError, match="notification.targetNotFound"):
        await NotificationService.create(db, draft)

    db.commit.assert_not_awaited()


@pytest.mark.parametrize("request_type", ["agent_action", "memory_change"])
async def test_approval_notice_preserves_snapshot_and_links_to_its_decision_context(monkeypatch, request_type) -> None:
    approval = ApprovalRequest(
        id=uuid4(), user_id=uuid4(), session_id=uuid4(), request_type=request_type,
        title="Freigabe erforderlich", summary="Vorschlag prüfen", created_at=datetime.now(UTC),
    )
    create = AsyncMock(return_value=uuid4())
    monkeypatch.setattr(NotificationService, "create", create)
    db = AsyncMock(spec=AsyncSession)
    await NotificationService.create_for_approval(db, approval)
    draft = create.await_args.args[1]
    item = Notification(id=uuid4(), **draft.model_dump(), created_at=approval.created_at)
    payload = _notification_payload(item).model_dump(mode="json", by_alias=True)
    if request_type == "memory_change":
        assert WebPushMemoryApprovalTarget.model_validate_json(
            json.dumps(payload["target"])
        ).model_dump(mode="json", by_alias=True) == payload["target"]
    assert (payload["title"], payload["body"], payload["target"]) == (
        approval.title, approval.summary,
        {"type": "memoryApproval", "approvalId": str(approval.id), "sessionId": str(approval.session_id)} if request_type == "memory_change"
        else {"type": "session", "sessionId": str(approval.session_id)},
    )


async def test_question_notice_uses_its_source_session(monkeypatch) -> None:
    owner_id = uuid4()
    request = QuestionRequest(id=uuid4(), session_id=uuid4(), questions=[{"question": "Welche Datei?"}])
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = owner_id
    create = AsyncMock()
    monkeypatch.setattr(NotificationService, "create", create)
    await NotificationService.create_for_question(db, request)
    draft = create.await_args.args[1]
    assert (draft.user_id, draft.session_id, draft.body, draft.deduplication_key) == (
        owner_id, request.session_id, "Welche Datei?", f"question:{request.id}",
    )


async def test_notification_page_is_owner_scoped_and_uses_full_keyset() -> None:
    user_id = uuid4()
    cursor_time = datetime(2026, 8, 14, 8, 0, tzinfo=UTC)
    cursor_id = uuid4()
    scalar_result = MagicMock()
    scalar_result.all.return_value = []
    db = AsyncMock(spec=AsyncSession)
    db.scalars.return_value = scalar_result

    await NotificationService.list_page(
        db, user_id, page_size=20, cursor=(cursor_time, cursor_id)
    )

    statement = db.scalars.await_args.args[0]
    sql = str(statement)
    assert "notifications.user_id =" in sql
    assert "notifications.created_at <" in sql
    assert "notifications.created_at =" in sql
    assert "notifications.id <" in sql
    assert "ORDER BY notifications.created_at DESC, notifications.id DESC" in sql
    assert 21 in statement.compile().params.values()


async def test_read_mutations_lock_the_user_before_notification_state() -> None:
    user = _user()
    item = _notification(user.id)
    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = [user.id, item]

    result = await NotificationService.mark_read(db, user.id, item.id)

    assert result is item
    assert item.read_at is not None
    first_statement, second_statement = [call.args[0] for call in db.scalar.await_args_list]
    assert "FROM user_accounts" in str(first_statement)
    assert "FOR UPDATE" in str(first_statement)
    assert "FROM notifications" in str(second_statement)
    assert "FOR UPDATE" in str(second_statement)


def test_notification_cursor_is_owner_bound() -> None:
    user_id = uuid4()
    created_at = datetime(2026, 8, 14, 8, 0, tzinfo=UTC)
    notification_id = uuid4()
    cursor = _encode_cursor(user_id, created_at, notification_id)

    assert _decode_cursor(cursor, user_id) == (created_at, notification_id)
    with pytest.raises(ApiError) as exc_info:
        _decode_cursor(cursor, uuid4())

    assert exc_info.value.code == "validation.invalid"
    assert exc_info.value.field == "cursor"


@pytest.mark.parametrize("source,expected", [
    (
        "# **Bericht**\n\n[Dokument](https://example.test/" + "x" * 600 + ")\n\n"
        "- `status_code`\n- ~~erledigt~~\n\n" + "weiter " * 90,
        "Bericht Dokument status_code erledigt" + " weiter" * 66 + "…",
    ),
    ('    print("**warning**")\n', 'print("**warning**")'),
])
async def test_notification_previews_remove_markdown_before_truncating(monkeypatch, source, expected) -> None:
    session = ChatSession(id=uuid4(), user_id=uuid4(), title="Test", session_type="chat")
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = session
    create = AsyncMock()
    monkeypatch.setattr(NotificationService, "create", create)
    monkeypatch.setattr("mato_loop_backend.notifications.session_is_present", lambda *_: False)
    await NotificationService.create_for_message(db, Message(
        id=uuid4(), session_id=session.id, role="assistant", content=source, created_at=datetime.now(UTC),
    ))
    item = Notification(id=uuid4(), **create.await_args.args[1].model_dump(), created_at=datetime.now(UTC))
    previews = [
        _notification_payload(item).body,
        web_push_payload(title=item.title, body=item.body, tag="notice", target=None).notification.body,
    ]
    assert previews == [expected] * 2


async def test_notification_list_returns_cursor_target_and_global_unread_count(monkeypatch) -> None:
    monkeypatch.setattr("mato_loop_backend.api.routes.notifications.MemoryService.reconcile_approvals", AsyncMock(return_value=False))
    user = _user()
    first = _notification(user.id)
    first.session_id = uuid4()
    overflow = _notification(
        user.id, created_at=datetime(2026, 8, 13, 8, 0, tzinfo=UTC)
    )
    monkeypatch.setattr(
        NotificationService, "list_page", AsyncMock(return_value=[first, overflow])
    )
    monkeypatch.setattr(NotificationService, "unread_count", AsyncMock(return_value=7))

    result = await list_notifications(user, AsyncMock(spec=AsyncSession), page_size=1)
    payload = result.model_dump(mode="json", by_alias=True)

    assert payload["unreadCount"] == 7
    assert payload["nextCursor"]
    assert payload["items"][0]["target"] == {
        "type": "session",
        "sessionId": str(first.session_id),
    }
    assert "deduplicationKey" not in payload["items"][0]


async def test_mark_read_is_owner_scoped_idempotent_and_committed(monkeypatch) -> None:
    user = _user()
    item = _notification(user.id)
    item.read_at = datetime(2026, 8, 14, 9, 0, tzinfo=UTC)
    db = AsyncMock(spec=AsyncSession)
    monkeypatch.setattr(NotificationService, "mark_read", AsyncMock(return_value=item))
    monkeypatch.setattr(NotificationService, "unread_count", AsyncMock(return_value=2))

    result = await mark_notification_read(item.id, user, db)

    assert result.unread_count == 2
    NotificationService.mark_read.assert_awaited_once_with(db, user.id, item.id)
    db.commit.assert_awaited_once()


async def test_mark_all_read_commits_and_returns_authoritative_count(monkeypatch) -> None:
    user = _user()
    db = AsyncMock(spec=AsyncSession)
    monkeypatch.setattr(NotificationService, "mark_all_read", AsyncMock(return_value=4))
    monkeypatch.setattr(NotificationService, "unread_count", AsyncMock(return_value=1))

    result = await mark_all_notifications_read(user, db)

    assert result.updated_count == 4
    assert result.unread_count == 1
    db.commit.assert_awaited_once()


@pytest.mark.parametrize("mode,kind", [
    (mode, kind)
    for mode in ["session", "individual", "all"]
    for kind in ["session.response", "approval.requested", "question.requested"]
] + [("migration", "session.response")])
async def test_session_notices_are_deleted_on_read_without_losing_other_notices(request, mode, kind) -> None:
    if not request.config.getoption("--memory-db"):
        pytest.skip("requires the migrated development PostgreSQL database")
    engine = create_async_engine(get_runtime_config().database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            await connection.begin()
            async with AsyncSession(connection, join_transaction_mode="create_savepoint", expire_on_commit=False) as db:
                user, other = _user(), _user()
                user.username, other.username = f"notice-{user.id}", f"notice-{other.id}"
                db.add_all([user, other])
                await db.flush()
                session = ChatSession(user_id=user.id, title="Read boundary")
                db.add(session)
                await db.flush()
                through = datetime.now(UTC) - timedelta(seconds=2)
                items = [_notification(owner.id, created_at=through + timedelta(seconds=offset))
                         for owner, offset in [(user, 0), (user, 1), (user, 0), (other, 0), (user, -1), (user, 0.5)]]
                for item in [items[0], items[1], items[3], items[4]]:
                    item.kind = kind
                items[2].kind = "memory.approval"
                items[5].kind = "agent.notice"
                for item in [*items[:3], *items[4:]]:
                    item.session_id = session.id
                if mode == "migration":
                    for item in [items[0], items[2], items[4]]:
                        item.read_at = through
                db.add_all(items)
                await db.flush()
                ids = [item.id for item in items]
                if mode == "session":
                    await NotificationService.mark_session_read(
                        db, user.id, session.id, through if kind == "session.response" else None,
                    )
                elif mode == "individual":
                    await mark_notification_read(items[0].id, user, db)
                    result = await mark_notification_read(items[0].id, user, db)
                    assert result.notification is None
                    await mark_notification_read(items[3].id, user, db)
                elif mode == "all":
                    await mark_all_notifications_read(user, db)
                else:
                    path = Path(__file__).parents[1] / "alembic/versions/b92e7594d532_remove_read_response_notifications.py"
                    upgrade = runpy.run_path(path)["upgrade"]

                    def cleanup(connection):
                        with Operations.context(MigrationContext.configure(connection)):
                            upgrade()

                    await connection.run_sync(cleanup)
                remaining = (await db.execute(select(Notification.id, Notification.read_at).where(
                    Notification.id.in_(ids),
                ))).all()
                assert {(item.id, item.read_at is not None) for item in remaining} == {
                    (ids[2], mode != "individual"), (ids[3], False),
                    (ids[5], mode in {"session", "all"}),
                    *([(ids[1], False)] if mode in {"individual", "migration"}
                      or (mode == "session" and kind == "session.response") else []),
                    *([(ids[4], False)] if mode == "individual" else []),
                }
    finally:
        await engine.dispose()


async def test_parallel_request_notices_release_connections_before_push(request, monkeypatch) -> None:
    if not request.config.getoption("--memory-db"):
        pytest.skip("requires the migrated development PostgreSQL database")
    engine = create_async_engine(get_runtime_config().database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    user = _user()
    user.username = f"parallel-notice-{user.id}"
    user_id = user.id
    monkeypatch.setattr("mato_loop_backend.notifications.AsyncSessionFactory", sessions)

    async def deliver(*_args):
        assert engine.pool.checkedout() == 0

    push = AsyncMock(side_effect=deliver)
    monkeypatch.setattr("mato_loop_backend.notifications.WebPushService.deliver_after_commit", push)
    try:
        async with sessions() as db:
            db.add(user)
            await db.flush()
            session = ChatSession(user_id=user_id, title="Concurrent requests")
            db.add(session)
            await db.commit()
            session_id = session.id
        flushed = asyncio.Barrier(2)

        async def create_request():
            async with sessions() as db:
                approval = ApprovalRequest(
                    user_id=user_id, session_id=session_id, request_type="agent_action",
                    action_kind="read", title="Lesen erlauben?", summary="Datei lesen",
                    capability="filesystem.read", resource_type="path", resource_scope="/notes",
                )
                db.add(approval)
                await db.flush()
                await flushed.wait()
                notification_id = await NotificationService.create_for_approval(db, approval)
                await db.commit()
                return notification_id

        async with asyncio.timeout(10), asyncio.TaskGroup() as tasks:
            pending = [tasks.create_task(create_request()) for _ in range(2)]
        for task in pending:
            await NotificationService.deliver_after_commit(task.result())
        assert push.await_count == 2
    finally:
        async with sessions() as db:
            await db.execute(delete(UserAccount).where(UserAccount.id == user_id))
            await db.commit()
        await engine.dispose()


@pytest.mark.parametrize("already_read", [False, True])
async def test_response_notification_is_skipped_when_seen(monkeypatch, already_read) -> None:
    now = datetime.now(UTC)
    session = ChatSession(
        id=uuid4(), user_id=uuid4(), title="Test", session_type="chat",
        last_read_at=now if already_read else None,
    )
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = session
    monkeypatch.setattr("mato_loop_backend.notifications.session_is_present", lambda *_: not already_read)
    create = AsyncMock()
    monkeypatch.setattr(NotificationService, "create", create)
    await NotificationService.create_for_message(db, Message(
        id=uuid4(), session_id=session.id, role="assistant", content="Done", created_at=now,
    ))
    create.assert_not_awaited()


@pytest.mark.parametrize("kind", ["approval.requested", "question.requested", "memory.approval", "agent.notice"])
async def test_session_request_notices_are_skipped_while_present(monkeypatch, kind) -> None:
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = uuid4()
    monkeypatch.setattr("mato_loop_backend.notifications.session_is_present", lambda *_: True)
    result = await NotificationService.create(db, NotificationDraft(
        user_id=uuid4(), session_id=uuid4(), kind=kind,
        title="Bitte prüfen", deduplication_key="request:1",
    ))
    assert (result is None) == (kind in {"approval.requested", "question.requested"})


async def test_presence_rejects_another_owners_session() -> None:
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = None
    with pytest.raises(ApiError, match="session.notFound"):
        await mark_notification_presence(_user(), db, NotificationPresenceRequest(
            clientId=uuid4(), sessionId=uuid4(),
        ))


async def test_delete_notification_is_owner_scoped_and_committed() -> None:
    user, notification_id = _user(), uuid4()
    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = [notification_id, 2]
    result = await delete_notification(notification_id, user, db)
    statement = db.scalar.await_args_list[0].args[0]
    assert user.id in statement.compile().params.values()
    assert result == {"unreadCount": 2}
    db.commit.assert_awaited_once()


@pytest.mark.parametrize("created", [True, False])
async def test_agent_notification_uses_current_owner_and_session(monkeypatch, created) -> None:
    from types import SimpleNamespace

    from mato_loop_backend.tools.arguments import NotifyArguments
    from mato_loop_backend.tools.interaction import _notify

    session = ChatSession(id=uuid4(), user_id=uuid4())
    db = AsyncMock(spec=AsyncSession)
    notification_id, run_id = uuid4(), uuid4()
    create, deliver = AsyncMock(return_value=(notification_id, created)), AsyncMock()
    monkeypatch.setattr(NotificationService, "upsert_agent_notice", create)
    monkeypatch.setattr(NotificationService, "deliver_after_commit", deliver)
    result = await _notify(NotifyArguments(title="Preis gefallen", body="Unter 10 Euro."), SimpleNamespace(
        db=db, session=session, run_id=run_id, tool_call=SimpleNamespace(id="call-1"),
    ))
    draft = create.await_args.args[1]
    assert (draft.user_id, draft.session_id, draft.kind) == (session.user_id, session.id, "agent.notice")
    assert draft.deduplication_key == f"tool:{run_id}"
    assert json.loads(result.content) == {"notificationId": str(notification_id), "created": created, "updated": not created}
    db.commit.assert_awaited_once()
    deliver.assert_awaited_once_with(notification_id, silent=not created)


@pytest.mark.parametrize("outcome", ["notify", "silence", "normal", "other_run", "tool_error", "runtime_error", "earlier_unread"])
async def test_response_notification_uses_persisted_current_run_tool_outcome(request, monkeypatch, outcome) -> None:
    if not request.config.getoption("--memory-db"):
        pytest.skip("requires the migrated development PostgreSQL database")
    from types import SimpleNamespace

    from mato_loop_backend.agent.tool_round import persist_tool_result
    from mato_loop_backend.api.routes.projects import _unread_project_sessions
    from mato_loop_backend.media.attachments import AttachmentService
    from mato_loop_backend.models import AgentRun, Project
    from mato_loop_backend.providers.base import ToolCall
    from mato_loop_backend.sessions.service import list_sessions
    from mato_loop_backend.settings import InstanceLimits, TrustLevel
    from mato_loop_backend.tools.arguments import NotifyArguments, ToolArguments
    from mato_loop_backend.tools.contracts import ToolResult
    from mato_loop_backend.tools.interaction import _notify, _silence

    monkeypatch.setattr("mato_loop_backend.notifications.session_is_present", lambda *_: False)
    monkeypatch.setattr(NotificationService, "deliver_after_commit", AsyncMock())
    engine = create_async_engine(get_runtime_config().database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            await connection.begin()
            async with AsyncSession(connection, join_transaction_mode="create_savepoint", expire_on_commit=False) as db:
                user = _user()
                user.username = f"quiet-{user.id}"
                db.add(user)
                await db.flush()
                project = Project(user_id=user.id, name="Check")
                db.add(project)
                await db.flush()
                session = ChatSession(user_id=user.id, project_id=project.id, title="Check", last_read_at=datetime.now(UTC) - timedelta(seconds=1))
                db.add(session)
                await db.flush()
                if outcome == "earlier_unread":
                    db.add(Message(session_id=session.id, role="assistant", content="Earlier unread answer"))
                run = AgentRun(id=uuid4(), session_id=session.id)
                db.add(run)
                await db.flush()
                call = ToolCall("notice", "notify" if outcome == "notify" else "silence", {})
                context = SimpleNamespace(db=db, session=session, run_id=run.id, tool_call=call)
                result = await _notify(NotifyArguments(title="Done", body="Finished"), context) if outcome == "notify" else await _silence(ToolArguments(), context)
                if outcome == "notify":
                    for priority in ("warning", "urgent"):
                        context.tool_call = ToolCall(f"correct-{priority}", "notify", {})
                        await _notify(NotifyArguments(title="Corrected", body="Complete", priority=priority), context)
                    notices = list(await db.scalars(select(Notification).where(Notification.user_id == user.id)))
                    assert [(_notification_payload(item).title, item.body, item.priority, str(item.id)) for item in notices] == [
                        ("🚨 Corrected", "Complete", "urgent", json.loads(result.content)["notificationId"]),
                    ]
                    # Removing a notice must not restore the duplicate completion notification.
                    await db.execute(delete(Notification).where(Notification.user_id == user.id))
                    await _notify(NotifyArguments(title="Another correction", body="Still complete"), context)
                    assert list(await db.scalars(select(Notification.id).where(Notification.user_id == user.id))) == []
                if outcome == "tool_error":
                    result = ToolResult.error("tool.unavailable")
                if outcome != "normal":
                    await persist_tool_result(
                        db, SimpleNamespace(id=uuid4()) if outcome == "other_run" else run, session, call,
                        content=result.content, metadata=result.metadata, attachments=(), file_actions=(),
                        trust_level=TrustLevel.PUBLIC, ephemeral_output=False,
                        limits=InstanceLimits(), attachment_service=AttachmentService,
                    )
                message = Message(
                    session_id=session.id, role="assistant", content="Still running",
                    error_code="agent.failed" if outcome == "runtime_error" else None,
                    error_run_id=run.id if outcome == "runtime_error" else None,
                    provider_data={"_matoloop": {"runId": str(run.id)}},
                )
                db.add(message)
                await db.flush()
                await NotificationService.create_for_message(db, message)
                kinds = list((await db.scalars(select(Notification.kind).where(Notification.user_id == user.id))).all())
                assert kinds == ([] if outcome in {"notify", "silence", "earlier_unread"} else ["session.response"])

                async def serialize(_db, item, **kwargs):
                    return {"id": str(item.id), "unread": kwargs["has_unread_messages"]}

                async def serializer_factory(_db, _sessions):
                    return serialize

                summaries = await list_sessions(
                    db, user.id, page_size=10, cursor=None, project_id=project.id,
                    standalone=False, session_serializer_factory=serializer_factory, sort="recent",
                )
                project_unread = await db.scalar(
                    select(_unread_project_sessions()).select_from(Project).where(Project.id == project.id)
                )
                assert (summaries["items"][0]["unread"], project_unread) == (outcome != "silence",) * 2
    finally:
        await engine.dispose()
