from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, StrictBool
from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend.api.errors import ApiError
from mato_loop_backend.api.permission_settings import (
    SettingsApiScope,
    _catalog_policies,
    _load_profile_access,
    _validate_permissions,
)
from mato_loop_backend.db.session import get_db_session
from mato_loop_backend.models import UserAccount
from mato_loop_backend.security import require_user
from mato_loop_backend.settings import (
    GeneralSettings,
    IntegrationSettings,
    SettingsStore,
    ToolSettings,
    WebAccessSettings,
    load_effective_tools,
)
from mato_loop_backend.settings.models import (
    AgentProfileAccessOverrides,
    AgentProfileOverrides,
    AgentProfilePermissions,
    ShellPrefixSettings,
    ToolPermissionPreset,
    TrustLevel,
)
from mato_loop_backend.tools.catalog import tool_registry

router = APIRouter(prefix="/tools", dependencies=[Depends(require_user)])
MEDIA_FIELDS = {
    "generate_image": "generateImage", "edit_image": "editImage", "generate_speech": "generateSpeech",
    "generate_audio": "generateAudio", "transcribe_audio": "transcribeAudio",
    "analyze_video": "analyzeVideo", "analyze_audio": "analyzeAudio",
}


class MediaTimeoutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timeout_seconds: int = Field(
        default=300,
        ge=10,
        le=3_600,
        validation_alias=AliasChoices("timeoutSeconds", "timeout_seconds"),
    )


class ToolConfigurationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    personal_settings_enabled: bool = Field(default=False, alias="personalSettingsEnabled")
    private_permissions: AgentProfilePermissions = Field(default_factory=AgentProfilePermissions, alias="privatePermissions")
    public_permissions: AgentProfilePermissions = Field(default_factory=AgentProfilePermissions, alias="publicPermissions")
    shell_prefixes: ShellPrefixSettings = Field(default_factory=ShellPrefixSettings, alias="shellPrefixes")
    tool_permission_presets: dict[TrustLevel, ToolPermissionPreset] = Field(default_factory=dict, alias="toolPermissionPresets")

    tool_availability: dict[str, StrictBool] = Field(
        validation_alias=AliasChoices("toolAvailability", "tool_availability")
    )
    web_access: WebAccessSettings = Field(
        default_factory=WebAccessSettings,
        validation_alias=AliasChoices("webAccess", "web_access"),
    )
    generate_image: MediaTimeoutRequest = Field(
        validation_alias=AliasChoices("generateImage", "generate_image")
    )
    edit_image: MediaTimeoutRequest = Field(
        validation_alias=AliasChoices("editImage", "edit_image")
    )
    generate_speech: MediaTimeoutRequest = Field(
        validation_alias=AliasChoices("generateSpeech", "generate_speech")
    )
    generate_audio: MediaTimeoutRequest = Field(
        validation_alias=AliasChoices("generateAudio", "generate_audio")
    )
    transcribe_audio: MediaTimeoutRequest = Field(
        validation_alias=AliasChoices("transcribeAudio", "transcribe_audio")
    )
    analyze_video: MediaTimeoutRequest = Field(
        validation_alias=AliasChoices("analyzeVideo", "analyze_video")
    )
    analyze_audio: MediaTimeoutRequest = Field(
        default_factory=lambda: MediaTimeoutRequest(timeout_seconds=1_800),
        validation_alias=AliasChoices("analyzeAudio", "analyze_audio"),
    )


