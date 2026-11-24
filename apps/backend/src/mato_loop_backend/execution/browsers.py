from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import secrets
from collections.abc import Awaitable, Callable
from contextlib import ExitStack, asynccontextmanager, contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from tempfile import SpooledTemporaryFile
from time import monotonic
from typing import Any, BinaryIO
from uuid import UUID, uuid4

from anyio import CancelScope
from fastapi import WebSocket
from pydantic import ValidationError

from mato_loop_backend.db.session import AsyncSessionFactory
from mato_loop_backend.diagnostics.service import schedule_diagnostic_event
from mato_loop_backend.error_contract import describe_error
from mato_loop_backend.execution.browser_contracts import (
    BROWSER_TRANSFER_TIMEOUT,
    MAX_BROWSER_FILE_BYTES,
    BrowserActionArguments,
    BrowserActionResult,
    BrowserControlResult,
    BrowserExecuteArguments,
    BrowserFileArguments,
    BrowserFileListResult,
    BrowserFileResult,
    BrowserInspectionData,
    BrowserNavigationArguments,
    BrowserNavigationResult,
    BrowserOpenResult,
    BrowserOrigin,
    BrowserResizeResult,
    BrowserRestoreResult,
)
from mato_loop_backend.execution.client import (
    ExecutionClient,
    ExecutionClientError,
)
from mato_loop_backend.execution.manager import execution_agent_connections
from mato_loop_backend.files.service import RootFileError, RootFileService
from mato_loop_backend.integrations.browser.adapter import browser_client, load_browser_integration
from mato_loop_backend.models import (
    AgentRun,
    ChatSession,
    ExecutionAgent,
    LoginSession,
    UserAccount,
)
from mato_loop_backend.permissions.policy import (
    PermissionDecision,
    integration_operation_permission,
)
from mato_loop_backend.providers.base import ToolCall
from mato_loop_backend.sessions.state import is_session_deleted
from mato_loop_backend.settings.effective import (
    AgentProfileUnavailable,
    integration_profile_available,
    load_agent_profile,
)

ACCESS_INTERVAL = 2
MAX_BROWSERS = 32
logger = logging.getLogger(__name__)


async def _file_request(connection, **arguments):
    wire = BrowserFileArguments(browser_id=connection.browser_id, **arguments)
    result = await connection.request(
        "integration.browser.file", wire.model_dump(mode="json"),
        BrowserControlResult if arguments["action"] in {"delete", "choose"} else BrowserFileResult,
        **({"timeout": BROWSER_TRANSFER_TIMEOUT} if arguments["action"] == "choose" else {}),
    )
    if isinstance(result, BrowserFileResult) and arguments.get("file_id", result.file_id) != result.file_id:
        raise ExecutionClientError("executionAgent.protocolError")
    if isinstance(result, BrowserControlResult) and not result.ok:
        raise ExecutionClientError("browser.actionFailed")
    return result


async def _stage_browser_file(
    connection, file: BinaryIO, filename: str, size: int,
    check_access: Callable[[], Awaitable[None]],
) -> str:
    """Stage a bounded upload while the caller holds the browser action lock."""
    if not 0 <= size <= MAX_BROWSER_FILE_BYTES:
        raise ExecutionClientError("browser.fileTooLarge")
    file_id = None
    completed = False
    try:
        await check_access()
        beginning = asyncio.create_task(_file_request(connection, action="begin", filename=filename, size=size))
        try:
            result = await asyncio.shield(beginning)
        except asyncio.CancelledError:
            with CancelScope(shield=True), suppress(Exception):
                file_id = (await beginning).file_id
            raise
        file_id = result.file_id
        if result.offset != 0 or result.size != size or result.complete:
            raise ExecutionClientError("executionAgent.protocolError")
        offset = 0
        while chunk := await asyncio.to_thread(file.read, 65536):
            await check_access()
            result = await _file_request(
                connection, action="write", file_id=file_id, offset=offset,
                data=base64.b64encode(chunk).decode("ascii"),
            )
            offset += len(chunk)
            if result.offset != offset or result.size != size or result.complete:
                raise ExecutionClientError("executionAgent.protocolError")
        await check_access()
        result = await _file_request(connection, action="finish", file_id=file_id)
        if not result.complete or result.size != size or result.offset != size:
            raise ExecutionClientError("executionAgent.protocolError")
        await check_access()
        completed = True
        return file_id
    finally:
        if file_id is not None and not completed:
            with CancelScope(shield=True), suppress(Exception):
                await _file_request(connection, action="delete", file_id=file_id)


@contextmanager
def _browser_timing(timings: dict, phase: str):
    started = monotonic()
    try:
        yield
    finally:
        timings[phase] = (monotonic() - started) * 1000


@contextmanager
def _browser_latency(operation: str, session_id: UUID):
    timings = {}
    outcome = "error"
    try:
        with _browser_timing(timings, "total_ms"):
            yield timings
        outcome = "ok"
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    finally:
        logger.info(
            "Browser coordinator latency operation=%s session_id=%s outcome=%s timings=%s",
            operation, session_id, outcome, timings,
        )


