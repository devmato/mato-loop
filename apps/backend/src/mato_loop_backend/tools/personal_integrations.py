from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
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
from pydantic_core import PydanticCustomError

from mato_loop_backend.api.errors import ApiError
from mato_loop_backend.db.session import AsyncSessionFactory
from mato_loop_backend.execution.client import ExecutionClientError, WorkspaceStatResult
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
from mato_loop_backend.integrations.mcp.stdio import (
    McpStdioBinding,
    McpStdioConfig,
    validate_environment,
)
from mato_loop_backend.integrations.mcp.stdio_service import (
    McpStdioRequest,
    owned_agent,
    refresh_stdio,
    save_stdio,
)
from mato_loop_backend.integrations.mealie.service import MealieIntegrationService
from mato_loop_backend.integrations.opencode.adapter import (
    OpenCodeIntegrationService,
    OpenCodeSettingsError,
)
from mato_loop_backend.integrations.opencode.client import OpenCodeError
from mato_loop_backend.integrations.openscad.service import OpenScadIntegrationService
from mato_loop_backend.integrations.paperless.service import PaperlessIntegrationService
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
from mato_loop_backend.permissions.policy import PermissionDisplayItem, personal_settings_permission
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
from mato_loop_backend.tools.validation import argument_error

logger = logging.getLogger(__name__)

