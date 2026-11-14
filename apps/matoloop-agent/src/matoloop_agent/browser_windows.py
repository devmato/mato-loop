"""Windows host lifecycle for the shared Playwright browser worker."""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from .browser import BrowserManager, BrowserRuntime, browser_command
from .errors import AgentError
from .terminal import MAX_BUFFER_BYTES


def contain_process_tree() -> None:
    """Keep auxiliary-process descendants in a job owned only by that process."""
    import ctypes
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("process_time", ctypes.c_longlong),
            ("job_time", ctypes.c_longlong),
            ("flags", wintypes.DWORD),
            ("minimum_working_set", ctypes.c_size_t),
            ("maximum_working_set", ctypes.c_size_t),
            ("active_processes", wintypes.DWORD),
            ("affinity", ctypes.c_size_t),
            ("priority", wintypes.DWORD),
            ("scheduling", wintypes.DWORD),
        ]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("basic", BasicLimits),
            ("io", ctypes.c_ulonglong * 6),
            ("process_memory", ctypes.c_size_t),
            ("job_memory", ctypes.c_size_t),
            ("peak_process_memory", ctypes.c_size_t),
            ("peak_job_memory", ctypes.c_size_t),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.GetCurrentProcess.argtypes = []
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess()):
            raise ctypes.WinError(ctypes.get_last_error())
    except BaseException:
        kernel.CloseHandle(job)
        raise
    # This non-inheritable handle must stay open until process exit, including crashes.
    # Closing it here would terminate this process together with its descendants.


@dataclass
class _Setup:
    terminal_id: str
    process: asyncio.subprocess.Process
    buffer: bytearray = field(default_factory=bytearray)
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    eof: bool = False

    @property
    def exit_code(self) -> int | None:
        return self.process.returncode


