"""Author-owned configuration and management of computer-bound MCP connections."""

from __future__ import annotations

from uuid import UUID

from pydantic import Field, field_validator
from sqlalchemy import select

from mato_loop_backend.api.errors import ApiError
from mato_loop_backend.execution.client import ExecutionClient
from mato_loop_backend.execution.manager import execution_agent_connections
from mato_loop_backend.execution.protocol import (
    MAX_REQUEST_BYTES,
    MCP_MANAGE_OPERATION,
    RequestMessage,
)
from mato_loop_backend.integrations.mcp.adapter import mcp_resource_key
from mato_loop_backend.integrations.mcp.client import MAX_MCP_CATALOG_BYTES, McpClientError
from mato_loop_backend.integrations.mcp.service import McpIntegrationService, delete_connection
from mato_loop_backend.integrations.mcp.stdio import (
    STDIO_CREDENTIAL_KIND,
    McpCloseResult,
    McpSessionsResult,
    McpStdioBinding,
    McpStdioConfig,
    credential_environment,
    runtime_arguments,
    runtime_json,
    stdio_arguments,
    validate_environment,
)
from mato_loop_backend.integrations.service import _load_integrations, _load_tools, _save_settings
from mato_loop_backend.models import ChatSession, ExecutionAgent
from mato_loop_backend.security import (
    delete_credential,
    store_credential,
)
from mato_loop_backend.settings import IntegrationInstance


class McpStdioRequest(McpStdioBinding):
    name: str = Field(min_length=1, max_length=160)
    transport: str = Field(pattern="^stdio$")
    execution_agent_id: UUID = Field(strict=False)
    enabled: bool = True
    environment_secrets: dict[str, str] = Field(default_factory=dict)
    remove_environment_secrets: list[str] = Field(default_factory=list, max_length=128)
    # Catalogs come only from validated discovery, not configuration requests.
    tools: list = Field(default_factory=list, max_length=0)

    _env_validator = field_validator("environment_secrets")(validate_environment)


async def owned_agent(db, user_id, agent_id, *, allow_revoked=False):
    conditions = [ExecutionAgent.id == agent_id, ExecutionAgent.user_id == user_id]
    if not allow_revoked:
        conditions.append(ExecutionAgent.revoked_at.is_(None))
    agent = await db.scalar(
        select(ExecutionAgent).where(*conditions)
    )
    if agent is None:
        raise ApiError(404, "executionAgent.notFound")
    return agent


async def owned_agent_states(db, user_id):
    """Include revoked owner rows for configuration cleanup, never runtime access."""
    agents = await db.scalars(select(ExecutionAgent).where(ExecutionAgent.user_id == user_id))
    return {agent.id: agent.revoked_at is not None for agent in agents.all()}


def connected_client(agent_id):
    operations = execution_agent_connections.snapshot(agent_id)
    if operations is None:
        raise McpClientError("mcp.unavailable")
    return ExecutionClient(agent_id, operations)


def stdio_payload(instance):
    config = McpStdioConfig.model_validate(instance.config)
    return {
        "id": str(instance.id),
        "name": instance.name,
        "transport": "stdio",
        "adapterType": "mcp_stdio",
        "executionAgentId": str(instance.execution_agent_id),
        "enabled": instance.enabled,
        **config.model_dump(mode="json", by_alias=True, exclude={"tools", "environment_secret_names"}),
        "environmentSecretNames": config.environment_secret_names or [],
        "tools": [tool.api_payload() for tool in config.tools],
    }


