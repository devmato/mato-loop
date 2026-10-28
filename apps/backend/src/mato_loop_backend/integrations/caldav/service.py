from __future__ import annotations

import logging
import re
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend.integrations.caldav.adapter import CALDAV_CREDENTIAL_KIND
from mato_loop_backend.integrations.contracts import IntegrationResource
from mato_loop_backend.integrations.registry import integration_adapter, validate_integration
from mato_loop_backend.integrations.service import (
    IntegrationSettingsDocument,
    IntegrationSettingsError,
    ToolSettingsDocument,
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
from mato_loop_backend.settings import (
    DefaultCalendarSettings,
    DefaultTaskListSettings,
    IntegrationInstance,
    IntegrationSettings,
)

logger = logging.getLogger(__name__)
_RESOURCE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,499}$")


class CalDavIntegrationService:
    @staticmethod
    async def _save(
        db: AsyncSession,
        settings: IntegrationSettingsDocument,
        *,
        owner_id: UUID | None = None,
        clear_policies: UUID | None = None,
    ) -> IntegrationSettings:
        if clear_policies is not None:
            instance = next((item for item in settings.instances if item.id == clear_policies), None)
            if instance is not None:
                _clear_integration_policies(instance)
            tools = await _load_tools(db, owner_id, for_update=True)
            integration_id = str(clear_policies)
            changed = tools.resource_labels.pop(integration_id, None) is not None
            if (
                tools.default_calendar is not None
                and str(tools.default_calendar.integration_id) == integration_id
            ):
                tools.default_calendar = None
                changed = True
            if (
                tools.default_task_list is not None
                and str(tools.default_task_list.integration_id) == integration_id
            ):
                tools.default_task_list = None
                changed = True
            if changed:
                await _save_settings(db, tools, owner_id)
        await _save_settings(db, settings, owner_id)
        await db.commit()
        return settings

    @classmethod
    async def create(
        cls,
        db: AsyncSession,
        *,
        name: str,
        base_url: str,
        username: str,
        password: str,
        enabled: bool,
        owner_id: UUID | None = None,
    ) -> IntegrationSettings:
        if not password:
            raise IntegrationSettingsError("validation.required")
        settings = await _load_integrations(db, owner_id, for_update=True)
        instance = IntegrationInstance(
            name=name,
            integration_type="calendar",
            adapter_type="caldav",
            enabled=enabled,
            config={"base_url": base_url, "username": username},
        )
        instance.owner_id = owner_id
        validate_integration(instance)
        credential = await store_credential(
            db,
            owner_id=instance.owner_id,
            kind=CALDAV_CREDENTIAL_KIND,
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
        base_url: str,
        username: str,
        password: str | None,
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
                and item.integration_type == "calendar"
                and item.adapter_type == "caldav"
            ),
            None,
        )
        if instance is None:
            raise IntegrationSettingsError("integration.notFound")
        if password == "":
            raise IntegrationSettingsError("validation.required")

        config = {"base_url": base_url, "username": username}
        connection_changed = instance.config != config
        old_credential_id = instance.credential_id
        instance.name = name
        instance.enabled = enabled
        instance.config = config
        validate_integration(instance)
        if password is not None:
            credential = await store_credential(
                db,
                owner_id=instance.owner_id,
                kind=CALDAV_CREDENTIAL_KIND,
                label=name,
                payload={"password": password},
            )
            instance.credential_id = credential.id
            if old_credential_id is not None:
                await delete_credential(
                    db, old_credential_id, owner_id=instance.owner_id
                )
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
        instance = next(
            (
                item
                for item in settings.instances
                if item.id == integration_id
                and item.integration_type == "calendar"
                and item.adapter_type == "caldav"
            ),
            None,
        )
        if instance is None:
            raise IntegrationSettingsError("integration.notFound")
        settings.instances = [item for item in settings.instances if item.id != integration_id]
        if instance.credential_id is not None:
            await delete_credential(
                db, instance.credential_id, owner_id=instance.owner_id
            )
        return await cls._save(
            db, settings, owner_id=owner_id, clear_policies=integration_id
        )


