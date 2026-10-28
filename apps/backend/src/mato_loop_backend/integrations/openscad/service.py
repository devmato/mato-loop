from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend.integrations.registry import validate_integration
from mato_loop_backend.integrations.service import (
    IntegrationSettingsError,
    _clear_integration_policies,
    _load_integrations,
    _save_settings,
)
from mato_loop_backend.models import ExecutionAgent
from mato_loop_backend.settings import IntegrationInstance, IntegrationSettings


class OpenScadIntegrationService:
    @staticmethod
    async def _owned_agent(
        db: AsyncSession,
        user_id: UUID,
        execution_agent_id: UUID,
        *,
        for_update: bool = True,
    ) -> None:
        statement = select(ExecutionAgent.id).where(
            ExecutionAgent.id == execution_agent_id,
            ExecutionAgent.user_id == user_id,
            ExecutionAgent.revoked_at.is_(None),
        )
        if for_update:
            statement = statement.with_for_update()
        if await db.scalar(statement) is None:
            raise IntegrationSettingsError("executionAgent.notFound")

    @staticmethod
    def _find(
        settings: IntegrationSettings, integration_id: UUID
    ) -> IntegrationInstance | None:
        return next(
            (
                item
                for item in settings.instances
                if item.id == integration_id
                and item.integration_type == "openscad"
                and item.adapter_type == "openscad"
            ),
            None,
        )

    @staticmethod
    def _ensure_unique(
        settings: IntegrationSettings,
        execution_agent_id: UUID,
        *,
        excluding: UUID | None = None,
    ) -> None:
        if any(
            item.id != excluding
            and item.integration_type == "openscad"
            and item.adapter_type == "openscad"
            and item.execution_agent_id == execution_agent_id
            for item in settings.instances
        ):
            raise IntegrationSettingsError("openscad.alreadyConfigured")

    @classmethod
    async def create(
        cls,
        db: AsyncSession,
        *,
        user_id: UUID,
        execution_agent_id: UUID,
        name: str,
        binary_path: str,
        enabled: bool,
        settings_owner_id: UUID | None = None,
    ) -> IntegrationSettings:
        await cls._owned_agent(db, user_id, execution_agent_id)
        settings = await _load_integrations(db, settings_owner_id, for_update=True)
        cls._ensure_unique(settings, execution_agent_id)
        instance = IntegrationInstance(
            name=name,
            integration_type="openscad",
            adapter_type="openscad",
            enabled=enabled,
            execution_agent_id=execution_agent_id,
            config={"binary_path": binary_path},
        )
        instance.owner_id = settings_owner_id
        validate_integration(instance)
        settings.instances.append(instance)
        await _save_settings(db, settings, settings_owner_id)
        await db.commit()
        return settings

    @classmethod
    async def update(
        cls,
        db: AsyncSession,
        integration_id: UUID,
        *,
        user_id: UUID,
        execution_agent_id: UUID,
        name: str,
        binary_path: str,
        enabled: bool,
        settings_owner_id: UUID | None = None,
        expected_instance: IntegrationInstance | None = None,
    ) -> IntegrationSettings:
        initial = await _load_integrations(db, settings_owner_id)
        current = cls._find(initial, integration_id)
        if current is None:
            raise IntegrationSettingsError("integration.notFound")
        if current.execution_agent_id is None:
            raise IntegrationSettingsError("executionAgent.notFound")
        for agent_id in sorted({current.execution_agent_id, execution_agent_id}, key=str):
            await cls._owned_agent(db, user_id, agent_id)
        settings = await _load_integrations(db, settings_owner_id, for_update=True, expected_instance=expected_instance)
        instance = cls._find(settings, integration_id)
        if instance is None:
            raise IntegrationSettingsError("integration.notFound")
        if instance.execution_agent_id != current.execution_agent_id:
            raise IntegrationSettingsError("integration.changed")
        cls._ensure_unique(settings, execution_agent_id, excluding=integration_id)
        binding_changed = (
            instance.execution_agent_id != execution_agent_id
            or instance.config != {"binary_path": binary_path}
        )
        instance.name = name
        instance.enabled = enabled
        instance.execution_agent_id = execution_agent_id
        instance.config = {"binary_path": binary_path}
        validate_integration(instance)
        if binding_changed:
            _clear_integration_policies(instance)
        await _save_settings(db, settings, settings_owner_id)
        await db.commit()
        return settings

    @classmethod
    async def delete(
        cls,
        db: AsyncSession,
        integration_id: UUID,
        *,
        user_id: UUID,
        settings_owner_id: UUID | None = None,
    ) -> IntegrationSettings:
        initial = await _load_integrations(db, settings_owner_id)
        current = cls._find(initial, integration_id)
        if current is None:
            raise IntegrationSettingsError("integration.notFound")
        if current.execution_agent_id is None:
            raise IntegrationSettingsError("executionAgent.notFound")
        await cls._owned_agent(db, user_id, current.execution_agent_id)
        settings = await _load_integrations(db, settings_owner_id, for_update=True)
        instance = cls._find(settings, integration_id)
        if instance is None:
            raise IntegrationSettingsError("integration.notFound")
        if instance.execution_agent_id != current.execution_agent_id:
            raise IntegrationSettingsError("integration.changed")
        settings.instances = [item for item in settings.instances if item.id != integration_id]
        await _save_settings(db, settings, settings_owner_id)
        await db.commit()
        return settings
