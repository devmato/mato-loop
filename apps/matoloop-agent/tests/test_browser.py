import asyncio
import base64
import contextlib
import json
import os
import struct
import tempfile
import unittest
from pathlib import Path
from time import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from playwright.async_api import Error as PlaywrightError
from pydantic import ValidationError

from matoloop_agent.browser import BrowserManager, BrowserRuntime, NativeBrowserRuntime
from matoloop_agent.browser_contracts import (
    BrowserControlArguments,
    BrowserExecuteArguments,
    BrowserFileArguments,
    BrowserIdArguments,
    BrowserInput,
    BrowserNavigationArguments,
    BrowserOpenArguments,
    BrowserOpenViewArguments,
    BrowserResizeArguments,
    BrowserSetupReadArguments,
    BrowserSetupResizeArguments,
    BrowserViewArguments,
)
from matoloop_agent.browser_storage import (
    TAB_RETENTION_SECONDS,
    read_tabs,
    tab_checkpoint,
    write_tabs,
)
from matoloop_agent.browser_worker import BrowserWorker
from matoloop_agent.config import AgentConfig
from matoloop_agent.connection import MatoLoopAgent
from matoloop_agent.errors import AgentError
from matoloop_agent.filesystem import Filesystem
from matoloop_agent.process import ProcessRunner
from matoloop_agent.sleep_inhibitor import SleepInhibitor
from matoloop_agent.terminal import TerminalManager


class BrowserTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.owner_id, self.browser_id = str(uuid4()), str(uuid4())
        config = AgentConfig.model_validate(
            {
                "homeDirectory": self.root,
                "backendUrl": "ws://localhost/api/execution-agents/connect",
                "allowInsecure": True,
                "agentId": str(uuid4()),
                "shellExecutable": Path("/bin/bash"),
            }
        )
        config._data_directory = self.root
        filesystem = Filesystem(config)
        self.manager = BrowserManager(
            config, TerminalManager(config, ProcessRunner(config, filesystem)), filesystem
        )
        self.addAsyncCleanup(self.manager.close)

    def test_expired_tab_checkpoint_is_pruned_without_a_worker(self):
        root = self.manager.runtime_root(self.manager.storage(self.owner_id))
        expired = tab_checkpoint(root, str(uuid4()))
        recent = tab_checkpoint(root, str(uuid4()))
        for path in (expired, recent):
            write_tabs(path, ["https://example.test/"], 0)
        interrupted = root / ".tabs-interrupted"
        interrupted.write_bytes(expired.read_bytes())
        old = time() - TAB_RETENTION_SECONDS - 1
        os.utime(expired, (old, old))
        os.utime(interrupted, (old, old))
        self.manager.prune_tab_checkpoints()
        self.assertEqual(list(root.glob("tabs-*.json")), [recent])
        self.assertFalse(interrupted.exists())

    def test_empty_live_browser_discards_the_checkpoint(self):
        worker = BrowserWorker(self.root)
        worker.checkpoint = tab_checkpoint(self.root, str(uuid4()))
        connected = Mock(return_value=False)
        worker.context = SimpleNamespace(browser=SimpleNamespace(is_connected=connected))
        write_tabs(worker.checkpoint, ["https://example.test/"], 0)
        worker.save_tabs()
        self.assertTrue(worker.checkpoint.exists())
        connected.return_value = True
        worker.save_tabs()
        self.assertFalse(worker.checkpoint.exists())

    async def test_failed_restore_keeps_the_original_address(self):
        worker = BrowserWorker(self.root)
        loaded = Mock(url="https://example.test/ok", goto=AsyncMock())
        failed = Mock(
            url="about:blank",
            goto=AsyncMock(side_effect=PlaywrightError("offline")),
            bring_to_front=AsyncMock(),
        )
        failed.is_closed.return_value = False
        worker.context = SimpleNamespace(
            pages=[loaded],
            new_page=AsyncMock(return_value=failed),
            browser=SimpleNamespace(is_connected=lambda: True),
        )
        worker.tabs = {"one": loaded, "two": failed}
        worker.checkpoint = tab_checkpoint(self.root, str(uuid4()))
        urls = [loaded.url, "https://example.test/offline"]
        await worker.restore_tabs(urls, 1)
        worker.save_tabs()
        self.assertEqual(read_tabs(worker.checkpoint), (urls, 1))

    def test_contract_and_native_view_availability(self):
        with patch.dict(os.environ, {"MATOLOOP_AGENT_CONTAINER": "0"}):
            self.assertIn(
                "integration.browser.view",
                MatoLoopAgent(self.manager.config, "test").hello().operations,
            )
        for invalid in ({"width": 319}, {"height": 2161}, {"width": True}, {"display": ":0"}):
            with self.assertRaises(ValidationError):
                BrowserResizeArguments.model_validate(
                    {"browserId": self.browser_id, "width": 1001, "height": 701, **invalid}
                )
        for invalid in (
            {"kind": "evaluate"},
            {"x": float("nan")},
            {"method": "Runtime.evaluate"},
            {"text": "ä" * 8193},
            {"modifiers": 16},
            {"modifiers": True},
        ):
            with self.assertRaises(ValidationError):
                BrowserInput.model_validate(
                    {
                        "kind": "text",
                        "tabId": self.browser_id,
                        "inputEpoch": self.browser_id,
                        **invalid,
                    }
                )
        for invalid in (
            {"action": "navigate", "url": "file:///etc/passwd"},
            {"action": "fill", "selector": "input"},
        ):
            with self.assertRaises(ValidationError):
                BrowserExecuteArguments(browserId=self.browser_id, **invalid)

    def test_binding_and_clean_environment(self):
        before = self.root.stat().st_mode
        root = self.manager.storage(self.owner_id)
        self.assertEqual(self.root.stat().st_mode, before)
        self.assertEqual(self.manager.storage(self.owner_id), root)
        with self.assertRaises(AgentError):
            self.manager.storage(str(uuid4()))
        (root / "binding").unlink()
        (root / "binding").symlink_to("/etc/passwd")
        with self.assertRaises(OSError):
            self.manager.storage(self.owner_id)
        user = SimpleNamespace(pw_name="matoloop-browser", pw_uid=10001, pw_gid=10001)
        with patch("os.getuid", return_value=0), patch("pwd.getpwnam", return_value=user):
            environment, options = self.manager.environment()
        self.assertNotIn("MATOLOOP_AGENT_PAIRING_TOKEN", environment)
        self.assertNotIn("MATOLOOP_AGENT_BACKEND_URL", environment)
        self.assertEqual((options["user"], options["extra_groups"]), (10001, []))

    async def test_opaque_staging_limits_and_symlink_rejection(self):
        worker = BrowserWorker(self.root)
        worker.live = self.root

        async def file(action, **kwargs):
            return await worker.file(
                BrowserFileArguments(browserId=self.browser_id, action=action, **kwargs)
            )

        entry = await file("begin", filename="test.txt", size=3)
        file_id = entry["fileId"]
        with self.assertRaises(AgentError):
            await file("finish", fileId=file_id)
        await file("write", fileId=file_id, data="YWJj")
        with self.assertRaises(AgentError):
            await file("write", fileId=file_id, offset=3, data="ZA==")
        self.assertTrue((await file("finish", fileId=file_id))["complete"])
        self.assertEqual(Path(worker.upload_payload(file_id)).name, "test.txt")
        with self.assertRaises(AgentError):
            await file("read", fileId=file_id)
        worker.files[file_id]["download"] = True
        self.assertEqual((await file("read", fileId=file_id, offset=1, maxBytes=1))["data"], "Yg==")
        (self.root / file_id).unlink()
        (self.root / file_id).symlink_to("/etc/passwd")
        with self.assertRaises(OSError):
            await file("read", fileId=file_id)
        await file("delete", fileId=file_id)
        self.assertFalse((self.root / file_id).is_symlink())

    def test_network_diagnostics_omit_authentication(self):
        worker = BrowserWorker(self.root)
        worker.network_diagnostic(
            "response", "https://user:secret@example.test/path?token=secret#key", 503
        )
        self.assertEqual(worker.diagnostics[-1], "response 503 https://example.test/path")
        for _ in range(60):
            worker.network_diagnostic("response", "https://example.test/" + "x" * 1000, 200)
        self.assertEqual(len(worker.diagnostics), 50)
        self.assertTrue(all(len(entry) <= 500 for entry in worker.diagnostics))

    async def test_manual_input_fences_stale_epochs_tabs_and_viewers(self):
        worker = BrowserWorker(self.root)
        page = Mock(
            is_closed=Mock(return_value=False), keyboard=SimpleNamespace(insert_text=AsyncMock())
        )
        worker.tabs[self.browser_id] = page
        worker.active_tab_id = self.browser_id
        viewer = Mock()
        worker.viewers.add(viewer)
        worker.release_input = AsyncMock()
        event = BrowserInput(
            kind="text", tabId=self.browser_id, inputEpoch=worker.input_epoch, text="hello"
        )
        await worker.input(event, viewer)
        page.keyboard.insert_text.assert_awaited_once_with("hello")
        page.keyboard.insert_text.reset_mock()
        await worker.control(False)
        await worker.input(event, viewer)
        await worker.control(True)
        await worker.input(event, viewer)
        await worker.input(event.model_copy(update={"input_epoch": worker.input_epoch}), Mock())
        await worker.input(
            event.model_copy(update={"input_epoch": worker.input_epoch, "tab_id": str(uuid4())}),
            viewer,
        )
        page.keyboard.insert_text.assert_not_awaited()

    async def test_cursor_sample_survives_pointer_movement_during_evaluation(self):
        worker = BrowserWorker(self.root)
        reader = asyncio.StreamReader()
        page = Mock()
        worker.tabs[self.browser_id] = page
        worker.active_tab_id = self.browser_id
        session = Mock(send=AsyncMock(), detach=AsyncMock())
        worker.context = Mock(new_cdp_session=AsyncMock(return_value=session))
        messages = []

        async def sample(_script, _point):
            worker.pointer = (page, worker.pointer[1] + 1, 10)
            return Mock(
                as_element=Mock(return_value=None),
                json_value=AsyncMock(return_value="pointer"),
                dispose=AsyncMock(),
            )

        page.main_frame = Mock(evaluate_handle=sample)

        def write(packet):
            messages.append(json.loads(packet[4:]))
            if messages[-1]["cursor"] == "pointer":
                reader.feed_eof()

        async def drain():
            worker.pointer = (page, 10, 10)

        async with asyncio.timeout(2):
            await worker.view_client(reader, Mock(write=write, drain=drain))
        self.assertEqual(messages[-1]["cursor"], "pointer")

    async def test_worker_response_errors_do_not_expose_payloads(self):
        runtime = BrowserRuntime(self.manager.config, self.manager.terminal)
        runtime.worker = SimpleNamespace(
            returncode=None,
            stdin=Mock(drain=AsyncMock()),
            stdout=SimpleNamespace(readline=AsyncMock(return_value=b'{"secret": "private"}\n')),
        )
        runtime.close = AsyncMock()
        with self.assertRaises(AgentError) as error:
            await runtime.rpc("health")
        self.assertEqual(error.exception.code, "browser.outcomeUnknown")
        self.assertNotIn("private", str(error.exception))

    async def test_opening_a_second_view_keeps_both_relays_connected(self):
        runtime = BrowserRuntime(self.manager.config, self.manager.terminal)
        self.addAsyncCleanup(runtime.close)
        runtime.worker = SimpleNamespace(returncode=None)
        runtime.browser_id = self.browser_id
        relays = []

        async def relay(_socket, _id, _token, ready):
            relays.append(asyncio.current_task())
            ready.set_result(True)
            await asyncio.Event().wait()

        try:
            with (
                patch.object(runtime, "rpc", AsyncMock(return_value={"socket": "/unused"})),
                patch.object(runtime, "relay", relay),
            ):
                for _ in range(2):
                    await runtime.dispatch(
                        "integration.browser.view",
                        BrowserViewArguments(
                            browserId=self.browser_id, relayId=str(uuid4()), relayToken="x" * 32
                        ),
                    )
            self.assertEqual([task.done() for task in relays], [False, False])
        finally:
            runtime.worker = None

    async def test_closing_a_tab_during_frame_delivery_keeps_streaming_the_next_tab(self):
        worker = BrowserWorker(self.root)
        page, next_page = Mock(is_closed=Mock(return_value=False)), Mock()
        worker.tabs = {"first": page, "next": next_page}
        worker.active_tab_id = "first"
        messages = []

        async def send(method, _params=None):
            if method == "Page.screencastFrameAck":
                raise PlaywrightError("Target page has been closed")

        session = Mock(send=send, detach=AsyncMock())
        session.on.side_effect = lambda _name, listener: listener({"data": "image", "sessionId": 1})
        worker.context = Mock(
            new_cdp_session=AsyncMock(
                side_effect=[session, Mock(send=AsyncMock(), detach=AsyncMock())]
            )
        )

        async def drain():
            if worker.active_tab_id == "first":
                page.is_closed.return_value = True
                worker.tabs.pop("first")
                worker.active_tab_id = "next"
            else:
                worker.viewers.clear()

        worker.viewers.add(
            Mock(write=lambda packet: messages.append(json.loads(packet[4:])), drain=drain)
        )
        async with asyncio.timeout(2):
            await worker.stream_views()
        self.assertEqual(messages[-1]["tabId"], "next")

    async def test_stream_acknowledges_skipped_images(self):
        worker = BrowserWorker(self.root)
        page = Mock()
        worker.tabs = {"active": page}
        worker.active_tab_id = "active"
        acknowledged = []

        async def send(method, params=None):
            if method == "Page.screencastFrameAck":
                acknowledged.append(params["sessionId"])

        def on(_name, listener):
            for frame_id in (1, 2):
                listener({"data": "image", "sessionId": frame_id})

        session = Mock(send=send, on=on, detach=AsyncMock())
        worker.context = Mock(new_cdp_session=AsyncMock(return_value=session))

        async def drain():
            worker.viewers.clear()

        worker.viewers.add(Mock(drain=drain))
        async with asyncio.timeout(2):
            await worker.stream_views()
        self.assertEqual(acknowledged, [1, 2])

    async def test_resize_releases_held_modifiers_before_fencing_old_keyup(self):
        worker = BrowserWorker(self.root)
        page = Mock(is_closed=Mock(return_value=False), set_viewport_size=AsyncMock())
        session = Mock(send=AsyncMock())
        worker.tabs[self.browser_id] = page
        worker.active_tab_id = self.browser_id
        worker.page_sessions[page] = asyncio.get_running_loop().create_future()
        worker.page_sessions[page].set_result(session)
        viewer = Mock()
        worker.viewers.add(viewer)
        event = BrowserInput(
            kind="key",
            event="down",
            key="Control",
            code="ControlLeft",
            keyCode=17,
            tabId=self.browser_id,
            inputEpoch=worker.input_epoch,
        )
        await worker.input(event, viewer)
        await worker.resize(1000, 700)
        await worker.input(event.model_copy(update={"event": "up"}), viewer)
        self.assertFalse(worker.held_keys)

    async def test_native_status_reports_missing_python_packages_without_starting_worker(self):
        self.manager.container = False
        with (
            patch("importlib.util.find_spec", return_value=None),
            patch.object(self.manager, "spawn", AsyncMock()) as spawn,
        ):
            status = await self.manager.status()
        self.assertEqual(status["state"], "missing")
        self.assertEqual(status["missing"], ["pillow", "playwright"])
        self.assertEqual(status["errorCode"], "browser.dependenciesMissing")
        spawn.assert_not_awaited()

    async def test_worker_response_failures_log_only_controlled_diagnostics(self):
        runtime = BrowserRuntime(self.manager.config, self.manager.terminal)
        for response, reason in (
            (b'{"private-token"', "invalidJson"),
            (b'{"ok":true,"result":{"private-token":true}}', "invalidResponse"),
            (OSError("private-token"), "transport"),
            (TimeoutError("private-token"), "timeout"),
        ):
            with self.subTest(reason=reason):
                readline = AsyncMock(
                    side_effect=response if isinstance(response, Exception) else None,
                    return_value=response if isinstance(response, bytes) else b"",
                )
                runtime.worker = SimpleNamespace(
                    returncode=None,
                    stdin=SimpleNamespace(write=Mock(), drain=AsyncMock()),
                    stdout=SimpleNamespace(readline=readline),
                )
                with (
                    patch.object(runtime, "close", new=AsyncMock()) as close,
                    self.assertLogs("matoloop_agent.browser", level="WARNING") as logs,
                ):
                    with self.assertRaises(AgentError) as error:
                        await runtime.rpc("execute", args={"action": "tabs"})
                self.assertEqual(error.exception.code, "browser.outcomeUnknown")
                close.assert_awaited_once()
                self.assertEqual(
                    logs.records[0].getMessage(),
                    f"Browser worker RPC failed: operation=execute reason={reason}",
                )
                self.assertNotIn("private-token", "\n".join(logs.output))

    async def test_resize_cancellation_drains_rpc_without_closing_browser(self):
        started, finish = asyncio.Event(), asyncio.Event()
        runtime = BrowserRuntime(self.manager.config, self.manager.terminal)

        async def rpc(*args, **kwargs):
            started.set()
            await finish.wait()
            return {"width": 1001, "height": 701}

        runtime.worker = SimpleNamespace(returncode=None)
        runtime.browser_id = self.browser_id
        with (
            patch.object(runtime, "rpc", side_effect=rpc),
            patch.object(runtime, "close", new=AsyncMock()) as close,
        ):
            pending = asyncio.create_task(
                runtime.dispatch(
                    "integration.browser.resize",
                    BrowserResizeArguments(browserId=self.browser_id, width=1001, height=701),
                )
            )
            await started.wait()
            pending.cancel()
            await asyncio.sleep(0)
            self.assertTrue(runtime.lock.locked())
            pending.cancel()
            await asyncio.sleep(0)
            self.assertTrue(runtime.lock.locked())
            finish.set()
            with self.assertRaises(asyncio.CancelledError):
                await pending
            close.assert_not_awaited()
            self.assertEqual(runtime.browser_id, self.browser_id)

    async def test_setup_keeps_sleep_assertion_until_process_exit(self):
        runtime = NativeBrowserRuntime(self.manager.config, None)
        release = Mock()
        inhibitor = SleepInhibitor(Mock(return_value=release))
        self.addCleanup(inhibitor.close)
        runtime.hold_setup_awake = inhibitor.hold
        waiting, finished = asyncio.Event(), asyncio.Event()

        async def wait():
            waiting.set()
            await finished.wait()

        process = SimpleNamespace(
            stdout=SimpleNamespace(read=AsyncMock(return_value=b"")),
            returncode=None,
            wait=wait,
        )
        with (
            patch("asyncio.create_subprocess_exec", return_value=process),
            patch.object(asyncio.get_running_loop(), "call_later") as schedule,
        ):
            try:
                with inhibitor.hold():
                    await runtime.launch_setup(self.root, {})
                    await waiting.wait()
                schedule.assert_not_called()
            finally:
                finished.set()
                await runtime.setup_task

    async def test_setup_replay_survives_terminal_and_browser_disconnect(self):
        entry = self.manager.terminal._spawn(
            str(uuid4()),
            [str(Path("/bin/bash").resolve()), "-c", "printf first; sleep .15; printf second"],
            str(self.root),
            24,
            80,
            {"PATH": "/usr/bin:/bin"},
        )
        self.manager.setup = entry
        self.manager.setup_task = asyncio.create_task(self.manager.setup_output(entry))
        await self.manager.terminal.close_all()
        await self.manager.close()
        await entry.wait_task
        await self.manager.setup_task
        args = BrowserSetupReadArguments(setupId=entry.terminal_id, offset=0)
        result = await self.manager.setup_io("setupread", args)
        self.assertEqual(base64.b64decode(result["data"]), b"firstsecond")
        self.assertEqual(result["offset"], 11)
        self.assertFalse(result["running"])
        self.assertEqual(await self.manager.setup_io("setupread", args), result)
        self.manager.setup_offset = 2_000_000
        entry.buffer[:] = b"last"
        result = await self.manager.setup_io("setupread", args)
        self.assertEqual(result["offset"], 2_000_000)
        self.assertEqual(base64.b64decode(result["data"]), b"last")
        resize = BrowserSetupResizeArguments(setupId=entry.terminal_id, rows=30, columns=100)
        self.assertEqual(await self.manager.setup_io("setupresize", resize), {"ok": True})

    async def test_maintenance_closes_independent_browser_setup(self):
        entry = self.manager.terminal._spawn(
            str(uuid4()),
            [str(Path("/bin/bash").resolve()), "-c", "sleep 60"],
            str(self.root),
            24,
            80,
            {"PATH": "/usr/bin:/bin"},
        )
        self.manager.setup = entry
        self.manager.setup_task = asyncio.create_task(self.manager.setup_output(entry))
        await self.manager.close(stop_setup=True)
        self.assertIsNotNone(entry.exit_code)
        with patch.object(self.manager.terminal, "_signal") as signal:
            await self.manager.close(stop_setup=True)
            signal.assert_not_called()

    @unittest.skipUnless(os.environ.get("PLAYWRIGHT_BROWSERS_PATH"), "Bundled Chromium runtime")
    async def test_isolated_runtimes_stream_and_input(self):
        self.root.chmod(0o711)
        body = (
            b"<!doctype html><title>Browser test</title><input aria-label='Name'>"
            b"<button onclick='window.answer=prompt(\"Answer?\")'>Prompt</button>"
            b"<textarea aria-label='Notes'></textarea>"
        )

        async def http(reader, writer):
            with contextlib.suppress(asyncio.IncompleteReadError):
                await reader.readuntil(b"\r\n\r\n")
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: "
                    + str(len(body)).encode()
                    + b"\r\nConnection: close\r\n\r\n"
                    + body
                )
                await writer.drain()
            writer.close()

        server = await asyncio.start_server(http, "127.0.0.1", 0)

        async def close_server():
            server.close()
            await self.manager.close()
            await server.wait_closed()

        self.addAsyncCleanup(close_server)
        url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/"
        ids = [self.browser_id, str(uuid4())]
        sessions = {browser_id: str(uuid4()) for browser_id in ids}
        old_profile = self.manager.runtime_root(self.manager.storage(self.owner_id)) / "profile"
        previous_profile = self.root / "previous-profile"
        previous_profile.mkdir()
        old_profile.symlink_to(previous_profile, target_is_directory=True)
        (old_profile / "sentinel").write_text("untouched")
        for browser_id in ids:
            await self.manager.dispatch(
                "integration.browser.open",
                BrowserOpenArguments(
                    ownerId=self.owner_id, browserId=browser_id, sessionId=sessions[browser_id]
                ),
            )

        tab_ids = {}

        async def execute(browser_id, action, **values):
            if action == "evaluate":
                values["tabId"] = tab_ids[browser_id]
            result = await self.manager.dispatch(
                "integration.browser.execute",
                BrowserExecuteArguments(browserId=browser_id, action=action, **values),
            )
            if result.get("tabId"):
                tab_ids[browser_id] = result["tabId"]
            return result

        for browser_id in ids:
            await execute(browser_id, "navigate", url=url)
        user_agent = await execute(ids[0], "evaluate", function="() => navigator.userAgent")
        self.assertRegex(user_agent["script"]["value"], r" Chrome/\d+\.0\.0\.0 ")
        await execute(
            ids[0],
            "evaluate",
            function="() => {document.cookie='owner=A'; "
            "localStorage.setItem('owner','A'); return true}",
        )
        other = await execute(
            ids[1], "evaluate", function="() => [document.cookie,localStorage.getItem('owner')]"
        )
        self.assertEqual(other["script"]["value"], ["", None])
        first = self.manager.runtimes[ids[0]]
        socket = (await first.rpc("view"))["socket"]
        reader, writer = await asyncio.open_unix_connection(socket)
        self.addCleanup(writer.close)

        async def frame(source=None):
            source = source or reader
            async with asyncio.timeout(10):
                while True:
                    size = struct.unpack(">I", await source.readexactly(4))[0]
                    value = json.loads(await source.readexactly(size))
                    if value["type"] == "frame":
                        return value

        image = await frame()
        self.assertTrue(base64.b64decode(image["data"]).startswith(b"\xff\xd8"))
        second_reader, second_writer = await asyncio.open_unix_connection(socket)
        self.addCleanup(second_writer.close)
        self.assertEqual((await frame(second_reader))["tabId"], image["tabId"])
        await frame()
        second_writer.close()
        await execute(
            ids[0], "evaluate", function="() => {document.body.style.background='red'; return true}"
        )
        image = await frame()
        await execute(
            ids[0],
            "evaluate",
            function="() => {document.querySelector('input').focus(); return true}",
        )
        payload = json.dumps(
            {
                "kind": "text",
                "tabId": image["tabId"],
                "inputEpoch": image["inputEpoch"],
                "text": "Grüße 🌍",
            }
        ).encode()
        writer.write(struct.pack(">I", len(payload)) + payload)
        await writer.drain()
        async with asyncio.timeout(5):
            while (
                await execute(
                    ids[0], "evaluate", function="() => document.querySelector('input').value"
                )
            )["script"]["value"] != "Grüße 🌍":
                await asyncio.sleep(0.05)
        button = (
            await execute(
                ids[0],
                "evaluate",
                function="() => document.querySelector('button').getBoundingClientRect().toJSON()",
            )
        )["script"]["value"]

        async def send_input(**event):
            data = json.dumps(
                {
                    "tabId": image["tabId"],
                    "inputEpoch": image["inputEpoch"],
                    **event,
                }
            ).encode()
            writer.write(struct.pack(">I", len(data)) + data)
            await writer.drain()

        await execute(
            ids[0],
            "evaluate",
            function="() => {document.querySelector('textarea').focus(); return true}",
        )
        for phase in ("down", "up"):
            await send_input(
                kind="key",
                event=phase,
                key="x",
                code="KeyX",
                keyCode=88,
                text="x" if phase == "down" else "",
            )
        for phase in ("down", "up"):
            await send_input(kind="key", event=phase, key="Enter", code="Enter", keyCode=13)
        async with asyncio.timeout(5):
            while (
                await execute(
                    ids[0], "evaluate", function="() => document.querySelector('textarea').value"
                )
            )["script"]["value"] != "x\n":
                await asyncio.sleep(0.05)

        await execute(
            ids[0],
            "evaluate",
            function="() => {document.body.style.minHeight='200vh'; window.scrollTo(0, 200);}",
        )
        writer.close()
        await writer.wait_closed()
        reader, writer = await asyncio.open_unix_connection(socket)
        self.addCleanup(writer.close)
        image = await frame()
        preserved = await execute(
            ids[0],
            "evaluate",
            function="() => [document.querySelector('textarea').value, window.scrollY]",
        )
        self.assertEqual(preserved["script"]["value"], ["x\n", 200])
        await send_input(kind="text", text="y")
        async with asyncio.timeout(5):
            while (
                await execute(
                    ids[0], "evaluate", function="() => document.querySelector('textarea').value"
                )
            )["script"]["value"] != "x\ny":
                await asyncio.sleep(0.05)
        await execute(ids[0], "evaluate", function="() => {window.scrollTo(0, 0);}")
        for phase in ("down", "up"):
            await send_input(kind="mouse", event=phase, x=button["x"] + 5, y=button["y"] + 5)
        async with asyncio.timeout(5):
            while True:
                size = struct.unpack(">I", await reader.readexactly(4))[0]
                state = json.loads(await reader.readexactly(size))
                if state.get("dialog"):
                    image = state
                    break
        await send_input(kind="dialog", accept=True, text="Confirmed")
        async with asyncio.timeout(5):
            while (await execute(ids[0], "evaluate", function="() => window.answer"))["script"][
                "value"
            ] != "Confirmed":
                await asyncio.sleep(0.05)
        await self.manager.dispatch(
            "integration.browser.control", BrowserControlArguments(browserId=ids[0], manual=False)
        )
        writer.write(struct.pack(">I", len(payload)) + payload)
        await writer.drain()
        value = await execute(
            ids[0], "evaluate", function="() => document.querySelector('input').value"
        )
        self.assertEqual(value["script"]["value"], "Grüße 🌍")
        await self.manager.dispatch(
            "integration.browser.close", BrowserIdArguments(browserId=ids[0])
        )
        with self.assertRaises(AgentError) as closed:
            await self.manager.dispatch(
                "integration.browser.openview",
                BrowserOpenViewArguments(
                    ownerId=self.owner_id,
                    browserId=str(uuid4()),
                    sessionId=sessions[ids[0]],
                    recoverOnly=True,
                ),
            )
        self.assertEqual(closed.exception.code, "browser.notConnected")
        self.assertIn(ids[1], self.manager.runtimes)
        self.assertEqual((old_profile / "sentinel").read_text(), "untouched")
        await self.manager.dispatch(
            "integration.browser.open",
            BrowserOpenArguments(ownerId=self.owner_id, browserId=ids[0], sessionId=str(uuid4())),
        )
        await execute(ids[0], "navigate", url=url)
        reset = await execute(ids[0], "evaluate", function="() => document.cookie")
        self.assertEqual(reset["script"]["value"], "")

        second_url = f"{url}second"
        await execute(ids[1], "newtab", url=second_url)
        # Losing the worker without running its cleanup still preserves the other chat's addresses.
        checkpoint = tab_checkpoint(old_profile.parent, sessions[ids[1]])
        async with asyncio.timeout(5):
            while read_tabs(checkpoint) != ([url, second_url], 1):
                await asyncio.sleep(0.05)
        lost = self.manager.runtimes[ids[1]]
        lost.worker.kill()
        await lost.worker.wait()
        recovered_id = str(uuid4())
        result = await self.manager.dispatch(
            "integration.browser.openview",
            BrowserOpenViewArguments(
                ownerId=self.owner_id,
                browserId=recovered_id,
                sessionId=sessions[ids[1]],
                recoverOnly=True,
                deferRestore=True,
            ),
        )
        self.assertTrue(result.get("restorePending"))
        before = await self.manager.dispatch(
            "integration.browser.navigation",
            BrowserNavigationArguments(browserId=recovered_id, action="state"),
        )
        self.assertEqual([tab["url"] for tab in before["tabs"]], ["about:blank"])
        result = await self.manager.dispatch(
            "integration.browser.control",
            BrowserControlArguments(browserId=recovered_id, manual=True, restoreTabs=True),
        )
        self.assertEqual(result.get("restoredTabs"), 2)
        restored = await self.manager.dispatch(
            "integration.browser.navigation",
            BrowserNavigationArguments(browserId=recovered_id, action="state"),
        )
        self.assertEqual([tab["url"] for tab in restored["tabs"]], [url, second_url])
        self.assertEqual(restored["activeTabId"], restored["tabs"][1]["id"])
