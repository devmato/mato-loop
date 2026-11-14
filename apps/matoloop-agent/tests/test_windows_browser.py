import asyncio
import base64
import contextlib
import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from matoloop_agent.browser_contracts import (
    BrowserExecuteArguments,
    BrowserFileArguments,
    BrowserUploadArguments,
)
from matoloop_agent.browser_windows import WindowsBrowserRuntime
from matoloop_agent.browser_worker import BrowserWorker
from matoloop_agent.config import AgentConfig
from matoloop_agent.errors import AgentError


class WindowsBrowserTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_loopback_view_requires_runtime_secret(self):
        worker = BrowserWorker(Path("."))

        async def view(reader, writer):
            writer.write(b"frame")
            await writer.drain()

        worker.view_client = AsyncMock(side_effect=view)
        server = await asyncio.start_server(worker.authenticated_view, "127.0.0.1", 0)
        self.addCleanup(server.close)
        endpoint = {
            "port": server.sockets[0].getsockname()[1],
            "token": worker.view_token.hex(),
        }
        runtime = WindowsBrowserRuntime(
            AgentConfig.model_validate({"homeDirectory": Path.cwd()}), None
        )
        async with asyncio.timeout(5):
            with self.assertRaises((AgentError, asyncio.IncompleteReadError)):
                await runtime.view_connection({**endpoint, "token": bytes(32).hex()})
            worker.view_client.assert_not_awaited()
            reader, writer = await runtime.view_connection(endpoint)
            try:
                self.assertEqual(await reader.readexactly(5), b"frame")
            finally:
                writer.close()
                await writer.wait_closed()
        worker.view_client.assert_awaited_once()
        server.close()
        await server.wait_closed()

    def test_worker_environment_does_not_inherit_agent_credentials(self):
        runtime = WindowsBrowserRuntime(
            AgentConfig.model_validate({"homeDirectory": Path.cwd()}), None
        )
        network = {
            "HTTPS_PROXY": "http://proxy.invalid:8080",
            "NODE_EXTRA_CA_CERTS": r"C:\certificates\company.pem",
            "UV_NATIVE_TLS": "1",
            "PLAYWRIGHT_DOWNLOAD_HOST": "https://downloads.invalid",
        }
        with patch.dict(
            os.environ,
            {
                "SystemRoot": r"C:\Windows",
                "MATOLOOP_AGENT_PAIRING_TOKEN": "private",
                "PATH": "private",
                **network,
            },
        ):
            environment, options = runtime.environment()
            setup = runtime.setup_environment()
        self.assertNotIn("MATOLOOP_AGENT_PAIRING_TOKEN", environment)
        self.assertNotIn("private", environment.values())
        self.assertEqual(options, {})
        self.assertTrue(network.keys().isdisjoint(environment))
        self.assertEqual({key: setup[key] for key in network}, network)
        self.assertNotIn("MATOLOOP_AGENT_PAIRING_TOKEN", setup)

    async def test_windows_upload_names_cannot_escape_staging(self):
        worker = BrowserWorker(Path("."))
        with patch("matoloop_agent.browser_worker.sys.platform", "win32"):
            for name in ("C:escape.txt", "NUL", "file:stream", "trailing."):
                with self.subTest(name=name), self.assertRaises(AgentError) as caught:
                    await worker.file(
                        BrowserFileArguments(
                            browserId=str(uuid4()), action="begin", filename=name, size=1
                        )
                    )
                self.assertEqual(caught.exception.code, "browser.invalidTarget")

    @unittest.skipUnless(sys.platform == "win32", "native Windows process lifecycle")
    async def test_worker_crash_terminates_descendants(self):
        import ctypes
        from ctypes import wintypes

        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import subprocess, sys\n"
            "from matoloop_agent.browser_windows import contain_process_tree\n"
            "contain_process_tree()\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
            "print(child.pid, flush=True)\n"
            "sys.stdin.buffer.read(1)\n",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )
        runtime = WindowsBrowserRuntime(
            AgentConfig.model_validate({"homeDirectory": Path.cwd()}), None
        )
        self.addAsyncCleanup(runtime.stop_worker, process)
        pid = int(await asyncio.wait_for(process.stdout.readline(), 10))
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.TerminateProcess.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        child = kernel.OpenProcess(0x00100001, False, pid)  # SYNCHRONIZE | PROCESS_TERMINATE
        if not child:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            process.kill()
            await asyncio.wait_for(process.wait(), 5)
            self.assertEqual(await asyncio.to_thread(kernel.WaitForSingleObject, child, 5000), 0)
        finally:
            if kernel.WaitForSingleObject(child, 0) == 258:  # WAIT_TIMEOUT
                kernel.TerminateProcess(child, 1)
            kernel.CloseHandle(child)


