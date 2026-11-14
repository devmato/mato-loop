from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import os
import shutil
import signal
import stat
import sys
from pathlib import Path
from time import monotonic
from uuid import uuid4

import websockets

from .browser_contracts import (
    BROWSER_FILE_RESULT,
    MAX_FILE_BYTES,
    BrowserActionResult,
    BrowserClipboardResult,
    BrowserExecuteArguments,
    BrowserFileOkResult,
    BrowserFilesResult,
    BrowserIdArguments,
    BrowserNavigationResult,
    BrowserResizeResult,
    BrowserSetupReadResult,
    BrowserStatus,
)
from .browser_storage import browser_file
from .errors import AgentError
from .platform import FilesystemAdapter
from .terminal import MAX_BUFFER_BYTES, TerminalManager

logger = logging.getLogger(__name__)
CONTAINER_INSTALLATION = Path("/usr/local/lib/matoloop-agent/tools/browser")
BROWSER_PATH = "/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin:/bin:/sbin"


def browser_command(command: str) -> list[str]:
    return [sys.executable, "-m", "matoloop_agent", command]


class BrowserRuntime:
    def __init__(
        self, config, terminal: TerminalManager | None, filesystem: FilesystemAdapter | None = None
    ) -> None:
        self.config = config
        self.terminal = terminal
        self.filesystem = filesystem
        self.lock = asyncio.Lock()
        self.worker = None
        self.container = os.environ.get("MATOLOOP_AGENT_CONTAINER") == "1"
        self.installation = (
            CONTAINER_INSTALLATION
            if self.container
            else config.data_dir.parent / "tools" / "browser"
        )
        self.browser_id = None
        self.session_id = None
        self.owner_id = None
        self.view_tasks: set[asyncio.Task] = set()
        self.setup = None
        self.setup_offset = 0
        self.setup_task = None
        self.last_status = BrowserStatus(state="missing")

    @property
    def maintenance_busy(self) -> bool:
        return self.browser_id is not None or (
            self.setup is not None and self.setup.exit_code is None
        )

    def environment(self) -> tuple[dict[str, str], dict]:
        import pwd

        user = pwd.getpwnam("matoloop-browser") if os.getuid() == 0 else pwd.getpwuid(os.getuid())
        environment = {
            "HOME": str(self.config.data_dir / "tools" / "browser"),
            "USER": user.pw_name,
            "LOGNAME": user.pw_name,
            "PATH": BROWSER_PATH,
            "LANG": "C.UTF-8",
            "PLAYWRIGHT_BROWSERS_PATH": str(self.installation),
            "MATOLOOP_AGENT_CONTAINER": "1" if self.container else "0",
        }
        options = (
            {"user": user.pw_uid, "group": user.pw_gid, "extra_groups": []}
            if os.getuid() == 0
            else {}
        )
        return environment, options

    @staticmethod
    def directory(path: Path, mode: int, uid: int) -> None:
        # Refuse symlinked ancestors, including a replaced persistent storage mount.
        for ancestor in reversed([path, *path.parents]):
            if ancestor == path:
                ancestor.mkdir(mode=mode, exist_ok=True)
            metadata = ancestor.lstat()
            if not stat.S_ISDIR(metadata.st_mode):
                raise AgentError(
                    "browser.profileOwnerMismatch", "Browser storage is not a directory"
                )
        metadata = path.lstat()
        if metadata.st_uid != uid:
            raise AgentError("browser.profileOwnerMismatch", "Browser storage owner differs")
        path.chmod(mode)

    def storage_directory(self, path: Path) -> None:
        self.directory(path, 0o711 if os.getuid() == 0 else 0o700, os.getuid())

    def storage(self, owner_id: str | None = None) -> Path:
        identity = hashlib.sha256(
            f"{self.config.backend_url}\n{self.config.agent_id}".encode()
        ).hexdigest()
        tools = self.config.data_dir / "tools"
        self.storage_directory(tools)
        base = tools / "browser"
        self.storage_directory(base)
        root = base / identity
        self.storage_directory(root)
        if owner_id is not None:
            binding = json.dumps(
                {
                    "owner": owner_id.lower(),
                    "agent": self.config.agent_id,
                    "backend": self.config.backend_url,
                },
                sort_keys=True,
            ).encode()
            try:
                with browser_file(root / "binding", "wb", create=True) as handle:
                    handle.write(binding)
            except FileExistsError:
                with browser_file(root / "binding") as handle:
                    if handle.read(16_384) != binding:
                        raise AgentError(
                            "browser.profileOwnerMismatch", "Browser binding differs"
                        ) from None
            self.owner_id = owner_id
        return root

    def runtime_root(self, root: Path) -> Path:
        _, options = self.environment()
        uid = options.get("user", os.getuid())
        runtime = root / "browser"
        try:
            runtime.mkdir(mode=0o700)
        except FileExistsError:
            pass
        else:
            if os.getuid() == 0:
                os.chown(runtime, uid, options["group"], follow_symlinks=False)
        self.directory(runtime, 0o700, uid)
        return runtime

    def missing_python_packages(self) -> list[str]:
        if self.container:
            return []
        from importlib.util import find_spec

        return [
            "pillow" if name == "PIL" else name
            for name in ("PIL", "playwright")
            if find_spec(name) is None
        ]

    @property
    def supported(self) -> bool:
        return sys.platform.startswith("linux")

    def worker_options(self) -> dict:
        return {"cwd": "/", "start_new_session": True}

    async def spawn(self, root: Path) -> None:
        if self.missing_python_packages():
            raise AgentError("browser.dependenciesMissing", "Required browser packages are missing")
        environment, options = self.environment()
        self.worker = await asyncio.create_subprocess_exec(
            *browser_command("browser-worker"),
            str(root),
            env=environment,
            **self.worker_options(),
            **options,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=1024 * 1024,
        )

    async def rpc(self, op: str, **values) -> dict:
        if self.worker is None or self.worker.returncode is not None:
            raise AgentError("browser.notConnected", "Browser worker is unavailable")
        try:
            arguments = values.get("args")
            code_execution = (
                op == "execute"
                and isinstance(arguments, dict)
                and arguments.get("action")
                in {
                    "run_code",
                    "evaluate",
                }
            )
            uploading = isinstance(arguments, dict) and (
                op == "execute"
                and arguments.get("action") == "upload"
                or op == "file"
                and arguments.get("action") == "choose"
            )
            rpc_timeout = (
                60 + MAX_FILE_BYTES / (256 * 1024) if uploading else 140 if code_execution else 55
            )
            async with asyncio.timeout(rpc_timeout):
                started = monotonic()
                self.worker.stdin.write(json.dumps({"op": op, **values}).encode() + b"\n")
                await self.worker.stdin.drain()
                raw = await self.worker.stdout.readline()
                roundtrip_ms = max(0, (monotonic() - started) * 1000)
                response = json.loads(raw)
            if op == "execute":
                elapsed_ms = response.get("elapsedMs")
                metrics = response.get("metrics")
                # Optional private diagnostics must not affect the operation's outcome.
                if not (type(elapsed_ms) in (int, float) and 0 <= elapsed_ms <= rpc_timeout * 1000):
                    elapsed_ms = None
                if not (
                    isinstance(metrics, dict)
                    and set(metrics)
                    <= {"settleMs", "settleOutcome", "snapshotMs", "snapshotCharacters"}
                    and metrics.get("settleOutcome", "settled")
                    in {"settled", "network", "loading", "pageActivity", "renderer"}
                    and all(
                        type(metrics[key]) in (int, float)
                        and 0 <= metrics[key] <= rpc_timeout * 1000
                        for key in {"settleMs", "snapshotMs"} & metrics.keys()
                    )
                    and type(metrics.get("snapshotCharacters", 0)) is int
                    and 0 <= metrics.get("snapshotCharacters", 0) <= MAX_FILE_BYTES
                ):
                    metrics = {}
                with contextlib.suppress(ValueError):
                    args = BrowserExecuteArguments.model_validate(values.get("args"))
                    browser_id = (
                        BrowserIdArguments(browserId=self.browser_id).browser_id
                        if self.browser_id is not None
                        else None
                    )
                    logger.info(
                        "Browser execution latency: method=integration.browser.execute "
                        "action=%s browser_id=%s pipe_roundtrip_ms=%.3f worker_elapsed_ms=%s "
                        "worker_phases=%s",
                        args.action,
                        browser_id,
                        roundtrip_ms,
                        elapsed_ms,
                        metrics,
                    )
            if not response["ok"]:
                code = BrowserStatus(state="error", errorCode=response["error"]).error_code
                logger.warning("Browser worker operation failed: %s", code)
                if op == "start":
                    stage = response.get("stage")
                    logger.warning(
                        "Browser startup failed: stage=%s profile=%s",
                        stage if stage in {"storage", "chromium", "page"} else "unknown",
                        "temporary",
                    )
                raise AgentError(code, "Browser worker operation failed")
            if op == "file":
                return BROWSER_FILE_RESULT.validate_python(response["result"]).model_dump(
                    by_alias=True, exclude_none=True
                )
            if op == "files":
                return BrowserFilesResult.model_validate(response["result"]).model_dump(
                    by_alias=True, exclude_none=True
                )
            if op == "execute":
                return BrowserActionResult.model_validate(response["result"]).model_dump(
                    by_alias=True
                )
            if op == "clipboard":
                return BrowserClipboardResult.model_validate(response["result"]).model_dump()
            if op == "navigation":
                return BrowserNavigationResult.model_validate(response["result"]).model_dump(
                    by_alias=True
                )
            if op == "resize":
                return BrowserResizeResult.model_validate(response["result"]).model_dump()
            if op == "control":
                return BrowserFileOkResult.model_validate(response["result"]).model_dump()
            return response["result"]
        except (
            TimeoutError,
            json.JSONDecodeError,
            ValueError,
            OSError,
            KeyError,
            TypeError,
        ) as exc:
            reason = (
                "timeout"
                if isinstance(exc, TimeoutError)
                else "invalidJson"
                if isinstance(exc, json.JSONDecodeError)
                else "transport"
                if isinstance(exc, OSError)
                else "invalidResponse"
            )
            logger.warning("Browser worker RPC failed: operation=%s reason=%s", op, reason)
            await self.close()
            raise AgentError("browser.outcomeUnknown", "Browser worker outcome is unknown") from exc
        except asyncio.CancelledError:
            # Never let the next RPC consume a cancelled operation's response.
            await self.close()
            raise

    async def close(self, *, stop_setup: bool = False) -> None:
        if stop_setup and self.setup is not None:
            if self.setup.exit_code is None:
                await self.stop_installation()
            if self.setup_task is not None:
                await self.setup_task
        closing = getattr(self, "_close_task", None)
        if closing is None or closing.done():
            process, self.worker = self.worker, None
            views, self.view_tasks = self.view_tasks, set()
            self.browser_id = None

            async def close() -> None:
                for view in views:
                    view.cancel()
                if process is not None:
                    await self.stop_worker(process)
                if views:
                    await asyncio.wait(views, timeout=1)

            closing = self._close_task = asyncio.create_task(close())
        # RPC cancellation and connection teardown may close the same worker concurrently.
        cancelled = False
        while not closing.done():
            try:
                await asyncio.shield(closing)
            except asyncio.CancelledError:
                cancelled = True
        closing.result()
        if cancelled:
            raise asyncio.CancelledError

    async def stop_installation(self) -> None:
        await self.terminal._terminate(self.setup)

    async def stop_worker(self, process) -> None:
        try:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), 12)
        finally:
            # Reap descendants only after the worker's 10-second cleanup budget.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), 1)

    async def status(self) -> dict:
        if not self.supported:
            return BrowserStatus(state="unsupported", errorCode="browser.unsupported").model_dump(
                by_alias=True
            )
        if self.setup is not None and self.setup.exit_code is None:
            return BrowserStatus(state="installing", setupId=self.setup.terminal_id).model_dump(
                by_alias=True
            )
        if self.setup is not None and self.setup.exit_code != 0:
            return BrowserStatus(
                state="error", setupId=self.setup.terminal_id, errorCode="browser.setupFailed"
            ).model_dump(by_alias=True)
        if missing := self.missing_python_packages():
            return BrowserStatus(
                state="missing",
                missing=missing,
                errorCode="browser.dependenciesMissing",
            ).model_dump(by_alias=True)
        if self.worker is not None:
            try:
                if (await self.rpc("health"))["ok"]:
                    return self.last_status.model_dump(by_alias=True)
            except AgentError:
                pass
            await self.close()
        try:
            root = self.runtime_root(self.storage())
            await self.spawn(root)
            self.last_status = BrowserStatus.model_validate(await self.rpc("start"))
        except KeyError:
            self.last_status = BrowserStatus(
                state="missing", missing=["runtimeUser"], errorCode="browser.dependenciesMissing"
            )
        except AgentError as exc:
            self.last_status = BrowserStatus(state="error", errorCode=exc.code)
        finally:
            await self.close()
        self.last_status.setup_id = self.setup.terminal_id if self.setup else None
        return self.last_status.model_dump(by_alias=True)

    async def setup_output(self, entry) -> None:
        try:
            while chunk := await self.terminal._read_fd(entry):
                async with entry.condition:
                    entry.buffer.extend(chunk)
                    self.setup_offset += len(chunk)
                    del entry.buffer[:-MAX_BUFFER_BYTES]
                    entry.condition.notify_all()
        finally:
            entry.eof = True
            self.terminal._close_fd(entry)
            async with entry.condition:
                entry.condition.notify_all()

    async def start_setup(self, owner_id: str) -> dict:
        root = self.storage(owner_id)
        if self.setup is not None and self.setup.exit_code is None:
            return await self.status()
        if self.worker is not None:
            raise AgentError("browser.busy", "Browser runtime is active")
        if self.setup_task is not None:
            await self.setup_task
        script = Path(__file__).with_name("browser-setup.sh")
        uv = self.config.data_dir.parent / "tools/bin/uv"
        if not uv.is_file():
            uv = Path(shutil.which("uv") or "uv")
        environment = {
            "HOME": str(Path.home()),
            "PATH": BROWSER_PATH,
            "LANG": "C.UTF-8",
            "TERM": "xterm-256color",
            "MATOLOOP_BROWSER_EXECUTABLE": sys.executable,
            "MATOLOOP_BROWSER_UV_EXECUTABLE": str(uv),
            "MATOLOOP_AGENT_CONTAINER": "1" if self.container else "0",
            "MATOLOOP_BROWSER_DATA_DIR": str(self.config.data_dir),
            "MATOLOOP_BROWSER_ROOT": str(root),
            "MATOLOOP_BROWSER_INSTALLATION": str(self.installation),
        }
        self.setup_offset = 0
        self.setup = self.terminal._spawn(
            str(uuid4()),
            [str(Path("/bin/bash").resolve()), str(script)],
            self.terminal.home,
            24,
            100,
            environment,
        )
        # This entry is deliberately not registered with disconnect-scoped terminals.
        self.setup_task = asyncio.create_task(self.setup_output(self.setup))
        return await self.status()

    async def setup_io(self, action: str, args) -> dict:
        entry = self.setup
        if entry is None or entry.terminal_id != args.setup_id:
            raise AgentError("browser.invalidTarget", "Setup identifier is unavailable")
        if action == "setupwrite":
            await self.terminal._write(entry, base64.b64decode(args.data, validate=True))
            return {"ok": True}
        if action == "setupresize":
            try:
                self.terminal._resize(entry, args.rows, args.columns)
            except AgentError as exc:
                if exc.code != "terminal.closed":
                    raise
            return {"ok": True}
        async with entry.condition:
            if args.offset >= self.setup_offset and not entry.eof and args.wait_seconds:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(entry.condition.wait(), args.wait_seconds)
            start = max(min(args.offset, self.setup_offset), self.setup_offset - len(entry.buffer))
            index = start - (self.setup_offset - len(entry.buffer))
            data = bytes(entry.buffer[index : index + args.max_bytes])
        return BrowserSetupReadResult(
            data=base64.b64encode(data).decode("ascii"),
            offset=start + len(data),
            running=entry.exit_code is None,
            exitCode=entry.exit_code,
        ).model_dump(by_alias=True)

    async def view_connection(self, endpoint):
        return await asyncio.open_unix_connection(endpoint["socket"])

    async def relay(self, endpoint, relay_id: str, relay_token: str, ready) -> None:
        url = (
            self.config.backend_url.removesuffix("/execution-agents/connect")
            + f"/browser-relay/{relay_id}"
        )
        writer = None
        tasks = []
        try:
            async with websockets.connect(
                url,
                additional_headers={"Authorization": f"Bearer {relay_token}"},
                max_size=1024 * 1024,
                max_queue=4,
                compression=None,
                open_timeout=10,
                proxy=None,
            ) as websocket:
                reader, writer = await self.view_connection(endpoint)
                ready.set_result(True)

                async def outbound():
                    while chunk := await reader.read(65_536):
                        await websocket.send(chunk)

                async def inbound():
                    async for chunk in websocket:
                        if not isinstance(chunk, bytes):
                            raise ValueError("Binary relay required")
                        writer.write(chunk)
                        await writer.drain()

                tasks = [asyncio.create_task(outbound()), asyncio.create_task(inbound())]
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except Exception:
            if not ready.done():
                ready.set_result(False)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if writer is not None:
                writer.close()
                with contextlib.suppress(ConnectionError):
                    await writer.wait_closed()
            if not ready.done():
                ready.set_result(False)

    async def stage_upload(self, args) -> dict:
        if self.filesystem is None:
            raise AgentError("browser.actionFailed", "Filesystem is unavailable")
        export = self.filesystem.open_export(args.path)
        handle, size, _ = export.__enter__()
        file_id = None
        try:
            if size > MAX_FILE_BYTES:
                raise AgentError("browser.fileTooLarge", "Browser file is too large")
            staged = await self.rpc(
                "file",
                args={
                    "browserId": args.browser_id,
                    "action": "begin",
                    "filename": Path(args.path).name,
                    "size": size,
                },
            )
            file_id = staged["fileId"]
            offset = 0
            while data := handle.read(65_536):
                staged = await self.rpc(
                    "file",
                    args={
                        "browserId": args.browser_id,
                        "action": "write",
                        "fileId": file_id,
                        "offset": offset,
                        "data": base64.b64encode(data).decode("ascii"),
                    },
                )
                offset = staged["offset"]
            return await self.rpc(
                "file",
                args={"browserId": args.browser_id, "action": "finish", "fileId": file_id},
            )
        except BaseException:
            if file_id is not None:
                with contextlib.suppress(AgentError):
                    await self.rpc(
                        "file",
                        args={
                            "browserId": args.browser_id,
                            "action": "delete",
                            "fileId": file_id,
                        },
                    )
            raise
        finally:
            export.__exit__(None, None, None)

    async def dispatch(self, operation: str, args) -> dict:
        action = operation.rsplit(".", 1)[-1]
        try:
            if action.startswith("setup") and action != "setup":
                return await self.setup_io(action, args)
            async with self.lock:
                if self.worker is not None and self.worker.returncode is not None:
                    await self.close()
                if action == "status":
                    return await self.status()
                if not self.supported:
                    raise AgentError("browser.unsupported", "Browser host is unsupported")
                if action == "setup":
                    return await self.start_setup(args.owner_id)
                if action == "open":
                    root = self.storage(args.owner_id)
                    if self.setup and self.setup.exit_code is None:
                        raise AgentError("browser.busy", "Browser setup is active")
                    if self.worker:
                        if (
                            self.browser_id == args.browser_id
                            and self.session_id == args.session_id
                            and (await self.rpc("health"))["ok"]
                        ):
                            return {"browserId": self.browser_id, "protocol": "chat-browser-v1"}
                        raise AgentError("browser.busy", "Another browser is active")
                    try:
                        await self.spawn(self.runtime_root(root))
                        self.last_status = BrowserStatus.model_validate(
                            await self.rpc("start", colorScheme=args.color_scheme)
                        )
                        if self.last_status.state != "ready":
                            raise AgentError(
                                self.last_status.error_code or "browser.notReady",
                                "Browser is not ready",
                            )
                        self.browser_id = args.browser_id
                        self.session_id = args.session_id
                    except BaseException:
                        await self.close()
                        raise
                    return {"browserId": self.browser_id, "protocol": "chat-browser-v1"}
                if action == "close" and self.browser_id is None:
                    return {"ok": True}
                if args.browser_id != self.browser_id:
                    raise AgentError("browser.notConnected", "Browser identifier is unavailable")
                if action == "close":
                    await self.close()
                    return {"ok": True}
                if action == "view":
                    result = await self.rpc("view")
                    ready = asyncio.get_running_loop().create_future()
                    view = asyncio.create_task(
                        self.relay(result, args.relay_id, args.relay_token, ready)
                    )
                    self.view_tasks.add(view)
                    view.add_done_callback(self.view_tasks.discard)
                    if not await ready:
                        raise AgentError("browser.notConnected", "Browser relay could not connect")
                    return {"ok": True, "protocol": "page-stream-v1"}
                if action == "files":
                    return await self.rpc("files")
                if action == "upload":
                    return await self.stage_upload(args)
                if action in {"resize", "navigation"}:
                    # Drain the response under the same lock even if a viewer disappears.
                    # Cancelling this pipe RPC would otherwise close the live browser.
                    pending = asyncio.create_task(
                        self.rpc(action, args=args.model_dump(by_alias=True, exclude_unset=True))
                    )
                    try:
                        return await asyncio.shield(pending)
                    except asyncio.CancelledError:
                        while not pending.done():
                            with contextlib.suppress(asyncio.CancelledError):
                                await asyncio.shield(pending)
                        pending.result()
                        raise
                return await self.rpc(action, args=args.model_dump(by_alias=True))
        except AgentError as exc:
            if exc.code.startswith("browser.") or (
                action == "publish" and exc.code == "path.exists"
            ):
                raise
            raise AgentError("browser.actionFailed", "Browser operation failed") from None
        except Exception:
            logger.error("Browser operation failed")
            raise AgentError("browser.actionFailed", "Browser operation failed") from None


