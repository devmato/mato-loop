from __future__ import annotations

import base64
import binascii
import sys
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator
from pydantic_core import PydanticCustomError

from .windows_paths import absolute

PROTOCOL_VERSION = 1
UUID_PATTERN = (
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)
PROTECTED_ENVIRONMENT_NAMES = {
    "BASHOPTS",
    "BASH_ENV",
    "CDPATH",
    "CLASSPATH",
    "ENV",
    "GLOBIGNORE",
    "HOME",
    "IFS",
    "JAVA_TOOL_OPTIONS",
    "LANG",
    "LD_LIBRARY_PATH",
    "LD_PRELOAD",
    "LOGNAME",
    "NODE_OPTIONS",
    "NODE_PATH",
    "PATH",
    "PERL5LIB",
    "PERL5OPT",
    "PS4",
    "PWD",
    "PYTHONHOME",
    "PYTHONPATH",
    "RUBYLIB",
    "RUBYOPT",
    "SHELL",
    "SHELLOPTS",
    "USER",
    "ZDOTDIR",
    "_JAVA_OPTIONS",
}
EnvironmentName = Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")]
EnvironmentValue = Annotated[str, Field(max_length=65_536)]


def _bounded_unprotected_environment(
    value: dict[str, str] | None,
) -> dict[str, str] | None:
    if value is None:
        return None
    protected = PROTECTED_ENVIRONMENT_NAMES.intersection(value)
    if sys.platform == "win32":
        protected.update(
            name
            for name in value
            if name.upper() in PROTECTED_ENVIRONMENT_NAMES
            or name.upper().startswith(("DYLD_", "GIT_CONFIG_"))
            or name.upper()
            in {
                "PSMODULEPATH",
                "COMSPEC",
                "PATHEXT",
                "SYSTEMROOT",
                "TEMP",
                "TMP",
                "USERPROFILE",
                "APPDATA",
                "LOCALAPPDATA",
            }
        )
    protected.update(name for name in value if name.startswith(("DYLD_", "GIT_CONFIG_")))
    if protected:
        raise PydanticCustomError(
            "operation_argument_invalid",
            "Protected environment variable",
            {"hint": "Do not replace host identity or process startup environment variables."},
        )
    if any("\x00" in item or len(item.encode("utf-8")) > 65_536 for item in value.values()):
        raise PydanticCustomError(
            "operation_argument_invalid",
            "Invalid environment value",
            {
                "hint": "Use environment values without NUL characters "
                "and with at most 65536 UTF-8 bytes."
            },
        )
    return value


def _absolute_path(value: str) -> str:
    return absolute(value)


def _relative_path(value: str, *, allow_dot: bool = False) -> str:
    if allow_dot and value == ".":
        return value
    parsed = PurePosixPath(value)
    if (
        parsed.is_absolute()
        or "\\" in value
        or parsed.as_posix() != value
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in value)
    ):
        raise PydanticCustomError(
            "operation_argument_invalid",
            "Invalid relative path",
            {
                "hint": "Use a normalized relative POSIX path "
                "without traversal or control characters."
            },
        )
    return value


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)


class SystemInfo(StrictModel):
    session_temporary_directory_supported: bool = Field(
        default=False, alias="sessionTemporaryDirectorySupported"
    )
    temporary_directory: str | None = Field(
        default=None, alias="temporaryDirectory", max_length=4096
    )

    _temporary_is_absolute = field_validator("temporary_directory")(
        lambda value: _absolute_path(value) if value is not None else None
    )
    operating_system: str | None = Field(
        default=None, alias="operatingSystem", min_length=1, max_length=80
    )
    distribution: str | None = Field(default=None, min_length=1, max_length=255)
    kernel: str | None = Field(default=None, min_length=1, max_length=160)
    architecture: str | None = Field(default=None, min_length=1, max_length=80)
    cpu_model: str | None = Field(default=None, alias="cpuModel", min_length=1, max_length=255)
    logical_cpu_count: int | None = Field(
        default=None, alias="logicalCpuCount", ge=1, le=2147483647
    )
    total_memory_bytes: int | None = Field(
        default=None, alias="totalMemoryBytes", ge=1, le=9223372036854775807
    )


