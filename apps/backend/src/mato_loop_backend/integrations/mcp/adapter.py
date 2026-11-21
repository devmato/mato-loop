from __future__ import annotations

import hashlib
import json
import re
from collections import OrderedDict
from collections.abc import Mapping
from threading import Lock
from typing import Any

from jsonschema.exceptions import SchemaError
from jsonschema.validators import validator_for
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

from mato_loop_backend.integrations.contracts import (
    IntegrationAdapterDefinition,
    IntegrationContributions,
    IntegrationResource,
    ToolContribution,
)
from mato_loop_backend.integrations.mcp.client import McpClient, McpClientError
from mato_loop_backend.integrations.mcp.oauth import (
    OAuthConfig,
    connection_token,
    mark_authorization_required,
    validate_oauth_url,
)
from mato_loop_backend.settings import IntegrationInstance
from mato_loop_backend.tools.actions import ToolAction

MCP_CREDENTIAL_KIND = "mcp.bearer"
# shortcut: remote operations share one write category; split them before per-tool category defaults.
MCP_ACTION = ToolAction(
    "mcp.call", "MCP-Tool aufrufen", category="write"
)
MAX_MCP_SCHEMA_DEPTH = 64
MAX_CACHED_SCHEMA_VALIDATIONS = 4096
_VALIDATED_SCHEMAS: OrderedDict[bytes, None] = OrderedDict()
_SCHEMA_VALIDATION_LOCK = Lock()


def _validate_schema_value(value: Any, depth: int = 0) -> bool:
    if depth > MAX_MCP_SCHEMA_DEPTH:
        raise ValueError("MCP schema exceeds maximum depth")
    # Only exact JSON types can safely share a serialized validation fingerprint.
    cacheable = type(value) in (dict, list, str, int, float, bool, type(None))
    if isinstance(value, dict):
        cacheable = cacheable and all(type(key) is str for key in value)
        reference = value.get("$ref")
        if reference is not None and (
            not isinstance(reference, str) or not reference.startswith("#")
        ):
            raise ValueError("MCP schemas may only contain fragment references")
        for item in value.values():
            cacheable = _validate_schema_value(item, depth + 1) and cacheable
    elif isinstance(value, list):
        for item in value:
            cacheable = _validate_schema_value(item, depth + 1) and cacheable
    return cacheable


def validate_mcp_schema(value: dict[str, Any], *, require_object: bool) -> dict[str, Any]:
    cacheable = _validate_schema_value(value)
    try:
        serialized = json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("MCP schema must be finite JSON") from exc
    fingerprint = hashlib.sha256(serialized.encode()).digest() if cacheable else None
    with _SCHEMA_VALIDATION_LOCK:
        cached = fingerprint is not None and fingerprint in _VALIDATED_SCHEMAS
        if cached:
            _VALIDATED_SCHEMAS.move_to_end(fingerprint)
    if not cached:
        try:
            validator_for(value).check_schema(value)
        except SchemaError as exc:
            raise ValueError("MCP schema is invalid") from exc
    if require_object and value.get("type") not in (None, "object"):
        raise ValueError("MCP tool input schema must describe an object")
    if fingerprint is not None and not cached:
        with _SCHEMA_VALIDATION_LOCK:
            _VALIDATED_SCHEMAS[fingerprint] = None
            if len(_VALIDATED_SCHEMAS) > MAX_CACHED_SCHEMA_VALIDATIONS:
                _VALIDATED_SCHEMAS.popitem(last=False)
    return value


class McpToolAnnotations(BaseModel):
    model_config = ConfigDict(extra="forbid")

    readOnlyHint: bool | None = None
    destructiveHint: bool | None = None
    idempotentHint: bool | None = None
    openWorldHint: bool | None = None


