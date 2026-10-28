from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import Text
from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend.agent.run_support import approval_payload
from mato_loop_backend.agent.session_secrets import SessionSecretStore
from mato_loop_backend.api.errors import ApiError
from mato_loop_backend.api.routes.approvals import (
    _permission_payload,
    all_permissions,
    list_permissions,
    pending_approvals,
    revoke_permissions,
)
from mato_loop_backend.models import (
    ApprovalRequest,
    ChatSession,
    ExecutionAgent,
    PermissionGrant,
    UserAccount,
)
from mato_loop_backend.permissions.policy import (
    administrator_permission,
    execution_directory_scope,
    shell_action_scope,
)
from mato_loop_backend.settings import ToolSettings, TrustLevel


def _approval(user_id, session_id, created_at: datetime, *, request_type="agent_action") -> ApprovalRequest:
    return ApprovalRequest(
        id=uuid4(),
        request_type=request_type,
        user_id=user_id,
        session_id=session_id,
        run_id=uuid4(),
        action_kind="session_read",
        risk_level="medium",
        title="Session lesen?",
        summary="Andere Session lesen",
        capability="session.read",
        resource_type="session",
        resource_scope=str(uuid4()),
        payload={"resourceDisplay": "Projekt"},
        available_decisions=["allow_once", "deny"],
        status="pending",
        created_at=created_at,
    )


@pytest.mark.parametrize("writable,denied", [(False, False), (True, False), (True, True)])
async def test_permissions_include_effective_session_scratch_rights(monkeypatch, writable, denied):
    user = UserAccount(id=uuid4(), username="owner")
    agent = ExecutionAgent(id=uuid4(), user_id=user.id, alias="Computer", external_filesystem_policy="deny" if denied else "ask")
    session = ChatSession(id=uuid4(), user_id=user.id, preferred_execution_agent_id=agent.id)
    db = AsyncMock(spec=AsyncSession)
    db.get.side_effect = lambda model, _id: session if model is ChatSession else agent
    db.scalar.return_value = agent
    db.scalars.return_value = MagicMock(all=lambda: [])
    settings = ToolSettings()
    settings._temporary_files_enabled = writable
    monkeypatch.setattr("mato_loop_backend.api.routes.approvals._permission_binding", AsyncMock(return_value=(None, TrustLevel.PRIVATE)))
    monkeypatch.setattr("mato_loop_backend.api.routes.approvals.load_agent_profile_tools", AsyncMock(return_value=(settings, None)))
    monkeypatch.setattr("mato_loop_backend.execution.manager.execution_agent_connections.system_info", lambda _id: SimpleNamespace(temporary_directory="/tmp/mato-loop"))
    result = await list_permissions(session.id, user, db)
    assert [(item["source"], item["resourceDisplay"], item["applicable"], item["derived"]) for item in result] == [
        (source, f"/tmp/mato-loop/{session.id}", not denied, True)
        for source in (["default", "profile"] if writable else ["default"])
    ]


async def test_permissions_include_only_names_of_remembered_session_values(monkeypatch):
    user = UserAccount(id=uuid4())
    session = ChatSession(id=uuid4(), user_id=user.id)
    store = SessionSecretStore()
    references = store.put(session.id, {"TOKEN": "private token", "USERNAME": "private username"})
    store.put(uuid4(), {"OTHER_TOKEN": "other session token"})
    monkeypatch.setattr("mato_loop_backend.api.routes.approvals.session_secrets", store)
    monkeypatch.setattr("mato_loop_backend.api.routes.approvals._permission_binding", AsyncMock(return_value=(None, TrustLevel.PRIVATE)))
    db = AsyncMock(spec=AsyncSession)
    db.get.return_value = session
    db.scalars.return_value = MagicMock(all=lambda: [])

    assert await list_permissions(session.id, user, db) == [
        {
            "id": reference, "sessionId": str(session.id),
            "resourceType": "session_secret", "resourceDisplay": name,
            "source": "session_input", "scope": "session", "readOnly": True,
        }
        for name, reference in references.items()
    ]


async def test_pending_approval_payload_includes_request_and_source_session() -> None:
    user = UserAccount(id=uuid4(), username="test", password_hash="hash")
    session = ChatSession(id=uuid4(), user_id=user.id, title="Projekt Aurora")
    approval = _approval(user.id, session.id, datetime(2026, 8, 11, 9, 0, tzinfo=UTC))
    execution_result = MagicMock()
    execution_result.all.return_value = [(approval, session)]
    db = AsyncMock(spec=AsyncSession)
    db.execute.return_value = execution_result

    result = await pending_approvals(user, db)

    assert result == [
        {
            **approval_payload(approval),
            "sourceSession": {"id": str(session.id), "title": session.title},
        }
    ]


