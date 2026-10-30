from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend.models import ExecutionAgent, UserAccount
from mato_loop_backend.settings.models import (
    GENERAL_PROFILE_ID,
    AgentIdentitySettings,
    AgentProfile,
    AgentProfilePermissions,
    AgentProfilesSettings,
    AiProviderSettings,
    AiSettings,
    EffectiveAgentIdentity,
    GeneralSettings,
    InstanceSkillsSettings,
    IntegrationAdapterAvailability,
    IntegrationInstance,
    IntegrationSettings,
    ModelDefaults,
    ModelDefaultsSettings,
    PersonaProfile,
    ToolSettings,
    TrustLevel,
    UserAgentIdentitySettings,
    UserAgentProfilesSettings,
    UserAiSettings,
    UserIntegrationSettings,
    UserModelDefaultsSettings,
    UserToolSettings,
    resolve_agent_profile,
)
from mato_loop_backend.settings.session_profiles import ChatModelOverride, SessionProfileSnapshot
from mato_loop_backend.settings.store import SettingsStore

if TYPE_CHECKING:
    from mato_loop_backend.models import ChatSession

def apply_session_ai(settings: AiSettings, session: ChatSession, profile_id: UUID | None = None) -> AiSettings:
    from mato_loop_backend.providers.reasoning import reasoning_profile
    from mato_loop_backend.providers.settings_service import (
        available_model,
        model_reference_available,
    )

    effective = settings.model_copy(deep=True)
    configuration = SessionProfileSnapshot.model_validate(session.session_profile or {})
    agent_id = profile_id or session.agent_profile_id
    override = configuration.agent_models.get(agent_id, ChatModelOverride())
    profile_override = settings.profile_models.get(agent_id, ChatModelOverride())
    candidates = (override.model_id, session.model_config_id, profile_override.model_id, settings.default_model_id)
    effective.default_model_id = next((
        model_id for model_id in candidates
        if model_id is not None and model_reference_available(settings, "default_model_id", model_id)
    ), None)
    effective.reasoning_effort = override.reasoning_effort or session.reasoning_effort or profile_override.reasoning_effort or effective.reasoning_effort
    configured_model_id = next((model_id for model_id in candidates if model_id is not None), None)
    inherited_reasoning = override.reasoning_effort is None and session.reasoning_effort is None
    if effective.reasoning_effort is not None and (inherited_reasoning or effective.default_model_id != configured_model_id):
        model = available_model(settings, effective.default_model_id)
        provider = next((item for item in settings.providers if model and item.id == model.provider_id), None)
        profile = reasoning_profile(model.model_id, model.discovery_metadata) if model and provider and (
            provider.adapter_type in {"openai-codex", "lemonade"}
            or provider.config.get("api_type", "chat_completions") == "chat_completions"
        ) else None
        if profile is None or effective.reasoning_effort not in profile.options:
            effective.reasoning_effort = None
    return effective


def session_agent_allowed(session: ChatSession, profile_id: UUID) -> bool:
    configuration = SessionProfileSnapshot.model_validate(session.session_profile or {})
    return configuration.agent_ids is None or profile_id in configuration.agent_ids

_INTEGRATION_TOOL_FIELDS = (
    "resource_labels",
    "default_calendar",
    "default_task_list",
)


async def personal_settings_allowed(db: AsyncSession, user_id: UUID, enabled: bool) -> bool:
    if enabled:
        return True
    user = await db.get(UserAccount, user_id, populate_existing=True)
    return user is not None and getattr(user, "is_admin", False) is True


async def default_agent_profile_id(db: AsyncSession) -> UUID:
    settings = await SettingsStore.load_optional(db, AgentProfilesSettings) or AgentProfilesSettings()
    return settings.default_profile_id


async def personal_skills_allowed(db: AsyncSession, user_id: UUID, *, for_update: bool = False) -> bool:
    settings = await SettingsStore.load_optional(db, InstanceSkillsSettings, for_update=for_update)
    return await personal_settings_allowed(db, user_id, settings.personal_skills_enabled if settings is not None else True)