def tool_configuration_payload(settings: ToolSettings) -> dict:
    from mato_loop_backend.settings.tool_presets import preset_catalog

    defaults = settings.model_copy(deep=True)
    defaults.private_permissions = AgentProfilePermissions()
    defaults.public_permissions = AgentProfilePermissions()
    groups = [
        group
        for group in tool_registry.catalog(settings, IntegrationSettings())
        if group["tools"]
    ]
    for group in groups:
        for tool in group["tools"]:
            tool["available"] = settings.tool_availability.get(tool["name"], True)
    return {
        "personalSettingsEnabled": settings.personal_settings_enabled,
        "toolPermissionPresets": {trust.value: getattr(settings, f"{trust.value}_permission_preset") for trust in TrustLevel},
        "presetOptions": preset_catalog(tool_registry.actions_by_capability),
        "privatePermissions": _tool_permissions_payload(settings.private_permissions, tool_registry.catalog(defaults, IntegrationSettings())),
        "publicPermissions": _tool_permissions_payload(settings.public_permissions, tool_registry.catalog(defaults, IntegrationSettings())),
        "shellPrefixes": settings.shell_prefixes.model_dump(),
        "webAccess": settings.web_access.model_dump(mode="json", by_alias=True),
        **{alias: {"timeoutSeconds": getattr(settings, field).timeout_seconds} for field, alias in MEDIA_FIELDS.items()},
        "privateGroups": tool_registry.catalog(defaults.model_copy(deep=True).select_trust(TrustLevel.PRIVATE), IntegrationSettings()),
        "publicGroups": tool_registry.catalog(defaults.model_copy(deep=True).select_trust(TrustLevel.PUBLIC), IntegrationSettings()),
        "groups": groups,
        "toolAvailability": settings.tool_availability,
    }


def _tool_permissions_payload(permissions: AgentProfilePermissions, groups: list[dict]) -> dict:
    allowed = _catalog_policies(groups)
    return AgentProfilePermissions(policies={key: value for key, value in permissions.policies.items() if key in allowed}).model_dump(mode="json", by_alias=True)