async def save_stdio(db, payload, *, owner_id, user_id, integration_id=None):
    await owned_agent(db, user_id, payload.execution_agent_id)
    settings = await _load_integrations(db, owner_id, for_update=True)
    current = next(
        (
            item
            for item in settings.instances
            if item.id == integration_id and item.adapter_type == "mcp_stdio"
        ),
        None,
    )
    if integration_id is not None and current is None:
        raise McpClientError("mcp.notFound")
    if current is not None and current.execution_agent_id != payload.execution_agent_id:
        await owned_agent(db, user_id, current.execution_agent_id)
    instance = current or IntegrationInstance(
        name=payload.name, integration_type="mcp", adapter_type="mcp_stdio"
    )
    instance.owner_id = owner_id
    old_env_values = dict(await credential_environment(db, instance)) if current else {}
    env_values = dict(old_env_values)
    for name in payload.remove_environment_secrets:
        env_values.pop(name, None)
    env_values.update(payload.environment_secrets)
    binding_fields = set(McpStdioBinding.model_fields)
    config = McpStdioBinding.model_validate(payload.model_dump(include=binding_fields))
    discovery = RequestMessage(
        id=instance.id,
        operation="integration.mcp.discover",
        arguments=stdio_arguments(config, env_values, instance.id, instance.id),
    )
    if len(discovery.model_dump_json(by_alias=True).encode("utf-8")) > MAX_REQUEST_BYTES:
        raise McpClientError("executionAgent.requestTooLarge")
    old_credential_id = instance.credential_id
    config_data = config.model_dump(mode="json", by_alias=True)
    # Keep list responses on the same snapshot even if the old credential is deleted.
    config_data["environmentSecretNames"] = sorted(env_values)
    # Retain a trusted catalog only while the executable binding is unchanged.
    old_config = McpStdioConfig.model_validate(instance.config) if current else None
    same_binding = (
        current
        and old_config.model_dump(include=binding_fields) == config.model_dump()
        and current.execution_agent_id == payload.execution_agent_id
        and old_env_values == env_values
    )
    config_data["tools"] = (
        [tool.model_dump(mode="json") for tool in old_config.tools] if same_binding else []
    )
    instance.name, instance.enabled = payload.name, payload.enabled
    instance.execution_agent_id = payload.execution_agent_id
    instance.config = config_data
    if payload.environment_secrets or payload.remove_environment_secrets:
        instance.credential_id = None
        if env_values:
            credential = await store_credential(
                db,
                owner_id=owner_id,
                kind=STDIO_CREDENTIAL_KIND,
                label=payload.name,
                payload={"environment": env_values},
            )
            instance.credential_id = credential.id
    if old_credential_id and instance.credential_id != old_credential_id:
        await delete_credential(db, old_credential_id, owner_id=owner_id)
    if current and (not same_binding or instance.credential_id != old_credential_id):
        await McpIntegrationService._clear_binding_policies(db, instance)
    if current is None:
        settings.instances.append(instance)
    await _save_settings(db, settings, owner_id)
    await db.commit()
    return instance


async def refresh_stdio(db, instance, user_id):
    await owned_agent(db, user_id, instance.execution_agent_id)
    # Dedicated management session is connection-scoped, never a user's chat process.
    wire = await runtime_arguments(db, instance, instance.id)
    payload, _session = await runtime_json(
        connected_client(instance.execution_agent_id),
        "integration.mcp.discover",
        wire,
        limit=MAX_MCP_CATALOG_BYTES,
    )
    try:
        config = McpStdioConfig.model_validate({**instance.config, "tools": payload})
    except ValueError as exc:
        raise McpClientError("mcp.invalidResponse") from exc
    settings = await _load_integrations(db, instance.owner_id, for_update=True)
    current = next((item for item in settings.instances if item.id == instance.id), None)
    if current is None or current.binding_data() != instance.binding_data():
        raise McpClientError("integration.changed")
    current.owner_id = instance.owner_id
    current.config = config.model_dump(mode="json", by_alias=True)
    tools = await _load_tools(db, instance.owner_id, for_update=True)
    if McpIntegrationService._prune_tool_state(
        tools, current, {mcp_resource_key(tool.name) for tool in config.tools}
    ):
        await _save_settings(db, tools, instance.owner_id)
    await _save_settings(db, settings, instance.owner_id)
    await db.commit()
    return current


async def managed_sessions(db, instance, user_id):
    await owned_agent(db, user_id, instance.execution_agent_id)
    result = await connected_client(instance.execution_agent_id).request(
        MCP_MANAGE_OPERATION, {"action": "list"}, McpSessionsResult
    )
    # A shared instance connection may have processes in multiple owners' chats.
    owned_chats = set(
        (await db.scalars(select(ChatSession.id).where(ChatSession.user_id == user_id))).all()
    )
    return [
        item
        for item in result.sessions
        if item.connection_id == instance.id
        and (item.session_id in owned_chats or item.session_id == instance.id)
    ]


async def close_session(db, instance, session_id, process_id, user_id):
    sessions = await managed_sessions(db, instance, user_id)
    if not any(item.session_id == session_id for item in sessions):
        raise McpClientError("mcp.notFound")
    # The ownership check must not substitute a newer process ID for the confirmed one.
    result = await connected_client(instance.execution_agent_id).request(
        MCP_MANAGE_OPERATION,
        {
            "action": "close",
            "connectionId": str(instance.id),
            "sessionId": str(session_id),
            "processId": str(process_id),
        },
        McpCloseResult,
    )
    if not result.closed:
        raise McpClientError("integration.changed")
    return result.model_dump(mode="json", by_alias=True)


async def delete_stdio(db, instance, user_id):
    settings = await _load_integrations(db, instance.owner_id, for_update=True)
    current = next(
        (
            item
            for item in settings.instances
            if item.id == instance.id
            and item.integration_type == "mcp"
            and item.adapter_type == "mcp_stdio"
        ),
        None,
    )
    if current is None:
        raise McpClientError("mcp.notFound")
    await owned_agent(db, user_id, current.execution_agent_id, allow_revoked=True)
    await delete_connection(db, settings, current, instance.owner_id)
