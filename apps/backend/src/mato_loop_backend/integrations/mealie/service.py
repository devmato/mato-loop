from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend.integrations.mealie.adapter import MEALIE_CREDENTIAL_KIND
from mato_loop_backend.integrations.registry import validate_integration
from mato_loop_backend.integrations.service import (
    IntegrationSettingsDocument,
    IntegrationSettingsError,
    _load_integrations,
    save_with_policy_cleanup,
)
from mato_loop_backend.security import delete_credential, store_credential
from mato_loop_backend.settings import IntegrationInstance, IntegrationSettings


class MealieIntegrationService:
    @staticmethod
    async def _save(
        db: AsyncSession,
        settings: IntegrationSettingsDocument,
        *,
        owner_id: UUID | None = None,
        clear_policies: UUID | None = None,
    ) -> IntegrationSettings:
        return await save_with_policy_cleanup(
            db, settings, owner_id=owner_id, clear_policies=clear_policies
        )

    @staticmethod
    def _find(
        settings: IntegrationSettingsDocument, integration_id: UUID
    ) -> IntegrationInstance | None:
        return next(
            (
                item
                for item in settings.instances
                if item.id == integration_id
                and item.integration_type == "recipes"
                and item.adapter_type == "mealie"
            ),
            None,
        )

    @classmethod
    async def create(
        cls,
        db: AsyncSession,
        *,
        name: str,
        base_url: str,
        token: str,
        enabled: bool,
        owner_id: UUID | None = None,
    ) -> IntegrationSettings:
        if not token or not token.strip():
            raise IntegrationSettingsError("validation.required")
        settings = await _load_integrations(db, owner_id, for_update=True)
        instance = IntegrationInstance(
            name=name,
            integration_type="recipes",
            adapter_type="mealie",
            enabled=enabled,
            config={"base_url": base_url.rstrip("/")},
        )
        instance.owner_id = owner_id
        validate_integration(instance)
        credential = await store_credential(
            db,
            owner_id=owner_id,
            kind=MEALIE_CREDENTIAL_KIND,
            label=name,
            payload={"token": token},
        )
        instance.credential_id = credential.id
        settings.instances.append(instance)
        return await cls._save(db, settings, owner_id=owner_id)

    @classmethod
    async def update(
        cls,
        db: AsyncSession,
        integration_id: UUID,
        *,
        name: str,
        base_url: str,
        token: str | None,
        enabled: bool,
        owner_id: UUID | None = None,
        expected_instance: IntegrationInstance | None = None,
    ) -> IntegrationSettings:
        settings = await _load_integrations(db, owner_id, for_update=True, expected_instance=expected_instance)
        instance = cls._find(settings, integration_id)
        if instance is None:
            raise IntegrationSettingsError("integration.notFound")
        if token is not None and not token.strip():
            raise IntegrationSettingsError("validation.required")
        config = {"base_url": base_url.rstrip("/")}
        connection_changed = instance.config != config
        old_credential_id = instance.credential_id
        instance.name = name
        instance.enabled = enabled
        instance.config = config
        validate_integration(instance)
        if token is not None:
            credential = await store_credential(
                db,
                owner_id=owner_id,
                kind=MEALIE_CREDENTIAL_KIND,
                label=name,
                payload={"token": token},
            )
            instance.credential_id = credential.id
            if old_credential_id is not None:
                await delete_credential(db, old_credential_id, owner_id=owner_id)
        return await cls._save(
            db,
            settings,
            owner_id=owner_id,
            clear_policies=integration_id if connection_changed else None,
        )

    @classmethod
    async def delete(
        cls, db: AsyncSession, integration_id: UUID, *, owner_id: UUID | None = None
    ) -> IntegrationSettings:
        settings = await _load_integrations(db, owner_id, for_update=True)
        instance = cls._find(settings, integration_id)
        if instance is None:
            raise IntegrationSettingsError("integration.notFound")
        settings.instances = [item for item in settings.instances if item.id != integration_id]
        if instance.credential_id is not None:
            await delete_credential(db, instance.credential_id, owner_id=owner_id)
        return await cls._save(
            db, settings, owner_id=owner_id, clear_policies=integration_id
        )
