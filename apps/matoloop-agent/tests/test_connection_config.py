import asyncio
import hashlib
import io
import json
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import websockets
from pydantic import ValidationError

from matoloop_agent.config import AgentConfig
from matoloop_agent.connection import MatoLoopAgent, serialize_message
from matoloop_agent.filesystem import AgentError
from matoloop_agent.protocol import Error, Hello, Request, Response, SystemInfo

AGENT_ID = "2ea504cc-e3c6-4e0d-aa05-080328100fe8"
REQUEST_ID = "bbc46aa8-698d-4554-92c2-1cd5c6cc33a6"


def configuration(home: Path, **values) -> AgentConfig:
    config = AgentConfig.model_validate(
        {
            "homeDirectory": home,
            "backendUrl": "ws://localhost/api/execution-agents/connect",
            "agentId": AGENT_ID,
            "allowInsecure": True,
            **values,
        }
    )
    config._data_directory = home
    return config


class ConnectionConfigTests(unittest.IsolatedAsyncioTestCase):
    async def test_dispatch_preserves_safe_schema_constraints(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            agent = MatoLoopAgent(configuration(Path(temporary)), "secret")
            for operation, arguments, expected in (
                (
                    "filesystem.read",
                    {"path": str(Path(temporary) / "private"), "limit": 0},
                    {"field": "limit", "reason": "below_minimum", "minimum": 1},
                ),
                (
                    "filesystem.patch",
                    {"patch": "private", "expectedSha256": {str(i): "a" * 64 for i in range(1001)}},
                    {"field": "expectedSha256", "reason": "too_long", "maxProperties": 1000},
                ),
            ):
                with self.subTest(operation=operation):
                    request = Request(
                        type="request", id=REQUEST_ID, operation=operation, arguments=arguments
                    )
                    with self.assertRaises(AgentError) as rejected:
                        await agent.dispatch(request)
                    self.assertEqual(rejected.exception.params, expected)

    def test_config_parent_is_data_directory_and_removed_fields_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            path = base / "config.json"
            path.write_text(json.dumps({"homeDirectory": os.fspath(base)}))
            config = AgentConfig.load(path)
            self.assertEqual(config.data_dir, base)
            self.assertEqual(config.max_transfer_bytes, 1024 * 1024 * 1024)

            path.write_text(
                json.dumps(
                    {
                        "homeDirectory": os.fspath(base),
                        "processExecutables": {"python": "/usr/bin/python3"},
                    }
                )
            )
            self.assertEqual(AgentConfig.load(path).home_directory, base)

            for removed in ("name", "roots", "dataDir", "gitExecutable", "allowNetwork"):
                path.write_text(json.dumps({"homeDirectory": os.fspath(base), removed: "old"}))
                with self.subTest(removed=removed):
                    with self.assertRaises(ValidationError) as rejected:
                        AgentConfig.load(path)
                    self.assertEqual(rejected.exception.errors()[0]["type"], "extra_forbidden")

    async def test_hello_uses_protocol_one_public_agent_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            config_path = base / "config.json"
            received = {"hellos": []}

            async def handler(websocket) -> None:
                received["authorization"] = websocket.request.headers["Authorization"]
                received["hellos"].append(json.loads(await websocket.recv()))
                await websocket.send(
                    json.dumps({"type": "welcome", "protocolVersion": 1, "agentId": AGENT_ID})
                )
                await websocket.close()

            async with websockets.serve(handler, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]
                config_path.write_text(
                    json.dumps(
                        {
                            "backendUrl": f"ws://127.0.0.1:{port}/api/execution-agents/connect",
                            "agentId": AGENT_ID,
                            "homeDirectory": os.fspath(base),
                            "allowInsecure": True,
                        }
                    )
                )
                config = AgentConfig.load(config_path)
                agent = MatoLoopAgent(config, "agent-secret")
                snapshots = [
                    {
                        "operatingSystem": "Linux",
                        "distribution": f"Test Linux {number}",
                        "kernel": "6.12.0",
                        "architecture": "x86_64",
                        "cpuModel": f"Test CPU {number}",
                        "logicalCpuCount": number,
                        "totalMemoryBytes": number * 1024,
                        "sessionTemporaryDirectorySupported": True,
                    }
                    for number in (1, 2)
                ]
                with patch.object(
                    agent,
                    "system_info",
                    side_effect=[
                        {**info, "agentVersion": "1.2.3", "memory": {"availableBytes": 0}}
                        for info in snapshots
                    ],
                ) as collect:
                    await agent._connect_once(asyncio.Event())
                    await agent._connect_once(asyncio.Event())
                self.assertEqual([call.args for call in collect.call_args_list], [(None,), (None,)])

            for hello, info in zip(received["hellos"], snapshots, strict=True):
                self.assertEqual(hello["systemInfo"], info)
                self.assertIsInstance(Hello.model_validate(hello).system_info, SystemInfo)
            hello = received["hellos"][-1]
            self.assertEqual(received["authorization"], "Bearer agent-secret")
            self.assertEqual(hello["protocolVersion"], 1)
            self.assertEqual(hello["computerName"], socket.gethostname())
            self.assertEqual(hello["homeDirectory"], os.fspath(base))
            self.assertIn("agentVersion", hello)
            self.assertNotIn("name", hello)
            self.assertNotIn("roots", hello)
            self.assertNotIn("dataDir", hello)

    async def test_maintenance_results_are_delivered_before_hello_and_while_connected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            outcomes = home / "agent-state"
            outcomes.mkdir()
            after_welcome = "bc7f3dfd-d674-4781-b5fa-22ee0c6e5eb0"
            events = []
            delivered = asyncio.Event()

            def save_result(job_id):
                (outcomes / f"maintenance-{job_id}.json").write_text(
                    json.dumps(
                        {
                            "backendUrl": config.backend_url,
                            "agentId": AGENT_ID,
                            "jobId": job_id,
                            "status": "succeeded",
                        }
                    )
                )

            async def handler(websocket):
                await websocket.recv()
                events.append("hello")
                await websocket.send(
                    json.dumps({"type": "welcome", "protocolVersion": 1, "agentId": AGENT_ID})
                )
                save_result(after_welcome)
                await asyncio.wait_for(delivered.wait(), 2)
                await websocket.close()

            def complete(request):
                job_id = request.url.path.split("/")[-2]
                events.append(job_id)
                if job_id == after_welcome:
                    delivered.set()
                return httpx.Response(204)

            sleep = asyncio.sleep
            client = httpx.AsyncClient

            async def fast_sleep(delay, result=None):
                return await sleep(0 if delay == 30 else delay, result)

            async with websockets.serve(handler, "127.0.0.1", 0) as server:
                config = configuration(
                    home,
                    backendUrl=f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/api/execution-agents/connect",
                )
                agent = MatoLoopAgent(config, "key")
                save_result(REQUEST_ID)
                with (
                    patch("matoloop_agent.connection.sleep", new=fast_sleep),
                    patch(
                        "matoloop_agent.maintenance_results.httpx.AsyncClient",
                        side_effect=lambda **kwargs: client(
                            transport=httpx.MockTransport(complete), **kwargs
                        ),
                    ),
                ):
                    await asyncio.wait_for(agent._connect_once(asyncio.Event()), 5)
            self.assertEqual(
                (events, list(outcomes.iterdir())), ([REQUEST_ID, "hello", after_welcome], [])
            )

    def test_system_info_refreshes_linux_probes_and_preserves_live_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            agent = MatoLoopAgent(configuration(Path(temporary)), "secret")
            files = {
                "/etc/os-release": 'NAME=Test\nPRETTY_NAME="Test Linux 1.2"\n',
                "/proc/cpuinfo": "Processor: fallback\nHardware: board\nmodel name: Test CPU\n",
                "/proc/meminfo": "MemTotal: 8192 kB\nMemAvailable: 2048 kB\n",
            }
            with (
                patch("matoloop_agent.platform_linux.sys.platform", "linux"),
                patch(
                    "matoloop_agent.platform_linux.os.uname",
                    return_value=os.uname_result(("Linux", "host", "6.12", "version", "x86_64")),
                ),
                patch("matoloop_agent.platform_linux.os.cpu_count", return_value=8),
                patch(
                    "matoloop_agent.platform_linux.open",
                    side_effect=lambda path, **_: io.StringIO(files[path]),
                ),
            ):
                info = agent.hello().system_info
                self.assertEqual(
                    info.model_dump(by_alias=True),
                    {
                        "operatingSystem": "Linux",
                        "distribution": "Test Linux 1.2",
                        "kernel": "6.12",
                        "architecture": "x86_64",
                        "cpuModel": "Test CPU",
                        "logicalCpuCount": 8,
                        "totalMemoryBytes": 8192 * 1024,
                        "temporaryDirectory": "/tmp/mato-loop",
                        "sessionTemporaryDirectorySupported": True,
                    },
                )
                live = agent.system_info(temporary)
                self.assertEqual(
                    live["memory"], {"totalBytes": 8192 * 1024, "availableBytes": 2048 * 1024}
                )
                self.assertEqual(live["disk"]["path"], temporary)
                self.assertGreater(live["disk"]["totalBytes"], 0)
                self.assertIn("availableBytes", live["disk"])
                self.assertEqual(live["platform"], "linux")
                files["/etc/os-release"] = 'PRETTY_NAME="Test Linux 2.0"\n'
                files["/proc/cpuinfo"] = "Processor: fallback\nHardware: new board\n"
                files["/proc/meminfo"] = "malformed\nMemAvailable: bad\nMemTotal: 16384 kB\n"
                refreshed = agent.hello().system_info
                self.assertEqual(refreshed.distribution, "Test Linux 2.0")
                self.assertEqual(refreshed.cpu_model, "new board")
                self.assertEqual(refreshed.total_memory_bytes, 16384 * 1024)
                files["/proc/meminfo"] = "MemTotal: 32768 kB\n"
                files["/proc/cpuinfo"] = "processor: 0\nProcessor: 0\n"
                with patch("matoloop_agent.platform_linux.os.cpu_count", return_value=16):
                    refreshed = agent.hello().system_info
                self.assertEqual(refreshed.logical_cpu_count, 16)
                self.assertEqual(refreshed.total_memory_bytes, 32768 * 1024)
                self.assertIsNone(refreshed.cpu_model)
                files["/proc/meminfo"] += "MemAvailable: 0 kB\n"
                self.assertEqual(agent.system_info(None)["memory"]["availableBytes"], 0)

    def test_hello_tolerates_missing_and_malformed_system_probes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            agent = MatoLoopAgent(configuration(Path(temporary)), "secret")
            with (
                patch("matoloop_agent.platform_linux.sys.platform", "linux"),
                patch("matoloop_agent.platform_linux.os.uname", side_effect=OSError),
                patch("matoloop_agent.platform_linux.os.cpu_count", side_effect=OSError),
                patch("matoloop_agent.platform_linux.open", side_effect=FileNotFoundError),
            ):
                self.assertEqual(
                    json.loads(serialize_message(agent.hello()))["systemInfo"],
                    {
                        "temporaryDirectory": "/tmp/mato-loop",
                        "sessionTemporaryDirectorySupported": True,
                    },
                )
            files = {
                "/etc/os-release": 'PRETTY_NAME="unterminated\n',
                "/usr/lib/os-release": "PRETTY_NAME=\n",
                "/proc/cpuinfo": "processor: 0\nmodel name: \nProcessor: fallback\n",
                "/proc/meminfo": "MemTotal: bad kB\nMemAvailable: 0 kB\n",
            }
            with (
                patch("matoloop_agent.platform_linux.sys.platform", "linux"),
                patch(
                    "matoloop_agent.platform_linux.os.uname",
                    return_value=os.uname_result(("L" * 100, "host", "K" * 200, "version", " ")),
                ),
                patch(
                    "matoloop_agent.platform_linux.open",
                    side_effect=lambda path, **_: io.StringIO(files[path]),
                ),
            ):
                for count in (None, 0, -1, True, "8", 2147483648):
                    with patch("matoloop_agent.platform_linux.os.cpu_count", return_value=count):
                        info = agent.hello().system_info
                        self.assertIsNone(info.logical_cpu_count)
                        self.assertIsNone(info.total_memory_bytes)
                        self.assertIsNone(info.distribution)
                        self.assertIsNone(info.architecture)
                        self.assertEqual(info.operating_system, "L" * 80)
                        self.assertEqual(info.kernel, "K" * 160)
                        self.assertEqual(info.cpu_model, "fallback")
                files["/etc/os-release"] = 'PRETTY_NAME="' + "D" * 300 + '"\n'
                files["/proc/cpuinfo"] = "model name: " + "C" * 300
                for total in ("", "0 kB", "-1 kB", "9007199254740992 kB", "1 MB", "1.5 kB"):
                    files["/proc/meminfo"] = f"MemTotal: {total}\nMemAvailable: 0 kB\n"
                    info = agent.hello().system_info
                    self.assertIsNone(info.total_memory_bytes)
                    self.assertEqual(info.distribution, "D" * 255)
                    self.assertEqual(info.cpu_model, "C" * 255)

    async def test_mutation_cancellation_waits_for_truthful_thread_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            started = threading.Event()
            release = threading.Event()
            received = {}

            def delayed_write(*_args) -> dict:
                started.set()
                release.wait(2)
                return {
                    "path": os.fspath(root / "file.txt"),
                    "size": 1,
                    "sha256": "a" * 64,
                }

            async def handler(websocket) -> None:
                await websocket.recv()
                await websocket.send(
                    json.dumps({"type": "welcome", "protocolVersion": 1, "agentId": AGENT_ID})
                )
                await websocket.send(
                    json.dumps(
                        {
                            "type": "request",
                            "id": REQUEST_ID,
                            "operation": "filesystem.write",
                            "arguments": {
                                "path": os.fspath(root / "file.txt"),
                                "content": "x",
                            },
                        }
                    )
                )
                await asyncio.to_thread(started.wait, 1)
                await websocket.send(json.dumps({"type": "cancel", "id": REQUEST_ID}))
                try:
                    with self.assertRaises(TimeoutError):
                        await asyncio.wait_for(websocket.recv(), 0.05)
                finally:
                    release.set()
                received.update(json.loads(await websocket.recv()))
                await websocket.close()

            async with websockets.serve(handler, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]
                agent = MatoLoopAgent(
                    configuration(
                        root,
                        backendUrl=f"ws://127.0.0.1:{port}/api/execution-agents/connect",
                    ),
                    "secret",
                )
                agent.filesystem.write = delayed_write
                await agent._connect_once(asyncio.Event())

            self.assertTrue(received["ok"])
            self.assertEqual(received["result"]["sha256"], "a" * 64)

    async def test_filesystem_permission_error_retains_controlled_error_code(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            received = {}

            async def handler(websocket):
                await websocket.recv()
                await websocket.send(
                    json.dumps(
                        {
                            "type": "welcome",
                            "protocolVersion": 1,
                            "agentId": AGENT_ID,
                        }
                    )
                )
                await websocket.send(
                    json.dumps(
                        {
                            "type": "request",
                            "id": REQUEST_ID,
                            "operation": "filesystem.delete",
                            "arguments": {
                                "path": os.fspath(Path(temporary) / "models"),
                                "recursive": True,
                            },
                        }
                    )
                )
                received.update(json.loads(await websocket.recv()))
                await websocket.close()

            async with websockets.serve(handler, "127.0.0.1", 0) as server:
                agent = MatoLoopAgent(
                    configuration(
                        Path(temporary),
                        backendUrl=f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/api/execution-agents/connect",
                    ),
                    "secret",
                )
                with patch.object(
                    agent.filesystem, "delete", side_effect=PermissionError("private detail")
                ):
                    await asyncio.wait_for(agent._connect_once(asyncio.Event()), 5)

            self.assertEqual(received["error"], {"code": "path.accessDenied"})

    async def test_cancelled_mutation_reports_its_actual_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            agent = MatoLoopAgent(configuration(root), "secret")
            started = threading.Event()
            release = threading.Event()

            def delayed_failure(*_args) -> dict:
                started.set()
                release.wait(2)
                raise AgentError("file.changed", "changed")

            agent.filesystem.write = delayed_failure
            request = Request.model_validate(
                {
                    "type": "request",
                    "id": REQUEST_ID,
                    "operation": "filesystem.write",
                    "arguments": {"path": os.fspath(root / "file.txt"), "content": "x"},
                }
            )
            task = asyncio.create_task(agent.dispatch(request))
            await asyncio.to_thread(started.wait, 1)
            agent.operations.append("maintenance.start")
            with self.assertRaises(AgentError) as busy:
                await agent.dispatch(
                    Request(
                        type="request",
                        id=AGENT_ID,
                        operation="maintenance.start",
                        arguments={"action": "uninstall", "jobId": AGENT_ID},
                    )
                )
            self.assertEqual(busy.exception.code, "maintenance.busy")
            task.cancel()
            release.set()
            with self.assertRaisesRegex(AgentError, "changed"):
                await task

    async def test_maintenance_blocks_admission_during_launch_and_external_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            agent = MatoLoopAgent(configuration(Path(temporary)), "secret")
            agent.operations.append("maintenance.start")
            entered, release = asyncio.Event(), asyncio.Event()

            async def launch(*_args):
                entered.set()
                await release.wait()
                return {"started": True}

            read = Request(type="request", id=REQUEST_ID, operation="system.info", arguments={})
            update = Request(
                type="request",
                id=AGENT_ID,
                operation="maintenance.start",
                arguments={"action": "uninstall", "jobId": AGENT_ID},
            )
            with (
                patch.object(agent.platform, "start_maintenance", side_effect=launch),
                patch.object(agent.platform, "maintenance_running", return_value=False) as running,
            ):
                task = asyncio.create_task(agent.dispatch(update))
                await entered.wait()
                task.cancel()
                try:
                    with self.assertRaises(AgentError) as busy:
                        await agent.dispatch(read)
                    self.assertEqual(busy.exception.code, "maintenance.busy")
                finally:
                    running.return_value = True
                    release.set()
                    await task
                with self.assertRaises(AgentError) as busy:
                    await agent.dispatch(read)
                self.assertEqual(busy.exception.code, "maintenance.busy")
                running.return_value = False
                self.assertIn("agentVersion", await agent.dispatch(read))

    async def test_confirmed_update_stops_requests_but_finishes_atomic_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            agent = MatoLoopAgent(configuration(Path(temporary)), "secret")
            agent.operations.extend(["maintenance.update.interrupt", "terminal.read"])
            started, release = threading.Event(), threading.Event()
            interrupted = asyncio.Event()

            def write(*args):
                started.set()
                release.wait(5)
                return {"sha256": "a" * 64}

            async def read(*args):
                try:
                    await asyncio.Future()
                finally:
                    interrupted.set()

            async def launch(*args, before_start):
                await before_start()
                return {"started": True}

            with (
                patch.object(agent.filesystem, "write", side_effect=write),
                patch.object(agent.terminal, "read", side_effect=read),
                patch.object(
                    agent.terminal, "close_all", new_callable=AsyncMock
                ) as close_terminals,
                patch.object(agent.browser, "close", new_callable=AsyncMock) as close_browser,
                patch.object(agent.platform, "start_maintenance", side_effect=launch),
                patch.object(agent.platform, "maintenance_running", return_value=False),
            ):
                writing = asyncio.create_task(
                    agent.dispatch(
                        Request(
                            type="request",
                            id=REQUEST_ID,
                            operation="filesystem.write",
                            arguments={"path": f"{temporary}/file", "content": "x"},
                        )
                    )
                )
                await asyncio.to_thread(started.wait, 2)
                reading = asyncio.create_task(
                    agent.dispatch(
                        Request(
                            type="request",
                            id=AGENT_ID,
                            operation="terminal.read",
                            arguments={"terminalId": REQUEST_ID},
                        )
                    )
                )
                await asyncio.sleep(0)
                update = asyncio.create_task(
                    agent.dispatch(
                        Request(
                            type="request",
                            id=AGENT_ID,
                            operation="maintenance.update.interrupt",
                            arguments={"action": "update", "jobId": AGENT_ID, "revision": "a" * 40},
                        )
                    )
                )
                try:
                    await asyncio.wait_for(interrupted.wait(), 2)
                    self.assertFalse(update.done())
                    with self.assertRaises(AgentError) as busy:
                        await agent.dispatch(
                            Request(
                                type="request", id=AGENT_ID, operation="system.info", arguments={}
                            )
                        )
                    self.assertEqual(busy.exception.code, "maintenance.busy")
                finally:
                    release.set()
                    await asyncio.gather(reading, return_exceptions=True)
                self.assertEqual(await writing, {"sha256": "a" * 64})
                self.assertEqual(await update, {"started": True})
                close_browser.assert_awaited_once_with(stop_setup=True)
                close_terminals.assert_awaited_once()

    async def test_dispatches_absolute_filesystem_operations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            agent = MatoLoopAgent(configuration(root), "secret")
            path = os.fspath(root / "app.txt")
            self.assertIn("filesystem.upsert", agent.operations)
            await agent.dispatch(
                Request.model_validate(
                    {
                        "type": "request",
                        "id": REQUEST_ID,
                        "operation": "filesystem.upsert",
                        "arguments": {"path": path, "content": "old"},
                    }
                )
            )
            result = await agent.dispatch(
                Request.model_validate(
                    {
                        "type": "request",
                        "id": REQUEST_ID,
                        "operation": "filesystem.edit",
                        "arguments": {"path": path, "oldText": "old", "newText": "new"},
                    }
                )
            )
            self.assertEqual((root / "app.txt").read_text(), "new")
            self.assertEqual(result["sha256"], hashlib.sha256(b"new").hexdigest())

    async def test_dispatch_forwards_shell_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            agent = MatoLoopAgent(
                configuration(Path(temporary), shellExecutable=Path("/bin/sh")), "secret"
            )
            agent.process.exec = AsyncMock(return_value={})
            await agent.dispatch(
                Request.model_validate(
                    {
                        "type": "request",
                        "id": REQUEST_ID,
                        "operation": "shell.run",
                        "arguments": {
                            "currentDirectory": temporary,
                            "command": "true",
                            "environment": {"CUSTOM_VALUE": "explicit"},
                        },
                    }
                )
            )
            self.assertEqual(
                agent.process.exec.await_args.kwargs["environment"],
                {"CUSTOM_VALUE": "explicit"},
            )

    async def test_connection_retry_reloads_rotated_access_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            agent = MatoLoopAgent(configuration(Path(temporary)), "old-key")
            loader = AsyncMock(side_effect=["new-key", "rotated-key"])
            with (
                patch.object(
                    agent,
                    "_connect_once",
                    new=AsyncMock(side_effect=[RuntimeError(), asyncio.CancelledError()]),
                ),
                patch("matoloop_agent.connection.sleep", new=AsyncMock()),
                self.assertRaises(asyncio.CancelledError),
            ):
                await agent.run_forever(loader)
            self.assertEqual(agent._access_key, "rotated-key")
            self.assertEqual(agent.transfer._access_key, "rotated-key")

    async def test_connection_backoff_resets_after_successful_handshake(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            agent = MatoLoopAgent(configuration(Path(temporary)), "access-key")
            attempts = 0

            async def connect(connected: asyncio.Event) -> None:
                nonlocal attempts
                attempts += 1
                if attempts == 3:
                    connected.set()
                if attempts == 4:
                    raise asyncio.CancelledError
                raise RuntimeError

            with (
                patch.object(agent, "_connect_once", side_effect=connect),
                patch("matoloop_agent.connection.sleep", new=AsyncMock()) as sleep,
                self.assertRaises(asyncio.CancelledError),
            ):
                await agent.run_forever()

            self.assertEqual([call.args[0] for call in sleep.await_args_list], [1.0, 2.0, 1.0])

    def test_complete_response_serialization_is_bounded(self) -> None:
        response = Response(id=REQUEST_ID, ok=True, result={"content": "x" * 1024 * 1024})
        with self.assertRaises(AgentError):
            serialize_message(response)
        error = Response(
            id=REQUEST_ID,
            ok=False,
            error=Error(
                code="openscad.renderFailed",
                params={"diagnostics": "safe output", "diagnosticsTruncated": False},
            ),
        )
        self.assertEqual(
            json.loads(serialize_message(error))["error"]["code"], "openscad.renderFailed"
        )


if __name__ == "__main__":
    unittest.main()