async def validate_browser_access(
    owner_id: UUID, integration_id: UUID, *, login_id: UUID | None = None,
    session_id: UUID | None = None, require_enabled: bool = True, run_id: UUID | None = None,
):
    """Resolve live authorization, never trust a client-supplied agent/profile handle."""
    async with asyncio.timeout(5), AsyncSessionFactory() as db:
        user = await db.get(UserAccount, owner_id)
        if user is None or not user.is_active:
            raise ExecutionClientError("auth.sessionExpired")
        if login_id is not None:
            login = await db.get(LoginSession, login_id)
            if login is None or login.user_id != owner_id or login.expires_at <= datetime.now(UTC):
                raise ExecutionClientError("auth.sessionExpired")
        instance = await load_browser_integration(
            db, owner_id, integration_id, require_enabled=require_enabled,
        )
        model_available = False
        if session_id is not None:
            session = await db.get(ChatSession, session_id)
            if (session is None or session.user_id != owner_id or session.session_type != "chat"
                    or session.parent_session_id is not None or await is_session_deleted(db, session)):
                raise ExecutionClientError("session.notFound")
            if run_id is not None:
                from mato_loop_backend.agent.session_settings import apply_run_session_settings

                run = await db.get(AgentRun, run_id)
                if run is None or run.session_id != session.id or run.parent_run_id is not None:
                    raise ExecutionClientError("browser.bindingChanged")
                if run.status in {"queued", "running", "waiting_approval", "waiting_input"}:
                    apply_run_session_settings(session, run)
            if session.preferred_execution_agent_id != instance.execution_agent_id:
                raise ExecutionClientError("browser.bindingChanged")
            try:
                profile = await load_agent_profile(db, owner_id, session.agent_profile_id)
                model_available = integration_profile_available(instance, profile)
            except AgentProfileUnavailable:
                pass
        return instance, model_available


@dataclass(slots=True)
class BrowserConnection:
    owner_id: UUID
    integration_id: UUID
    agent_id: UUID
    session_id: UUID
    client: ExecutionClient
    generation: int
    run_id: UUID | None = None
    browser_id: UUID = field(default_factory=uuid4)
    view_id: UUID = field(default_factory=uuid4)
    sockets: dict[UUID, tuple[WebSocket, UUID]] = field(default_factory=dict)
    view_sockets: dict[UUID, WebSocket] = field(default_factory=dict)
    busy: bool = False
    model_available: bool = False
    closed: bool = False
    ready: bool = False
    manual_enabled: bool = True
    navigation: BrowserNavigationResult | None = None
    restored_tabs: int = 0
    restore_incomplete: bool = False
    replaced_browser_id: UUID | None = None
    restore_notice_pending: bool = False
    restore_pending: bool = False
    downloads: list[dict[str, Any]] = field(default_factory=list)
    origins: dict[str, tuple[BrowserOrigin, int]] = field(default_factory=dict)
    control_epoch: int = 0
    task: asyncio.Task | None = None
    action_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def diagnostic(self, event: str, reason: str, *, error_code: str | None = None) -> None:
        schedule_diagnostic_event(
            self.owner_id, f"browser.{event}", component="browser",
            session_id=self.session_id, correlation_id=self.browser_id,
            payload={
                "browserId": str(self.browser_id), "viewId": str(self.view_id),
                "agentId": str(self.agent_id), "integrationId": str(self.integration_id),
                "reason": reason,
                "errorCode": describe_error(error_code).code if error_code is not None else None,
            },
        )

    async def send(self, payload: dict, *, view_id: UUID | None = None) -> None:
        async def send_view(key, socket):
            try:
                async with asyncio.timeout(3):
                    await socket.send_json({**payload, "viewId": str(key)})
            except Exception:
                with suppress(Exception):
                    await socket.close()

        async with self.send_lock:
            await asyncio.gather(*(
                send_view(key, socket) for key, (socket, _) in list(self.sockets.items())
                if not self.closed and (view_id is None or key == view_id)
            ))

    async def state(self, *, view_id: UUID | None = None, report_restore: bool = False) -> None:
        restored_tabs = self.restored_tabs if report_restore else 0
        await self.send({
            "type": "state", "browserId": str(self.browser_id), "viewId": str(self.view_id),
            "busy": self.busy or not self.manual_enabled,
            "downloads": self.downloads,
            **({"restoredTabs": restored_tabs} if restored_tabs else {}),
            **({"restoreIncomplete": True} if report_restore and self.restore_incomplete else {}),
        }, view_id=view_id)

    def origin(self, kind: str, *, run_id: UUID | None = None) -> dict[str, Any]:
        origin = BrowserOrigin(
            id=uuid4(), kind=kind, owner_id=self.owner_id, session_id=self.session_id,
            integration_id=self.integration_id, run_id=run_id,
            view_id=self.view_id if kind == "human" else None,
        )
        self.control_epoch += 1
        token = str(origin.id)
        self.origins[token] = (origin, self.control_epoch)
        return {"origin": token, "controlEpoch": self.control_epoch}

    async def check_access(self, *, login_id: UUID | None = None) -> None:
        if self.closed or execution_agent_connections.generation(self.agent_id) != self.generation:
            raise ExecutionClientError("browser.notConnected")
        instance, self.model_available = await validate_browser_access(
            self.owner_id, self.integration_id, session_id=self.session_id,
            run_id=self.run_id, login_id=login_id,
        )
        if self.closed or instance.execution_agent_id != self.agent_id:
            raise ExecutionClientError("browser.bindingChanged")

    async def check_manual(self, *, view_id: UUID | None = None) -> None:
        if view_id is None:
            await self.check_access()
        else:
            await self.check_view(view_id)
        if not self.ready or not self.sockets:
            raise ExecutionClientError("browser.notConnected")
        if self.busy or not self.manual_enabled:
            raise ExecutionClientError("browser.busy")

    async def check_view(self, view_id: UUID) -> None:
        binding = self.sockets.get(view_id)
        if binding is None:
            raise ExecutionClientError("browser.notConnected")
        await self.check_access(login_id=binding[1])
        if self.closed or self.sockets.get(view_id) is not binding:
            raise ExecutionClientError("browser.notConnected")

    async def request(self, *args, **kwargs):
        """Keep dispatched human operations alive until the shared action lock can pass."""
        pending = asyncio.create_task(self.client.request(*args, **kwargs))
        try:
            return await asyncio.shield(pending)
        except asyncio.CancelledError:
            with CancelScope(shield=True):
                await asyncio.gather(pending, return_exceptions=True)
            raise


