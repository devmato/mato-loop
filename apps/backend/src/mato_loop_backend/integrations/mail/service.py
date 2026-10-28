from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend.integrations.mail.adapter import MAIL_CREDENTIAL_KIND
from mato_loop_backend.integrations.registry import validate_integration
from mato_loop_backend.integrations.service import (
    IntegrationSettingsDocument,
    IntegrationSettingsError,
    _clear_integration_policies,
    _load_integrations,
    _save_settings,
)
from mato_loop_backend.security import delete_credential, store_credential
from mato_loop_backend.settings import IntegrationInstance, IntegrationSettings


class MailIntegrationService:
    @staticmethod
    def _find(
        settings: IntegrationSettingsDocument, integration_id: UUID
    ) -> IntegrationInstance | None:
        return next(
            (
                item
                for item in settings.instances
                if item.id == integration_id
                and item.integration_type == "mail"
                and item.adapter_type == "imap_smtp"
            ),
            None,
        )

    @staticmethod
    def _ensure_unique(
        settings: IntegrationSettingsDocument,
        config: dict,
        *,
        excluding: UUID | None = None,
    ) -> None:
        account_key = (
            config["imap_host"].casefold(),
            config["imap_port"],
            config["username"].casefold(),
        )
        if any(
            item.id != excluding
            and item.integration_type == "mail"
            and item.adapter_type == "imap_smtp"
            and (
                str(item.config.get("imap_host", "")).casefold(),
                item.config.get("imap_port"),
                str(item.config.get("username", "")).casefold(),
            )
            == account_key
            for item in settings.instances
        ):
            raise IntegrationSettingsError("mail.alreadyConfigured")

    @staticmethod
    async def _save(
        db: AsyncSession,
        settings: IntegrationSettingsDocument,
        *,
        owner_id: UUID | None,
        clear_policies: UUID | None = None,
    ) -> IntegrationSettings:
        if clear_policies is not None:
            instance = MailIntegrationService._find(settings, clear_policies)
            if instance is not None:
                _clear_integration_policies(instance)
        await _save_settings(db, settings, owner_id)
        await db.commit()
        return settings

    @classmethod
    async def create(
        cls,
        db: AsyncSession,
        *,
        name: str,
        config: dict,
        password: str,
        enabled: bool,
        owner_id: UUID | None = None,
    ) -> IntegrationSettings:
        if not password:
            raise IntegrationSettingsError("validation.required")
        settings = await _load_integrations(db, owner_id, for_update=True)
        cls._ensure_unique(settings, config)
        instance = IntegrationInstance(
            name=name,
            integration_type="mail",
            adapter_type="imap_smtp",
            enabled=enabled,
            config=config,
        )
        instance.owner_id = owner_id
        validate_integration(instance)
        credential = await store_credential(
            db,
            owner_id=owner_id,
            kind=MAIL_CREDENTIAL_KIND,
            label=name,
            payload={"password": password},
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
        config: dict,
        password: str | None,
        enabled: bool,
        owner_id: UUID | None = None,
        expected_instance: IntegrationInstance | None = None,
    ) -> IntegrationSettings:
        if password == "":
            raise IntegrationSettingsError("validation.required")
        settings = await _load_integrations(db, owner_id, for_update=True, expected_instance=expected_instance)
        instance = cls._find(settings, integration_id)
        if instance is None:
            raise IntegrationSettingsError("integration.notFound")
        cls._ensure_unique(settings, config, excluding=integration_id)
        connection_changed = instance.config != config
        old_credential_id = instance.credential_id
        instance.name = name
        instance.config = config
        instance.enabled = enabled
        validate_integration(instance)
        if password is not None:
            credential = await store_credential(
                db,
                owner_id=owner_id,
                kind=MAIL_CREDENTIAL_KIND,
                label=name,
                payload={"password": password},
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
