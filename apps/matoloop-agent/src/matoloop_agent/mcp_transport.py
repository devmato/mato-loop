"""Bounded, non-logging stdio framing for the MCP SDK's ClientSession."""

from __future__ import annotations

import asyncio
import json
import math
import os
import signal
import sys
from contextlib import asynccontextmanager, suppress

import anyio
from mcp import StdioServerParameters
from mcp.client.stdio import (
    _create_platform_compatible_process,
    _get_executable_command,
    _terminate_process_tree,
    get_default_environment,
)
from mcp.shared.message import SessionMessage
from mcp.types import (
    JSONRPCError,
    JSONRPCMessage,
    JSONRPCNotification,
    JSONRPCRequest,
    JSONRPCResponse,
    ServerNotification,
    ServerRequest,
)

from .errors import AgentError

# The SDK's public stdio client parses unbounded text and logs rejected inputs.
# These private helpers retain its Windows Job Object / POSIX process-group support
# without copying platform process management into this bounded framing adapter.
FRAME_ENVELOPE_BYTES = 64 * 1024
CHUNK_BYTES = 64 * 1024
SHUTDOWN_GRACE_SECONDS = 0.5
TERMINATION_SECONDS = 2.0


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Non-finite JSON number")
    return number


def _invalid_constant(value: str):
    raise ValueError("Non-finite JSON constant")


def _decode_frame(frame: bytearray) -> SessionMessage:
    try:
        payload = json.loads(
            frame.decode("utf-8"),
            parse_float=_finite_float,
            parse_constant=_invalid_constant,
        )
        message = JSONRPCMessage.model_validate(payload)
        if isinstance(message.root, JSONRPCResponse | JSONRPCError):
            # The SDK emits sequential integer IDs and logs raw strings when
            # normalization fails. Accept bounded decimal echoes, never raw IDs.
            identifier = payload["id"]
            if isinstance(identifier, str):
                if len(identifier) > 16 or not identifier.isascii() or not identifier.isdecimal():
                    raise ValueError("Invalid response ID")
                identifier = int(identifier)
            if type(identifier) is not int or not 0 <= identifier < 2**53:
                raise ValueError("Invalid response ID")
            message.root.id = identifier
        # Match ClientSession's validation, including its rejection of unknown
        # methods, before its receive loop can log input-bearing SDK errors.
        elif isinstance(message.root, JSONRPCRequest):
            ServerRequest.model_validate(
                message.root.model_dump(by_alias=True, mode="json", exclude_none=True)
            )
        elif isinstance(message.root, JSONRPCNotification):
            ServerNotification.model_validate(
                message.root.model_dump(by_alias=True, mode="json", exclude_none=True)
            )
        return SessionMessage(message)
    except (ValueError, RecursionError):
        # Neither Pydantic errors nor their input-bearing causes may escape.
        raise AgentError("mcp.invalidResponse", "MCP server returned an invalid frame") from None


async def _shutdown(process) -> None:
    with anyio.CancelScope(shield=True):
        if process.stdin is not None:
            with anyio.move_on_after(SHUTDOWN_GRACE_SECONDS):
                with suppress(anyio.BrokenResourceError, anyio.ClosedResourceError, OSError):
                    await process.stdin.aclose()
        with anyio.move_on_after(SHUTDOWN_GRACE_SECONDS):
            await process.wait()
        returncode = process.popen.returncode if hasattr(process, "popen") else process.returncode
        if returncode is None or sys.platform == "win32":
            # The SDK helper includes TERM/KILL escalation for the entire tree.
            with anyio.move_on_after(2 * TERMINATION_SECONDS + 1):
                await _terminate_process_tree(process, timeout_seconds=TERMINATION_SECONDS)
        if sys.platform != "win32":
            # A server may exit on stdin EOF while descendants retain its group.
            # The SDK resolves pgid from the leader and cannot find that group
            # once the leader is reaped. Its spawn helper sets pgid == pid.
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        with anyio.move_on_after(TERMINATION_SECONDS):
            if hasattr(process, "aclose"):
                await process.aclose()
            else:
                # The SDK's Windows fallback exposes context-manager cleanup only.
                await process.__aexit__(None, None, None)


