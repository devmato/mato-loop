import asyncio
import json
import logging
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import anyio
from mcp import ClientSession, StdioServerParameters

from matoloop_agent.errors import AgentError
from matoloop_agent.mcp_transport import CHUNK_BYTES, bounded_stdio_client


def agent_errors(error):
    if isinstance(error, BaseExceptionGroup):
        return [item for child in error.exceptions for item in agent_errors(child)]
    return [error] if isinstance(error, AgentError) else []


class McpTransportTests(unittest.IsolatedAsyncioTestCase):
    async def consume(self, code, limit=128):
        server = StdioServerParameters(command=sys.executable, args=["-c", code])
        async with bounded_stdio_client(server, limit) as (read, _):
            return await read.receive()

    async def test_oversized_frames_are_rejected_before_parsing(self):
        limit = CHUNK_BYTES + 128
        for newline in (b"", b"\n"):
            with (
                self.subTest(newline=newline),
                patch("matoloop_agent.mcp_transport._decode_frame") as decode,
            ):
                with self.assertRaises(ExceptionGroup) as raised:
                    await asyncio.wait_for(
                        self.consume(
                            "import sys, time; "
                            f"sys.stdout.buffer.write(b'x' * {limit + 1} + {newline!r}); "
                            "sys.stdout.buffer.flush(); time.sleep(30)",
                            limit,
                        ),
                        timeout=8,
                    )
                self.assertEqual(
                    [error.code for error in agent_errors(raised.exception)],
                    ["mcp.responseTooLarge"],
                )
                decode.assert_not_called()

    async def test_invalid_frames_do_not_log_rejected_inputs(self):
        sentinel = "PRIVATE_MCP_FRAME_SENTINEL"
        frames = (
            sentinel.encode() + b"\n",
            sentinel.encode(),
            b"\xff\n",
            b'{"jsonrpc":"2.0","id":1,"result":{"value":NaN}}\n',
            b'{"jsonrpc":"2.0","id":1,"result":{"value":1e999}}\n',
            b"{}\n",
        )
        for frame in frames:
            with self.subTest(frame=frame), self.assertNoLogs(level=logging.WARNING):
                with self.assertRaises(ExceptionGroup) as raised:
                    await self.consume(f"import os; os.write(1, {frame!r})")
                self.assertEqual(
                    [error.code for error in agent_errors(raised.exception)],
                    ["mcp.invalidResponse"],
                )
                self.assertNotIn(sentinel, str(raised.exception))

    async def test_valid_frame_at_eof_without_newline(self):
        frame = b'{"jsonrpc":"2.0","id":1,"result":{"text":"hello"}}'
        message = await self.consume(f"import os; os.write(1, {frame!r})", len(frame))
        self.assertEqual(message.message.root.result, {"text": "hello"})

    async def test_client_session_never_logs_invalid_server_messages(self):
        sentinel = "PRIVATE_MCP_PARAMS_SENTINEL"
        messages = (
            {"method": "notifications/progress", "params": {"progress": sentinel}},
            {"id": 1, "method": "sampling/createMessage", "params": {"messages": sentinel}},
            {"method": "notifications/unknown", "params": {"value": sentinel}},
        )

        async def consume_with_session(frame):
            server = StdioServerParameters(
                command=sys.executable, args=["-c", f"import os; os.write(1, {frame!r})"]
            )
            async with bounded_stdio_client(server, 1024) as (read, write):
                async with ClientSession(read, write):
                    await asyncio.Event().wait()

        for message in messages:
            frame = (json.dumps({"jsonrpc": "2.0", **message}) + "\n").encode()
            with self.subTest(method=message["method"]), self.assertNoLogs(level=logging.WARNING):
                with self.assertRaises(ExceptionGroup) as raised:
                    await asyncio.wait_for(consume_with_session(frame), timeout=8)
                errors = agent_errors(raised.exception)
                self.assertEqual([error.code for error in errors], ["mcp.invalidResponse"])
                self.assertNotIn(sentinel, str(errors[0]))

    async def test_response_ids_are_normalized_or_rejected_before_sdk_logging(self):
        sentinel = "PRIVATE_MCP_RESPONSE_ID_SENTINEL"
        initialized = {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "serverInfo": {"name": "fixture", "version": "1"},
        }

        async def initialize(identifier, body):
            frame = {"jsonrpc": "2.0", "id": identifier, **body}
            code = f"import sys; sys.stdin.readline(); print({json.dumps(frame)!r}, flush=True)"
            server = StdioServerParameters(command=sys.executable, args=["-c", code])
            async with bounded_stdio_client(server, 1024) as (read, write):
                async with ClientSession(read, write) as client:
                    return await client.initialize()

        for identifier in (sentinel, "9" * 128, True):
            for body in (
                {"result": initialized},
                {"error": {"code": -32603, "message": sentinel}},
            ):
                with (
                    self.subTest(identifier=identifier, body=body),
                    self.assertNoLogs(level=logging.WARNING),
                ):
                    with self.assertRaises(ExceptionGroup) as raised:
                        await asyncio.wait_for(initialize(identifier, body), timeout=5)
                    errors = agent_errors(raised.exception)
                    self.assertEqual([error.code for error in errors], ["mcp.invalidResponse"])
                    self.assertNotIn(sentinel, str(errors[0]))
        for identifier in (0, "0"):
            with self.subTest(identifier=identifier), self.assertNoLogs(level=logging.WARNING):
                response = await asyncio.wait_for(
                    initialize(identifier, {"result": initialized}), 5
                )
                self.assertEqual(response.serverInfo.name, "fixture")

    @unittest.skipUnless(sys.platform == "linux", "Uses Linux process state to detect zombies")
    async def test_descendant_is_killed_even_after_server_exits_on_stdin_eof(self):
        code = (
            "import json, subprocess, sys; "
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
            "print(json.dumps({'jsonrpc':'2.0','id':1,'result':{'pid':child.pid}}), flush=True); "
            "sys.stdin.read()"
        )
        message = await self.consume(code)
        pid = message.message.root.result["pid"]
        status = Path(f"/proc/{pid}/stat")
        try:
            for _ in range(100):
                try:
                    state = status.read_text().split()[2]
                except (FileNotFoundError, ProcessLookupError):
                    # The kernel can reap the child while /proc is being read.
                    return
                if state == "Z":
                    return
                await asyncio.sleep(0.01)
            self.fail("MCP descendant survived server shutdown")
        finally:
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass

    async def test_repeated_cancellation_still_terminates_tree_and_closes_handles(self):
        entered = asyncio.Event()
        terminating = asyncio.Event()
        release = asyncio.Event()
        process = Mock(spec=["stdin", "stdout", "returncode", "wait", "aclose", "pid"])
        process.pid = 99999999
        process.stdin = Mock(aclose=AsyncMock())
        process.stdout = Mock(receive=AsyncMock(side_effect=anyio.EndOfStream))
        process.returncode = None
        process.wait = AsyncMock(side_effect=lambda: None)
        process.aclose = AsyncMock()

        async def terminate(*args, **kwargs):
            terminating.set()
            await release.wait()
            process.returncode = -9

        async def worker():
            async with bounded_stdio_client(StdioServerParameters(command="fixture"), 128):
                entered.set()
                await asyncio.Event().wait()

        with (
            patch(
                "matoloop_agent.mcp_transport._create_platform_compatible_process",
                AsyncMock(return_value=process),
            ),
            patch(
                "matoloop_agent.mcp_transport._terminate_process_tree",
                AsyncMock(side_effect=terminate),
            ) as terminate_tree,
        ):
            task = asyncio.create_task(worker())
            await entered.wait()
            task.cancel()
            await asyncio.wait_for(terminating.wait(), 2)
            task.cancel()
            await asyncio.sleep(0)
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
            terminate_tree.assert_awaited_once()
            process.aclose.assert_awaited_once()

    async def test_startup_cancellation_waits_for_creator_and_closes_process(self):
        spawning = asyncio.Event()
        release = asyncio.Event()
        process = Mock(spec=["stdin", "returncode", "wait", "aclose", "pid"])
        process.pid = 99999999
        process.stdin = Mock(aclose=AsyncMock())
        process.returncode = 0
        process.wait = AsyncMock()
        process.aclose = AsyncMock()

        async def create(**kwargs):
            spawning.set()
            await release.wait()
            return process

        async def worker():
            try:
                async with bounded_stdio_client(StdioServerParameters(command="fixture"), 128):
                    self.fail("Cancelled startup entered the client context")
            except asyncio.CancelledError:
                process.stdin.aclose.assert_awaited_once()
                process.aclose.assert_awaited_once()
                raise

        with patch(
            "matoloop_agent.mcp_transport._create_platform_compatible_process",
            AsyncMock(side_effect=create),
        ):
            task = asyncio.create_task(worker())
            await asyncio.wait_for(spawning.wait(), 2)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