def resolve_agent_persona(
    instance: AgentIdentitySettings,
    personal: UserAgentIdentitySettings | None,
    agent_profile: AgentProfile | None,
) -> PersonaProfile:
    catalog = {persona.id: persona for persona in instance.personas}
    selected = instance.default_persona_id
    if agent_profile is not None:
        if agent_profile.owner_id is None:
            selected = instance.profile_personas.get(agent_profile.id, selected)
    if instance.personal_personas_enabled and personal is not None:
        own = {persona.id: persona for persona in personal.personas}
        available = {**catalog, **own}
        if agent_profile is not None:
            personal_id = personal.profile_personas.get(agent_profile.id)
            if personal_id in available:
                return available[personal_id].model_copy(deep=True)
        if personal.default_persona_id in own:
            return own[personal.default_persona_id].model_copy(deep=True)
    return catalog.get(selected, catalog[instance.default_persona_id]).model_copy(deep=True)


async def load_effective_agent_identity(
    db: AsyncSession,
    user_id: UUID,
    agent_profile: AgentProfile | None = None,
    *,
    for_update: bool = False,
) -> EffectiveAgentIdentity:
    instance = await SettingsStore.load(
        db, AgentIdentitySettings, for_update=for_update
    )
    instance = instance.model_copy(deep=True)
    instance.personal_personas_enabled = await personal_settings_allowed(db, user_id, instance.personal_personas_enabled)
    personal = await SettingsStore.load_optional(
        db, UserAgentIdentitySettings, user_id=user_id, for_update=for_update
    )
    display_name = instance.display_name
    if (
        instance.personal_personas_enabled
        and personal is not None
        and personal.display_name is not None
    ):
        display_name = personal.display_name

    return EffectiveAgentIdentity(
        display_name=display_name,
        persona=resolve_agent_persona(instance, personal, agent_profile),
        personal_display_name_enabled=instance.personal_personas_enabled,
        persona_editable=instance.personal_personas_enabled and agent_profile is not None,
    )


async def load_effective_ai(db: AsyncSession, user_id: UUID, *, include_personal: bool = True) -> AiSettings:
    from mato_loop_backend.providers.settings_service import (
        first_available_agent_model,
        model_reference_available,
    )

    inventory = await SettingsStore.load(db, AiProviderSettings)
    defaults = await SettingsStore.load_optional(db, ModelDefaultsSettings) or ModelDefaultsSettings()
    values = defaults.model_dump(include=set(ModelDefaults.model_fields), exclude_none=True)
    personal_defaults = None
    if include_personal and await personal_settings_allowed(db, user_id, defaults.personal_settings_enabled):
        personal_defaults = await SettingsStore.load_optional(db, UserModelDefaultsSettings, user_id=user_id) or UserModelDefaultsSettings()
        values.update(personal_defaults.model_dump(exclude={"profile_models"}, exclude_none=True))
    instance = AiSettings.model_validate({**inventory.model_dump(include=set(AiProviderSettings.model_fields)), **values})
    personal = await SettingsStore.load_optional(db, UserAiSettings, user_id=user_id) if include_personal else None
    for provider in instance.providers:
        provider.owner_id = None
    if personal is not None and await personal_settings_allowed(db, user_id, instance.personal_providers_enabled):
        personal = personal.model_copy(deep=True)
        personal_provider_ids = {provider.id for provider in personal.providers}
        for provider in personal.providers:
            provider.owner_id = user_id
        instance.providers.extend(personal.providers)
        instance.models.extend(
            model for model in personal.models if model.provider_id in personal_provider_ids
        )

    if personal_defaults is not None:
        for profile_id, choice in personal_defaults.profile_models.items():
            fields = choice.model_dump(exclude_none=True)
            if choice.model_id is not None and not model_reference_available(instance, "default_model_id", choice.model_id):
                fields.pop("model_id")
            inherited = instance.profile_models.get(profile_id, ChatModelOverride())
            instance.profile_models[profile_id] = inherited.model_copy(update=fields)

    if instance.default_model_id is None:
        instance.default_model_id = first_available_agent_model(instance)

    general = await SettingsStore.load_optional(db, GeneralSettings) or GeneralSettings()
    if not general.model_trust_enabled:
        for provider in instance.providers:
            provider.trust_level = TrustLevel.PRIVATE
        for model in instance.models:
            model.trust_level = TrustLevel.PRIVATE

    return instance


