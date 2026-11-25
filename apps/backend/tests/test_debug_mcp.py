import json
import logging
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import delete, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.applications import Starlette
from starlette.routing import Mount

import mato_loop_backend.diagnostics.mcp as diagnostics_mcp
from mato_loop_backend.db.session import AsyncSessionFactory
from mato_loop_backend.diagnostics import logs
from mato_loop_backend.diagnostics.service import record_diagnostic_event
from mato_loop_backend.models import (
    ApprovalRequest,
    ChatSession,
    DiagnosticEvent,
    Message,
    UserAccount,
)
from mato_loop_backend.security import hash_token


async def test_debug_token_verifier_binds_active_admin(monkeypatch) -> None:
    user = UserAccount(
        id=uuid4(),
        username="admin",
        is_active=True,
        is_admin=True,
        debug_token_hash=hash_token("secret"),
    )
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = user

    @asynccontextmanager
    async def session_factory():
        yield db

    monkeypatch.setattr(diagnostics_mcp, "AsyncSessionFactory", session_factory)

    access = await diagnostics_mcp.DebugTokenVerifier().verify_token("secret")

    assert access is not None
    assert access.subject == str(user.id)
    assert access.scopes == ["debug:read"]
    statement = db.scalar.await_args.args[0].compile(dialect=postgresql.dialect())
    sql = str(statement)
    assert "user_accounts.debug_token_hash" in sql
    assert "user_accounts.is_active IS true" in sql
    assert "user_accounts.is_admin IS true" in sql
    assert hash_token("secret") in statement.params.values()


async def test_debug_token_verifier_rejects_unknown_token(monkeypatch) -> None:
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = None

    @asynccontextmanager
    async def session_factory():
        yield db

    monkeypatch.setattr(diagnostics_mcp, "AsyncSessionFactory", session_factory)

    assert await diagnostics_mcp.DebugTokenVerifier().verify_token("unknown") is None


async def test_debug_tool_errors_do_not_expose_exception_text() -> None:
    async def broken() -> dict:
        raise RuntimeError("database password leaked")

    result = await diagnostics_mcp._safe_tool(broken)()

    assert result == {
        "error": {"code": "debug.queryFailed", "message": "The debug query failed."}
    }


def test_debug_list_content_is_bounded() -> None:
    payload = diagnostics_mcp._compact_message(
        {"content": "x" * 3_000, "toolCalls": [{"arguments": "y" * 3_000}]}
    )

    assert len(payload["content"]) == 2_000
    assert payload["contentLength"] == 3_000
    assert payload["toolCalls"] is None
    assert payload["detailFields"]["toolCalls"] > 3_000


def test_debug_approval_payload_includes_resolution_without_duplicate_details() -> None:
    resolved_at = datetime.now(UTC)
    approval = ApprovalRequest(
        id=uuid4(),
        user_id=uuid4(),
        action_kind="shell_run",
        risk_level="low",
        title="Run command?",
        summary="Command may have side effects.",
        capability="shell.read",
        resource_type="shell_command",
        resource_scope="{}",
        payload={"tool": "shell_run"},
        available_decisions=["allow_once", "deny"],
        status="approved",
        decision="allow_once",
        created_at=resolved_at - timedelta(seconds=1),
        resolved_at=resolved_at,
    )

    payload = diagnostics_mcp._debug_approval_payload(approval)

    assert payload["decision"] == "allow_once"
    assert payload["resolvedAt"] == resolved_at.isoformat()
    assert payload["payload"] == {"tool": "shell_run"}
    assert "details" not in payload


async def test_unavailable_debug_mount_returns_controlled_error() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=diagnostics_mcp.DebugMcpMount()),
        base_url="https://matoloop.test",
    ) as client:
        response = await client.get("/mcp")

    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "code": "debug.unavailable",
            "message": "The debug service is unavailable.",
        }
    }


