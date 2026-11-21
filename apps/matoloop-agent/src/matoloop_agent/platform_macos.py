"""Apple Silicon user-session adapter for the shared POSIX Agent services."""

from __future__ import annotations

import asyncio
import ctypes
import os
import platform
import re
import shutil
import subprocess
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from pathlib import Path
from uuid import UUID

from .browser_macos import MacOSBrowserManager
from .config import AgentConfig
from .errors import AgentError
from .platform import FilesystemAdapter, RuntimeServices
from .platform_posix import PosixPlatform
from .temporary import temporary_directory


class MacOSPlatform(PosixPlatform):
    browser_class = MacOSBrowserManager

    @property
    def platform_id(self) -> str:
        return "darwin"

    def inhibit_idle_sleep(self) -> Callable[[], None]:
        iokit = ctypes.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
        cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
        cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        cf.CFRelease.restype = None
        iokit.IOPMAssertionCreateWithName.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        iokit.IOPMAssertionCreateWithName.restype = ctypes.c_int32
        iokit.IOPMAssertionRelease.argtypes = [ctypes.c_uint32]
        iokit.IOPMAssertionRelease.restype = ctypes.c_int32

        utf8 = 0x08000100  # kCFStringEncodingUTF8
        assertion_on = 255  # kIOPMAssertionLevelOn
        kind = cf.CFStringCreateWithCString(None, b"PreventUserIdleSystemSleep", utf8)
        name = cf.CFStringCreateWithCString(None, b"MatoLoop Agent work", utf8)
        assertion = ctypes.c_uint32()
        try:
            if not kind or not name:
                raise OSError("Could not create power assertion strings")
            if iokit.IOPMAssertionCreateWithName(kind, assertion_on, name, ctypes.byref(assertion)):
                raise OSError("IOKit rejected the idle sleep assertion")
        finally:
            if kind:
                cf.CFRelease(kind)
            if name:
                cf.CFRelease(name)

        def release() -> None:
            if iokit.IOPMAssertionRelease(assertion):
                raise OSError("IOKit could not release the idle sleep assertion")

        return release

    def default_config_path(self, environ: Mapping[str, str]) -> Path:
        return Path(
            environ.get("MATOLOOP_AGENT_CONFIG")
            or Path.home() / "Library/Application Support/MatoLoop Agent/data/config.json"
        )

    def run_auxiliary(self, command: str, arguments: list[str]) -> bool:
        if command in {"browser-setup", "browser-install"}:
            from .browser_macos import finish_setup

            raise SystemExit(asyncio.run(finish_setup(install_packages=command == "browser-setup")))
        if command == "browser-worker":
            from .browser_worker import serve

            asyncio.run(serve(Path(arguments[0])))
            return True
        return False

    @staticmethod
    def _maintenance_available(config: AgentConfig) -> bool:
        root = config.data_dir.parent
        return (
            all(
                (root / name).is_file()
                for name in (
                    ".managed-by-matoloop-agent",
                    "installer.py",
                    "bin/python",
                    "install.json",
                )
            )
            and Path.home().joinpath("Library/LaunchAgents/com.matoloop.agent.plist").is_file()
        )

    def build_runtime(
        self, config: AgentConfig, access_key: str, *, transfer_client=None
    ) -> RuntimeServices:
        if platform.machine() != "arm64" or os.geteuid() == 0:
            raise RuntimeError("macOS Agent requires a native Apple Silicon user session")
        runtime = super().build_runtime(config, access_key, transfer_client=transfer_client)
        operations = set(runtime.operations) - {"administrator.run", "maintenance.update"}
        if not self._maintenance_available(config):
            operations -= {"maintenance.start", "maintenance.update.interrupt"}
        return replace(runtime, operations=tuple(sorted(operations)))

    def system_info(self, current_directory: str | None, filesystem: FilesystemAdapter) -> dict:
        from matoloop_version import get_version

        uname = os.uname()
        result = {
            "temporaryDirectory": temporary_directory(),
            "sessionTemporaryDirectorySupported": True,
            "agentVersion": get_version(),
            "platform": "darwin",
            "operatingSystem": "macOS",
            "distribution": platform.mac_ver()[0] or None,
            "kernel": uname.release[:160],
            "architecture": uname.machine[:80],
            "logicalCpuCount": os.cpu_count(),
        }
        for key, name in (
            ("cpuModel", "machdep.cpu.brand_string"),
            ("totalMemoryBytes", "hw.memsize"),
        ):
            try:
                value = subprocess.check_output(
                    ["/usr/sbin/sysctl", "-n", name], timeout=5, text=True
                ).strip()
                if key == "cpuModel":
                    result[key] = value[:255] or None
                elif value.isdecimal() and 0 < int(value) <= 9223372036854775807:
                    result[key] = int(value)
            except (OSError, subprocess.SubprocessError):
                pass
        if current_directory is not None:
            directory = filesystem.resolve_directory(current_directory)
            usage = shutil.disk_usage(directory["path"])
            result["disk"] = {
                "totalBytes": usage.total,
                "availableBytes": usage.free,
                **directory,
            }
        return result

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
        try:
            valid_id = str(UUID(job_id)) == job_id
        except ValueError:
            valid_id = False
        if (
            not valid_id
            or action not in {"update", "uninstall"}
            or password is not None
            or (action == "update" and not re.fullmatch(r"[0-9a-f]{40}", revision or ""))
            or (action == "uninstall" and revision is not None)
        ):
            raise AgentError("request.invalid", "Invalid macOS maintenance request")
        if before_start is not None:
            await before_start()
        root = config.data_dir.parent
        child = None
        try:
            child = await asyncio.create_subprocess_exec(
                str(root / "bin/python"),
                str(root / "installer.py"),
                "maintenance-start",
                action,
                job_id,
                str(root),
                *(["--revision", revision] if revision else []),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            if await asyncio.wait_for(child.wait(), 45):
                raise AgentError("maintenance.failed", "Maintenance worker could not start")
        except (OSError, TimeoutError) as exc:
            raise AgentError("maintenance.failed", "Maintenance handoff failed") from exc
        finally:
            if child is not None and child.returncode is None:
                child.terminate()
                # The launcher must finish exact-job cleanup before cancellation returns.
                cleanup = asyncio.create_task(child.wait())
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        pass
                cleanup.result()
        return {"started": True}
