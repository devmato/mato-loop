from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend.integrations.registry import validate_integration
from mato_loop_backend.integrations.search import normalize_search_tool_settings
from mato_loop_backend.integrations.service import (
    IntegrationSettingsDocument,
    IntegrationSettingsError,
    _clear_integration_policies,
    _load_integrations,
    _load_tools,
    _save_settings,
)
from mato_loop_backend.security import delete_credential
from mato_loop_backend.settings import IntegrationInstance, IntegrationSettings


class SearchIntegrationService:
    @staticmethod
    async def _save(
        db: AsyncSession,
        settings: IntegrationSettingsDocument,
        *,
        owner_id: UUID | None = None,
        normalize_tools: bool = False,
        remove_resource_policies: UUID | None = None,
    ) -> IntegrationSettings:
        if normalize_tools or remove_resource_policies is not None:
            tool_settings = await _load_tools(db, owner_id, for_update=True)
            changed = normalize_tools and normalize_search_tool_settings(
                tool_settings, settings
            )
            if remove_resource_policies is not None:
                instance = next((item for item in settings.instances if item.id == remove_resource_policies), None)
                if instance is not None:
                    _clear_integration_policies(instance)
                changed = (
                    tool_settings.resource_labels.pop(
                        str(remove_resource_policies), None
                    )
                    is not None
                    or changed
                )
            if changed:
                await _save_settings(db, tool_settings, owner_id)
        await _save_settings(db, settings, owner_id)
        await db.commit()
        return settings

    @classmethod
    async def create(
        cls,
        db: AsyncSession,
        *,
        name: str,
        provider_type: str,
        base_url: str,
        enabled: bool,
        owner_id: UUID | None = None,
    ) -> IntegrationSettings:
        settings = await _load_integrations(db, owner_id, for_update=True)
        instance = IntegrationInstance(
            name=name,
            integration_type="search",
            adapter_type=provider_type,
            enabled=enabled,
            config={"base_url": base_url.rstrip("/")},
        )
        instance.owner_id = owner_id
        validate_integration(instance)
        settings.instances.append(instance)
        return await cls._save(db, settings, owner_id=owner_id)

    @classmethod
    async def update(
        cls,
        db: AsyncSession,
        integration_id: UUID,
        *,
        name: str,
        provider_type: str,
        base_url: str,
        enabled: bool,
        owner_id: UUID | None = None,
        expected_instance: IntegrationInstance | None = None,
    ) -> IntegrationSettings:
        settings = await _load_integrations(db, owner_id, for_update=True, expected_instance=expected_instance)
        instance = next(
            (
                item
                for item in settings.instances
                if item.id == integration_id
                and item.integration_type == "search"
                and item.adapter_type == provider_type
            ),
            None,
        )
        if instance is None:
            raise IntegrationSettingsError("integration.notFound")
        config = {"base_url": base_url.rstrip("/")}
        connection_changed = instance.config != config
        instance.name = name
        instance.enabled = enabled
        instance.config = config
        validate_integration(instance)
        return await cls._save(
            db,
            settings,
            owner_id=owner_id,
            normalize_tools=True,
            remove_resource_policies=integration_id if connection_changed else None,
        )

    @classmethod
    async def delete(
        cls, db: AsyncSession, integration_id: UUID, *, owner_id: UUID | None = None
    ) -> IntegrationSettings:
        settings = await _load_integrations(db, owner_id, for_update=True)
        instance = next(
            (
                item
                for item in settings.instances
                if item.id == integration_id and item.integration_type == "search"
            ),
            None,
        )
        if instance is None:
            raise IntegrationSettingsError("integration.notFound")
        settings.instances = [item for item in settings.instances if item.id != integration_id]
        if instance.credential_id:
            await delete_credential(
                db, instance.credential_id, owner_id=instance.owner_id
            )
        return await cls._save(
            db,
            settings,
            owner_id=owner_id,
            normalize_tools=True,
            remove_resource_policies=integration_id,
        )
