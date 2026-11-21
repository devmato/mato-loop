"""Chat-owned stdio MCP processes with bounded admission and idle lifetime."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from mcp import ClientSession, StdioServerParameters

from .errors import AgentError
from .mcp_transport import FRAME_ENVELOPE_BYTES, bounded_stdio_client
from .protocol import ArtifactDescriptor

MAX_CATALOG_BYTES = 32 * 1024 * 1024
MAX_RESULT_BYTES = 16 * 1024 * 1024
# Reserve 15 seconds against the backend's 65/90 second defaults. Cleanup can
# outlive a request (e.g. while spawning); the worker keeps ownership in that case.
DISCOVER_TIMEOUT_SECONDS = 50
CALL_TIMEOUT_SECONDS = 75
CLEANUP_TIMEOUT_SECONDS = 10


@dataclass
class _Job:
    operation: str
    arguments: object
    deadline: float
    future: asyncio.Future
    dispatched: bool = False


@dataclass
class _Session:
    fingerprint: str
    arguments: object
    id: str = field(default_factory=lambda: str(uuid4()))
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    task: asyncio.Task | None = None
    stopping: bool = False
    initializing: bool = True
    active: _Job | None = None
    pending: int = 0
    expires_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class McpProcesses:
    def __init__(self, cache, filesystem, *, max_processes: int = 4):
        self.cache = cache
        self.filesystem = filesystem
        self.max_processes = max_processes
        self.sessions: dict[tuple[str, str, str], _Session] = {}
        self.lock = asyncio.Lock()

    @property
    def maintenance_busy(self):
        return any(not item.task.done() for item in self.sessions.values())

    def _prune(self):
        self.sessions = {key: item for key, item in self.sessions.items() if not item.task.done()}

    def _request_stop(self, item):
        if not item.stopping:
            item.stopping = True
            item.task.cancel()

    def _finish_queued(self, item):
        item.stopping = True
        while not item.queue.empty():
            future = item.queue.get_nowait().future
            item.pending -= 1
            if not future.done():
                future.set_exception(AgentError("mcp.unavailable", "MCP process stopped"))

    async def _stop(self, item):
        self._request_stop(item)
        cleanup = asyncio.gather(item.task, return_exceptions=True)
        cancelled = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError

    async def close_all(self):
        async with self.lock:
            items = list(self.sessions.values())
            for item in items:
                self._request_stop(item)
            cancelled = False
            for item in items:
                try:
                    await self._stop(item)
                except asyncio.CancelledError:
                    cancelled = True
            self._prune()
            if cancelled:
                raise asyncio.CancelledError

    async def dispatch(self, operation, arguments, backend_id):
        if operation == "integration.mcp.manage" and arguments.action == "list":
            async with self.lock:
                self._prune()
                return {
                    "sessions": [
                        {
                            "connectionId": key[1],
                            "sessionId": key[2],
                            "id": item.id,
                            "idleTimeoutSeconds": item.arguments.idle_timeout_seconds,
                            "expiresAt": item.expires_at.isoformat(),
                            "busy": item.pending > 0,
                        }
                        for key, item in self.sessions.items()
                        if key[0] == backend_id
                    ]
                }
        key = (backend_id, arguments.connection_id, arguments.session_id)
        if operation == "integration.mcp.manage" and arguments.action == "close":
            async with self.lock:
                self._prune()
                item = self.sessions.get(key)
                if item is None or item.id != arguments.process_id:
                    raise AgentError("integration.changed", "MCP process is no longer current")
                if item.pending:
                    raise AgentError("mcp.busy", "MCP process has active calls")
                await self._stop(item)
                self.sessions.pop(key, None)
                return {"closed": True}

        discover = operation == "integration.mcp.discover"
        loop = asyncio.get_running_loop()
        deadline = loop.time() + (DISCOVER_TIMEOUT_SECONDS if discover else CALL_TIMEOUT_SECONDS)
        item, job, started = None, None, False
        try:
            async with asyncio.timeout_at(deadline):
                self.filesystem.resolve_directory(arguments.working_directory)
                # Discovery must never borrow a chat process.
                if discover:
                    key = (backend_id, arguments.connection_id, str(uuid4()))
                material = arguments.model_dump(exclude={"name", "arguments", "output_schema"})
                fingerprint = hashlib.sha256(
                    json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                async with self.lock:
                    self._prune()
                    item = self.sessions.get(key)
                    if item and item.stopping:
                        raise AgentError("mcp.busy", "MCP process is stopping")
                    if item and item.fingerprint != fingerprint:
                        if item.pending:
                            raise AgentError("mcp.busy", "MCP process has active calls")
                        # Stop the old process without waiting past the new call's deadline.
                        self._request_stop(item)
                        await asyncio.wait({item.task})
                        self.sessions.pop(key, None)
                        item = None
                    if loop.time() >= deadline:
                        raise TimeoutError
                    started = item is None
                    if item is None:
                        if len(self.sessions) >= self.max_processes:
                            raise AgentError("mcp.processLimitReached", "MCP process limit reached")
                        item = _Session(fingerprint, arguments)
                        item.expires_at = datetime.now(UTC) + timedelta(
                            seconds=arguments.idle_timeout_seconds
                        )
                        item.task = asyncio.create_task(self._worker(item))
                        # Cancellation before the first step skips the worker's finally.
                        item.task.add_done_callback(lambda task: self._finish_queued(item))
                        self.sessions[key] = item
                    item.pending += 1
                    job = _Job(operation, arguments, deadline, loop.create_future())
                    item.queue.put_nowait(job)
                payload = await job.future
                expires_at = item.expires_at.isoformat()
                limit = MAX_CATALOG_BYTES if discover else MAX_RESULT_BYTES
                data = json.dumps(payload, ensure_ascii=True, allow_nan=False).encode()
                if len(data) > limit:
                    raise AgentError("mcp.responseTooLarge", "MCP result exceeds its size limit")
                identifier = str(uuid4())
                artifact = ArtifactDescriptor(
                    artifactId=identifier,
                    backendId=backend_id,
                    sessionId=arguments.session_id,
                    path=f"mcp/{identifier}.json",
                    filename="mcp-result.json",
                    size=len(data),
                    sha256=hashlib.sha256(data).hexdigest(),
                    contentType="application/json",
                )
                if loop.time() >= deadline:
                    raise TimeoutError
                await asyncio.to_thread(self.cache.store, artifact, io.BytesIO(data))
                if loop.time() >= deadline:
                    raise TimeoutError
            return {
                "artifact": artifact.model_dump(by_alias=True),
                "session": {
                    "id": item.id,
                    "started": started,
                    "idleTimeoutSeconds": arguments.idle_timeout_seconds,
                    "expiresAt": expires_at,
                },
            }
        except TimeoutError:
            if job is not None and (
                discover or item.active is job or (started and item.initializing)
            ):
                self._request_stop(item)
                await asyncio.wait({item.task}, timeout=CLEANUP_TIMEOUT_SECONDS)
            if job is not None and job.dispatched:
                raise AgentError("mcp.outcomeUnknown", "MCP tool outcome is unknown") from None
            raise AgentError("mcp.timeout", "MCP operation timed out") from None
        except asyncio.CancelledError:
            if job is not None and (
                discover or item.active is job or (started and item.initializing)
            ):
                await self._stop(item)
            raise
        finally:
            if discover and job is not None:
                if not item.stopping:
                    self._request_stop(item)
                    await asyncio.wait({item.task}, timeout=CLEANUP_TIMEOUT_SECONDS)
                if item.task.done():
                    self.sessions.pop(key, None)

    async def _discover(self, client):
        tools, cursors, cursor, size = [], set(), None, 2
        while True:
            result = await client.list_tools(cursor=cursor)
            for tool in result.tools:
                item = {
                    "name": tool.name,
                    "title": tool.title,
                    "description": tool.description or "",
                    "input_schema": tool.inputSchema,
                    "output_schema": tool.outputSchema,
                    "annotations": {
                        name: getattr(tool.annotations, name, None)
                        for name in (
                            "readOnlyHint",
                            "destructiveHint",
                            "idempotentHint",
                            "openWorldHint",
                        )
                    },
                }
                size += len(json.dumps(item, ensure_ascii=True, allow_nan=False).encode()) + 1
                if size > MAX_CATALOG_BYTES:
                    raise AgentError("mcp.responseTooLarge", "MCP catalog exceeds its size limit")
                tools.append(item)
            cursor = result.nextCursor
            if cursor is None:
                return tools
            if cursor in cursors:
                raise AgentError("mcp.invalidResponse", "MCP catalog cursor repeated")
            cursors.add(cursor)
            size += len(cursor.encode())

    async def _worker(self, item):
        current = None
        failure = "mcp.startFailed"
        loop = asyncio.get_running_loop()
        try:
            # Keep the initiating request separate from followers so it gets
            # the startup error instead of the generic queue-drain error.
            current = item.queue.get_nowait()
            args = item.arguments
            params = StdioServerParameters(
                command=args.executable,
                args=args.args,
                cwd=args.working_directory,
                env=args.environment,
            )
            limit = MAX_RESULT_BYTES if hasattr(args, "name") else MAX_CATALOG_BYTES
            async with bounded_stdio_client(params, limit + FRAME_ENVELOPE_BYTES) as (read, write):
                async with ClientSession(read, write) as client:
                    await client.initialize()
                    item.initializing = False
                    failure = "mcp.unavailable"
                    while True:
                        if current is None:
                            try:
                                current = await asyncio.wait_for(
                                    item.queue.get(), args.idle_timeout_seconds
                                )
                            except TimeoutError:
                                return
                        future = current.future
                        if future.cancelled() or loop.time() >= current.deadline:
                            if not future.done():
                                future.set_exception(
                                    AgentError("mcp.timeout", "MCP operation timed out")
                                )
                            item.pending -= 1
                            current = None
                            continue
                        item.active = current
                        arguments = current.arguments
                        if current.operation == "integration.mcp.discover":
                            payload = await self._discover(client)
                        else:
                            client._tool_output_schemas[arguments.name] = arguments.output_schema
                            current.dispatched = True
                            result = await client.call_tool(
                                arguments.name, arguments=arguments.arguments
                            )
                            payload = result.model_dump(
                                mode="json", by_alias=True, exclude_none=True
                            )
                        item.expires_at = datetime.now(UTC) + timedelta(
                            seconds=args.idle_timeout_seconds
                        )
                        item.pending -= 1
                        item.active = None
                        if not future.done():
                            future.set_result(payload)
                        current = None
        except BaseException as exc:
            if current is not None and current.dispatched:
                failure = "mcp.outcomeUnknown"
            # Task groups wrap transport and discovery failures. Only controlled
            # codes may cross this boundary; never log or expose exception text.
            remaining = [exc]
            while remaining:
                error = remaining.pop()
                if isinstance(error, BaseExceptionGroup):
                    remaining.extend(reversed(error.exceptions))
                elif isinstance(error, AgentError) and error.code in {
                    "mcp.responseTooLarge",
                    "mcp.invalidResponse",
                }:
                    failure = error.code
                    break
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
        finally:
            item.active = None
            if current is not None:
                future = current.future
                item.pending -= 1
                if not future.done():
                    message = "MCP process stopped"
                    if failure == "mcp.startFailed":
                        message = "MCP process could not start or initialize"
                    future.set_exception(AgentError(failure, message))
            self._finish_queued(item)
