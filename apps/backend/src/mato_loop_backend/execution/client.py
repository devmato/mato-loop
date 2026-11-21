from __future__ import annotations

import asyncio
import base64
import binascii
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, BinaryIO, ClassVar, Literal, Self
from uuid import UUID

from anyio import CancelScope
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from mato_loop_backend.execution.manager import (
    ExecutionAgentError,
    execution_agent_connections,
)
from mato_loop_backend.execution.paths import absolute_path
from mato_loop_backend.execution.paths import parent as computer_parent
from mato_loop_backend.execution.protocol import MCP_MANAGE_OPERATION, EventMessage
from mato_loop_backend.execution.transfers import (
    EXPORT_TIMEOUT_SECONDS,
    TransferError,
    stage_file,
    transfer_tickets,
)


class ExecutionResult(BaseModel):
    # Missing newly required wire fields identify an older operation contract.
    upgrade_fields: ClassVar[frozenset[str]] = frozenset()
    model_config = ConfigDict(
        alias_generator=lambda value: value.split("_")[0]
        + "".join(part.capitalize() for part in value.split("_")[1:]),
        populate_by_name=True,
        extra="ignore",
        strict=True,
    )


def _absolute_path(value: str) -> str:
    return absolute_path(value)


class WorkspaceEntry(ExecutionResult):
    name: str
    path: str
    kind: Literal["file", "directory", "symlink", "other"]
    size: int | None = Field(ge=0)
    modified_at: float

    _validate_path = field_validator("path")(_absolute_path)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not value or "/" in value or value in {".", ".."}:
            raise ValueError("invalid entry name")
        return value


class WorkspaceListResult(ExecutionResult):
    entries: list[WorkspaceEntry]
    has_more: bool
    next_offset: int | None = Field(default=None, ge=0)


class WorkspaceReadResult(ExecutionResult):
    content: str
    lines_read: int = Field(ge=0)
    has_more: bool
    next_offset: int | None = Field(default=None, ge=1)
    truncated_lines: list[int]
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class WorkspaceGlobResult(ExecutionResult):
    paths: list[str] = Field(max_length=100)
    truncated: bool

    _validate_paths = field_validator("paths")(
        lambda values: [_absolute_path(value) for value in values]
    )


class WorkspaceGrepMatch(ExecutionResult):
    path: str
    line: int = Field(ge=1)
    text: str = Field(max_length=2_000)

    _validate_path = field_validator("path")(_absolute_path)


class WorkspaceGrepResult(ExecutionResult):
    matches: list[WorkspaceGrepMatch] = Field(max_length=100)
    truncated: bool


class WorkspacePathResult(ExecutionResult):
    path: str

    _validate_path = field_validator("path")(_absolute_path)


class WorkspaceWriteResult(WorkspacePathResult):
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(ge=0)


class WorkspacePatchFile(WorkspaceWriteResult):
    operation: Literal["add", "update", "move", "delete"]
    destination: str | None = None

    @field_validator("destination")
    @classmethod
    def validate_destination(cls, value: str | None) -> str | None:
        return _absolute_path(value) if value is not None else None


class WorkspacePatchResult(ExecutionResult):
    files: list[WorkspacePatchFile] = Field(min_length=1, max_length=1_000)


class WorkspaceExportResult(ExecutionResult):
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(ge=0)


class WorkspaceStatResult(WorkspacePathResult):
    kind: Literal["file", "directory", "symlink", "other"]
    size: int | None = Field(ge=0)
    modified_at: float


class WorkspaceDeleteResult(ExecutionResult):
    pass


class ProcessResult(ExecutionResult):
    exit_code: int
    timed_out: bool
    duration_ms: int = Field(ge=0)
    stdout_bytes: int = Field(ge=0)
    stderr_bytes: int = Field(ge=0)
    stdout_truncated: bool
    stderr_truncated: bool


class TerminalOpenResult(ExecutionResult):
    terminal_id: UUID = Field(strict=False)


class TerminalReadResult(ExecutionResult):
    data: str = Field(max_length=87_384)
    eof: bool
    exit_code: int | None = None

    @field_validator("data")
    @classmethod
    def validate_data(cls, value: str) -> str:
        try:
            decoded = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("invalid terminal data") from exc
        if len(decoded) > 65_536:
            raise ValueError("terminal data exceeds requested maximum")
        return value


