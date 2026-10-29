from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    StrictBool,
    ValidationError,
    field_validator,
    model_validator,
)

from mato_loop_backend.db.session import AsyncSessionFactory
from mato_loop_backend.execution.client import ExecutionClientError
from mato_loop_backend.integrations.arcane.service import ArcaneIntegrationService
from mato_loop_backend.integrations.bookstack.service import BookStackIntegrationService
from mato_loop_backend.integrations.browser.adapter import BrowserIntegrationService
from mato_loop_backend.integrations.caldav.service import (
    CalDavIntegrationService,
    caldav_defaults_payload,
    discover_caldav_resources,
    save_caldav_defaults,
)
from mato_loop_backend.integrations.mail.service import MailIntegrationService
from mato_loop_backend.integrations.mcp.client import McpClientError
from mato_loop_backend.integrations.mcp.service import McpIntegrationService
from mato_loop_backend.integrations.mealie.service import MealieIntegrationService
from mato_loop_backend.integrations.opencode.adapter import (
    OpenCodeIntegrationService,
    OpenCodeSettingsError,
)
from mato_loop_backend.integrations.opencode.client import OpenCodeError
from mato_loop_backend.integrations.openscad.service import OpenScadIntegrationService
from mato_loop_backend.integrations.registry import integration_adapter
from mato_loop_backend.integrations.searxng.service import SearchIntegrationService
from mato_loop_backend.integrations.service import (
    IntegrationSettingsError,
    _load_tools,
)
from mato_loop_backend.integrations.sonobin.adapter import sonobin_base_url
from mato_loop_backend.integrations.sonobin.service import SonoBinIntegrationService
from mato_loop_backend.integrations.tbs_desk.adapter import TbsDeskIntegrationService
from mato_loop_backend.integrations.tbs_desk.client import TbsDeskError
from mato_loop_backend.settings import (
    IntegrationAdapterAvailability,
    IntegrationInstance,
    IntegrationSettings,
    SettingsStore,
    UserIntegrationSettings,
)
from mato_loop_backend.tools.arguments import PersonalSettingsArguments
from mato_loop_backend.tools.context import ToolContext
from mato_loop_backend.tools.contracts import ToolResult
from mato_loop_backend.tools.secrets import resolve_secret_refs

logger = logging.getLogger(__name__)

# Each type uses its existing product service. The map only describes model-writable fields.
_CONFIG_FIELDS = {
    "searxng": ("base_url",),
    "caldav": ("base_url", "username"),
    "imap_smtp": ("imap_host", "imap_port", "imap_security", "smtp_host", "smtp_port", "smtp_security", "username", "from_address"),
    "tbs-desk": ("base_url", "username"),
    "bookstack": ("base_url",),
    "mealie": ("base_url",),
    "arcane": ("base_url",),
    "sonobin": ("base_url",),
    "openscad": ("execution_agent_id", "binary_path"),
    "opencode": ("base_url", "username"),
    "browser": ("execution_agent_id",),
    "mcp_streamable_http": ("endpoint",),
}
_SECRETS = {
    "caldav": ("password", "username"), "imap_smtp": ("password", "username"),
    "tbs-desk": ("password", "username"), "bookstack": ("token_id", "token_secret"),
    "mealie": ("token",), "arcane": ("api_key",),
    "sonobin": ("bearer_token",), "opencode": ("password", "username"),
    "mcp_streamable_http": ("bearer_token",),
}
_SERVICES = {
    "searxng": SearchIntegrationService, "caldav": CalDavIntegrationService,
    "imap_smtp": MailIntegrationService, "tbs-desk": TbsDeskIntegrationService,
    "bookstack": BookStackIntegrationService, "mealie": MealieIntegrationService,
    "arcane": ArcaneIntegrationService, "sonobin": SonoBinIntegrationService,
    "openscad": OpenScadIntegrationService, "opencode": OpenCodeIntegrationService,
    "browser": BrowserIntegrationService, "mcp_streamable_http": McpIntegrationService,
}


