"""Native Windows composition of the standalone Agent's core capabilities."""

from __future__ import annotations

import asyncio
import ctypes
import hashlib
import json
import os
import platform
import shutil
import socket
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from pathlib import Path
from typing import Any
from uuid import UUID

from .browser_contracts import BROWSER_ARGUMENT_MODELS
from .browser_windows import WindowsBrowserManager
from .cache_windows import WindowsCache
from .config import AgentConfig
from .credentials_windows import WindowsCredentialStore
from .errors import AgentError
from .filesystem_windows import WindowsFilesystem
from .platform import FilesystemAdapter, RuntimeServices
from .process_windows import WindowsProcess
from .search_windows import WindowsSearch
from .transfer import TransferClient


class _Unavailable:
    async def close(self, *, stop_setup: bool = False) -> None:
        return None

    async def close_all(self) -> None:
        return None


class WindowsPlatform:
    @property
    def platform_id(self) -> str:
        return "win32"

    def computer_name(self) -> str:
        return socket.gethostname()

    def default_config_path(self, environ: Mapping[str, str]) -> Path:
        return Path(
            environ.get("MATOLOOP_AGENT_CONFIG")
            or Path(environ["LOCALAPPDATA"]) / "matoloop-agent" / "data" / "config.json"
        )

    def credential_store(self, data_dir: Path) -> WindowsCredentialStore:
        return WindowsCredentialStore(data_dir)

    def clear_cache(self, config: AgentConfig) -> int:
        return WindowsCache(config.data_dir, config.max_session_cache_bytes).clear()

    @staticmethod
    def _maintenance_available(config: AgentConfig) -> bool:
        root = config.data_dir.parent
        if (
            not (root / ".managed-by-matoloop-agent").is_file()
            or not (root / "installer.ps1").is_file()
        ):
            return False
        try:
            return (
                json.loads((root / "install.json").read_text(encoding="utf-8-sig")).get(
                    "maintenanceReady"
                )
                is True
            )
        except (OSError, ValueError, AttributeError):
            return False

    def maintenance_running(self, config: AgentConfig) -> bool:
        if not self._maintenance_available(config):
            return False
        if (config.data_dir / "maintenance-request.json").exists():
            return True
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.OpenMutexW.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        root = str(config.data_dir.parent).lower().rstrip("\\/")
        name = "Global\\MatoLoopAgentInstaller-" + hashlib.sha256(root.encode()).hexdigest()
        handle = kernel.OpenMutexW(0x00100001, False, name)
        if not handle:
            return ctypes.get_last_error() != 2
        try:
            result = kernel.WaitForSingleObject(handle, 0)
            if result in {0, 0x80}:
                kernel.ReleaseMutex(handle)
                return False
            return True
        finally:
            kernel.CloseHandle(handle)

    async def start_maintenance(
        self,
        config: AgentConfig,
        action: str,
        job_id: str,
        password: str | None,
        revision: str | None = None,
        *,
        before_start: Callable[[], Awaitable[None]] | None = None,
    ) -> dict:
        if not self._maintenance_available(config):
            raise AgentError("maintenance.unavailable", "Native maintenance is unavailable")
        if (
            action not in {"update", "uninstall"}
            or str(UUID(job_id)) != job_id
            or password is not None
        ):
            raise AgentError("request.invalid", "Invalid Windows maintenance request")
        if before_start is not None:
            await before_start()
        request = config.data_dir / "maintenance-request.json"
        try:
            with request.open("x", encoding="utf-8") as file:
                json.dump({"action": action, "jobId": job_id, "revision": revision}, file)
        except OSError as exc:
            raise AgentError(
                "maintenance.failed", "Maintenance request could not be prepared"
            ) from exc
        try:
            shell = os.path.join(
                os.environ["SystemRoot"], "System32", "WindowsPowerShell", "v1.0", "powershell.exe"
            )
            process = await asyncio.create_subprocess_exec(
                shell,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "$ErrorActionPreference='Stop'; "
                "Start-ScheduledTask -TaskName 'MatoLoop Agent Maintenance'",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(process.wait(), 15)
            if process.returncode:
                raise AgentError("maintenance.failed", "Maintenance task could not be started")
        except BaseException:
            request.unlink(missing_ok=True)
            raise
        return {"started": True}

    def run_auxiliary(self, command: str, arguments: list[str]) -> bool:
        if command in {"browser-install", "browser-setup", "browser-worker"}:
            from .browser_windows import contain_process_tree

            contain_process_tree()
        if command in {"browser-install", "browser-setup"}:
            from .browser_windows import finish_setup

            raise SystemExit(asyncio.run(finish_setup(install_packages=command == "browser-setup")))
        if command == "browser-worker":
            from .browser_worker import serve

            asyncio.run(serve(Path(arguments[0])))
            return True
        return False

    def build_runtime(
        self, config: AgentConfig, access_key: str, *, transfer_client=None
    ) -> RuntimeServices:
        filesystem = WindowsFilesystem(config)
        cache = WindowsCache(config.data_dir, config.max_session_cache_bytes)
        process = WindowsProcess(config, filesystem)
        unavailable = _Unavailable()
        transfer = transfer_client or TransferClient(
            max_bytes=config.max_transfer_bytes,
            backend_url=config.backend_url,
            access_key=access_key,
        )
        operations = (
            "system.info",
            *(
                (
                    "git.changes",
                    "git.repository",
                    "git.branch",
                    "git.worktree",
                    "git.worktree.remove",
                )
                if shutil.which("git")
                else ()
            ),
            "filesystem.resolve",
            "filesystem.stat",
            "filesystem.list",
            "filesystem.read",
            "filesystem.write",
            "filesystem.upsert",
            "filesystem.edit",
            "filesystem.patch",
            "filesystem.mkdir",
            "filesystem.rename",
            "filesystem.delete",
            "filesystem.copy",
            "filesystem.move",
            "filesystem.import",
            "filesystem.export",
            "filesystem.glob",
            "filesystem.grep",
            "cache.ensure",
            "cache.skills.prepare",
            "cache.skills.release",
            "cache.store",
            "cache.export",
            "cache.clear",
            "shell.inspect",
            "shell.run",
        )
        return RuntimeServices(
            platform=self,
            filesystem=filesystem,
            cache=cache,
            process=process,
            terminal=unavailable,
            search=WindowsSearch(filesystem),
            browser=WindowsBrowserManager(config, None, filesystem),
            openscad=unavailable,
            transfer=transfer,
            operations=operations
            + tuple(sorted(BROWSER_ARGUMENT_MODELS))
            + (
                ("maintenance.start", "maintenance.update.interrupt")
                if self._maintenance_available(config)
                else ()
            ),
        )

    def system_info(self, current_directory: str | None, filesystem: FilesystemAdapter) -> dict:
        from matoloop_version import get_version

        from .temporary import temporary_directory

        result = {
            "temporaryDirectory": temporary_directory(),
            "sessionTemporaryDirectorySupported": True,
            "agentVersion": get_version(),
            "platform": self.platform_id,
            "operatingSystem": "Windows",
            "distribution": platform.platform()[:255],
            "kernel": platform.version()[:160],
            "architecture": platform.machine()[:80],
            "logicalCpuCount": os.cpu_count(),
        }
        if current_directory is not None:
            directory = filesystem.resolve_directory(current_directory)
            usage = shutil.disk_usage(current_directory)
            result["disk"] = {"totalBytes": usage.total, "availableBytes": usage.free, **directory}
        return result

    def run(self, coroutine: Coroutine[Any, Any, None]) -> None:
        try:
            asyncio.run(coroutine)
        except KeyboardInterrupt:
            pass
