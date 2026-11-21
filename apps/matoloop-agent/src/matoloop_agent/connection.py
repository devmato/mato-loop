from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import nullcontext, suppress
from typing import Any

import websockets
from pydantic import ValidationError

from .browser_contracts import BROWSER_ARGUMENT_MODELS
from .config import AgentConfig
from .errors import AgentError, argument_details
from .maintenance_results import replay_maintenance_results
from .platform import RuntimeServices, TransferAdapter, current_platform
from .protocol import (
    AdministratorArguments,
    CacheEnsureArguments,
    CacheExportArguments,
    CacheStoreArguments,
    Cancel,
    CopyMoveArguments,
    DeleteArguments,
    EditArguments,
    Error,
    Event,
    ExportArguments,
    GitChangesArguments,
    GlobArguments,
    GrepArguments,
    Hello,
    ImportArguments,
    ListArguments,
    MaintenanceStartArguments,
    MkdirArguments,
    OpenScadDiscoverArguments,
    OpenScadRenderArguments,
    OpenScadTestArguments,
    PatchArguments,
    ReadArguments,
    RenameArguments,
    Request,
    ResolveDirectoryArguments,
    Response,
    ShellArguments,
    ShellInspectArguments,
    SkillCachePrepareArguments,
    SkillCacheReleaseArguments,
    StatArguments,
    StrictModel,
    SystemInfo,
    SystemInfoArguments,
    TerminalCloseArguments,
    TerminalOpenArguments,
    TerminalReadArguments,
    TerminalResizeArguments,
    TerminalRunArguments,
    TerminalWriteArguments,
    UpsertArguments,
    Welcome,
    WriteArguments,
    parse_inbound,
    validate_arguments,
)
from .sleep_inhibitor import SleepInhibitor

logger = logging.getLogger(__name__)
MAX_WIRE_BYTES = 1024 * 1024
MAINTENANCE_DRAIN_SECONDS = 60
# Background polling and bookkeeping are not work, even while a UI is open.
PASSIVE_OPERATIONS = frozenset(
    {
        "system.info",
        "terminal.read",
        "integration.browser.status",
        "integration.browser.setupread",
        "integration.browser.view",
        "cache.skills.release",
    }
)


def serialize_message(message, max_bytes: int = MAX_WIRE_BYTES) -> str:
    payload = message.model_dump_json(by_alias=True, exclude_none=True)
    if len(payload.encode("utf-8")) >= min(max_bytes, MAX_WIRE_BYTES):
        raise AgentError("operation.tooLarge", "Response exceeds the transport limit")
    return payload


async def _run_mutation(function: Callable, *args, **kwargs) -> dict[str, Any]:
    worker = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        # A thread cannot be stopped safely. Its real result remains the terminal outcome.
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                pass
        return worker.result()