class WindowsBrowserRuntime(BrowserRuntime):
    @property
    def supported(self) -> bool:
        return sys.platform == "win32"

    def environment(self) -> tuple[dict[str, str], dict]:
        environment = {
            key: value
            for key, value in os.environ.items()
            if key.upper()
            in {
                "SYSTEMROOT",
                "WINDIR",
                "COMSPEC",
                "TEMP",
                "TMP",
                "USERPROFILE",
                "APPDATA",
                "LOCALAPPDATA",
            }
        }
        system = Path(os.environ["SystemRoot"])
        environment.update(
            PATH=os.pathsep.join(
                map(str, (Path(sys.executable).parent, system / "System32", system))
            ),
            PLAYWRIGHT_BROWSERS_PATH=str(self.installation),
            PYTHONIOENCODING="utf-8",
            PYTHONUNBUFFERED="1",
        )
        return environment, {}

    def setup_environment(self) -> dict[str, str]:
        environment, _ = self.environment()
        environment.update(
            (key, value)
            for key, value in os.environ.items()
            if key.upper()
            in {
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "ALL_PROXY",
                "NO_PROXY",
                "SSL_CERT_FILE",
                "SSL_CERT_DIR",
                "REQUESTS_CA_BUNDLE",
                "CURL_CA_BUNDLE",
                "NODE_EXTRA_CA_CERTS",
                "UV_NATIVE_TLS",
                "PLAYWRIGHT_DOWNLOAD_HOST",
                "PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST",
            }
        )
        return environment

    def worker_options(self) -> dict:
        return {
            "cwd": str(Path(sys.executable).parent),
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP,
        }

    def storage_directory(self, path: Path) -> None:
        from . import windows_handles

        with windows_handles.directory(path.as_posix(), create=True):
            pass

    def runtime_root(self, root: Path) -> Path:
        runtime = root / "browser"
        self.storage_directory(runtime)
        return runtime

    async def stop_worker(self, process) -> None:
        if process.returncode is not None:
            return
        # Windows terminate() skips Python cleanup. EOF lets Playwright close first.
        if process.stdin is not None:
            process.stdin.close()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), 12)
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
        await asyncio.wait_for(process.wait(), 5)

    async def stop_installation(self) -> None:
        await self.stop_worker(self.setup.process)

    async def view_connection(self, endpoint):
        port, token = endpoint["port"], bytes.fromhex(endpoint["token"])
        if type(port) is not int or not 1 <= port <= 65535 or len(token) != 32:
            raise AgentError("browser.notConnected", "Invalid local browser endpoint")
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            async with asyncio.timeout(5):
                writer.write(token)
                await writer.drain()
                if await reader.readexactly(1) != b"\x01":
                    raise AgentError("browser.notConnected", "Browser authentication failed")
        except BaseException:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()
            raise
        return reader, writer

    async def setup_output(self, entry) -> None:
        try:
            while chunk := await entry.process.stdout.read(65_536):
                async with entry.condition:
                    entry.buffer.extend(chunk)
                    self.setup_offset += len(chunk)
                    del entry.buffer[:-MAX_BUFFER_BYTES]
                    entry.condition.notify_all()
            await entry.process.wait()
        finally:
            entry.eof = True
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
        uv = self.config.data_dir.parent / "tools" / "bin" / "uv.exe"
        if not uv.is_file():
            resolved = shutil.which("uv")
            if resolved is None:
                raise AgentError("browser.setupFailed", "Browser setup requires uv")
            uv = Path(resolved)
        environment = self.setup_environment()
        environment.update(
            MATOLOOP_BROWSER_UV_EXECUTABLE=str(uv),
            MATOLOOP_BROWSER_DATA_DIR=str(self.config.data_dir),
            MATOLOOP_BROWSER_ROOT=str(root),
            MATOLOOP_BROWSER_INSTALLATION=str(self.installation),
        )
        process = await asyncio.create_subprocess_exec(
            *browser_command("browser-setup"),
            **self.worker_options(),
            env=environment,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        self.setup_offset = 0
        self.setup = _Setup(str(uuid4()), process)
        self.setup_task = asyncio.create_task(self.setup_output(self.setup))
        return await self.status()

    async def setup_io(self, action: str, args) -> dict:
        if self.setup is None or self.setup.terminal_id != args.setup_id:
            raise AgentError("browser.invalidTarget", "Setup identifier is unavailable")
        if action == "setupwrite":
            raise AgentError("browser.invalidTarget", "Windows browser setup is non-interactive")
        if action == "setupresize":
            return {"ok": True}
        return await super().setup_io(action, args)


class WindowsBrowserManager(BrowserManager, WindowsBrowserRuntime):
    runtime_class = WindowsBrowserRuntime


async def finish_setup(*, install_packages: bool = False) -> int:
    from .config import AgentConfig

    data = Path(os.environ["MATOLOOP_BROWSER_DATA_DIR"])
    config = AgentConfig.model_validate({"homeDirectory": data})
    config._data_directory = data
    manager = WindowsBrowserManager(config, None)
    manager.installation = Path(os.environ["MATOLOOP_BROWSER_INSTALLATION"])
    try:
        root = manager.runtime_root(Path(os.environ["MATOLOOP_BROWSER_ROOT"]))
        environment = manager.setup_environment()
        commands = []
        if install_packages:
            print("Installing browser Python packages in the Agent environment.", flush=True)
            commands.append(
                [
                    os.environ["MATOLOOP_BROWSER_UV_EXECUTABLE"],
                    "pip",
                    "install",
                    "--python",
                    sys.executable,
                    "pillow>=12.1.1,<13",
                    "playwright==1.59.0",
                ]
            )
        manager.storage_directory(manager.installation)
        commands.append(
            [
                sys.executable,
                "-m",
                "playwright",
                "install",
                "--no-shell",
                "chromium",
            ]
        )
        for command in commands:
            process = await asyncio.create_subprocess_exec(
                *command, env=environment, **manager.worker_options()
            )
            if await process.wait() != 0:
                raise AgentError("browser.setupFailed", "Browser installation failed")
        print("Starting the sandboxed browser runtime check.", flush=True)
        await manager.spawn(root)
        status = await manager.rpc("start")
        if status["state"] != "ready":
            raise AgentError(status.get("errorCode") or "browser.notReady", "Browser probe failed")
        print("Browser is ready.", flush=True)
        return 0
    except Exception:
        print("Browser setup or startup check failed.", flush=True)
        return 1
    finally:
        await manager.close()