async def test_debug_mcp_http_authenticates_and_filters_sessions(monkeypatch, tmp_path) -> None:
    user = UserAccount(id=uuid4(), username="admin", is_active=True, is_admin=True)
    session = ChatSession(
        id=uuid4(),
        user_id=user.id,
        title="Broken chat",
        session_type="chat",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = user
    scalar_result = MagicMock()
    scalar_result.all.return_value = [session]
    db.scalars.return_value = scalar_result

    @asynccontextmanager
    async def session_factory():
        yield db

    monkeypatch.setattr(diagnostics_mcp, "AsyncSessionFactory", session_factory)
    monkeypatch.setattr(logs, "get_runtime_config", lambda: SimpleNamespace(data_dir=tmp_path))
    (tmp_path / "logs").mkdir()
    log_record = {
        "timestamp": "2026-10-06T14:58:13+00:00",
        "level": "WARNING",
        "logger": "mato_loop_backend.integrations.mcp.oauth",
        "message": "Login failed",
    }
    (tmp_path / "logs" / "backend.log").write_text(json.dumps(log_record) + "\n")
    server, mcp_app = diagnostics_mcp.create_debug_mcp()
    app = Starlette(routes=[Mount("/api/debug", app=mcp_app)])
    transport = httpx.ASGITransport(app=app)

    async with server.session_manager.run():
        async with httpx.AsyncClient(
            transport=transport,
            base_url="https://matoloop.test",
            headers={"Authorization": "Bearer secret"},
        ) as client:
            async with streamable_http_client(
                "https://matoloop.test/api/debug/mcp", http_client=client
            ) as (read, write, _session_id):
                async with ClientSession(read, write) as mcp_session:
                    await mcp_session.initialize()
                    result = await mcp_session.call_tool("list_sessions", {"limit": 1})
                    log_result = await mcp_session.call_tool(
                        "get_backend_logs", {"limit": 1, "level": "WARNING"}
                    )

    assert result.isError is False
    assert result.structuredContent == {
        "items": [
            {
                "id": str(session.id),
                "title": "Broken chat",
                "type": "chat",
                "projectId": None,
                "parentSessionId": None,
                "currentDirectory": None,
                "createdAt": session.created_at.isoformat(),
                "updatedAt": session.updated_at.isoformat(),
            }
        ]
    }
    statement = db.scalars.await_args.args[0].compile(dialect=postgresql.dialect())
    assert user.id in statement.params.values()
    assert log_result.structuredContent == {"items": [log_record], "truncated": False}


def test_backend_logs_rotate_filter_and_bound(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        logs, "get_runtime_config", lambda: SimpleNamespace(data_dir=tmp_path, log_level="INFO")
    )
    logger = logging.getLogger("mato_loop_backend")
    previous_level = logger.level
    handler = logs.install_backend_log_handler()
    try:
        logger.info("Skip below minimum severity")
        logger.warning("Before rotation")
        handler.doRollover()
        child = logging.getLogger("mato_loop_backend.integrations.mcp.oauth")
        child.warning("After rotation")
        assert [record["message"] for record in logs.read_backend_logs(2, level="WARNING")["items"]] == [
            "Before rotation", "After rotation",
        ]
        assert [record["message"] for record in logs.read_backend_logs(2, child.name)["items"]] == [
            "After rotation",
        ]
        for _ in range(140):
            child.warning("x" * 8_000)
        result = logs.read_backend_logs(500)
        assert result["truncated"] and len(json.dumps(result)) <= logs.MAX_REPLY_CHARACTERS
        assert all(file.stat().st_size <= logs.MAX_LOG_BYTES for file in (tmp_path / "logs").iterdir())
    finally:
        logger.removeHandler(handler)
        handler.close()
        logger.setLevel(previous_level)


def test_backend_log_tracebacks_exclude_exception_values() -> None:
    try:
        raise RuntimeError("private-token")
    except RuntimeError:
        record = logging.LogRecord("mato_loop_backend", logging.ERROR, "", 0, "Operation failed", (), sys.exc_info())
    text = logs.BackendLogFormatter().format(record)
    assert "private-token" not in text and json.loads(text)["exceptionType"] == "RuntimeError"


async def test_debug_mcp_loads_owner_model_defaults(monkeypatch) -> None:
    user = UserAccount(id=uuid4(), username="admin", is_active=True, is_admin=True)
    session = ChatSession(id=uuid4(), user_id=user.id, agent_profile_id=uuid4())
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = user

    @asynccontextmanager
    async def session_factory():
        yield db

    profile_ai = AsyncMock(side_effect=RuntimeError("stop after profile lookup"))
    monkeypatch.setattr(diagnostics_mcp, "AsyncSessionFactory", session_factory)
    monkeypatch.setattr(diagnostics_mcp, "_owned_session", AsyncMock(return_value=session))
    monkeypatch.setattr(diagnostics_mcp, "load_effective_ai", profile_ai)
    server, mcp_app = diagnostics_mcp.create_debug_mcp()
    app = Starlette(routes=[Mount("/api/debug", app=mcp_app)])

    async with server.session_manager.run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://matoloop.test",
            headers={"Authorization": "Bearer secret"},
        ) as client:
            async with streamable_http_client(
                "https://matoloop.test/api/debug/mcp", http_client=client
            ) as (read, write, _session_id):
                async with ClientSession(read, write) as mcp_session:
                    await mcp_session.initialize()
                    await mcp_session.call_tool("inspect_session", {"session_id": str(session.id)})
                    await mcp_session.call_tool(
                        "inspect_tool_access", {"session_id": str(session.id)}
                    )

    assert profile_ai.await_args_list[0].args[1:] == (user.id,)
    assert profile_ai.await_args_list[1].args[1:] == (user.id,)