async def load_effective_integrations(
    db: AsyncSession, user_id: UUID
) -> IntegrationSettings:
    from mato_loop_backend.integrations.registry import integration_adapter

    instance = (await SettingsStore.load(db, IntegrationSettings)).model_copy(deep=True)
    for item in instance.instances:
        item.owner_id = None
    instance.instances = [
        item
        for item in instance.instances
        if instance.availability_for(item.adapter_type)
        != IntegrationAdapterAvailability.DISABLED
        and not (
            (adapter := integration_adapter(item.integration_type, item.adapter_type))
            and adapter.personal_only
        )
    ]

    personal = await SettingsStore.load_optional(
        db, UserIntegrationSettings, user_id=user_id
    )
    if personal is None:
        personal_instances = []
    else:
        personal_instances = personal.model_copy(deep=True).instances
    admin_personal = await personal_settings_allowed(db, user_id, False) if any(
        instance.availability_for(item.adapter_type) == IntegrationAdapterAvailability.INSTANCE_ONLY
        for item in personal_instances
    ) else False
    for item in personal_instances:
        adapter = integration_adapter(item.integration_type, item.adapter_type)
        if adapter is not None and adapter.instance_only:
            continue
        if (
            instance.availability_for(item.adapter_type)
            != IntegrationAdapterAvailability.FULL
            and not (admin_personal and instance.availability_for(item.adapter_type) == IntegrationAdapterAvailability.INSTANCE_ONLY)
        ):
            continue
        item.owner_id = user_id
        instance.instances.append(item)

    agent_ids = {
        item.execution_agent_id
        for item in instance.instances
        if item.execution_agent_id is not None
    }
    if agent_ids:
        owned_agent_ids = set(
            (
                await db.scalars(
                    select(ExecutionAgent.id).where(
                        ExecutionAgent.id.in_(agent_ids),
                        ExecutionAgent.user_id == user_id,
                        ExecutionAgent.revoked_at.is_(None),
                    )
                )
            ).all()
        )
        instance.instances = [
            item
            for item in instance.instances
            if item.execution_agent_id is None
            or item.execution_agent_id in owned_agent_ids
        ]
    return instance


def integration_tool_loading(instance: IntegrationInstance, profile: AgentProfile | None) -> str:
    if profile is not None:
        for profile_id in (profile.id, profile.base_profile_id):
            override = instance.profile_permissions.get(profile_id)
            if override is not None and override.tool_loading is not None:
                return override.tool_loading
    mode = instance.permissions.tool_loading or (profile.integration_tool_loading if profile else "on_demand")
    if mode == "native":
        from mato_loop_backend.integrations.registry import integration_adapter

        adapter = integration_adapter(instance.integration_type, instance.adapter_type)
        return "all" if adapter is not None and adapter.integration_type != "mcp" else "on_demand"
    return mode


def integration_profile_available(instance: IntegrationInstance, profile: AgentProfile) -> bool:
    override = instance.profile_permissions.get(profile.id)
    if override is not None and override.available is not None:
        return override.available
    base = instance.profile_permissions.get(profile.base_profile_id)
    return base.available if base is not None and base.available is not None else (
        profile.integration_id == instance.id or profile.integrations_enabled_by_default
    )


