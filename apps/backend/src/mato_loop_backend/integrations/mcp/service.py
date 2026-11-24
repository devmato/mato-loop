from __future__ import annotations

from uuid import UUID

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend.integrations.mcp.adapter import (
    MCP_CREDENTIAL_KIND,
    McpConfig,
    mcp_resource_key,
)
from mato_loop_backend.integrations.mcp.client import McpClient, McpClientError
from mato_loop_backend.integrations.mcp.oauth import (
    OAUTH_CLIENT_CREDENTIAL_KIND,
    OAUTH_CREDENTIAL_KIND,
    connection_token,
    delete_user_oauth_credentials,
    find_user_oauth_credential,
    lock_user_oauth_credential,
    mark_authorization_required,
    oauth_payload_without_shared_secret,
    oauth_status,
    rebind_oauth_endpoint,
    user_scoped_oauth,
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
    load_credential,
    read_credential,
    store_credential,
)
from mato_loop_backend.settings import IntegrationInstance


async def delete_connection(db, settings, instance: IntegrationInstance, owner_id: UUID | None) -> None:
    settings.instances = [item for item in settings.instances if item.id != instance.id]
    if user_scoped_oauth(instance):
        await delete_user_oauth_credentials(db, instance.id)
    if instance.credential_id is not None:
        await delete_credential(db, instance.credential_id, owner_id=owner_id)
    tools = await _load_tools(db, owner_id, for_update=True)
    if tools.resource_labels.pop(str(instance.id), None) is not None:
        await _save_settings(db, tools, owner_id)
    await _save_settings(db, settings, owner_id)
    await db.commit()


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
    async def _discover_instance(cls, db, instance, *, user_id: UUID | None = None):
        config = cls.config_model.model_validate(instance.config)
        token = await connection_token(db, instance, user_id=user_id)
        cls._validate_connection(str(config.endpoint), token)
        return await cls._discover(
            str(config.endpoint),
            token,
            authentication_failed=(
                lambda: mark_authorization_required(instance, token, user_id)
            )
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
    async def create_user_scoped_oauth(
        cls,
        db: AsyncSession,
        *,
        name: str,
        endpoint: str,
        enabled: bool,
        oauth: dict,
        client_secret: str | None,
        owner_id: UUID | None,
    ) -> IntegrationInstance:
        if owner_id is not None or oauth.get("user_scoped") is not True:
            raise IntegrationSettingsError("mcp.configurationInvalid")
        method = oauth.get("token_endpoint_auth_method", "none")
        if (method == "none") != (client_secret is None):
            raise IntegrationSettingsError("mcp.oauthConfigurationInvalid")
        endpoint = cls._validate_connection(endpoint, None)
        config = cls.config_model(endpoint=endpoint, oauth=oauth).model_dump(mode="json")
        settings = await _load_integrations(db, owner_id, for_update=True)
        instance = IntegrationInstance(
            name=name,
            integration_type=cls.integration_type,
            adapter_type=cls.adapter_type,
            enabled=enabled,
            config=config,
        )
        instance.owner_id = None
        validate_integration(instance)
        credential = await store_credential(
            db,
            owner_id=None,
            kind=OAUTH_CLIENT_CREDENTIAL_KIND,
            label=name,
            payload={
                "catalog_initialized": False,
                "client_info": {
                    "client_id": oauth.get("client_id"),
                    "client_secret": client_secret,
                    "token_endpoint_auth_method": method,
                    "scope": oauth.get("scope"),
                    "client_name": "MatoLoop",
                }
            },
        )
        instance.credential_id = credential.id
        settings.instances.append(instance)
        await _save_settings(db, settings, owner_id)
        await db.commit()
        return instance

    @classmethod
    async def update_user_scoped_oauth(
        cls,
        db: AsyncSession,
        integration_id: UUID,
        *,
        name: str,
        endpoint: str,
        enabled: bool,
        oauth: dict,
        client_secret: str | None,
        owner_id: UUID | None,
    ) -> IntegrationInstance:
        if owner_id is not None or oauth.get("user_scoped") is not True:
            raise IntegrationSettingsError("mcp.configurationInvalid")
        initial = await _load_integrations(db, owner_id)
        current = cls._find(initial, integration_id)
        if current is None or not user_scoped_oauth(current):
            raise IntegrationSettingsError("mcp.notFound")
        current.owner_id = None
        revision = current.model_dump(mode="json")
        current_config = cls.config_model.model_validate(current.config)
        template = await load_credential(
            db, current.credential_id, owner_id=None, populate_existing=True
        )
        if template is None or template.kind != OAUTH_CLIENT_CREDENTIAL_KIND:
            raise IntegrationSettingsError("mcp.oauthConfigurationInvalid")
        client_info = dict(read_credential(template).get("client_info") or {})
        if client_secret is not None:
            client_info["client_secret"] = client_secret
        method = oauth.get("token_endpoint_auth_method", "none")
        if method == "none":
            if client_secret is not None:
                raise IntegrationSettingsError("mcp.oauthConfigurationInvalid")
            client_info.pop("client_secret", None)
        if method != "none" and not client_info.get("client_secret"):
            raise IntegrationSettingsError("mcp.oauthConfigurationInvalid")
        client_info.update(
            client_id=oauth.get("client_id"),
            token_endpoint_auth_method=method,
            scope=oauth.get("scope"),
        )
        endpoint = cls._validate_connection(endpoint, None)
        config = cls.config_model(
            endpoint=endpoint,
            oauth=oauth,
            tools=[item.model_dump(mode="json") for item in current_config.tools],
        ).model_dump(mode="json")

        settings = await _load_integrations(db, owner_id, for_update=True)
        instance = cls._find(settings, integration_id)
        if instance is None or instance.model_dump(mode="json") != revision:
            raise IntegrationSettingsError("integration.changed")
        binding_changed = instance.config != config or client_secret is not None
        instance.name = name
        instance.enabled = enabled
        instance.config = config
        validate_integration(instance)
        await store_credential(
            db,
            owner_id=None,
            kind=OAUTH_CLIENT_CREDENTIAL_KIND,
            label=name,
            payload={
                "catalog_initialized": (
                    read_credential(template).get("catalog_initialized", False)
                    if not binding_changed
                    else False
                ),
                "client_info": client_info,
            },
            credential=template,
        )
        if binding_changed:
            await delete_user_oauth_credentials(db, integration_id)
            await cls._clear_binding_policies(db, instance)
        await _save_settings(db, settings, owner_id)
        await db.commit()
        return instance

    @classmethod
    async def transition_to_user_scoped_oauth(
        cls,
        db: AsyncSession,
        integration_id: UUID,
        *,
        name: str,
        endpoint: str,
        enabled: bool,
        oauth: dict,
        client_secret: str | None,
        owner_id: UUID | None,
    ) -> IntegrationInstance:
        if owner_id is not None or oauth.get("user_scoped") is not True:
            raise IntegrationSettingsError("mcp.configurationInvalid")
        initial = await _load_integrations(db, owner_id)
        current = cls._find(initial, integration_id)
        if current is None or user_scoped_oauth(current):
            raise IntegrationSettingsError("mcp.notFound")
        current.owner_id = None
        revision = current.model_dump(mode="json")
        current_config = cls.config_model.model_validate(current.config)
        method = oauth.get("token_endpoint_auth_method", "none")
        client_info = {
            "client_id": oauth.get("client_id"),
            "token_endpoint_auth_method": method,
            "scope": oauth.get("scope"),
            "client_name": "MatoLoop",
        }
        if client_secret is not None:
            client_info["client_secret"] = client_secret
        elif current.credential_id:
            previous = await load_credential(
                db, current.credential_id, owner_id=None, populate_existing=True
            )
            if previous and previous.kind == OAUTH_CREDENTIAL_KIND:
                previous_payload = read_credential(previous)
                previous_info = previous_payload.get("client_info") or {}
                if previous_info.get("client_id") == oauth.get("client_id"):
                    client_info["client_secret"] = previous_info.get("client_secret")
        if method != "none" and not client_info.get("client_secret"):
            raise IntegrationSettingsError("mcp.oauthConfigurationInvalid")
        if method == "none":
            client_info.pop("client_secret", None)
        endpoint = cls._validate_connection(endpoint, None)
        endpoint_changed = endpoint != str(current_config.endpoint)
        tools = [] if endpoint_changed else [
            item.model_dump(mode="json") for item in current_config.tools
        ]
        config = cls.config_model(
            endpoint=endpoint, oauth=oauth, tools=tools
        ).model_dump(mode="json")

        settings = await _load_integrations(db, owner_id, for_update=True)
        instance = cls._find(settings, integration_id)
        if instance is None or instance.model_dump(mode="json") != revision:
            raise IntegrationSettingsError("integration.changed")
        old_credential_id = instance.credential_id
        template = await store_credential(
            db,
            owner_id=None,
            kind=OAUTH_CLIENT_CREDENTIAL_KIND,
            label=name,
            payload={
                "catalog_initialized": not endpoint_changed
                and bool(current_config.tools),
                "client_info": client_info,
            },
        )
        instance.name = name
        instance.enabled = enabled
        instance.config = config
        instance.credential_id = template.id
        instance.owner_id = None
        validate_integration(instance)
        if old_credential_id is not None:
            await delete_credential(db, old_credential_id, owner_id=None)
        await delete_user_oauth_credentials(db, integration_id)
        await cls._clear_binding_policies(db, instance)
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
        user_id: UUID | None = None,
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
                endpoint, tools = await cls._discover_instance(
                    db, current, user_id=user_id
                )
            else:
                tools = [item.model_dump(mode="json") for item in current_config.tools]
        else:
            discovery_token = token
            if discovery_token is None and not remove_token and current_config.oauth is None:
                discovery_token = await connection_token(db, current, user_id=user_id)
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
            if user_scoped_oauth(current):
                await delete_user_oauth_credentials(db, integration_id)
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
        cls,
        db: AsyncSession,
        integration_id: UUID,
        *,
        owner_id: UUID | None,
        user_id: UUID | None = None,
    ) -> IntegrationInstance:
        initial = await _load_integrations(db, owner_id)
        current = cls._find(initial, integration_id)
        if current is None:
            raise IntegrationSettingsError("mcp.notFound")
        current.owner_id = owner_id
        revision = current.model_dump(mode="json")
        endpoint, tools = await cls._discover_instance(db, current, user_id=user_id)

        settings = await _load_integrations(db, owner_id, for_update=True)
        instance = cls._find(settings, integration_id)
        if instance is None:
            raise IntegrationSettingsError("mcp.notFound")
        if instance.model_dump(mode="json") != revision:
            raise IntegrationSettingsError("integration.changed")
        endpoint_changed = instance.config["endpoint"] != endpoint
        if instance.config.get("oauth") is not None and endpoint_changed:
            if user_scoped_oauth(instance):
                await delete_user_oauth_credentials(db, instance.id)
            else:
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
        await delete_connection(db, settings, instance, owner_id)

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
        delegated_target = attempt.draft.get("oauth", {}).get("user_scoped") is True
        delegated = instance is not None and user_scoped_oauth(instance) and delegated_target
        if attempt.integration_id and instance is None:
            raise IntegrationSettingsError("integration.changed")
        if instance is not None and delegated:
            revision = instance.model_dump(mode="json")
            expected = attempt.revision or {}
            if any(
                value != expected.get(key)
                for key, value in revision.items()
                if key != "config"
            ) or revision.get("credential_id") != expected.get("credential_id") or {
                key: value
                for key, value in revision.get("config", {}).items()
                if key != "tools"
            } != {
                key: value
                for key, value in expected.get("config", {}).items()
                if key != "tools"
            }:
                raise IntegrationSettingsError("integration.changed")
        elif (
            attempt.integration_id
            and instance.model_dump(mode="json") != attempt.revision
        ):
            raise IntegrationSettingsError("integration.changed")
        if delegated:
            if attempt.integration_id is None or attempt.owner_id is not None:
                raise McpClientError("mcp.oauthConfigurationInvalid")
            instance.owner_id = None
            await lock_user_oauth_credential(db, instance.id, attempt.user_id)
            previous = await find_user_oauth_credential(
                db, instance, attempt.user_id, for_update=True
            )
            if attempt.bootstrap:
                template = await load_credential(
                    db, instance.credential_id, owner_id=None, populate_existing=True
                )
                if template is None or template.kind != OAUTH_CLIENT_CREDENTIAL_KIND:
                    raise McpClientError("mcp.oauthConfigurationInvalid")
                template_payload = read_credential(template)
                await store_credential(
                    db,
                    owner_id=None,
                    kind=template.kind,
                    label=template.label,
                    payload={**template_payload, "catalog_initialized": True},
                    credential=template,
                )
            elif not attempt.payload:
                raise McpClientError("mcp.oauthFailed")
            else:
                canonical_endpoint = attempt.payload.get("endpoint")
                if not canonical_endpoint or httpx.URL(canonical_endpoint) != httpx.URL(
                    instance.config["endpoint"]
                ):
                    raise IntegrationSettingsError("integration.changed")
            await store_credential(
                db,
                owner_id=attempt.user_id,
                kind=OAUTH_CREDENTIAL_KIND,
                label=attempt.draft["name"],
                payload=oauth_payload_without_shared_secret(attempt.payload),
                metadata={"integration_id": str(instance.id)},
                credential=previous,
            )
            if attempt.bootstrap:
                instance.config = {
                    **instance.config,
                    "endpoint": attempt.draft["endpoint"],
                    "tools": attempt.tools or [],
                }
                validate_integration(instance)
                tool_settings = await _load_tools(db, None, for_update=True)
                resource_keys = {
                    mcp_resource_key(tool["name"])
                    for tool in instance.config["tools"]
                }
                if cls._prune_tool_state(tool_settings, instance, resource_keys):
                    await _save_settings(db, tool_settings, None)
                if any(
                    instance.config.get(key) != attempt.revision.get("config", {}).get(key)
                    for key in ("endpoint", "tools")
                ):
                    await cls._clear_binding_policies(db, instance)
            await _save_settings(db, settings, attempt.owner_id)
            await db.commit()
            return instance
        if instance is None:
            instance = IntegrationInstance(
                name=attempt.draft["name"],
                integration_type="mcp",
                adapter_type="mcp_streamable_http",
            )
            settings.instances.append(instance)
        instance.owner_id = attempt.owner_id
        old_credential_id = instance.credential_id
        if user_scoped_oauth(instance):
            await delete_user_oauth_credentials(db, instance.id)
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