async def test_debug_mcp_postgres_isolates_two_admins(request) -> None:
    if not request.config.getoption("--debug-db"):
        pytest.skip("requires --debug-db")
    first = UserAccount(
        username=f"debug-{uuid4()}",
        is_active=True,
        is_admin=True,
        debug_token_hash=hash_token("first-secret"),
    )
    second = UserAccount(
        username=f"debug-{uuid4()}",
        is_active=True,
        is_admin=True,
    )
    async with AsyncSessionFactory() as db:
        db.add_all([first, second])
        await db.flush()
        own = ChatSession(user_id=first.id, title="Own diagnostic session")
        foreign = ChatSession(user_id=second.id, title="Foreign diagnostic session")
        db.add_all([own, foreign])
        await db.flush()
        created_at = datetime.now(UTC)
        messages = [
            Message(
                session_id=own.id,
                role=role,
                content=f"message-{index}",
                created_at=created_at + timedelta(seconds=index),
            )
            for index, role in enumerate(("user", "assistant", "tool", "assistant"))
        ]
        db.add_all(messages)
        await db.commit()

    await record_diagnostic_event(
        first.id, "test.recorded", component="test", session_id=own.id
    )
    await record_diagnostic_event(
        second.id, "test.not-recorded", component="test", session_id=foreign.id
    )
    await record_diagnostic_event(
        first.id, "test.cross-owner", component="test", session_id=foreign.id
    )
    async with AsyncSessionFactory() as db:
        event_types = set(
            (
                await db.scalars(
                    select(DiagnosticEvent.event_type).where(
                        DiagnosticEvent.user_id.in_([first.id, second.id])
                    )
                )
            ).all()
        )
    assert event_types == {"test.recorded"}
    for tool, code in (("run_tool_script", "tool.scriptStepFailed"), ("run_tool_script", None), ("read", "file.notFound")):
        await record_diagnostic_event(
            first.id, "tool.completed", component="tool", session_id=own.id,
            payload={"name": tool, "errorCode": code},
        )

    server, mcp_app = diagnostics_mcp.create_debug_mcp()
    app = Starlette(routes=[Mount("/api/debug", app=mcp_app)])
    transport = httpx.ASGITransport(app=app)
    try:
        async with server.session_manager.run():
            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://matoloop.test",
                headers={"Authorization": "Bearer first-secret"},
            ) as client:
                async with streamable_http_client(
                    "https://matoloop.test/api/debug/mcp", http_client=client
                ) as (read, write, _session_id):
                    async with ClientSession(read, write) as mcp_session:
                        await mcp_session.initialize()
                        result = await mcp_session.call_tool("list_sessions", {"limit": 100})
                        filtered = await mcp_session.call_tool("get_diagnostics", {
                            "session_id": str(own.id), "tool_name": "run_tool_script", "errors_only": True, "limit": 1,
                        })
                        first_page = await mcp_session.call_tool(
                            "get_messages", {"session_id": str(own.id), "page_size": 2}
                        )
                        second_page = await mcp_session.call_tool(
                            "get_messages",
                            {
                                "session_id": str(own.id),
                                "page_size": 2,
                                "cursor": first_page.structuredContent["nextCursor"],
                            },
                        )
        titles = {item["title"] for item in result.structuredContent["items"]}
        assert "Own diagnostic session" in titles
        assert "Foreign diagnostic session" not in titles
        assert [event["payload"] for event in filtered.structuredContent["items"]] == [
            {"name": "run_tool_script", "errorCode": "tool.scriptStepFailed"},
        ]
        assert [item["id"] for item in first_page.structuredContent["items"]] == [
            str(messages[2].id),
            str(messages[3].id),
        ]
        assert first_page.structuredContent["nextCursor"] == str(messages[2].id)
        assert [item["id"] for item in second_page.structuredContent["items"]] == [
            str(messages[0].id),
            str(messages[1].id),
        ]
        assert second_page.structuredContent["nextCursor"] is None
    finally:
        async with AsyncSessionFactory() as db:
            await db.execute(delete(UserAccount).where(UserAccount.id.in_([first.id, second.id])))
            await db.commit()