class TerminalControlResult(ExecutionResult):
    pass


class DirectoryResult(ExecutionResult):
    path: str

    _validate_path = field_validator("path")(_absolute_path)


class SystemInfoResult(ExecutionResult):
    agent_version: str
    platform: str
    operating_system: str | None = None
    kernel: str | None = None
    architecture: str | None = None
    logical_cpu_count: int | None = None
    distribution: str | None = Field(default=None, min_length=1, max_length=255)
    cpu_model: str | None = Field(default=None, min_length=1, max_length=255)
    total_memory_bytes: int | None = Field(default=None, ge=1, le=9223372036854775807)
    memory: dict[str, int] | None = None
    disk: dict[str, Any] | None = None


class ShellTarget(ExecutionResult):
    path: str
    kind: Literal[
        "directory", "input", "output", "executable", "source", "script", "path",
        "toolchain_metadata", "discard",
        "path",
    ]
    access: Literal["read", "write"] | None = None

    _validate_path = field_validator("path")(_absolute_path)

    @model_validator(mode="after")
    def validate_special_target(self) -> Self:
        if not self.path.startswith("/") and self.kind in {"discard", "toolchain_metadata"}:
            raise ValueError("POSIX-only shell target on Windows")
        if self.kind == "discard" and self.path != "/dev/null":
            raise ValueError("invalid shell discard target")
        if self.kind == "toolchain_metadata":
            directories = {
                "/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin",
            }
            if self.path not in directories and computer_parent(self.path) not in directories:
                raise ValueError("invalid shell toolchain metadata target")
        if self.kind in {"toolchain_metadata", "discard"} and self.access is not None:
            raise ValueError("invalid shell special target access")
        if self.kind in {"input", "source", "script", "executable"} and self.access not in {
            None,
            "read",
        }:
            raise ValueError("invalid shell read target access")
        if self.kind == "output" and self.access not in {None, "write"}:
            raise ValueError("invalid shell output target access")
        return self


class ShellCommand(ExecutionResult):
    tokens: list[str] = Field(min_length=1, max_length=256)
    prefix: list[str] | None = Field(default=None, min_length=1, max_length=8)
    classification: Literal["read", "write"]

    @field_validator("tokens", "prefix")
    @classmethod
    def validate_tokens(cls, value: list[str] | None, info: ValidationInfo) -> list[str] | None:
        # Arguments are semantic data, never reparsed as shell source or grant prefixes.
        if value is not None and any(
            not token
            or len(token) > 1_024
            or any(
                (ord(character) < 32 and not (
                    info.field_name == "tokens" and index > 0 and character in "\n\t"
                ))
                or 127 <= ord(character) <= 159
                for character in token
            )
            for index, token in enumerate(value)
        ):
            raise ValueError("invalid shell token")
        return value


class ShellInspectResult(ExecutionResult):
    upgrade_fields = frozenset({"requiredCategories"})
    required_categories: list[Literal["read", "write", "delete"]] = Field(min_length=1, max_length=3)
    targets: list[ShellTarget]
    analysis_complete: bool
    classification: Literal["read", "write"]
    read_only: bool = False
    commands: list[ShellCommand] = Field(max_length=64)
    inspection_revision: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_classification(self) -> Self:
        if len(set(self.required_categories)) != len(self.required_categories) or (
            bool(set(self.required_categories) & {"write", "delete"}) != (self.classification == "write")
        ):
            raise ValueError("inconsistent shell categories")
        if self.read_only != (self.classification == "read") or (
            self.classification == "read"
            and (
                any(command.classification == "write" for command in self.commands)
                or any(target.access == "write" for target in self.targets)
            )
        ):
            raise ValueError("inconsistent shell classification")
        return self


