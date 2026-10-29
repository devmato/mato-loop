from __future__ import annotations

from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend.integrations.jina.adapter import (
    JINA_CREDENTIAL_KIND,
    JinaConfig,
    JinaCookie,
    JinaCredentials,
)
from mato_loop_backend.integrations.service import (
    IntegrationSettingsError,
    _load_integrations,
    save_with_policy_cleanup,
)
from mato_loop_backend.security import (
    delete_credential,
    load_credential,
    read_credential,
    store_credential,
)
from mato_loop_backend.settings import IntegrationInstance, IntegrationSettings


async def credentials(db: AsyncSession, instance: IntegrationInstance) -> JinaCredentials:
    if instance.credential_id is None:
        return JinaCredentials()
    record = await load_credential(db, instance.credential_id, owner_id=None, populate_existing=True)
    if record is None or record.kind != JINA_CREDENTIAL_KIND:
        raise IntegrationSettingsError("integration.invalidConfiguration")
    try:
        return JinaCredentials.model_validate(read_credential(record))
    except ValidationError as exc:
        raise IntegrationSettingsError("integration.invalidConfiguration") from exc


def find(settings: IntegrationSettings, integration_id: UUID) -> IntegrationInstance:
    instance = next((item for item in settings.instances if item.id == integration_id
                     and item.integration_type == "webfetch" and item.adapter_type == "jina"), None)
    if instance is None:
        raise IntegrationSettingsError("integration.notFound")
    return instance


class JinaIntegrationService:
    @staticmethod
    async def save(
        db: AsyncSession, integration_id: UUID | None, *, name: str,
        base_url: str, enabled: bool, api_key: str | None,
        timeout_seconds: int = 90, remove_overlay: bool = False,
        cookies: list[dict] | None = None,
    ) -> IntegrationSettings:
        config = JinaConfig(base_url=base_url, timeout_seconds=timeout_seconds, remove_overlay=remove_overlay)
        if api_key is not None and any(not 33 <= ord(char) <= 126 for char in api_key):
            raise IntegrationSettingsError("validation.invalid", field="api_key")
        validated_cookies = [JinaCookie.model_validate(item) for item in cookies] if cookies is not None else None
        if validated_cookies is not None and any(not cookie.reader_value_supported for cookie in validated_cookies):
            raise IntegrationSettingsError("validation.invalid", field="cookies")
        settings = await _load_integrations(db, None, for_update=True)
        instance = find(settings, integration_id) if integration_id else IntegrationInstance(
            name=name, integration_type="webfetch", adapter_type="jina",
        )
        if api_key is not None or cookies is not None:
            secrets = await credentials(db, instance)
            if api_key is not None:
                secrets.api_key = api_key or None
            if validated_cookies is not None:
                secrets.cookies = validated_cookies
            previous = instance.credential_id
            instance.credential_id = None
            if secrets.api_key or secrets.cookies:
                credential = await store_credential(
                    db, owner_id=None, kind=JINA_CREDENTIAL_KIND, label=name,
                    payload=secrets.model_dump(mode="json", exclude_defaults=True),
                )
                instance.credential_id = credential.id
            if previous is not None:
                await delete_credential(db, previous, owner_id=None)
            config.cookie_domains = sorted({cookie.domain for cookie in secrets.cookies})
            config.has_api_key = secrets.api_key is not None
        else:
            config.cookie_domains = instance.config.get("cookie_domains", [])
            config.has_api_key = instance.config.get("has_api_key", instance.credential_id is not None)
        config_data = config.model_dump(mode="json")
        changed = instance.config != config_data
        instance.name, instance.enabled, instance.config = name, enabled, config_data
        if integration_id is None:
            settings.instances.append(instance)
        return await save_with_policy_cleanup(
            db, settings, owner_id=None,
            clear_policies=instance.id if changed and integration_id else None,
        )

    @staticmethod
    async def delete(db: AsyncSession, integration_id: UUID) -> IntegrationSettings:
        settings = await _load_integrations(db, None, for_update=True)
        instance = find(settings, integration_id)
        if instance.credential_id:
            await delete_credential(db, instance.credential_id, owner_id=None)
        settings.instances.remove(instance)
        return await save_with_policy_cleanup(db, settings, owner_id=None)