class BrowserManager(BrowserRuntime):
    """Route each chat's opaque browser ID to its own disposable worker."""

    runtime_class = BrowserRuntime

    def __init__(self, config, terminal, filesystem=None):
        super().__init__(config, terminal, filesystem)
        self.runtimes: dict[str, BrowserRuntime] = {}

    @property
    def maintenance_busy(self) -> bool:
        return bool(self.runtimes) or super().maintenance_busy

    async def dispatch(self, operation: str, args) -> dict:
        action = operation.rsplit(".", 1)[-1]
        if action.startswith("setup") or action == "status":
            if self.runtimes:
                if action == "status":
                    return await next(iter(self.runtimes.values())).dispatch(operation, args)
                if action == "setup":
                    raise AgentError("browser.busy", "Browser runtime is active")
            return await super().dispatch(operation, args)
        async with self.lock:
            runtime = self.runtimes.get(args.browser_id)
            if runtime is None:
                if action == "close":
                    return {"ok": True}
                if action != "open":
                    raise AgentError("browser.notConnected", "Browser identifier is unavailable")
                if len(self.runtimes) >= 8 or (self.setup and self.setup.exit_code is None):
                    raise AgentError("browser.busy", "Browser runtime capacity is unavailable")
                runtime = self.runtime_class(self.config, self.terminal, self.filesystem)
                runtime.installation, runtime.container = self.installation, self.container
                self.runtimes[args.browser_id] = runtime
        try:
            return await runtime.dispatch(operation, args)
        finally:
            if runtime.worker is None and self.runtimes.get(args.browser_id) is runtime:
                self.runtimes.pop(args.browser_id, None)

    async def close(self, *, stop_setup: bool = False) -> None:
        runtimes, self.runtimes = self.runtimes, {}
        await asyncio.gather(
            super().close(stop_setup=stop_setup),
            *(runtime.close() for runtime in runtimes.values()),
        )