class Hello(StrictModel):
    type: Literal["hello"] = "hello"
    protocol_version: Literal[1] = Field(default=1, alias="protocolVersion")
    agent_id: str = Field(alias="agentId", pattern=UUID_PATTERN)
    agent_version: str = Field(alias="agentVersion", min_length=1, max_length=80)
    computer_name: str = Field(alias="computerName", min_length=1, max_length=255)
    platform: str = Field(min_length=1, max_length=80)
    home_directory: str = Field(alias="homeDirectory", min_length=1, max_length=4096)
    operations: list[str] = Field(max_length=64)
    system_info: SystemInfo | None = Field(default=None, alias="systemInfo")

    _home_is_absolute = field_validator("home_directory")(_absolute_path)

    @model_validator(mode="after")
    def platform_path(self):
        if self.home_directory.startswith("/") == (self.platform == "win32"):
            raise ValueError("home path does not match the agent platform")
        return self


class Welcome(StrictModel):
    type: Literal["welcome"]
    protocol_version: Literal[1] = Field(alias="protocolVersion")
    agent_id: str = Field(alias="agentId", pattern=UUID_PATTERN)


class Request(StrictModel):
    type: Literal["request"]
    id: str = Field(pattern=UUID_PATTERN)
    operation: str = Field(min_length=1, max_length=64)
    arguments: dict[str, Any]


class Cancel(StrictModel):
    type: Literal["cancel"]
    id: str = Field(pattern=UUID_PATTERN)


class Error(StrictModel):
    code: str = Field(pattern=r"^[a-z][A-Za-z]*(?:\.[a-z][A-Za-z]*)+$", max_length=120)
    params: dict[str, str | bool | int | float] | None = Field(default=None, max_length=20)


class Response(StrictModel):
    type: Literal["response"] = "response"
    id: str = Field(pattern=UUID_PATTERN)
    ok: bool
    result: dict[str, Any] | None = None
    error: Error | None = None

    @model_validator(mode="after")
    def consistent_result(self):
        if (
            self.ok == (self.error is not None)
            or (self.ok and self.result is None)
            or (not self.ok and self.result is not None)
        ):
            raise ValueError("response success and payload are inconsistent")
        return self


class Event(StrictModel):
    type: Literal["event"] = "event"
    id: str = Field(pattern=UUID_PATTERN)
    event: Literal["stdout", "stderr"]
    data: str = Field(max_length=65_536)


class PathArguments(StrictModel):
    path: str = Field(min_length=1, max_length=4096)

    _path_is_absolute = field_validator("path")(_absolute_path)


class StatArguments(PathArguments):
    pass


class GitChangesArguments(PathArguments):
    file: str | None = Field(default=None, min_length=1, max_length=4096)
    status_only: bool = Field(default=False, alias="statusOnly")

    @field_validator("file")
    @classmethod
    def relative_file(cls, value: str | None) -> str | None:
        return _relative_path(value) if value is not None else None


class GitRepositoryArguments(PathArguments):
    check_removal: bool = Field(default=False, alias="checkRemoval")
    target_branch: str | None = Field(
        default=None, alias="targetBranch", min_length=1, max_length=300
    )
    project_id: str | None = Field(default=None, alias="projectId", pattern=UUID_PATTERN)


class GitBranchArguments(PathArguments):
    action: Literal["create", "switch", "rename", "delete"]
    name: str = Field(min_length=1, max_length=300)
    target_branch: str | None = Field(default=None, alias="targetBranch", max_length=300)


class GitWorktreeRemoveArguments(PathArguments):
    common_directory: str = Field(alias="commonDirectory", min_length=1, max_length=4096)
    repository: str = Field(min_length=1, max_length=4096)
    force: bool = False

    _common_path = field_validator("common_directory")(_absolute_path)
    _repository_path = field_validator("repository")(_absolute_path)


