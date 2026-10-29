from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)

import mato_loop_backend.permissions.resolution as permissions_resolution
import mato_loop_backend.tools.access as tools_access
from mato_loop_backend.permissions.policy import (
    personal_settings_permission,
    personal_variables_permission,
)
from mato_loop_backend.personal_variables import (
    PERSONAL_VARIABLE_DESCRIPTION_MAX_LENGTH,
    PERSONAL_VARIABLE_NAME_PATTERN,
    PersonalVariableError,
    create_personal_variable,
    get_personal_variable,
    list_personal_variables,
    personal_variable_payload,
    update_personal_variable,
)
from mato_loop_backend.settings import (
    AgentProfileUnavailable,
    IntegrationAdapterAvailability,
    MemorySettings,
    SettingsStore,
    ToolExecutionPolicy,
    UserAgentIdentitySettings,
    UserProfileSettings,
    load_agent_profile,
    load_effective_agent_identity,
)
from mato_loop_backend.tools.actions import _PERSONAL_SETTINGS_READ, _PERSONAL_SETTINGS_WRITE
from mato_loop_backend.tools.arguments import (
    PersonalSettingsArguments,
    UpdateAgentIdentityArguments,
    UpdateUserProfileArguments,
)
from mato_loop_backend.tools.context import ToolContext
from mato_loop_backend.tools.contracts import ToolDefinition, ToolResult
from mato_loop_backend.tools.identity import _update_agent_identity, _update_user_profile
from mato_loop_backend.tools.personal_integrations import (
    _CONFIG_FIELDS,
    _SECRETS,
    manage_integrations,
)
from mato_loop_backend.tools.secrets import resolve_secret_refs
from mato_loop_backend.user_profile import user_profile_runtime_payload

# ponytail: expose only scalar, non-policy settings; add fields when their owner checks are defined.
_SCALAR_SETTINGS = {
    "memory": (
        MemorySettings,
        ("enabled", "automatic_dreaming_enabled", "dreaming_instructions",
         "automatic_memory_snippet_limit", "automatic_session_snippet_limit",
         "recent_session_snippet_limit", "automatic_context_max_tokens"),
    ),
}
_PROFILE_FIELDS = frozenset(UserProfileSettings.model_fields)


