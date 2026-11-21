"""Computer-bound MCP runtime; process configuration never comes from model arguments."""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import replace
from typing import Any
from uuid import UUID

from cryptography.fernet import InvalidToken
from jsonschema.exceptions import ValidationError as SchemaValidationError
from jsonschema.validators import validator_for
from mcp.types import CallToolResult
from pydantic import Field, ValidationError, field_validator

from mato_loop_backend.execution.artifacts import ArtifactDescriptor, export_artifact
from mato_loop_backend.execution.client import ExecutionClientError, ExecutionResult
from mato_loop_backend.execution.paths import absolute_path
from mato_loop_backend.execution.protocol import WireModel
from mato_loop_backend.integrations.contracts import (
    IntegrationAdapterDefinition,
    IntegrationContributions,
    ToolContribution,
)
from mato_loop_backend.integrations.execution import selected_integration_binding
from mato_loop_backend.integrations.mcp.adapter import (
    MCP_ACTION,
    McpCatalogTool,
    McpConfig,
    mcp_contributions,
    mcp_resources,
)
from mato_loop_backend.integrations.mcp.client import McpClientError
from mato_loop_backend.media.attachments import AttachmentError, validate_image
from mato_loop_backend.models import AgentRun
from mato_loop_backend.security import load_credential, read_credential
from mato_loop_backend.tools.contracts import ToolAttachment, ToolResult

STDIO_CREDENTIAL_KIND = "mcp.stdio.environment"
MAX_STDIO_RESULT_BYTES = 16 * 1024 * 1024
STDIO_INSTRUCTION = (
    "Computer-bound MCP processes keep volatile state per chat and connection. "
    "They can stop after the idle timeout, a configuration change, a disconnect, or a restart. "
    "mcpSession.started=true identifies a new process, not restored previous state. "
    "Save important volatile state explicitly "
    "using the server's supported save/export tools before pauses or the final answer; "
    "do not promise automatic saving. Never retry a call with an unknown outcome automatically."
)


def validate_environment(value: dict[str, str]) -> dict[str, str]:
    if len(value) > 128 or any(
        re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) is None or "\0" in item or len(item) > 32768
        for key, item in value.items()
    ):
        raise ValueError("Invalid MCP environment")
    return value


class McpStdioBinding(WireModel):
    executable: str = Field(min_length=1, max_length=4096)
    args: list[str] = Field(default_factory=list, max_length=256)
    working_directory: str = Field(max_length=4096)
    environment: dict[str, str] = Field(default_factory=dict)
    idle_timeout_seconds: int = Field(default=1800, ge=1, le=86400)

    _paths = field_validator("executable", "working_directory")(absolute_path)
    _environment = field_validator("environment")(validate_environment)

    @field_validator("args")
    @classmethod
    def valid_args(cls, value):
        if any("\0" in item or len(item) > 32768 for item in value):
            raise ValueError("Invalid MCP arguments")
        return value


class McpStdioConfig(McpStdioBinding):
    tools: list[McpCatalogTool] = Field(default_factory=list)
    environment_secret_names: list[str] | None = None

    _tools = field_validator("tools")(McpConfig.unique_tools.__func__)


class McpProcessSession(ExecutionResult):
    id: str = Field(min_length=1, max_length=128)
    started: bool
    idle_timeout_seconds: int = Field(ge=1, le=86400)
    expires_at: str = Field(min_length=1, max_length=100)


class McpRuntimeResult(ExecutionResult):
    artifact: ArtifactDescriptor
    session: McpProcessSession


class McpManagedSession(ExecutionResult):
    connection_id: UUID = Field(strict=False)
    session_id: UUID = Field(strict=False)
    id: str = Field(min_length=1, max_length=128)
    idle_timeout_seconds: int = Field(ge=1, le=86400)
    expires_at: str = Field(min_length=1, max_length=100)
    busy: bool


