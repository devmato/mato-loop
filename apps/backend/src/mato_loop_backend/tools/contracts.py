from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from jsonschema.validators import validator_for
from pydantic import BaseModel
from referencing import Registry

from mato_loop_backend.error_contract import error_payload
from mato_loop_backend.files.roots import (
    internal_file_roots,
    model_file_root_schema,
    model_file_roots,
)
from mato_loop_backend.permissions.policy import PermissionRequest
from mato_loop_backend.settings import TrustLevel
from mato_loop_backend.tools.actions import ApprovalEditField, ToolAction
from mato_loop_backend.tools.snapshots import ToolSnapshotPolicy

if TYPE_CHECKING:
    from mato_loop_backend.tools.context import ToolContext

PERMISSION_REASON_ARGUMENT = "_reason"


MAX_DYNAMIC_ARGUMENT_BYTES = 256 * 1024


MAX_DYNAMIC_JSON_DEPTH = 64


class DynamicToolArgumentsError(ValueError):
    def __init__(self, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field


class ToolCallSuperseded(Exception):
    """A new user instruction supersedes a tool call awaiting user input."""


class RepositoryInstructionsChanged(Exception):
    """A tool must be reconsidered after its local repository rules enter context."""

    def __init__(self, targets: dict[str, Any]) -> None:
        self.targets = targets


class ToolArgumentsEdited(Exception):
    def __init__(
        self,
        edits: tuple[tuple[tuple[str, ...], str], ...],
        approval_binding: str | None = None,
    ) -> None:
        self.edits = edits
        self.approval_binding = approval_binding


def _json_depth(value: Any, depth: int = 0) -> int:
    if depth > MAX_DYNAMIC_JSON_DEPTH:
        raise DynamicToolArgumentsError("dynamic JSON exceeds maximum depth")
    if isinstance(value, dict):
        return max((_json_depth(item, depth + 1) for item in value.values()), default=depth)
    if isinstance(value, list):
        return max((_json_depth(item, depth + 1) for item in value), default=depth)
    return depth


@dataclass(slots=True)
class ToolAttachment:
    data: bytes
    mime_type: str
    filename: str
    kind: Literal["image", "audio", "file"] = "image"
    category: Literal["generated", "downloaded"] = "generated"
    presentation: Literal["result", "internal"] = "result"
    duration_ms: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    file_root_id: UUID | None = None
    file_id: UUID | None = None
    file_revision: int | None = None
    file_path: str | None = None
    # Host-only: never create a session file or attachment record for these bytes.
    ephemeral: bool = False


@dataclass(frozen=True, slots=True)
class ToolFileAction:
    operation: Literal["write", "create", "edit", "delete", "mkdir", "copy", "move", "provide"]
    root: Literal["session", "workspace"]
    root_identity: str | None
    path: str
    filename: str
    size_bytes: int | None
    execution_agent_id: str | None = None
    current_directory: str | None = None
    kind: Literal["file", "directory", "collection", "path"] = "file"
    source_path: str | None = None
    source_root: Literal["session", "workspace"] | None = None
    paths: tuple[str, ...] | None = None
    archive_root_contents: bool = False
    file_id: str | None = None
    file_revision: int | None = None


@dataclass(slots=True)
class ToolResult:
    content: str
    metadata: dict[str, Any]
    is_error: bool = False
    attachments: tuple[ToolAttachment, ...] = ()
    file_actions: tuple[ToolFileAction, ...] = ()
    trust_level: TrustLevel | None = None

    @classmethod
    def json(
        cls,
        value: Any,
        *,
        metadata: dict[str, Any] | None = None,
        file_actions: tuple[ToolFileAction, ...] = (),
        trust_level: TrustLevel | None = None,
    ) -> ToolResult:
        return cls(
            json.dumps(model_file_roots(value), ensure_ascii=False),
            metadata or {},
            file_actions=file_actions,
            trust_level=trust_level,
        )

    @classmethod
    def raw_json(cls, value: Any) -> ToolResult:
        return cls(json.dumps(value, ensure_ascii=False, allow_nan=False), {})

    @classmethod
    def error(
        cls,
        code: str,
        *,
        field: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> ToolResult:
        payload = error_payload(
            code, field=field, params=params, fallback_code="tool.executionFailed"
        )
        descriptor = payload["error"]
        result = cls.json(payload, metadata={"errorCode": descriptor["code"]})
        result.is_error = True
        return result


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    arguments_model: type[BaseModel] | None
    handler: Callable[[Any, ToolContext], Awaitable[ToolResult]]
    tool_type: Literal["backend", "computer", "shared"] = "backend"
    requires_search: bool = False
    requires_webfetch: bool = False
    requires_memory: bool = False
    requires_memory_write: bool = False
    required_capability: str | None = None
    required_operation: str | None = None
    group: str = "agent"
    label: str | None = None
    icon: str = "build"
    actions: tuple[ToolAction, ...] = ()
    alternative_action_groups: tuple[frozenset[str], ...] = ()
    instance_argument: str | None = None
    resource_argument: str | None = None
    policy_resource_key: str | None = None
    releases_private_data: bool = False
    authorization_factory: (
        Callable[[Any, ToolContext], tuple[PermissionRequest, ...] | Awaitable[tuple[PermissionRequest, ...]]] | None
    ) = None
    prompt_fragments: tuple[str, ...] = ()
    arguments_schema: dict[str, Any] | None = None
    approval_edit_fields: tuple[ApprovalEditField, ...] = ()
    permission_reason_instruction: str | None = None
    requires_permission_reason: bool = False
    snapshot_policy: ToolSnapshotPolicy | None = None

    def schema(self) -> dict[str, Any]:
        if self.arguments_schema is not None:
            parameters = deepcopy(self.arguments_schema)
        else:
            parameters = self.arguments_model.model_json_schema()
            model_file_root_schema(parameters)
        if self.actions or self.requires_permission_reason:
            parameters.setdefault("properties", {})[PERMISSION_REASON_ARGUMENT] = {
                "type": "string",
                "minLength": 1,
                "maxLength": 500,
                "description": (
                    "Optional purpose in the user's language, shown for approval."
                    + (" " + self.permission_reason_instruction if self.permission_reason_instruction else "")
                ),
            }
        schema = parameters
        _remove_schema_titles(schema)
        return {"name": self.name, "description": self.description, "parameters": schema}

    def validate_arguments(self, value: object) -> Any:
        if (self.actions or self.requires_permission_reason) and isinstance(value, dict):
            value = dict(value)
            reason_supplied = PERMISSION_REASON_ARGUMENT in value
            reason = value.pop(PERMISSION_REASON_ARGUMENT, None)
            if reason_supplied and (
                not isinstance(reason, str) or not reason.strip() or len(reason) > 500
            ):
                raise DynamicToolArgumentsError(
                    "permission reason is invalid", field=PERMISSION_REASON_ARGUMENT
                )
        if self.arguments_schema is not None:
            _json_depth(value)
            try:
                size = len(
                    json.dumps(
                        value, ensure_ascii=True, separators=(",", ":"), allow_nan=False
                    ).encode()
                )
            except (TypeError, ValueError) as exc:
                raise DynamicToolArgumentsError("dynamic arguments are not finite JSON") from exc
            if size > MAX_DYNAMIC_ARGUMENT_BYTES:
                raise DynamicToolArgumentsError("dynamic arguments exceed maximum size")
            validator_for(self.arguments_schema)(
                self.arguments_schema, registry=Registry()
            ).validate(value)
            return value
        return self.arguments_model.model_validate(internal_file_roots(value))

    def actions_satisfied(self, capabilities: set[str]) -> bool:
        declared = {action.capability for action in self.actions}
        alternatives = frozenset().union(*self.alternative_action_groups)
        return (
            capabilities <= declared
            and declared - alternatives <= capabilities
            and all(capabilities & group for group in self.alternative_action_groups)
        )


def _remove_schema_titles(value: Any) -> None:
    if isinstance(value, dict):
        if isinstance(value.get("title"), str):
            value.pop("title")
        for item in value.values():
            _remove_schema_titles(item)
    elif isinstance(value, list):
        for item in value:
            _remove_schema_titles(item)