async def test_pending_approvals_query_filters_owner_and_orders_oldest_first() -> None:
    user = UserAccount(id=uuid4(), username="test", password_hash="hash")
    session = ChatSession(id=uuid4(), user_id=user.id, title="Eigene Session")
    oldest = _approval(user.id, session.id, datetime(2026, 8, 11, 8, 0, tzinfo=UTC))
    newest = _approval(user.id, session.id, datetime(2026, 8, 11, 10, 0, tzinfo=UTC))
    execution_result = MagicMock()
    execution_result.all.return_value = [(oldest, session), (newest, session)]
    db = AsyncMock(spec=AsyncSession)
    db.execute.return_value = execution_result

    result = await pending_approvals(user, db)

    assert [item["id"] for item in result] == [str(oldest.id), str(newest.id)]
    statement = db.execute.await_args.args[0]
    sql = str(statement)
    assert "LEFT OUTER JOIN chat_sessions ON chat_sessions.id = approval_requests.session_id" in sql
    assert "approval_requests.user_id =" in sql
    assert "approval_requests.action_kind !=" in sql
    assert "ORDER BY approval_requests.created_at" in sql
    assert {"pending", user.id}.issubset(set(statement.compile().params.values()))


async def test_pending_memory_approval_has_no_required_session() -> None:
    user = UserAccount(id=uuid4(), username="test", password_hash="hash")
    approval = _approval(
        user.id,
        None,
        datetime(2026, 8, 11, 9, 0, tzinfo=UTC),
        request_type="memory_change",
    )
    approval.run_id = None
    execution_result = MagicMock()
    execution_result.all.return_value = [(approval, None)]
    db = AsyncMock(spec=AsyncSession)
    db.execute.return_value = execution_result

    result = await pending_approvals(user, db)

    assert result[0]["requestType"] == "memory_change"
    assert result[0]["sessionId"] is None
    assert result[0]["runId"] is None
    assert result[0]["sourceSession"] is None


async def test_all_permissions_include_owned_session_context(monkeypatch) -> None:
    monkeypatch.setattr("mato_loop_backend.api.routes.approvals._permission_binding", AsyncMock(return_value=(None, TrustLevel.PRIVATE)))
    user = UserAccount(id=uuid4(), username="test", password_hash="hash")
    first_session = ChatSession(id=uuid4(), user_id=user.id, title="Projekt Aurora")
    second_session = ChatSession(id=uuid4(), user_id=user.id, title="Recherche")
    created_at = datetime(2026, 8, 11, 9, 0, tzinfo=UTC)
    source = _approval(user.id, first_session.id, created_at)
    source.payload = {
        "resourceDisplay": "/home/test/projekt",
        "shellClassification": "read",
        "shellPrefixes": [["git", "status"]],
    }
    shell_grant = PermissionGrant(
        id=uuid4(),
        session_id=first_session.id,
        capability="shell.read",
        resource_type="shell_command",
        resource_scope="scope-1",
        source_approval_id=source.id,
        created_at=created_at,
    )
    session_grant = PermissionGrant(
        id=uuid4(),
        session_id=second_session.id,
        capability="session.search",
        resource_type="session_collection",
        resource_scope="*",
        created_at=created_at,
    )
    execution_result = MagicMock()
    execution_result.all.return_value = [
        (shell_grant, first_session, source),
        (session_grant, second_session, None),
    ]
    db = AsyncMock(spec=AsyncSession)
    db.execute.return_value = execution_result

    result = await all_permissions(user, db)

    assert [item["sessionId"] for item in result] == [
        str(first_session.id),
        str(second_session.id),
    ]
    assert result[0]["sourceSession"] == {
        "id": str(first_session.id),
        "title": "Projekt Aurora",
    }
    assert result[0]["resourceDisplay"] == "/home/test/projekt"
    assert result[0]["shellPrefixes"] == [["git", "status"]]
    assert result[1]["sourceSession"]["title"] == "Recherche"
    statement = db.execute.await_args.args[0]
    sql = str(statement)
    assert "JOIN chat_sessions ON chat_sessions.id = permission_grants.session_id" in sql
    assert "LEFT OUTER JOIN approval_requests" in sql
    assert "chat_sessions.user_id =" in sql
    assert "ORDER BY permission_grants.created_at" in sql
    assert user.id in statement.compile().params.values()


