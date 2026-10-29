from __future__ import annotations

from typing import Literal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend.api.errors import ApiError
from mato_loop_backend.integrations.registry import integration_adapters
from mato_loop_backend.models import UserAccount
from mato_loop_backend.settings import (
    IntegrationAdapterAvailability,
    IntegrationInstance,
    IntegrationSettings,
    SettingsStore,
    UserIntegrationSettings,
)

SettingsApiScope = Literal["instance", "personal"]


IntegrationSettingsDocument = IntegrationSettings | UserIntegrationSettings


async def _load_scoped_integrations(
    db: AsyncSession,
    scope: SettingsApiScope,
    user: UserAccount | None,
    *,
    for_update: bool = False,
    mutation: bool = False,
    adapter_type: str | None = None,
) -> tuple[IntegrationSettingsDocument, UUID | None]:
    if scope == "instance":
        if user is not None and not getattr(user, "is_admin", False):
            raise ApiError(403, "auth.adminRequired")
        adapter = next(
            (item for item in integration_adapters() if item.adapter_type == adapter_type),
            None,
        )
        if adapter is not None and adapter.personal_only:
            raise ApiError(409, "integration.personalOnly")
        settings = await SettingsStore.load(
            db, IntegrationSettings, for_update=for_update
        )
        if adapter_type is not None and settings.availability_for(
            adapter_type
        ) == IntegrationAdapterAvailability.DISABLED:
            raise ApiError(409, "integration.adapterDisabled")
        return settings, None
    if user is None:
        raise ApiError(401, "auth.notAuthenticated")
    adapter = next((item for item in integration_adapters() if item.adapter_type == adapter_type), None)
    if adapter is not None and adapter.instance_only:
        raise ApiError(409, "integration.personalAdapterDisabled")
    instance = await SettingsStore.load(db, IntegrationSettings)
    availability = (
        instance.availability_for(adapter_type) if adapter_type is not None else None
    )
    if availability == IntegrationAdapterAvailability.DISABLED:
        raise ApiError(409, "integration.adapterDisabled")
    if availability == IntegrationAdapterAvailability.INSTANCE_ONLY and not user.is_admin:
        raise ApiError(409, "integration.personalAdapterDisabled")
    settings = await SettingsStore.load(
        db, UserIntegrationSettings, user_id=user.id, for_update=for_update
    )
    for item in settings.instances:
        item.owner_id = user.id
    return settings, user.id


def _integration(
    settings: IntegrationSettings, integration_id: UUID
) -> IntegrationInstance | None:
    return next((item for item in settings.instances if item.id == integration_id), None)
