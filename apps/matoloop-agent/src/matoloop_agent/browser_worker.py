"""Browser worker; run_code has full worker-account OS and profile access.

Python imports are unrestricted. Output capture protects ordinary print framing,
not malicious descriptor writes such as os.write; this is not an OS sandbox.
"""

from __future__ import annotations

import ast
import asyncio
import base64
import contextlib
import hashlib
import hmac
import inspect
import json
import os
import signal
import struct
import subprocess
import sys
import tempfile
from collections import deque
from importlib.metadata import version
from io import BytesIO, TextIOBase
from pathlib import Path
from time import monotonic
from typing import Literal
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from PIL import Image
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright
from pydantic import ValidationError

from .browser_contracts import (
    MAX_FILE_BYTES,
    MAX_OBSERVATION_BYTES,
    BrowserActionResult,
    BrowserClipboardArguments,
    BrowserClipboardResult,
    BrowserCodeFeedback,
    BrowserControlArguments,
    BrowserExecuteArguments,
    BrowserFileArguments,
    BrowserFormField,
    BrowserInput,
    BrowserNavigationArguments,
    BrowserNavigationResult,
    BrowserResizeArguments,
    BrowserResizeResult,
    BrowserScriptResult,
    BrowserStatus,
    decode_chunk,
)
from .browser_storage import browser_file
from .errors import AgentError


def fail(code: str) -> AgentError:
    return AgentError(f"browser.{code}", "Browser operation failed")


class ScriptOutput(TextIOBase):
    def __init__(self):
        self.text = ""
        self.truncated = False

    def writable(self):
        return True

    def write(self, text):
        remaining = 32_768 - len(self.text)
        self.text += text[:remaining]
        self.truncated |= len(text) > remaining
        return len(text)


def launch_error_code(error: Exception) -> str:
    message = str(error).lower()
    if (
        "profile appears to be in use by another" in message
        or "opening in existing browser session." in message
        or ("processsingleton" in message and "/singletonlock: file exists" in message)
    ):
        return "profileInUse"
    if any(
        marker in message
        for marker in ("sandbox", "operation not permitted", "failed to move to new namespace")
    ):
        return "sandboxUnavailable"
    return "notReady"


