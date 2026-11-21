import asyncio
import hashlib
import json
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import uuid4

from pydantic import ValidationError

from matoloop_agent.errors import AgentError
from matoloop_agent.mcp import McpProcesses
from matoloop_agent.mcp_transport import bounded_stdio_client
from matoloop_agent.protocol import ArtifactDescriptor, Request, validate_arguments

# Handle calls concurrently so the fixture cannot hide missing client-side serialization.
SERVER = r"""
import json
import os
import sys
import threading
import time
from pathlib import Path

counter = 0
active = 0
write_lock = threading.Lock()


def reply(identifier, result):
    with write_lock:
        print(json.dumps({"jsonrpc": "2.0", "id": identifier, "result": result}), flush=True)


def call(message):
    global counter, active
    active += 1
    arguments = message["params"].get("arguments", {})
    if arguments.get("marker"):
        Path(arguments["marker"]).touch()
    if arguments.get("exit"):
        os._exit(7)
    deadline = time.monotonic() + 5
    while arguments.get("release") and not Path(arguments["release"]).exists():
        if time.monotonic() > deadline:
            raise RuntimeError("test did not release the call")
        time.sleep(0.01)
    time.sleep(arguments.get("delay", 0))
    counter += 1
    result = {"counter": counter, "pid": os.getpid(), "overlap": active > 1}
    active -= 1
    reply(message["id"], {"content": [{"type": "text", "text": json.dumps(result)}]})


for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "initialize":
        reply(message["id"], {
            "protocolVersion": message["params"]["protocolVersion"],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "counter-fixture", "version": "1"},
        })
    elif method == "tools/list":
        reply(message["id"], {"tools": [{
            "name": "counter", "description": "Increment process-local state",
            "inputSchema": {"type": "object"},
        }], "nextCursor": "repeated" if os.getenv("REPEAT_CURSOR") else None})
    elif method == "tools/call":
        threading.Thread(target=call, args=(message,), daemon=True).start()
"""


class McpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.script = self.directory / "server.py"
        self.script.write_text(SERVER, encoding="utf-8")
        self.connection_id = str(uuid4())
        self.session_id = str(uuid4())
        self.config = {
            "connectionId": self.connection_id,
            "sessionId": self.session_id,
            "executable": Path(sys.executable).as_posix(),
            "args": [str(self.script)],
            "workingDirectory": self.directory.as_posix(),
            "idleTimeoutSeconds": 30,
        }
        self.artifacts = {}
        self.cache = Mock()
        self.cache.store.side_effect = lambda descriptor, stream: self.artifacts.update(
            {descriptor.artifact_id: stream.read()}
        )
        self.filesystem = Mock()
        self.processes = McpProcesses(self.cache, self.filesystem)
        self.addAsyncCleanup(self.processes.close_all)

    async def dispatch(self, action, arguments=None, *, backend="backend"):
        operation = f"integration.mcp.{action}"
        if action in {"sessions", "close"}:
            operation = "integration.mcp.manage"
            arguments = {
                "action": "list" if action == "sessions" else "close",
                **(arguments or {}),
            }
        request = Request(
            type="request",
            id=str(uuid4()),
            operation=operation,
            arguments=arguments or {},
        )
        return await asyncio.wait_for(
            self.processes.dispatch(request.operation, validate_arguments(request), backend),
            timeout=10,
        )

    async def call(self, *, arguments=None, backend="backend", **config):
        return await self.dispatch(
            "call",
            {**self.config, **config, "name": "counter", "arguments": arguments or {}},
            backend=backend,
        )

    def payload(self, result):
        return json.loads(self.artifacts[result["artifact"]["artifactId"]])

    def value(self, result):
        return json.loads(self.payload(result)["content"][0]["text"])

    async def wait_for_marker(self, marker):
        async with asyncio.timeout(5):
            while not marker.exists():
                await asyncio.sleep(0.01)

    async def test_discovery_is_short_lived_and_calls_keep_chat_process_and_scoped_artifacts(self):
        discovered = await self.dispatch("discover", self.config)
        self.assertEqual((await self.dispatch("sessions"))["sessions"], [])
        first = await self.call()
        second = await self.call(arguments={"delay": 0.01})
        self.assertEqual(self.payload(discovered)[0]["name"], "counter")
        self.assertEqual([self.value(item)["counter"] for item in (first, second)], [1, 2])
        self.assertNotEqual(first["session"]["id"], discovered["session"]["id"])
        self.assertEqual(second["session"]["id"], first["session"]["id"])
        self.assertTrue(discovered["session"]["started"])
        self.assertFalse(second["session"]["started"])
        descriptor = ArtifactDescriptor.model_validate(second["artifact"])
        data = self.artifacts[descriptor.artifact_id]
        self.assertEqual(
            (descriptor.backend_id, descriptor.session_id, descriptor.size, descriptor.sha256),
            ("backend", self.session_id, len(data), hashlib.sha256(data).hexdigest()),
        )
        self.filesystem.resolve_directory.assert_called_with(self.directory.as_posix())

    async def test_chat_connection_and_backend_each_isolate_process_state(self):
        results = [
            await self.call(),
            await self.call(sessionId=str(uuid4())),
            await self.call(connectionId=str(uuid4())),
            await self.call(backend="other-backend"),
        ]
        self.assertEqual([self.value(result)["counter"] for result in results], [1] * 4)
        self.assertEqual(len({self.value(result)["pid"] for result in results}), 4)
        self.assertEqual(len((await self.dispatch("sessions"))["sessions"]), 3)
        other = await self.dispatch("sessions", backend="other-backend")
        self.assertEqual([item["id"] for item in other["sessions"]], [results[3]["session"]["id"]])

    async def test_configuration_replacement_restarts_idle_process(self):
        previous = await self.call()
        alternate = self.directory / "alternate"
        alternate.mkdir()
        for changed in (
            {"environment": {"COUNTER_CONFIG": "changed"}},
            {"args": [str(self.script), "changed"]},
            {"workingDirectory": alternate.as_posix()},
            {"idleTimeoutSeconds": 20},
        ):
            with self.subTest(changed=changed):
                self.config.update(changed)
                result = await self.call()
                self.assertNotEqual(result["session"]["id"], previous["session"]["id"])
                self.assertEqual(self.value(result)["counter"], 1)
                self.assertEqual(len((await self.dispatch("sessions"))["sessions"]), 1)
                previous = result

    async def test_stale_close_does_not_stop_the_replacement_process(self):
        await self.call()
        row = (await self.dispatch("sessions"))["sessions"][0]
        replacement = await self.call(idleTimeoutSeconds=20)
        with self.assertRaises(AgentError) as raised:
            await self.dispatch(
                "close",
                {
                    "connectionId": row["connectionId"],
                    "sessionId": row["sessionId"],
                    "processId": row["id"],
                },
            )
        remaining = (await self.dispatch("sessions"))["sessions"]
        self.assertEqual(
            (raised.exception.code, [item["id"] for item in remaining]),
            ("integration.changed", [replacement["session"]["id"]]),
        )

    async def test_idle_expiry_prunes_and_restarts_process(self):
        first = await self.call(idleTimeoutSeconds=1)
        worker = next(iter(self.processes.sessions.values())).task
        await asyncio.wait_for(asyncio.shield(worker), timeout=5)
        self.assertEqual((await self.dispatch("sessions"))["sessions"], [])
        second = await self.call(idleTimeoutSeconds=1)
        self.assertNotEqual(second["session"]["id"], first["session"]["id"])
        self.assertEqual(self.value(second)["counter"], 1)

    async def test_four_process_cap_reuses_existing_and_admits_after_close(self):
        chats = [self.session_id, *(str(uuid4()) for _ in range(3))]
        results = await asyncio.gather(*(self.call(sessionId=chat) for chat in chats))
        self.assertEqual(len((await self.dispatch("sessions"))["sessions"]), 4)
        with self.assertRaises(AgentError) as raised:
            await self.call(sessionId=str(uuid4()))
        self.assertEqual(raised.exception.code, "mcp.processLimitReached")
        self.assertEqual(self.value(await self.call())["counter"], 2)
        closed = await self.dispatch(
            "close",
            {
                "connectionId": self.connection_id,
                "sessionId": chats[-1],
                "processId": results[-1]["session"]["id"],
            },
        )
        self.assertEqual(closed, {"closed": True})
        self.assertTrue((await self.call(sessionId=str(uuid4())))["session"]["started"])

    async def test_calls_serialize_and_busy_process_cannot_close_or_reconfigure(self):
        marker, release = self.directory / "entered", self.directory / "release"
        first = asyncio.create_task(
            self.call(arguments={"marker": str(marker), "release": str(release)})
        )
        try:
            await self.wait_for_marker(marker)
            listed = (await self.dispatch("sessions"))["sessions"]
            self.assertTrue(listed[0]["busy"])
            with self.assertRaises(AgentError) as close_error:
                await self.dispatch(
                    "close",
                    {
                        "connectionId": self.connection_id,
                        "sessionId": self.session_id,
                        "processId": listed[0]["id"],
                    },
                )
            self.assertEqual(close_error.exception.code, "mcp.busy")
            with self.assertRaises(AgentError) as replacement_error:
                await self.call(environment={"COUNTER_CONFIG": "replacement"})
            self.assertEqual(replacement_error.exception.code, "mcp.busy")
            second = asyncio.create_task(self.call())
            try:
                await asyncio.sleep(0.1)
                self.assertFalse(second.done())
            finally:
                release.touch()
                results = await asyncio.gather(first, second)
        finally:
            release.touch()
            await first
        self.assertEqual([self.value(result)["counter"] for result in results], [1, 2])
        self.assertFalse(any(self.value(result)["overlap"] for result in results))

    async def test_cancelled_queued_call_does_not_stop_active_call(self):
        marker, release = self.directory / "entered", self.directory / "release"
        first = asyncio.create_task(
            self.call(arguments={"marker": str(marker), "release": str(release)})
        )
        second = None
        try:
            await self.wait_for_marker(marker)
            second = asyncio.create_task(self.call())
            async with asyncio.timeout(5):
                while next(iter(self.processes.sessions.values())).pending < 2:
                    await asyncio.sleep(0.01)
            second.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await second
            release.touch()
            result = await first
            following = await self.call()
            self.assertEqual(
                (self.value(result)["counter"], self.value(following)["counter"]), (1, 2)
            )
        finally:
            release.touch()
            await asyncio.gather(
                *(task for task in (first, second) if task), return_exceptions=True
            )

    async def test_repeated_cancellation_and_close_all_wait_for_worker_cleanup(self):
        entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

        @asynccontextmanager
        async def slow_cleanup(*args):
            try:
                async with bounded_stdio_client(*args) as streams:
                    yield streams
            finally:
                entered.set()
                await release.wait()
                finished.set()

        marker = self.directory / "entered"
        with patch("matoloop_agent.mcp.bounded_stdio_client", slow_cleanup):
            call = asyncio.create_task(
                self.call(
                    arguments={"marker": str(marker), "release": str(self.directory / "hold")}
                )
            )
            closing = None
            try:
                await self.wait_for_marker(marker)
                worker = next(iter(self.processes.sessions.values())).task
                call.cancel()
                await asyncio.wait_for(entered.wait(), timeout=5)
                with self.assertRaises(AgentError) as raised:
                    await self.call()
                self.assertEqual(raised.exception.code, "mcp.busy")
                closing = asyncio.create_task(self.processes.close_all())
                for _ in range(2):
                    await asyncio.sleep(0.01)
                    call.cancel()
                    closing.cancel()
                await asyncio.sleep(0.01)
                self.assertTrue(self.processes.maintenance_busy)
                self.assertFalse(worker.done())
                release.set()
                results = await asyncio.gather(call, closing, return_exceptions=True)
                self.assertTrue(finished.is_set())
                self.assertTrue(
                    all(isinstance(result, asyncio.CancelledError) for result in results)
                )
                self.assertEqual(self.processes.sessions, {})
            finally:
                release.set()
                await asyncio.gather(
                    *(task for task in (call, closing) if task), return_exceptions=True
                )

    async def test_discovery_preserves_controlled_errors_inside_task_groups(self):
        for code, limit, environment in (
            ("mcp.responseTooLarge", 64, {}),
            ("mcp.invalidResponse", 1024, {"REPEAT_CURSOR": "1"}),
        ):
            with self.subTest(code=code), patch("matoloop_agent.mcp.MAX_CATALOG_BYTES", limit):
                with self.assertRaises(AgentError) as raised:
                    await self.dispatch("discover", {**self.config, "environment": environment})
                self.assertEqual(raised.exception.code, code)

    async def test_close_all_before_worker_starts_finishes_queued_call(self):
        request = Request(
            type="request",
            id=str(uuid4()),
            operation="integration.mcp.call",
            arguments={**self.config, "name": "counter", "arguments": {}},
        )
        call = asyncio.create_task(
            self.processes.dispatch(request.operation, validate_arguments(request), "backend")
        )
        # close_all is ready before dispatch schedules the worker's first step.
        closing = asyncio.create_task(self.processes.close_all())
        await asyncio.wait_for(closing, timeout=5)
        with self.assertRaises(AgentError) as raised:
            await asyncio.wait_for(call, timeout=5)
        self.assertEqual(raised.exception.code, "mcp.unavailable")

    async def test_close_all_finishes_active_and_queued_calls_and_clears_busy_state(self):
        marker = self.directory / "entered"
        first = asyncio.create_task(
            self.call(arguments={"marker": str(marker), "release": str(self.directory / "release")})
        )
        second = None
        try:
            await self.wait_for_marker(marker)
            second = asyncio.create_task(self.call())
            await asyncio.sleep(0.05)
            workers = [item.task for item in self.processes.sessions.values()]
            self.assertTrue(self.processes.maintenance_busy)
            await asyncio.wait_for(self.processes.close_all(), timeout=5)
            results = await asyncio.gather(first, second, return_exceptions=True)
            self.assertEqual(
                [result.code for result in results], ["mcp.outcomeUnknown", "mcp.unavailable"]
            )
            self.assertTrue(all(worker.done() for worker in workers))
            self.assertFalse(self.processes.maintenance_busy)
            self.assertEqual((await self.dispatch("sessions"))["sessions"], [])
        finally:
            await self.processes.close_all()
            await asyncio.gather(
                *(task for task in (first, second) if task), return_exceptions=True
            )

    async def test_server_exit_after_dispatch_reports_unknown_outcome_without_replay(self):
        marker = self.directory / "dispatched"
        with self.assertRaises(AgentError) as raised:
            await self.call(arguments={"marker": str(marker), "exit": True})
        self.assertEqual(raised.exception.code, "mcp.outcomeUnknown")
        self.assertTrue(marker.exists())
        self.assertEqual(self.artifacts, {})
        self.assertEqual(self.value(await self.call())["counter"], 1)

    async def test_initiating_call_gets_startup_error_on_spawn_or_initialize_failure(self):
        for config in (
            {"executable": (self.directory / "missing-executable").as_posix()},
            {"args": ["-c", "import sys; sys.stdin.readline()"]},
        ):
            with self.subTest(config=config), self.assertRaises(AgentError) as raised:
                await self.call(**config)
            self.assertEqual(raised.exception.code, "mcp.startFailed")
        self.assertEqual((await self.dispatch("sessions"))["sessions"], [])

    async def test_call_timeout_reports_whether_tool_was_dispatched(self):
        with patch("matoloop_agent.mcp.CALL_TIMEOUT_SECONDS", 0.5):
            with self.assertRaises(AgentError) as startup:
                await self.call(args=["-c", "import sys; sys.stdin.read()"])
            marker, release = self.directory / "deadline-entered", self.directory / "deadline-hold"
            with self.assertRaises(AgentError) as tool:
                await self.call(arguments={"marker": str(marker), "release": str(release)})
        self.assertEqual(
            (startup.exception.code, tool.exception.code), ("mcp.timeout", "mcp.outcomeUnknown")
        )
        self.assertTrue(marker.exists())
        self.assertEqual((await self.dispatch("sessions"))["sessions"], [])

    async def test_expired_queued_call_never_runs_and_does_not_stop_active_call(self):
        marker, release = self.directory / "queue-entered", self.directory / "queue-release"
        skipped = self.directory / "queue-skipped"
        first = asyncio.create_task(
            self.call(arguments={"marker": str(marker), "release": str(release)})
        )
        try:
            await self.wait_for_marker(marker)
            with patch("matoloop_agent.mcp.CALL_TIMEOUT_SECONDS", 0.05):
                with self.assertRaises(AgentError) as raised:
                    await self.call(arguments={"marker": str(skipped)})
            self.assertEqual(raised.exception.code, "mcp.timeout")
        finally:
            release.touch()
            await first
        self.assertEqual(self.value(await self.call())["counter"], 2)
        self.assertFalse(skipped.exists())

    async def test_directory_rejection_prevents_process_creation(self):
        self.filesystem.resolve_directory.side_effect = AgentError(
            "file.accessDenied", "Directory is not permitted"
        )
        with self.assertRaises(AgentError) as raised:
            await self.call()
        self.assertEqual(raised.exception.code, "file.accessDenied")
        self.assertEqual(self.processes.sessions, {})