class GitWorktreeArguments(PathArguments):
    common_directory: str = Field(alias="commonDirectory", min_length=1, max_length=4096)
    owner_id: str = Field(alias="ownerId", pattern=UUID_PATTERN)
    project_id: str = Field(alias="projectId", pattern=UUID_PATTERN)
    session_id: str = Field(alias="sessionId", pattern=UUID_PATTERN)
    target_branch: str = Field(alias="targetBranch", min_length=1, max_length=300)
    remote: str | None = Field(default=None, min_length=1, max_length=300)
    destinations: list[Annotated[str, Field(min_length=1, max_length=4096)]] = Field(
        min_length=1, max_length=10
    )

    _common_path = field_validator("common_directory")(_absolute_path)
    _destination_paths = field_validator("destinations")(
        lambda values: [_absolute_path(value) for value in values]
    )


class ListArguments(PathArguments):
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=1000)


class ReadArguments(PathArguments):
    offset: int = Field(default=1, ge=1)
    limit: int = Field(default=2000, ge=1, le=2000)
    max_line_length: int = Field(default=2000, alias="maxLineLength", ge=1, le=10_000)


class GlobArguments(PathArguments):
    pattern: str = Field(min_length=1, max_length=1000, pattern=r"^[^\x00]+$")


class GrepArguments(PathArguments):
    pattern: str = Field(min_length=1, max_length=4096, pattern=r"^[^\x00]+$")
    include: str | None = Field(default=None, min_length=1, max_length=1000, pattern=r"^[^\x00]+$")


class WriteArguments(PathArguments):
    content: str
    expected_sha256: str | None = Field(
        default=None, alias="expectedSha256", pattern=r"^[0-9a-f]{64}$"
    )


class UpsertArguments(PathArguments):
    content: str


class EditArguments(PathArguments):
    old_text: str = Field(alias="oldText", min_length=1)
    new_text: str = Field(alias="newText")
    replace_all: bool = Field(default=False, alias="replaceAll")
    expected_sha256: str | None = Field(
        default=None, alias="expectedSha256", pattern=r"^[0-9a-f]{64}$"
    )

    @model_validator(mode="after")
    def different_text(self):
        if self.old_text == self.new_text:
            raise PydanticCustomError(
                "operation_argument_invalid",
                "Edit has no change",
                {"hint": "newText must differ from oldText."},
            )
        return self


