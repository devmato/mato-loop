from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend.integrations.contracts import IntegrationAdapterDefinition
from mato_loop_backend.integrations.opencode.client import OpenCodeClient, canonicalize_base_url
from mato_loop_backend.integrations.service import _load_integrations
from mato_loop_backend.models import Project, UserAccount
from mato_loop_backend.security import (
    delete_credential,
    load_credential,
    read_credential,
    store_credential,
)
from mato_loop_backend.settings import (
    IntegrationInstance,
    SettingsStore,
    UserIntegrationSettings,
)

OPENCODE_CREDENTIAL_KIND = "opencode.basic_auth"


class OpenCodeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: HttpUrl
    username: str = Field(default="opencode", min_length=1, max_length=255)
    server_root: str = Field(min_length=1, max_length=4096)
    server_version: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_endpoint(self) -> OpenCodeConfig:
        if (
            self.base_url.username is not None
            or self.base_url.password is not None
            or self.base_url.query is not None
            or self.base_url.fragment is not None
        ):
            raise ValueError("OpenCode URL must not contain credentials, query, or fragment")
        if ":" in self.username or any(character in "\r\n" for character in self.username):
            raise ValueError("OpenCode username is invalid")
        path = PureWindowsPath(self.server_root)
        if not path.is_absolute() and not PurePosixPath(self.server_root).is_absolute():
            raise ValueError("OpenCode server root must be absolute")
        return self

    @field_validator("server_root", "server_version")
    @classmethod
    def reject_control_characters(cls, value: str) -> str:
        if any(ord(character) < 32 for character in value):
            raise ValueError("OpenCode metadata contains control characters")
        return value


OPENCODE_ADAPTER = IntegrationAdapterDefinition(
    integration_type="coding",
    adapter_type="opencode",
    config_model=OpenCodeConfig,
    execution_location="backend",
    personal_only=True,
)


class OpenCodeSettingsError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


async def _password(
    db: AsyncSession, instance: IntegrationInstance, user_id: UUID
) -> str | None:
    if instance.credential_id is None:
        return None
    credential = await load_credential(
        db, instance.credential_id, owner_id=user_id, populate_existing=True
    )
    if credential is None or credential.kind != OPENCODE_CREDENTIAL_KIND:
        raise OpenCodeSettingsError("opencode.authenticationFailed")
    try:
        payload = read_credential(credential)
    except Exception as exc:
        raise OpenCodeSettingsError("opencode.authenticationFailed") from exc
    if set(payload) != {"password"} or not isinstance(payload["password"], str):
        raise OpenCodeSettingsError("opencode.authenticationFailed")
    return payload["password"]


async def open_code_client(
    db: AsyncSession, instance: IntegrationInstance, user_id: UUID
) -> tuple[OpenCodeClient, OpenCodeConfig]:
    if instance.owner_id not in {None, user_id}:
        raise OpenCodeSettingsError("integration.notFound")
    config = OpenCodeConfig.model_validate(instance.config)
    password = await _password(db, instance, user_id)
    client = OpenCodeClient(str(config.base_url), config.username, password)
    probe = await client.probe()
    if probe.root != config.server_root:
        await client.aclose()
        raise OpenCodeSettingsError("opencode.rootChanged")
    return client, config