class McpSessionsResult(ExecutionResult):
    sessions: list[McpManagedSession]


class McpCloseResult(ExecutionResult):
    closed: bool


async def credential_environment(db, instance) -> dict[str, str]:
    if instance.credential_id is None:
        return {}
    credential = await load_credential(
        db, instance.credential_id, owner_id=instance.owner_id, populate_existing=True
    )
    if credential is None or credential.kind != STDIO_CREDENTIAL_KIND:
        raise McpClientError("mcp.configurationInvalid")
    try:
        env_values = dict(read_credential(credential))["environment"]
        if not isinstance(env_values, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in env_values.items()
        ):
            raise ValueError("Invalid MCP credential")
        return validate_environment(env_values)
    except (InvalidToken, KeyError, TypeError, ValueError) as exc:
        raise McpClientError("mcp.configurationInvalid") from exc


def stdio_arguments(
    binding: McpStdioBinding, secrets: dict[str, str], connection_id: UUID, session_id: UUID
) -> dict:
    environment = {**binding.environment, **secrets}
    if (
        len(environment) > 128
        or any(len(name) > 128 for name in environment)
        or binding.environment.keys() & secrets.keys()
    ):
        raise McpClientError("mcp.configurationInvalid")
    return {
        "executable": binding.executable,
        "args": binding.args,
        "workingDirectory": binding.working_directory,
        "idleTimeoutSeconds": binding.idle_timeout_seconds,
        "environment": environment,
        "connectionId": str(connection_id),
        "sessionId": str(session_id),
    }


async def runtime_arguments(db, instance, session_id: UUID) -> dict:
    config = McpStdioConfig.model_validate(instance.config)
    secret_values = await credential_environment(db, instance)
    if config.environment_secret_names is not None and secret_values.keys() != set(
        config.environment_secret_names
    ):
        raise McpClientError("mcp.configurationInvalid")
    return stdio_arguments(config, secret_values, instance.id, session_id)


async def runtime_json(client, operation: str, arguments: dict, *, limit: int):
    result = await client.request(
        operation,
        arguments,
        McpRuntimeResult,
        timeout=90 if operation == "integration.mcp.call" else 65,
    )
    artifact = result.artifact
    if artifact.backend_id != str(client.agent_id) or artifact.session_id != arguments["sessionId"]:
        raise McpClientError("mcp.invalidResponse")
    if artifact.size > limit:
        raise McpClientError("mcp.responseTooLarge")
    if artifact.content_type != "application/json":
        raise McpClientError("mcp.invalidResponse")
    data = await export_artifact(client, artifact, limit=limit, too_large_code="mcp.responseTooLarge")
    try:
        payload = json.loads(
            data.decode("utf-8"),
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("Nonfinite JSON")),
        )
        # Exponent overflow (for example 1e400) bypasses parse_constant.
        json.dumps(payload, allow_nan=False)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise McpClientError("mcp.invalidResponse") from exc
    return payload, result.session


def call_result(payload: Any, output_schema: dict | None) -> ToolResult:
    try:
        parsed = CallToolResult.model_validate(payload)
        if output_schema is not None and not parsed.isError:
            if parsed.structuredContent is None:
                raise ValueError("Missing structured MCP output")
            validator_for(output_schema)(output_schema).validate(parsed.structuredContent)
        attachments = []
        content = []
        for index, item in enumerate(parsed.content):
            if item.type == "image":
                data = base64.b64decode(item.data, validate=True)
                validated = validate_image(data, item.mimeType)
                attachments.append(
                    ToolAttachment(
                        data, validated.mime_type, f"mcp-{index}.image", category="downloaded"
                    )
                )
                content.append(
                    {
                        "type": "image",
                        "attachmentIndex": len(attachments) - 1,
                        "mimeType": validated.mime_type,
                    }
                )
            else:
                # Resource URIs and embedded text remain data; never follow server paths.
                content.append(item.model_dump(mode="json", by_alias=True, exclude_none=True))
        result_payload = parsed.model_dump(mode="json", by_alias=True, exclude_none=True)
        result_payload["content"] = content
    except (
        ValidationError,
        ValueError,
        AttachmentError,
        binascii.Error,
        SchemaValidationError,
    ) as exc:
        raise McpClientError("mcp.invalidResponse") from exc
    result = ToolResult.raw_json(result_payload)
    result.is_error = bool(parsed.isError)
    result.attachments = tuple(attachments)
    if parsed.isError:
        result.metadata["errorCode"] = "mcp.callFailed"
    return result