async def _finish_shutdown(process) -> None:
    # AnyIO shielding handles scope cancellation, but not repeated asyncio Task.cancel().
    # Process cleanup runs in a separate task; task-group scopes stay in the
    # calling task. Wait for cleanup even if another caller cancels that task again.
    with anyio.CancelScope(shield=True):
        cleanup = asyncio.create_task(_shutdown(process))
        cancelled = False
        while True:
            try:
                await asyncio.shield(cleanup)
                break
            except asyncio.CancelledError:
                if cleanup.done():
                    cleanup.result()
                    raise
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError


@asynccontextmanager
async def bounded_stdio_client(server: StdioServerParameters, max_frame_bytes: int):
    """Yield SDK message streams; the caller supplies the complete wire-frame limit.

    Use the operation's payload limit plus FRAME_ENVELOPE_BYTES for JSON-RPC framing.
    Limits apply to bytes before UTF-8 decoding or JSON/Pydantic parsing.
    """
    if max_frame_bytes <= 0:
        raise ValueError("MCP frame limit must be positive")
    incoming, read_stream = anyio.create_memory_object_stream[SessionMessage | Exception](0)
    write_stream, outgoing = anyio.create_memory_object_stream[SessionMessage](0)
    process = None
    try:
        with open(os.devnull, "w") as stderr:
            with anyio.CancelScope(shield=True):
                # Raw Task.cancel() bypasses AnyIO shielding. Keep ownership of
                # the creator until its process is available for final cleanup.
                creator = asyncio.create_task(
                    _create_platform_compatible_process(
                        command=_get_executable_command(server.command),
                        args=server.args,
                        env={**get_default_environment(), **(server.env or {})},
                        errlog=stderr,
                        cwd=server.cwd,
                    )
                )
                cancelled = False
                while True:
                    try:
                        process = await asyncio.shield(creator)
                        break
                    except asyncio.CancelledError:
                        if creator.cancelled():
                            raise
                        cancelled = True
                if cancelled:
                    raise asyncio.CancelledError

        async def stdout_reader():
            assert process.stdout is not None
            frame = bytearray()
            async with incoming:
                while True:
                    try:
                        chunk = await process.stdout.receive(CHUNK_BYTES)
                    except anyio.EndOfStream:
                        if frame:
                            await incoming.send(_decode_frame(frame))
                        return
                    start = 0
                    while start < len(chunk):
                        end = chunk.find(b"\n", start)
                        stop = len(chunk) if end < 0 else end
                        if len(frame) + stop - start > max_frame_bytes:
                            raise AgentError(
                                "mcp.responseTooLarge", "MCP frame exceeds its size limit"
                            )
                        frame.extend(chunk[start:stop])
                        if end < 0:
                            break
                        await incoming.send(_decode_frame(frame))
                        frame.clear()
                        start = end + 1

        async def stdin_writer():
            assert process.stdin is not None
            async with outgoing:
                async for message in outgoing:
                    try:
                        data = (
                            json.dumps(
                                message.message.model_dump(
                                    mode="json", by_alias=True, exclude_none=True
                                ),
                                ensure_ascii=True,
                                allow_nan=False,
                                separators=(",", ":"),
                            )
                            + "\n"
                        ).encode("utf-8")
                    except (ValueError, RecursionError):
                        raise AgentError(
                            "mcp.invalidResponse", "MCP message is not valid JSON"
                        ) from None
                    for start in range(0, len(data), CHUNK_BYTES):
                        await process.stdin.send(data[start : start + CHUNK_BYTES])

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(stdout_reader)
            tasks.start_soon(stdin_writer)
            try:
                yield read_stream, write_stream
            finally:
                tasks.cancel_scope.cancel()
    finally:
        try:
            if process is not None:
                await _finish_shutdown(process)
        finally:
            with anyio.CancelScope(shield=True):
                await read_stream.aclose()
                await write_stream.aclose()
                await incoming.aclose()
                await outgoing.aclose()