def filter_agent_profile_integrations(
    settings: IntegrationSettings,
    profile: AgentProfile | None,
    *,
    allowed_ids: Collection[UUID | str] | None = None,
) -> IntegrationSettings:
    """Filter a runtime copy without changing integration enablement or policies."""
    effective = settings.model_copy(deep=True)
    ceiling = {str(value) for value in allowed_ids} if allowed_ids is not None else None
    effective.instances = [
        item for item in effective.instances
        if (ceiling is None or str(item.id) in ceiling)
        and (
            profile is None
            or integration_profile_available(item, profile)
        )
    ]
    selected_ids = {item.id for item in effective.instances}
    effective._agent_profile_excluded_ids |= frozenset(
        str(item.id) for item in settings.instances
        if item.id not in selected_ids
    )
    return effective


async def load_effective_tools(
    db: AsyncSession, user_id: UUID, trust_level: TrustLevel = TrustLevel.PUBLIC
) -> ToolSettings:
    from mato_loop_backend.tools.catalog import tool_registry

    instance = (await SettingsStore.load(db, ToolSettings)).model_copy(deep=True)
    if not await personal_skills_allowed(db, user_id):
        instance.tool_availability.update(dict.fromkeys(("save_skill", "edit_skill"), False))
    if not instance.tool_availability.get("browser", True):
        from mato_loop_backend.integrations.browser.adapter import BROWSER_TOOL_ACTIONS

        instance.tool_availability.update(dict.fromkeys(BROWSER_TOOL_ACTIONS, False))
    personal = await SettingsStore.load_optional(db, UserToolSettings, user_id=user_id)
    if personal is not None:
        shared_labels = instance.resource_labels
        for field in _INTEGRATION_TOOL_FIELDS:
            setattr(instance, field, getattr(personal, field))
        # Personal settings may contain old copies of shared integration labels.
        instance.resource_labels = personal.resource_labels | shared_labels
    for branch in (instance.private_permissions, instance.public_permissions):
        branch.policies = {key: value for key, value in branch.policies.items() if key in tool_registry.actions_by_capability}
        branch.integration_policies = {}
        branch.resource_policies = {}
        branch.resource_availability = {}
    return instance.select_trust(trust_level)


def apply_integration_permissions(tools: ToolSettings, integrations: IntegrationSettings, profile_id: UUID | None = None) -> None:
    for field in ("private_permissions", "public_permissions"):
        target = getattr(tools, field)
        target.integration_policies = {}
        target.resource_policies = {}
        target.resource_availability = {}
        for instance in integrations.instances:
            branch = getattr(instance.permissions, field)
            policies = dict(branch.policies)
            resources = {key: dict(value) for key, value in branch.resource_policies.items()}
            availability = dict(instance.permissions.resource_availability)
            override = instance.profile_permissions.get(profile_id)
            if override is not None:
                selected = getattr(override, field)
                policies.update(selected.policies)
                for key, value in selected.resource_policies.items():
                    resources.setdefault(key, {}).update(value)
                availability.update(override.resource_availability)
            target.integration_policies[str(instance.id)] = policies
            target.resource_policies[str(instance.id)] = resources
            target.resource_availability[str(instance.id)] = availability


def personal_integration_tool_settings(instance: ToolSettings) -> UserToolSettings:
    return UserToolSettings.model_validate(instance.model_dump(include=set(_INTEGRATION_TOOL_FIELDS)))


class AgentProfileUnavailable(Exception):
    pass


def shipped_profiles() -> list[AgentProfile]:
    from mato_loop_backend.settings.models import core_profiles

    return core_profiles()


@dataclass
class EffectiveAgentProfiles:
    personal_agents_enabled: bool
    profiles: list[AgentProfile]
    default_profile_id: UUID = GENERAL_PROFILE_ID