class McpProtocolTests(unittest.TestCase):
    def test_manage_requires_a_valid_action_and_close_identity(self):
        close = {
            "action": "close",
            "connectionId": str(uuid4()),
            "sessionId": str(uuid4()),
            "processId": str(uuid4()),
        }
        for arguments in (
            {},
            {"action": "sessions"},
            {**close, "action": "list"},
            {key: value for key, value in close.items() if key != "processId"},
            {**close, "processId": "not-a-uuid"},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ValidationError):
                validate_arguments(
                    Request(
                        type="request",
                        id=str(uuid4()),
                        operation="integration.mcp.manage",
                        arguments=arguments,
                    )
                )

    def test_malformed_process_configuration_and_call_arguments_are_rejected(self):
        valid = {
            "connectionId": str(uuid4()),
            "sessionId": str(uuid4()),
            "executable": "/usr/bin/python3",
            "workingDirectory": "/tmp",
            "name": "counter",
            "arguments": {},
        }
        invalid = [
            {"connectionId": "not-a-uuid"},
            {"sessionId": "../chat"},
            {"args": ["before\x00after"]},
            {"args": "not-a-list"},
            {"args": ["x"] * 257},
            {"environment": {"INVALID=NAME": "value"}},
            {"environment": {"VALUE": "before\x00after"}},
            {"environment": {"VALUE": 1}},
            {"idleTimeoutSeconds": 0},
            {"idleTimeoutSeconds": 86401},
            {"idleTimeoutSeconds": True},
            {"arguments": []},
            {"name": ""},
            {"outputSchema": []},
            {"unexpected": True},
        ]
        for field in ("executable", "workingDirectory"):
            invalid.extend(
                {field: path}
                for path in (
                    "relative/path",
                    "/tmp/../escape",
                    "/tmp//double",
                    "/tmp/./dot",
                    "/tmp/trailing/",
                    "/tmp/bad\x00path",
                    "/tmp/bad\npath",
                    "C:\\unsafe",
                )
            )
        for changed in invalid:
            with self.subTest(changed=changed), self.assertRaises(ValidationError):
                validate_arguments(
                    Request(
                        type="request",
                        id=str(uuid4()),
                        operation="integration.mcp.call",
                        arguments={**valid, **changed},
                    )
                )
