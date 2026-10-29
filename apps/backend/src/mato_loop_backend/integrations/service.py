from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend.settings import (
    IntegrationInstance,
    IntegrationSettings,
    SettingsStore,
    ToolSettings,
    UserIntegrationSettings,
    UserToolSettings,
)
from mato_loop_backend.settings.effective import personal_integration_tool_settings
from mato_loop_backend.settings.models import IntegrationPermissions


class IntegrationSettingsError(RuntimeError):
    def __init__(self, code: str, *, field: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.field = field


IntegrationSettingsDocument = IntegrationSettings | UserIntegrationSettings
ToolSettingsDocument = ToolSettings | UserToolSettings


def _clear_integration_policies(instance: IntegrationInstance) -> None:
    instance.permissions = IntegrationPermissions(tool_loading=instance.permissions.tool_loading)
    instance.profile_permissions = {
        profile_id: IntegrationPermissions(available=permissions.available, tool_loading=permissions.tool_loading)
        for profile_id, permissions in instance.profile_permissions.items()
        if permissions.available is not None or permissions.tool_loading is not None
    }


async def _load_integrations(
    db: AsyncSession, owner_id: UUID | None, *, for_update: bool = False,
    expected_instance: IntegrationInstance | None = None,
) -> IntegrationSettingsDocument:
    settings_type = UserIntegrationSettings if owner_id is not None else IntegrationSettings
    settings = await SettingsStore.load(
        db, settings_type, user_id=owner_id,
        for_update=for_update or expected_instance is not None,
    )
    if expected_instance is not None and not any(
        instance.id == expected_instance.id
        and instance.model_dump(mode="json") == expected_instance.model_dump(mode="json")
        for instance in settings.instances
    ):
        raise IntegrationSettingsError("integration.changed")
    for instance in settings.instances:
        instance.owner_id = owner_id
    return settings


async def _load_tools(
    db: AsyncSession, owner_id: UUID | None, *, for_update: bool = False
) -> ToolSettingsDocument:
    if owner_id is None:
        return await SettingsStore.load(db, ToolSettings, for_update=for_update)
    personal = await SettingsStore.load_optional(
        db, UserToolSettings, user_id=owner_id, for_update=for_update
    )
    if personal is not None:
        return personal
    instance = await SettingsStore.load(db, ToolSettings)
    return personal_integration_tool_settings(instance)


async def _save_settings(
    db: AsyncSession,
    settings: IntegrationSettingsDocument | ToolSettingsDocument,
    owner_id: UUID | None,
) -> None:
    await SettingsStore.save(db, settings, user_id=owner_id)


async def save_with_policy_cleanup(
    db: AsyncSession,
    settings: IntegrationSettingsDocument,
    *,
    owner_id: UUID | None,
    clear_policies: UUID | None = None,
) -> IntegrationSettingsDocument:
    if clear_policies is not None:
        instance = next((item for item in settings.instances if item.id == clear_policies), None)
        if instance is not None:
            _clear_integration_policies(instance)
        tools = await _load_tools(db, owner_id, for_update=True)
        integration_id = str(clear_policies)
        if tools.resource_labels.pop(integration_id, None) is not None:
            await _save_settings(db, tools, owner_id)
    await _save_settings(db, settings, owner_id)
    await db.commit()
    return settings