# Each type uses its existing product service. The map only describes model-writable fields.
_CONFIG_FIELDS = {
    "searxng": ("base_url",),
    "caldav": ("base_url", "username"),
    "imap_smtp": ("imap_host", "imap_port", "imap_security", "smtp_host", "smtp_port", "smtp_security", "username", "from_address"),
    "tbs-desk": ("base_url", "username"),
    "bookstack": ("base_url",),
    "mealie": ("base_url",),
    "paperless": ("base_url",),
    "arcane": ("base_url",),
    "sonobin": ("base_url",),
    "openscad": ("execution_agent_id", "binary_path"),
    "opencode": ("base_url", "username"),
    "browser": ("execution_agent_id",),
    "mcp_streamable_http": ("endpoint",),
    "mcp_stdio": tuple(McpStdioBinding.model_fields),
}
_SECRETS = {
    "caldav": ("password", "username"), "imap_smtp": ("password", "username"),
    "tbs-desk": ("password", "username"), "bookstack": ("token_id", "token_secret"),
    "mealie": ("token",), "arcane": ("api_key",),
    "paperless": ("token",),
    "sonobin": ("bearer_token",), "opencode": ("password", "username"),
    "mcp_streamable_http": ("bearer_token",),
}
_SERVICES = {
    "searxng": SearchIntegrationService, "caldav": CalDavIntegrationService,
    "imap_smtp": MailIntegrationService, "tbs-desk": TbsDeskIntegrationService,
    "bookstack": BookStackIntegrationService, "mealie": MealieIntegrationService,
    "paperless": PaperlessIntegrationService,
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
    executable: str | None = None
    working_directory: str | None = None
    args: list[str] | None = None
    environment: dict[str, str] | None = None
    idle_timeout_seconds: int | None = Field(default=None, strict=True)
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
        for key in sorted(self.model_fields_set):
            if getattr(self, key) is None:
                raise PydanticCustomError("tool_argument_invalid", "integration fields must not be null", {"field": key, "hint": "Omit the field instead of setting it to null."})
        for key in ("base_url", "endpoint"):
            url = getattr(self, key)
            if url is not None and (
                url.username is not None or url.password is not None
                or url.query is not None or url.fragment is not None
            ):
                raise PydanticCustomError("tool_argument_invalid", "invalid integration URL", {"field": key, "hint": "Use a URL without credentials, query, or fragment."})
        if self.name is not None and not self.name.strip():
            raise PydanticCustomError("tool_argument_invalid", "blank integration name", {"field": "name", "reason": "too_short", "hint": "Use a non-blank name."})
        if self.secret_refs and self.remove_credential:
            raise PydanticCustomError("tool_argument_invalid", "conflicting credential changes", {"field": "secret_refs", "hint": "Do not combine secret_refs with remove_credential."})
        for reference in self.secret_refs.values():
            if isinstance(reference, UUID):
                continue
            prefix, separator, raw_id = reference.partition(":")
            try:
                valid = separator and prefix in {"run-secret", "session-secret"} and str(UUID(raw_id)) == raw_id
            except ValueError:
                valid = False
            if not valid:
                raise PydanticCustomError("tool_argument_invalid", "invalid secret reference", {"field": "secret_refs", "hint": "Use a valueRef returned by secret_input or variable_read."})
        return self


def _payload(item: IntegrationInstance) -> dict[str, Any]:
    if item.adapter_type == "mcp_stdio":
        binding = McpStdioConfig.model_validate(item.config)
        return {
            "id": str(item.id), "adapterType": item.adapter_type, "name": item.name,
            "enabled": item.enabled, "executionAgentId": str(item.execution_agent_id),
            "config": binding.model_dump(include={"executable", "working_directory", "idle_timeout_seconds"}, by_alias=False),
            "hasCredential": item.credential_id is not None,
            "transport": "stdio", "toolCount": len(binding.tools),
        }
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
            if adapter_type == "mcp_stdio":  # Inherits the HTTP MCP gate.
                continue
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
        return ToolResult.error("tool.invalidArguments", field=f"changes.{sorted(set(changes) - {'default_resource_key', 'default_task_list_key'})[0]}", params={"reason": "extra_field"})
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


async def _stdio_target(context: ToolContext, request: McpStdioRequest):
    client = context.execution_client
    if client is None or request.execution_agent_id != client.agent_id:
        return ToolResult.error("executionAgent.unavailable")
    if not {"integration.mcp.discover", "integration.mcp.call", "cache.export"} <= client.operations:
        return ToolResult.error("executionAgent.operationUnavailable")
    try:
        agent = await owned_agent(context.db, context.session.user_id, client.agent_id)
    except ApiError as exc:
        return ToolResult.error(exc.code)
    from mato_loop_backend.sessions.service import execution_filesystem_policy

    if any(execution_filesystem_policy(agent, path) == "deny" for path in (request.executable, request.working_directory)):
        return ToolResult.error("file.accessDenied")
    try:
        executable = await client.request("filesystem.stat", {"path": request.executable}, WorkspaceStatResult)
    except ExecutionClientError as exc:
        return ToolResult.error(exc.code)
    if executable.path != request.executable or executable.kind != "file":
        return ToolResult.error("file.symlinkForbidden" if executable.kind == "symlink" else "file.notAFile")
    return agent.display_name


def _stdio_confirmation(user_id, request, computer_name, secret_names):
    return replace(
        personal_settings_permission(user_id, "integrations", "create"),
        display_items=(
            PermissionDisplayItem("Computer", computer_name),
            PermissionDisplayItem("Verbindung", request.name),
            PermissionDisplayItem("Programm", request.executable, "code"),
            PermissionDisplayItem("Argumente", json.dumps(request.args, ensure_ascii=False), "code"),
            PermissionDisplayItem("Arbeitsverzeichnis", request.working_directory, "code"),
            PermissionDisplayItem("Umgebung", json.dumps(request.environment, ensure_ascii=False), "code"),
            *((PermissionDisplayItem("Geschützte Variablen", tuple(sorted(secret_names)), "list"),) if secret_names else ()),
            PermissionDisplayItem("Ausführung", "Aktiviert: Das Programm wird zur Tool-Erkennung mit den Betriebssystemrechten des Agents gestartet."
                                  if request.enabled else "Deaktiviert: Das Programm wird nicht gestartet."),
            PermissionDisplayItem("Leerlauf-Timeout", f"{request.idle_timeout_seconds} Sekunden"),
        ),
        available_decisions=("allow_once", "deny"),
        approval_details={"settingsTargetHash": hashlib.sha256(json.dumps(
            request.model_dump(mode="json"), sort_keys=True,
        ).encode()).hexdigest()},
    )


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
        return ToolResult.error("tool.invalidArguments", field="adapter_type", params={"reason": "invalid_choice", "hint": "Use a supported adapter type; an existing integration's adapter type cannot be changed."})
    if adapter_type == "mcp_stdio" and (action != "create" or arguments.integration_id is not None):
        return ToolResult.error("tool.invalidArguments", field="action", params={
            "hint": "Create local MCP connections here. To change or remove one, use Settings > MCP servers.",
        })
    if action != "delete" and global_settings.availability_for(adapter_type) != IntegrationAdapterAvailability.FULL:
        return ToolResult.error("integration.personalAdapterDisabled")
    changes = None
    stdio = None
    if action != "delete":
        try:
            changes = IntegrationChanges.model_validate(arguments.changes)
        except ValidationError as exc:
            return argument_error(exc, arguments.changes, prefix="changes")
        allowed = {"name", "enabled", "secret_refs", *_CONFIG_FIELDS[adapter_type]}
        if adapter_type in {"mcp_streamable_http", "opencode"}:
            allowed.add("remove_credential")
        if changes.model_fields_set - allowed:
            return ToolResult.error("tool.invalidArguments", field=f"changes.{sorted(changes.model_fields_set - allowed)[0]}", params={"reason": "extra_field"})
        if adapter_type != "mcp_stdio" and set(changes.secret_refs) - set(_SECRETS.get(adapter_type, ())):
            return ToolResult.error("tool.invalidArguments", field="changes.secret_refs", params={"reason": "invalid_choice", "hint": "Use only credential fields supported by the selected adapter."})
        if changes.remove_credential and adapter_type not in {"mcp_streamable_http", "opencode"}:
            return ToolResult.error("tool.invalidArguments", field="changes.remove_credential", params={"reason": "invalid_choice"})
        if adapter_type == "bookstack" and len(changes.secret_refs) == 1:
            return ToolResult.error("tool.invalidArguments", field="changes.secret_refs", params={"reason": "missing", "hint": "Provide both token_id and token_secret references."})
        if action == "create" and adapter_type != "mcp_stdio" and (
            changes.name is None or any(
                getattr(changes, field) is None and field not in changes.secret_refs
                for field in _CONFIG_FIELDS[adapter_type]
            )
            or any(field not in changes.secret_refs for field in _SECRETS.get(adapter_type, ())
                   if field != "username" and adapter_type not in {"opencode", "mcp_streamable_http"})
            or changes.remove_credential
        ):
            return ToolResult.error("tool.invalidArguments", field="changes", params={"reason": "missing", "hint": "Creating an integration requires a name, all adapter configuration fields and required secret_refs; remove_credential must be false."})
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
            except ValidationError as exc:
                if adapter_type == "sonobin":
                    return ToolResult.error("tool.invalidArguments", field="changes.base_url", params={
                        "reason": "invalid_format", "hint": "Use an HTTPS base URL without credentials, query, or fragment.",
                    })
                return argument_error(exc, {"endpoint": config.get("endpoint")}, prefix="changes")
            except (ValueError, KeyError):
                return ToolResult.error("tool.invalidArguments", field="changes.base_url" if adapter_type == "sonobin" else "changes.endpoint", params={"reason": "invalid_format"})
        elif adapter_type == "mcp_stdio":
            if context.execution_client is None:
                return ToolResult.error("executionAgent.unavailable")
            data = {**config, "transport": "stdio", "execution_agent_id": context.execution_client.agent_id,
                    "name": changes.name, "enabled": changes.enabled if changes.enabled is not None else True}
            try:
                stdio = McpStdioRequest.model_validate(data)
            except ValidationError as exc:
                return argument_error(exc, data, prefix="changes")
            try:
                validate_environment(dict.fromkeys(changes.secret_refs, ""))
            except ValueError:
                return ToolResult.error("tool.invalidArguments", field="changes.secret_refs")
            if stdio.environment.keys() & changes.secret_refs.keys():
                return ToolResult.error("mcp.configurationInvalid")
            computer_name = await _stdio_target(context, stdio)
            if isinstance(computer_name, ToolResult):
                return computer_name
        elif adapter_type not in {"opencode", "browser"}:
            adapter = next((item for item in (integration_adapter(kind, adapter_type) for kind in (
                "search", "calendar", "mail", "support", "knowledge", "recipes", "documents", "infrastructure", "openscad"
            )) if item is not None), None)
            if adapter is None:
                return ToolResult.error("tool.unavailable")
            if adapter_type == "openscad":
                config = {"binary_path": config["binary_path"]}
            if "username" not in changes.secret_refs:
                try:
                    adapter.config_model.model_validate(config)
                except ValidationError as exc:
                    return argument_error(exc, config, prefix="changes")
                except KeyError:
                    return ToolResult.error("tool.invalidArguments", field="changes", params={"reason": "missing"})
    approval_kwargs = {}
    if stdio is not None:
        approval_kwargs["confirmation"] = _stdio_confirmation(
            context.session.user_id, stdio, computer_name, changes.secret_refs,
        )
    denied = await authorize(
        context, action, "integrations", integration_id=arguments.integration_id,
        fields=tuple(sorted((arguments.changes or {}).keys())), changes=arguments.changes,
        **approval_kwargs,
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
        return ToolResult.error("tool.invalidArguments", field="changes.secret_refs", params={"reason": "invalid_value", "hint": "Request new credentials matching the adapter's length and character requirements."})
    if stdio is not None:
        return await _create_personal_stdio(context, stdio, credential_values)
    if "username" in credential_values:
        if adapter_type == "opencode" and (
            len(credential_values["username"]) > 255
            or ":" in credential_values["username"]
            or any(character in "\r\n" for character in credential_values["username"])
        ):
            return ToolResult.error("tool.invalidArguments", field="changes.secret_refs.username", params={"reason": "invalid_value", "hint": "Use a username of at most 255 characters without colons or line breaks."})
        config["username"] = credential_values["username"]
        if adapter_type in {"caldav", "imap_smtp", "tbs-desk"}:
            adapter = next((item for item in (integration_adapter(kind, adapter_type) for kind in (
                "calendar", "mail", "support"
            )) if item is not None), None)
            try:
                adapter.config_model.model_validate(config)
            except ValidationError as exc:
                return argument_error(exc, config, prefix="changes")
            except AttributeError:
                return ToolResult.error("tool.unavailable")
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
        elif adapter_type == "paperless":
            service, kwargs = PaperlessIntegrationService, {**base, "base_url": config.get("base_url"), "token": credential_values.get("token"), "owner_id": owner_id}
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


async def _create_personal_stdio(context, request, credentials):
    owner_id = context.session.user_id
    target = await _stdio_target(context, request)
    if isinstance(target, ToolResult):
        return target
    try:
        request = McpStdioRequest.model_validate(dict(
            request.model_dump(by_alias=False), environment_secrets=credentials,
        ))
    except ValidationError:
        return ToolResult.error("mcp.configurationInvalid")
    try:
        async with AsyncSessionFactory() as service_db:
            saved = await save_stdio(service_db, request, owner_id=owner_id, user_id=owner_id)
            result = {**_payload(saved), "saved": True, "effectiveFrom": "next_run"}
            if not request.enabled:
                return ToolResult.json(result)
            try:
                refreshed = await refresh_stdio(service_db, saved, owner_id)
            except (ApiError, IntegrationSettingsError, McpClientError, ExecutionClientError) as exc:
                error = ToolResult.error(exc.code)
                error.content = json.dumps({**result, **json.loads(error.content), "toolCatalogRefreshed": False})
                return error
            return ToolResult.json({**result, **_payload(refreshed), "toolCatalogRefreshed": True})
    except (ApiError, IntegrationSettingsError, McpClientError, ExecutionClientError) as exc:
        return ToolResult.error(exc.code)
