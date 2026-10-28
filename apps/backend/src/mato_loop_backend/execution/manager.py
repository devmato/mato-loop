from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

from fastapi import WebSocket
from pydantic import ValidationError

from mato_loop_backend.execution.protocol import (
    MAX_EVENT_BYTES,
    MAX_MESSAGE_BYTES,
    MAX_OUTPUT_BYTES,
    MAX_REQUEST_BYTES,
    SUPPORTED_OPERATIONS,
    CancelMessage,
    EventMessage,
    HelloMessage,
    RequestMessage,
    ResponseMessage,
    SystemInfo,
)

EventHandler = Callable[[EventMessage], Awaitable[None] | None]
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0
CANCEL_SEND_TIMEOUT_SECONDS = 1.0
MAX_IGNORED_REQUESTS = 1_024
_MUTATING_FILESYSTEM_OPERATIONS = {
    "filesystem.copy",
    "filesystem.delete",
    "filesystem.edit",
    "filesystem.export",
    "filesystem.import",
    "filesystem.mkdir",
    "filesystem.move",
    "filesystem.patch",
    "filesystem.rename",
    "filesystem.upsert",
    "filesystem.write",
    "git.worktree",
    "git.worktree.remove",
}


class ExecutionAgentError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def disconnect_error_code(operation: str, dispatched: bool) -> str:
    if not dispatched:
        return "executionAgent.unavailable"
    if operation in {
        "integration.browser.execute", "integration.browser.setup",
        "integration.browser.setupwrite", "integration.browser.open",
        "integration.browser.close",
        "integration.browser.file", "integration.browser.navigation",
        "integration.browser.files", "integration.browser.upload",
    }:
        return "browser.outcomeUnknown"
    if operation in _MUTATING_FILESYSTEM_OPERATIONS:
        return "executionAgent.outcomeUnknown"
    if operation in {"maintenance.start", "maintenance.update", "maintenance.update.interrupt"}:
        return "executionAgent.outcomeUnknown"
    if operation.startswith("shell.") or operation == "terminal.run":
        return "process.outcomeUnknown"
    return "executionAgent.unavailable"


@dataclass(slots=True)
class _PendingRequest:
    operation: str
    future: asyncio.Future[ResponseMessage]
    event_handler: EventHandler | None = None
    dispatched: bool = False
    output_bytes: int = 0