async def finish_setup() -> int:
    """Complete the fixed setup script with a runtime-user install and a disposable probe."""
    from .config import AgentConfig

    data = Path(os.environ["MATOLOOP_BROWSER_DATA_DIR"])
    config = AgentConfig.model_validate({"homeDirectory": data})
    config._data_directory = data
    manager = BrowserManager(config, None)
    manager.installation = Path(os.environ["MATOLOOP_BROWSER_INSTALLATION"])
    try:
        root = manager.runtime_root(Path(os.environ["MATOLOOP_BROWSER_ROOT"]))
        environment, options = manager.environment()
        environment["HOME"] = str(root)
        if not manager.container:
            print("Installing the matching Chromium version.", flush=True)
            manager.directory(manager.installation.parent, 0o711 if options else 0o700, os.getuid())
            if not manager.installation.exists():
                manager.installation.mkdir(mode=0o700)
                if options:
                    os.chown(manager.installation, options["user"], options["group"])
            manager.directory(manager.installation, 0o700, options.get("user", os.getuid()))
            process = await asyncio.create_subprocess_exec(
                *browser_command("browser-install-worker"),
                env=environment,
                cwd="/",
                **options,
            )
            if await process.wait() != 0:
                raise AgentError("browser.setupFailed", "Managed browser installation failed")
            print("Chromium installation completed.", flush=True)
        print("Starting the browser runtime check.", flush=True)
        await manager.spawn(root)
        status = BrowserStatus.model_validate(await manager.rpc("start"))
        if status.state != "ready":
            raise AgentError(status.error_code or "browser.notReady", "Browser probe failed")
        print(f"Chromium: {status.browser_version}", flush=True)
        print(f"Playwright: {status.playwright_version}", flush=True)
        print("Browser is ready.", flush=True)
        return 0
    except Exception:
        print("Browser setup or startup check failed.", flush=True)
        return 1
    finally:
        await manager.close()