async def execute_stdio_tool(instance, item, arguments, context):
    try:
        instance, _revision = selected_integration_binding(
            context, instance.id, integration_type="mcp", adapter_type="mcp_stdio"
        )
        run = await context.db.get(AgentRun, context.run_id)
        required = {"integration.mcp.call", "cache.export"}
        client = context.execution_client
        if (
            run is None
            or run.execution_agent_id != instance.execution_agent_id
            or client is None
            or client.agent_id != instance.execution_agent_id
            or not required <= set(run.execution_operations or [])
        ):
            return ToolResult.error("tool.unavailable")
        latest = next(
            (
                candidate
                for candidate in (await context.current_integrations()).instances
                if candidate.id == instance.id
            ),
            None,
        )
        if latest is None or latest.binding_data() != instance.binding_data() or not latest.enabled:
            return ToolResult.error("tool.unavailable")
        wire = await runtime_arguments(context.db, instance, context.session.id)
        wire.update(name=item.name, arguments=arguments, outputSchema=item.output_schema)
        try:
            payload, session = await runtime_json(
                client, "integration.mcp.call", wire, limit=MAX_STDIO_RESULT_BYTES
            )
        except ExecutionClientError as exc:
            if exc.code.startswith("mcp.") or exc.code == "executionAgent.requestTooLarge":
                raise McpClientError(exc.code) from exc
            raise McpClientError("mcp.outcomeUnknown") from exc
        result = call_result(payload, item.output_schema)
        result.metadata["mcpSession"] = session.model_dump(mode="json", by_alias=True)
        # Metadata alone is not included in every provider's model-facing result.
        result.content = json.dumps(
            {"result": json.loads(result.content), "mcpSession": result.metadata["mcpSession"]},
            ensure_ascii=True,
        )
        return result
    except ValueError:
        return ToolResult.error("tool.unavailable")
    except McpClientError as exc:
        return ToolResult.error(exc.code)


def stdio_contributions(instance):
    contribution = mcp_contributions(instance)
    timeout = McpStdioConfig.model_validate(instance.config).idle_timeout_seconds
    return IntegrationContributions(
        tools=tuple(
            replace(
                tool,
                description=(
                    f"{tool.description}\n"
                    f"This connection's process idle timeout is {timeout} seconds."
                ),
                execution_operation="integration.mcp.call",
                result_model=McpRuntimeResult,
                required_execution_operations=("cache.export",),
                prompt_fragments=(STDIO_INSTRUCTION,),
            )
            for tool in contribution.tools
        ),
    )


MCP_STDIO_ADAPTER = IntegrationAdapterDefinition(
    integration_type="mcp",
    adapter_type="mcp_stdio",
    config_model=McpStdioConfig,
    contribution_factory=stdio_contributions,
    resource_actions=(MCP_ACTION,),
    resource_loader=mcp_resources,
    execution_location="agent",
    execution_kind="mcp",
    catalog_visible=False,
    dynamic_execution_tools=True,
    execution_tools=(
        ToolContribution(
            name="mcp_stdio_call",
            description="Call a configured computer MCP tool.",
            arguments_model=None,
            handler=None,
            execution_operation="integration.mcp.call",
            result_model=McpRuntimeResult,
        ),
    ),
)