@pytest.mark.parametrize("kind", ["execution_directory", "execution_path", "administrator_path", "shell_action", "shell_computer", "run_action"])
@pytest.mark.parametrize("same_computer", [True, False])
async def test_permission_target_display_does_not_require_source_approval(kind, same_computer):
    user = UserAccount(id=uuid4())
    computer = ExecutionAgent(id=uuid4(), user_id=user.id, alias="Server")
    session = ChatSession(id=uuid4(), user_id=user.id, current_directory="/work",
        preferred_execution_agent_id=computer.id if same_computer else uuid4())
    grant = PermissionGrant(id=uuid4(), session_id=session.id, resource_type=kind,
        resource_scope=shell_action_scope(computer.id, "/work", "pwd") if kind == "shell_action" else execution_directory_scope(computer.id, "/work"),
        capability="execution.directory" if kind == "execution_directory" else "resource.read",
        trust_level="public", created_at=datetime.now(UTC))
    source = None
    if kind == "shell_computer":
        grant.resource_scope, grant.capability = str(computer.id), "category.read"
        session.current_directory = "/other"
    if kind == "run_action":
        request = administrator_permission(computer.id, "/work", capability="shell.read", command="cat /root/.ssh/config")
        source = _approval(user.id, session.id, grant.created_at)
        source.resource_type, source.resource_scope, source.capability = request.resource_type, request.resource_scope, request.capability
        grant.source_approval_id, grant.resource_scope = source.id, "a" * 64
    db = AsyncMock(spec=AsyncSession)
    db.get.return_value = computer
    payload = await _permission_payload(db, user, grant, source, session, (None, TrustLevel.PUBLIC))
    assert (payload["resourceDisplay"], payload["executionAgentName"], payload["applicable"], payload.get("command"), payload.get("elevated", False)) == (
        "Shell" if kind == "shell_computer" else "/work", "Server", same_computer, "cat /root/.ssh/config" if kind == "run_action" else "pwd" if kind == "shell_action" else None,
        kind in {"administrator_path", "run_action"},
    )


async def test_revoke_permissions_deletes_only_session_wide_grants_for_owned_session() -> None:
    user = UserAccount(id=uuid4(), username="test", password_hash="hash")
    session = ChatSession(id=uuid4(), user_id=user.id, title="Projekt Aurora")
    db = AsyncMock(spec=AsyncSession)
    db.get.return_value = session

    await revoke_permissions(session.id, user, db)

    statement = db.execute.await_args.args[0]
    assert "DELETE FROM permission_grants" in str(statement)
    assert "permission_grants.session_id =" in str(statement)
    assert "permission_grants.run_id IS NULL" in str(statement)
    assert session.id in statement.compile().params.values()
    db.commit.assert_awaited_once()


async def test_revoke_permissions_rejects_foreign_session() -> None:
    user = UserAccount(id=uuid4(), username="test", password_hash="hash")
    session = ChatSession(id=uuid4(), user_id=uuid4(), title="Fremde Session")
    db = AsyncMock(spec=AsyncSession)
    db.get.return_value = session

    with pytest.raises(ApiError) as exc_info:
        await revoke_permissions(session.id, user, db)

    assert exc_info.value.code == "approval.notFound"
    db.execute.assert_not_awaited()
    db.commit.assert_not_awaited()


def test_approval_envelope_metadata_supports_owned_optional_sources() -> None:
    table = ApprovalRequest.__table__

    assert table.c.user_id.nullable is False
    assert table.c.user_id.index is True
    assert table.c.request_type.server_default is not None
    assert table.c.session_id.nullable is True
    assert next(iter(table.c.session_id.foreign_keys)).ondelete == "SET NULL"
    assert table.c.run_id.nullable is True
    assert next(iter(table.c.run_id.foreign_keys)).ondelete == "SET NULL"
    assert "ix_approval_requests_user_pending" in {index.name for index in table.indexes}


def test_resource_scope_columns_are_unbounded() -> None:
    assert isinstance(ApprovalRequest.__table__.c.resource_scope.type, Text)
    assert isinstance(PermissionGrant.__table__.c.resource_scope.type, Text)