class VariableChanges(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str | None = Field(default=None, min_length=1, max_length=120, pattern=PERSONAL_VARIABLE_NAME_PATTERN.pattern)
    secret: bool | None = None
    description: str | None = Field(default=None, max_length=PERSONAL_VARIABLE_DESCRIPTION_MAX_LENGTH)
    value_ref: UUID | str | None = None

    @field_validator("value_ref", mode="before")
    @classmethod
    def normalize_reference(cls, value: Any) -> Any:
        if isinstance(value, str):
            try:
                return UUID(value)
            except ValueError:
                prefix, separator, raw_id = value.partition(":")
                try:
                    if separator and prefix in {"run-secret", "session-secret"} and str(UUID(raw_id)) == raw_id:
                        return value
                except ValueError:
                    pass
                raise ValueError("invalid secret reference") from None
        return value


def _policy(context: ToolContext, capability: str) -> ToolExecutionPolicy:
    default = (
        _PERSONAL_SETTINGS_READ if capability == "settings.personal.read"
        else _PERSONAL_SETTINGS_WRITE
    ).default_policy(context.tool_settings)
    return permissions_resolution.tool_execution_policy(context.tool_settings, capability, default)


async def _authorize(
    context: ToolContext, action: str, area: str, *,
    fields: tuple[str, ...] = (), integration_id: UUID | None = None,
    changes: dict[str, Any] | None = None,
) -> ToolResult | None:
    capability = "settings.personal.read" if action == "get" else "settings.personal.update"
    context.tool_settings = await tools_access.profile_tool_settings(context)
    policy = _policy(context, capability)
    if policy == ToolExecutionPolicy.DISABLED or not context.tool_settings.tool_availability.get("customize_matoloop", True):
        return ToolResult.error("tool.unavailable")
    approved = False
    if policy == ToolExecutionPolicy.ASK:
        approved = await tools_access.allow(
            context, personal_settings_permission(
                context.session.user_id, area, action,
                fields=fields, integration_id=integration_id, changes=changes,
            ),
        )
        if not approved:
            return ToolResult.error("permission.denied")
    context.tool_settings = await tools_access.profile_tool_settings(context)
    policy = _policy(context, capability)
    if (policy == ToolExecutionPolicy.DISABLED
        or not context.tool_settings.tool_availability.get("customize_matoloop", True)
        or (policy == ToolExecutionPolicy.ASK and not approved)):
        return ToolResult.error("tool.unavailable")
    return None


async def _personal_settings(
    arguments: PersonalSettingsArguments, context: ToolContext
) -> ToolResult:
    area, action = arguments.area, arguments.action
    if action == "list":
        context.tool_settings = await tools_access.profile_tool_settings(context)
        if not context.tool_settings.tool_availability.get("customize_matoloop", True) or all(
            _policy(context, capability) == ToolExecutionPolicy.DISABLED
            for capability in ("settings.personal.read", "settings.personal.update")
        ):
            return ToolResult.error("tool.unavailable")
        from mato_loop_backend.integrations.registry import integration_adapters
        from mato_loop_backend.settings import IntegrationSettings

        instance = await SettingsStore.load(context.db, IntegrationSettings)
        from mato_loop_backend.settings.effective import personal_settings_allowed

        admin = await personal_settings_allowed(context.db, context.session.user_id, False)
        adapters = {
            adapter.adapter_type: {
                "fields": ["name", "enabled", *_CONFIG_FIELDS[adapter.adapter_type]],
                "credentialFields": list(_SECRETS.get(adapter.adapter_type, ())),
            }
            for adapter in integration_adapters()
            if adapter.adapter_type in _CONFIG_FIELDS
            and (instance.availability_for(adapter.adapter_type) == IntegrationAdapterAvailability.FULL or admin and instance.availability_for(adapter.adapter_type) == IntegrationAdapterAvailability.INSTANCE_ONLY)
        }
        if instance.availability_for("mcp_streamable_http") == IntegrationAdapterAvailability.FULL or admin and instance.availability_for("mcp_streamable_http") == IntegrationAdapterAvailability.INSTANCE_ONLY:
            adapters["mcp_streamable_http"] = {
                "fields": ["name", "enabled", "endpoint"],
                "credentialFields": ["bearer_token"],
                "authenticationModes": ["none", "bearer", "oauth"],
                "oauthSetup": {
                    "settingsPath": "/settings/mcp",
                    "instructions": "MatoLoop supports OAuth. Start the browser login in Settings > MCP servers. This tool configures static Bearer credentials; it cannot complete interactive OAuth setup.",
                },
            }
        areas = {
            **{key: list(fields) for key, (_type, fields) in _SCALAR_SETTINGS.items()},
            "user_profile": sorted(_PROFILE_FIELDS),
            "agent_identity": ["display_name", "instructions", "set_as_default"],
            "variables": ["name", "secret", "description", "value_ref"],
            "integrations": adapters,
            "integrationOptions": ["caldav_defaults"],
        }
        result = {"areas": {area: areas[area]} if area else areas}
        if area in {None, "agent_identity"}:
            result["agentIdentityUsage"] = (
                "Use action=update. changes.instructions edits the active persona for all agents using it, preserving assignments. "
                "Copy user-supplied instructions exactly. changes.set_as_default=true makes it your personal default; "
                "false clears the personal default; omit to keep it unchanged. "
                "Editing or selecting a global persona as the default creates a personal copy. "
                "The copy is assigned only to the current agent unless setting it as the default. "
                "Existing personal profile assignments take precedence over the default."
            )
        return ToolResult.json(result)

    assert area is not None
    if area == "integrations":
        return await manage_integrations(arguments, context, _authorize)
    if area == "variables":
        if action == "get":
            denied = await _authorize(context, action, area)
            if denied:
                return denied
            if not await tools_access.allow(
                context, personal_variables_permission(context.session.user_id)
            ):
                return ToolResult.error("permission.denied")
            context.tool_settings = await tools_access.profile_tool_settings(context)
            if permissions_resolution.tool_execution_policy(
                context.tool_settings, "secrets.access", ToolExecutionPolicy.ASK
            ) == ToolExecutionPolicy.DISABLED:
                return ToolResult.error("tool.unavailable")
            variables = await list_personal_variables(context.db, context.session.user_id)
            if arguments.variable_id is not None and not any(
                item.id == arguments.variable_id for item in variables
            ):
                return ToolResult.error("personalVariable.notFound")
            return ToolResult.json({"variables": [
                {**personal_variable_payload(item), "valueRef": str(item.id)}
                for item in variables
                if arguments.variable_id is None or item.id == arguments.variable_id
            ]})
        current = (
            await get_personal_variable(context.db, context.session.user_id, arguments.variable_id)
            if arguments.variable_id is not None else None
        )
        if action in {"update", "delete"} and current is None:
            return ToolResult.error("personalVariable.notFound")
        if action != "delete":
            try:
                changes = VariableChanges.model_validate(arguments.changes)
            except ValidationError:
                return ToolResult.error("tool.invalidArguments")
            if action == "create" and (changes.name is None or changes.value_ref is None):
                return ToolResult.error("tool.invalidArguments")
        denied = await _authorize(
            context, action, area, fields=tuple(sorted((arguments.changes or {}).keys())),
            changes=arguments.changes,
        )
        if denied:
            return denied
        context.tool_settings = await tools_access.profile_tool_settings(context)
        write_policy = permissions_resolution.tool_execution_policy(
            context.tool_settings, "secrets.write", ToolExecutionPolicy.ASK
        )
        if write_policy == ToolExecutionPolicy.DISABLED:
            return ToolResult.error("tool.unavailable")
        if write_policy == ToolExecutionPolicy.ASK and not await tools_access.allow(
            context, personal_variables_permission(context.session.user_id, write=True,
                                                   names=(changes.name or current.label,) if action != "delete" else (current.label,)),
        ):
            return ToolResult.error("permission.denied")
        context.tool_settings = await tools_access.profile_tool_settings(context)
        if permissions_resolution.tool_execution_policy(
            context.tool_settings, "secrets.write", ToolExecutionPolicy.ASK
        ) == ToolExecutionPolicy.DISABLED:
            return ToolResult.error("tool.unavailable")
        if action == "delete":
            assert current is not None
            current = await get_personal_variable(
                context.db, context.session.user_id, current.id, for_update=True
            )
            if current is None:
                return ToolResult.error("personalVariable.notFound")
            await context.db.delete(current)
            await context.db.commit()
            return ToolResult.json({"deleted": True})
        assert changes is not None
        values = await resolve_secret_refs(
            context, {"value": changes.value_ref}
        ) if changes.value_ref is not None else {}
        if isinstance(values, ToolResult):
            return values
        try:
            if action == "create":
                variable = await create_personal_variable(
                    context.db, context.session.user_id, name=changes.name,
                    value=values["value"], secret=changes.secret if changes.secret is not None else True,
                    description=changes.description,
                )
            else:
                assert current is not None
                variable = await update_personal_variable(
                    context.db, context.session.user_id, arguments.variable_id,
                    name=changes.name or current.label, value=values.get("value"),
                    secret=changes.secret if changes.secret is not None else current.metadata_json.get("secret") is True,
                    description=changes.description if "description" in changes.model_fields_set else current.metadata_json.get("description"),
                )
        except PersonalVariableError as exc:
            return ToolResult.error(exc.code, field=exc.field)
        await context.db.commit()
        return ToolResult.json({"id": str(variable.id), "name": variable.label, "saved": True})

    if area in {"agent_identity", "user_profile"} and action == "update":
        context.tool_settings = await tools_access.profile_tool_settings(context)
        legacy_name = "update_agent_identity" if area == "agent_identity" else "update_user_profile"
        if (_policy(context, "settings.personal.update") == ToolExecutionPolicy.DISABLED
            or not context.tool_settings.tool_availability.get("customize_matoloop", True)
            or not context.tool_settings.tool_availability.get(legacy_name, True)):
            return ToolResult.error("tool.unavailable")
        denied = await _authorize(
            context, action, area,
            fields=tuple(sorted(arguments.changes)), changes=arguments.changes,
        )
        if denied:
            return denied
    if area == "agent_identity":
        if action == "get":
            denied = await _authorize(context, action, area)
            if denied:
                return denied
            try:
                profile = await load_agent_profile(
                    context.db, context.session.user_id,
                    context.agent_profile_id or context.session.agent_profile_id,
                )
            except AgentProfileUnavailable:
                return ToolResult.error("tool.unavailable")
            identity = await load_effective_agent_identity(
                context.db, context.session.user_id, profile,
            )
            personal = await SettingsStore.load(context.db, UserAgentIdentitySettings, user_id=context.session.user_id)
            return ToolResult.json({
                "displayName": identity.display_name,
                "personaName": identity.persona.name,
                "personaEditable": identity.persona_editable,
                "personalDisplayNameEnabled": identity.personal_display_name_enabled,
                "personalDefaultPersonaId": str(personal.default_persona_id) if personal.default_persona_id else None,
            })
        try:
            changes = UpdateAgentIdentityArguments.model_validate(arguments.changes)
        except ValidationError:
            return ToolResult.error("tool.invalidArguments")
        result = await _update_agent_identity(changes, context)
        if not result.is_error:
            await context.db.commit()
        return result

    if area == "user_profile":
        if action == "get":
            denied = await _authorize(context, action, area)
            if denied:
                return denied
            profile = await SettingsStore.load(context.db, UserProfileSettings, user_id=context.session.user_id)
            return ToolResult.json(user_profile_runtime_payload(profile, context.model_trust_level))
        try:
            changes = UpdateUserProfileArguments.model_validate(
                {**(arguments.changes or {}), "model_scope": arguments.model_scope}
            )
        except ValidationError:
            return ToolResult.error("tool.invalidArguments")
        result = await _update_user_profile(changes, context)
        if not result.is_error:
            await context.db.commit()
        return result

    if area in _SCALAR_SETTINGS:
        settings_type, fields = _SCALAR_SETTINGS[area]
        if action == "get":
            denied = await _authorize(context, action, area)
            if denied:
                return denied
            settings = await SettingsStore.load(context.db, settings_type, user_id=context.session.user_id)
            return ToolResult.json(settings.model_dump(mode="json", include=set(fields)))
        changes = arguments.changes or {}
        if set(changes) - set(fields) or any(value is None for value in changes.values()):
            return ToolResult.error("tool.invalidArguments")
        if any(
            type(value) is not settings_type.model_fields[field].annotation
            for field, value in changes.items()
        ):
            return ToolResult.error("tool.invalidArguments")
        denied = await _authorize(
            context, action, area, fields=tuple(sorted(changes)), changes=changes,
        )
        if denied:
            return denied
        current = await SettingsStore.load(
            context.db, settings_type, user_id=context.session.user_id, for_update=True
        )
        try:
            updated = settings_type.model_validate({**current.model_dump(), **changes})
        except ValidationError:
            return ToolResult.error("tool.invalidArguments")
        await SettingsStore.save(context.db, updated, user_id=context.session.user_id)
        await context.db.commit()
        return ToolResult.json({"updatedFields": sorted(changes), "effectiveFrom": "next_run"})

CUSTOMIZE_MATOLOOP = ToolDefinition(
    "customize_matoloop",
    "List or manage personal MatoLoop settings, variables, and integration connections. "
    "Use only for an explicit latest-message request. For credentials, call secret_input or "
    "variable_read and pass only opaque references in changes.secret_refs or changes.value_ref. "
    "For integrations use area=integrations, adapter_type from list, and changes with name, "
    "listed fields, and secret_refs keyed by listed credentialFields. "
    "Never put secret values in changes. "
    "Use action=list to discover supported areas and fields; area is an optional filter. "
    "Use action=get to read saved values. SonoBin takes base_url and builds its MCP endpoint automatically. "
    "Changes apply from the next run. "
    "MCP supports no authentication, Bearer tokens, and OAuth. Interactive OAuth setup requires "
    "Settings > MCP servers.",
    PersonalSettingsArguments,
    _personal_settings,
    group="agent", label="MatoLoop persönlich anpassen", icon="tune",
    actions=(_PERSONAL_SETTINGS_READ, _PERSONAL_SETTINGS_WRITE),
    alternative_action_groups=(frozenset({"settings.personal.read", "settings.personal.update"}),),
    releases_private_data=True,
)