class OpenCodeIntegrationService:
    @staticmethod
    async def _settings(
        db: AsyncSession, user_id: UUID, *, for_update: bool = False,
        expected_instance: IntegrationInstance | None = None,
    ) -> UserIntegrationSettings:
        return await _load_integrations(
            db, user_id, for_update=for_update, expected_instance=expected_instance,
        )

    @staticmethod
    def _find(
        settings: UserIntegrationSettings, integration_id: UUID
    ) -> IntegrationInstance | None:
        return next(
            (
                instance
                for instance in settings.instances
                if instance.id == integration_id
                and instance.integration_type == "coding"
                and instance.adapter_type == "opencode"
            ),
            None,
        )

    @staticmethod
    def _canonical_base_url(value: str) -> str:
        return canonicalize_base_url(value)

    @classmethod
    async def create(
        cls,
        db: AsyncSession,
        *,
        user_id: UUID,
        name: str,
        base_url: str,
        username: str,
        password: str | None,
        enabled: bool,
    ) -> UserIntegrationSettings:
        canonical_url = cls._canonical_base_url(base_url)
        async with OpenCodeClient(canonical_url, username, password) as client:
            probe = await client.probe()
        settings = await cls._settings(db, user_id, for_update=True)
        if any(
            item.integration_type == "coding"
            and item.adapter_type == "opencode"
            and cls._canonical_base_url(str(item.config.get("base_url", "")))
            == canonical_url
            for item in settings.instances
        ):
            raise OpenCodeSettingsError("opencode.alreadyConfigured")
        instance = IntegrationInstance(
            name=name,
            integration_type="coding",
            adapter_type="opencode",
            enabled=enabled,
            config=OpenCodeConfig(
                base_url=canonical_url,
                username=username,
                server_root=probe.root,
                server_version=probe.version,
            ).model_dump(mode="json"),
        )
        instance.owner_id = user_id
        if password is not None:
            credential = await store_credential(
                db,
                owner_id=user_id,
                kind=OPENCODE_CREDENTIAL_KIND,
                label=name,
                payload={"password": password},
            )
            instance.credential_id = credential.id
        settings.instances.append(instance)
        await SettingsStore.save(db, settings, user_id=user_id)
        await db.commit()
        return settings

    @classmethod
    async def update(
        cls,
        db: AsyncSession,
        integration_id: UUID,
        *,
        user_id: UUID,
        name: str,
        base_url: str,
        username: str,
        password: str | None,
        remove_password: bool,
        enabled: bool,
        expected_instance: IntegrationInstance | None = None,
    ) -> UserIntegrationSettings:
        if password is not None and remove_password:
            raise OpenCodeSettingsError("validation.invalidChoice")
        await db.scalar(
            select(UserAccount.id)
            .where(UserAccount.id == user_id)
            .with_for_update()
        )
        initial = await cls._settings(db, user_id)
        current = cls._find(initial, integration_id)
        if current is None:
            raise OpenCodeSettingsError("integration.notFound")
        old_config = OpenCodeConfig.model_validate(current.config)
        canonical_url = cls._canonical_base_url(base_url)
        connection_changed = (
            cls._canonical_base_url(str(old_config.base_url)) != canonical_url
            or old_config.username != username
        )
        referenced = await db.scalar(
            select(Project.id)
            .where(
                Project.user_id == user_id,
                Project.coding_integration_id == integration_id,
            )
            .limit(1)
        )
        if referenced is not None and connection_changed:
            raise OpenCodeSettingsError("opencode.integrationReferenced")
        current_password = await _password(db, current, user_id)
        effective_password = password if password is not None else None if remove_password else current_password
        if connection_changed or password is not None or remove_password or (enabled and not current.enabled):
            async with OpenCodeClient(canonical_url, username, effective_password) as client:
                probe = await client.probe()
        else:
            server_root = old_config.server_root
            server_version = old_config.server_version
        if connection_changed or password is not None or remove_password or (enabled and not current.enabled):
            server_root = probe.root
            server_version = probe.version
        settings = await cls._settings(db, user_id, for_update=True, expected_instance=expected_instance)
        instance = cls._find(settings, integration_id)
        if instance is None or instance.config != current.config:
            raise OpenCodeSettingsError("integration.changed")
        old_credential_id = instance.credential_id
        instance.name = name
        instance.enabled = enabled
        instance.config = OpenCodeConfig(
            base_url=canonical_url,
            username=username,
            server_root=server_root,
            server_version=server_version,
        ).model_dump(mode="json")
        if password is not None:
            credential = await store_credential(
                db,
                owner_id=user_id,
                kind=OPENCODE_CREDENTIAL_KIND,
                label=name,
                payload={"password": password},
            )
            instance.credential_id = credential.id
        elif remove_password:
            instance.credential_id = None
        if old_credential_id is not None and old_credential_id != instance.credential_id:
            await delete_credential(db, old_credential_id, owner_id=user_id)
        await SettingsStore.save(db, settings, user_id=user_id)
        await db.commit()
        return settings

    @classmethod
    async def delete(
        cls, db: AsyncSession, integration_id: UUID, *, user_id: UUID
    ) -> None:
        await db.scalar(
            select(UserAccount.id)
            .where(UserAccount.id == user_id)
            .with_for_update()
        )
        if await db.scalar(
            select(Project.id)
            .where(
                Project.user_id == user_id,
                Project.coding_integration_id == integration_id,
            )
            .limit(1)
        ) is not None:
            raise OpenCodeSettingsError("opencode.integrationReferenced")
        settings = await cls._settings(db, user_id, for_update=True)
        instance = cls._find(settings, integration_id)
        if instance is None:
            raise OpenCodeSettingsError("integration.notFound")
        settings.instances = [item for item in settings.instances if item.id != integration_id]
        if instance.credential_id is not None:
            await delete_credential(db, instance.credential_id, owner_id=user_id)
        await SettingsStore.save(db, settings, user_id=user_id)
        await db.commit()

    @classmethod
    async def test(
        cls, db: AsyncSession, integration_id: UUID, *, user_id: UUID
    ) -> dict[str, str]:
        settings = await cls._settings(db, user_id)
        instance = cls._find(settings, integration_id)
        if instance is None:
            raise OpenCodeSettingsError("integration.notFound")
        client, config = await open_code_client(db, instance, user_id)
        async with client:
            await client.list_models()
        return {"status": "ok", "version": config.server_version}