class BrowserWorker:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.context = None
        self.playwright = None
        self.tasks: set[asyncio.Task] = set()
        self.tabs: dict[str, object] = {}
        self.files: dict[str, dict] = {}
        self.pending_choosers: dict[object, object] = {}
        self.diagnostics = deque(maxlen=50)
        self.server = None
        self.temporary = None
        self.manual = True
        self.input_lock = asyncio.Lock()
        self.input_epoch = str(uuid4())
        self.viewers = set()
        self.input_writer = None
        self.view_task = None
        self.view_refresh = False
        self.dialogs = {}
        self.held_keys = {}
        self.held_buttons = {}
        self.pointer = None
        self.document_ids: dict[object, str] = {}
        self.snapshot_key = os.urandom(32)
        self.geometry = {"width": 1280, "height": 800}
        self.page_sessions: dict[object, asyncio.Task] = {}
        self.loading_frames: dict[object, set[str]] = {}
        self.pending_requests: dict[object, set[object]] = {}
        self.request_activity: dict[object, float] = {}
        self.page_history: dict[object, dict] = {}
        self.active_tab_id = None
        self.startup_stage = "storage"
        self.execution_metrics = {}
        self.file_origin = None
        self.control_epoch = None
        self.view_token = os.urandom(32)
        self.view_endpoint = None

    def task(self, coroutine) -> None:
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def start(
        self,
        *,
        color_scheme: Literal["light", "dark"] = "light",
    ) -> dict:
        self.startup_stage = "storage"
        try:
            with tempfile.TemporaryFile(dir=self.root):
                pass
        except OSError:
            raise fail("notReady") from None
        # Keep the Unix socket path below Linux's 108-byte sockaddr_un limit.
        self.temporary = tempfile.TemporaryDirectory(
            prefix="matoloop-browser-", dir=self.root if sys.platform == "win32" else None
        )
        live = Path(self.temporary.name)
        self.live = live
        self.environment = {
            **os.environ,
            "HOME": str(live),
            "XDG_CONFIG_HOME": str(live / "config"),
            "XDG_CACHE_HOME": str(live / "cache"),
            "XDG_RUNTIME_DIR": str(live),
        }
        self.startup_stage = "chromium"
        self.playwright = await async_playwright().start()
        executable = self.playwright.chromium.executable_path
        if not Path(executable).is_file():
            return BrowserStatus(
                state="missing",
                missing=["chromium"],
                errorCode="browser.dependenciesMissing",
                playwrightVersion=version("playwright"),
            ).model_dump(by_alias=True)
        launch = {
            "headless": True,
            "chromium_sandbox": True,
            "env": self.environment,
            "accept_downloads": True,
            "color_scheme": color_scheme,
            "downloads_path": str(live / "downloads"),
            "args": ["--disable-gpu", "--hide-crash-restore-bubble"],
        }
        launch["viewport"] = self.geometry
        launch["channel"] = "chromium"
        try:
            if sys.platform != "win32":
                browser_major = (
                    subprocess.check_output(
                        [executable, "--version"],
                        env=self.environment,
                        stderr=subprocess.DEVNULL,
                        text=True,
                        timeout=5,
                    )
                    .split()[-1]
                    .split(".")[0]
                )
                # Some firewalls reject HeadlessChrome even for ordinary page reads.
                launch["user_agent"] = (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    f"(KHTML, like Gecko) Chrome/{browser_major}.0.0.0 Safari/537.36"
                )
            self.context = await self.playwright.chromium.launch_persistent_context(
                str(live / "profile"),
                **launch,
                timeout=20_000,
            )
        except Exception as exc:
            # Classify locally; raw Playwright errors can contain profile data and secrets.
            raise fail(launch_error_code(exc)) from None
        self.startup_stage = "page"
        self.context.set_default_timeout(8000)
        self.context.on(
            "requestfailed", lambda request: self.network_diagnostic("requestfailed", request.url)
        )
        self.context.on(
            "response",
            lambda response: self.network_diagnostic("response", response.url, response.status),
        )
        self.context.on("page", self.add_page)
        for page in self.context.pages:
            self.add_page(page)
        if not self.context.pages:
            raise fail("notReady")
        await self.context.pages[0].bring_to_front()
        await self.page_sessions[self.context.pages[0]]
        await self.context.pages[0].screenshot(type="png", timeout=8000)
        if sys.platform == "win32":
            self.server = await asyncio.start_server(self.authenticated_view, "127.0.0.1", 0)
            self.view_endpoint = {
                "port": self.server.sockets[0].getsockname()[1],
                "token": self.view_token.hex(),
            }
        else:
            self.server = await asyncio.start_unix_server(self.view_client, path=str(live / "view"))
            self.view_endpoint = {"socket": str(live / "view")}
        return BrowserStatus(
            state="ready",
            browserVersion=self.context.browser.version,
            playwrightVersion=version("playwright"),
        ).model_dump(by_alias=True)

    async def resize(self, width: int, height: int) -> dict:
        geometry = BrowserResizeResult(width=width, height=height).model_dump()
        async with self.input_lock:
            await self.release_input()
            for page in list(self.tabs.values()):
                await self.dispatch_input(page, page.set_viewport_size(geometry))
            self.geometry = geometry
            self.input_epoch = str(uuid4())
        return self.geometry

    def network_diagnostic(self, event: str, url: str, status: int | None = None) -> None:
        try:
            if len(url) > 8192:
                raise ValueError("URL exceeds diagnostic limit")
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"}:
                raise ValueError("Non-web URL")
            # Keep resource paths, not authentication userinfo, query tokens or fragments.
            target = urlunsplit(
                (parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], parsed.path, "", "")
            )
        except ValueError:
            target = "[URL omitted]"
        prefix = f"{event} {status}" if status is not None else event
        self.diagnostics.append(f"{prefix} {target}"[:500])

    async def control(
        self,
        manual: bool,
        *,
        origin: str | None = None,
        control_epoch: int | None = None,
    ) -> dict:
        if origin is not None:
            self.file_origin, self.control_epoch = origin, control_epoch
        async with self.input_lock:
            if not manual and self.dialogs:
                raise fail("busy")
            if manual != self.manual:
                self.manual = False
                self.input_epoch = str(uuid4())
                self.pending_choosers.clear()
                await self.release_input()
            self.manual = manual
        return {"ok": True}

    async def release_input(self) -> None:
        self.input_writer = None
        keys, self.held_keys = self.held_keys, {}
        buttons, self.held_buttons = self.held_buttons, {}
        for (page, _), params in keys.items():
            if not page.is_closed():
                await self.dispatch_input(
                    page,
                    (await self.page_sessions[page]).send(
                        "Input.dispatchKeyEvent", {"type": "keyUp", **params}
                    ),
                )
        for page, button in buttons:
            if not page.is_closed():
                await self.dispatch_input(page, page.mouse.up(button=button))

    async def reset_input(self) -> None:
        async with self.input_lock:
            await self.release_input()
            self.input_epoch = str(uuid4())

    async def input(self, event: BrowserInput, writer) -> None:
        async with self.input_lock:
            if (
                writer not in self.viewers
                or not self.manual
                or (event.kind != "release" and event.input_epoch != self.input_epoch)
                or (event.kind != "release" and event.tab_id != self.active_tab_id)
            ):
                return
            if event.kind == "release":
                if self.input_writer is writer:
                    await self.release_input()
                return
            page = self.tabs.get(event.tab_id)
            if page is None or page.is_closed():
                return
            if page in self.dialogs and event.kind != "dialog":
                return
            if event.kind == "dialog":
                dialog = self.dialogs.pop(page, None)
                if dialog is not None:
                    if event.accept:
                        await dialog.accept(event.text)
                    else:
                        await dialog.dismiss()
                return
            if self.input_writer is not writer:
                await self.release_input()
                self.input_writer = writer
            if event.kind == "text":
                await self.dispatch_input(page, page.keyboard.insert_text(event.text))
            elif event.kind == "key":
                params = {
                    "key": event.key,
                    "code": event.code,
                    "windowsVirtualKeyCode": event.key_code,
                }
                session = await self.page_sessions[page]
                if event.event == "down":
                    self.held_keys[(page, event.code)] = params
                else:
                    self.held_keys.pop((page, event.code), None)
                modifiers = sum(
                    {"Alt": 1, "Control": 2, "Meta": 4, "Shift": 8}.get(key, 0)
                    for key in {value["key"] for value in self.held_keys.values()}
                )
                await self.dispatch_input(
                    page,
                    session.send(
                        "Input.dispatchKeyEvent",
                        {
                            "type": "keyDown" if event.event == "down" else "keyUp",
                            "modifiers": modifiers,
                            **params,
                            **(
                                {"text": event.text or "\r", "unmodifiedText": event.text or "\r"}
                                if event.event == "down"
                                and (event.text or (event.key == "Enter" and not modifiers & 7))
                                else {}
                            ),
                        },
                    ),
                )
            elif event.kind == "mouse":
                self.pointer = (
                    (page, event.x, event.y)
                    if event.x < self.geometry["width"] and event.y < self.geometry["height"]
                    else None
                )
                if event.event == "down":
                    self.held_buttons[(page, event.button)] = True
                elif event.event == "up":
                    self.held_buttons.pop((page, event.button), None)
                button = event.button
                if event.event == "move":
                    button = next(
                        (button for candidate, button in self.held_buttons if candidate is page),
                        "none",
                    )
                elif event.event == "wheel":
                    button = "none"
                session = await self.page_sessions[page]
                await self.dispatch_input(
                    page,
                    session.send(
                        "Input.dispatchMouseEvent",
                        {
                            "type": {
                                "move": "mouseMoved",
                                "down": "mousePressed",
                                "up": "mouseReleased",
                                "wheel": "mouseWheel",
                            }[event.event],
                            "x": event.x,
                            "y": event.y,
                            "modifiers": event.modifiers,
                            "button": button,
                            "buttons": sum(
                                {"left": 1, "right": 2, "middle": 4}[button]
                                for candidate, button in self.held_buttons
                                if candidate is page
                            ),
                            "clickCount": event.click_count if event.event in {"up", "down"} else 0,
                            **(
                                {"deltaX": event.delta_x, "deltaY": event.delta_y}
                                if event.event == "wheel"
                                else {}
                            ),
                        },
                    ),
                )

    async def dispatch_input(self, page, command) -> None:
        # Chromium delays command acknowledgements until website dialogs close.
        opened = asyncio.Event()

        def listener(_dialog):
            opened.set()

        page.on("dialog", listener)
        if page in self.dialogs:
            opened.set()
        dispatched = asyncio.create_task(command)
        dialog = asyncio.create_task(opened.wait())
        try:
            done, _ = await asyncio.wait((dispatched, dialog), return_when=asyncio.FIRST_COMPLETED)
            if dispatched in done:
                dispatched.result()
        finally:
            page.remove_listener("dialog", listener)
            for task in (dispatched, dialog):
                task.cancel()
            await asyncio.gather(dispatched, dialog, return_exceptions=True)

    async def cursor_at(self, page, x: float, y: float) -> str:
        frame = page.main_frame
        while frame is not None:
            handle = await frame.evaluate_handle(
                """([x, y]) => {
                    let node = document.elementFromPoint(x, y);
                    while (node) {
                        const inner = node.shadowRoot?.elementFromPoint(x, y);
                        if (!inner || inner === node) break;
                        node = inner;
                    }
                    if (!node) return 'default';
                    if (node.tagName === 'IFRAME') return node;
                    const style = getComputedStyle(node);
                    const cursor = style.cursor.split(',').at(-1).trim().slice(0, 32);
                    if (cursor !== 'auto') return cursor;
                    if (style.userSelect === 'none') return 'default';
                    const textCursor = style.writingMode.startsWith('vertical')
                        ? 'vertical-text' : 'text';
                    if (node.isContentEditable) return textCursor;
                    const range = document.caretRangeFromPoint(x, y);
                    const text = range?.startContainer, offset = range?.startOffset;
                    if (text?.nodeType !== 3 || !node.contains(text)) return 'default';
                    range.setStart(text, Math.max(0, offset - 1));
                    range.setEnd(text, Math.min(text.length, offset + 1));
                    return [...range.getClientRects()].some(rect =>
                        x >= rect.left && x < rect.right &&
                        y >= rect.top && y < rect.bottom) ? textCursor : 'default';
                }""",
                [x, y],
            )
            try:
                element = handle.as_element()
                if element is None:
                    return await handle.json_value()
                x, y = await element.evaluate(
                    """(node, [x, y]) => {
                        const rect = node.getBoundingClientRect();
                        return [(x - rect.left) * node.offsetWidth / rect.width - node.clientLeft,
                            (y - rect.top) * node.offsetHeight / rect.height - node.clientTop];
                    }""",
                    [x, y],
                )
                frame = await element.content_frame()
            finally:
                await handle.dispose()
        return "default"

    async def stream_views(self) -> None:
        session = None
        current = None
        frames = deque(maxlen=1)
        ready = asyncio.Event()
        previous = epoch = latest = None
        cursor = "default"
        cursor_sampled = 0

        def frame(event):
            frames.append(event)
            ready.set()

        async def write(writer, packet):
            try:
                writer.write(packet)
                async with asyncio.timeout(3):
                    await writer.drain()
            except (ConnectionError, TimeoutError):
                self.viewers.discard(writer)
                writer.close()

        try:
            while self.viewers:
                page = self.tabs.get(self.active_tab_id)
                if page is not current or epoch != self.input_epoch:
                    if session is not None:
                        session.remove_listener("Page.screencastFrame", frame)
                        with contextlib.suppress(PlaywrightError):
                            await self.dispatch_input(current, session.send("Page.stopScreencast"))
                            await self.dispatch_input(current, session.detach())
                    frames.clear()
                    session = latest = None
                    current = page
                    epoch = self.input_epoch
                    self.pointer = None
                    cursor = "default"
                    if page is not None:
                        try:
                            session = await self.context.new_cdp_session(page)
                            session.on("Page.screencastFrame", frame)
                            await self.dispatch_input(
                                page,
                                session.send(
                                    "Page.startScreencast", {"format": "jpeg", "quality": 80}
                                ),
                            )
                        except PlaywrightError:
                            if not page.is_closed():
                                raise
                            continue
                dialog = self.dialogs.get(page)
                pointer = self.pointer
                if (
                    self.manual
                    and not dialog
                    and pointer
                    and pointer[0] is page
                    and monotonic() - cursor_sampled >= 0.1
                ):
                    cursor_sampled = monotonic()
                    with contextlib.suppress(PlaywrightError, TimeoutError):
                        async with asyncio.timeout(0.2):
                            cursor = await self.cursor_at(page, *pointer[1:])
                if page is not self.tabs.get(self.active_tab_id) or epoch != self.input_epoch:
                    continue
                if (
                    not self.manual
                    or page in self.dialogs
                    or not self.pointer
                    or self.pointer[0] is not page
                ):
                    cursor = "default"
                state = {
                    "tabId": self.active_tab_id,
                    "inputEpoch": self.input_epoch,
                    "manual": self.manual,
                    "width": self.geometry["width"],
                    "height": self.geometry["height"],
                    "cursor": cursor,
                    "dialog": {
                        "type": dialog.type,
                        "message": dialog.message[:2000],
                        "defaultValue": dialog.default_value[:2000],
                    }
                    if dialog
                    else None,
                }
                refresh, self.view_refresh = self.view_refresh, False
                event = frames.pop() if frames else None
                if event is not None:
                    latest = event["data"]
                if event is not None or state != previous or refresh:
                    image = latest if event is not None or refresh else None
                    payload = json.dumps(
                        {
                            **state,
                            "type": "frame" if image else "state",
                            **({"data": image} if image else {}),
                        }
                    ).encode()
                    if len(payload) > 4 * 1024 * 1024:
                        raise fail("fileTooLarge")
                    packet = struct.pack(">I", len(payload)) + payload
                    await asyncio.gather(*(write(writer, packet) for writer in list(self.viewers)))
                    previous = state
                    if event is not None:
                        try:
                            await session.send(
                                "Page.screencastFrameAck", {"sessionId": event["sessionId"]}
                            )
                        except PlaywrightError:
                            if not page.is_closed():
                                raise
                ready.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(ready.wait(), 0.1)
        finally:
            if session is not None:
                with contextlib.suppress(PlaywrightError):
                    await self.dispatch_input(current, session.send("Page.stopScreencast"))
                    await self.dispatch_input(current, session.detach())

    async def authenticated_view(self, reader, writer) -> None:
        task = asyncio.current_task()
        self.tasks.add(task)
        try:
            async with asyncio.timeout(5):
                supplied = await reader.readexactly(len(self.view_token))
                if not hmac.compare_digest(supplied, self.view_token):
                    return
                writer.write(b"\x01")
                await writer.drain()
            await self.view_client(reader, writer)
        except (TimeoutError, ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            self.tasks.discard(task)
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    async def view_client(self, reader, writer) -> None:
        async with self.input_lock:
            self.viewers.add(writer)
            self.view_refresh = True
            if self.view_task is None or self.view_task.done():
                self.view_task = asyncio.create_task(self.stream_views())
            sender = self.view_task
        task = asyncio.current_task()
        self.tasks.add(task)
        header = None
        try:
            while not sender.done():
                header = asyncio.create_task(reader.readexactly(4))
                done, _ = await asyncio.wait([header, sender], return_when=asyncio.FIRST_COMPLETED)
                if sender in done:
                    header.cancel()
                    await asyncio.gather(header, return_exceptions=True)
                    sender.result()
                    break
                size = struct.unpack(">I", header.result())[0]
                if not 0 < size <= 32768:
                    raise fail("invalidTarget")
                async with asyncio.timeout(5):
                    data = await reader.readexactly(size)
                    event = BrowserInput.model_validate_json(data)
                    await self.input(event, writer)
        except (
            asyncio.IncompleteReadError,
            ConnectionError,
            PlaywrightError,
            ValueError,
            AgentError,
            TimeoutError,
        ):
            pass
        finally:
            if header is not None:
                header.cancel()
                await asyncio.gather(header, return_exceptions=True)
            async with self.input_lock:
                self.viewers.discard(writer)
                if self.input_writer is writer:
                    with contextlib.suppress(PlaywrightError):
                        await self.release_input()
                if not self.viewers:
                    sender.cancel()
                    await asyncio.gather(sender, return_exceptions=True)
            writer.close()
            self.tasks.discard(task)

    async def clipboard(self, args: BrowserClipboardArguments) -> dict:
        if not self.manual:
            raise fail("busy")
        page = self.tabs.get(self.active_tab_id)
        if page is None:
            raise fail("invalidTarget")
        if args.action == "write":
            await page.keyboard.insert_text(args.text)
            return BrowserClipboardResult(text=args.text).model_dump()
        # Read the focused frame's selection, never the host's system clipboard.
        text = ""
        for frame in page.frames:
            selection = await frame.evaluate("""() => {
                if (!document.hasFocus()) return null;
                const node = document.activeElement;
                if (node && ['INPUT', 'TEXTAREA'].includes(node.tagName) &&
                    typeof node.selectionStart === 'number')
                    return node.type === 'password' ? '' :
                        node.value.slice(node.selectionStart, node.selectionEnd);
                return window.getSelection()?.toString() ?? '';
            }""")
            if selection is not None:
                text = selection
        return BrowserClipboardResult(text=text).model_dump()

    def add_page(self, page) -> None:
        if page in self.document_ids:
            return
        if len(self.tabs) >= 32:
            self.task(page.close())
            return
        tab_id = str(uuid4())
        self.tabs[tab_id] = page
        self.active_tab_id = tab_id
        self.task(self.reset_input())
        self.document_ids[page] = str(uuid4())
        self.loading_frames[page] = set()
        self.pending_requests[page] = set()

        def request_started(request):
            if page in self.pending_requests and request.resource_type in {
                "document",
                "xhr",
                "fetch",
            }:
                self.pending_requests[page].add(request)
                self.request_activity[page] = monotonic()

        def request_finished(request):
            pending = self.pending_requests.get(page, set())
            if request in pending:
                pending.discard(request)
                self.request_activity[page] = monotonic()

        page.on("request", request_started)
        page.on("requestfinished", request_finished)
        page.on("requestfailed", request_finished)
        observation = asyncio.create_task(self.observe_page(page))
        self.page_sessions[page] = observation
        self.tasks.add(observation)

        def observed(task):
            self.tasks.discard(task)
            if not task.cancelled():
                task.exception()  # A native page may close before anyone awaits its CDP setup.

        observation.add_done_callback(observed)

        def closed():
            self.tabs.pop(tab_id, None)
            if self.active_tab_id == tab_id:
                self.active_tab_id = next(reversed(self.tabs), None)
            self.pending_choosers.pop(page, None)
            self.document_ids.pop(page, None)
            self.loading_frames.pop(page, None)
            self.pending_requests.pop(page, None)
            self.request_activity.pop(page, None)
            self.page_history.pop(page, None)
            self.page_sessions.pop(page, None)
            self.dialogs.pop(page, None)

        def navigated(frame):
            self.pending_choosers.pop(page, None)
            if frame == page.main_frame:
                self.document_ids[page] = str(uuid4())
                self.task(self.reset_input())

        def dialog_opened(dialog):
            if self.manual:
                self.input_epoch = str(uuid4())
                self.dialogs[page] = dialog
            else:
                self.task(dialog.dismiss())

        page.on("close", closed)
        # Listening intercepts the native picker; the human supplies an opaque staged file.
        page.on("filechooser", lambda chooser: self.pending_choosers.__setitem__(page, chooser))
        page.on("framenavigated", navigated)
        page.on("dialog", dialog_opened)
        page.on(
            "download",
            lambda download: self.task(
                self.download(download, self.file_origin, self.control_epoch)
            ),
        )
        page.on(
            "console",
            lambda message: self.diagnostics.append(f"{message.type}: {message.text[:500]}"),
        )
        page.on("pageerror", lambda error: self.diagnostics.append(str(error)[:500]))

    async def observe_page(self, page):
        session = await self.context.new_cdp_session(page)
        frames = self.loading_frames[page]
        session.on("Page.frameStartedLoading", lambda event: frames.add(event["frameId"]))
        session.on("Page.frameStoppedLoading", lambda event: frames.discard(event["frameId"]))
        session.on("Page.frameDetached", lambda event: frames.discard(event["frameId"]))
        await session.send("Page.enable")
        return session

    async def navigation_state(self) -> dict:
        tabs = []
        for tab_id, page in list(self.tabs.items()):
            if page.is_closed():
                continue
            try:
                observation = self.page_sessions.get(page)
                if observation is None:
                    continue
                try:
                    async with asyncio.timeout(0.1):
                        session = await asyncio.shield(observation)
                except TimeoutError:
                    continue
                info = (await session.send("Target.getTargetInfo"))["targetInfo"]
                tabs.append(
                    {
                        "id": tab_id,
                        "url": info["url"][:4096],
                        "title": info["title"][:500],
                        "loading": bool(self.loading_frames.get(page)),
                    }
                )
            except PlaywrightError:
                # A page may close or replace its execution context during this read.
                continue
        back = forward = loading = False
        page = self.tabs.get(self.active_tab_id)
        if page is not None:
            session = await self.page_sessions[page]
            try:
                async with asyncio.timeout(0.1):
                    self.page_history[page] = await session.send("Page.getNavigationHistory")
            except (PlaywrightError, TimeoutError):
                pass
            history = self.page_history.get(page, {"currentIndex": -1, "entries": []})
            back = history["currentIndex"] > 0
            forward = history["currentIndex"] < len(history["entries"]) - 1
            loading = bool(self.loading_frames.get(self.tabs.get(self.active_tab_id)))
        return BrowserNavigationResult(
            tabs=tabs,
            activeTabId=self.active_tab_id,
            canGoBack=back,
            canGoForward=forward,
            loading=loading,
        ).model_dump(by_alias=True)

    async def navigation(self, args: BrowserNavigationArguments) -> dict:
        if not self.context:
            raise fail("notConnected")
        if args.action == "state":
            return await self.navigation_state()
        if not self.manual:
            raise fail("busy")
        page = self.tabs.get(args.tab_id)
        if args.tab_id and (page is None or page.is_closed()):
            raise fail("invalidTarget")
        if args.action == "new":
            if len(self.tabs) >= 32:
                raise fail("busy")
            page = await self.context.new_page()
            await self.page_sessions[page]
            await page.bring_to_front()
            self.active_tab_id = next(key for key, value in self.tabs.items() if value is page)
            if args.url:
                await self.navigation_command(
                    await self.page_sessions[page], "Page.navigate", {"url": args.url}
                )
        elif args.action == "select":
            await self.release_input()
            await page.bring_to_front()
            self.active_tab_id = args.tab_id
            self.input_epoch = str(uuid4())
        elif args.action == "close":
            if len(self.tabs) == 1:
                fresh = await self.context.new_page()
                await self.page_sessions[fresh]
            await page.close()
        else:
            session = await self.page_sessions[page]
            if args.action == "navigate":
                await self.navigation_command(session, "Page.navigate", {"url": args.url})
            elif args.action in {"back", "forward"}:
                await session.send("Page.stopLoading")
                history = await session.send("Page.getNavigationHistory")
                index = history["currentIndex"] + (-1 if args.action == "back" else 1)
                if 0 <= index < len(history["entries"]):
                    await self.navigation_command(
                        session,
                        "Page.navigateToHistoryEntry",
                        {"entryId": history["entries"][index]["id"]},
                    )
            elif args.action == "reload":
                await self.navigation_command(session, "Page.reload", {})
            elif args.action == "stop":
                await session.send("Page.stopLoading")
                self.loading_frames[page].clear()
        return await self.navigation_state()

    async def navigation_command(self, session, method: str, params: dict) -> None:
        async def navigate():
            try:
                async with asyncio.timeout(15):
                    result = await session.send(method, params)
                return not result.get("errorText")
            except (PlaywrightError, TimeoutError):
                return False

        # A slow response must not hold the host pipe and prevent state reads or Stop.
        pending = asyncio.create_task(navigate())
        self.tasks.add(pending)

        def finished(task):
            self.tasks.discard(task)
            if not task.cancelled() and not task.result():
                self.diagnostics.append("browser.actionFailed: navigation did not complete")

        pending.add_done_callback(finished)
        done, _ = await asyncio.wait([pending], timeout=0.1)
        if done and not pending.result():
            raise fail("actionFailed")

    async def download(self, download, origin: str | None, control_epoch: int | None) -> None:
        file_id = None
        try:
            if not self.make_file_room() or self.total_size() + MAX_FILE_BYTES > 2 * MAX_FILE_BYTES:
                await download.cancel()
                return
            file_id = str(uuid4())
            filename = download.suggested_filename.replace("\\", "_").replace("/", "_")
            filename = "".join(c for c in filename if ord(c) >= 32 and ord(c) != 127)[:255]
            if filename in {"", ".", ".."}:
                filename = "download"
            self.files[file_id] = {
                "id": file_id,
                "filename": filename,
                "size": MAX_FILE_BYTES,
                "ready": False,
                "download": True,
                "status": "pending",
                "origin": origin,
                "controlEpoch": control_epoch,
            }
            path = await download.path()
            if path is None:
                raise fail("actionFailed")
            size = path.stat().st_size
            if size > MAX_FILE_BYTES:
                raise fail("fileTooLarge")
            target = self.live / file_id
            await download.save_as(target)
            with browser_file(target) as handle:
                size = os.fstat(handle.fileno()).st_size
                if size > MAX_FILE_BYTES:
                    raise fail("fileTooLarge")
                digest = hashlib.file_digest(handle, "sha256").hexdigest()
            self.files[file_id].update(size=size, ready=True, status="ready", sha256=digest)
            await download.delete()
        except Exception as exc:
            if file_id is not None:
                (self.live / file_id).unlink(missing_ok=True)
                self.files[file_id].update(
                    size=None,
                    ready=False,
                    status="failed",
                    errorCode=exc.code if isinstance(exc, AgentError) else "browser.actionFailed",
                )
            with contextlib.suppress(Exception):
                await download.delete()
            self.diagnostics.append("browser.actionFailed: download was not retained")

    def total_size(self) -> int:
        return sum(
            entry["size"] for entry in self.files.values() if entry.get("status") != "failed"
        )

    def make_file_room(self) -> bool:
        while len(self.files) >= 32:
            failed = next(
                (
                    file_id
                    for file_id, entry in self.files.items()
                    if entry.get("status") == "failed"
                ),
                None,
            )
            if failed is None:
                return False
            del self.files[failed]
        return True

    def file_status(self) -> dict:
        fields = (
            "id",
            "filename",
            "status",
            "origin",
            "controlEpoch",
            "size",
            "sha256",
            "errorCode",
        )
        return {
            "downloads": [
                {
                    key: entry[key]
                    for key in fields
                    if key in entry and (key != "size" or entry["status"] == "ready")
                }
                for entry in self.files.values()
                if entry["download"]
            ]
        }

    def upload_payload(self, file_id: str) -> str:
        entry = self.files.get(file_id)
        if (
            not entry
            or not entry["ready"]
            or entry["download"]
            or entry.get("screenshot")
            or entry.get("snapshot")
        ):
            raise fail("invalidTarget")
        with browser_file(self.live / file_id) as handle:
            if os.fstat(handle.fileno()).st_size > MAX_FILE_BYTES:
                raise fail("fileTooLarge")
            # Playwright streams paths; a private hard link preserves the website filename.
            target = self.live / "uploads" / file_id / entry["filename"]
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if not target.exists():
                os.link(self.live / file_id, target, follow_symlinks=False)
            if target.is_symlink() or not os.path.samestat(
                os.fstat(handle.fileno()), target.stat(follow_symlinks=False)
            ):
                raise fail("invalidTarget")
        return str(target)

    async def file(self, args: BrowserFileArguments) -> dict:
        if args.action == "begin":
            if sys.platform == "win32":
                from .windows_paths import absolute

                try:
                    absolute("C:/" + args.filename)
                except ValueError:
                    raise fail("invalidTarget") from None
            if not self.make_file_room() or self.total_size() + args.size > 2 * MAX_FILE_BYTES:
                raise fail("fileTooLarge")
            file_id = str(uuid4())
            with browser_file(self.live / file_id, "wb", create=True):
                pass
            self.files[file_id] = {
                "id": file_id,
                "filename": args.filename,
                "size": args.size,
                "ready": False,
                "download": False,
            }
            return {"fileId": file_id, "offset": 0, "size": args.size, "complete": False}
        entry = self.files.get(args.file_id)
        if entry is None:
            raise fail("invalidTarget")
        if args.action == "choose":
            if not self.manual:
                raise fail("busy")
            payload = self.upload_payload(args.file_id)
            for page, chooser in list(self.pending_choosers.items()):
                if page.is_closed():
                    self.pending_choosers.pop(page, None)
                    continue
                if page is not self.tabs.get(self.active_tab_id):
                    continue
                if self.pending_choosers.get(page) is not chooser:
                    raise fail("invalidTarget")
                try:
                    await chooser.set_files(
                        payload, timeout=8000 + entry["size"] / (256 * 1024) * 1000
                    )
                    return {"ok": True}
                except PlaywrightError:
                    raise fail("invalidTarget") from None
                finally:
                    # Do not discard a newer chooser opened by a page's change handler.
                    if self.pending_choosers.get(page) is chooser:
                        self.pending_choosers.pop(page, None)
            raise fail("invalidTarget")
        path = self.live / args.file_id
        if args.action == "delete":
            path.unlink(missing_ok=True)
            if not entry["download"]:
                upload = self.live / "uploads" / args.file_id / entry["filename"]
                if upload.parent.exists():
                    upload.unlink(missing_ok=True)
                    upload.parent.rmdir()
            del self.files[args.file_id]
            return {"ok": True}
        with browser_file(path, "r+b") as handle:
            size = os.fstat(handle.fileno()).st_size
            if size > MAX_FILE_BYTES:
                raise fail("fileTooLarge")
            if args.action == "write":
                data = decode_chunk(args.data)
                if entry["ready"] or entry["download"] or args.offset != size:
                    raise fail("invalidTarget")
                if size + len(data) > entry["size"]:
                    raise fail("fileTooLarge")
                handle.seek(size)
                handle.write(data)
                size += len(data)
            elif args.action == "finish":
                if (
                    entry["download"]
                    or entry.get("screenshot")
                    or entry.get("snapshot")
                    or size != entry["size"]
                ):
                    raise fail("invalidTarget")
                entry["ready"] = True
            elif args.action == "read":
                if (
                    not (entry["download"] or entry.get("screenshot") or entry.get("snapshot"))
                    or not entry["ready"]
                    or args.offset > size
                    or size != entry["size"]
                ):
                    raise fail("invalidTarget")
                handle.seek(args.offset)
                data = handle.read(args.max_bytes)
                return {
                    "fileId": args.file_id,
                    "filename": entry["filename"],
                    "size": size,
                    "offset": args.offset + len(data),
                    "complete": args.offset + len(data) == size,
                    "data": base64.b64encode(data).decode("ascii"),
                }
        return {
            "fileId": args.file_id,
            "offset": size,
            "size": entry["size"],
            "complete": entry["ready"],
        }

    def discard_generated_files(self, kind: str) -> None:
        for file_id, entry in list(self.files.items()):
            if entry.get(kind):
                (self.live / file_id).unlink(missing_ok=True)
                del self.files[file_id]

    async def stage_generated_file(
        self, data: bytes, browser_id: str, filename: str, kind: str
    ) -> dict:
        if not data or len(data) > MAX_OBSERVATION_BYTES:
            raise fail("fileTooLarge")
        self.discard_generated_files(kind)
        staged = await self.file(
            BrowserFileArguments(
                browserId=browser_id, action="begin", filename=filename, size=len(data)
            )
        )
        file_id = staged["fileId"]
        self.files[file_id][kind] = True
        try:
            with browser_file(self.live / file_id, "wb") as handle:
                handle.write(data)
            self.files[file_id]["ready"] = True
        except BaseException:
            self.discard_generated_files(kind)
            raise
        return {"fileId": file_id, "size": len(data)}

    async def screenshot(self, page, browser_id: str) -> dict:
        self.discard_generated_files("screenshot")
        data = await page.screenshot(type="png", full_page=False, timeout=8000)
        with Image.open(BytesIO(data)) as image:
            width, height = image.size
            scale = min(
                1, 1568 / width, 1568 / height, (1.15 * 1024 * 1024 / (width * height)) ** 0.5
            )
            if scale < 1:
                with (
                    image.resize(
                        (max(1, int(width * scale)), max(1, int(height * scale))),
                        Image.Resampling.LANCZOS,
                    ) as resized,
                    BytesIO() as output,
                ):
                    resized.save(output, format="PNG")
                    data = output.getvalue()
        return await self.stage_generated_file(data, browser_id, "screenshot.png", "screenshot")

    async def execute(self, args: BrowserExecuteArguments) -> dict:
        self.execution_metrics = {}
        if not self.context:
            raise fail("notConnected")
        action = args.action
        page = self.tabs.get(args.tab_id or self.active_tab_id)
        if args.tab_id and page is None:
            raise fail("invalidTarget")
        text = screenshot = snapshot = result_data = batch = script = None
        completed, attempted, code_error = [], None, None

        async def step(line, operation, *values, **options):
            nonlocal attempted
            attempted = line
            value = await operation(*values, **options)
            completed.append(line)
            attempted = None
            return value

        try:
            if action == "newtab":
                if len(self.tabs) >= 32:
                    raise fail("busy")
                page = await step("page = await page.context.new_page()", self.context.new_page)
            if action not in {"tabs", "diagnostics"} and page is None:
                raise fail("invalidTarget")
            if page is not None and action not in {"tabs", "diagnostics"}:
                if action == "select":
                    await step("await page.bring_to_front()", page.bring_to_front)
                else:
                    await page.bring_to_front()
                self.active_tab_id = next(
                    (tab_id for tab_id, candidate in self.tabs.items() if candidate is page),
                    self.active_tab_id,
                )
            if action == "select":
                self.active_tab_id = args.tab_id
            if action in {"navigate", "newtab"} and args.url:
                await step(
                    f"await page.goto({args.url!r}, wait_until='domcontentloaded', timeout=15000)",
                    page.goto,
                    args.url,
                    wait_until="domcontentloaded",
                    timeout=15_000,
                )
            elif action == "closetab":
                if len(self.tabs) == 1:
                    await step("await page.context.new_page()", self.context.new_page)
                await step("await page.close()", page.close)
                page = self.tabs.get(self.active_tab_id)
            elif action == "inspect":
                if args.settle:
                    settle_outcome = await self._settle(page, strict=args.guard)
                text, result_data, snapshot = await self._inspect(
                    page,
                    browser_id=None if args.guard else args.browser_id,
                    guard=args.guard,
                    selector=args.selector,
                    depth=args.depth,
                )
                if args.settle and settle_outcome is not None:
                    result_data["settled"] = False
            elif action == "screenshot":
                screenshot = await self.screenshot(page, args.browser_id)
            elif action in {"click", "fill", "upload"}:
                # Playwright selectors are locators, never caller-controlled JavaScript.
                locator = page.locator(args.selector)
                target = f"page.locator({args.selector!r})"
                if action == "click":
                    await step(f"await {target}.click()", locator.click)
                elif action == "fill":
                    if args.slowly:
                        if not await locator.is_editable() or not await locator.evaluate("""node =>
                            node.isContentEditable || node.tagName === 'TEXTAREA' ||
                            (node.tagName === 'INPUT' &&
                                ['text','search','email','tel','url','password','number']
                                    .includes(node.type))"""):
                            raise fail("invalidTarget")
                        await step(
                            f"await {target}.press_sequentially({args.text!r})",
                            locator.press_sequentially,
                            args.text,
                        )
                    else:
                        await step(f"await {target}.fill({args.text!r})", locator.fill, args.text)
                    if args.submit:
                        await step(f"await {target}.press('Enter')", locator.press, "Enter")
                else:
                    await locator.set_input_files(
                        self.upload_payload(args.upload_id),
                        timeout=8000 + self.files[args.upload_id]["size"] / (256 * 1024) * 1000,
                    )
            elif action == "press":
                if args.selector:
                    await step(
                        f"await page.locator({args.selector!r}).press({args.key!r})",
                        page.locator(args.selector).press,
                        args.key,
                    )
                else:
                    await step(
                        f"await page.keyboard.press({args.key!r})", page.keyboard.press, args.key
                    )
            elif action == "fill_form":
                batch = await self._fill_form(page, args.fields)
                code_error = batch.get("errorCode")
                # Execution freezes ElementHandles; these are equivalent locator patterns only.
                count = batch["completed"] + (code_error == "browser.outcomeUnknown")
                for index, field in enumerate(args.fields[:count]):
                    target = f"page.locator({'aria-ref=' + field.ref!r})"
                    if field.type == "textbox":
                        call = f"fill({field.value!r}, timeout=1000)"
                    elif field.type == "combobox":
                        call = f"select_option(label={field.value!r}, timeout=1000)"
                    else:
                        call = f"set_checked({field.value == 'true'!r}, timeout=1000)"
                    line = f"await {target}.{call}"
                    if index < batch["completed"]:
                        completed.append(line)
                    else:
                        attempted = line
            elif action == "scroll":
                delta = -600 if args.direction == "up" else 600
                await step(f"await page.mouse.wheel(0, {delta})", page.mouse.wheel, 0, delta)
            elif action == "diagnostics":
                text = "\n".join(self.diagnostics)[:32_768]
            elif action in {"run_code", "evaluate"}:
                script = await self._script(page, args)
        except Exception:
            if attempted is not None:
                code_error = "browser.outcomeUnknown"
            elif completed and action == "newtab" and args.url:
                code_error = "browser.outcomeUnknown"
            elif completed:
                result_data = {"observationFailed": True}
            else:
                raise

        feedback = None
        if completed or attempted is not None or batch is not None:
            feedback = BrowserCodeFeedback(errorCode=code_error)
            # Omit whole repr-based lines, never cut a literal or exceed the combined budget.
            for line in completed:
                candidate = "\n".join(filter(None, (feedback.completed, line)))
                if len(candidate) <= 32_768:
                    feedback.completed = candidate
                else:
                    feedback.truncated = True
            if attempted is not None:
                if len(feedback.completed) + len(attempted) <= 32_768:
                    feedback.attempted = attempted
                else:
                    feedback.truncated = True
        observation_deadline = (
            asyncio.get_running_loop().time() + 8
            if batch is not None or script is not None or feedback is not None
            else None
        )
        if args.observe and action != "inspect" and page is not None:
            try:
                async with asyncio.timeout_at(observation_deadline):
                    settle_outcome = await self._settle(page, strict=False)
                    text, result_data, snapshot = await self._inspect(
                        page, browser_id=args.browser_id, guard=False
                    )
                    if settle_outcome is not None:
                        result_data["settled"] = False
            except Exception:
                # The mutation completed; a failed optional observation must not invite replay.
                text, result_data = None, {"observationFailed": True}
        tabs = []
        for tab_id, tab in list(self.tabs.items()):
            if not tab.is_closed():
                try:
                    async with asyncio.timeout_at(observation_deadline):
                        title = (await tab.title())[:500]
                except Exception:
                    if batch is None and script is None and feedback is None:
                        raise
                    title = ""
                tabs.append({"id": tab_id, "url": tab.url[:4096], "title": title})
        return BrowserActionResult(
            tabs=tabs,
            tabId=next((k for k, v in self.tabs.items() if v is page), None),
            url=page.url[:4096] if page else None,
            text=text,
            screenshot=screenshot,
            snapshot=snapshot,
            data=result_data,
            batch=batch,
            script=script,
            playwrightCode=feedback,
            downloads=[
                {key: entry[key] for key in ("id", "filename", "size")}
                for entry in self.files.values()
                if entry["download"] and entry["ready"]
            ],
        ).model_dump(by_alias=True)

    async def _script(self, page, args: BrowserExecuteArguments) -> BrowserScriptResult:
        compiled = None
        if args.action == "run_code":
            try:
                tree = ast.parse(args.code, filename="<browser-run-code>")
                runs = [
                    node
                    for node in tree.body
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name == "run"
                ]
                if len(runs) != 1 or not isinstance(runs[0], ast.AsyncFunctionDef):
                    raise ValueError("An async run(page) function is required")
                arguments = runs[0].args
                positional = arguments.posonlyargs + arguments.args
                if (
                    len(positional) != 1
                    or positional[0].arg != "page"
                    or arguments.vararg
                    or arguments.kwarg
                    or arguments.kwonlyargs
                    or arguments.defaults
                ):
                    raise ValueError("An async run(page) function is required")
                # Contract check only, not a sandbox. Module-level statements execute once.
                compiled = compile(tree, "<browser-run-code>", "exec")
            except (SyntaxError, ValueError, TypeError, RecursionError):
                raise fail("invalidTarget") from None
        output = ScriptOutput()
        result = BrowserScriptResult()
        script_deadline = None
        try:
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                async with asyncio.timeout(120) as script_deadline:
                    if compiled is not None:
                        namespace = {"__name__": "__browser_script__"}
                        exec(compiled, namespace)
                        if not inspect.iscoroutinefunction(namespace.get("run")):
                            raise ValueError("An async run(page) function is required")
                        value = await namespace["run"](page)
                    else:
                        target = page.locator(args.selector) if args.selector else page
                        # Validate before Playwright converts non-JSON JS values to null/objects.
                        evaluated = await target.evaluate(
                            "async element => { let value; try { value = await ("
                            + args.function
                            + """
                            )(element); } catch { return [false, 'runtimeError', null, null]; }
                            try {
                                let budget = 32768;
                                const check = (v, depth = 0) => {
                                    if (depth > 32) throw 'resultTooDeep';
                                    if (--budget < 0) throw 'resultTooLarge';
                                    if (v === null || typeof v === 'boolean') return;
                                    if (typeof v === 'string') {
                                        budget -= v.length;
                                        if (budget < 0) throw 'resultTooLarge';
                                        return;
                                    }
                                    if (typeof v === 'number' && Number.isFinite(v)) return;
                                    if (typeof v !== 'object' ||
                                        (!Array.isArray(v) &&
                                            Object.getPrototypeOf(v) !== Object.prototype &&
                                            Object.getPrototypeOf(v) !== null))
                                        throw 'resultTypeUnsupported';
                                    if (Array.isArray(v) && v.length > budget)
                                        throw 'resultTooLarge';
                                    for (const key of Reflect.ownKeys(v)) {
                                        if (typeof key !== 'string') throw 'resultTypeUnsupported';
                                        budget -= key.length;
                                        check(v[key], depth + 1);
                                    }
                                };
                                check(value);
                                const json = JSON.stringify(value);
                                if (new TextEncoder().encode(json).length > 32768)
                                    throw 'resultTooLarge';
                                return [true, null, null, JSON.parse(json)];
                            } catch (detail) {
                                return [false, 'invalidResult',
                                    ['resultTooLarge','resultTooDeep','resultTypeUnsupported']
                                        .includes(detail) ? detail : 'resultTypeUnsupported', null];
                            }
                        }"""
                        )
                        if evaluated[0] is not True:
                            result = BrowserScriptResult(
                                errorCode="browser.outcomeUnknown",
                                failureReason=evaluated[1],
                                failureDetail=evaluated[2],
                            )
                        value = evaluated[3]
                    if script_deadline.expired():
                        raise TimeoutError
                    if result.error_code is None:
                        result = BrowserScriptResult(value=value)
        except BaseException as exc:
            if asyncio.current_task().cancelling():
                # External RPC cancellation must still tear down the worker, not drain/retry it.
                raise
            detail = None
            if isinstance(exc, PlaywrightTimeoutError):
                reason = "playwrightTimeout"
            elif (
                isinstance(exc, TimeoutError)
                and script_deadline is not None
                and script_deadline.expired()
            ):
                reason = "scriptTimeout"
            elif isinstance(exc, ValidationError):
                reason = "invalidResult"
                messages = " ".join(error["msg"] for error in exc.errors())
                detail = (
                    "resultTooDeep"
                    if "depth" in messages
                    else "resultTypeUnsupported"
                    if "finite JSON" in messages or "string keys" in messages
                    else "resultTooLarge"
                )
            elif isinstance(exc, PlaywrightError):
                reason = "targetUnavailable"
            else:
                reason = "runtimeError"
                if isinstance(exc, TypeError):
                    detail = "typeError"
                elif isinstance(exc, AttributeError):
                    detail = "attributeError"
            line = None
            traceback = exc.__traceback__
            while traceback is not None:
                if traceback.tb_frame.f_code.co_filename == "<browser-run-code>":
                    line = traceback.tb_lineno
                traceback = traceback.tb_next
            result = BrowserScriptResult(
                errorCode="browser.outcomeUnknown",
                failureReason=reason,
                failureDetail=detail,
                line=line,
            )
        result.output, result.output_truncated = output.text, output.truncated
        return result

    async def _settle(self, page, *, strict: bool = True) -> str | None:
        # Observe in an isolated world: page scripts cannot replace the native APIs.
        # Each observer self-destructs even if the pipe, task, or document disappears.
        started = monotonic()
        settle_seconds = 1.8 if strict else 0.6
        deadline = started + settle_seconds
        session = None
        group = "matoloop-settle-" + uuid4().hex
        observers = []
        try:
            async with asyncio.timeout_at(asyncio.get_running_loop().time() + settle_seconds):
                session = await asyncio.shield(self.page_sessions[page])
                while self.pending_requests.get(page) or self.loading_frames.get(page):
                    await asyncio.sleep(0.025)
                tree = await session.send("Page.getFrameTree")
                frames = [tree["frameTree"]]
                for frame in frames:
                    frames.extend(frame.get("childFrames", []))
                    world = await session.send(
                        "Page.createIsolatedWorld",
                        {"frameId": frame["frame"]["id"], "worldName": "matoloop-settle"},
                    )
                    remaining = max(1, int((deadline - monotonic()) * 1000))
                    result = await session.send(
                        "Runtime.evaluate",
                        {
                            "contextId": world["executionContextId"],
                            "objectGroup": group,
                            "expression": """(() => {
                                let last = performance.now(), expired = false;
                                const observer = new MutationObserver(() => {
                                    last = performance.now();
                                });
                                observer.observe(document, {subtree:true, childList:true,
                                    attributes:true, characterData:true});
                                const close = () => {
                                    expired = true; observer.disconnect(); clearTimeout(limit);
                                };
                                const limit = setTimeout(close, """
                            + str(remaining)
                            + """);
                                return {close, ready:() => {
                                    const busy = document.readyState !== 'complete' ||
                                        document.getAnimations().some(animation =>
                                            (animation.playState === 'running' || animation.pending)
                                            && Number.isFinite(
                                                animation.effect?.getComputedTiming().endTime));
                                    if (busy) last = performance.now();
                                    return !expired && performance.now() - last >= 150;
                                }};
                            })()""",
                        },
                    )
                    observers.append(result["result"]["objectId"])
                while True:
                    states = await asyncio.gather(
                        *(
                            session.send(
                                "Runtime.callFunctionOn",
                                {
                                    "objectId": observer,
                                    "functionDeclaration": "function(){return this.ready()}",
                                    "returnByValue": True,
                                },
                            )
                            for observer in observers
                        )
                    )
                    if (
                        all(state.get("result", {}).get("value") is True for state in states)
                        and monotonic() - self.request_activity.get(page, 0) >= 0.15
                        and not self.pending_requests.get(page)
                        and not self.loading_frames.get(page)
                    ):
                        self.execution_metrics.update(
                            settleMs=round((monotonic() - started) * 1000, 3),
                            settleOutcome="settled",
                        )
                        return None
                    await asyncio.sleep(0.025)
        except (TimeoutError, PlaywrightError, KeyError):
            pass
        finally:
            # The timer is the fallback for a lost renderer or cancellation during registration.
            with contextlib.suppress(Exception):
                async with asyncio.timeout(0.1):
                    for observer in observers:
                        await session.send(
                            "Runtime.callFunctionOn",
                            {
                                "objectId": observer,
                                "functionDeclaration": "function(){this.close()}",
                            },
                        )
                        await session.send("Runtime.releaseObject", {"objectId": observer})
            if session is not None:
                with contextlib.suppress(Exception):
                    async with asyncio.timeout(0.1):
                        await session.send("Runtime.releaseObjectGroup", {"objectGroup": group})
        outcome = (
            "network"
            if self.pending_requests.get(page)
            else "loading"
            if self.loading_frames.get(page)
            else "pageActivity"
            if observers
            else "renderer"
        )
        self.execution_metrics.update(
            settleMs=round((monotonic() - started) * 1000, 3),
            settleOutcome=outcome,
        )
        if strict:
            raise fail("observationUnstable")
        return outcome

    async def _fill_form(self, page, fields: list[BrowserFormField]) -> dict:
        handles, guards = [], []
        completed, dispatched = 0, False
        document_id = self.document_ids.get(page)
        try:
            async with asyncio.timeout(25):
                for field in fields:
                    targets = await page.locator("aria-ref=" + field.ref).element_handles()
                    handles.extend(targets)
                    if len(targets) != 1:
                        raise fail("invalidTarget")
                    guard = await targets[0].evaluate_handle(
                        r"""(node, field) => {
                        const doc = node.ownerDocument, form = node.form;
                        const state = () => JSON.stringify({
                            tag:node.tagName, type:node.type, name:node.name, id:node.id,
                            role:node.getAttribute('role'), aria:node.getAttribute('aria-label'),
                            labelledby:node.getAttribute('aria-labelledby'),
                            labelledText:(node.getAttribute('aria-labelledby') || '').split(/\s+/)
                                .map(id => doc.getElementById(id)?.textContent),
                            attributes:Array.from(node.attributes, attr => [attr.name, attr.value])
                                .filter(([name]) => !['class','style','tabindex'].includes(name)),
                            labels:Array.from(node.labels || [], label => label.textContent),
                            value:node.isContentEditable ? node.textContent : node.value,
                            checked:node.checked, disabled:node.disabled, readOnly:node.readOnly,
                            editable:node.isContentEditable,
                            action:form?.action, method:form?.method, target:form?.target,
                            enctype:form?.enctype,
                            options:node.options && Array.from(node.options, option =>
                                [option.value, option.label,
                                    option.disabled || option.parentElement.disabled,
                                    option.selected])
                        });
                        const textbox = node.tagName === 'TEXTAREA' || node.isContentEditable ||
                            (node.tagName === 'INPUT' &&
                                ['text','search','email','tel','url','password','number']
                                    .includes(node.type));
                        const matches = node.tagName === 'SELECT' ? Array.from(node.options)
                            .filter(option => option.label === field.value) : [];
                        const valid = field.type === 'textbox' ? textbox :
                            field.type === 'combobox' ? node.tagName === 'SELECT' &&
                                !node.multiple && matches.length === 1 && !matches[0].disabled &&
                                    !matches[0].parentElement.disabled :
                                (node.tagName === 'INPUT' && node.type === field.type) ||
                                    (node.getAttribute('role') === field.type &&
                                        ['true','false'].includes(node.getAttribute('aria-checked')));
                        const initial = state();
                        return {valid:valid && !node.disabled && !node.readOnly &&
                                initial.length <= 524288,
                            unchanged:() => node.isConnected && node.ownerDocument === doc &&
                                node.form === form && state() === initial};
                    }""",
                        field.model_dump(),
                    )
                    guards.append(guard)
                    if not await guard.evaluate("guard => guard.valid && guard.unchanged()"):
                        raise fail("invalidTarget")
                    if not await targets[0].is_visible() or not await targets[0].is_enabled():
                        raise fail("invalidTarget")
                for field, handle, guard in zip(fields, handles, guards, strict=True):
                    if (
                        not await handle.is_visible()
                        or not await handle.is_enabled()
                        or not await guard.evaluate("guard => guard.unchanged()")
                        or self.document_ids.get(page) != document_id
                    ):
                        raise fail("snapshotChanged")
                    dispatched = True
                    if field.type == "textbox":
                        await handle.fill(field.value, timeout=1000)
                    elif field.type == "combobox":
                        await handle.select_option(label=field.value, timeout=1000)
                    else:
                        await handle.set_checked(field.value == "true", timeout=1000)
                    completed += 1
                    dispatched = False
            return {"completed": completed, "total": len(fields)}
        except Exception as exc:
            if not completed and not dispatched:
                if isinstance(exc, AgentError):
                    raise
                raise fail("snapshotChanged") from None
            return {
                "completed": completed,
                "total": len(fields),
                "errorCode": "browser.outcomeUnknown" if dispatched else "browser.snapshotChanged",
            }
        finally:
            for handle in [*guards, *handles]:
                with contextlib.suppress(Exception):
                    async with asyncio.timeout(0.1):
                        await handle.dispose()

    @staticmethod
    def _snapshot_preview(text: str) -> str:
        if len(text) <= 32_768:
            return text
        prefix = text[:32_768]
        return prefix.rsplit("\n", 1)[0] if "\n" in prefix else prefix

    async def _inspect(
        self,
        page,
        *,
        browser_id: str | None = None,
        guard: bool = True,
        selector: str | None = None,
        depth: int | None = None,
    ) -> tuple[str | None, dict[str, str | int], dict | None]:
        document_id = self.document_ids[page]
        state = (
            await page.locator("body").evaluate("""body => {
                const query = 'a[href],button,input,select,textarea,form,' +
                    '[role],[contenteditable=true]';
                const state = [];
                const elements = body.querySelectorAll(query);
                for (let index = 0; index < Math.min(elements.length, 80); index++) {
                    const node = elements[index];
                    const id = node.id ? '#' + CSS.escape(node.id) : '';
                    const uniqueId = id && id.length <= 1900 &&
                        document.querySelectorAll(id).length === 1;
                    const selector = uniqueId
                        ? id : 'body >> ' + query + ' >> nth=' + index;
                    const tag = node.tagName.toLowerCase();
                    const formAction = tag === 'form' ? node.action :
                        (node.hasAttribute('formaction') ? node.formAction :
                            node.form?.action) || '';
                    const href = node.getAttribute('href') === null ? '' :
                        String(node.href?.baseVal ?? node.href ?? '');
                    const value = node.isContentEditable ? node.textContent : node.value;
                    if (value?.length > 65536) return {tooLarge: true};
                    state.push({selector, value, checked: node.checked,
                        disabled: node.disabled, readOnly: node.readOnly, href, formAction,
                        formMethod: node.hasAttribute('formmethod') ? node.formMethod :
                            node.form?.method || node.method,
                        selected: node.selectedOptions ?
                            Array.from(node.selectedOptions, option => option.index) : null,
                        focused: node === document.activeElement});
                    if (JSON.stringify(state).length > 524288) return {tooLarge: true};
                }
                state.push({focusedTag: document.activeElement?.tagName,
                    focusedId: document.activeElement?.id});
                state.push({width: innerWidth, height: innerHeight,
                    screenWidth: screen.width, screenHeight: screen.height,
                    scrollX, scrollY});
                return {state};
            }""")
            if guard
            else {}
        )
        if state.get("tooLarge"):
            raise fail("fileTooLarge")
        started = monotonic()
        target = page.locator(selector) if selector else page
        options = {"mode": "ai", "timeout": 8000}
        if depth is not None:
            options["depth"] = depth
        try:
            text = await target.aria_snapshot(**options)
        except PlaywrightError:
            if selector is not None:
                raise fail("invalidTarget") from None
            raise
        self.execution_metrics.update(
            snapshotMs=round((monotonic() - started) * 1000, 3),
            snapshotCharacters=len(text) if isinstance(text, str) else 0,
        )
        if not isinstance(text, str):
            raise fail("fileTooLarge")
        encoded = text.encode("utf-8")
        if len(encoded) > MAX_OBSERVATION_BYTES:
            raise fail("fileTooLarge")
        if self.document_ids.get(page) != document_id:
            raise fail("invalidTarget")
        material = json.dumps(
            {
                "pageId": next(k for k, v in self.tabs.items() if v is page),
                "documentId": document_id,
                "url": page.url,
                **({"geometry": self.geometry} if guard else {}),
                "text": text,
                **state,
            }
        ).encode()
        snapshot = (
            await self.stage_generated_file(encoded, browser_id, "browser-snapshot.yml", "snapshot")
            if encoded and browser_id is not None and len(text) > 32_768
            else None
        )
        released = (
            None if snapshot is not None and len(text) > 32_768 else self._snapshot_preview(text)
        )
        return (
            released,
            {
                "snapshotSha256": hmac.new(self.snapshot_key, material, hashlib.sha256).hexdigest(),
                "snapshotCharacters": len(text),
                **({"snapshotScope": "element"} if selector else {}),
                **({"snapshotDepth": depth} if depth is not None else {}),
            },
            snapshot,
        )

    async def close(self) -> None:
        closing = getattr(self, "_close_task", None)
        if closing is None:

            async def close() -> None:
                deadline = asyncio.get_running_loop().time() + 10
                self.pending_choosers.clear()
                self.document_ids.clear()
                tasks = set(self.tasks) | set(self.page_sessions.values())
                for task in tasks:
                    task.cancel()
                if self.server:
                    self.server.close()
                try:
                    async with asyncio.timeout_at(deadline):
                        if self.context:
                            with contextlib.suppress(Exception):
                                await asyncio.wait_for(self.context.close(), 5)
                        if self.playwright:
                            with contextlib.suppress(Exception):
                                await asyncio.wait_for(self.playwright.stop(), 2)
                        if tasks:
                            done, _ = await asyncio.wait(tasks, timeout=2)
                            for task in done:
                                if not task.cancelled():
                                    task.exception()
                        if self.server:
                            await self.server.wait_closed()
                except TimeoutError:
                    pass
                finally:
                    try:
                        self.discard_generated_files("screenshot")
                        self.discard_generated_files("snapshot")
                    finally:
                        if self.temporary:
                            self.temporary.cleanup()

            closing = self._close_task = asyncio.create_task(close())
        while not closing.done():
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(closing)
        closing.result()


async def serve(root: Path) -> None:
    if sys.platform not in {"linux", "win32"} or (sys.platform == "linux" and os.getuid() == 0):
        raise RuntimeError("Browser worker requires Linux or Windows and must not run as root")
    os.umask(0o077)
    if sys.platform == "linux":
        import resource

        # Bound in-progress downloads as well as completed files on POSIX.
        resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_FILE_BYTES, MAX_FILE_BYTES))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    worker = BrowserWorker(root)
    if sys.platform == "win32":

        async def readline():
            return await asyncio.to_thread(sys.stdin.buffer.readline, 1024 * 1024)
    else:
        reader = asyncio.StreamReader(limit=1024 * 1024)
        protocol = asyncio.StreamReaderProtocol(reader)
        await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, sys.stdin.buffer)
        current = asyncio.current_task()
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, current.cancel)
        readline = reader.readline
    # Late task prints must not become a second pipe response after script capture ends.
    protocol_output, sys.stdout = sys.stdout, sys.stderr
    try:
        while raw := await readline():
            op = None
            elapsed_ms = None
            try:
                request = json.loads(raw)
                op = request["op"]
                if op == "start":
                    result = await worker.start(
                        color_scheme=request.get("colorScheme", "light"),
                    )
                elif op == "execute":
                    args = BrowserExecuteArguments.model_validate(request["args"])
                    started = monotonic()
                    try:
                        result = await worker.execute(args)
                    finally:
                        elapsed_ms = max(0, (monotonic() - started) * 1000)
                elif op == "navigation":
                    result = await worker.navigation(
                        BrowserNavigationArguments.model_validate(request["args"])
                    )
                elif op == "file":
                    result = await worker.file(BrowserFileArguments.model_validate(request["args"]))
                elif op == "files":
                    result = worker.file_status()
                elif op == "control":
                    args = BrowserControlArguments.model_validate(request["args"])
                    result = await worker.control(
                        args.manual,
                        origin=args.origin,
                        control_epoch=args.control_epoch,
                    )
                elif op == "clipboard":
                    result = await worker.clipboard(
                        BrowserClipboardArguments.model_validate(request["args"])
                    )
                elif op == "resize":
                    args = BrowserResizeArguments.model_validate(request["args"])
                    result = await worker.resize(args.width, args.height)
                elif op == "view":
                    result = worker.view_endpoint
                elif op == "health":
                    result = {"ok": bool(worker.context and worker.context.browser.is_connected())}
                elif op == "close":
                    break
                else:
                    raise fail("invalidTarget")
                response = {"ok": True, "result": result}
            except AgentError as exc:
                response = {"ok": False, "error": exc.code}
            except Exception:
                response = {"ok": False, "error": "browser.actionFailed"}
            if not response["ok"] and op == "start":
                response["stage"] = worker.startup_stage
            if elapsed_ms is not None:
                response["elapsedMs"] = elapsed_ms
                if metrics := getattr(worker, "execution_metrics", {}):
                    response["metrics"] = metrics
            payload = json.dumps(response, ensure_ascii=True)
            if len(payload) > 950_000:
                payload = '{"ok":false,"error":"browser.fileTooLarge"}'
            print(payload, file=protocol_output, flush=True)
    except asyncio.CancelledError:
        pass
    finally:
        try:
            await worker.close()
        finally:
            sys.stdout = protocol_output
