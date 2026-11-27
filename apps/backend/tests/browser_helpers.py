"""Shared browser transport fixture with real routes and a scripted agent."""

import asyncio
import base64
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Initialize the runtime before routes, matching application import order.
from mato_loop_backend.agent.runtime import AgentRuntime  # noqa: F401
from mato_loop_backend.api.errors import register_error_handlers
from mato_loop_backend.api.routes import browsers as routes
from mato_loop_backend.api.websocket_access import _BrowserLogin
from mato_loop_backend.db.session import get_db_session
from mato_loop_backend.execution import browsers as execution
from mato_loop_backend.execution.protocol import SUPPORTED_OPERATIONS, ResponseMessage
from mato_loop_backend.files.storage import SessionStorage
from mato_loop_backend.models import ChatSession, ExecutionAgent, LoginSession, UserAccount
from mato_loop_backend.security import require_user
from mato_loop_backend.settings import (
    AgentProfile,
    IntegrationInstance,
    IntegrationSettings,
    SettingsStore,
    UserIntegrationSettings,
)

ORIGIN = {"origin": "https://ui.test"}


@pytest.fixture
def env(monkeypatch, tmp_path):
    user = UserAccount(id=uuid4(), username="owner", is_active=True)
    agent = ExecutionAgent(id=uuid4(), user_id=user.id, alias="Computer", home_directory="/home/user")
    session = ChatSession(id=uuid4(), user_id=user.id, session_type="chat",
                          preferred_execution_agent_id=agent.id, control_center={"tabs": [{"app_id": "browser"}]})
    login = LoginSession(id=uuid4(), user_id=user.id, expires_at=datetime.now(UTC) + timedelta(hours=1))
    instance = IntegrationInstance(name="Browser", integration_type="browser", adapter_type="browser",
                                   execution_agent_id=agent.id, owner_id=user.id)
    objects = {(type(obj), obj.id): obj for obj in (user, agent, session, login)}
    documents = {(IntegrationSettings, None): IntegrationSettings(),
                 (UserIntegrationSettings, user.id): UserIntegrationSettings(instances=[instance])}
    profile = AgentProfile(name="Test", instructions="Test profile")
    coordinator = execution.BrowserCoordinator()
    storage = SessionStorage(tmp_path / "sessions")
    root_service = execution.RootFileService

    def root_files(session_id, **kwargs):
        storage.provision(session_id, kwargs.get("project_id"), user_id=kwargs.get("user_id"))
        return root_service(session_id, session_storage=storage, **kwargs)

    monkeypatch.setattr(execution, "RootFileService", root_files)
    case = SimpleNamespace(user=user, agent=agent, session=session, login=login, instance=instance,
                           objects=objects, documents=documents, profile=profile, coordinator=coordinator,
                           calls=[], reply=None, generation=1,
                           operations=SUPPORTED_OPERATIONS - {"integration.browser.openview"},
                           setup_id=uuid4(), file_id=str(uuid4()), file_size=0, file_offset=0,
                            manual=True, clipboard="")
    case.navigation = {
        "tabs": [{"id": str(uuid4()), "url": "about:blank", "title": "", "loading": False}],
        "activeTabId": None, "canGoBack": False, "canGoForward": False,
        "loading": False,
    }
    case.navigation["activeTabId"] = case.navigation["tabs"][0]["id"]
    case.diagnostics = Mock()
    monkeypatch.setattr(execution, "schedule_diagnostic_event", case.diagnostics)

    class Db:
        async def get(self, model, key):
            return objects.get((model, key))

        async def scalar(self, statement):
            params = statement.compile().params
            stored_session = objects.get((ChatSession, params.get("id_1")))
            if (stored_session is not None and params.get("user_id_1") == stored_session.user_id
                    and stored_session.delete_at is None):
                return stored_session
            if (params.get("id_1") == agent.id and params.get("user_id_1") == agent.user_id
                    and agent.revoked_at is None):
                return agent.id
            return None

        async def commit(self):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

    async def load(_db, model, *, user_id=None, **_kwargs):
        return documents.get((model, user_id), model()).model_copy(deep=True)

    async def request(agent_id, operation, arguments, **kwargs):
        assert agent_id == agent.id
        assert kwargs["expected_generation"] == case.generation
        case.calls.append((operation, arguments))
        result = await case.reply(operation, arguments) if case.reply else None
        if result is None:
            if operation == "integration.browser.open":
                result = {"browserId": arguments["browserId"], "protocol": "chat-browser-v1"}
            elif operation == "integration.browser.view":
                coordinator.relays[UUID(arguments["relayId"])].accepted = True
                result = {"ok": True, "protocol": "page-stream-v1"}
            elif operation == "integration.browser.control":
                assert any(arguments["browserId"] == str(item.browser_id) for item in coordinator.connections.values())
                assert type(arguments["manual"]) is bool
                case.manual = arguments["manual"]
                result = {"ok": True}
            elif operation == "integration.browser.clipboard":
                if arguments["action"] == "write":
                    assert case.manual, "Clipboard write reached an agent locked against manual input"
                    case.clipboard = arguments["text"]
                else:
                    assert arguments["action"] == "copy" and "text" not in arguments
                result = {"text": case.clipboard}
            elif operation == "integration.browser.resize":
                result = {"width": arguments["width"], "height": arguments["height"]}
            elif operation == "integration.browser.navigation":
                result = case.navigation
            elif operation == "integration.browser.status":
                result = {"state": "missing"}
            elif operation == "integration.browser.setup":
                result = {"state": "installing", "setupId": str(case.setup_id)}
            elif operation == "integration.browser.setupread":
                await asyncio.Event().wait()
            elif operation == "integration.browser.execute":
                result = ({"tabId": arguments["tabId"], "url": "https://page.test/",
                           "data": {"snapshotSha256": "a" * 64}}
                          if arguments["action"] == "inspect" else {})
            elif operation == "integration.browser.file":
                action = arguments["action"]
                if action in {"choose", "delete"}:
                    result = {"ok": True}
                elif action == "read":
                    result = {"fileId": arguments["fileId"], "offset": 1, "size": 1,
                              "complete": True, "data": "eA==", "filename": "sample.txt"}
                else:
                    if action == "begin":
                        case.file_size, case.file_offset = arguments["size"], 0
                    elif action == "write":
                        case.file_offset += len(base64.b64decode(arguments["data"], validate=True))
                    result = {"fileId": case.file_id, "offset": case.file_offset,
                              "size": case.file_size, "complete": action == "finish"}
            elif operation == "integration.browser.files":
                result = {"downloads": []}
            elif operation == "integration.browser.upload":
                result = {"fileId": case.file_id, "offset": 1, "size": 1, "complete": True}
            elif operation in {"integration.browser.close", "integration.browser.setupwrite", "integration.browser.setupresize"}:
                result = {"ok": True}
            else:
                pytest.fail(f"Unexpected RPC: {operation}")
        return ResponseMessage(type="response", id=uuid4(), ok=True, result=result)

    monkeypatch.setattr(execution, "AsyncSessionFactory", Db)
    monkeypatch.setattr(routes, "AsyncSessionFactory", Db)
    monkeypatch.setattr(SettingsStore, "load", load)
    monkeypatch.setattr(execution, "load_agent_profile", AsyncMock(return_value=profile))
    monkeypatch.setattr(execution.execution_agent_connections, "generation", lambda _id: case.generation)
    monkeypatch.setattr(execution.execution_agent_connections, "snapshot", lambda _id: case.operations)
    monkeypatch.setattr(execution.execution_agent_connections, "request", request)
    monkeypatch.setattr(routes, "browser_coordinator", coordinator)
    monkeypatch.setattr(routes, "_setup_viewers", {})
    monkeypatch.setattr(routes, "_setup_starts", {})
    monkeypatch.setattr(routes, "get_runtime_config", lambda: SimpleNamespace(cors_origin_list=[ORIGIN["origin"]]))
    monkeypatch.setattr(routes, "_browser_login", AsyncMock(return_value=_BrowserLogin(user, login.id, login.expires_at)))

    @asynccontextmanager
    async def lifespan(_app):
        yield
        await coordinator.shutdown()
        starts = list(routes._setup_starts.values())
        for task in starts:
            task.cancel()
        await asyncio.gather(*starts, return_exceptions=True)
        assert not routes._setup_starts

    app = FastAPI(lifespan=lifespan)
    register_error_handlers(app)
    app.include_router(routes.router)
    app.dependency_overrides[require_user] = lambda: user
    app.dependency_overrides[get_db_session] = Db
    case.app = app
    case.url = f"/browsers/{instance.id}?sessionId={session.id}"
    case.setup_url = f"/browsers/{instance.id}/setup"
    with TestClient(app) as client:
        case.client = client
        yield case


def file_url(env, state, suffix):
    return (f"/browsers/{env.instance.id}/{suffix}?sessionId={env.session.id}"
            f"&browserId={state['browserId']}&viewId={state['viewId']}")


def view_url(env, state):
    return file_url(env, state, "view")


@contextmanager
def open_view(env, state):
    with env.client.websocket_connect(view_url(env, state), headers=ORIGIN) as socket:
        try:
            async def wait_until_ready():
                async with asyncio.timeout(3):
                    while not any(relay.accepted and not relay.connection.action_lock.locked()
                                  for relay in env.coordinator.relays.values()):
                        await asyncio.sleep(0)

            env.client.portal.call(wait_until_ready)
            yield socket
        finally:
            # Complete the protocol close before TestClient cancels the ASGI task.
            socket.close()
            while socket.receive()["type"] != "websocket.close":
                pass


def other_session(env, title="Session B"):
    session = ChatSession(id=uuid4(), user_id=env.user.id, session_type="chat", title=title,
                          preferred_execution_agent_id=env.agent.id, control_center={"tabs": [{"app_id": "browser"}]})
    env.objects[ChatSession, session.id] = session
    return session