@unittest.skipUnless(
    sys.platform == "win32" and os.environ.get("PLAYWRIGHT_BROWSERS_PATH"),
    "native Windows with explicitly installed Chromium",
)
class WindowsBrowserRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_browser_automation_streaming_files_and_isolation(self):
        from matoloop_agent.browser_contracts import BrowserOpenArguments, BrowserOwnerArguments
        from matoloop_agent.platform_windows import WindowsPlatform

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        config = AgentConfig.model_validate(
            {
                "homeDirectory": root,
                "backendUrl": "ws://localhost/api/execution-agents/connect",
                "allowInsecure": True,
                "agentId": str(uuid4()),
            }
        )
        config._data_directory = root
        services = WindowsPlatform().build_runtime(config, "unused")
        manager = services.browser
        manager.installation = Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"])
        self.addAsyncCleanup(manager.close)
        self.assertIn("integration.browser.execute", services.operations)
        owner_id = str(uuid4())
        await manager.dispatch("integration.browser.setup", BrowserOwnerArguments(ownerId=owner_id))
        await asyncio.wait_for(manager.setup_task, 120)
        self.assertEqual((await manager.status())["state"], "ready", manager.setup.buffer.decode())
        body = (
            b'<!doctype html><title>Debug app</title><input aria-label="Name">'
            b'<a href="/download" download="debug.txt">Download</a>'
        )

        async def http(reader, writer):
            try:
                request = await reader.readuntil(b"\r\n\r\n")
                payload = b"Windows download" if request.startswith(b"GET /download ") else body
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: "
                    + str(len(payload)).encode()
                    + b"\r\nConnection: close\r\n\r\n"
                    + payload
                )
                await writer.drain()
            finally:
                writer.close()

        server = await asyncio.start_server(http, "127.0.0.1", 0)
        self.addCleanup(server.close)
        url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/"
        identifiers = [str(uuid4()), str(uuid4())]
        tab_ids = []
        for browser_id in identifiers:
            await manager.dispatch(
                "integration.browser.open",
                BrowserOpenArguments(
                    ownerId=owner_id, browserId=browser_id, sessionId=str(uuid4())
                ),
            )
            navigation = await manager.dispatch(
                "integration.browser.execute",
                BrowserExecuteArguments(browserId=browser_id, action="navigate", url=url),
            )
            tab_ids.append(navigation["tabId"])
        first, second = [manager.runtimes[key] for key in identifiers]
        self.assertIsInstance(first, WindowsBrowserRuntime)
        script = await manager.dispatch(
            "integration.browser.execute",
            BrowserExecuteArguments(
                browserId=identifiers[0],
                tabId=tab_ids[0],
                action="run_code",
                code=(
                    "async def run(page):\n"
                    "    await page.get_by_label('Name').fill('Windows')\n"
                    "    await page.evaluate(\"localStorage.setItem('debug', 'first')\")\n"
                    "    await page.get_by_role('link', name='Download').click()\n"
                    "    return await page.get_by_label('Name').input_value()\n"
                ),
            ),
        )
        self.assertEqual(script["script"]["value"], "Windows")
        isolated = await manager.dispatch(
            "integration.browser.execute",
            BrowserExecuteArguments(
                browserId=identifiers[1],
                tabId=tab_ids[1],
                action="evaluate",
                function="() => localStorage.getItem('debug')",
            ),
        )
        self.assertIsNone(isolated["script"]["value"])
        upload = root / "upload.txt"
        upload.write_bytes(b"debug upload")
        staged = await first.stage_upload(
            BrowserUploadArguments(browserId=identifiers[0], path=upload.as_posix())
        )
        self.assertTrue(staged["complete"])
        async with asyncio.timeout(10):
            while (
                not (downloads := (await first.rpc("files"))["downloads"])
                or downloads[0]["status"] == "pending"
            ):
                await asyncio.sleep(0.05)
        self.assertEqual(downloads[0]["status"], "ready")
        downloaded = await first.rpc(
            "file",
            args={
                "browserId": identifiers[0],
                "action": "read",
                "fileId": downloads[0]["id"],
            },
        )
        self.assertEqual(base64.b64decode(downloaded["data"]), b"Windows download")
        image = await manager.dispatch(
            "integration.browser.execute",
            BrowserExecuteArguments(browserId=identifiers[0], action="screenshot"),
        )
        screenshot = await first.rpc(
            "file",
            args={
                "browserId": identifiers[0],
                "action": "read",
                "fileId": image["screenshot"]["fileId"],
            },
        )
        self.assertTrue(base64.b64decode(screenshot["data"]).startswith(b"\x89PNG"))
        reader, writer = await first.view_connection(await first.rpc("view"))
        try:
            async with asyncio.timeout(15):
                frame = {}
                while frame.get("type") != "frame":
                    size = struct.unpack(">I", await reader.readexactly(4))[0]
                    self.assertLessEqual(size, 4 * 1024 * 1024)
                    frame = json.loads(await reader.readexactly(size))
            self.assertTrue(frame["data"])
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()
        workers = [first.worker, second.worker]
        await manager.close()
        self.assertTrue(all(worker.returncode == 0 for worker in workers))
        self.assertFalse(list(root.rglob("matoloop-browser-*")))
        server.close()
        await server.wait_closed()
