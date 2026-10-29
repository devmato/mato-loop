from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, Any, ClassVar, Literal, Self
from unicodedata import category as unicode_category
from uuid import UUID, uuid4

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    SerializationInfo,
    StrictBool,
    computed_field,
    field_serializer,
    field_validator,
    model_serializer,
    model_validator,
)

from mato_loop_backend.public_url import normalize_public_base_url

_MODEL_RUNTIME_KEYS = {
    "context_window_tokens": "contextWindowTokens",
    "discovered_context_window_tokens": "discoveredContextWindowTokens",
    "use_discovered_context_window": "useDiscoveredContextWindow",
    "max_output_tokens": "maxOutputTokens",
    "characters_per_token": "charactersPerToken",
    "supports_tools": "supportsTools",
    "supports_parallel_tool_calls": "supportsParallelToolCalls",
    "supports_streaming": "supportsStreaming",
    "input_modalities": "inputModalities",
    "supports_image_input": "supportsImageInput",
    "reasoning_summary": "reasoningSummary",
    "reasoning_effort": "reasoningEffort",
    "trust_level": "trustLevel",
    "pseudonymization_enabled": "pseudonymizationEnabled",
}

DEFAULT_WEB_ACCESS_USER_AGENT = "MatoLoop/1.0"


def _camel_case(value: str) -> str:
    first, *rest = value.split("_")
    return first + "".join(part.capitalize() for part in rest)


class SettingsScope(StrEnum):
    GLOBAL = "global"
    USER = "user"


class SettingsDocument(BaseModel):
    settings_key: ClassVar[str]
    settings_scope: ClassVar[SettingsScope] = SettingsScope.GLOBAL


