from __future__ import annotations

from dataclasses import replace
from uuid import UUID

from mato_loop_backend.integrations.arcane.adapter import ARCANE_ADAPTER
from mato_loop_backend.integrations.bookstack.adapter import BOOKSTACK_ADAPTER
from mato_loop_backend.integrations.browser.adapter import BROWSER_ADAPTER
from mato_loop_backend.integrations.caldav.adapter import CALDAV_ADAPTER
from mato_loop_backend.integrations.contracts import (
    IntegrationAdapterDefinition,
    IntegrationContributions,
    ToolContribution,
)
from mato_loop_backend.integrations.jina.adapter import JINA_ADAPTER
from mato_loop_backend.integrations.mail.adapter import MAIL_ADAPTER
from mato_loop_backend.integrations.mcp.adapter import MCP_ADAPTER
from mato_loop_backend.integrations.mealie.adapter import MEALIE_ADAPTER
from mato_loop_backend.integrations.opencode.adapter import OPENCODE_ADAPTER
from mato_loop_backend.integrations.openscad.adapter import OPENSCAD_ADAPTER
from mato_loop_backend.integrations.searxng.adapter import SEARXNG_ADAPTER
from mato_loop_backend.integrations.sonobin.adapter import SONOBIN_ADAPTER
from mato_loop_backend.integrations.tbs_desk.adapter import TBS_DESK_ADAPTER
from mato_loop_backend.settings.models import (
    IntegrationAdapterAvailability,
    IntegrationInstance,
    IntegrationSettings,
    ToolSettings,
)
from mato_loop_backend.tools.actions import ToolAction


def agent_profile_search_available(
    settings: IntegrationSettings, tools: ToolSettings
) -> bool:
    from mato_loop_backend.integrations.search import search_provider_options

    available = {item["value"] for item in search_provider_options(settings)}
    return tools.web_search.primary_provider in available


_ADAPTERS = {
    ("webfetch", "jina"): JINA_ADAPTER,
    ("search", "searxng"): SEARXNG_ADAPTER,
    ("calendar", "caldav"): CALDAV_ADAPTER,
    ("mail", "imap_smtp"): MAIL_ADAPTER,
    ("knowledge", "bookstack"): BOOKSTACK_ADAPTER,
    ("recipes", "mealie"): MEALIE_ADAPTER,
    ("infrastructure", "arcane"): ARCANE_ADAPTER,
    ("openscad", "openscad"): OPENSCAD_ADAPTER,
    ("coding", "opencode"): OPENCODE_ADAPTER,
    ("browser", "browser"): BROWSER_ADAPTER,
    ("mcp", "mcp_streamable_http"): MCP_ADAPTER,
    ("music", "sonobin"): SONOBIN_ADAPTER,
    ("support", "tbs-desk"): TBS_DESK_ADAPTER,
}


def integration_adapter(
    integration_type: str, adapter_type: str
) -> IntegrationAdapterDefinition | None:
    return _ADAPTERS.get((integration_type, adapter_type))


def integration_adapters() -> tuple[IntegrationAdapterDefinition, ...]:
    return tuple(
        _ADAPTERS[key] for key in sorted(_ADAPTERS) if _ADAPTERS[key].catalog_visible
    )


def integration_action(capability: str) -> ToolAction | None:
    for adapter in _ADAPTERS.values():
        actions = (*adapter.resource_actions, *(action for tool in (*adapter.shared_tools, *adapter.execution_tools) for action in tool.actions))
        for action in actions:
            if action.capability == capability:
                return action
    return None


def integration_adapter_type_registered(adapter_type: str) -> bool:
    return any(adapter.adapter_type == adapter_type for adapter in _ADAPTERS.values())


def register_integration_adapter(adapter: IntegrationAdapterDefinition) -> None:
    key = (adapter.integration_type, adapter.adapter_type)
    if key in _ADAPTERS:
        raise ValueError("duplicate integration adapter")
    _ADAPTERS[key] = adapter
    if adapter.execution_location == "agent":
        from mato_loop_backend.execution import manager as execution_manager

        execution_manager.SUPPORTED_OPERATIONS |= frozenset(
            tool.execution_operation
            for tool in adapter.execution_tools
            if tool.execution_operation is not None
        )


