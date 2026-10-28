from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend.integrations.arcane.adapter import ARCANE_CREDENTIAL_KIND
from mato_loop_backend.integrations.registry import validate_integration
from mato_loop_backend.integrations.service import (
    IntegrationSettingsDocument,
    IntegrationSettingsError,
    _load_integrations,
    _save_settings,
    save_with_policy_cleanup,
)
from mato_loop_backend.security import delete_credential, store_credential
from mato_loop_backend.settings import IntegrationInstance


class ArcaneIntegrationService:
    @staticmethod
    def _find(settings: IntegrationSettingsDocument, integration_id: UUID) -> IntegrationInstance | None:
        return next((item for item in settings.instances if item.id == integration_id
                     and item.integration_type == "infrastructure" and item.adapter_type == "arcane"), None)

    @classmethod
    async def create(cls, db: AsyncSession, *, name: str, base_url: str, api_key: str,
                     enabled: bool, owner_id: UUID | None = None) -> IntegrationSettingsDocument:
        settings = await _load_integrations(db, owner_id, for_update=True)
        instance = IntegrationInstance(name=name, integration_type="infrastructure", adapter_type="arcane",
                                       enabled=enabled, config={"base_url": base_url.rstrip("/")})
        instance.owner_id = owner_id
        validate_integration(instance)
        credential = await store_credential(db, owner_id=owner_id, kind=ARCANE_CREDENTIAL_KIND,
                                            label=name, payload={"api_key": api_key})
        instance.credential_id = credential.id
        settings.instances.append(instance)
        await _save_settings(db, settings, owner_id)
        await db.commit()
        return settings

    @classmethod
    async def update(cls, db: AsyncSession, integration_id: UUID, *, name: str, base_url: str,
                     api_key: str | None, enabled: bool, owner_id: UUID | None = None,
                     expected_instance: IntegrationInstance | None = None) -> IntegrationSettingsDocument:
        settings = await _load_integrations(db, owner_id, for_update=True, expected_instance=expected_instance)
        instance = cls._find(settings, integration_id)
        if instance is None:
            raise IntegrationSettingsError("integration.notFound")
        config = {"base_url": base_url.rstrip("/")}
        changed = instance.config != config or api_key is not None
        previous = instance.credential_id
        instance.name, instance.config, instance.enabled = name, config, enabled
        validate_integration(instance)
        if api_key is not None:
            credential = await store_credential(db, owner_id=owner_id, kind=ARCANE_CREDENTIAL_KIND,
                                                label=name, payload={"api_key": api_key})
            instance.credential_id = credential.id
            if previous is not None:
                await delete_credential(db, previous, owner_id=owner_id)
        return await save_with_policy_cleanup(
            db, settings, owner_id=owner_id, clear_policies=integration_id if changed else None
        )

    @classmethod
    async def delete(cls, db: AsyncSession, integration_id: UUID, *, owner_id: UUID | None = None) -> None:
        settings = await _load_integrations(db, owner_id, for_update=True)
        instance = cls._find(settings, integration_id)
        if instance is None:
            raise IntegrationSettingsError("integration.notFound")
        settings.instances = [item for item in settings.instances if item.id != integration_id]
        if instance.credential_id is not None:
            await delete_credential(db, instance.credential_id, owner_id=owner_id)
        await save_with_policy_cleanup(db, settings, owner_id=owner_id, clear_policies=integration_id)
