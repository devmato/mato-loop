from __future__ import annotations

import asyncio
import ctypes
import errno
import os
import pwd
import shlex
import shutil
import sys
import tempfile
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from typing import Any

from .config import AgentConfig
from .errors import AgentError
from .openscad import OpenScadRunner
from .platform import FilesystemAdapter, RuntimeServices
from .platform_posix import PosixPlatform
from .protocol import ARGUMENT_MODELS
from .transfer import TransferClient


class LinuxPlatform(PosixPlatform):
    @property
    def platform_id(self) -> str:
        return sys.platform

    def inhibit_idle_sleep(self) -> Callable[[], None] | None:
        if os.environ.get("MATOLOOP_AGENT_CONTAINER") == "1":
            return None

        # logind grants a lock owned by a file descriptor; no helper can outlive the Agent.
        lib = ctypes.CDLL("libsystemd.so.0")
        ptr, text = ctypes.c_void_p, ctypes.c_char_p
        out_ptr = ctypes.POINTER(ptr)
        lib.sd_bus_open_system.argtypes = [out_ptr]
        lib.sd_bus_set_method_call_timeout.argtypes = [ptr, ctypes.c_uint64]
        lib.sd_bus_call_method.argtypes = [ptr, text, text, text, text, ptr, out_ptr, text]
        lib.sd_bus_message_read.argtypes = [ptr, text]
        lib.sd_bus_message_unref.argtypes = [ptr]
        lib.sd_bus_message_unref.restype = ptr
        lib.sd_bus_unref.argtypes = [ptr]
        lib.sd_bus_unref.restype = ptr

        def check(result: int) -> None:
            if result < 0:
                raise OSError(-result, os.strerror(-result))

        bus, reply = ptr(), ptr()

        def inhibit(what: bytes, mode: bytes) -> int:
            return lib.sd_bus_call_method(
                bus,
                b"org.freedesktop.login1",
                b"/org/freedesktop/login1",
                b"org.freedesktop.login1.Manager",
                b"Inhibit",
                None,
                ctypes.byref(reply),
                b"ssss",
                what,
                b"MatoLoop Agent",
                b"Local work in progress",
                mode,
            )

        check(lib.sd_bus_open_system(ctypes.byref(bus)))
        try:
            check(lib.sd_bus_set_method_call_timeout(bus, 1_000_000))
            mode = b"block-weak"
            for what in (b"idle:sleep", b"idle"):
                result = inhibit(what, mode)
                # Before systemd 257, the only blocking mode had the weaker semantics.
                if result == -errno.EINVAL and mode == b"block-weak":
                    mode = b"block"
                    result = inhibit(what, mode)
                if what == b"idle:sleep" and result in (-errno.EACCES, -errno.EPERM):
                    continue
                check(result)
                break
            fd = ctypes.c_int(-1)
            check(lib.sd_bus_message_read(reply, b"h", ctypes.byref(fd)))
            lock_fd = os.dup(fd.value)  # The reply owns the original descriptor.
        finally:
            lib.sd_bus_message_unref(reply)
            lib.sd_bus_unref(bus)
        return lambda: os.close(lock_fd)

    def default_config_path(self, environ: Mapping[str, str]) -> Path:
        configured = environ.get("MATOLOOP_AGENT_CONFIG")
        if configured:
            return Path(configured)
        data_home = Path(environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
        return data_home / "matoloop-agent" / "config.json"

    def run_auxiliary(self, command: str, arguments: list[str]) -> bool:
        if command == "browser-install":
            from .browser import finish_setup

            raise SystemExit(asyncio.run(finish_setup()))
        if command == "browser-install-worker":
            from playwright.__main__ import main as install_playwright

            if os.getuid() == 0:
                raise SystemExit(1)
            sys.argv = ["playwright", "install", "--no-shell", "chromium"]
            install_playwright()
            return True
        if command == "browser-worker":
            from .browser_worker import serve

            asyncio.run(serve(Path(arguments[0])))
            return True
        return False

    def build_runtime(
        self,
        config: AgentConfig,
        access_key: str,
        *,
        transfer_client: TransferClient | None = None,
    ) -> RuntimeServices:
        runtime = super().build_runtime(config, access_key, transfer_client=transfer_client)
        openscad = OpenScadRunner(config, runtime.filesystem, runtime.cache, runtime.transfer)
        operations = set(runtime.operations) | {
            op for op in ARGUMENT_MODELS if op.startswith("integration.openscad.")
        }
        managed_container = os.environ.get("MATOLOOP_AGENT_INSTALL_ROOT")
        if not (
            os.environ.get("MATOLOOP_AGENT_CONTAINER") == "1"
            and managed_container
            and (Path(managed_container) / "launcher.pid").is_file()
        ):
            operations.discard("maintenance.update")
        root = config.data_dir.parent
        if (
            os.environ.get("MATOLOOP_AGENT_CONTAINER") == "1"
            or not (root / ".managed-by-matoloop-agent").is_file()
            or not (root / "installer.py").is_file()
            or not (root / "bin/python").exists()
            or not shutil.which("sudo")
            or not shutil.which("systemd-run")
        ):
            operations.discard("maintenance.start")
        if not {"maintenance.start", "maintenance.update"}.intersection(operations):
            operations.discard("maintenance.update.interrupt")
        if shutil.which("sudo") is None or os.geteuid() == 0:
            operations.discard("administrator.run")
        if not openscad.render_available:
            operations -= {"integration.openscad.render", "integration.openscad.render6"}
        return replace(runtime, openscad=openscad, operations=tuple(sorted(operations)))

    def maintenance_running(self, config: AgentConfig) -> bool:
        if os.environ.get("MATOLOOP_AGENT_CONTAINER") == "1":
            root = os.environ.get("MATOLOOP_AGENT_INSTALL_ROOT")
            return bool(root and (Path(root) / "update-request.json").exists())
        return super().maintenance_running(config)

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
        if os.environ.get("MATOLOOP_AGENT_CONTAINER") == "1":
            root = os.environ.get("MATOLOOP_AGENT_INSTALL_ROOT")
            if action != "update" or not root:
                raise AgentError("maintenance.unavailable", "Container maintenance is unavailable")
            if before_start is not None:
                await before_start()
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(Path(root) / "installer.py"),
                "container-request",
                job_id,
                "--revision",
                revision,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            if await process.wait() != 0:
                raise AgentError("maintenance.failed", "Container update could not be queued")
            return {"started": True}
        root = config.data_dir.parent
        if (
            not (root / ".managed-by-matoloop-agent").is_file()
            or not (root / "installer.py").is_file()
        ):
            raise AgentError("maintenance.unavailable", "Native maintenance is unavailable")
        sudo = shutil.which("sudo")
        systemd_run = shutil.which("systemd-run")
        if not sudo or not systemd_run:
            raise AgentError("maintenance.unavailable", "System maintenance is unavailable")

        async def authorized(*command: str) -> int:
            argv = [sudo, "-S", "-p", ""] if password else [sudo, "-n"]
            try:
                child = await asyncio.create_subprocess_exec(
                    *argv,
                    *command,
                    stdin=asyncio.subprocess.PIPE if password else asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                try:
                    await asyncio.wait_for(
                        child.communicate((password + "\n").encode() if password else None), 15
                    )
                except (TimeoutError, asyncio.CancelledError):
                    child.kill()
                    await child.wait()
                    raise
                return child.returncode
            except (OSError, TimeoutError) as exc:
                raise AgentError("maintenance.failed", "Maintenance could not be started") from exc

        # Invalidate cached sudo timestamps: the independent unit cannot inherit them.
        if await authorized("-k", "-v") != 0:
            raise AgentError(
                "process.sudoAuthFailed" if password else "process.sudoAuthRequired",
                "Sudo authorization failed",
            )

        if before_start is not None:
            await before_start()

        with tempfile.TemporaryDirectory(prefix="matoloop-maintenance-") as directory:
            socket_path = str(Path(directory) / "handoff.sock")
            received = asyncio.get_running_loop().create_future()

            async def handoff(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
                try:
                    writer.write((password or "").encode())
                    await writer.drain()
                    writer.write_eof()
                    await writer.drain()
                    if not received.done():
                        received.set_result(True)
                finally:
                    writer.close()
                    await writer.wait_closed()

            server = await asyncio.start_unix_server(handoff, path=socket_path)
            try:
                result = await authorized(
                    systemd_run,
                    "--system",
                    "--collect",
                    f"--unit=matoloop-agent-maintenance-{job_id}",
                    f"--property=User={pwd.getpwuid(os.getuid()).pw_name}",
                    "--property=RuntimeMaxSec=3600",
                    str(root / "bin/python"),
                    str(root / "installer.py"),
                    "maintenance-run",
                    action,
                    job_id,
                    socket_path,
                    str(root),
                    *(["--revision", revision] if revision else []),
                )
                if result != 0:
                    raise AgentError("maintenance.failed", "Maintenance could not be started")
                await asyncio.wait_for(received, 15)
            except TimeoutError as exc:
                raise AgentError("maintenance.failed", "Maintenance handoff failed") from exc
            finally:
                server.close()
                await server.wait_closed()
        return {"started": True}

    def system_info(
        self, current_directory: str | None, filesystem: FilesystemAdapter
    ) -> dict[str, Any]:
        from matoloop_version import get_version

        from .temporary import temporary_directory

        result: dict[str, Any] = {
            "temporaryDirectory": temporary_directory(),
            "sessionTemporaryDirectorySupported": True,
            "agentVersion": get_version(),
            "platform": self.platform_id[:80],
            "operatingSystem": None,
            "kernel": None,
            "architecture": None,
            "logicalCpuCount": None,
        }
        with suppress(OSError, ValueError):
            uname = os.uname()
            result.update(
                operatingSystem=uname.sysname,
                kernel=uname.release,
                architecture=uname.machine,
            )
        with suppress(OSError, ValueError):
            count = os.cpu_count()
            if type(count) is int and 1 <= count <= 2147483647:
                result["logicalCpuCount"] = count
        for path in ("/etc/os-release", "/usr/lib/os-release"):
            try:
                with open(path, encoding="utf-8") as handle:
                    for line in handle:
                        name, _, value = line.partition("=")
                        if name.strip() == "PRETTY_NAME":
                            parts = shlex.split(value, comments=True)
                            if len(parts) == 1:
                                result["distribution"] = parts[0]
                            break
                break
            except (OSError, ValueError):
                continue
        with suppress(OSError, ValueError):
            models = {}
            with open("/proc/cpuinfo", encoding="utf-8") as handle:
                for line in handle:
                    name, _, value = line.partition(":")
                    name, value = name.strip(), value.strip()
                    if name in {"model name", "Hardware", "Processor"} and value:
                        if not value.isdecimal():
                            models.setdefault(name, value)
            result["cpuModel"] = next(
                (
                    models[name]
                    for name in ("model name", "Hardware", "Processor")
                    if name in models
                ),
                None,
            )
        values = {}
        try:
            with open("/proc/meminfo", encoding="ascii") as handle:
                for line in handle:
                    name, _, value = line.partition(":")
                    if name in {"MemTotal", "MemAvailable"}:
                        with suppress(ValueError):
                            amount, unit = value.split()
                            size = int(amount) * 1024
                            minimum = 1 if name == "MemTotal" else 0
                            if unit == "kB" and minimum <= size <= 9223372036854775807:
                                values[name] = size
        except (OSError, ValueError):
            pass
        result["totalMemoryBytes"] = values.get("MemTotal")
        if values.keys() >= {"MemTotal", "MemAvailable"}:
            result["memory"] = {
                "totalBytes": values["MemTotal"],
                "availableBytes": values["MemAvailable"],
            }
        for name, limit in (
            ("operatingSystem", 80),
            ("distribution", 255),
            ("kernel", 160),
            ("architecture", 80),
            ("cpuModel", 255),
        ):
            value = result.get(name)
            result[name] = value.strip()[:limit] or None if isinstance(value, str) else None
        if current_directory is not None:
            directory = filesystem.resolve_directory(current_directory)
            usage = shutil.disk_usage(directory["path"])
            result["disk"] = {
                "totalBytes": usage.total,
                "availableBytes": usage.free,
                **directory,
            }
        return result