def installed_integration_tools(
    settings: IntegrationSettings | None = None,
) -> tuple[ToolContribution, ...]:
    tools: dict[str, ToolContribution] = {}
    for adapter in _ADAPTERS.values():
        if settings is not None and settings.availability_for(
            adapter.adapter_type
        ) == IntegrationAdapterAvailability.DISABLED:
            continue
        for tool in adapter.shared_tools:
            tools.setdefault(tool.name, tool)
    return tuple(tools.values())


def validate_integration(instance: IntegrationInstance) -> None:
    adapter = integration_adapter(instance.integration_type, instance.adapter_type)
    if adapter is None:
        raise ValueError("unsupported integration adapter")
    if (adapter.execution_location == "agent") != (
        instance.execution_agent_id is not None
    ):
        raise ValueError("integration execution location does not match its agent binding")
    adapter.config_model.model_validate(instance.config)


def effective_execution_integration_settings(
    settings: IntegrationSettings,
    execution_agent_id: UUID | None,
    execution_operations: set[str] | frozenset[str],
) -> IntegrationSettings:
    effective = settings.model_copy(deep=True)
    for instance in effective.instances:
        if (
            effective.availability_for(instance.adapter_type)
            == IntegrationAdapterAvailability.DISABLED
        ):
            instance.enabled = False
            continue
        adapter = integration_adapter(instance.integration_type, instance.adapter_type)
        if adapter is None:
            continue
        if adapter.execution_location == "backend":
            if instance.execution_agent_id is not None:
                instance.enabled = False
            continue
        if instance.execution_agent_id != execution_agent_id:
            instance.enabled = False
        instance._execution_operations = frozenset(execution_operations)
    return effective


def effective_integration_contributions(
    settings: IntegrationSettings,
) -> IntegrationContributions:
    tools = []
    options = []
    prompt_fragments = []
    agent_profiles = []
    shared_instances: dict[tuple[str, str], list[IntegrationInstance]] = {}
    for instance in settings.instances:
        if (
            not instance.enabled
            or settings.availability_for(instance.adapter_type)
            == IntegrationAdapterAvailability.DISABLED
        ):
            continue
        adapter = integration_adapter(instance.integration_type, instance.adapter_type)
        if adapter is None:
            continue
        try:
            contribution = adapter.contributions(instance)
        except ValueError:
            continue
        shared_instances.setdefault(
            (instance.integration_type, instance.adapter_type), []
        ).append(instance)
        tools.extend(
            replace(
                tool,
                integration_id=str(instance.id),
                prompt_fragments=(
                    *tool.prompt_fragments,
                    *contribution.prompt_fragments,
                ),
            )
            for tool in contribution.tools
        )
        options.extend(contribution.tool_options)
        prompt_fragments.extend(contribution.prompt_fragments)
        agent_profiles.extend(
            profile.model_copy(update={"integration_id": instance.id})
            for profile in contribution.agent_profiles
        )
    for adapter_key, instances in shared_instances.items():
        adapter = _ADAPTERS[adapter_key]
        integration_ids = tuple(str(instance.id) for instance in instances)
        tools.extend(
            replace(tool, integration_ids=integration_ids)
            for tool in adapter.shared_tools
        )
    duplicate_profile_names = {
        profile.name for profile in agent_profiles
        if sum(item.name == profile.name for item in agent_profiles) > 1
    }
    names = {str(item.id): item.name for item in settings.instances}
    duplicate_account_names = {
        name for name in names.values() if sum(item == name for item in names.values()) > 1
    }
    return IntegrationContributions(
        tools=tuple(tools),
        tool_options=tuple(options),
        prompt_fragments=tuple(prompt_fragments),
        agent_profiles=tuple(
            profile.model_copy(update={"name": (
                f"{profile.name} ({names[str(profile.integration_id)]}, "
                f"{str(profile.integration_id)[:8]})"
                if names[str(profile.integration_id)] in duplicate_account_names
                else f"{profile.name} ({names[str(profile.integration_id)]})"
            )}) if profile.name in duplicate_profile_names else profile
            for profile in agent_profiles
        ),
    )