def normalize_agent_display_name(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("display_name must not be blank")
    if any(unicode_category(character) in {"Cc", "Cf"} for character in cleaned):
        raise ValueError("display_name must not contain control characters")
    return cleaned


def normalize_skill_name(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("name must not be blank")
    if any(unicode_category(character) in {"Cc", "Cf"} for character in cleaned):
        raise ValueError("name must not contain control characters")
    return cleaned


def normalize_skill_text(value: str, field: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field} must not be blank")
    return cleaned


class MemoryPolicy(StrEnum):
    INHERIT = "inherit"
    ALLOW = "allow"
    DENY = "deny"


class TrustLevel(StrEnum):
    PUBLIC = "public"
    PRIVATE = "private"

ToolPermissionPreset = Literal["ask_all", "balanced", "ask_changes", "ask_delete", "auto_all"]


class ToolExecutionPolicy(StrEnum):
    DISABLED = "disabled"
    ASK = "ask"
    AUTO = "auto"


GENERAL_PROFILE_ID = UUID("00000000-0000-4000-8000-000000000001")
DEFAULT_PERSONA_ID = UUID("00000000-0000-4000-8000-000000000002")
PLAN_PROFILE_ID = UUID("00000000-0000-4000-8000-000000000003")
BUILD_PROFILE_ID = UUID("00000000-0000-4000-8000-000000000004")
SYSADMIN_PROFILE_ID = UUID("00000000-0000-4000-8000-000000000005")
CORE_PROFILE_NAMES = {
    GENERAL_PROFILE_ID: "Allgemein",
    PLAN_PROFILE_ID: "Plan",
    BUILD_PROFILE_ID: "Build",
    SYSADMIN_PROFILE_ID: "Sysadmin",
}
CORE_PROFILE_IDS = frozenset(CORE_PROFILE_NAMES)
WEATHER_SKILL_ID = UUID("00000000-0000-4000-8000-000000000101")

ProfileToolCallLimit = Annotated[int, Field(strict=True, gt=0)] | None


class PersonaProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: UUID = Field(default_factory=uuid4)
    name: str = Field(min_length=1, max_length=80)
    instructions: str = Field(default="", max_length=32_002)


def validate_persona_catalog(personas: list[PersonaProfile]) -> list[PersonaProfile]:
    if len({persona.id for persona in personas}) != len(personas) or len({persona.name.casefold() for persona in personas}) != len(personas):
        raise ValueError("persona ids and names must be unique")
    return personas


class AgentProfilePermissions(BaseModel):
    model_config = ConfigDict(
        alias_generator=lambda value: _camel_case(value),
        populate_by_name=True,
        extra="forbid",
    )

    policies: dict[str, ToolExecutionPolicy] = Field(default_factory=dict)
    integration_policies: dict[str, dict[str, ToolExecutionPolicy]] = Field(
        default_factory=dict
    )
    resource_policies: dict[
        str, dict[str, dict[str, ToolExecutionPolicy]]
    ] = Field(default_factory=dict)
    resource_availability: dict[str, dict[str, StrictBool]] = Field(default_factory=dict)

    def merged(self, override: AgentProfilePermissions, *, include_policies: bool = True) -> AgentProfilePermissions:
        data = self.model_dump()
        if include_policies:
            data["policies"].update(override.policies)
        for integration_id, policies in override.integration_policies.items():
            data["integration_policies"].setdefault(integration_id, {}).update(policies)
        for integration_id, resources in override.resource_policies.items():
            target = data["resource_policies"].setdefault(integration_id, {})
            for resource_key, policies in resources.items():
                target.setdefault(resource_key, {}).update(policies)
        for integration_id, resources in override.resource_availability.items():
            data["resource_availability"].setdefault(integration_id, {}).update(resources)
        return AgentProfilePermissions.model_validate(data)


class ShellPrefixSettings(BaseModel):
    read: list[list[str]] = Field(default_factory=list, max_length=100)
    write: list[list[str]] = Field(default_factory=list, max_length=100)

    @field_validator("read", "write")
    @classmethod
    def validate_prefixes(cls, value: list[list[str]]) -> list[list[str]]:
        normalized: list[list[str]] = []
        for prefix in value:
            if not 1 <= len(prefix) <= 8 or any(
                not token
                or len(token) > 256
                or any(not 33 <= ord(character) <= 126 for character in token)
                for token in prefix
            ):
                raise ValueError("invalid shell prefix")
            if prefix not in normalized:
                normalized.append(prefix)
        return normalized


class AgentProfileAccessOverrides(BaseModel):
    model_config = ConfigDict(alias_generator=lambda value: _camel_case(value), populate_by_name=True, extra="forbid")

    integrations_enabled_by_default: StrictBool | None = None
    integration_tool_loading: Literal["on_demand", "native", "all"] | None = None
    temporary_files_enabled: StrictBool | None = None
    private_permissions: AgentProfilePermissions | None = None
    public_permissions: AgentProfilePermissions | None = None
    shell_prefixes: ShellPrefixSettings | None = None
    tool_availability: dict[str, StrictBool] | None = None

    @model_validator(mode="after")
    def reject_null_overrides(self) -> Self:
        if any(
            getattr(self, field) is None
            for field in self.model_fields_set
        ):
            raise ValueError("access overrides must not be null")
        return self

    @model_serializer(mode="wrap")
    def serialize_sparse(self, handler, info):
        data = handler(self)
        keys = {_camel_case(field) if info.by_alias else field for field in self.model_fields_set}
        return {key: value for key, value in data.items() if key in keys}


def validate_builtin_access_overrides(value: dict[UUID, AgentProfileAccessOverrides]) -> dict[UUID, AgentProfileAccessOverrides]:
    if value.keys() - CORE_PROFILE_NAMES.keys():
        raise ValueError("access overrides require a built-in profile")
    return value


class AgentProfile(BaseModel):
    _shell_prefixes_configured: bool = PrivateAttr(default=False)
    model_config = ConfigDict(
        alias_generator=lambda value: _camel_case(value),
        populate_by_name=True,
        extra="forbid",
    )

    id: UUID = Field(default_factory=uuid4)
    enabled: StrictBool = True
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)
    instructions: str = Field(min_length=1, max_length=16_000)
    runtime_instructions: str = Field(default="", max_length=4000)
    max_tool_calls: ProfileToolCallLimit = 25
    todo_enabled: StrictBool = False
    repository_instructions_enabled: StrictBool = False
    integrations_enabled_by_default: StrictBool = True
    integration_tool_loading: Literal["on_demand", "native", "all"] = "on_demand"
    temporary_files_enabled: StrictBool = False
    private_permissions: AgentProfilePermissions = Field(
        default_factory=AgentProfilePermissions
    )
    public_permissions: AgentProfilePermissions = Field(
        default_factory=AgentProfilePermissions
    )
    shell_prefixes: ShellPrefixSettings = Field(default_factory=ShellPrefixSettings)
    tool_availability: dict[str, StrictBool] = Field(default_factory=dict)
    delegation_enabled: StrictBool = False
    session_write_paths: list[str] | None = None
    base_profile_id: UUID | None = Field(default=None, exclude=True)
    owner_id: UUID | None = Field(default=None, exclude=True)
    integration_id: UUID | None = Field(default=None, exclude=True)
    editable: bool = Field(default=True, exclude=True)

    @field_validator("name", "description", "instructions", "runtime_instructions", mode="before")
    @classmethod
    def normalize_text(cls, value: Any, info: Any) -> Any:
        if not isinstance(value, str):
            return value
        cleaned = value.strip()
        if info.field_name in {"name", "instructions"} and not cleaned:
            raise ValueError(f"{info.field_name} must not be blank")
        return cleaned

    @field_validator("session_write_paths")
    @classmethod
    def validate_session_write_paths(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        for path in value:
            normalized = PurePosixPath(path)
            if (
                not path
                or "\\" in path
                or path.endswith("/")
                or normalized.is_absolute()
                or str(normalized) != path
                or any(part in {".", ".."} for part in normalized.parts)
                or any(character in path for character in "*?[]{}")
            ):
                raise ValueError("session write paths must be normalized relative POSIX file paths")
        return value


class AgentProfileOverrides(BaseModel):
    model_config = ConfigDict(
        alias_generator=lambda value: _camel_case(value),
        populate_by_name=True,
        extra="forbid",
    )

    instructions: str | None = Field(default=None, max_length=16_000)
    runtime_instructions: str | None = Field(default=None, max_length=4000)
    max_tool_calls: ProfileToolCallLimit = None
    todo_enabled: StrictBool | None = None
    repository_instructions_enabled: StrictBool | None = None
    integrations_enabled_by_default: StrictBool | None = None
    integration_tool_loading: Literal["on_demand", "native", "all"] | None = None
    temporary_files_enabled: StrictBool | None = None
    private_permissions: AgentProfilePermissions | None = None
    public_permissions: AgentProfilePermissions | None = None
    shell_prefixes: ShellPrefixSettings | None = None
    tool_availability: dict[str, StrictBool] | None = None
    delegation_enabled: StrictBool | None = None
    session_write_paths: list[str] | None = None

    _validate_paths = field_validator("session_write_paths")(
        lambda value: AgentProfile.validate_session_write_paths(value)
    )

    @model_validator(mode="after")
    def reject_null_non_nullable_fields(self) -> Self:
        nullable = {
            "max_tool_calls",
            "session_write_paths",
        }
        if any(
            field in self.model_fields_set and getattr(self, field) is None
            for field in type(self).model_fields
            if field not in nullable
        ):
            raise ValueError("only nullable profile fields accept null overrides")
        return self

    @field_validator("instructions", mode="before")
    @classmethod
    def normalize_instructions(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        value = value.strip()
        if not value:
            raise ValueError("instructions must not be blank")
        return value


class CustomAgentProfile(BaseModel):
    model_config = ConfigDict(
        alias_generator=lambda value: _camel_case(value),
        populate_by_name=True,
        extra="forbid",
    )

    id: UUID = Field(default_factory=uuid4)
    enabled: StrictBool = True
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(max_length=500)
    base_profile_id: UUID
    overrides: AgentProfileOverrides

    @field_validator("name", "description", mode="before")
    @classmethod
    def normalize_text(cls, value: Any, info: Any) -> Any:
        if not isinstance(value, str):
            return value
        cleaned = value.strip()
        if info.field_name == "name" and not cleaned:
            raise ValueError("name must not be blank")
        return cleaned

    @field_serializer("overrides")
    def serialize_overrides(
        self, value: AgentProfileOverrides, info: SerializationInfo
    ) -> dict[str, Any]:
        return value.model_dump(
            mode=info.mode, by_alias=info.by_alias, exclude_unset=True
        )


def resolve_agent_profile(
    profile: CustomAgentProfile, base: AgentProfile, owner_id: UUID | None
) -> AgentProfile:
    return apply_profile_overrides(base, profile.overrides).model_copy(update={
        "id": profile.id,
        "enabled": profile.enabled,
        "name": profile.name,
        "description": profile.description,
        "base_profile_id": profile.base_profile_id,
        "owner_id": owner_id,
    })


def apply_profile_overrides(
    base: AgentProfile, overrides: AgentProfileOverrides | AgentProfileAccessOverrides,
) -> AgentProfile:
    values = base.model_dump()
    values.update(overrides.model_dump(exclude_unset=True))
    for field in ("private_permissions", "public_permissions"):
        if field in overrides.model_fields_set:
            values[field] = getattr(base, field).merged(getattr(overrides, field)).model_dump()
    if overrides.tool_availability is not None:
        values["tool_availability"] = {**base.tool_availability, **overrides.tool_availability}
    result = AgentProfile.model_validate(values)
    result._shell_prefixes_configured = base._shell_prefixes_configured or "shell_prefixes" in overrides.model_fields_set
    return result


def general_profile() -> AgentProfile:
    return AgentProfile(
        id=GENERAL_PROFILE_ID,
        name=CORE_PROFILE_NAMES[GENERAL_PROFILE_ID],
        description="Standardagent für Sessions ohne ausgewähltes Profil.",
        instructions="Follow the user's request and the selected persona.",
        integration_tool_loading="native",
        delegation_enabled=False,
        session_write_paths=None,
    )


def plan_profile() -> AgentProfile:
    policies = {
        "filesystem.read": ToolExecutionPolicy.AUTO,
        "filesystem.write": ToolExecutionPolicy.ASK,
        "filesystem.delete": ToolExecutionPolicy.ASK,
        "working_directory.external": ToolExecutionPolicy.ASK,
        "session.files.write": ToolExecutionPolicy.ASK,
        "session.files.delete": ToolExecutionPolicy.ASK,
        "shell.read": ToolExecutionPolicy.AUTO,
        "shell.write": ToolExecutionPolicy.ASK,
        "shell.delete": ToolExecutionPolicy.ASK,
    }
    return AgentProfile(
        id=PLAN_PROFILE_ID,
        name=CORE_PROFILE_NAMES[PLAN_PROFILE_ID],
        description=(
            "Analysiert und plant, ohne zu implementieren. Schreib- und Löschaktionen benötigen eine Freigabe. "
            "Liest Dateien im Arbeitsverzeichnis und führt als lesend "
            "eingestufte Shell-Befehle computerweit automatisch aus, "
            "auch mit öffentlichen Modellen; gelesene Inhalte werden an das Modell übermittelt."
        ),
        instructions=(
            "Analyze the user's request and relevant evidence without changing the project. "
            "When the profile permits temporary files, use the reported temporary directory for analysis checks only. "
            "Do not implement the plan. Match depth to the requested deliverable. For questions and idea "
            "assessments, give a concise assessment and recommendation, not an implementation plan. "
            "For conceptual assessments, answer directly when the supplied facts are sufficient. Use tools "
            "only to resolve a specific missing fact that could change the recommendation. "
            "For planning requests, return the shortest actionable plan directly in the conversation. "
            "Inspect only evidence needed for that deliverable; stop when it is sufficient. "
            "Resolve missing implementation facts from the code rather than asking the user. "
            "Prefer a short numbered list. Do not restate the request or add boilerplate sections. "
            "Include the recommended approach, affected areas, assumptions, risks, implementation steps, and "
            "verification only when they add actionable information. Follow user instructions within these "
            "planning limits. Prefer the simplest "
            "approach that fully meets the request; do not substitute a simpler but different feature. "
            "Reuse existing code and conventions, then "
            "standard-library or native features, then installed dependencies. Add abstractions, "
            "dependencies, configuration, or extension points only when current requirements justify "
            "them. Ask only decisions needed for the current deliverable. For implementation plans, "
            "before finalizing, ask all currently known questions together only for unresolved user "
            "choices whose alternatives would materially change user-visible behavior, scope, architecture, "
            "compatibility, migration, or acceptance. Ask again only when an answer reveals another required "
            "decision. Do not turn such choices into assumptions; decide ordinary implementation details from "
            "project evidence. After required decisions are resolved, return the requested deliverable."
        ),
        runtime_instructions=(
            "Plan mode is active. Inspect, analyze, ask questions, and plan only. Available tools, user requests, "
            "and tool approvals do not authorize implementation. Do not modify project files or external state, "
            "install software, start or stop services, or delegate implementation. Permitted temporary files "
            "are for analysis checks in the reported temporary directory only. Memory reads and writes follow "
            "the normal memory permissions. These limits apply to actual effects through tools, shell commands, "
            "scripts, APIs, and subprocesses; shell.read does not guarantee read-only effects. "
            "When asked to implement, continue only permitted analysis and planning and explain that "
            "implementation requires Build. Do not announce implementation or mark it as started or completed."
        ),
        max_tool_calls=None,
        todo_enabled=True,
        repository_instructions_enabled=True,
        temporary_files_enabled=True,
        integrations_enabled_by_default=False,
        private_permissions=AgentProfilePermissions(policies=policies),
        public_permissions=AgentProfilePermissions(policies=policies),
        delegation_enabled=True,
        session_write_paths=None,
    )


def build_profile() -> AgentProfile:
    policies = {
        "filesystem.read": ToolExecutionPolicy.AUTO,
        "filesystem.write": ToolExecutionPolicy.AUTO,
        "filesystem.delete": ToolExecutionPolicy.AUTO,
        "working_directory.external": ToolExecutionPolicy.ASK,
        "shell.read": ToolExecutionPolicy.AUTO,
        "shell.write": ToolExecutionPolicy.AUTO,
        "shell.delete": ToolExecutionPolicy.AUTO,
    }
    return AgentProfile(
        id=BUILD_PROFILE_ID,
        name=CORE_PROFILE_NAMES[BUILD_PROFILE_ID],
        description=(
            "Liest, ändert und löscht Dateien im Arbeitsverzeichnis automatisch. Führt Shell-Aktionen "
            "computerweit automatisch aus, "
            "auch mit öffentlichen Modellen; gelesene Inhalte werden an das Modell übermittelt."
        ),
        instructions=(
            "Carry out the exact current task; keep an analysis-only task read-only. "
            "Understand the relevant context before changing it. Prefer the simplest solution that "
            "fully meets the request: reuse existing code and conventions, then standard-library or "
            "native features, then installed dependencies. Add abstractions, dependencies, configuration, "
            "or extension points only when current requirements justify them. Fix root causes, preserve "
            "required validation and security, and avoid unrelated refactors. When the user asks to implement "
            "a plan discussed earlier in the conversation, apply newer user instructions over it. A profile "
            "switch alone does not authorize implementation. Ask only for a missing user decision "
            "that materially blocks the work; decide ordinary implementation details from the project. "
            "Verify the result appropriately and report the outcome, checks, and remaining blockers concisely."
        ),
        max_tool_calls=None,
        todo_enabled=True,
        repository_instructions_enabled=True,
        temporary_files_enabled=True,
        integrations_enabled_by_default=False,
        private_permissions=AgentProfilePermissions(policies=policies),
        public_permissions=AgentProfilePermissions(policies=policies),
        delegation_enabled=True,
        session_write_paths=None,
    )


def sysadmin_profile() -> AgentProfile:
    policies = {
        "filesystem.read": ToolExecutionPolicy.AUTO,
        "filesystem.write": ToolExecutionPolicy.ASK,
        "filesystem.delete": ToolExecutionPolicy.ASK,
        "working_directory.external": ToolExecutionPolicy.AUTO,
        "shell.read": ToolExecutionPolicy.AUTO,
        "shell.write": ToolExecutionPolicy.ASK,
        "shell.delete": ToolExecutionPolicy.ASK,
    }
    return AgentProfile(
        id=SYSADMIN_PROFILE_ID,
        name=CORE_PROFILE_NAMES[SYSADMIN_PROFILE_ID],
        description=(
            "Unterstützt bei Einrichtung, Wartung und Fehlerdiagnose von Systemen, Diensten und "
            "Docker-Containern. Liest automatisch, auch mit öffentlichen Modellen; gelesene Inhalte "
            "werden an das Modell übermittelt. Änderungen benötigen eine Freigabe."
        ),
        instructions=(
            "Support the user's system administration tasks, including setup, configuration, maintenance, "
            "diagnostics, services, networking, storage, permissions, packages, and Docker. Inspect the "
            "actual state, configuration, status, and logs before making changes. Use available read actions "
            "without asking for confirmation. For changes, use the provided tools and rely on the host "
            "approval flow instead of asking for duplicate confirmation in chat. Prefer the smallest safe "
            "change, avoid unrelated changes, and preserve security controls. Explain material downtime or "
            "data-loss risks before the applicable tool call. Verify approved changes and report the outcome, "
            "checks, and remaining problems concisely. Never expose secrets."
        ),
        max_tool_calls=None,
        todo_enabled=True,
        integrations_enabled_by_default=False,
        private_permissions=AgentProfilePermissions(policies=policies),
        public_permissions=AgentProfilePermissions(policies=policies),
        session_write_paths=None,
    )


def core_profiles() -> list[AgentProfile]:
    return [general_profile(), plan_profile(), build_profile(), sysadmin_profile()]


def cap_agent_profile_policy(
    settings: ToolSettings,
    capability: str,
    baseline: ToolExecutionPolicy,
    *,
    integration_id: UUID | None = None,
    resource_key: str | None = None,
    include_scoped: bool = False,
) -> ToolExecutionPolicy:
    permissions = settings._agent_permissions
    if settings._agent_profile_unavailable:
        return ToolExecutionPolicy.DISABLED
    if permissions is None:
        return baseline
    profile_policy = permissions.policies.get(capability) if integration_id is None else None
    if integration_id is None and profile_policy is None and capability == "session.files.read":
        profile_policy = permissions.policies.get("filesystem.read")
    if integration_id is not None:
        profile_policy = permissions.integration_policies.get(
            str(integration_id), {}
        ).get(capability, profile_policy)
        if resource_key is not None:
            profile_policy = (
                permissions.resource_policies.get(str(integration_id), {})
                .get(resource_key, {})
                .get(capability, profile_policy)
            )
    elif include_scoped and profile_policy in {None, ToolExecutionPolicy.DISABLED}:
        scoped_policies = (
            policies.get(capability)
            for policies in permissions.integration_policies.values()
        )
        resource_policies = (
            policies.get(capability)
            for resources in permissions.resource_policies.values()
            for policies in resources.values()
        )
        profile_policy = next(
            (
                policy
                for policy in (*scoped_policies, *resource_policies)
                if policy is not None and policy != ToolExecutionPolicy.DISABLED
            ),
            profile_policy,
        )
        if (capability == "filesystem.read" or settings._temporary_files_enabled and capability == "filesystem.write") and profile_policy == ToolExecutionPolicy.DISABLED:
            # Offer existing file tools; concrete temporary paths are authorized separately.
            profile_policy = ToolExecutionPolicy.ASK
    if profile_policy is None:
        return baseline
    return profile_policy


class IntegrationAdapterAvailability(StrEnum):
    FULL = "full"
    INSTANCE_ONLY = "instance_only"
    DISABLED = "disabled"


class ModelOrigin(StrEnum):
    MANUAL = "manual"
    DISCOVERED = "discovered"


class ModelAvailability(StrEnum):
    AVAILABLE = "available"
    MISSING = "missing"


class ModelCapability(StrEnum):
    CHAT = "chat"
    IMAGE_GENERATION = "image_generation"
    IMAGE_EDITING = "image_editing"
    SPEECH_GENERATION = "speech_generation"
    AUDIO_GENERATION = "audio_generation"
    AUDIO_TRANSCRIPTION = "audio_transcription"


class ModelInputModality(StrEnum):
    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"


class ReasoningEffort(StrEnum):
    NONE = "none"
    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"
    ULTRA = "ultra"


class LlmProviderConfig(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    name: str
    adapter_type: str
    enabled: bool = True
    trust_level: TrustLevel = Field(
        default=TrustLevel.PUBLIC,
        validation_alias=AliasChoices("trust_level", "trustLevel"),
    )
    pseudonymization_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "pseudonymization_enabled", "pseudonymizationEnabled"
        ),
    )
    memory_policy: MemoryPolicy = MemoryPolicy.INHERIT
    credential_id: UUID | None = None
    owner_id: UUID | None = Field(default=None, exclude=True)
    config: dict[str, Any] = Field(default_factory=dict)
    models_last_synced_at: datetime | None = None


class ModelConfig(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    provider_id: UUID
    model_id: str
    name: str | None = None
    enabled: bool = True
    origin: ModelOrigin = ModelOrigin.MANUAL
    availability: ModelAvailability = ModelAvailability.AVAILABLE
    last_seen_at: datetime | None = None
    discovery_metadata: dict[str, Any] = Field(default_factory=dict)
    capabilities: set[ModelCapability] = Field(
        default_factory=lambda: {ModelCapability.CHAT}
    )
    memory_policy: MemoryPolicy = MemoryPolicy.INHERIT
    trust_level: TrustLevel | None = Field(
        default=None,
        validation_alias=AliasChoices("trust_level", "trustLevel"),
    )
    pseudonymization_enabled: bool | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "pseudonymization_enabled", "pseudonymizationEnabled"
        ),
    )
    config: dict[str, Any] = Field(default_factory=dict)
    context_window_tokens: int = Field(
        default=128_000,
        ge=2,
        le=10_000_000,
        validation_alias=AliasChoices("context_window_tokens", "contextWindowTokens"),
    )
    discovered_context_window_tokens: int | None = Field(
        default=None,
        ge=2,
        le=10_000_000,
        validation_alias=AliasChoices(
            "discovered_context_window_tokens", "discoveredContextWindowTokens"
        ),
    )
    use_discovered_context_window: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "use_discovered_context_window", "useDiscoveredContextWindow"
        ),
    )
    max_output_tokens: int | None = Field(
        default=None,
        strict=True,
        gt=0,
        validation_alias=AliasChoices("max_output_tokens", "maxOutputTokens"),
    )
    discovered_max_output_tokens: int | None = Field(
        default=None,
        strict=True,
        gt=0,
        validation_alias=AliasChoices(
            "discovered_max_output_tokens", "discoveredMaxOutputTokens"
        ),
    )
    characters_per_token: float = Field(
        default=4.0,
        gt=0,
        le=100,
        validation_alias=AliasChoices("characters_per_token", "charactersPerToken"),
    )
    supports_tools: bool = Field(
        default=True,
        validation_alias=AliasChoices("supports_tools", "supportsTools"),
    )
    supports_parallel_tool_calls: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "supports_parallel_tool_calls", "supportsParallelToolCalls"
        ),
    )
    supports_streaming: bool = Field(
        default=True,
        validation_alias=AliasChoices("supports_streaming", "supportsStreaming"),
    )
    input_modalities: set[ModelInputModality] = Field(
        default_factory=lambda: {ModelInputModality.TEXT},
        validation_alias=AliasChoices("input_modalities", "inputModalities"),
    )
    reasoning_summary: bool = Field(
        default=False,
        validation_alias=AliasChoices("reasoning_summary", "reasoningSummary"),
    )
    reasoning_effort: ReasoningEffort | None = Field(
        default=None,
        validation_alias=AliasChoices("reasoning_effort", "reasoningEffort"),
    )
    @model_validator(mode="before")
    @classmethod
    def load_legacy_runtime_config(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        values = dict(value)
        config = value.get("config") if isinstance(value.get("config"), dict) else {}
        for snake_key, camel_key in _MODEL_RUNTIME_KEYS.items():
            if snake_key in values or camel_key in values:
                continue
            if snake_key in config:
                values[snake_key] = config[snake_key]
            elif camel_key in config:
                values[snake_key] = config[camel_key]
        if "input_modalities" not in values and "inputModalities" not in values:
            supports_image = values.get(
                "supports_image_input", values.get("supportsImageInput", False)
            )
            values["input_modalities"] = (
                [ModelInputModality.TEXT, ModelInputModality.IMAGE]
                if supports_image is True
                else [ModelInputModality.TEXT]
            )
        values.pop("supports_image_input", None)
        values.pop("supportsImageInput", None)
        return values

    @model_validator(mode="after")
    def validate_runtime_limits(self) -> Self:
        if (
            self.use_discovered_context_window
            and self.discovered_context_window_tokens is not None
        ):
            self.context_window_tokens = self.discovered_context_window_tokens
        if not self.supports_tools:
            self.supports_parallel_tool_calls = False
        return self

    @computed_field(return_type=bool)
    @property
    def supports_image_input(self) -> bool:
        return ModelInputModality.IMAGE in self.input_modalities

    @property
    def context_window_source(self) -> str:
        if self.use_discovered_context_window:
            return (
                "provider"
                if self.discovered_context_window_tokens is not None
                else "fallback"
            )
        return "override"


def effective_model_trust(
    provider: LlmProviderConfig, model: ModelConfig
) -> TrustLevel:
    return model.trust_level or provider.trust_level


def effective_model_pseudonymization(
    provider: LlmProviderConfig, model: ModelConfig
) -> bool:
    if model.pseudonymization_enabled is not None:
        return model.pseudonymization_enabled
    return provider.pseudonymization_enabled


class IntegrationPermissionBranch(BaseModel):
    model_config = ConfigDict(alias_generator=_camel_case, populate_by_name=True, extra="forbid")

    policies: dict[str, ToolExecutionPolicy] = Field(default_factory=dict)
    resource_policies: dict[str, dict[str, ToolExecutionPolicy]] = Field(default_factory=dict)


class IntegrationPermissions(BaseModel):
    model_config = ConfigDict(alias_generator=_camel_case, populate_by_name=True, extra="forbid")

    private_permissions: IntegrationPermissionBranch = Field(default_factory=IntegrationPermissionBranch)
    public_permissions: IntegrationPermissionBranch = Field(default_factory=IntegrationPermissionBranch)
    available: StrictBool | None = None
    tool_loading: Literal["on_demand", "all"] | None = None
    resource_availability: dict[str, StrictBool] = Field(default_factory=dict)


class IntegrationInstance(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    name: str
    integration_type: str
    adapter_type: str
    enabled: bool = True
    execution_agent_id: UUID | None = Field(
        default=None,
        validation_alias=AliasChoices("execution_agent_id", "executionAgentId"),
    )
    credential_id: UUID | None = None
    owner_id: UUID | None = Field(default=None, exclude=True)
    config: dict[str, Any] = Field(default_factory=dict)
    permissions: IntegrationPermissions = Field(default_factory=IntegrationPermissions)
    profile_permissions: dict[UUID, IntegrationPermissions] = Field(default_factory=dict)
    _execution_operations: frozenset[str] | None = PrivateAttr(default=None)

    def binding_data(self) -> dict[str, Any]:
        """Keep live permission changes separate from the connection revision fence."""
        return self.model_dump(mode="json", exclude={"permissions", "profile_permissions"})


class WebAccessSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    user_agent: str = Field(
        default=DEFAULT_WEB_ACCESS_USER_AGENT,
        min_length=1,
        max_length=512,
        alias="userAgent",
    )
    max_response_mib: int = Field(
        default=64,
        ge=1,
        le=256,
        alias="maxResponseMiB",
    )
    public_addresses_only: bool = Field(default=True, alias="publicAddressesOnly")

    @field_validator("user_agent", mode="before")
    @classmethod
    def validate_user_agent(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        if any(not 32 <= ord(character) <= 126 for character in value):
            raise ValueError("user_agent must contain visible ASCII characters only")
        return value.strip()


class WebSearchToolSettings(BaseModel):
    primary_provider: str = Field(
        default="no_key",
        min_length=1,
        max_length=100,
        validation_alias=AliasChoices("primary_provider", "primaryProvider"),
    )

    @field_validator("primary_provider", mode="before")
    @classmethod
    def migrate_duckduckgo_provider(cls, value: Any) -> Any:
        return "no_key" if value == "duckduckgo" else value


class WebFetchToolSettings(WebSearchToolSettings):
    primary_provider: str = Field(
        default="builtin", min_length=1, max_length=100,
        validation_alias=AliasChoices("primary_provider", "primaryProvider"),
    )


class MediaToolSettings(BaseModel):
    timeout_seconds: int = Field(
        default=300,
        ge=10,
        le=3_600,
        validation_alias=AliasChoices("timeout_seconds", "timeoutSeconds"),
    )


class DefaultCalendarSettings(BaseModel):
    integration_id: UUID = Field(
        validation_alias=AliasChoices("integration_id", "integrationId")
    )
    calendar_key: str = Field(
        pattern=r"^calendar:[a-f0-9]{32}$",
        validation_alias=AliasChoices("calendar_key", "calendarKey"),
    )


class DefaultTaskListSettings(BaseModel):
    integration_id: UUID = Field(
        validation_alias=AliasChoices("integration_id", "integrationId")
    )
    task_list_key: str = Field(
        pattern=r"^tasklist:[a-f0-9]{32}$",
        validation_alias=AliasChoices("task_list_key", "taskListKey"),
    )


class IntegrationToolSettings(SettingsDocument):
    resource_labels: dict[str, dict[str, str]] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("resource_labels", "resourceLabels"),
    )
    default_calendar: DefaultCalendarSettings | None = Field(
        default=None,
        validation_alias=AliasChoices("default_calendar", "defaultCalendar"),
    )
    default_task_list: DefaultTaskListSettings | None = Field(
        default=None,
        validation_alias=AliasChoices("default_task_list", "defaultTaskList"),
    )
    web_search: WebSearchToolSettings = Field(
        default_factory=WebSearchToolSettings,
        validation_alias=AliasChoices("web_search", "webSearch"),
    )


class ToolSettings(IntegrationToolSettings):
    settings_key = "tools"

    personal_settings_enabled: bool = False
    private_permission_preset: ToolPermissionPreset = "ask_changes"
    public_permission_preset: ToolPermissionPreset = "balanced"

    _frozen_action_defaults: dict[str, ToolExecutionPolicy] | None = PrivateAttr(default=None)
    _agent_profile: AgentProfile | None = PrivateAttr(default=None)
    _delegation_ceiling: dict[str, Any] | None = PrivateAttr(default=None)
    _agent_permissions: AgentProfilePermissions | None = PrivateAttr(default=None)
    _agent_profile_unavailable: bool = PrivateAttr(default=False)
    _temporary_files_enabled: bool = PrivateAttr(default=False)
    _trust_level: TrustLevel = PrivateAttr(default=TrustLevel.PRIVATE)

    private_permissions: AgentProfilePermissions = Field(
        default_factory=AgentProfilePermissions,
        validation_alias=AliasChoices("private_permissions", "privatePermissions"),
    )
    public_permissions: AgentProfilePermissions = Field(
        default_factory=AgentProfilePermissions,
        validation_alias=AliasChoices("public_permissions", "publicPermissions"),
    )
    tool_availability: dict[str, bool] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("tool_availability", "toolAvailability"),
    )
    shell_prefixes: ShellPrefixSettings = Field(
        default_factory=ShellPrefixSettings,
        validation_alias=AliasChoices("shell_prefixes", "shellPrefixes"),
    )
    web_access: WebAccessSettings = Field(
        default_factory=WebAccessSettings,
        validation_alias=AliasChoices("web_access", "webAccess"),
    )
    generate_image: MediaToolSettings = Field(
        default_factory=MediaToolSettings,
        validation_alias=AliasChoices("generate_image", "generateImage"),
    )
    edit_image: MediaToolSettings = Field(
        default_factory=MediaToolSettings,
        validation_alias=AliasChoices("edit_image", "editImage"),
    )
    generate_speech: MediaToolSettings = Field(
        default_factory=MediaToolSettings,
        validation_alias=AliasChoices("generate_speech", "generateSpeech"),
    )
    generate_audio: MediaToolSettings = Field(
        default_factory=MediaToolSettings,
        validation_alias=AliasChoices("generate_audio", "generateAudio"),
    )
    transcribe_audio: MediaToolSettings = Field(
        default_factory=MediaToolSettings,
        validation_alias=AliasChoices("transcribe_audio", "transcribeAudio"),
    )
    analyze_video: MediaToolSettings = Field(
        default_factory=lambda: MediaToolSettings(timeout_seconds=1_800),
        validation_alias=AliasChoices("analyze_video", "analyzeVideo"),
    )
    analyze_audio: MediaToolSettings = Field(
        default_factory=lambda: MediaToolSettings(timeout_seconds=1_800),
        validation_alias=AliasChoices("analyze_audio", "analyzeAudio"),
    )

    @property
    def permissions(self) -> AgentProfilePermissions:
        return (
            self.private_permissions
            if self._trust_level == TrustLevel.PRIVATE
            else self.public_permissions
        )

    @property
    def trust_level(self) -> TrustLevel:
        return self._trust_level

    def select_trust(self, trust_level: TrustLevel) -> ToolSettings:
        self._trust_level = trust_level
        return self


class UserToolSettings(IntegrationToolSettings):
    settings_key = "tools"
    settings_scope = SettingsScope.USER



class AgentProfilesSettings(SettingsDocument):
    settings_key = "agent-profiles"

    personal_agents_enabled: bool = True
    profiles: list[CustomAgentProfile] = Field(default_factory=list, max_length=99)
    builtin_access_overrides: dict[UUID, AgentProfileAccessOverrides] = Field(default_factory=dict)
    builtin_enabled: dict[UUID, StrictBool] = Field(default_factory=dict)
    default_profile_id: UUID = GENERAL_PROFILE_ID

    _validate_builtin_access = field_validator("builtin_access_overrides")(validate_builtin_access_overrides)

    @model_validator(mode="after")
    def validate_default_profile(self) -> Self:
        if self.builtin_enabled.keys() - CORE_PROFILE_IDS:
            raise ValueError("only built-in profile enablement can be overridden")
        active_ids = {key for key in CORE_PROFILE_IDS if self.builtin_enabled.get(key, True)}
        active_ids.update(profile.id for profile in self.profiles if profile.enabled)
        if self.default_profile_id not in active_ids:
            raise ValueError("the default profile must be an active instance profile")
        return self


class UserAgentProfilesSettings(SettingsDocument):
    settings_key = "agent-profiles"
    settings_scope = SettingsScope.USER

    profiles: list[CustomAgentProfile] = Field(default_factory=list, max_length=20)


class GeneralSettings(SettingsDocument):
    settings_key = "general"

    default_locale: str = "de-DE"
    default_timezone: str = "Europe/Berlin"
    public_base_url: str | None = Field(default=None, max_length=2083, alias="publicBaseUrl")
    model_trust_enabled: StrictBool = Field(default=True, alias="modelTrustEnabled")
    agent_auto_update_enabled: StrictBool = Field(default=False, alias="agentAutoUpdateEnabled")

    model_config = ConfigDict(populate_by_name=True)
    _normalize_public_url = field_validator("public_base_url", mode="before")(normalize_public_base_url)


class ChannelSettings(SettingsDocument):
    settings_key = "channels"

    enabled: bool = False
    allowed_providers: set[str] = Field(default_factory=lambda: {"telegram"})


class WebPushSettings(SettingsDocument):
    settings_key = "web-push"
    model_config = ConfigDict(
        alias_generator=_camel_case,
        populate_by_name=True,
        extra="forbid",
    )

    enabled: bool = False
    contact_email: Annotated[str | None, Field(default=None, max_length=254)]

    @field_validator("contact_email")
    @classmethod
    def validate_contact_email(cls, value: str | None) -> str | None:
        value = value.strip() if value else None
        if value is not None and not re.fullmatch(r"[^@\s]+@[^@\s]+", value):
            raise ValueError("invalid contact email")
        return value


class InstanceLimits(SettingsDocument):
    settings_key = "instance-limits"
    model_config = ConfigDict(
        alias_generator=_camel_case,
        populate_by_name=True,
        extra="forbid",
    )

    max_upload_mib: int = Field(default=1024, ge=1, le=1024)
    max_attachments_per_message: int = Field(default=8, ge=1, le=16)
    max_message_upload_mib: int = Field(default=1024, ge=1, le=1024)
    user_quota_mib: int | None = Field(default=None, ge=1, le=102_400)
    max_image_preview_mib: int = Field(default=5, ge=1, le=64)
    max_editable_text_mib: int = Field(default=10, ge=1, le=16)
    max_audio_duration_seconds: int = Field(default=3_600, ge=1, le=3_600)
    max_video_duration_seconds: int = Field(default=900, ge=1, le=900)
    max_extracted_characters: int = Field(default=5_000_000, ge=1_000, le=10_000_000)
    max_pdf_pages: int = Field(default=1_000, ge=1, le=5_000)
    max_workbook_sheets: int = Field(default=100, ge=1, le=500)
    max_workbook_cells: int = Field(default=1_000_000, ge=1_000, le=5_000_000)
    max_presentation_slides: int = Field(default=500, ge=1, le=2_000)
    max_archive_entries: int = Field(default=5_000, ge=1, le=20_000)
    max_archive_uncompressed_mib: int = Field(default=256, ge=1, le=1_024)
    max_archive_entry_mib: int = Field(default=64, ge=1, le=256)
    max_archive_compression_ratio: int = Field(default=200, ge=1, le=500)

    @model_validator(mode="before")
    @classmethod
    def remove_session_quota(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        return {key: item for key, item in value.items() if key not in {"session_quota_mib", "sessionQuotaMib"}}

    @model_validator(mode="after")
    def validate_related_limits(self) -> Self:
        if self.max_upload_mib > self.max_message_upload_mib:
            raise ValueError("max_upload_mib must not exceed max_message_upload_mib")
        if self.max_archive_entry_mib > self.max_archive_uncompressed_mib:
            raise ValueError(
                "max_archive_entry_mib must not exceed max_archive_uncompressed_mib"
            )
        return self

    @staticmethod
    def _mib(value: int) -> int:
        return value * 1024 * 1024

    @property
    def max_upload_bytes(self) -> int:
        return self._mib(self.max_upload_mib)

    @property
    def max_message_upload_bytes(self) -> int:
        return self._mib(self.max_message_upload_mib)

    @property
    def user_quota_bytes(self) -> int | None:
        return self._mib(self.user_quota_mib) if self.user_quota_mib is not None else None

    @property
    def max_image_preview_bytes(self) -> int:
        return self._mib(self.max_image_preview_mib)

    @property
    def max_editable_text_bytes(self) -> int:
        return self._mib(self.max_editable_text_mib)

    @property
    def max_archive_uncompressed_bytes(self) -> int:
        return self._mib(self.max_archive_uncompressed_mib)

    @property
    def max_archive_entry_bytes(self) -> int:
        return self._mib(self.max_archive_entry_mib)


class ChatModelOverride(BaseModel):
    model_config = ConfigDict(alias_generator=_camel_case, populate_by_name=True, extra="forbid")
    model_id: UUID | None = None
    reasoning_effort: ReasoningEffort | None = None


class ModelDefaults(BaseModel):
    model_config = ConfigDict(alias_generator=_camel_case, populate_by_name=True, extra="forbid")

    default_model_id: UUID | None = None
    reasoning_effort: ReasoningEffort | None = None
    profile_models: dict[UUID, ChatModelOverride] = Field(default_factory=dict)
    compaction_model_id: UUID | None = None
    title_generation_model_id: UUID | None = None
    title_vision_fallback_enabled: StrictBool | None = None
    dreaming_model_id: UUID | None = None
    vision_model_id: UUID | None = None
    video_model_id: UUID | None = None
    audio_analysis_model_id: UUID | None = None
    image_generation_model_id: UUID | None = None
    speech_generation_model_id: UUID | None = None
    audio_generation_model_id: UUID | None = None
    audio_transcription_model_id: UUID | None = None


class ModelDefaultsSettings(SettingsDocument, ModelDefaults):
    settings_key = "model-defaults"
    personal_settings_enabled: bool = True


class UserModelDefaultsSettings(SettingsDocument, ModelDefaults):
    settings_key = "model-defaults"
    settings_scope = SettingsScope.USER


class AiSettingsBase(SettingsDocument):
    model_config = ConfigDict(alias_generator=_camel_case, populate_by_name=True, extra="ignore")
    providers: list[LlmProviderConfig] = Field(default_factory=list)
    models: list[ModelConfig] = Field(default_factory=list)


class AiProviderSettings(AiSettingsBase):
    settings_key = "ai"

    personal_providers_enabled: bool = False


class AiSettings(AiProviderSettings, ModelDefaults):
    """Resolved runtime inventory and defaults; not a persisted settings document."""
    title_vision_fallback_enabled: bool = True


class UserAiSettings(AiSettingsBase):
    settings_key = "ai"
    settings_scope = SettingsScope.USER


class IntegrationSettingsBase(SettingsDocument):
    instances: list[IntegrationInstance] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_agent_integration_kinds(self) -> Self:
        keys = [
            (instance.execution_agent_id, instance.adapter_type)
            for instance in self.instances
            if instance.execution_agent_id is not None
        ]
        if len(keys) != len(set(keys)):
            raise ValueError("only one integration instance per kind and MatoLoop Agent is allowed")
        return self


class IntegrationSettings(IntegrationSettingsBase):
    settings_key = "integrations"
    _agent_profile_excluded_ids: frozenset[str] = PrivateAttr(default=frozenset())
    webfetch_integration_id: UUID | None = None

    adapter_availability: dict[str, IntegrationAdapterAvailability] = Field(
        default_factory=dict
    )

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_availability(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        availability = dict(data.get("adapter_availability", {}))
        backend_adapters = {"searxng", "caldav", "bookstack", "mealie", "imap_smtp"}
        agent_adapters = {"openscad"}
        legacy_adapters = backend_adapters | agent_adapters
        if (
            "personal_integrations_enabled" in data
            or "allowed_personal_adapter_types" in data
        ):
            enabled = data.pop("personal_integrations_enabled", True)
            allowed = set(
                data.pop("allowed_personal_adapter_types", legacy_adapters)
            )
            for adapter_type in legacy_adapters:
                if enabled and adapter_type in allowed:
                    continue
                availability.setdefault(
                    adapter_type,
                    IntegrationAdapterAvailability.DISABLED
                    if adapter_type in agent_adapters
                    else IntegrationAdapterAvailability.INSTANCE_ONLY,
                )
        for adapter_type in data.pop("disabled_integration_adapter_types", set()):
            availability[adapter_type] = IntegrationAdapterAvailability.DISABLED
        data["adapter_availability"] = availability
        return data

    @model_validator(mode="after")
    def remove_default_availability(self) -> Self:
        self.adapter_availability = {
            adapter_type: availability
            for adapter_type, availability in self.adapter_availability.items()
            if availability != IntegrationAdapterAvailability.FULL
            or adapter_type == "mcp_streamable_http"
        }
        return self

    def availability_for(self, adapter_type: str) -> IntegrationAdapterAvailability:
        if adapter_type in {"jina", "searxng"}:
            return (
                IntegrationAdapterAvailability.DISABLED
                if self.adapter_availability.get(adapter_type) == IntegrationAdapterAvailability.DISABLED
                else IntegrationAdapterAvailability.INSTANCE_ONLY
            )
        return self.adapter_availability.get(
            adapter_type,
            IntegrationAdapterAvailability.INSTANCE_ONLY
            if adapter_type == "mcp_streamable_http"
            else IntegrationAdapterAvailability.FULL,
        )


class UserIntegrationSettings(IntegrationSettingsBase):
    settings_key = "integrations"
    settings_scope = SettingsScope.USER


class AgentRuntimeSettings(SettingsDocument):
    settings_key = "agent-runtime"

    request_timeout_seconds: int = Field(default=300, ge=10, le=3600)
    compaction_trigger_ratio: float = Field(default=0.8, ge=0.5, le=0.95)
    compaction_target_ratio: float = Field(default=0.6, ge=0.1, le=0.9)
    safety_reserve_tokens: int = Field(default=4_096, ge=0, le=131_072)
    protected_recent_turns: int = Field(default=2, ge=1, le=100)
    max_compaction_requests: int = Field(default=3, ge=1, le=20)
    historical_tool_output_tokens: int = Field(default=2_000, ge=0, le=100_000)

    @model_validator(mode="after")
    def validate_compaction_ratios(self) -> Self:
        if self.compaction_target_ratio >= self.compaction_trigger_ratio:
            raise ValueError("compaction_target_ratio must be less than compaction_trigger_ratio")
        return self


class MemorySettings(SettingsDocument):
    settings_key = "memory"
    settings_scope = SettingsScope.USER

    enabled: bool = True
    automatic_dreaming_enabled: bool = False
    dreaming_instructions: str = Field(default="", max_length=8_000)
    dreaming_interval_minutes: int = Field(default=60, ge=1, le=10_080)
    dreaming_idle_minutes: int = Field(default=15, ge=1, le=10_080)
    dreaming_max_batch_messages: int = Field(default=50, ge=2, le=500)
    dreaming_max_candidates: int = Field(default=10, ge=1, le=100)
    dreaming_update_policy: ToolExecutionPolicy = ToolExecutionPolicy.AUTO
    dreaming_remove_policy: ToolExecutionPolicy = ToolExecutionPolicy.ASK
    automatic_memory_snippet_limit: int = Field(default=2, ge=0, le=5)
    automatic_session_snippet_limit: int = Field(default=3, ge=0, le=5)
    recent_session_snippet_limit: int = Field(default=2, ge=0, le=5)
    automatic_context_max_tokens: int = Field(default=1_024, ge=128, le=2_048)


class UserProfileFact(BaseModel):
    model_config = ConfigDict(
        alias_generator=_camel_case,
        populate_by_name=True,
        extra="forbid",
    )

    both: str | None = Field(default=None, min_length=1, max_length=20_000)
    private_only: str | None = Field(default=None, min_length=1, max_length=20_000)
    public_only: str | None = Field(default=None, min_length=1, max_length=20_000)

    @field_validator("both", "private_only", "public_only", mode="before")
    @classmethod
    def normalize_content(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


class UserProfileSettings(SettingsDocument):
    settings_key = "user-profile"
    settings_scope = SettingsScope.USER
    model_config = ConfigDict(
        alias_generator=_camel_case,
        populate_by_name=True,
        extra="forbid",
    )

    full_name: UserProfileFact = Field(default_factory=UserProfileFact)
    preferred_name: UserProfileFact = Field(default_factory=UserProfileFact)
    form_of_address: UserProfileFact = Field(default_factory=UserProfileFact)
    pronouns: UserProfileFact = Field(default_factory=UserProfileFact)
    primary_language: UserProfileFact = Field(default_factory=UserProfileFact)
    primary_residence: UserProfileFact = Field(default_factory=UserProfileFact)


class AgentIdentitySettings(SettingsDocument):
    settings_key = "agent-identity"

    display_name: str = Field(default="MatoLoop", min_length=1, max_length=80)
    personal_personas_enabled: bool = True
    personas: list[PersonaProfile] = Field(default_factory=lambda: [PersonaProfile(id=DEFAULT_PERSONA_ID, name="Standard")])
    default_persona_id: UUID = DEFAULT_PERSONA_ID
    profile_personas: dict[UUID, UUID] = Field(default_factory=dict)

    _validate_catalog = field_validator("personas")(validate_persona_catalog)

    @model_validator(mode="after")
    def validate_default_persona(self) -> Self:
        ids = {persona.id for persona in self.personas}
        if self.default_persona_id not in ids or set(self.profile_personas.values()) - ids:
            raise ValueError("persona references must point to the instance catalog")
        return self

    @field_validator("display_name")
    @classmethod
    def validate_display_name(cls, value: str) -> str:
        return normalize_agent_display_name(value)

class UserAgentIdentitySettings(SettingsDocument):
    settings_key = "agent-identity"
    settings_scope = SettingsScope.USER

    display_name: str | None = Field(default=None, min_length=1, max_length=80)
    personas: list[PersonaProfile] = Field(default_factory=list)
    default_persona_id: UUID | None = None
    profile_personas: dict[UUID, UUID] = Field(default_factory=dict)

    _validate_catalog = field_validator("personas")(validate_persona_catalog)

    @field_validator("display_name")
    @classmethod
    def validate_display_name(cls, value: str | None) -> str | None:
        return normalize_agent_display_name(value) if value is not None else None

class EffectiveAgentIdentity(BaseModel):
    display_name: str
    persona: PersonaProfile
    personal_display_name_enabled: bool = True
    persona_editable: bool = False


class SkillReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    enabled: StrictBool = True


class SkillDefinition(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=1024)
    instructions: str = Field(max_length=128 * 1024)
    enabled: StrictBool = True
    owner_id: UUID | None = Field(default=None, exclude=True)
    editable: bool = Field(default=True, exclude=True)
    document: str = ""
    revision: str = ""
    resource_paths: list[str] = Field(default_factory=list)
    files_path: str | None = None
    computer_path: str | None = None
    computer_id: UUID | None = None

    @field_validator("name", mode="before")
    @classmethod
    def validate_name(cls, value: Any) -> Any:
        return normalize_skill_name(value) if isinstance(value, str) else value

    @field_validator("description", "instructions", mode="before")
    @classmethod
    def validate_text(cls, value: Any, info: Any) -> Any:
        if isinstance(value, str):
            return value.strip() if info.field_name == "instructions" else normalize_skill_text(value, info.field_name)
        return value


DEFAULT_SKILLS: dict[UUID, SkillDefinition] = {
    WEATHER_SKILL_ID: SkillDefinition(
        id=WEATHER_SKILL_ID,
        name="weather",
        description=(
            "Retrieve current weather conditions and forecasts for a location and time range. "
            "Use for requests about temperature, rain, snow, wind, or weather-dependent "
            "activities. Do not use for translation or general explanations of weather."
        ),
        instructions="""
Answer in the language of the user's request. Use the available web fetch tool to retrieve live
data from Open-Meteo. Do not use remembered weather data and do not invent unavailable values.

Resolve the requested place with
https://geocoding-api.open-meteo.com/v1/search?name=<place>&count=5&language=en&format=json.
Percent-encode the complete place value before inserting it into the URL. Use country or region
details from the request to select a result. If no location is available, or multiple results remain
plausible, ask one focused question instead of guessing.

Fetch weather from https://api.open-meteo.com/v1/forecast with the selected latitude and longitude,
timezone=auto, and only the fields needed from:
- current=temperature_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m,wind_direction_10m
- hourly=temperature_2m,apparent_temperature,precipitation_probability,precipitation,weather_code,wind_speed_10m,wind_direction_10m
- daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,precipitation_sum,wind_speed_10m_max
Request enough forecast_days for the requested period. Without a time period, return current
conditions. Resolve relative dates in the location's timezone. Use Celsius, km/h, and mm unless the
user requests other units. Never present data beyond the returned forecast range.

Translate WMO weather codes accurately: 0 clear; 1 mainly clear; 2 partly cloudy; 3 overcast; 45 or
48 fog; 51, 53, or 55 drizzle; 56 or 57 freezing drizzle; 61, 63, or 65 rain; 66 or 67 freezing
rain; 71, 73, or 75 snow; 77 snow grains; 80, 81, or 82 rain showers; 85 or 86 snow showers; 95
thunderstorm; 96 or 99 thunderstorm with hail.

Start with the resolved place and requested local period. Format weather values as a compact
Markdown table. For current conditions, use rows for condition, temperature / feels like,
precipitation, and wind. For several days, use columns for day, condition, minimum / maximum, and
rain probability. For a part of a day, show only relevant hourly rows. Add wind or precipitation
amount only when relevant to the request. If daily precipitation_probability_max is used, label it
as the maximum hourly probability. Mark missing returned values as unavailable; do not create an
empty table when the request fails. After the table, give at most one short practical conclusion.
Cite the fetched Open-Meteo source and state the local timestamp or period represented by the data.
Do not describe the data timestamp as the retrieval time.
""",
        enabled=False,
    )
}


def default_skills() -> list[SkillReference]:
    from mato_loop_backend.skill_packages import SkillFile, SkillPackage, skill_md

    return [
        SkillReference(id=skill.id, name=skill.name, enabled=skill.enabled,
                       revision=SkillPackage({"SKILL.md": SkillFile(skill_md(skill.name, skill.description, skill.instructions))}).revision)
        for skill in DEFAULT_SKILLS.values()
    ]


class InstanceSkillsSettings(SettingsDocument):
    settings_key = "skills"

    personal_skills_enabled: bool = True
    skills: list[SkillReference] = Field(default_factory=default_skills, max_length=40)

    @model_validator(mode="after")
    def add_missing_defaults(self) -> Self:
        existing = {skill.id for skill in self.skills}
        self.skills.extend(
            skill for skill in default_skills() if skill.id not in existing
        )
        if sum(skill.id not in DEFAULT_SKILLS for skill in self.skills) > 20:
            raise ValueError("at most 20 custom skills are allowed")
        return self


class SkillsSettings(SettingsDocument):
    settings_key = "skills"
    settings_scope = SettingsScope.USER

    skills: list[SkillReference] = Field(default_factory=list, max_length=40)

    @model_validator(mode="after")
    def validate_custom_limit(self) -> Self:
        if sum(skill.id not in DEFAULT_SKILLS for skill in self.skills) > 20:
            raise ValueError("at most 20 custom skills are allowed")
        return self


class UserPreferences(SettingsDocument):
    settings_key = "user-preferences"
    settings_scope = SettingsScope.USER

    dark_mode: bool = False
    session_drawer_open: bool = True
    default_execution_agent_id: UUID | None = None
    preferred_language: str | None = Field(
        default=None, min_length=2, max_length=35,
        pattern=r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$",
    )