class IntegrationChanges(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=160)
    enabled: StrictBool | None = None
    base_url: HttpUrl | None = None
    endpoint: HttpUrl | None = None
    username: str | None = Field(default=None, min_length=1, max_length=320)
    imap_host: str | None = None
    imap_port: int | None = Field(default=None, ge=1, le=65535, strict=True)
    imap_security: str | None = None
    smtp_host: str | None = None
    smtp_port: int | None = Field(default=None, ge=1, le=65535, strict=True)
    smtp_security: str | None = None
    from_address: str | None = None
    execution_agent_id: UUID | None = None
    binary_path: str | None = None
    secret_refs: dict[str, UUID | str] = Field(default_factory=dict)
    remove_credential: StrictBool = False

    @field_validator("secret_refs", mode="before")
    @classmethod
    def normalize_refs(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        converted = {}
        for key, reference in value.items():
            if isinstance(reference, str):
                try:
                    reference = UUID(reference)
                except ValueError:
                    pass
            converted[key] = reference
        return converted

    @model_validator(mode="after")
    def validate_values(self) -> IntegrationChanges:
        if any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("integration fields must not be null")
        for url in (self.base_url, self.endpoint):
            if url is not None and (
                url.username is not None or url.password is not None
                or url.query is not None or url.fragment is not None
            ):
                raise ValueError("integration URLs must not contain credentials, queries, or fragments")
        if self.name is not None and not self.name.strip():
            raise ValueError("integration name must not be blank")
        if self.secret_refs and self.remove_credential:
            raise ValueError("cannot set and remove credentials together")
        for reference in self.secret_refs.values():
            if isinstance(reference, UUID):
                continue
            prefix, separator, raw_id = reference.partition(":")
            try:
                valid = separator and prefix in {"run-secret", "session-secret"} and str(UUID(raw_id)) == raw_id
            except ValueError:
                valid = False
            if not valid:
                raise ValueError("invalid secret reference")
        return self


def _payload(item: IntegrationInstance) -> dict[str, Any]:
    return {
        "id": str(item.id), "adapterType": item.adapter_type, "name": item.name,
        "enabled": item.enabled, "config": {
            field: str(value) if isinstance(value, UUID) else value
            for field, value in ({"base_url": sonobin_base_url(item.config.get("endpoint") or item.config.get("base_url"))}
                                 if item.adapter_type == "sonobin" else item.config).items()
            if field in _CONFIG_FIELDS[item.adapter_type] and field != "username"
        },
        "hasCredential": item.credential_id is not None,
        **({"authenticationMode": "oauth" if item.config.get("oauth") is not None else "bearer" if item.credential_id else "none"}
           if item.adapter_type == "mcp_streamable_http" else {}),
        **({"executionAgentId": str(item.execution_agent_id)} if item.execution_agent_id else {}),
    }


async def _settings(context: ToolContext) -> tuple[IntegrationSettings, UserIntegrationSettings]:
    global_settings = await SettingsStore.load(context.db, IntegrationSettings)
    from mato_loop_backend.settings.effective import personal_settings_allowed

    if await personal_settings_allowed(context.db, context.session.user_id, False):
        global_settings = global_settings.model_copy(deep=True)
        for adapter_type in _CONFIG_FIELDS:
            if global_settings.availability_for(adapter_type) == IntegrationAdapterAvailability.INSTANCE_ONLY:
                global_settings.adapter_availability[adapter_type] = IntegrationAdapterAvailability.FULL
    personal = await SettingsStore.load(
        context.db, UserIntegrationSettings, user_id=context.session.user_id
    )
    return global_settings, personal


async def _caldav_defaults(
    arguments: PersonalSettingsArguments, context: ToolContext,
    authorize: Callable[..., Awaitable[ToolResult | None]],
) -> ToolResult:
    assert arguments.integration_id is not None
    changes = arguments.changes or {}
    if set(changes) - {"default_resource_key", "default_task_list_key"}:
        return ToolResult.error("tool.invalidArguments")
    global_settings, personal = await _settings(context)
    item = next((item for item in personal.instances if item.id == arguments.integration_id
                 and item.adapter_type == "caldav"), None)
    if item is None:
        return ToolResult.error("integration.notFound")
    if global_settings.availability_for("caldav") != IntegrationAdapterAvailability.FULL:
        return ToolResult.error("integration.personalAdapterDisabled")
    denied = await authorize(context, arguments.action, "integrations", integration_id=item.id,
                             fields=tuple(sorted(changes)), changes=changes)
    if denied:
        return denied
    current_global, current_personal = await _settings(context)
    current = next((candidate for candidate in current_personal.instances if candidate.id == item.id), None)
    if current_global.availability_for("caldav") != IntegrationAdapterAvailability.FULL:
        return ToolResult.error("integration.personalAdapterDisabled")
    if current is None or current.model_dump(mode="json") != item.model_dump(mode="json"):
        return ToolResult.error("integration.changed")
    current.owner_id = context.session.user_id
    try:
        resources = await discover_caldav_resources(context.db, current)
    except IntegrationSettingsError as exc:
        return ToolResult.error(exc.code)
    tools = await _load_tools(context.db, context.session.user_id,
                              for_update=arguments.action == "update")
    if arguments.action == "update":
        latest = await SettingsStore.load(
            context.db, UserIntegrationSettings, user_id=context.session.user_id, for_update=True
        )
        if not any(candidate.id == current.id and candidate.model_dump(mode="json") ==
                   current.model_dump(mode="json") for candidate in latest.instances):
            return ToolResult.error("integration.changed")
        try:
            await save_caldav_defaults(
                context.db, context.session.user_id, current, resources, tools, changes,
            )
        except IntegrationSettingsError as exc:
            return ToolResult.error(exc.code, field=exc.field)
    return ToolResult.json(caldav_defaults_payload(current, resources, tools))


async def manage_integrations(
    arguments: PersonalSettingsArguments, context: ToolContext,
    authorize: Callable[..., Awaitable[ToolResult | None]],
) -> ToolResult:
    if arguments.adapter_type == "search_selection":
        return ToolResult.error("integration.personalAdapterDisabled")
    if arguments.adapter_type == "caldav_defaults":
        return await _caldav_defaults(arguments, context, authorize)
    action = arguments.action
    global_settings, personal = await _settings(context)
    instances = [item for item in personal.instances if item.adapter_type in _CONFIG_FIELDS]
    current = next((item for item in instances if item.id == arguments.integration_id), None)
    if action == "get":
        denied = await authorize(context, action, "integrations", integration_id=arguments.integration_id)
        if denied:
            return denied
        global_settings, personal = await _settings(context)
        instances = [item for item in personal.instances if item.adapter_type in _CONFIG_FIELDS]
        if arguments.integration_id is not None and not any(
            item.id == arguments.integration_id for item in instances
        ):
            return ToolResult.error("integration.notFound")
        return ToolResult.json({"integrations": [
            _payload(item) for item in instances
            if arguments.integration_id is None or item.id == arguments.integration_id
        ]})
    if action != "create" and current is None:
        return ToolResult.error("integration.notFound")
    adapter_type = arguments.adapter_type or (current.adapter_type if current else None)
    if adapter_type not in _CONFIG_FIELDS or (current and current.adapter_type != adapter_type):
        return ToolResult.error("tool.invalidArguments")
    if action != "delete" and global_settings.availability_for(adapter_type) != IntegrationAdapterAvailability.FULL:
        return ToolResult.error("integration.personalAdapterDisabled")
    changes = None
    if action != "delete":
        try:
            changes = IntegrationChanges.model_validate(arguments.changes)
        except ValidationError:
            return ToolResult.error("tool.invalidArguments")
        allowed = {"name", "enabled", "secret_refs", *_CONFIG_FIELDS[adapter_type]}
        if adapter_type in {"mcp_streamable_http", "opencode"}:
            allowed.add("remove_credential")
        if changes.model_fields_set - allowed or set(changes.secret_refs) - set(_SECRETS.get(adapter_type, ())):
            return ToolResult.error("tool.invalidArguments")
        if changes.remove_credential and adapter_type not in {"mcp_streamable_http", "opencode"}:
            return ToolResult.error("tool.invalidArguments")
        if adapter_type == "bookstack" and len(changes.secret_refs) == 1:
            return ToolResult.error("tool.invalidArguments")
        if action == "create" and (
            changes.name is None or any(
                getattr(changes, field) is None and field not in changes.secret_refs
                for field in _CONFIG_FIELDS[adapter_type]
            )
            or any(field not in changes.secret_refs for field in _SECRETS.get(adapter_type, ())
                   if field != "username" and adapter_type not in {"opencode", "mcp_streamable_http"})
            or changes.remove_credential
        ):
            return ToolResult.error("tool.invalidArguments")
        config = dict(current.config) if current is not None else {}
        if adapter_type == "sonobin" and current is not None:
            config["base_url"] = sonobin_base_url(current.config["endpoint"])
        for field in _CONFIG_FIELDS[adapter_type]:
            value = getattr(changes, field)
            if value is not None:
                config[field] = str(value) if isinstance(value, (UUID, HttpUrl)) else value
        if adapter_type in {"mcp_streamable_http", "sonobin"}:
            try:
                config["endpoint"] = str(_SERVICES[adapter_type].config_model(
                    endpoint=config["base_url"] if adapter_type == "sonobin" else config["endpoint"],
                ).endpoint)
            except (ValueError, KeyError):
                return ToolResult.error("tool.invalidArguments")
        elif adapter_type not in {"opencode", "browser"}:
            adapter = next((item for item in (integration_adapter(kind, adapter_type) for kind in (
                "search", "calendar", "mail", "support", "knowledge", "recipes", "infrastructure", "openscad"
            )) if item is not None), None)
            if adapter is None:
                return ToolResult.error("tool.invalidArguments")
            if adapter_type == "openscad":
                config = {"binary_path": config["binary_path"]}
            if "username" not in changes.secret_refs:
                try:
                    adapter.config_model.model_validate(config)
                except (ValidationError, KeyError):
                    return ToolResult.error("tool.invalidArguments")
    denied = await authorize(
        context, action, "integrations", integration_id=arguments.integration_id,
        fields=tuple(sorted((arguments.changes or {}).keys())), changes=arguments.changes,
    )
    if denied:
        return denied
    credential_values = await resolve_secret_refs(context, changes.secret_refs) if changes else {}
    if isinstance(credential_values, ToolResult):
        return credential_values
    latest_global, latest_personal = await _settings(context)
    if action != "delete" and latest_global.availability_for(adapter_type) != IntegrationAdapterAvailability.FULL:
        return ToolResult.error("integration.personalAdapterDisabled")
    if current is not None and not any(
        item.id == current.id and item.model_dump(mode="json") == current.model_dump(mode="json")
        for item in latest_personal.instances
    ):
        return ToolResult.error("integration.changed")
    if any(
        not value or len(value) > (4_096 if adapter_type == "opencode" else 10_000)
        for value in credential_values.values()
    ) or (
        adapter_type == "arcane" and any(
            not 33 <= ord(character) <= 126
            for value in credential_values.values() for character in value
        )
    ):
        return ToolResult.error("tool.invalidArguments")
    if "username" in credential_values:
        if adapter_type == "opencode" and (
            len(credential_values["username"]) > 255
            or ":" in credential_values["username"]
            or any(character in "\r\n" for character in credential_values["username"])
        ):
            return ToolResult.error("tool.invalidArguments")
        config["username"] = credential_values["username"]
        if adapter_type in {"caldav", "imap_smtp", "tbs-desk"}:
            adapter = next((item for item in (integration_adapter(kind, adapter_type) for kind in (
                "calendar", "mail", "support"
            )) if item is not None), None)
            try:
                adapter.config_model.model_validate(config)
            except (ValidationError, AttributeError):
                return ToolResult.error("tool.invalidArguments")
    owner_id = context.session.user_id
    if action == "delete":
        try:
            kwargs = {"user_id": owner_id} if adapter_type == "opencode" else (
                {"user_id": owner_id, "settings_owner_id": owner_id}
                if adapter_type == "openscad" else {"owner_id": owner_id}
            )
            async with AsyncSessionFactory() as service_db:
                await _SERVICES[adapter_type].delete(service_db, arguments.integration_id, **kwargs)
        except (IntegrationSettingsError, McpClientError, OpenCodeSettingsError, OpenCodeError, ExecutionClientError, TbsDeskError) as exc:
            logger.warning("Personal integration deletion failed with %s", type(exc).__name__)
            return ToolResult.error(exc.code)
        return ToolResult.json({"deleted": True, "id": str(arguments.integration_id)})
    name = changes.name if changes and changes.name is not None else current.name if current else ""
    enabled = changes.enabled if changes and changes.enabled is not None else current.enabled if current else True
    operation = "create" if action == "create" else "update"
    identifier = () if action == "create" else (arguments.integration_id,)
    base = {"name": name, "enabled": enabled}
    try:
        if adapter_type == "searxng":
            service, kwargs = SearchIntegrationService, {**base, "provider_type": "searxng", "base_url": config.get("base_url"), "owner_id": owner_id}
        elif adapter_type == "caldav":
            service, kwargs = CalDavIntegrationService, {**base, "base_url": config.get("base_url"), "username": config.get("username"), "password": credential_values.get("password"), "owner_id": owner_id}
        elif adapter_type == "imap_smtp":
            service, kwargs = MailIntegrationService, {**base, "config": config, "password": credential_values.get("password"), "owner_id": owner_id}
        elif adapter_type == "tbs-desk":
            service, kwargs = TbsDeskIntegrationService, {**base, "base_url": config.get("base_url"), "username": config.get("username"), "password": credential_values.get("password"), "owner_id": owner_id}
        elif adapter_type == "bookstack":
            service, kwargs = BookStackIntegrationService, {**base, "base_url": config.get("base_url"), "token_id": credential_values.get("token_id"), "token_secret": credential_values.get("token_secret"), "owner_id": owner_id}
        elif adapter_type == "mealie":
            service, kwargs = MealieIntegrationService, {**base, "base_url": config.get("base_url"), "token": credential_values.get("token"), "owner_id": owner_id}
        elif adapter_type == "arcane":
            service, kwargs = ArcaneIntegrationService, {**base, "base_url": config.get("base_url"), "api_key": credential_values.get("api_key"), "owner_id": owner_id}
        elif adapter_type in {"mcp_streamable_http", "sonobin"}:
            service = McpIntegrationService if adapter_type == "mcp_streamable_http" else SonoBinIntegrationService
            kwargs = {**base, "endpoint": config.get("endpoint"), "token": credential_values.get("bearer_token"), "owner_id": owner_id}
            if action == "update":
                kwargs["remove_token"] = changes.remove_credential
        elif adapter_type == "openscad":
            service, kwargs = OpenScadIntegrationService, {**base, "user_id": owner_id, "execution_agent_id": UUID(str(changes.execution_agent_id or current.execution_agent_id)), "binary_path": config.get("binary_path"), "settings_owner_id": owner_id}
        elif adapter_type == "opencode":
            service, kwargs = OpenCodeIntegrationService, {**base, "user_id": owner_id, "base_url": config.get("base_url"), "username": config.get("username"), "password": credential_values.get("password")}
            if action == "update":
                kwargs["remove_password"] = changes.remove_credential
        else:
            service, kwargs = BrowserIntegrationService, {**base, "owner_id": owner_id, "execution_agent_id": UUID(str(changes.execution_agent_id or current.execution_agent_id))}
            if action == "update":
                kwargs = {"owner_id": owner_id, "changes": {
                    key: getattr(changes, key) for key in changes.model_fields_set if key in {"name", "enabled", "execution_agent_id"}
                }}
        # Product services commit or roll back their own transaction, never the agent's state.
        if action == "update":
            kwargs["expected_instance"] = current
        async with AsyncSessionFactory() as service_db:
            result = await getattr(service, operation)(service_db, *identifier, **kwargs)
    except (IntegrationSettingsError, McpClientError, OpenCodeSettingsError, OpenCodeError, ExecutionClientError, TbsDeskError) as exc:
        logger.warning("Personal integration configuration failed with %s", type(exc).__name__)
        return ToolResult.error(exc.code)
    instances = result.instances if isinstance(result, UserIntegrationSettings) else (
        [result] if isinstance(result, IntegrationInstance) else []
    )
    if action == "create":
        before = {item.id for item in latest_personal.instances}
        item = next((item for item in instances if item.id not in before), None)
    else:
        item = next((item for item in instances if item.id == arguments.integration_id), None)
    return ToolResult.json({**_payload(item), "effectiveFrom": "next_run"}) if item else ToolResult.error("tool.executionFailed")