async def load_effective_agent_profiles(
    db: AsyncSession,
    user_id: UUID,
    *,
    for_update: bool = False,
    include_personal: bool = True,
    include_integrations: bool = True,
) -> EffectiveAgentProfiles:
    from mato_loop_backend.integrations.registry import effective_integration_contributions
    from mato_loop_backend.settings.models import apply_profile_overrides

    instance = await SettingsStore.load_optional(
        db, AgentProfilesSettings, for_update=for_update
    ) or AgentProfilesSettings()
    shipped = shipped_profiles()
    personal = (
        await SettingsStore.load_optional(db, UserAgentProfilesSettings, user_id=user_id, for_update=for_update)
        if include_personal and await personal_settings_allowed(db, user_id, instance.personal_agents_enabled) else None
    )
    shipped = [
        apply_profile_overrides(profile, instance.builtin_access_overrides[profile.id])
        if profile.id in instance.builtin_access_overrides else profile
        for profile in shipped
    ]
    bases = {profile.id: profile for profile in shipped}
    for profile in shipped:
        profile.enabled = instance.builtin_enabled.get(profile.id, True)
    effective = EffectiveAgentProfiles(
        personal_agents_enabled=instance.personal_agents_enabled,
        default_profile_id=instance.default_profile_id,
        profiles=[
            *shipped,
            *(
                resolve_agent_profile(profile, bases[profile.base_profile_id], None)
                for profile in instance.profiles
                if profile.base_profile_id in bases
            ),
        ],
    )
    if personal is not None:
        effective.profiles.extend(
            resolve_agent_profile(profile, bases[profile.base_profile_id], user_id)
            for profile in personal.profiles
            if profile.base_profile_id in bases
        )
    if include_integrations:
        integrations = await load_effective_integrations(db, user_id)
        for profile in effective_integration_contributions(integrations).agent_profiles:
            profile.owner_id = user_id
            effective.profiles.append(profile)
    return effective


async def load_agent_profile(
    db: AsyncSession,
    user_id: UUID,
    profile_id: UUID | None,
    *,
    for_update: bool = False,
) -> AgentProfile:
    settings = await load_effective_agent_profiles(
        db, user_id, for_update=for_update, include_integrations=False
    )
    profile_id = profile_id or settings.default_profile_id
    profile = next((item for item in settings.profiles if item.id == profile_id), None)
    if profile is None:
        settings = await load_effective_agent_profiles(db, user_id, for_update=for_update)
        profile = next((item for item in settings.profiles if item.id == profile_id), None)
    if profile is None:
        raise AgentProfileUnavailable
    return profile


async def load_agent_profile_tools(
    db: AsyncSession,
    user_id: UUID,
    profile_id: UUID | None,
    trust_level: TrustLevel,
) -> tuple[ToolSettings, AgentProfile]:
    from mato_loop_backend.tools.catalog import tool_registry

    tools = await load_effective_tools(db, user_id, trust_level)
    profile = await load_agent_profile(db, user_id, profile_id)
    permission_profile = profile
    if profile.owner_id is not None and profile.integration_id is None and not await personal_settings_allowed(db, user_id, tools.personal_settings_enabled):
        permission_profile = next(item for item in shipped_profiles() if item.id == profile.base_profile_id)
        instance_profiles = await SettingsStore.load(db, AgentProfilesSettings)
        from mato_loop_backend.settings.models import apply_profile_overrides

        if permission_profile.id in instance_profiles.builtin_access_overrides:
            permission_profile = apply_profile_overrides(permission_profile, instance_profiles.builtin_access_overrides[permission_profile.id])
    if permission_profile._shell_prefixes_configured or permission_profile.shell_prefixes.read or permission_profile.shell_prefixes.write:
        tools.shell_prefixes = permission_profile.shell_prefixes
    for name, available in permission_profile.tool_availability.items():
        tools.tool_availability[name] = tools.tool_availability.get(name, True) and available
    tools._agent_permissions = AgentProfilePermissions(policies={
        key: value for key, value in getattr(permission_profile, f"{trust_level.value}_permissions").policies.items()
        if key in tool_registry.actions_by_capability
    })
    tools._agent_profile = profile
    tools._temporary_files_enabled = profile.temporary_files_enabled
    apply_integration_permissions(tools, await load_effective_integrations(db, user_id), profile.id)
    return tools, profile