@router.get("/availability")
async def get_tool_availability(
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict:
    settings = await load_effective_tools(db, user.id)
    return {
        "tools": settings.tool_availability,
    }


@router.get("/configuration")
async def get_tool_configuration(
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict:
    if not user.is_admin:
        raise ApiError(403, "auth.adminRequired")
    return tool_configuration_payload(await SettingsStore.load(db, ToolSettings))


@router.put("/configuration")
async def update_tool_configuration(
    payload: ToolConfigurationRequest,
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict:
    if not user.is_admin:
        raise ApiError(403, "auth.adminRequired")
    general = await SettingsStore.load(db, GeneralSettings, for_update=True) if payload.tool_permission_presets else None
    settings = await SettingsStore.load(db, ToolSettings, for_update=True)
    if general is not None and not general.model_trust_enabled and payload.tool_permission_presets.get(TrustLevel.PUBLIC, settings.public_permission_preset) != settings.public_permission_preset:
        raise ApiError(422, "validation.invalidChoice", field="toolPermissionPresets")
    known_tools = {
        tool["name"]
        for group in tool_registry.catalog(settings, IntegrationSettings())
        for tool in group["tools"]
    }
    if any(
        settings.tool_availability.get(name) != payload.tool_availability[name]
        for name in payload.tool_availability.keys() - known_tools
    ):
        raise ApiError(422, "validation.invalidChoice", field="toolAvailability")
    settings.tool_availability = {
        **{name: value for name, value in settings.tool_availability.items() if name not in known_tools},
        **payload.tool_availability,
    }
    for field in ("private_permissions", "public_permissions"):
        if field in payload.model_fields_set:
            value = getattr(payload, field)
            _validate_permissions(value, _catalog_policies(tool_registry.catalog(settings, IntegrationSettings())), field)
            setattr(settings, field, value)
    for field in ("personal_settings_enabled", "shell_prefixes"):
        if field in payload.model_fields_set:
            setattr(settings, field, getattr(payload, field))
    if "web_access" in payload.model_fields_set:
        settings.web_access = payload.web_access
    for field in MEDIA_FIELDS:
        getattr(settings, field).timeout_seconds = getattr(payload, field).timeout_seconds
    for trust, preset in payload.tool_permission_presets.items():
        setattr(settings, f"{trust.value}_permission_preset", preset)
    await SettingsStore.save(db, settings)
    await db.commit()
    return tool_configuration_payload(settings)


class ProfileToolPermissionsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    private_permissions: AgentProfilePermissions = Field(default_factory=AgentProfilePermissions, alias="privatePermissions")
    public_permissions: AgentProfilePermissions = Field(default_factory=AgentProfilePermissions, alias="publicPermissions")
    shell_prefixes: ShellPrefixSettings | None = Field(default=None, alias="shellPrefixes")
    tool_availability: dict[str, StrictBool] = Field(default_factory=dict, alias="toolAvailability")


async def _profile_permissions(db, user, profile_id, scope, *, for_update=False):
    settings, owner_id, overrides, baseline, tools = await _load_profile_access(db, user, profile_id, scope, for_update=for_update)
    groups = {}
    for trust in TrustLevel:
        copy = tools.model_copy(deep=True).select_trust(trust)
        copy._agent_permissions = getattr(baseline, f"{trust.value}_permissions")
        for name, available in {**baseline.tool_availability, **(overrides.tool_availability or {})}.items():
            copy.tool_availability[name] = tools.tool_availability.get(name, True) and available
        groups[trust.value] = tool_registry.catalog(copy, IntegrationSettings())
    payload = {
        "groupsByTrust": groups,
        "shellPrefixes": overrides.shell_prefixes.model_dump() if overrides.shell_prefixes is not None else None,
        "inheritedShellPrefixes": (baseline.shell_prefixes if baseline._shell_prefixes_configured else tools.shell_prefixes).model_dump(),
        "toolAvailability": overrides.tool_availability or {},
        "globalToolAvailability": tools.tool_availability,
        "inheritedToolAvailability": {**tools.tool_availability, **{name: tools.tool_availability.get(name, True) and available for name, available in baseline.tool_availability.items()}},
        **{f"{trust.value}Permissions": _tool_permissions_payload(getattr(overrides, f"{trust.value}_permissions") or AgentProfilePermissions(), groups[trust.value]) for trust in TrustLevel},
    }
    return settings, owner_id, overrides, payload


@router.get("/profiles/{profile_id}/permissions")
async def get_profile_tool_permissions(
    profile_id: UUID,
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    scope: SettingsApiScope = "personal",
) -> dict:
    return (await _profile_permissions(db, user, profile_id, scope))[3]


@router.put("/profiles/{profile_id}/permissions")
async def update_profile_tool_permissions(
    profile_id: UUID,
    payload: ProfileToolPermissionsRequest,
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    scope: SettingsApiScope = "personal",
) -> dict:
    settings, owner_id, overrides, current = await _profile_permissions(db, user, profile_id, scope, for_update=True)
    values = overrides.model_dump(exclude_unset=True)
    for trust in TrustLevel:
        field = f"{trust.value}_permissions"
        if field not in payload.model_fields_set:
            continue
        branch = getattr(payload, field)
        _validate_permissions(branch, _catalog_policies(current["groupsByTrust"][trust.value]), field)
        values.pop(field, None)
        if branch.policies:
            values[field] = branch.model_dump()
    if "shell_prefixes" in payload.model_fields_set:
        values.pop("shell_prefixes", None)
        if payload.shell_prefixes is not None:
            values["shell_prefixes"] = payload.shell_prefixes.model_dump()
    if "tool_availability" in payload.model_fields_set:
        known = {tool["name"] for group in current["groupsByTrust"]["private"] for tool in group["tools"]}
        if payload.tool_availability.keys() - known:
            raise ApiError(422, "validation.invalidChoice", field="toolAvailability")
        values.pop("tool_availability", None)
        if payload.tool_availability:
            values["tool_availability"] = payload.tool_availability
    if isinstance(overrides, AgentProfileAccessOverrides):
        settings.builtin_access_overrides[profile_id] = AgentProfileAccessOverrides.model_validate(values)
    else:
        next(item for item in settings.profiles if item.id == profile_id).overrides = AgentProfileOverrides.model_validate(values)
    await SettingsStore.save(db, settings, user_id=owner_id)
    await db.commit()
    return (await _profile_permissions(db, user, profile_id, scope))[3]