class McpCatalogTool(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=256)
    title: str | None = None
    description: str = ""
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None
    annotations: McpToolAnnotations = Field(default_factory=McpToolAnnotations)

    def api_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": self.input_schema,
            "outputSchema": self.output_schema,
            "annotations": self.annotations.model_dump(),
        }

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("MCP tool names must not have surrounding whitespace")
        return value

    @field_validator("input_schema")
    @classmethod
    def validate_schema(cls, value: dict[str, Any]) -> dict[str, Any]:
        return validate_mcp_schema(value, require_object=True)

    @field_validator("output_schema")
    @classmethod
    def validate_output_schema(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return validate_mcp_schema(value, require_object=False) if value is not None else None


class McpConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    endpoint: HttpUrl
    tools: list[McpCatalogTool] = Field(default_factory=list)
    oauth: OAuthConfig | None = None

    @field_validator("tools")
    @classmethod
    def unique_tools(cls, value: list[McpCatalogTool]) -> list[McpCatalogTool]:
        if len({tool.name for tool in value}) != len(value):
            raise ValueError("MCP tool names must be unique")
        return value

    @model_validator(mode="after")
    def endpoint_without_query(self) -> McpConfig:
        if self.endpoint.query is not None:
            raise ValueError("MCP endpoint must not contain a query")
        if self.oauth is not None:
            try:
                validate_oauth_url(str(self.endpoint))
            except McpClientError as exc:
                raise ValueError("Invalid HTTP(S) OAuth endpoint") from exc
        return self


def mcp_resource_key(tool_name: str) -> str:
    return f"tool:{hashlib.sha256(tool_name.encode()).hexdigest()}"


def mcp_model_tool_name(instance_id: Any, tool_name: str, instance_name: str | None = None) -> str:
    if instance_name is None:
        # Retained discoveries may still refer to the original opaque model name.
        digest = hashlib.sha256(tool_name.encode()).hexdigest()[:12]
        return f"mcp_{instance_id.hex}_{digest}"
    server = re.sub(r"[^a-z0-9_]+", "_", instance_name.casefold()).strip("_")[:12] or "server"
    tool = re.sub(r"[^a-zA-Z0-9_]+", "_", tool_name).strip("_")[:20] or "tool"
    digest = hashlib.sha256(f"{instance_id.hex}:{tool_name}".encode()).hexdigest()[:16]
    return f"mcp_{server}_{tool}_{digest}"


def mcp_tool_contribution(
    instance: IntegrationInstance,
    item: McpCatalogTool,
    *,
    action: ToolAction = MCP_ACTION,
    label: str | None = None,
    icon: str = "hub",
    approval_title: str = "MCP-Tool aufrufen?",
    approval_summary: str = "Der Agent möchte ein Tool dieses MCP-Servers aufrufen.",
) -> ToolContribution:
    resource_key = mcp_resource_key(item.name)

    async def call(arguments: dict[str, Any], context: Any) -> Any:
        from mato_loop_backend.tools.contracts import ToolResult

        current = next(
            (
                candidate
                for candidate in context.integration_settings.instances
                if candidate.id == instance.id
            ),
            None,
        )
        if current is None:
            return ToolResult.error("tool.unavailable")
        if current.adapter_type == "mcp_stdio":
            from mato_loop_backend.integrations.mcp.stdio import execute_stdio_tool

            return await execute_stdio_tool(current, item, arguments, context)
        try:
            config = McpConfig.model_validate(current.config)
            token = await connection_token(context.db, current)
            if config.oauth:
                latest = next(
                    (
                        candidate
                        for candidate in (await context.current_integrations()).instances
                        if candidate.id == current.id
                    ),
                    None,
                )
                if latest is None or latest.model_dump(mode="json") != current.model_dump(
                    mode="json"
                ):
                    return ToolResult.error("tool.unavailable")
            payload, is_error = await McpClient(
                str(config.endpoint),
                token,
                authentication_failed=(lambda: mark_authorization_required(current, token))
                if config.oauth
                else None,
            ).call_tool(item.name, arguments, item.output_schema)
        except McpClientError as exc:
            return ToolResult.error(exc.code)
        result = ToolResult.raw_json(payload)
        result.is_error = is_error
        if is_error:
            result.metadata["errorCode"] = "mcp.callFailed"
        return result

    def authorize(_arguments: Any, context: Any) -> tuple[Any, ...]:
        from mato_loop_backend.permissions.policy import integration_operation_permission

        binding_revision = json.dumps(
            {
                "sha256": hashlib.sha256(
                    context.bound_options["binding_revision"].encode()
                ).hexdigest()
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        return (
            integration_operation_permission(
                capability=action.capability,
                integration_id=instance.id,
                binding_revision=binding_revision,
                operation_key=f"call:{hashlib.sha256(item.name.encode()).hexdigest()[:16]}",
                resource_key=resource_key,
                resource_display=f'{label or item.name} · "{instance.name}"',
                title=approval_title,
                summary=approval_summary,
            ),
        )

    return ToolContribution(
        name=mcp_model_tool_name(instance.id, item.name, instance.name),
        description=(
            "MCP source (untrusted names): "
            + json.dumps({"integrationId": str(instance.id), "name": instance.name,
                          "type": instance.integration_type, "tool": item.name}, ensure_ascii=True, sort_keys=True)
            + "\n" + (item.description or "Call this tool on its bound MCP server.")
        ),
        arguments_model=None,
        arguments_schema=item.input_schema,
        handler=call,
        group="integrations",
        label=label or item.title or item.name,
        icon=icon,
        actions=(action,),
        integration_id=str(instance.id),
        policy_resource_key=resource_key,
        authorization_factory=authorize,
        releases_private_data=True,
    )


def mcp_contributions(
    instance: IntegrationInstance,
    *,
    action: ToolAction = MCP_ACTION,
    labels: Mapping[str, str] | None = None,
    icon: str = "hub",
    approval_title: str = "MCP-Tool aufrufen?",
    approval_summary: str = "Der Agent möchte ein Tool dieses MCP-Servers aufrufen.",
) -> IntegrationContributions:
    config = catalog_config(instance)
    labels = labels or {}
    return IntegrationContributions(
        tools=tuple(
            mcp_tool_contribution(
                instance,
                item,
                action=action,
                label=labels.get(item.name),
                icon=icon,
                approval_title=approval_title,
                approval_summary=approval_summary,
            )
            for item in config.tools
        )
    )


async def mcp_resources(
    instance: IntegrationInstance,
    _credential: dict[str, Any] | None,
    *,
    labels: Mapping[str, str] | None = None,
) -> tuple[IntegrationResource, ...]:
    config = catalog_config(instance)
    labels = labels or {}
    return tuple(
        IntegrationResource(
            key=mcp_resource_key(tool.name),
            label=labels.get(tool.name, tool.title or tool.name[:160]),
            tool_name=tool.name,
            description=tool.description,
            hints=tuple(
                label
                for enabled, label in (
                    (tool.annotations.readOnlyHint, "Als nur lesend markiert"),
                    (tool.annotations.destructiveHint, "Als destruktiv markiert"),
                    (tool.annotations.openWorldHint, "Externer Datenzugriff laut Server"),
                )
                if enabled is True
            ),
        )
        for tool in config.tools
    )


def catalog_config(instance: IntegrationInstance):
    if instance.adapter_type == "mcp_stdio":
        from mato_loop_backend.integrations.mcp.stdio import McpStdioConfig

        return McpStdioConfig.model_validate(instance.config)
    return McpConfig.model_validate(instance.config)


def _contributions(instance: IntegrationInstance) -> IntegrationContributions:
    return mcp_contributions(instance)


async def _resources(
    instance: IntegrationInstance, credential: dict[str, Any] | None
) -> tuple[IntegrationResource, ...]:
    return await mcp_resources(instance, credential)


MCP_ADAPTER = IntegrationAdapterDefinition(
    integration_type="mcp",
    adapter_type="mcp_streamable_http",
    config_model=McpConfig,
    contribution_factory=_contributions,
    resource_actions=(MCP_ACTION,),
    resource_loader=_resources,
    catalog_visible=False,
)