class PatchArguments(StrictModel):
    patch: str = Field(min_length=1, max_length=512 * 1024)
    expected_sha256: dict[str, Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]] | None = Field(
        default=None, alias="expectedSha256", max_length=1000
    )

    @field_validator("expected_sha256")
    @classmethod
    def absolute_revision_paths(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        for path in value or {}:
            if len(path) > 4096:
                raise PydanticCustomError(
                    "operation_argument_invalid",
                    "Revision path is too long",
                    {"hint": "Use revision paths of at most 4096 characters."},
                )
            _absolute_path(path)
        return value


class MkdirArguments(PathArguments):
    parents: bool = False


class RenameArguments(PathArguments):
    destination: str = Field(min_length=1, max_length=4096)

    _destination_is_absolute = field_validator("destination")(_absolute_path)


class DeleteArguments(PathArguments):
    recursive: bool = False


class CopyMoveArguments(PathArguments):
    destination: str = Field(min_length=1, max_length=4096)

    _destination_is_absolute = field_validator("destination")(_absolute_path)


class TransferSpec(StrictModel):
    url: str = Field(min_length=1, max_length=4096)
    token: str = Field(min_length=1, max_length=8192)


class ImportArguments(PathArguments):
    transfer: TransferSpec
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    overwrite: bool = False


class ExportArguments(PathArguments):
    transfer: TransferSpec
    expected_sha256: str | None = Field(
        default=None, alias="expectedSha256", pattern=r"^[0-9a-f]{64}$"
    )


class HostDirectoryArguments(StrictModel):
    current_directory: str = Field(alias="currentDirectory", min_length=1, max_length=4096)

    _cwd_is_absolute = field_validator("current_directory")(_absolute_path)


class ShellArguments(HostDirectoryArguments):
    temporary_session_id: str | None = Field(
        default=None, alias="temporarySessionId", pattern=UUID_PATTERN
    )
    command: str = Field(min_length=1, max_length=256 * 1024)
    timeout_seconds: float | None = Field(default=None, alias="timeoutSeconds", gt=0, le=3600)
    expected_inspection_revision: str | None = Field(
        default=None, alias="expectedInspectionRevision", pattern=r"^[0-9a-f]{64}$"
    )
    environment: dict[EnvironmentName, EnvironmentValue] | None = Field(default=None, max_length=32)

    _environment_is_bounded = field_validator("environment")(_bounded_unprotected_environment)


def _single_line_password(value: str | None) -> str | None:
    if value is not None and any(character in value for character in "\r\n\0"):
        raise PydanticCustomError(
            "operation_argument_invalid",
            "Invalid sudo password",
            {
                "hint": "Request a new single-line administrator password "
                "of at most 4096 characters without NUL characters."
            },
        )
    return value


class ResolveDirectoryArguments(PathArguments):
    pass


class SystemInfoArguments(StrictModel):
    current_directory: str | None = Field(
        default=None, alias="currentDirectory", min_length=1, max_length=4096
    )

    @field_validator("current_directory")
    @classmethod
    def absolute_directory(cls, value: str | None) -> str | None:
        return _absolute_path(value) if value is not None else None


class ShellInspectArguments(HostDirectoryArguments):
    command: str = Field(min_length=1, max_length=256 * 1024)


class AdministratorArguments(StrictModel):
    operation: Literal[
        "authenticate",
        "filesystem.read",
        "filesystem.write",
        "filesystem.upsert",
        "filesystem.edit",
        "filesystem.delete",
        "shell.inspect",
        "shell.run",
    ]
    arguments: dict[str, Any]
    sudo_password: str | None = Field(
        default=None, alias="sudoPassword", min_length=1, max_length=4096, repr=False
    )

    _password_is_single_line = field_validator("sudo_password")(_single_line_password)

    @model_validator(mode="after")
    def validate_operation(self):
        if self.operation == "authenticate":
            if self.arguments or self.sudo_password is not None:
                raise PydanticCustomError(
                    "operation_argument_invalid",
                    "Invalid authentication probe",
                    {"hint": "Do not supply arguments or a password for an authentication probe."},
                )
        else:
            ARGUMENT_MODELS[self.operation].model_validate(self.arguments)
        return self


class TerminalOpenArguments(StrictModel):
    terminal_id: str | None = Field(default=None, alias="terminalId", pattern=UUID_PATTERN)
    rows: int = Field(ge=1, le=1000)
    columns: int = Field(ge=1, le=1000)


class TerminalIdArguments(StrictModel):
    terminal_id: str = Field(alias="terminalId", pattern=UUID_PATTERN)


class TerminalReadArguments(TerminalIdArguments):
    max_bytes: int = Field(default=65_536, alias="maxBytes", ge=1, le=65_536)
    wait_seconds: float = Field(default=0, alias="waitSeconds", ge=0, le=30)


class TerminalWriteArguments(TerminalIdArguments):
    data: str = Field(max_length=87_384)

    @field_validator("data")
    @classmethod
    def bounded_base64(cls, value: str) -> str:
        try:
            decoded = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise PydanticCustomError(
                "operation_argument_invalid",
                "Invalid base64",
                {"hint": "Encode data as valid base64."},
            ) from exc
        if len(decoded) > 65_536:
            raise PydanticCustomError(
                "operation_argument_invalid",
                "Data is too large",
                {"hint": "Send at most 65536 decoded bytes per write."},
            )
        return value


class TerminalResizeArguments(TerminalIdArguments):
    rows: int = Field(ge=1, le=1000)
    columns: int = Field(ge=1, le=1000)


class TerminalCloseArguments(TerminalIdArguments):
    pass


class TerminalRunArguments(HostDirectoryArguments, TerminalIdArguments):
    rows: int = Field(ge=1, le=1000)
    columns: int = Field(ge=1, le=1000)
    command: str = Field(min_length=1, max_length=256 * 1024)
    timeout_seconds: float | None = Field(default=None, alias="timeoutSeconds", gt=0, le=3600)
    expected_inspection_revision: str | None = Field(
        default=None, alias="expectedInspectionRevision", pattern=r"^[0-9a-f]{64}$"
    )


class ArtifactDescriptor(StrictModel):
    artifact_id: str = Field(alias="artifactId", min_length=1, max_length=128)
    backend_id: str = Field(alias="backendId", min_length=1, max_length=128)
    session_id: str = Field(alias="sessionId", min_length=1, max_length=128)
    path: str = Field(min_length=1, max_length=1000, pattern=r"^[^\x00\\]+$")
    filename: str = Field(min_length=1, max_length=255)
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_type: str = Field(alias="contentType", min_length=1, max_length=255)

    @field_validator("path")
    @classmethod
    def relative_path(cls, value: str) -> str:
        return _relative_path(value)

    @field_validator("filename")
    @classmethod
    def plain_filename(cls, value: str) -> str:
        if value in {".", ".."} or "/" in value or "\\" in value or "\x00" in value:
            raise PydanticCustomError(
                "operation_argument_invalid",
                "Invalid artifact filename",
                {"hint": "Use a plain artifact filename without directories."},
            )
        return value


class CacheEnsureArguments(StrictModel):
    artifact: ArtifactDescriptor
    transfer: TransferSpec


class SkillCacheReleaseArguments(StrictModel):
    run_id: str = Field(alias="runId", pattern=UUID_PATTERN)
    session_id: str = Field(alias="sessionId", pattern=UUID_PATTERN)


class SkillCachePrepareArguments(SkillCacheReleaseArguments):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    size: int = Field(ge=1, le=8 * 1024 * 1024 + 256 * 1024)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    transfer: TransferSpec


class CacheStoreArguments(PathArguments):
    artifact: ArtifactDescriptor


class CacheClearArguments(StrictModel):
    pass


class CacheExportArguments(StrictModel):
    artifact: ArtifactDescriptor
    transfer: TransferSpec


class OpenScadDiscoverArguments(StrictModel):
    pass


class OpenScadTestArguments(StrictModel):
    executable_path: str = Field(alias="executablePath", min_length=1, max_length=4096)

    _executable_is_absolute = field_validator("executable_path")(_absolute_path)


class OpenScadWorkspaceSource(StrictModel):
    type: Literal["workspace"]
    path: str = Field(min_length=1, max_length=4096)

    _path_is_absolute = field_validator("path")(_absolute_path)


class OpenScadArchiveSource(StrictModel):
    type: Literal["archive"]
    artifact: ArtifactDescriptor
    transfer: TransferSpec


OpenScadSource = Annotated[
    OpenScadWorkspaceSource | OpenScadArchiveSource, Field(discriminator="type")
]


class OpenScadRenderArguments(StrictModel):
    executable_path: str = Field(alias="executablePath", min_length=1, max_length=4096)
    session_id: str = Field(
        alias="sessionId", min_length=1, max_length=128, pattern=r"^[^/\\\x00]+$"
    )
    entrypoint: str = Field(min_length=1, max_length=1000, pattern=r"^[^\x00\\]+$")
    source: OpenScadSource
    output: Literal["preview", "stl"] = "preview"
    preview_images: bool = Field(default=False, alias="previewImages")

    _executable_is_absolute = field_validator("executable_path")(_absolute_path)

    @field_validator("entrypoint")
    @classmethod
    def relative_entrypoint(cls, value: str) -> str:
        return _relative_path(value)


class MaintenanceStartArguments(StrictModel):
    action: Literal["update", "uninstall"]
    job_id: str = Field(alias="jobId", pattern=UUID_PATTERN)
    revision: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    sudo_password: str | None = Field(
        default=None, alias="sudoPassword", min_length=1, max_length=4096
    )

    _password_is_single_line = field_validator("sudo_password")(_single_line_password)

    @model_validator(mode="after")
    def revision_matches_action(self):
        if (self.action == "update") != (self.revision is not None):
            raise PydanticCustomError(
                "operation_argument_invalid",
                "Invalid target revision",
                {"hint": "Provide a target revision only for updates."},
            )
        return self


class MaintenanceUpdateArguments(MaintenanceStartArguments):
    action: Literal["update"]


ARGUMENT_MODELS: dict[str, type[StrictModel]] = {
    "git.changes": GitChangesArguments,
    "git.repository": GitRepositoryArguments,
    "git.branch": GitBranchArguments,
    "git.worktree": GitWorktreeArguments,
    "git.worktree.remove": GitWorktreeRemoveArguments,
    "maintenance.start": MaintenanceStartArguments,
    "maintenance.update": MaintenanceUpdateArguments,
    "maintenance.update.interrupt": MaintenanceUpdateArguments,
    "system.info": SystemInfoArguments,
    "filesystem.resolve": ResolveDirectoryArguments,
    "filesystem.stat": StatArguments,
    "filesystem.list": ListArguments,
    "filesystem.read": ReadArguments,
    "filesystem.glob": GlobArguments,
    "filesystem.grep": GrepArguments,
    "filesystem.write": WriteArguments,
    "filesystem.upsert": UpsertArguments,
    "filesystem.edit": EditArguments,
    "filesystem.patch": PatchArguments,
    "filesystem.mkdir": MkdirArguments,
    "filesystem.rename": RenameArguments,
    "filesystem.delete": DeleteArguments,
    "filesystem.copy": CopyMoveArguments,
    "filesystem.move": CopyMoveArguments,
    "filesystem.import": ImportArguments,
    "filesystem.export": ExportArguments,
    "shell.run": ShellArguments,
    "shell.inspect": ShellInspectArguments,
    "shell.classify": ShellInspectArguments,
    "administrator.run": AdministratorArguments,
    "terminal.open": TerminalOpenArguments,
    "terminal.read": TerminalReadArguments,
    "terminal.write": TerminalWriteArguments,
    "terminal.resize": TerminalResizeArguments,
    "terminal.close": TerminalCloseArguments,
    "terminal.run": TerminalRunArguments,
    "cache.ensure": CacheEnsureArguments,
    "cache.skills.prepare": SkillCachePrepareArguments,
    "cache.skills.release": SkillCacheReleaseArguments,
    "cache.store": CacheStoreArguments,
    "cache.export": CacheExportArguments,
    "cache.clear": CacheClearArguments,
    "integration.openscad.discover": OpenScadDiscoverArguments,
    "integration.openscad.test": OpenScadTestArguments,
    "integration.openscad.render": OpenScadRenderArguments,
    "integration.openscad.render6": OpenScadRenderArguments,
}

INBOUND_ADAPTER = TypeAdapter(Welcome | Request | Cancel)


def parse_inbound(data: str | bytes) -> Welcome | Request | Cancel:
    return INBOUND_ADAPTER.validate_json(data)


def validate_arguments(request: Request) -> StrictModel:
    from .browser_contracts import BROWSER_ARGUMENT_MODELS

    model = ARGUMENT_MODELS.get(request.operation) or BROWSER_ARGUMENT_MODELS.get(request.operation)
    if model is None:
        raise ValueError("unsupported operation")
    return model.model_validate(request.arguments)