@dataclass(slots=True)
class BrowserRelay:
    connection: BrowserConnection
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)
    relay_id: UUID = field(default_factory=uuid4)
    incoming: asyncio.Queue[bytes] = field(default_factory=lambda: asyncio.Queue(maxsize=4))
    outgoing: asyncio.Queue[bytes] = field(default_factory=lambda: asyncio.Queue(maxsize=4))
    ended: asyncio.Event = field(default_factory=asyncio.Event)
    accepted: bool = False


@dataclass(slots=True)
class BrowserViewAccess:
    connection: BrowserConnection
    view_id: UUID
    login_id: UUID

    def __getattr__(self, name):
        return getattr(self.connection, name)

    async def check_manual(self) -> None:
        binding = self.connection.sockets.get(self.view_id)
        if binding is None or binding[1] != self.login_id:
            raise ExecutionClientError("browser.notConnected")
        await self.connection.check_manual(view_id=self.view_id)


class BrowserCoordinator:
    def __init__(self) -> None:
        self.connections: dict[tuple[UUID, UUID], BrowserConnection] = {}
        self.relays: dict[UUID, BrowserRelay] = {}
        self.lock = asyncio.Lock()

    def current_for(self, session_id: UUID, agent_id: UUID) -> BrowserConnection | None:
        connection = self.connections.get((session_id, agent_id))
        return connection if (
            connection is not None and connection.session_id == session_id
            and not connection.closed and connection.ready
            and connection.generation == execution_agent_connections.generation(agent_id)
        ) else None

    async def open(self, owner_id, integration_id, session_id, login_id=None, websocket=None,
                   *, run_id: UUID | None = None,
                   expected_browser_id: UUID | None = None,
                   color_scheme: str = "light") -> BrowserConnection:
        instance, model_available = await validate_browser_access(
            owner_id, integration_id, login_id=login_id, session_id=session_id,
            run_id=run_id,
        )
        agent_id = instance.execution_agent_id
        async with self.lock:
            previous = self.connections.get((session_id, agent_id))
            client = browser_client(agent_id)
            restore = client.supports("integration.browser.openview")
            if expected_browser_id is not None and (
                (previous is None and not restore)
                or (previous is not None
                    and expected_browser_id not in {previous.browser_id, previous.replaced_browser_id}
                    and not (previous.restore_pending or previous.restored_tabs or previous.restore_incomplete))
            ):
                raise ExecutionClientError("browser.notConnected")
            if previous is not None:
                if previous.integration_id != integration_id or previous.owner_id != owner_id:
                    raise ExecutionClientError("browser.busy")
                if previous.closed or previous.generation != execution_agent_connections.generation(agent_id):
                    raise ExecutionClientError("browser.unavailable")
                connection = previous
            else:
                if len(self.connections) >= MAX_BROWSERS:
                    raise ExecutionClientError("browser.busy")
                connection = BrowserConnection(
                    owner_id, integration_id, agent_id, session_id, client,
                    execution_agent_connections.generation(agent_id), run_id=run_id,
                    model_available=model_available,
                    replaced_browser_id=expected_browser_id,
                )
                self.connections[(session_id, agent_id)] = connection
                connection.task = asyncio.create_task(self._start(
                    connection, color_scheme, restore=restore,
                    recover_only=expected_browser_id is not None,
                ))
            opening = connection.task if not connection.ready else None
        if opening is not None:
            await asyncio.shield(opening)
        if run_id is not None:
            connection.run_id = run_id
        await connection.check_access()
        if websocket is not None:
            if connection.restore_pending:
                async with connection.action_lock:
                    await connection.check_access(login_id=login_id)
                    await self._restore_tabs(connection)
                    await connection.check_access(login_id=login_id)
            connection.sockets[uuid4()] = (websocket, login_id)
        return connection

    async def _restore_tabs(self, connection: BrowserConnection) -> None:
        # Called under the action lock, after validating the human view or authorizing the tool.
        if not connection.restore_pending:
            return
        result = await connection.request(
            "integration.browser.control", {
                "browserId": str(connection.browser_id), "manual": True, "restoreTabs": True,
            }, BrowserRestoreResult, timeout=30,
        )
        if not result.ok:
            raise ExecutionClientError("browser.actionFailed")
        connection.restore_pending = False
        self._record_restore(connection, result)

    @staticmethod
    def _record_restore(connection: BrowserConnection, result: BrowserOpenResult | BrowserRestoreResult) -> None:
        connection.restored_tabs = result.restored_tabs
        connection.restore_incomplete = result.restore_incomplete
        connection.restore_notice_pending = bool(result.restored_tabs or result.restore_incomplete)

    async def _start(self, connection: BrowserConnection,
                      color_scheme: str = "light", *, restore: bool = False,
                      recover_only: bool = False) -> BrowserConnection:
        try:
            result = await connection.request(
                "integration.browser.openview" if restore else "integration.browser.open",
                {
                    "ownerId": str(connection.owner_id),
                    "browserId": str(connection.browser_id),
                    "sessionId": str(connection.session_id),
                    "colorScheme": color_scheme,
                    **({"recoverOnly": recover_only, "deferRestore": True} if restore else {}),
                },
                BrowserOpenResult, timeout=60,
            )
            if result.browser_id != connection.browser_id:
                raise ExecutionClientError("executionAgent.protocolError")
            connection.restore_pending = result.restore_pending
            self._record_restore(connection, result)
            await connection.check_access()
            result = await connection.request(
                "integration.browser.control",
                {"browserId": str(connection.browser_id), "manual": True,
                 **connection.origin("human")},
                BrowserControlResult,
            )
            if not result.ok:
                raise ExecutionClientError("browser.actionFailed")
            await connection.check_access()
            connection.ready = True
            connection.diagnostic("opened", "start")
            connection.task = asyncio.create_task(self.watch(connection))
            return connection
        except BaseException as exc:
            code = (
                exc.code if isinstance(exc, ExecutionClientError) else "browser.unavailable"
            )
            if isinstance(exc, Exception):
                with suppress(Exception):
                    await connection.send({"type": "error", "error": describe_error(code).payload()})
            await self.close(
                connection, reason="startupFailed",
                error_code=code,
            )
            raise

    async def watch(self, connection: BrowserConnection) -> None:
        try:
            while not connection.closed:
                await asyncio.sleep(ACCESS_INTERVAL)
                await connection.check_access()
                if (not connection.manual_enabled
                        and not connection.action_lock.locked()):
                    raise ExecutionClientError("browser.actionFailed")
                if connection.sockets and connection.navigation is not None and not connection.action_lock.locked():
                    try:
                        await self.navigate(connection, BrowserNavigationArguments(action="state"))
                    except ExecutionClientError:
                        # Command failures have their own reply; transient observations retain state.
                        pass
                if not connection.action_lock.locked():
                    async with connection.action_lock:
                        await connection.check_access()
                        try:
                            changed = await self.poll_downloads(connection)
                        except ExecutionClientError as exc:
                            if exc.code == "browser.notConnected":
                                raise
                            # A failed background observation need not end a live browser.
                            continue
                    if changed:
                        await connection.state()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            code = exc.code if isinstance(exc, ExecutionClientError) else "browser.unavailable"
            with suppress(Exception):
                await connection.send({"type": "error", "error": describe_error(code).payload(), "operation": "runtime"})
            await self.close(
                connection,
                reason=("agentDisconnected" if execution_agent_connections.generation(connection.agent_id)
                        != connection.generation else "watchFailed"),
                error_code=code,
            )

    async def release(self, connection: BrowserConnection, websocket: WebSocket) -> None:
        for view_id, (socket, _) in list(connection.sockets.items()):
            if socket is websocket:
                connection.sockets.pop(view_id, None)
                viewer = connection.view_sockets.get(view_id)
                if viewer is not None:
                    with suppress(Exception):
                        await viewer.close()

    async def close(self, connection: BrowserConnection, *, reason: str = "manualClose",
                    error_code: str | None = None) -> None:
        if connection.closed:
            return
        connection.closed = True
        connection.diagnostic("closed", reason, error_code=error_code)
        if connection.task is not None and connection.task is not asyncio.current_task():
            connection.task.cancel()
        for relay_id, relay in list(self.relays.items()):
            if relay.connection is connection:
                relay.ended.set()
                self.relays.pop(relay_id, None)
        try:
            async with connection.action_lock:
                with suppress(Exception):
                    await connection.request(
                        "integration.browser.close", {
                            "browserId": str(connection.browser_id),
                            **({"preserveTabs": True} if reason != "manualClose"
                               and connection.client.supports("integration.browser.openview") else {}),
                        },
                        BrowserControlResult, timeout=15,
                    )
        finally:
            for socket in [*connection.view_sockets.values(), *(item[0] for item in connection.sockets.values())]:
                if socket is not None:
                    with suppress(Exception):
                        async with asyncio.timeout(1):
                            await socket.close()
            if self.connections.get((connection.session_id, connection.agent_id)) is connection:
                self.connections.pop((connection.session_id, connection.agent_id))

    async def _snapshot(self, connection: BrowserConnection, tab_id: str, *, settle: bool = False) -> tuple[str, BrowserActionResult]:
        result = await connection.client.request(
            "integration.browser.execute",
            {"browserId": str(connection.browser_id), "action": "inspect", "tabId": tab_id, "settle": settle, "guard": True},
            BrowserActionResult, timeout=30,
        )
        if result.tab_id != tab_id or result.data is None or "snapshotSha256" not in result.data:
            raise ExecutionClientError("browser.bindingChanged")
        if result.batch is not None or result.script is not None:
            raise ExecutionClientError("executionAgent.protocolError")
        try:
            inspection = BrowserInspectionData.model_validate(result.data)
        except ValidationError as exc:
            raise ExecutionClientError("executionAgent.protocolError") from exc
        digest = hashlib.sha256(f"{result.url}\n{inspection.snapshot_sha256}".encode()).hexdigest()
        return digest, result.model_copy(update={
            "data": {**result.data, "snapshotSha256": digest},
        })

    @asynccontextmanager
    async def _model_action(self, connection: BrowserConnection, run_id: UUID | None = None):
        run_id = run_id or connection.run_id
        failed = False
        connection.busy = True
        try:
            await connection.state()
            connection.manual_enabled = False
            result = await connection.request(
                "integration.browser.control",
                {"browserId": str(connection.browser_id), "manual": False,
                 **(connection.origin("model", run_id=run_id) if run_id is not None else {})}, BrowserControlResult,
            )
            if not result.ok:
                raise ExecutionClientError("browser.actionFailed")
            yield
        except BaseException:
            failed = True
            raise
        finally:
            try:
                with CancelScope(shield=True):
                    result = await connection.request(
                        "integration.browser.control",
                        # Delayed model effects remain subject to file permissions after unlocking.
                        {"browserId": str(connection.browser_id), "manual": True}, BrowserControlResult,
                    )
                    if not result.ok:
                        raise ExecutionClientError("browser.actionFailed")
                    connection.manual_enabled = True
            except Exception:
                if not failed:
                    raise
            finally:
                connection.busy = False
                with suppress(Exception):
                    await connection.state()

    async def resize(self, connection: BrowserConnection, width: int, height: int,
                     *, view_id: UUID | None = None) -> BrowserResizeResult:
        dimensions = BrowserResizeResult(width=width, height=height)
        async with connection.action_lock:
            await connection.check_access()
            if view_id is not None:
                await connection.check_view(view_id)
            if self.connections.get((connection.session_id, connection.agent_id)) is not connection or not connection.sockets:
                raise ExecutionClientError("browser.notConnected")
            result = await connection.request(
                "integration.browser.resize", {"browserId": str(connection.browser_id), **dimensions.model_dump()},
                BrowserResizeResult, timeout=30,
            )
            if result != dimensions:
                raise ExecutionClientError("executionAgent.protocolError")
            return result

    async def navigate(self, connection: BrowserConnection, arguments: BrowserNavigationArguments,
                       *, completed: bool = False, view_id: UUID | None = None) -> BrowserNavigationResult:
        async with connection.action_lock:
            await connection.check_access()
            if view_id is not None:
                await connection.check_view(view_id)
            if self.connections.get((connection.session_id, connection.agent_id)) is not connection or not connection.sockets:
                raise ExecutionClientError("browser.notConnected")
            if arguments.action != "state" and (
                connection.busy or not connection.manual_enabled
                or (view_id is not None and view_id not in connection.view_sockets)
            ):
                raise ExecutionClientError("browser.busy")
            result = await connection.request(
                "integration.browser.navigation",
                {"browserId": str(connection.browser_id),
                 **arguments.model_dump(mode="json", exclude_unset=True)},
                BrowserNavigationResult, timeout=30,
            )
            if result != connection.navigation or completed:
                connection.navigation = result
                for key in list(connection.sockets):
                    await connection.send({"type": "navigation", "browserId": str(connection.browser_id),
                                           "completed": completed and key == view_id,
                                           **result.model_dump(mode="json")}, view_id=key)
            return result

    async def snapshot(self, session_id, agent_id, integration_id, tab_id) -> str:
        with _browser_latency("snapshot", session_id) as timings:
            connection = self.current_for(session_id, agent_id)
            if connection is None or connection.integration_id != integration_id:
                raise ExecutionClientError("browser.notConnected")
            timings["browser_id"] = str(connection.browser_id)
            with ExitStack() as waiting:
                waiting.enter_context(_browser_timing(timings, "lock_wait_ms"))
                async with connection.action_lock:
                    waiting.close()
                    with _browser_timing(timings, "access_ms"):
                        await connection.check_access()
                    if self.current_for(session_id, agent_id) is not connection or not connection.model_available:
                        raise ExecutionClientError("browser.bindingChanged")
                    async with self._model_action(connection):
                        with _browser_timing(timings, "reinspection_rpc_ms"):
                            digest, _ = await self._snapshot(connection, tab_id, settle=True)
                    return digest

    async def execute(self, session_id, agent_id, integration_id, arguments: dict[str, Any]) -> BrowserActionResult:
        with _browser_latency("execute", session_id) as timings:
            connection = self.current_for(session_id, agent_id)
            if connection is None or connection.integration_id != integration_id:
                raise ExecutionClientError("browser.notConnected")
            timings["browser_id"] = str(connection.browser_id)
            payload = dict(arguments)
            expected_id = payload.pop("expectedBrowserId", None)
            expected_snapshot = payload.pop("expectedSnapshot", None)
            expected_run_id = payload.pop("expectedRunId", None)
            run_id = UUID(expected_run_id) if expected_run_id is not None else None
            action = BrowserActionArguments.model_validate(payload)
            timings["action"] = action.action
            with ExitStack() as waiting:
                waiting.enter_context(_browser_timing(timings, "lock_wait_ms"))
                async with connection.action_lock:
                    waiting.close()
                    with _browser_timing(timings, "access_ms"):
                        await connection.check_access()
                    def check_binding():
                        if (self.current_for(session_id, agent_id) is not connection
                                or str(connection.browser_id) != expected_id
                                or not connection.model_available):
                            raise ExecutionClientError("browser.bindingChanged")
                    check_binding()
                    # A guarded action must still target the page that was approved, not a restored page.
                    if connection.restore_pending and expected_snapshot is not None:
                        raise ExecutionClientError("browser.bindingChanged")
                    result = None
                    try:
                        async with self._model_action(connection, run_id):
                            if connection.restore_pending:
                                await self._restore_tabs(connection)
                                await connection.check_access()
                                check_binding()
                            if expected_snapshot is not None:
                                try:
                                    BrowserInspectionData(snapshotSha256=expected_snapshot)
                                except ValidationError as exc:
                                    raise ExecutionClientError("browser.bindingChanged") from exc
                                with _browser_timing(timings, "reinspection_rpc_ms"):
                                    digest, inspection = await self._snapshot(connection, action.tab_id)
                                check_binding()
                                if expected_snapshot != digest:
                                    raise ExecutionClientError("browser.snapshotChanged")
                                if (
                                    action.action == "inspect"
                                    and action.selector is None
                                    and action.depth is None
                                    and BrowserInspectionData.model_validate(
                                        inspection.data
                                    ).snapshot_characters
                                    <= 32_768
                                ):
                                    return inspection
                            check_binding()
                            read_action = action.action in {"tabs", "inspect", "screenshot", "diagnostics"}
                            wire = BrowserExecuteArguments(
                                browser_id=connection.browser_id,
                                settle=action.action == "inspect",
                                **action.model_dump(
                                    by_alias=False, exclude={"observe"} if read_action else set()
                                ),
                            )
                            excluded = {"observe"} if read_action else set()
                            if action.action != "run_code":
                                excluded.add("code")
                            if action.action != "evaluate":
                                excluded.add("function")
                            if action.action != "fill":
                                excluded.update({"slowly", "submit"})
                            wire_payload = wire.model_dump(mode="json", exclude=excluded)
                            with _browser_timing(timings, "action_rpc_ms"):
                                if action.action == "screenshot":
                                    result = await self._screenshot(connection, wire_payload, check_binding)
                                else:
                                    result = await connection.client.request(
                                        "integration.browser.execute", wire_payload, BrowserActionResult,
                                        timeout=160 if action.action in {"run_code", "evaluate"} else BROWSER_TRANSFER_TIMEOUT if action.action == "upload" else 60,
                                    )
                            if not result.matches_action(action):
                                raise ExecutionClientError("executionAgent.protocolError")
                            if result.snapshot_file is not None:
                                raise ExecutionClientError("executionAgent.protocolError")
                            if action.action == "inspect" and result.tab_id != action.tab_id:
                                raise ExecutionClientError("browser.bindingChanged")
                            if result.data is not None and "snapshotSha256" in result.data:
                                try:
                                    observation = BrowserInspectionData.model_validate(result.data)
                                except ValidationError as exc:
                                    raise ExecutionClientError("executionAgent.protocolError") from exc
                                digest = hashlib.sha256(
                                    f"{result.url}\n{observation.snapshot_sha256}".encode()
                                ).hexdigest()
                                result = result.model_copy(update={
                                    "data": {**result.data, "snapshotSha256": digest},
                                })
                            if result.snapshot is not None:
                                result._snapshot_stream = await self._read_browser_file(
                                    connection, result.snapshot, check_binding
                                )
                    except BaseException:
                        if result is not None and result._snapshot_stream is not None:
                            result._snapshot_stream.close()
                        raise
                    if action.action == "screenshot":
                        await connection.check_access()
                        check_binding()
                    return result

    async def poll_downloads(self, connection: BrowserConnection, *, retry: bool = False) -> bool:
        listed = await connection.request(
            "integration.browser.files", {"browserId": str(connection.browser_id)}, BrowserFileListResult,
        )
        statuses = []
        previous_downloads = connection.downloads
        previous = {item["id"]: item for item in previous_downloads}
        for download in listed.downloads:
            captured = connection.origins.get(download.origin or "")
            if captured is None or captured[1] != download.control_epoch:
                continue
            origin = captured[0]
            status = download.model_dump(mode="json")
            if download.status == "ready" and previous.get(download.id, {}).get("status") in ({"saved"} if retry else {"saved", "failed"}):
                statuses.append(previous[download.id])
                continue
            if download.status == "ready":
                decision = await self._download_decision(connection, origin, download.filename)
                if decision == PermissionDecision.ASK:
                    status.update(status="permission_required", errorCode="browser.downloadPermissionRequired")
                elif decision != PermissionDecision.ALLOW:
                    status.update(status="failed", errorCode="permission.denied")
                else:
                    async def reauthorize(download=download, captured=captured, origin=origin):
                        if connection.origins.get(download.origin or "") != captured:
                            raise ExecutionClientError("browser.bindingChanged")
                        if await self._download_decision(connection, origin, download.filename) != PermissionDecision.ALLOW:
                            raise ExecutionClientError("permission.denied")

                    try:
                        status = await self.save_download(connection, download, authorize=reauthorize)
                    except (ExecutionClientError, RootFileError, OSError) as exc:
                        code = exc.code if isinstance(exc, (ExecutionClientError, RootFileError)) else "browser.actionFailed"
                        if isinstance(exc, OSError):
                            logger.warning("Browser download could not be stored", exc_info=True)
                        status = next((item for item in connection.downloads if item["id"] == download.id and item.get("status") == "saved"), {
                            **status, "status": "failed", "errorCode": code,
                        })
            statuses.append(status)
        listed_ids = {download.id for download in listed.downloads}
        statuses.extend(
            item for item in connection.downloads
            if item.get("status") == "saved" and item.get("id") not in listed_ids
        )
        statuses = statuses[:32]
        changed = statuses != previous_downloads
        connection.downloads = statuses
        return changed

    async def save_download(
        self, connection: BrowserConnection, download, *, authorize: Callable[[], Awaitable[None]],
    ) -> dict:
        from mato_loop_backend.files.access import create_output_stream
        from mato_loop_backend.settings import InstanceLimits, SettingsStore

        previous = next((item for item in connection.downloads if item["id"] == download.id and item.get("status") == "saved"), None)
        if previous is not None:
            return previous
        await connection.check_access()
        async with AsyncSessionFactory() as db:
            session = await db.get(ChatSession, connection.session_id)
            if session is None or session.user_id != connection.owner_id:
                raise ExecutionClientError("session.notFound")
            limits = await SettingsStore.load(db, InstanceLimits)
            root_files = RootFileService(
                session.id, project_id=session.project_id, user_id=connection.owner_id,
                user_quota_bytes=limits.user_quota_bytes,
            )
        if download.size > limits.max_upload_bytes:
            raise ExecutionClientError("browser.fileTooLarge")
        descriptor = BrowserFileResult(file_id=download.id, offset=download.size, size=download.size, complete=True)
        with await self._read_browser_file(
            connection, descriptor, lambda: None, delete=False, expected_sha256=download.sha256,
        ) as stream:
            await connection.check_access()
            await authorize()

            async def store():
                revision = await create_output_stream(root_files, download.filename, stream)
                status = {
                    **download.model_dump(mode="json"), "status": "saved", "root": "files",
                    "path": revision.path, "fileId": str(revision.file_id), "revision": revision.revision,
                }
                connection.downloads = [item for item in connection.downloads if item["id"] != download.id] + [status]
                await connection.check_access()
                with suppress(Exception):
                    await _file_request(connection, action="delete", file_id=download.id)
                return status

            saving = asyncio.create_task(store())
            try:
                return await asyncio.shield(saving)
            except asyncio.CancelledError:
                while not saving.done():
                    try:
                        with CancelScope(shield=True):
                            await asyncio.shield(saving)
                    except asyncio.CancelledError:
                        continue
                saving.result()
                raise

    async def _download_decision(
        self, connection: BrowserConnection, origin: BrowserOrigin, destination: str,
    ) -> PermissionDecision:
        if origin.kind == "human":
            return PermissionDecision.ALLOW
        from mato_loop_backend.agent.runtime import AgentRuntime
        from mato_loop_backend.tools.registry import _model_revision

        async with AsyncSessionFactory() as db:
            session = await db.get(ChatSession, origin.session_id)
            run = await db.get(AgentRun, origin.run_id)
            agent = await db.get(ExecutionAgent, connection.agent_id)
            try:
                instance = await load_browser_integration(
                    db, origin.owner_id, origin.integration_id,
                )
            except ExecutionClientError:
                return PermissionDecision.DENY
            if (
                session is None or run is None or agent is None
                or session.user_id != origin.owner_id or run.session_id != session.id
                or run.execution_agent_id != connection.agent_id or agent.user_id != origin.owner_id
                or origin.integration_id != connection.integration_id
                or instance.execution_agent_id != connection.agent_id
            ):
                return PermissionDecision.DENY
            operation = {
                "browserId": str(connection.browser_id),
                "originId": str(origin.id),
                "path": destination,
            }
            requests = (
                integration_operation_permission(
                    capability="browser.use",
                    integration_id=origin.integration_id,
                    binding_revision=_model_revision(instance),
                    operation_key=hashlib.sha256(
                        json.dumps(operation, sort_keys=True).encode()
                    ).hexdigest(),
                    grant_resource_key=f"browser:{connection.browser_id}",
                    title="Browser-Downloads lesen?",
                    summary="Der Agent möchte den Status eines Browser-Downloads lesen.",
                    resource_display=destination,
                    action_kind="browser_download_status",
                ),
            )
            call = ToolCall(
                f"browser-download:{origin.id}", "browser_downloads", {"path": destination},
            )
            root_files = RootFileService(
                session.id, project_id=session.project_id, run_id=run.id,
            )
            decisions = [
                await AgentRuntime._authorize(
                    db, run, session, call, request, root_files, check_only=True,
                )
                for request in requests
            ]
            if any(decision in {PermissionDecision.DENY, PermissionDecision.USER_DENIED} for decision in decisions):
                return PermissionDecision.DENY
            return PermissionDecision.ASK if PermissionDecision.ASK in decisions else PermissionDecision.ALLOW

    async def _screenshot(self, connection: BrowserConnection, payload: dict, check_binding) -> BrowserActionResult:
        result = None
        pending = asyncio.create_task(connection.client.request(
            "integration.browser.execute", payload, BrowserActionResult, timeout=60,
        ))
        try:
            try:
                result = await asyncio.shield(pending)
            except asyncio.CancelledError:
                # Drain capture to recover its staging ID before releasing the shared action lock.
                with CancelScope(shield=True), suppress(Exception):
                    result = await pending
                raise
            if result.screenshot is None:
                raise ExecutionClientError("executionAgent.protocolError")
            with await self._read_browser_file(
                connection, result.screenshot, check_binding, delete=False
            ) as image:
                result._screenshot_bytes = image.read()
            return result
        finally:
            if result is not None and result.screenshot is not None:
                with CancelScope(shield=True), suppress(Exception):
                    wire = BrowserFileArguments(
                        browser_id=connection.browser_id, action="delete", file_id=result.screenshot.file_id,
                    )
                    await connection.request(
                        "integration.browser.file", wire.model_dump(mode="json"), BrowserControlResult, timeout=10,
                    )

    async def _read_browser_file(
        self, connection: BrowserConnection, descriptor, check_binding, *, delete: bool = True,
        expected_sha256: str | None = None,
    ):
        output = SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b")
        digest = hashlib.sha256()
        try:
            async with asyncio.timeout(60 + descriptor.size / (256 * 1024)):
                offset = 0
                next_access_check = 0.0
                while offset < descriptor.size:
                    if monotonic() >= next_access_check:
                        await connection.check_access()
                        next_access_check = monotonic() + ACCESS_INTERVAL
                    if connection.closed or execution_agent_connections.generation(connection.agent_id) != connection.generation:
                        raise ExecutionClientError("browser.notConnected")
                    check_binding()
                    wire = BrowserFileArguments(
                        browser_id=connection.browser_id,
                        action="read",
                        file_id=descriptor.file_id,
                        offset=offset,
                    )
                    chunk = await connection.request(
                        "integration.browser.file",
                        wire.model_dump(mode="json"),
                        BrowserFileResult,
                    )
                    try:
                        data = base64.b64decode(chunk.data or "", validate=True)
                    except (binascii.Error, ValueError) as exc:
                        raise ExecutionClientError("executionAgent.protocolError") from exc
                    if (
                        chunk.file_id != descriptor.file_id
                        or chunk.size != descriptor.size
                        or chunk.offset > descriptor.size
                        or chunk.offset != offset + len(data)
                        or not data
                        or chunk.complete != (chunk.offset == chunk.size)
                    ):
                        raise ExecutionClientError("executionAgent.protocolError")
                    output.write(data)
                    digest.update(data)
                    offset = chunk.offset
                await connection.check_access()
                check_binding()
            if expected_sha256 is not None and digest.hexdigest() != expected_sha256:
                raise ExecutionClientError("executionAgent.protocolError")
            output.seek(0)
            return output
        except BaseException:
            output.close()
            raise
        finally:
            if delete:
                with CancelScope(shield=True), suppress(Exception):
                    wire = BrowserFileArguments(
                        browser_id=connection.browser_id,
                        action="delete",
                        file_id=descriptor.file_id,
                    )
                    await connection.request(
                        "integration.browser.file",
                        wire.model_dump(mode="json"),
                        BrowserControlResult,
                        timeout=10,
                    )

    async def manual(self, owner_id, integration_id, session_id, browser_id,
                     view_id: UUID, login_id: UUID) -> BrowserViewAccess:
        connection = next((item for item in self.connections.values() if item.browser_id == browser_id), None)
        if (connection is None or connection.owner_id != owner_id
                or connection.integration_id != integration_id or connection.session_id != session_id
                or view_id not in connection.sockets or connection.sockets[view_id][1] != login_id):
            raise ExecutionClientError("browser.notConnected")
        access = BrowserViewAccess(connection, view_id, login_id)
        await access.check_manual()
        return access

    async def viewer(self, owner_id, integration_id, session_id, browser_id,
                     view_id: UUID, login_id: UUID) -> BrowserConnection:
        connection = next((item for item in self.connections.values() if item.browser_id == browser_id), None)
        if (connection is None or connection.owner_id != owner_id
                or connection.integration_id != integration_id or connection.session_id != session_id
                or view_id not in connection.sockets or connection.sockets[view_id][1] != login_id
                or connection.closed or not connection.ready):
            raise ExecutionClientError("browser.notConnected")
        await connection.check_access()
        await connection.check_view(view_id)
        return connection

    async def shutdown(self) -> None:
        await asyncio.gather(*(self.close(item, reason="shutdown") for item in list(self.connections.values())))


browser_coordinator = BrowserCoordinator()