class MatoLoopAgent:
    def __init__(
        self,
        config: AgentConfig,
        access_key: str,
        *,
        transfer_client: TransferAdapter | None = None,
        runtime: RuntimeServices | None = None,
    ) -> None:
        if config.backend_url is None or config.agent_id is None:
            raise ValueError("MatoLoop Agent connection is not configured")
        if transfer_client is not None and runtime is not None:
            raise ValueError("transfer_client and runtime cannot both be supplied")
        self.config = config
        self.runtime = runtime or current_platform().build_runtime(
            config,
            access_key,
            transfer_client=transfer_client,
        )
        self.platform = self.runtime.platform
        self.filesystem = self.runtime.filesystem
        self.cache = self.runtime.cache
        from .skill_cache import SkillCache

        self.skill_cache = SkillCache(self.cache)
        self.process = self.runtime.process
        self.terminal = self.runtime.terminal
        self.browser = self.runtime.browser
        self.search = self.runtime.search
        self.transfer = self.runtime.transfer
        self.openscad = self.runtime.openscad
        from .mcp import McpProcesses

        self.mcp = McpProcesses(self.cache, self.filesystem)
        self._access_key = access_key
        self.operations = list(self.runtime.operations)
        self._request_tasks: set[asyncio.Task] = set()
        self._maintenance_starting = False
        self._sleep_inhibitor = SleepInhibitor(self.platform.inhibit_idle_sleep)
        self.browser.hold_setup_awake = self._sleep_inhibitor.hold

    def hello(self) -> Hello:
        home = self.filesystem.resolve_directory(self.config.home_directory.as_posix())
        info = self.system_info(None)
        return Hello(
            agentId=self.config.agent_id,
            agentVersion=info["agentVersion"],
            computerName=self.platform.computer_name(),
            platform=self.platform.platform_id,
            homeDirectory=home["path"],
            operations=self.operations,
            systemInfo=SystemInfo.model_validate(
                {
                    field.alias or name: info.get(field.alias or name)
                    for name, field in SystemInfo.model_fields.items()
                    if (field.alias or name) in info
                }
            ),
        )

    def system_info(self, current_directory: str | None) -> dict[str, Any]:
        return self.platform.system_info(current_directory, self.filesystem)

    async def dispatch(
        self, request: Request, stream=None, *, backend_id: str | None = None
    ) -> dict[str, Any]:
        if request.operation not in self.operations:
            raise AgentError("operation.unsupported", "Operation is not supported")
        interrupt = request.operation == "maintenance.update.interrupt"
        maintenance = interrupt or request.operation in {"maintenance.start", "maintenance.update"}
        if (
            self._maintenance_starting
            or self.platform.maintenance_running(self.config)
            or (
                maintenance
                and not interrupt
                and (
                    self._request_tasks
                    or getattr(self.terminal, "maintenance_busy", False)
                    or getattr(self.browser, "maintenance_busy", False)
                    or self.mcp.maintenance_busy
                )
            )
        ):
            raise AgentError(
                "maintenance.busy", "Agent operations or maintenance are still running"
            )
        if maintenance:
            self._maintenance_starting = True
        task = asyncio.current_task()
        if not maintenance:
            self._request_tasks.add(task)
        try:
            if not maintenance:
                return await self._dispatch(request, stream, backend_id=backend_id)
            worker = asyncio.create_task(self._dispatch(request, stream, backend_id=backend_id))
            # Resolve launch admission even if the backend cancels its request.
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    pass
            return worker.result()
        finally:
            self._request_tasks.discard(task)
            if maintenance:
                self._maintenance_starting = False

    async def _interrupt_operations(self) -> None:
        tasks = tuple(self._request_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=MAINTENANCE_DRAIN_SECONDS)
            if pending:
                raise AgentError("maintenance.busy", "Agent operations have not finished stopping")
        await self.browser.close(stop_setup=True)
        await self.mcp.close_all()
        await self.terminal.close_all()

    async def _dispatch(
        self, request: Request, stream=None, *, backend_id: str | None = None
    ) -> dict[str, Any]:
        try:
            arguments = validate_arguments(request)
        except (ValidationError, ValueError) as exc:
            params = (
                argument_details(exc)
                if isinstance(exc, ValidationError)
                else {"reason": "invalid_value"}
            )
            if params.get("field") == "patch":
                params["field"] = "patchText"
            if request.operation.startswith("integration.browser."):
                raise AgentError(
                    "browser.invalidTarget", "Invalid browser arguments", params
                ) from exc
            raise AgentError("request.invalid", "Invalid operation arguments", params) from exc

        passive = request.operation in PASSIVE_OPERATIONS or (
            isinstance(arguments, GitChangesArguments) and arguments.status_only
        )
        with nullcontext() if passive else self._sleep_inhibitor.hold():
            return await self._execute_operation(request.operation, arguments, stream, backend_id)

    async def _execute_operation(
        self, operation: str, arguments: StrictModel, stream, backend_id: str | None
    ) -> dict[str, Any]:
        if isinstance(arguments, AdministratorArguments):
            from .administrator import run_administrator

            return await run_administrator(self.config, arguments, stream)
        if operation == "shell.classify":
            return self.process.inspect_shell(
                arguments.command, arguments.current_directory, resolve_paths=False
            )

        if operation in BROWSER_ARGUMENT_MODELS:
            return await self.browser.dispatch(operation, arguments)
        if operation.startswith("integration.mcp."):
            if backend_id is None:
                raise AgentError("request.invalid", "MCP requires an authenticated backend binding")
            return await self.mcp.dispatch(operation, arguments, backend_id)
        if isinstance(arguments, SystemInfoArguments):
            return await asyncio.to_thread(self.system_info, arguments.current_directory)
        if isinstance(arguments, MaintenanceStartArguments):
            return await self.platform.start_maintenance(
                self.config,
                arguments.action,
                arguments.job_id,
                arguments.sudo_password,
                arguments.revision,
                **(
                    {"before_start": self._interrupt_operations}
                    if operation == "maintenance.update.interrupt"
                    else {}
                ),
            )
        if isinstance(arguments, ResolveDirectoryArguments):
            return await asyncio.to_thread(self.filesystem.resolve_directory, arguments.path)
        if isinstance(arguments, OpenScadDiscoverArguments):
            return self.openscad.discover()
        if isinstance(arguments, OpenScadTestArguments):
            return await self.openscad.test(arguments.executable_path)

        if isinstance(arguments, StatArguments):
            return await asyncio.to_thread(self.filesystem.stat, arguments.path)
        if isinstance(arguments, GitChangesArguments):
            from .git import changes

            return await changes(
                self.filesystem, arguments.path, arguments.file, status_only=arguments.status_only
            )
        if operation == "git.repository":
            from .git import _check_removal_permissions, project_worktree_directory, repository

            result = {
                **await repository(
                    self.filesystem, arguments.path, target_branch=arguments.target_branch
                ),
                "worktreeRoot": (self.config.data_dir / "worktrees").as_posix(),
            }
            if arguments.check_removal and result["repository"] is not None:
                result["removalChecked"] = True
                result["removalError"] = None
                try:
                    await asyncio.to_thread(
                        _check_removal_permissions,
                        result["repository"],
                        result["gitDirectory"]
                        if result["gitDirectory"] != result["commonDirectory"]
                        else None,
                    )
                except AgentError as exc:
                    if exc.code != "git.worktreePermissionDenied":
                        raise
                    result["removalError"] = {"code": exc.code, "params": exc.params}
            if arguments.project_id is not None:
                result["projectWorktreeDirectory"] = (
                    await asyncio.to_thread(
                        project_worktree_directory,
                        self.config,
                        self.filesystem,
                        arguments.project_id,
                    )
                ).as_posix()
            return result
        if operation == "git.worktree":
            from .git import prepare_worktree

            return await prepare_worktree(self.config, self.filesystem, arguments)
        if operation == "git.branch":
            from .git import branch_action

            return await branch_action(self.config, self.filesystem, arguments)
        if operation == "git.worktree.remove":
            from .git import remove_worktree

            return await remove_worktree(self.config, self.filesystem, arguments)
        if isinstance(arguments, ListArguments):
            return await asyncio.to_thread(
                self.filesystem.list,
                arguments.path,
                arguments.offset,
                arguments.limit,
            )
        if isinstance(arguments, ReadArguments):
            return await asyncio.to_thread(
                self.filesystem.read,
                arguments.path,
                arguments.offset,
                arguments.limit,
                arguments.max_line_length,
            )
        if isinstance(arguments, GlobArguments):
            return await self.search.glob(
                arguments.path,
                arguments.pattern,
            )
        if isinstance(arguments, GrepArguments):
            return await self.search.grep(
                arguments.path,
                arguments.pattern,
                arguments.include,
            )
        if isinstance(arguments, WriteArguments):
            return await _run_mutation(
                self.filesystem.write,
                arguments.path,
                arguments.content,
                arguments.expected_sha256,
            )
        if isinstance(arguments, UpsertArguments):
            return await _run_mutation(
                self.filesystem.upsert,
                arguments.path,
                arguments.content,
            )
        if isinstance(arguments, EditArguments):
            return await _run_mutation(
                self.filesystem.edit,
                arguments.path,
                arguments.old_text,
                arguments.new_text,
                arguments.replace_all,
                arguments.expected_sha256,
            )
        if isinstance(arguments, PatchArguments):
            return await _run_mutation(
                self.filesystem.patch,
                arguments.patch,
                arguments.expected_sha256,
            )
        if isinstance(arguments, MkdirArguments):
            return await _run_mutation(
                self.filesystem.mkdir,
                arguments.path,
                arguments.parents,
            )
        if isinstance(arguments, CopyMoveArguments):
            return await _run_mutation(
                self.filesystem.copy_or_move,
                arguments.path,
                arguments.destination,
                move=operation == "filesystem.move",
            )
        if isinstance(arguments, RenameArguments):
            return await _run_mutation(
                self.filesystem.rename,
                arguments.path,
                arguments.destination,
            )
        if isinstance(arguments, DeleteArguments):
            return await _run_mutation(
                self.filesystem.delete,
                arguments.path,
                arguments.recursive,
            )
        if isinstance(arguments, ImportArguments):
            transfer_dir = self.config.data_dir / "transfers"
            temporary = await self.transfer.download(
                arguments.transfer,
                transfer_dir,
                expected_size=arguments.size,
                expected_sha256=arguments.sha256,
            )
            try:
                return await _run_mutation(
                    self.filesystem.import_file,
                    arguments.path,
                    temporary,
                    arguments.overwrite,
                    arguments.size,
                    arguments.sha256,
                )
            finally:
                temporary.unlink(missing_ok=True)
        if isinstance(arguments, ExportArguments):
            export = self.filesystem.open_export(arguments.path)
            handle, size, digest = await asyncio.to_thread(export.__enter__)
            try:
                if arguments.expected_sha256 is not None and digest != arguments.expected_sha256:
                    raise AgentError("file.changed", "File changed since it was selected")
                await self.transfer.upload(
                    arguments.transfer,
                    handle,
                    size=size,
                    sha256=digest,
                )
            finally:
                await asyncio.to_thread(export.__exit__, None, None, None)
            return {"size": size, "sha256": digest}
        if isinstance(arguments, CacheEnsureArguments):
            if arguments.artifact.backend_id != backend_id:
                raise AgentError("cache.invalidDescriptor", "Artifact backend does not match")
            return await self.cache.ensure(arguments.artifact, arguments.transfer, self.transfer)
        if isinstance(arguments, SkillCachePrepareArguments):
            return await self.skill_cache.prepare(arguments, backend_id, self.transfer)
        if isinstance(arguments, SkillCacheReleaseArguments):
            return await self.skill_cache.release(
                backend_id, arguments.session_id, arguments.run_id
            )
        if isinstance(arguments, CacheExportArguments):
            if arguments.artifact.backend_id != backend_id:
                raise AgentError("cache.invalidDescriptor", "Artifact backend does not match")
            export = self.cache.open_export(arguments.artifact)
            handle, size, digest = await asyncio.to_thread(export.__enter__)
            try:
                try:
                    await self.transfer.upload(
                        arguments.transfer,
                        handle,
                        size=size,
                        sha256=digest,
                    )
                except AgentError as exc:
                    if exc.code.startswith("transfer."):
                        raise AgentError(
                            "cache.exportFailed", "Cached artifact upload failed"
                        ) from exc
                    raise
            finally:
                await asyncio.to_thread(export.__exit__, None, None, None)
            return {"size": size, "sha256": digest}
        if isinstance(arguments, CacheStoreArguments):
            if arguments.artifact.backend_id != backend_id:
                raise AgentError("cache.invalidDescriptor", "Artifact backend does not match")
            export = self.filesystem.open_export(arguments.path)
            handle, _, _ = await asyncio.to_thread(export.__enter__)
            try:
                return await _run_mutation(self.cache.store, arguments.artifact, handle)
            finally:
                await asyncio.to_thread(export.__exit__, None, None, None)
        if isinstance(arguments, ShellArguments):
            if stream is None:

                async def stream(_channel: str, _data: str) -> None:
                    return None

            return await self.process.exec(
                current_directory=arguments.current_directory,
                command=arguments.command,
                timeout=arguments.timeout_seconds,
                stream=stream,
                expected_inspection_revision=arguments.expected_inspection_revision,
                environment=arguments.environment,
                temporary_session_id=arguments.temporary_session_id,
            )
        if isinstance(arguments, ShellInspectArguments):
            return await asyncio.to_thread(
                self.process.inspect_shell,
                arguments.command,
                arguments.current_directory,
            )
        if isinstance(arguments, TerminalOpenArguments):
            return await self.terminal.open(
                arguments.rows, arguments.columns, terminal_id=arguments.terminal_id
            )
        if isinstance(arguments, TerminalReadArguments):
            return await self.terminal.read(
                arguments.terminal_id,
                arguments.max_bytes,
                arguments.wait_seconds,
            )
        if isinstance(arguments, TerminalWriteArguments):
            return await self.terminal.write(arguments.terminal_id, arguments.data)
        if isinstance(arguments, TerminalResizeArguments):
            return await self.terminal.resize(
                arguments.terminal_id,
                arguments.rows,
                arguments.columns,
            )
        if isinstance(arguments, TerminalCloseArguments):
            return await self.terminal.close(arguments.terminal_id)
        if isinstance(arguments, TerminalRunArguments):
            if stream is None:

                async def stream(_channel: str, _data: str) -> None:
                    return None

            return await self.terminal.run(
                terminal_id=arguments.terminal_id,
                current_directory=arguments.current_directory,
                rows=arguments.rows,
                columns=arguments.columns,
                command=arguments.command,
                timeout=arguments.timeout_seconds,
                expected_inspection_revision=arguments.expected_inspection_revision,
                stream=stream,
            )
        if isinstance(arguments, OpenScadRenderArguments):
            if backend_id is None:
                raise AgentError("cache.invalidDescriptor", "Backend identity is unavailable")
            return await self.openscad.render(
                arguments,
                backend_id,
                six_views=operation == "integration.openscad.render6",
            )
        # CacheClearArguments has no fields and is intentionally last.
        if operation == "cache.clear":
            return await _run_mutation(lambda: {"removed": self.cache.clear()})
        raise AgentError("operation.unsupported", "Operation is not supported")

    async def run_forever(
        self, access_key_loader: Callable[[], Awaitable[str]] | None = None
    ) -> None:
        try:
            self.cache.evict()
            delay = 1.0
            async with self.transfer:
                while True:
                    connected = asyncio.Event()
                    try:
                        if access_key_loader is not None:
                            self._access_key = await access_key_loader()
                            self.transfer.set_access_key(self._access_key)
                        await self._connect_once(connected)
                        delay = 1.0
                        await asyncio.sleep(delay)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        if connected.is_set():
                            delay = 1.0
                        logger.warning("MatoLoop Agent connection failed; retrying")
                        await asyncio.sleep(delay)
                        delay = min(delay * 2, 30.0)
        finally:
            self._sleep_inhibitor.close()
            try:
                await self.mcp.close_all()
            finally:
                try:
                    await self.browser.close()
                finally:
                    await self.terminal.close_all()

    async def _connect_once(self, connected: asyncio.Event) -> None:
        # Deliver completed updates before hello can trigger another automatic update.
        await replay_maintenance_results(self.config, self._access_key)
        async with websockets.connect(
            self.config.backend_url,
            additional_headers={"Authorization": f"Bearer {self._access_key}"},
            max_size=self.config.max_message_bytes,
            ping_interval=20,
            ping_timeout=20,
            open_timeout=15,
            close_timeout=2,
        ) as socket:
            send_lock = asyncio.Lock()

            async def send(message) -> None:
                async with send_lock:
                    await socket.send(serialize_message(message, self.config.max_message_bytes))

            await send(self.hello())
            try:
                first = parse_inbound(await asyncio.wait_for(socket.recv(), 15))
            except (ValidationError, TimeoutError) as exc:
                raise AgentError("protocol.invalid", "Backend welcome was invalid") from exc
            if not isinstance(first, Welcome):
                raise AgentError("protocol.invalid", "Backend did not send welcome")
            if first.agent_id != self.config.agent_id:
                raise AgentError("protocol.invalid", "Backend welcomed a different agent")
            connected.set()

            tasks: dict[str, asyncio.Task] = {}
            completed: set[str] = set()

            async def execute(request: Request) -> None:
                async def stream(channel: str, data: str) -> None:
                    await send(Event(id=request.id, event=channel, data=data))

                try:
                    result = await self.dispatch(request, stream, backend_id=first.agent_id)
                    await send(Response(id=request.id, ok=True, result=result))
                except asyncio.CancelledError:
                    with suppress(Exception):
                        await send(
                            Response(
                                id=request.id,
                                ok=False,
                                error=Error(
                                    code="browser.outcomeUnknown"
                                    if request.operation in BROWSER_ARGUMENT_MODELS
                                    else "operation.cancelled"
                                ),
                            )
                        )
                    raise
                except AgentError as exc:
                    code = exc.code
                    if (
                        request.operation in BROWSER_ARGUMENT_MODELS
                        and code != "maintenance.busy"
                        and not code.startswith("browser.")
                    ):
                        code = (
                            "browser.fileTooLarge"
                            if code == "operation.tooLarge"
                            else "browser.actionFailed"
                        )
                    await send(
                        Response(
                            id=request.id,
                            ok=False,
                            error=Error(code=code, params=exc.params),
                        )
                    )
                except Exception as exc:
                    logger.error("Execution operation failed")
                    await send(
                        Response(
                            id=request.id,
                            ok=False,
                            error=Error(
                                code="browser.actionFailed"
                                if request.operation in BROWSER_ARGUMENT_MODELS
                                else "path.accessDenied"
                                if isinstance(exc, PermissionError)
                                and request.operation.startswith("filesystem.")
                                else "operation.failed"
                            ),
                        )
                    )
                finally:
                    tasks.pop(request.id, None)
                    completed.add(request.id)
                    if len(completed) > 10_000:
                        completed.clear()

            async def report_maintenance() -> None:
                while True:
                    await asyncio.sleep(30)
                    await replay_maintenance_results(self.config, self._access_key)

            reporter = asyncio.create_task(report_maintenance())
            try:
                async for raw in socket:
                    try:
                        message = parse_inbound(raw)
                    except ValidationError:
                        await socket.close(code=1007, reason="Invalid protocol message")
                        break
                    if isinstance(message, Request):
                        if message.id in tasks or message.id in completed:
                            await send(
                                Response(
                                    id=message.id,
                                    ok=False,
                                    error=Error(code="request.duplicate"),
                                )
                            )
                        else:
                            tasks[message.id] = asyncio.create_task(execute(message))
                    elif isinstance(message, Cancel):
                        task = tasks.get(message.id)
                        if task is not None:
                            task.cancel()
                    else:
                        await socket.close(code=1007, reason="Unexpected protocol message")
                        break
            finally:
                try:
                    reporter.cancel()
                    await asyncio.gather(reporter, return_exceptions=True)
                    for task in tasks.values():
                        task.cancel()
                    try:
                        await self.mcp.close_all()
                    finally:
                        try:
                            await self.browser.close()
                        finally:
                            await self.terminal.close_all()
                    await asyncio.gather(*tasks.values(), return_exceptions=True)
                    self.skill_cache.disconnect()
                finally:
                    # Browser setup is deliberately independent of the backend connection.
                    setup = self.browser.setup_task
                    if setup is None or setup.done():
                        self._sleep_inhibitor.close()