class ExecutionClientError(Exception):
    def __init__(self, code: str, params: dict[str, Any] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.params = dict(params) if params is not None else None
        self.field = self.params.pop("field", None) if self.params and code == "tool.invalidArguments" else None


_REMOTE_ERROR_CODES = {
    **{code: code for code in (
        "mcp.configurationInvalid", "mcp.connectionFailed", "mcp.invalidResponse",
        "mcp.protocolError", "mcp.refreshFailed", "mcp.responseTooLarge",
        "mcp.timeout", "mcp.toolNotFound", "mcp.unavailable", "mcp.outcomeUnknown",
        "mcp.processLimitReached", "mcp.busy", "mcp.startFailed",
    )},
    "browser.unavailable": "browser.unavailable",
    "browser.notReady": "browser.notReady",
    "browser.busy": "browser.busy",
    "browser.notConnected": "browser.notConnected",
    "browser.bindingChanged": "browser.bindingChanged",
    "browser.snapshotChanged": "browser.snapshotChanged",
    "browser.observationUnstable": "browser.observationUnstable",
    "browser.setupFailed": "browser.setupFailed",
    "browser.dependenciesMissing": "browser.dependenciesMissing",
    "browser.unsupported": "browser.unsupported",
    "browser.sandboxUnavailable": "browser.sandboxUnavailable",
    "browser.actionFailed": "browser.actionFailed",
    "browser.invalidTarget": "browser.invalidTarget",
    "browser.fileTooLarge": "browser.fileTooLarge",
    "browser.profileOwnerMismatch": "browser.profileOwnerMismatch",
    "browser.profileInUse": "browser.profileInUse",
    "browser.outcomeUnknown": "browser.outcomeUnknown",
    "browser.downloadPermissionRequired": "browser.downloadPermissionRequired",
    "file.changed": "file.changed",
    "file.editTextNotFound": "file.editTextNotFound",
    "file.editTextNotUnique": "file.editTextNotUnique",
    "file.invalidEncoding": "file.invalidEncoding",
    "file.patchContextMismatch": "file.patchContextMismatch",
    "file.patchIncomplete": "file.patchIncomplete",
    "file.tooLarge": "file.tooLarge",
    "cache.exportFailed": "cache.exportFailed",
    "cache.invalidContent": "cache.invalidContent",
    "cache.invalidDescriptor": "cache.invalidDescriptor",
    "cache.invalidPath": "cache.invalidPath",
    "cache.notFound": "cache.notFound",
    "openscad.headlessPreviewUnavailable": "openscad.previewUnavailable",
    "openscad.invalidExecutable": "openscad.invalidExecutable",
    "openscad.invalidArchive": "openscad.invalidArchive",
    "openscad.invalidOutput": "openscad.invalidArtifact",
    "openscad.notFound": "openscad.notFound",
    "openscad.previewFailed": "openscad.previewFailed",
    "openscad.projectTooLarge": "openscad.projectTooLarge",
    "openscad.renderFailed": "openscad.renderFailed",
    "openscad.startFailed": "openscad.renderFailed",
    "openscad.testFailed": "openscad.testFailed",
    "openscad.timedOut": "openscad.renderFailed",
    "openscad.unavailable": "openscad.unavailable",
    "openscad.unsafeProject": "openscad.unsafeProject",
    "openscad.versionFailed": "openscad.testFailed",
    "operation.failed": "tool.executionFailed",
    "operation.tooLarge": "file.operationTooLarge",
    "path.exists": "file.pathAlreadyExists",
    "path.accessDenied": "file.accessDenied",
    "path.invalid": "file.invalidPath",
    "path.invalidDestination": "file.invalidDestination",
    "path.notDirectory": "file.directoryNotFound",
    "path.notEmpty": "file.directoryNotEmpty",
    "path.notFile": "file.notAFile",
    "path.notFound": "file.pathNotFound",
    "path.outsideRoots": "file.invalidPath",
    "path.rootMutation": "file.rootMutationForbidden",
    "path.symlinkUnsupported": "file.symlinkForbidden",
    "path.unsupportedType": "file.unsupportedType",
    "path.unavailable": "file.unavailable",
    "process.notAllowed": "process.notAllowed",
    "process.sudoUnavailable": "process.sudoUnavailable",
    "process.sudoAuthRequired": "process.sudoAuthRequired",
    "process.sudoAuthFailed": "process.sudoAuthFailed",
    "process.inspectionChanged": "process.inspectionChanged",
    "process.invalidSyntax": "process.invalidSyntax",
    "process.inspectionLimitExceeded": "process.inspectionLimitExceeded",
    "process.unsupportedTargetQuoting": "process.unsupportedTargetQuoting",
    "process.outputTooLarge": "process.outputTooLarge",
    "process.startFailed": "process.startFailed",
    "process.timedOut": "process.timedOut",
    "process.unavailable": "process.unavailable",
    "request.invalid": "tool.invalidArguments",
    "root.stale": "executionAgent.unavailable",
    "root.unavailable": "executionAgent.unavailable",
    "root.unknown": "executionAgent.unavailable",
    "terminal.closed": "terminal.disconnected",
    "terminal.exists": "terminal.commandActive",
    "terminal.limitReached": "terminal.unavailable",
    "terminal.notFound": "terminal.disconnected",
    "terminal.busy": "terminal.commandActive",
    "terminal.stale": "approval.targetChanged",
    "operation.unsupported": "executionAgent.operationUnavailable",
    "maintenance.busy": "maintenance.busy",
}

_MANAGER_ERROR_CODES = frozenset(
    {
        "mcp.outcomeUnknown",
        "browser.outcomeUnknown",
        "executionAgent.operationUnavailable",
        "executionAgent.outcomeUnknown",
        "executionAgent.protocolError",
        "executionAgent.replaced",
        "executionAgent.requestTooLarge",
        "executionAgent.responseTooLarge",
        "executionAgent.revoked",
        "executionAgent.timeout",
        "executionAgent.unavailable",
        "process.outcomeUnknown",
        "process.outputTooLarge",
    }
)


@dataclass(slots=True)
class ExecutionClient:
    agent_id: UUID
    operations: frozenset[str]
    current_directory: str | None = None
    expected_generation: int | None = None

    def supports(self, operation: str) -> bool:
        return operation in self.operations

    async def read_bytes(self, path: str, *, limit: int) -> bytes:
        with await self.open_export(path, limit=limit) as content:
            return content.read(limit + 1)

    async def open_export(self, path: str, *, limit: int) -> BinaryIO:
        stat_result = await self.request(
            "filesystem.stat", {"path": path}, WorkspaceStatResult
        )
        assert isinstance(stat_result, WorkspaceStatResult)
        if stat_result.kind != "file":
            raise ExecutionClientError("file.notAFile")
        if stat_result.size is None or stat_result.size > limit:
            raise ExecutionClientError("file.tooLarge")
        grant, ticket = transfer_tickets.create_upload(
            self.agent_id,
            size=stat_result.size,
            ttl_seconds=EXPORT_TIMEOUT_SECONDS + 30,
        )
        try:
            result = await self.request(
                "filesystem.export",
                {
                    "path": path,
                    "transfer": {
                        "url": execution_agent_connections.transfer_url(
                            self.agent_id, grant.ticket_id
                        ),
                        "token": grant.token,
                    },
                },
                WorkspaceExportResult,
                timeout=EXPORT_TIMEOUT_SECONDS,
            )
            assert isinstance(result, WorkspaceExportResult)
            if (
                result.size != ticket.actual_size
                or result.sha256 != ticket.actual_sha256
                or ticket.content is None
            ):
                raise ExecutionClientError("executionAgent.protocolError")
            content = ticket.content
            ticket.content = None
            return content
        finally:
            transfer_tickets.revoke(grant.ticket_id)

    async def write_bytes(
        self, path: str, source: BinaryIO, *, limit: int
    ) -> WorkspaceWriteResult:
        staging = asyncio.create_task(asyncio.to_thread(stage_file, source, max_bytes=limit))
        try:
            staged, size, digest = await asyncio.shield(staging)
        except asyncio.CancelledError:
            while not staging.done():
                try:
                    with CancelScope(shield=True):
                        await asyncio.shield(staging)
                except (asyncio.CancelledError, Exception):
                    continue
            with suppress(Exception):
                staging.result()[0].close()
            raise
        except TransferError as exc:
            raise ExecutionClientError(
                "file.tooLarge"
                if exc.code == "transfer.tooLarge"
                else "executionAgent.protocolError"
            ) from exc
        grant = None
        try:
            grant, _ = transfer_tickets.create_download(
                self.agent_id, staged, size=size, sha256=digest
            )
            result = await self.request(
                "filesystem.import",
                {
                    "path": path,
                    "transfer": {
                        "url": execution_agent_connections.transfer_url(
                            self.agent_id, grant.ticket_id
                        ),
                        "token": grant.token,
                    },
                    "size": size,
                    "sha256": digest,
                    "overwrite": False,
                },
                WorkspaceWriteResult,
                timeout=EXPORT_TIMEOUT_SECONDS,
            )
            assert isinstance(result, WorkspaceWriteResult)
            if result.path != path or result.size != size or result.sha256 != digest:
                raise ExecutionClientError("executionAgent.protocolError")
            return result
        finally:
            if grant is not None:
                transfer_tickets.revoke(grant.ticket_id)
            staged.close()

    async def request(
        self,
        operation: str,
        arguments: dict[str, Any],
        result_model: type[ExecutionResult],
        *,
        timeout: float | None = None,
        on_event: Callable[[EventMessage], Awaitable[None] | None] | None = None,
    ) -> ExecutionResult:
        if not self.supports(operation):
            raise ExecutionClientError("executionAgent.operationUnavailable")
        try:
            response = await execution_agent_connections.request(
                self.agent_id,
                operation,
                arguments,
                timeout=timeout,
                on_event=on_event,
                **({"expected_generation": self.expected_generation} if self.expected_generation is not None else {}),
            )
        except ExecutionAgentError as exc:
            raise ExecutionClientError(
                exc.code if exc.code in _MANAGER_ERROR_CODES else "executionAgent.unavailable"
            ) from exc
        if not response.ok:
            code = response.error.code if response.error is not None else None
            if code == "request.invalid" and operation in {
                "terminal.open", "terminal.read", "terminal.write", "terminal.resize", "terminal.close",
            }:
                mapped_code = "executionAgent.protocolError"
            elif (
                code == "integration.changed"
                and operation == MCP_MANAGE_OPERATION
                and arguments.get("action") == "close"
            ):
                mapped_code = code
            elif operation.startswith("integration.mcp.") and code in {"operation.failed", "operation.cancelled"}:
                mapped_code = "mcp.outcomeUnknown" if operation == "integration.mcp.call" else "mcp.unavailable"
            elif operation.startswith("integration.browser.") and code in {"operation.failed", "operation.cancelled"}:
                mapped_code = (
                    "browser.outcomeUnknown" if code == "operation.cancelled"
                    else "browser.actionFailed"
                )
            elif code == "operation.cancelled":
                mapped_code = (
                    "executionAgent.outcomeUnknown" if operation in {"git.worktree", "git.worktree.remove", "git.branch"} else
                    "process.interrupted"
                    if operation.startswith("shell.") or operation == "terminal.run"
                    else "tool.executionInterrupted"
                )
            elif code == "operation.failed" and (
                operation.startswith("shell.") or operation == "terminal.run"
            ):
                mapped_code = "process.failed"
            elif operation.startswith("git.") and code in {
                "git.notRepository", "git.repositoryRootRequired", "git.remoteRequired",
                "git.invalidBranch", "git.fetchFailed", "git.worktreeFailed",
                "git.worktreeChanged", "git.worktreeLocation",
                "git.localRemoteUnsupported", "git.localBranchRequired",
                "git.worktreeRemoveFailed", "git.worktreeInUse",
                "git.worktreePermissionDenied",
                "git.dirty", "git.branchRequired", "git.branchInUse", "git.branchPublished",
                "git.branchUnmerged", "git.targetRequired", "git.worktreeLocked", "git.mainWorktree",
                "git.actionFailed", "git.configurationRequired",
            }:
                mapped_code = code
            else:
                mapped_code = _REMOTE_ERROR_CODES.get(
                    code or "", "executionAgent.protocolError"
                )
            raise ExecutionClientError(
                mapped_code,
                response.error.params if response.error is not None
                and not operation.startswith(("integration.browser.", "integration.mcp.")) else None,
            )
        try:
            return result_model.model_validate(response.result)
        except ValidationError as exc:
            if all(error["type"] == "missing" and len(error["loc"]) == 1
                   and error["loc"][0] in getattr(result_model, "upgrade_fields", ()) for error in exc.errors()):
                raise ExecutionClientError("executionAgent.updateRequired") from exc
            raise ExecutionClientError("executionAgent.protocolError") from exc


__all__ = [
    "DirectoryResult",
    "ExecutionClient",
    "ExecutionClientError",
    "ProcessResult",
    "ShellInspectResult",
    "SystemInfoResult",
    "TerminalControlResult",
    "TerminalOpenResult",
    "TerminalReadResult",
    "WorkspaceDeleteResult",
    "WorkspaceExportResult",
    "WorkspaceGlobResult",
    "WorkspaceGrepResult",
    "WorkspaceListResult",
    "WorkspacePatchResult",
    "WorkspacePathResult",
    "WorkspaceReadResult",
    "WorkspaceStatResult",
    "WorkspaceWriteResult",
]
