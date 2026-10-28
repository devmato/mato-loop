from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend.integrations.mcp.adapter import (
    MCP_CREDENTIAL_KIND,
    McpConfig,
    mcp_resource_key,
)
from mato_loop_backend.integrations.mcp.client import McpClient, McpClientError
from mato_loop_backend.integrations.mcp.oauth import (
    OAUTH_CREDENTIAL_KIND,
    connection_token,
    mark_authorization_required,
    oauth_status,
    rebind_oauth_endpoint,
)
from mato_loop_backend.integrations.registry import validate_integration
from mato_loop_backend.integrations.service import (
    IntegrationSettingsDocument,
    IntegrationSettingsError,
    _clear_integration_policies,
    _load_integrations,
    _load_tools,
    _save_settings,
)
from mato_loop_backend.security import (
    delete_credential,
    store_credential,
)
from mato_loop_backend.settings import IntegrationInstance


class McpIntegrationService:
    integration_type = "mcp"
    adapter_type = "mcp_streamable_http"
    config_model = McpConfig
    token_required = False

    @classmethod
    def _validate_connection(cls, endpoint: str, token: str | None) -> str:
        if cls.token_required and not token:
            raise IntegrationSettingsError("mcp.configurationInvalid")
        try:
            return str(cls.config_model(endpoint=endpoint).endpoint)
        except ValueError as exc:
            raise IntegrationSettingsError("mcp.configurationInvalid") from exc

    @classmethod
    def _find(
        cls, settings: IntegrationSettingsDocument, integration_id: UUID
    ) -> IntegrationInstance | None:
        return next(
            (
                item
                for item in settings.instances
                if item.id == integration_id
                and item.integration_type == cls.integration_type
                and item.adapter_type == cls.adapter_type
            ),
            None,
        )

    @staticmethod
    def _prune_tool_state(tools, instance: IntegrationInstance, resource_keys: set[str]) -> bool:
        for permissions in (instance.permissions, *instance.profile_permissions.values()):
            permissions.resource_availability = {
                key: value for key, value in permissions.resource_availability.items() if key in resource_keys
            }
            for branch in (permissions.private_permissions, permissions.public_permissions):
                branch.resource_policies = {
                    key: value for key, value in branch.resource_policies.items() if key in resource_keys
                }
        key = str(instance.id)
        labels = tools.resource_labels.get(key)
        if labels is not None:
            filtered_labels = {
                resource_key: label
                for resource_key, label in labels.items()
                if resource_key in resource_keys
            }
            if filtered_labels != labels:
                if filtered_labels:
                    tools.resource_labels[key] = filtered_labels
                else:
                    tools.resource_labels.pop(key, None)
                return True
        return False

    @classmethod
    async def _discover(
        cls, endpoint: str, token: str | None, *, authentication_failed=None
    ) -> tuple[str, list[dict]]:
        client = McpClient(
            endpoint, token, authentication_failed=authentication_failed
        )
        tools = await client.discover_tools()
        try:
            config = cls.config_model(endpoint=client.endpoint, tools=tools)
            return str(config.endpoint), config.model_dump(mode="json")["tools"]
        except ValueError as exc:
            raise McpClientError("mcp.invalidResponse") from exc

    @classmethod
    async def _discover_instance(cls, db, instance):
        config = cls.config_model.model_validate(instance.config)
        token = await connection_token(db, instance)
        cls._validate_connection(str(config.endpoint), token)
        return await cls._discover(
            str(config.endpoint),
            token,
            authentication_failed=(lambda: mark_authorization_required(instance, token))
            if config.oauth
            else None,
        )

    @classmethod
    async def create(
        cls,
        db: AsyncSession,
        *,
        name: str,
        endpoint: str,
        token: str | None,
        enabled: bool,
        owner_id: UUID | None,
    ) -> IntegrationInstance:
        endpoint = cls._validate_connection(endpoint, token)
        endpoint, tools = await cls._discover(endpoint, token)
        settings = await _load_integrations(db, owner_id, for_update=True)
        instance = IntegrationInstance(
            name=name,
            integration_type=cls.integration_type,
            adapter_type=cls.adapter_type,
            enabled=enabled,
            config={"endpoint": endpoint, "tools": tools},
        )
        instance.owner_id = owner_id
        validate_integration(instance)
        if token is not None:
            credential = await store_credential(
                db,
                owner_id=owner_id,
                kind=MCP_CREDENTIAL_KIND,
                label=name,
                payload={"token": token},
            )
            instance.credential_id = credential.id
        settings.instances.append(instance)
        await _save_settings(db, settings, owner_id)
        await db.commit()
        return instance

    @classmethod
    async def update(
        cls,
        db: AsyncSession,
        integration_id: UUID,
        *,
        name: str,
        endpoint: str,
        token: str | None,
        remove_token: bool,
        enabled: bool,
        owner_id: UUID | None,
        remove_oauth: bool = False,
        expected_instance: IntegrationInstance | None = None,
    ) -> IntegrationInstance:
        initial = await _load_integrations(db, owner_id)
        current = cls._find(initial, integration_id)
        if current is None:
            raise IntegrationSettingsError("mcp.notFound")
        current.owner_id = owner_id
        revision = current.model_dump(mode="json")
        current_config = cls.config_model.model_validate(current.config)
        remove_oauth = remove_oauth and current_config.oauth is not None
        if current_config.oauth is not None and not remove_oauth:
            if str(current_config.endpoint) != endpoint or token is not None or remove_token:
                raise McpClientError("mcp.authorizationRequired")
            if enabled:
                endpoint, tools = await cls._discover_instance(db, current)
            else:
                tools = [item.model_dump(mode="json") for item in current_config.tools]
        else:
            discovery_token = token
            if discovery_token is None and not remove_token and current_config.oauth is None:
                discovery_token = await connection_token(db, current)
            endpoint = cls._validate_connection(endpoint, discovery_token)
            endpoint, tools = await cls._discover(endpoint, discovery_token)

        settings = await _load_integrations(db, owner_id, for_update=True, expected_instance=expected_instance)
        instance = cls._find(settings, integration_id)
        if instance is None:
            raise IntegrationSettingsError("mcp.notFound")
        if instance.model_dump(mode="json") != revision:
            raise IntegrationSettingsError("integration.changed")
        endpoint_changed = (
            str(cls.config_model.model_validate(instance.config).endpoint) != endpoint
        )
        if endpoint_changed and current_config.oauth is not None and not remove_oauth:
            await rebind_oauth_endpoint(db, instance, endpoint)
        old_credential_id = instance.credential_id
        instance.name = name
        instance.enabled = enabled
        instance.config = {
            **instance.config,
            "endpoint": endpoint,
            "tools": tools,
        }
        if remove_oauth:
            instance.config.pop("oauth", None)
        validate_integration(instance)
        if token is not None:
            credential = await store_credential(
                db,
                owner_id=owner_id,
                kind=MCP_CREDENTIAL_KIND,
                label=name,
                payload={"token": token},
            )
            instance.credential_id = credential.id
        elif remove_token or remove_oauth:
            instance.credential_id = None
        if old_credential_id is not None and old_credential_id != instance.credential_id:
            await delete_credential(db, old_credential_id, owner_id=owner_id)
        if endpoint_changed or (remove_oauth and current_config.oauth is not None):
            await cls._clear_binding_policies(db, instance)
        else:
            tool_settings = await _load_tools(db, owner_id, for_update=True)
            resource_keys = {mcp_resource_key(tool["name"]) for tool in tools}
            if cls._prune_tool_state(tool_settings, instance, resource_keys):
                await _save_settings(db, tool_settings, owner_id)
        await _save_settings(db, settings, owner_id)
        await db.commit()
        return instance

    @classmethod
    async def refresh(
        cls, db: AsyncSession, integration_id: UUID, *, owner_id: UUID | None
    ) -> IntegrationInstance:
        initial = await _load_integrations(db, owner_id)
        current = cls._find(initial, integration_id)
        if current is None:
            raise IntegrationSettingsError("mcp.notFound")
        current.owner_id = owner_id
        revision = current.model_dump(mode="json")
        endpoint, tools = await cls._discover_instance(db, current)

        settings = await _load_integrations(db, owner_id, for_update=True)
        instance = cls._find(settings, integration_id)
        if instance is None:
            raise IntegrationSettingsError("mcp.notFound")
        if instance.model_dump(mode="json") != revision:
            raise IntegrationSettingsError("integration.changed")
        endpoint_changed = instance.config["endpoint"] != endpoint
        if instance.config.get("oauth") is not None and endpoint_changed:
            await rebind_oauth_endpoint(db, instance, endpoint)
        instance.config["endpoint"] = endpoint
        instance.config["tools"] = tools
        validate_integration(instance)
        if endpoint_changed:
            await cls._clear_binding_policies(db, instance)
        else:
            resource_keys = {mcp_resource_key(tool["name"]) for tool in tools}
            tool_settings = await _load_tools(db, owner_id, for_update=True)
            if cls._prune_tool_state(tool_settings, instance, resource_keys):
                await _save_settings(db, tool_settings, owner_id)
        await _save_settings(db, settings, owner_id)
        await db.commit()
        return instance

    @classmethod
    async def delete(cls, db: AsyncSession, integration_id: UUID, *, owner_id: UUID | None) -> None:
        settings = await _load_integrations(db, owner_id, for_update=True)
        instance = cls._find(settings, integration_id)
        if instance is None:
            raise IntegrationSettingsError("mcp.notFound")
        settings.instances = [item for item in settings.instances if item.id != integration_id]
        if instance.credential_id is not None:
            await delete_credential(db, instance.credential_id, owner_id=owner_id)
        tools = await _load_tools(db, owner_id, for_update=True)
        if tools.resource_labels.pop(str(integration_id), None) is not None:
            await _save_settings(db, tools, owner_id)
        await _save_settings(db, settings, owner_id)
        await db.commit()

    @classmethod
    async def test(cls, db: AsyncSession, integration_id: UUID, *, owner_id: UUID | None) -> None:
        settings = await _load_integrations(db, owner_id)
        instance = cls._find(settings, integration_id)
        if instance is None:
            raise IntegrationSettingsError("mcp.notFound")
        instance.owner_id = owner_id
        await cls._discover_instance(db, instance)

    @classmethod
    async def complete_oauth(cls, db, attempt):
        if oauth_status(attempt.payload) != "authorized":
            raise McpClientError("mcp.authorizationRequired")
        settings = await _load_integrations(db, attempt.owner_id, for_update=True)
        instance = cls._find(settings, attempt.integration_id) if attempt.integration_id else None
        if attempt.integration_id and (
            instance is None or instance.model_dump(mode="json") != attempt.revision
        ):
            raise IntegrationSettingsError("integration.changed")
        if instance is None:
            instance = IntegrationInstance(
                name=attempt.draft["name"],
                integration_type="mcp",
                adapter_type="mcp_streamable_http",
            )
            settings.instances.append(instance)
        instance.owner_id = attempt.owner_id
        old_credential_id = instance.credential_id
        credential = await store_credential(
            db,
            owner_id=attempt.owner_id,
            kind=OAUTH_CREDENTIAL_KIND,
            label=attempt.draft["name"],
            payload=attempt.payload,
        )
        instance.name = attempt.draft["name"]
        instance.enabled = attempt.draft["enabled"]
        instance.config = {
            **instance.config,
            "endpoint": attempt.draft["endpoint"],
            "oauth": attempt.draft["oauth"],
            "tools": attempt.tools,
        }
        instance.credential_id = credential.id
        validate_integration(instance)
        if old_credential_id:
            await delete_credential(db, old_credential_id, owner_id=attempt.owner_id)
        if attempt.integration_id:
            await cls._clear_binding_policies(db, instance)
        await _save_settings(db, settings, attempt.owner_id)
        await db.commit()
        return instance

    @classmethod
    async def _clear_binding_policies(cls, db, instance):
        _clear_integration_policies(instance)
        tools = await _load_tools(db, instance.owner_id, for_update=True)
        if tools.resource_labels.pop(str(instance.id), None) is not None:
            await _save_settings(db, tools, instance.owner_id)

    @classmethod
    async def disconnect_oauth(cls, db, integration_id, *, owner_id):
        settings = await _load_integrations(db, owner_id, for_update=True)
        instance = cls._find(settings, integration_id)
        if instance is None:
            raise IntegrationSettingsError("mcp.notFound")
        instance.owner_id = owner_id
        if instance.config.get("oauth") is None:
            raise IntegrationSettingsError("mcp.oauthConfigurationInvalid")
        if instance.credential_id:
            await delete_credential(db, instance.credential_id, owner_id=owner_id)
        instance.credential_id = None
        await cls._clear_binding_policies(db, instance)
        await _save_settings(db, settings, owner_id)
        await db.commit()
        return instance
