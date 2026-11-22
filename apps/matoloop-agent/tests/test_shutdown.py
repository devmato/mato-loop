import asyncio
import contextlib
import json
import os
import signal
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import websockets

from matoloop_agent.browser import BrowserManager
from matoloop_agent.browser_contracts import BrowserOpenArguments
from matoloop_agent.browser_worker import BrowserWorker
from matoloop_agent.config import AgentConfig
from matoloop_agent.connection import MatoLoopAgent
from matoloop_agent.sleep_inhibitor import SleepInhibitor

# Run the actual CLI and signal handling, replacing only pairing and test-specific I/O.
CHILD = """
import asyncio, os, sys, threading
from pathlib import Path
from unittest.mock import patch
from matoloop_agent.__main__ import main
from matoloop_agent.browser import BrowserManager
from matoloop_agent.connection import MatoLoopAgent
from matoloop_agent.credentials import NotPairedError, PairingTemporaryError, ResolvedCredential

mode, config, backend, agent_id = sys.argv[1:]
async def credential(**kwargs):
    if mode in ('unpaired', 'pairing-retry'):
        print('READY', flush=True)
        raise (NotPairedError if mode == 'unpaired' else PairingTemporaryError)('test')
    return ResolvedCredential(backend, agent_id, 'synthetic-access-key')

async def disconnected(self, connected):
    print('READY', flush=True)
    if mode == 'stuck':
        asyncio.create_task(asyncio.to_thread(threading.Event().wait))
        await asyncio.sleep(0.05)
    raise OSError('synthetic disconnect')

async def close(self):
    print('CLOSING', flush=True)
    await asyncio.sleep(0.3)
    print('CLOSED', flush=True)

original_init = BrowserManager.__init__
def init(self, *args, **kwargs):
    original_init(self, *args, **kwargs)
    self.installation = Path(os.environ['PLAYWRIGHT_BROWSERS_PATH'])

original_timer = threading.Timer
def shutdown_timer(interval, *args, **kwargs):
    assert interval == 25
    return original_timer(2 if mode == 'stuck' else interval, *args, **kwargs)

sys.argv = ['matoloop-agent', 'run', '--config', config]
with patch('matoloop_agent.__main__.resolve_credential', credential):
    if mode == 'browser':
        with patch.object(BrowserManager, '__init__', init):
            main()
    else:
        with patch.object(MatoLoopAgent, '_connect_once', disconnected), \\
             patch.object(BrowserManager, 'close', close), \\
             patch('matoloop_agent.platform_posix.threading.Timer', shutdown_timer):
            main()
"""


class ShutdownTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="matoloop-shutdown-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.root.chmod(0o711)
        self.agent_id = str(uuid4())
        self.config = AgentConfig.model_validate(
            {
                "homeDirectory": self.root,
                "backendUrl": "ws://127.0.0.1:1/api/execution-agents/connect",
                "agentId": self.agent_id,
                "allowInsecure": True,
            }
        )
        self.config._data_directory = self.root

    async def child(self, mode):
        config = self.root / "config.json"
        config.write_text(self.config.model_dump_json(by_alias=True))
        environment = {
            key: value for key, value in os.environ.items() if not key.startswith("MATOLOOP_AGENT_")
        }
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            CHILD,
            mode,
            str(config),
            self.config.backend_url,
            self.agent_id,
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        async def cleanup():
            if process.returncode is None:
                process.kill()
            await process.wait()

        self.addAsyncCleanup(cleanup)
        return process

    async def test_sigterm_during_pairing_and_retry(self):
        for mode in ("unpaired", "pairing-retry", "retry"):
            with self.subTest(mode=mode):
                process = await self.child(mode)
                self.assertEqual(await asyncio.wait_for(process.stdout.readline(), 5), b"READY\n")
                process.send_signal(signal.SIGTERM)
                if mode == "retry":
                    self.assertEqual(await process.stdout.readline(), b"CLOSING\n")
                    process.send_signal(signal.SIGTERM)
                    await asyncio.sleep(0.05)
                    process.send_signal(signal.SIGTERM)
                    self.assertEqual(await process.stdout.readline(), b"CLOSED\n")
                await asyncio.wait_for(process.wait(), 3)
                self.assertEqual(process.returncode, 0, (await process.stderr.read()).decode())

    @unittest.skipUnless(os.environ.get("PLAYWRIGHT_BROWSERS_PATH"), "Bundled browser smoke test")
    async def test_old_profile_singleton_is_preserved_without_loading_it(self):
        owner = str(uuid4())
        manager = BrowserManager(self.config, None)
        manager.installation = Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"])
        root = manager.runtime_root(manager.storage(owner))
        profile = root / "profile"
        profile.mkdir(mode=0o700)
        if os.getuid() == 0:
            info = root.stat()
            os.chown(profile, info.st_uid, info.st_gid)
        lock = profile / "SingletonLock"
        lock.symlink_to("previous-container-987654")
        original = lock.lstat()
        try:
            await manager.dispatch(
                "integration.browser.open",
                BrowserOpenArguments(ownerId=owner, browserId=str(uuid4()), sessionId=str(uuid4())),
            )
            self.assertEqual(os.readlink(lock), "previous-container-987654")
            self.assertEqual(lock.lstat().st_ino, original.st_ino)
        finally:
            await manager.close()

    @unittest.skipUnless(os.environ.get("PLAYWRIGHT_BROWSERS_PATH"), "Bundled browser smoke test")
    async def test_closing_one_disposable_browser_keeps_the_other_alive(self):
        owner = str(uuid4())
        first, second = BrowserManager(self.config, None), BrowserManager(self.config, None)
        first.installation = second.installation = Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"])
        try:
            await first.dispatch(
                "integration.browser.open",
                BrowserOpenArguments(
                    ownerId=owner,
                    browserId=str(uuid4()),
                    sessionId=str(uuid4()),
                ),
            )
            await second.dispatch(
                "integration.browser.open",
                BrowserOpenArguments(ownerId=owner, browserId=str(uuid4()), sessionId=str(uuid4())),
            )
            await second.close()
            self.assertTrue((await next(iter(first.runtimes.values())).rpc("health"))["ok"])
        finally:
            await second.close()
            await first.close()

    async def test_shutdown_deadline_includes_executor_teardown(self):
        process = await self.child("stuck")
        self.assertEqual(await asyncio.wait_for(process.stdout.readline(), 5), b"READY\n")
        await asyncio.sleep(0.1)
        started = asyncio.get_running_loop().time()
        process.send_signal(signal.SIGTERM)
        self.assertEqual(await process.stdout.readline(), b"CLOSING\n")
        self.assertEqual(await process.stdout.readline(), b"CLOSED\n")
        await asyncio.wait_for(process.wait(), 5)
        self.assertEqual(process.returncode, 1)
        self.assertLess(asyncio.get_running_loop().time() - started, 5)

    async def test_outer_lifetime_closes_during_disconnected_retry_and_key_reload(self):
        for during_reload in (False, True):
            with self.subTest(during_reload=during_reload):
                agent = MatoLoopAgent(self.config, "synthetic")
                with (
                    patch.object(agent.browser, "close", new=AsyncMock()) as browser,
                    patch.object(agent.terminal, "close_all", new=AsyncMock()) as terminal,
                    patch.object(agent, "_connect_once", new=AsyncMock(side_effect=OSError)),
                    patch(
                        "matoloop_agent.connection.sleep",
                        side_effect=asyncio.CancelledError,
                    ),
                ):
                    loader = (
                        AsyncMock(side_effect=asyncio.CancelledError) if during_reload else None
                    )
                    with self.assertRaises(asyncio.CancelledError):
                        await agent.run_forever(loader)
                browser.assert_awaited_once()
                terminal.assert_awaited_once()

    async def test_manager_close_joins_once_and_signals_before_waiting_for_view(self):
        manager = BrowserManager(self.config, None)
        finished = asyncio.Event()
        process = SimpleNamespace(
            pid=123456, terminate=Mock(), wait=AsyncMock(side_effect=finished.wait)
        )
        manager.worker = process
        view_cancelled = asyncio.Event()

        async def view():
            try:
                await asyncio.Event().wait()
            finally:
                view_cancelled.set()
                await finished.wait()

        manager.view_tasks.add(asyncio.create_task(view()))
        await asyncio.sleep(0)
        with patch("matoloop_agent.browser.os.killpg") as kill:
            first = asyncio.create_task(manager.close())
            await view_cancelled.wait()
            process.terminate.assert_called_once()
            second = asyncio.create_task(manager.close())
            first.cancel()
            await asyncio.sleep(0)
            self.assertFalse(first.done())
            finished.set()
            results = await asyncio.wait_for(
                asyncio.gather(first, second, return_exceptions=True), 2
            )
            self.assertIsInstance(results[0], asyncio.CancelledError)
            self.assertIsNone(results[1])
            await manager.close()
            process.terminate.assert_called_once()
            kill.assert_called_once_with(process.pid, signal.SIGKILL)

    async def test_worker_closes_profile_before_cancelled_tasks_and_stops_driver(self):
        worker = BrowserWorker(self.root)
        released = asyncio.Event()

        async def request():
            try:
                await asyncio.Event().wait()
            finally:
                await released.wait()

        task = asyncio.create_task(request())
        worker.tasks.add(task)
        await asyncio.sleep(0)
        worker.context = SimpleNamespace(close=AsyncMock(side_effect=released.set))
        worker.playwright = SimpleNamespace(stop=AsyncMock())
        await asyncio.wait_for(worker.close(), 2)
        await worker.close()
        worker.context.close.assert_awaited_once()
        worker.playwright.stop.assert_awaited_once()
        self.assertTrue(task.done())

    async def test_worker_bounds_context_close_and_driver_stop(self):
        worker = BrowserWorker(self.root)
        worker.context = SimpleNamespace(close=AsyncMock(side_effect=asyncio.Event().wait))
        worker.playwright = SimpleNamespace(stop=AsyncMock(side_effect=asyncio.Event().wait))
        wait_for = asyncio.wait_for

        async def short_wait_for(awaitable, timeout):
            return await wait_for(awaitable, timeout / 100)

        started = asyncio.get_running_loop().time()
        with patch(
            "matoloop_agent.browser_worker.asyncio.wait_for", side_effect=short_wait_for
        ) as bounded_wait:
            await worker.close()
        self.assertLess(asyncio.get_running_loop().time() - started, 1)
        self.assertLess(sum(call.args[1] for call in bounded_wait.call_args_list), 10)
        worker.context.close.assert_awaited_once()
        worker.playwright.stop.assert_awaited_once()

    async def test_disconnect_closes_browser_before_waiting_for_cancelled_request(self):
        started, closed = asyncio.Event(), asyncio.Event()
        release_sleep = Mock()

        async def execute(*args, **kwargs):
            acquire_sleep.assert_called_once_with()
            release_sleep.assert_not_called()
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await closed.wait()

        async def backend(socket):
            await socket.recv()
            await socket.send(
                json.dumps({"type": "welcome", "protocolVersion": 1, "agentId": self.agent_id})
            )
            await socket.send(
                json.dumps(
                    {
                        "type": "request",
                        "id": str(uuid4()),
                        "operation": "cache.clear",
                        "arguments": {},
                    }
                )
            )
            await started.wait()
            await socket.close()

        async with websockets.serve(backend, "127.0.0.1", 0) as server:
            self.config.backend_url = (
                f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/api/execution-agents/connect"
            )
            agent = MatoLoopAgent(self.config, "synthetic")
            acquire_sleep = Mock(return_value=release_sleep)
            agent._sleep_inhibitor = SleepInhibitor(acquire_sleep)
            with (
                patch.object(agent, "_execute_operation", side_effect=execute),
                patch.object(agent.browser, "close", side_effect=closed.set) as close,
            ):
                try:
                    await asyncio.wait_for(agent._connect_once(asyncio.Event()), 3)
                    close.assert_awaited_once()
                    release_sleep.assert_called_once_with()
                finally:
                    closed.set()

    @unittest.skipUnless(os.environ.get("PLAYWRIGHT_BROWSERS_PATH"), "Bundled browser smoke test")
    async def test_real_entrypoint_sigterm_discards_browser_profile(self):
        cookies = []
        completed = asyncio.Queue()
        owner_id, browser_id = str(uuid4()), str(uuid4())

        async def http(reader, writer):
            try:
                headers = await reader.readuntil(b"\r\n\r\n")
                cookies.append(headers)
                body = b"<!doctype html><title>Shutdown fixture</title>synthetic"
                writer.write(
                    b"HTTP/1.1 200 OK\r\nConnection: close\r\n"
                    b"Set-Cookie: shutdown=retained; Max-Age=3600; Path=/\r\n"
                    + f"Content-Length: {len(body)}\r\n\r\n".encode()
                    + body
                )
                await writer.drain()
            except (asyncio.IncompleteReadError, ConnectionError):
                pass
            finally:
                writer.close()
                with contextlib.suppress(ConnectionError):
                    await writer.wait_closed()

        async def backend(socket):
            try:
                await socket.recv()
                await socket.send(
                    json.dumps({"type": "welcome", "protocolVersion": 1, "agentId": self.agent_id})
                )
                for operation, arguments in (
                    (
                        "open",
                        {
                            "ownerId": owner_id,
                            "browserId": browser_id,
                            "sessionId": str(uuid4()),
                        },
                    ),
                    (
                        "execute",
                        {
                            "browserId": browser_id,
                            "action": "navigate",
                            "url": url,
                        },
                    ),
                ):
                    await socket.send(
                        json.dumps(
                            {
                                "type": "request",
                                "id": str(uuid4()),
                                "operation": "integration.browser." + operation,
                                "arguments": arguments,
                            }
                        )
                    )
                    response = json.loads(await socket.recv())
                    self.assertTrue(response["ok"], response)
                await completed.put(None)
                await socket.wait_closed()
            except Exception as exc:
                await completed.put(exc)

        async with (
            await asyncio.start_server(http, "127.0.0.1", 0) as site,
            websockets.serve(backend, "127.0.0.1", 0) as server,
        ):
            url = f"http://127.0.0.1:{site.sockets[0].getsockname()[1]}/"
            self.config.backend_url = (
                f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/api/execution-agents/connect"
            )
            for _restart in range(2):
                cookies.clear()
                process = await self.child("browser")
                error = await asyncio.wait_for(completed.get(), 60)
                if error:
                    raise error
                self.assertNotIn(b"Cookie: shutdown=retained", cookies[0])
                process.send_signal(signal.SIGTERM)
                await asyncio.sleep(0.05)
                with contextlib.suppress(ProcessLookupError):
                    process.send_signal(signal.SIGTERM)
                await asyncio.wait_for(process.wait(), 20)
                self.assertEqual(process.returncode, 0, (await process.stderr.read()).decode())


if __name__ == "__main__":
    unittest.main()