async def discover_caldav_resources(
    db: AsyncSession, instance: IntegrationInstance, *, release_connection: bool = False,
) -> tuple[IntegrationResource, ...]:
    adapter = integration_adapter("calendar", "caldav")
    if adapter is None or adapter.resource_loader is None:
        raise IntegrationSettingsError("integration.resourcePoliciesUnsupported")
    try:
        credential = None
        if instance.credential_id is not None:
            record = await load_credential(
                db, instance.credential_id, owner_id=instance.owner_id,
                populate_existing=True,
            )
            if record is None or record.kind != CALDAV_CREDENTIAL_KIND:
                raise ValueError("invalid calendar credential")
            credential = read_credential(record)
        if release_connection:
            await db.rollback()
        resources = await adapter.resources(instance, credential)
        keys = set()
        for resource in resources:
            if (
                not isinstance(resource, IntegrationResource)
                or not isinstance(resource.key, str)
                or _RESOURCE_KEY.fullmatch(resource.key) is None
                or not isinstance(resource.label, str)
                or not 1 <= len(resource.label) <= 160
                or resource.label != resource.label.strip()
                or (resource.group_key is not None and (
                    not isinstance(resource.group_key, str)
                    or _RESOURCE_KEY.fullmatch(resource.group_key) is None
                ))
                or resource.key in keys
            ):
                raise ValueError("invalid calendar resource")
            keys.add(resource.key)
        return resources
    except Exception as exc:
        logger.warning("Calendar resource discovery failed with %s", type(exc).__name__)
        raise IntegrationSettingsError("integration.resourceDiscoveryFailed") from exc


def caldav_defaults_payload(
    instance: IntegrationInstance,
    resources: tuple[IntegrationResource, ...],
    tools: ToolSettingsDocument,
) -> dict:
    keys = {resource.key for resource in resources}
    calendar = tools.default_calendar
    task_list = tools.default_task_list
    return {
        "integrationId": str(instance.id),
        "resources": [
            {"key": resource.key, "groupKey": resource.group_key, "label": resource.label}
            for resource in resources
        ],
        "defaultResourceKey": (
            calendar.calendar_key
            if calendar is not None and calendar.integration_id == instance.id
            and calendar.calendar_key in keys else None
        ),
        "defaultTaskListKey": (
            task_list.task_list_key
            if task_list is not None and task_list.integration_id == instance.id
            and task_list.task_list_key in keys else None
        ),
    }


async def save_caldav_defaults(
    db: AsyncSession,
    owner_id: UUID | None,
    instance: IntegrationInstance,
    resources: tuple[IntegrationResource, ...],
    tools: ToolSettingsDocument,
    changes: dict[str, str | None],
) -> None:
    keys = {resource.key for resource in resources}
    for field, value in changes.items():
        if field not in {"default_resource_key", "default_task_list_key"} or (
            value is not None and (
                not isinstance(value, str)
                or value not in keys
                or not value.startswith(
                    "calendar:" if field == "default_resource_key" else "tasklist:"
                )
            )
        ):
            raise IntegrationSettingsError("validation.invalidChoice", field=field)
    labels = {resource.key: resource.label for resource in resources}
    if labels:
        tools.resource_labels[str(instance.id)] = labels
    else:
        tools.resource_labels.pop(str(instance.id), None)
    if "default_resource_key" in changes:
        value = changes["default_resource_key"]
        if value is not None:
            tools.default_calendar = DefaultCalendarSettings(
                integration_id=instance.id, calendar_key=value
            )
        elif tools.default_calendar is not None and tools.default_calendar.integration_id == instance.id:
            tools.default_calendar = None
    if "default_task_list_key" in changes:
        value = changes["default_task_list_key"]
        if value is not None:
            tools.default_task_list = DefaultTaskListSettings(
                integration_id=instance.id, task_list_key=value
            )
        elif tools.default_task_list is not None and tools.default_task_list.integration_id == instance.id:
            tools.default_task_list = None
    await _save_settings(db, tools, owner_id)
    await db.commit()