@dataclass(slots=True)
class _Connection:
    websocket: WebSocket
    generation: int
    operations: frozenset[str]
    system_info: SystemInfo | None = None
    pending: dict[UUID, _PendingRequest] = field(default_factory=dict)
    ignored: dict[UUID, None] = field(default_factory=dict)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class ExecutionAgentConnectionManager:
    def __init__(
        self, *, default_timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS
    ) -> None:
        if default_timeout <= 0:
            raise ValueError("default timeout must be positive")
        self.default_timeout = default_timeout
        self._connections: dict[UUID, _Connection] = {}
        self._generation = 0
        self._lock = asyncio.Lock()

    def is_connected(self, agent_id: UUID) -> bool:
        return agent_id in self._connections

    def snapshot(self, agent_id: UUID) -> frozenset[str] | None:
        connection = self._connections.get(agent_id)
        return connection.operations if connection else None

    def generation(self, agent_id: UUID) -> int | None:
        connection = self._connections.get(agent_id)
        return connection.generation if connection else None

    def system_info(self, agent_id: UUID) -> SystemInfo | None:
        connection = self._connections.get(agent_id)
        return connection.system_info if connection else None

    def transfer_url(self, agent_id: UUID, ticket_id: UUID) -> str:
        connection = self._connections.get(agent_id)
        if connection is None:
            raise ExecutionAgentError("executionAgent.unavailable")
        websocket_url = urlsplit(str(connection.websocket.url))
        suffix = "/execution-agents/connect"
        if not websocket_url.path.endswith(suffix):
            raise ExecutionAgentError("executionAgent.protocolError")
        path = (
            websocket_url.path[: -len(suffix)]
            + f"/workspace/transfers/{ticket_id}"
        )
        return urlunsplit(
            (
                "https" if websocket_url.scheme == "wss" else "http",
                websocket_url.netloc,
                path,
                "",
                "",
            )
        )

    async def register(self, hello: HelloMessage, websocket: WebSocket) -> None:
        async with self._lock:
            self._generation += 1
            connection = _Connection(
                websocket=websocket,
                generation=self._generation,
                operations=frozenset(hello.operations) & SUPPORTED_OPERATIONS,
                system_info=hello.system_info,
            )
            old = self._connections.get(hello.agent_id)
            self._connections[hello.agent_id] = connection
        if old is not None:
            self._fail_connection(old, "executionAgent.replaced")
            try:
                await old.websocket.close(code=4001, reason="Replaced by a newer connection")
            except Exception:
                pass

    async def unregister(
        self,
        agent_id: UUID,
        websocket: WebSocket,
        *,
        error_code: str | None = None,
    ) -> None:
        async with self._lock:
            connection = self._connections.get(agent_id)
            if connection is None or connection.websocket is not websocket:
                return
            del self._connections[agent_id]
        self._fail_connection(connection, error_code)

    async def close(self, agent_id: UUID) -> None:
        async with self._lock:
            connection = self._connections.pop(agent_id, None)
        if connection is None:
            return
        self._fail_connection(connection, "executionAgent.revoked")
        try:
            await connection.websocket.close(code=4003, reason="Agent access revoked")
        except Exception:
            pass

    async def request(
        self,
        agent_id: UUID,
        operation: str,
        arguments: dict[str, Any],
        *,
        timeout: float | None = None,
        on_event: EventHandler | None = None,
        expected_generation: int | None = None,
    ) -> ResponseMessage:
        request = RequestMessage(id=uuid4(), operation=operation, arguments=arguments)
        payload = request.model_dump_json(by_alias=True)
        if len(payload.encode("utf-8")) > MAX_REQUEST_BYTES:
            raise ExecutionAgentError("executionAgent.requestTooLarge")
        async with self._lock:
            connection = self._connections.get(agent_id)
            if connection is None:
                raise ExecutionAgentError("executionAgent.unavailable")
            if expected_generation is not None and connection.generation != expected_generation:
                raise ExecutionAgentError("executionAgent.replaced")
            if operation not in connection.operations:
                raise ExecutionAgentError("executionAgent.operationUnavailable")
            generation = connection.generation
            pending = _PendingRequest(
                operation=operation,
                future=asyncio.get_running_loop().create_future(),
                event_handler=on_event,
            )
            connection.pending[request.id] = pending

        effective_timeout = timeout if timeout is not None else self.default_timeout
        if effective_timeout <= 0:
            await self._abandon(agent_id, connection, generation, request.id)
            raise ValueError("timeout must be positive")
        try:
            async with asyncio.timeout(effective_timeout):
                async with connection.send_lock:
                    async with self._lock:
                        if (
                            self._connections.get(agent_id) is not connection
                            or connection.generation != generation
                        ):
                            raise ExecutionAgentError("executionAgent.replaced")
                    pending.dispatched = True
                    await connection.websocket.send_text(payload)
                return await asyncio.shield(pending.future)
        except TimeoutError as exc:
            await self._abandon(agent_id, connection, generation, request.id)
            raise ExecutionAgentError(
                disconnect_error_code(operation, pending.dispatched)
                if pending.dispatched
                and disconnect_error_code(operation, True).endswith("outcomeUnknown")
                else "executionAgent.timeout"
            ) from exc
        except asyncio.CancelledError:
            await self._abandon(agent_id, connection, generation, request.id)
            raise
        except ExecutionAgentError:
            raise
        except Exception as exc:
            raise ExecutionAgentError(
                disconnect_error_code(operation, pending.dispatched)
            ) from exc
        finally:
            async with self._lock:
                removed = connection.pending.pop(request.id, None)
                if removed is not None and not removed.future.done():
                    removed.future.cancel()
            if pending.future.done() and not pending.future.cancelled():
                pending.future.exception()

    async def cancel(self, agent_id: UUID, request_id: UUID) -> bool:
        async with self._lock:
            connection = self._connections.get(agent_id)
            if connection is None:
                return False
            pending = connection.pending.get(request_id)
            if pending is None or not pending.dispatched:
                return False
            generation = connection.generation
        return await self._send_cancel(
            agent_id, connection, generation, request_id
        )

    async def _send_cancel(
        self,
        agent_id: UUID,
        connection: _Connection,
        generation: int,
        request_id: UUID,
    ) -> bool:
        payload = CancelMessage(id=request_id).model_dump_json(by_alias=True)
        try:
            async with connection.send_lock:
                async with self._lock:
                    if (
                        self._connections.get(agent_id) is not connection
                        or connection.generation != generation
                    ):
                        return False
                await connection.websocket.send_text(payload)
        except Exception:
            return False
        return True

    async def _abandon(
        self,
        agent_id: UUID,
        connection: _Connection,
        generation: int,
        request_id: UUID,
    ) -> None:
        async with self._lock:
            pending = connection.pending.pop(request_id, None)
            self._remember_ignored(connection, request_id)
            should_cancel = (
                pending is not None
                and pending.dispatched
                and not pending.future.done()
                and self._connections.get(agent_id) is connection
                and connection.generation == generation
            )
            if pending is not None and not pending.future.done():
                pending.future.cancel()
        if should_cancel:
            with suppress(Exception):
                await asyncio.wait_for(
                    self._send_cancel(
                        agent_id, connection, generation, request_id
                    ),
                    CANCEL_SEND_TIMEOUT_SECONDS,
                )

    async def receive(self, agent_id: UUID, websocket: WebSocket, raw: str) -> None:
        if len(raw.encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise ExecutionAgentError("executionAgent.responseTooLarge")
        try:
            message_type = json.loads(raw).get("type")
            if message_type == "event":
                message: EventMessage | ResponseMessage = EventMessage.model_validate_json(raw)
            elif message_type == "response":
                message = ResponseMessage.model_validate_json(raw)
            else:
                raise ValueError("invalid inbound message type")
        except (AttributeError, TypeError, ValueError, ValidationError) as exc:
            raise ExecutionAgentError("executionAgent.protocolError") from exc

        async with self._lock:
            connection = self._connections.get(agent_id)
            if connection is None or connection.websocket is not websocket:
                raise ExecutionAgentError("executionAgent.replaced")
            pending = connection.pending.get(message.id)
            if pending is None:
                if message.id in connection.ignored:
                    return
                raise ExecutionAgentError("executionAgent.protocolError")
            if pending.future.done():
                raise ExecutionAgentError("executionAgent.protocolError")
            if isinstance(message, ResponseMessage):
                pending.future.set_result(message)
                return

            event_bytes = len(message.data.encode("utf-8"))
            # Empty events still consume budget so they cannot bypass the output limit.
            pending.output_bytes += max(1, event_bytes)
            if (
                event_bytes > MAX_EVENT_BYTES
                or pending.output_bytes > MAX_OUTPUT_BYTES
            ):
                raise ExecutionAgentError("process.outputTooLarge")
            event_handler = pending.event_handler
        if event_handler is not None:
            result = event_handler(message)
            if inspect.isawaitable(result):
                await result

    def request_ids(self, agent_id: UUID) -> tuple[UUID, ...]:
        connection = self._connections.get(agent_id)
        return tuple(connection.pending) if connection else ()

    @staticmethod
    def _remember_ignored(connection: _Connection, request_id: UUID) -> None:
        connection.ignored[request_id] = None
        if len(connection.ignored) > MAX_IGNORED_REQUESTS:
            connection.ignored.pop(next(iter(connection.ignored)))

    @staticmethod
    def _fail_connection(connection: _Connection, code: str | None) -> None:
        for pending in connection.pending.values():
            if pending.future.done():
                continue
            disconnect_code = disconnect_error_code(
                pending.operation, pending.dispatched
            )
            pending.future.set_exception(
                ExecutionAgentError(
                    disconnect_code
                    if disconnect_code.endswith("outcomeUnknown")
                    else code or disconnect_code
                )
            )


execution_agent_connections = ExecutionAgentConnectionManager()
