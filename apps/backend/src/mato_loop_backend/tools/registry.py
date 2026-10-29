from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from mato_loop_backend.integrations.contracts import ToolContribution
from mato_loop_backend.integrations.jina.adapter import (
    provider_available as webfetch_provider_available,
)
from mato_loop_backend.integrations.jina.adapter import provider_key as webfetch_provider_key
from mato_loop_backend.integrations.registry import (
    agent_profile_search_available,
    effective_integration_contributions,
    installed_integration_tools,
    integration_adapter,
)
from mato_loop_backend.permissions.policy import PermissionDecision, PermissionRequest
from mato_loop_backend.providers.base import ToolCall
from mato_loop_backend.settings import (
    IntegrationInstance,
    IntegrationSettings,
    ToolExecutionPolicy,
    ToolSettings,
)
from mato_loop_backend.settings.effective import filter_agent_profile_integrations
from mato_loop_backend.tools.actions import ToolAction
from mato_loop_backend.tools.context import ToolContext
from mato_loop_backend.tools.contracts import (
    ToolArgumentsEdited,
    ToolDefinition,
    ToolResult,
)
from mato_loop_backend.tools.display import TOOL_GROUPS

logger = logging.getLogger("mato_loop_backend.agent.tools")


PolicyResolver = Callable[..., ToolExecutionPolicy]


CapabilityResolver = Callable[..., set[str]]


def _model_revision(value: BaseModel) -> str:
    return json.dumps(
        value.binding_data() if isinstance(value, IntegrationInstance) else value.model_dump(mode="json"),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _provider_revision(value: str | None, settings: IntegrationSettings) -> str | None:
    if value is None or ":" not in value:
        return None
    integration_id = value.split(":", 1)[1]
    instance = next(
        (item for item in settings.instances if str(item.id) == integration_id), None
    )
    return _model_revision(instance) if instance is not None else None


def _tool_available(name: str, settings: ToolSettings) -> bool:
    if name == "memory_browse" and not settings.tool_availability.get("memory_retrieve", True):
        return False
    if name == "todo_write" and not (
        settings._agent_profile and settings._agent_profile.todo_enabled
    ):
        return False
    if name.startswith("browser_"):
        if not settings.tool_availability.get("browser", True):
            return False
        ceiling = settings._delegation_ceiling
        if ceiling is not None and name not in ceiling["toolNames"]:
            return False
    return settings.tool_availability.get(name, True)


def _actions_enabled(
    definition: ToolDefinition,
    tool_settings: ToolSettings,
    policy_resolver: PolicyResolver,
    *,
    integration_id: UUID | None = None,
) -> bool:
    if not _tool_available(definition.name, tool_settings):
        return False
    enabled = {
        action.capability
        for action in definition.actions
        if policy_resolver(
            tool_settings,
            action.capability,
            action.default_policy(tool_settings),
            integration_id=integration_id,
            resource_key=definition.policy_resource_key,
            include_scoped=True,
        )
        != ToolExecutionPolicy.DISABLED
    }
    return definition.actions_satisfied(enabled)


@dataclass(frozen=True, slots=True)
class BoundTool:
    definition: ToolDefinition
    source_type: str = "builtin"
    source_id: str | None = None
    source_revision: str | None = None
    integration_revisions: tuple[tuple[str, str], ...] = ()
    selected_options: dict[str, str | None] = field(default_factory=dict)
    default_arguments: dict[str, str] = field(default_factory=dict)

    def schema(self) -> dict[str, Any]:
        schema = self.definition.schema()
        parameters = schema["parameters"]
        instance_argument = self.definition.instance_argument
        if instance_argument is not None and self.integration_revisions:
            parameters["properties"][instance_argument]["enum"] = [
                integration_id for integration_id, _revision in self.integration_revisions
            ]
        if not self.default_arguments:
            return schema
        parameters["required"] = [
            name
            for name in parameters.get("required", [])
            if name not in self.default_arguments
        ]
        return schema


def _tool_prompt_fragments(tools: dict[str, BoundTool]) -> tuple[str, ...]:
    fragments = [
        fragment
        for tool in tools.values()
        for fragment in tool.definition.prompt_fragments
    ]
    if any(
        tools[name].default_arguments
        for name in ("calendar_query_events", "calendar_create_event")
        if name in tools
    ):
        fragments.append(
            "A default calendar is configured. For it, call calendar_query_events or "
            "calendar_create_event directly and omit both integration_id and calendar_key. "
            "For another calendar, include both."
        )
    if any(
        tools[name].default_arguments
        for name in ("tasks_query", "tasks_create", "tasks_create_many")
        if name in tools
    ):
        fragments.append(
            "A default task list is configured. For it, call tasks_query, tasks_create, or "
            "tasks_create_many directly and omit both integration_id and task_list_key. For "
            "another task list, include both."
        )
    if {
        "calendar_create_event",
        "calendar_update_event",
        "calendar_delete_event",
    } & tools.keys():
        fragments.append(
            "For mutations, copy eventToken, integrationId, and calendarKey from a returned event. A "
            "mutation succeeds only with confirming event or deleted=true. Use localTiming exactly; "
            "otherwise convert timestamps to the prompt timezone."
        )
    if {"tasks_update", "tasks_set_completed", "tasks_delete"} & tools.keys():
        fragments.append(
            "For task mutations, copy taskToken, integrationId, and taskListKey from one returned "
            "task. Success requires a confirming task or deleted=true."
        )
    return tuple(fragments)


@dataclass(frozen=True, slots=True)
class EffectiveToolSet:
    registry: ToolRegistry
    tools: dict[str, BoundTool]
    prompt_fragments: tuple[str, ...] = ()

    def schemas(self) -> list[dict[str, Any]]:
        return [tool.schema() for tool in self.tools.values()]

    def only(self, *names: str) -> EffectiveToolSet:
        selected = {name: self.tools[name] for name in names if name in self.tools}
        return EffectiveToolSet(self.registry, selected, _tool_prompt_fragments(selected))

    def without(self, *names: str) -> EffectiveToolSet:
        excluded = set(names)
        selected = {
            name: tool for name, tool in self.tools.items() if name not in excluded
        }
        return EffectiveToolSet(self.registry, selected, _tool_prompt_fragments(selected))

    async def execute(self, call: ToolCall, context: ToolContext) -> ToolResult:
        tool = self.tools.get(call.name)
        if tool is None:
            return ToolResult.error("tool.unavailable")
        edited = False
        original_arguments = dict(call.arguments)
        while True:
            try:
                result = await self.registry._execute_definition(call, context, tool)
                if edited:
                    result.metadata["userEdited"] = True
                    try:
                        content = json.loads(result.content)
                    except (TypeError, ValueError):
                        content = None
                    if isinstance(content, dict) and "requestedArguments" in content:
                        content.update({
                            "requestedArguments": original_arguments,
                            "executedArguments": dict(call.arguments),
                            "userEdited": True,
                        })
                        result.content = json.dumps(content, ensure_ascii=False)
                return result
            except ToolArgumentsEdited as exc:
                allowed = {field.path for field in tool.definition.approval_edit_fields}
                if not exc.edits or any(path not in allowed for path, _value in exc.edits):
                    return ToolResult.error("approval.requestChanged")
                arguments = deepcopy(call.arguments)
                for path, value in exc.edits:
                    target = arguments
                    for segment in path[:-1]:
                        child = target.get(segment)
                        if not isinstance(child, dict):
                            return ToolResult.error("tool.invalidArguments", field=segment)
                        target = child
                    target[path[-1]] = value
                call.arguments.clear()
                call.arguments.update(arguments)
                context.bound_options.clear()
                context.bound_resource = None
                context.approval_resolution.clear()
                context.approval_resolution["argument_edit_approved"] = json.dumps(
                    {
                        "arguments": call.arguments,
                        "permission": exc.approval_binding,
                    },
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                if context.arguments_edited_handler is not None:
                    await context.arguments_edited_handler(call.arguments)
                edited = True


class ToolRegistry:
    def __init__(
        self,
        definitions: list[ToolDefinition],
        operation_definitions: list[ToolDefinition] | None = None,
        *,
        group_actions: dict[str, tuple[ToolAction, ...]] | None = None,
        contributions_resolver: Callable[..., Any] = effective_integration_contributions,
        policy_resolver: PolicyResolver | None = None,
        capability_resolver: CapabilityResolver | None = None,
        allow_handler: Callable[..., Awaitable[bool]] | None = None,
    ) -> None:
        self._definitions: dict[str, ToolDefinition] = {}
        for definition in definitions:
            if definition.name in self._definitions:
                raise ValueError(f"duplicate tool: {definition.name}")
            self._definitions[definition.name] = definition
        self._operation_definitions = {
            definition.name: definition for definition in operation_definitions or []
        }
        if self._definitions.keys() & self._operation_definitions.keys():
            raise ValueError("duplicate operation tool")
        self._group_actions = group_actions or {}
        self._contributions_resolver = contributions_resolver
        self._policy_resolver = policy_resolver
        self._capability_resolver = capability_resolver
        self._allow_handler = allow_handler
        self.actions_by_capability = {action.capability: action for action in self.policy_actions()}

    async def allow(
        self, context: ToolContext, request: PermissionRequest
    ) -> bool:
        if self._allow_handler is not None:
            return await self._allow_handler(context, request)
        return await context.ask(request) == PermissionDecision.ALLOW

    def _actions_enabled(
        self,
        definition: ToolDefinition,
        tool_settings: ToolSettings,
        source_id: str | None = None,
    ) -> bool:
        if self._policy_resolver is None:
            raise RuntimeError("tool registry policy resolver is not configured")
        return _actions_enabled(
            definition,
            tool_settings,
            self._policy_resolver,
            integration_id=(
                UUID(source_id)
                if source_id is not None and definition.policy_resource_key is not None
                else None
            ),
        )

    def _combined_definitions(
        self, integration_settings: IntegrationSettings
    ) -> tuple[
        dict[str, ToolDefinition],
        dict[
            str,
            tuple[str, str | None, str | None, tuple[tuple[str, str], ...]],
        ],
        tuple[str, ...],
    ]:
        contributions = self._contributions_resolver(integration_settings)
        definitions = dict(self._definitions)
        sources: dict[
            str,
            tuple[str, str | None, str | None, tuple[tuple[str, str], ...]],
        ] = {name: ("builtin", None, None, ()) for name in definitions}
        revisions = {
            str(instance.id): _model_revision(instance)
            for instance in integration_settings.instances
        }
        for contribution in contributions.tools:
            if contribution.name in definitions:
                raise ValueError(f"duplicate contributed tool: {contribution.name}")
            definitions[contribution.name] = self._contributed_definition(contribution)
            sources[contribution.name] = (
                "integration",
                contribution.integration_id,
                revisions.get(contribution.integration_id),
                tuple(
                    (instance_id, revision)
                    for instance_id, revision in revisions.items()
                    if instance_id in contribution.integration_ids
                ),
            )
        attached_fragments = {
            fragment
            for contribution in contributions.tools
            for fragment in contribution.prompt_fragments
        }
        return definitions, sources, tuple(
            fragment
            for fragment in contributions.prompt_fragments
            if fragment not in attached_fragments
        )

    @staticmethod
    def _contributed_definition(contribution: ToolContribution) -> ToolDefinition:
        return ToolDefinition(
            name=contribution.name,
            description=contribution.description,
            arguments_model=contribution.arguments_model,
            handler=contribution.handler,
            arguments_schema=contribution.arguments_schema,
            tool_type="computer" if contribution.execution_operation else "backend",
            requires_search=contribution.requires_search,
            requires_webfetch=contribution.requires_webfetch,
            requires_memory=contribution.requires_memory,
            requires_memory_write=contribution.requires_memory_write,
            required_capability=contribution.required_capability,
            group=contribution.group,
            label=contribution.label,
            icon=contribution.icon,
            actions=contribution.actions,
            alternative_action_groups=contribution.alternative_action_groups,
            instance_argument=contribution.instance_argument,
            resource_argument=contribution.resource_argument,
            policy_resource_key=contribution.policy_resource_key,
            releases_private_data=contribution.releases_private_data,
            authorization_factory=contribution.authorization_factory,
            prompt_fragments=contribution.prompt_fragments,
            approval_edit_fields=contribution.approval_edit_fields,
            permission_reason_instruction=contribution.permission_reason_instruction,
            snapshot_policy=contribution.snapshot_policy,
        )

    def inactive_capability_groups(
        self,
        integration_settings: IntegrationSettings,
        offered_names: set[str] | frozenset[str],
        *,
        webfetch_enabled: bool,
        web_search_enabled: bool,
        memory_enabled: bool,
        memory_write_enabled: bool,
        tool_capabilities: set[str] | frozenset[str],
        tool_settings: ToolSettings,
        execution_operations: set[str] | frozenset[str] = frozenset(),
    ) -> tuple[str, ...]:
        active_definitions = self._combined_definitions(integration_settings)[0]
        installed_definitions = dict(self._definitions)
        installed_definitions.update(active_definitions)
        installed_definitions.update(
            {
                name: definition
                for name, definition in self._operation_definitions.items()
                if definition.required_operation in execution_operations
            }
        )
        for contribution in installed_integration_tools(integration_settings):
            installed_definitions.setdefault(
                contribution.name, self._contributed_definition(contribution)
            )

        grouped: dict[str, list[str]] = {
            "integrations": [],
            "media": [],
            "web": [],
            "memory": [],
            "settings": [],
            "temporary": [],
        }
        for name, definition in installed_definitions.items():
            if name in {"memory_retrieve", "session_search", "search_tools"}:
                continue
            if name in offered_names:
                continue
            if name == "todo_write" and not _tool_available(name, tool_settings):
                continue
            if definition.group == "integrations" and name not in active_definitions:
                reason = "integrations"
            elif definition.requires_search and not web_search_enabled:
                reason = "web"
            elif definition.requires_webfetch and not webfetch_enabled:
                reason = "web"
            elif definition.requires_memory and not memory_enabled:
                reason = "memory"
            elif definition.requires_memory_write and not memory_write_enabled:
                reason = "memory"
            elif (
                definition.required_capability is not None
                and definition.required_capability not in tool_capabilities
            ):
                if definition.required_capability == "project.coding":
                    continue
                reason = (
                    "media"
                    if definition.required_capability.startswith("media.")
                    else "settings"
                )
            elif (
                definition.required_operation is not None
                and definition.required_operation not in execution_operations
            ):
                reason = "temporary"
            elif not self._actions_enabled(definition, tool_settings):
                reason = (
                    "integrations"
                    if definition.group == "integrations"
                    else "settings"
                )
            else:
                reason = "temporary"
            grouped[reason].append(name)

        templates = {
            "integrations": (
                "Integration tools [{names}]: configure under Integrationen or the agent profile."
            ),
            "media": (
                'Media tools [{names}]: enable them under "Einstellungen > Tools" and configure '
                'a capable model under "Einstellungen > Standardmodelle".'
            ),
            "web": (
                'Web tools [{names}]: enable web access for the session or under '
                '"Einstellungen > Tools".'
            ),
            "memory": (
                'Memory tools [{names}]: enable Memory for the session and its model.'
            ),
            "settings": (
                "Optional tools [{names}]: configure under Tools or integration settings."
            ),
            "temporary": (
                "Temporarily unavailable tools [{names}]: finish the current required workflow first."
            ),
        }
        return tuple(
            templates[reason].format(names=", ".join(names))
            for reason, names in grouped.items()
            if names
        )

    def available(
        self,
        integration_settings: IntegrationSettings,
        webfetch_enabled: bool = False,
        web_search_enabled: bool = True,
        memory_enabled: bool = False,
        memory_write_enabled: bool = False,
        tool_capabilities: set[str] | frozenset[str] = frozenset(),
        execution_operations: set[str] | frozenset[str] = frozenset(),
    ) -> list[ToolDefinition]:
        return [
            definition
            for definition in (
                *self._definitions.values(),
                *self._operation_definitions.values(),
            )
            if self._is_available(
                definition,
                webfetch_enabled=webfetch_enabled,
                web_search_enabled=web_search_enabled,
                memory_enabled=memory_enabled,
                memory_write_enabled=memory_write_enabled,
                tool_capabilities=tool_capabilities,
                execution_operations=execution_operations,
            )
        ]

    @staticmethod
    def _is_available(
        definition: ToolDefinition,
        *,
        webfetch_enabled: bool,
        web_search_enabled: bool,
        memory_enabled: bool,
        memory_write_enabled: bool,
        tool_capabilities: set[str] | frozenset[str],
        execution_operations: set[str] | frozenset[str] = frozenset(),
    ) -> bool:
        return (
            (not definition.requires_search or web_search_enabled)
            and (not definition.requires_webfetch or webfetch_enabled)
            and (not definition.requires_memory or memory_enabled)
            and (not definition.requires_memory_write or memory_write_enabled)
            and (
                definition.required_capability is None
                or definition.required_capability in tool_capabilities
            )
            and (
                definition.required_operation is None
                or definition.required_operation in execution_operations
            )
        )

    def schemas(
        self,
        integration_settings: IntegrationSettings,
        webfetch_enabled: bool = False,
        web_search_enabled: bool = True,
        memory_enabled: bool = False,
        memory_write_enabled: bool = False,
        tool_capabilities: set[str] | frozenset[str] = frozenset(),
        execution_operations: set[str] | frozenset[str] = frozenset(),
    ) -> list[dict[str, Any]]:
        return self.effective(
            integration_settings,
            webfetch_enabled,
            web_search_enabled,
            memory_enabled,
            memory_write_enabled,
            tool_capabilities,
            execution_operations=execution_operations,
        ).schemas()

    def effective(
        self,
        integration_settings: IntegrationSettings,
        webfetch_enabled: bool = False,
        web_search_enabled: bool = True,
        memory_enabled: bool = False,
        memory_write_enabled: bool = False,
        tool_capabilities: set[str] | frozenset[str] = frozenset(),
        tool_settings: ToolSettings | None = None,
        execution_operations: set[str] | frozenset[str] = frozenset(),
        coding_integration_id: UUID | None = None,
    ) -> EffectiveToolSet:
        from mato_loop_backend.agent.subagents import integration_ceiling_ids

        integration_settings = filter_agent_profile_integrations(
            integration_settings,
            tool_settings._agent_profile if tool_settings is not None else None,
            allowed_ids=(
                () if tool_settings._agent_profile_unavailable
                else integration_ceiling_ids(tool_settings._delegation_ceiling)
            ) if tool_settings is not None else None,
        )
        coding_instances = [
            item for item in integration_settings.instances
            if item.enabled and item.adapter_type == "opencode"
            and item.integration_type == "coding"
            and (coding_integration_id is None or item.id == coding_integration_id)
        ]
        definitions, sources, prompt_fragments = self._combined_definitions(
            integration_settings
        )
        if "webfetch" in definitions and webfetch_provider_key(integration_settings) != "builtin":
            from mato_loop_backend.tools.arguments import JinaWebFetchArguments

            definitions["webfetch"] = replace(
                definitions["webfetch"], arguments_model=JinaWebFetchArguments,
                description=(
                    "Read pages and documents through Jina Reader as extracted text/Markdown or sanitized HTML, "
                    "not guaranteed verbatim structured data or validated image attachments. "
                    "Use download_file for original files. pdf_pages uses direct PDF rendering; omit Reader options. "
                    "Default to auto unless the user requests a mode. If JavaScript content is incomplete, "
                    "retry at most once with engine=browser and no_cache=true. "
                    "Use selectors only for known expected elements. "
                    "Do not automatically retry HTTP 401/403, login, or CAPTCHA blocks. No audio/video/captions."
                ),
            )
        if "customize_matoloop" in definitions:
            # Keep legacy direct calls and saved policies, but offer one settings tool to models.
            definitions.pop("update_agent_identity", None)
            definitions.pop("update_user_profile", None)
            definitions.pop("variable_write", None)
        if "context_search" in definitions:
            definitions.pop("memory_retrieve", None)
            definitions.pop("session_search", None)
        for name, definition in self._operation_definitions.items():
            if definition.required_operation in execution_operations:
                definitions[name] = definition
                sources[name] = ("builtin", None, None, ())
        available_names = {
            item.name
            for item in definitions.values()
            if self._is_available(
                item,
                webfetch_enabled=webfetch_enabled,
                web_search_enabled=web_search_enabled,
                memory_enabled=memory_enabled,
                memory_write_enabled=memory_write_enabled,
                tool_capabilities=tool_capabilities,
                execution_operations=execution_operations,
            )
            and (
                tool_settings is None
                or self._actions_enabled(item, tool_settings, sources[item.name][1])
            )
            and (item.required_capability != "project.coding" or bool(coding_instances))
            and (
                item.name != "web_search" or tool_settings is None
                or agent_profile_search_available(integration_settings, tool_settings)
            )
            and (
                item.name != "webfetch" or webfetch_provider_available(integration_settings)
            )
        }
        if "context_search" in available_names and tool_settings is not None:
            from mato_loop_backend.tools.context_search import source_available

            if not any(source_available(tool_settings, source, memory_enabled=memory_enabled)
                       for source in ("memories", "sessions")):
                available_names.remove("context_search")
        search_provider = (
            tool_settings.web_search.primary_provider
            if tool_settings is not None
            else None
        )

        def default_arguments(name: str) -> dict[str, str]:
            integration_revisions = sources[name][3]
            if name in {"calendar_query_events", "calendar_create_event"}:
                default_calendar = (
                    tool_settings.default_calendar if tool_settings else None
                )
                if (
                    default_calendar is not None
                    and str(default_calendar.integration_id) in dict(integration_revisions)
                ):
                    return {
                        "integration_id": str(default_calendar.integration_id),
                        "calendar_key": default_calendar.calendar_key,
                    }
            if name in {"tasks_query", "tasks_create", "tasks_create_many"}:
                default_task_list = (
                    tool_settings.default_task_list if tool_settings else None
                )
                if (
                    default_task_list is not None
                    and str(default_task_list.integration_id)
                    in dict(integration_revisions)
                ):
                    return {
                        "integration_id": str(default_task_list.integration_id),
                        "task_list_key": default_task_list.task_list_key,
                    }
            return {}

        tools = {
            name: BoundTool(
                definition,
                *sources[name],
                selected_options=(
                    {
                        "provider": search_provider,
                        "provider_revision": _provider_revision(
                            search_provider, integration_settings
                        ),
                    }
                    if name == "web_search" and search_provider is not None
                    else {
                        "provider": webfetch_provider_key(integration_settings),
                        "provider_revision": _provider_revision(webfetch_provider_key(integration_settings), integration_settings),
                    } if name == "webfetch" else {}
                ),
                default_arguments=default_arguments(name),
            )
            for name, definition in definitions.items()
            if name in available_names
        }
        return EffectiveToolSet(
            self,
            tools,
            (*prompt_fragments, *_tool_prompt_fragments(tools)),
        )

    def catalog(
        self, tool_settings: ToolSettings, integration_settings: IntegrationSettings
    ) -> list[dict[str, Any]]:
        definitions, sources, _prompt_fragments = self._combined_definitions(
            integration_settings
        )
        definitions.update(self._operation_definitions)
        sources.update(
            {name: ("builtin", None, None, ()) for name in self._operation_definitions}
        )
        def action_payload(action: ToolAction) -> dict[str, Any]:
            return {
                "capability": action.capability,
                "label": action.label,
                "policy": self._policy_resolver(
                    tool_settings,
                    action.capability,
                    action.default_policy(tool_settings),
                ).value,
                "configurable": action.configurable,
                "allowedPolicies": [policy.value for policy in action.allowed_policies],
                "available": True,
            }

        tools_by_group: dict[str, list[dict[str, Any]]] = {}
        for definition in definitions.values():
            source_type, source_id, _source_revision, _integration_revisions = sources[
                definition.name
            ]
            actions = [action_payload(action) for action in definition.actions]
            tools_by_group.setdefault(definition.group, []).append(
                {
                    "name": definition.name,
                    "label": definition.label or definition.name,
                    "icon": definition.icon,
                    "toolType": definition.tool_type,
                    "source": {
                        "type": source_type,
                        "id": source_id,
                        "ids": [
                            integration_id
                            for integration_id, _revision in _integration_revisions
                        ],
                    },
                    "actions": actions,
                    "available": _tool_available(definition.name, tool_settings),
                }
            )
        group_metadata = {item["id"]: item for item in TOOL_GROUPS}
        group_ids = [item["id"] for item in TOOL_GROUPS]
        group_ids.extend(group for group in tools_by_group if group not in group_metadata)
        return [
            {
                **group_metadata.get(
                    group_id,
                    {"id": group_id, "label": group_id, "icon": "extension"},
                ),
                "actions": [
                    action_payload(action)
                    for action in self._group_actions.get(group_id, ())
                ],
                "tools": tools_by_group.get(group_id, []),
            }
            for group_id in group_ids
            if tools_by_group.get(group_id) or self._group_actions.get(group_id)
        ]

    def policy_actions(
        self, integration_settings: IntegrationSettings | None = None
    ) -> tuple[ToolAction, ...]:
        # Policy metadata may include the same static tools on different computers.
        definitions = [*self._definitions.values(), *self._operation_definitions.values()]
        if integration_settings is not None:
            definitions.extend(
                self._contributed_definition(contribution)
                for contribution in self._contributions_resolver(integration_settings).tools
            )
        actions = (
            *(action for definition in definitions for action in definition.actions),
            *(action for actions in self._group_actions.values() for action in actions),
        )
        categories = {}
        defaults = {}
        for action in actions:
            previous = categories.setdefault(action.capability, action.category)
            if previous != action.category:
                raise ValueError(f"conflicting categories for capability: {action.capability}")
            if defaults.setdefault(action.capability, action.default) != action.default:
                raise ValueError(f"conflicting defaults for capability: {action.capability}")
        return actions

    def integration_resource_actions(
        self, instance: IntegrationInstance
    ) -> tuple[ToolAction, ...]:
        adapter = integration_adapter(instance.integration_type, instance.adapter_type)
        if adapter is None:
            return ()
        try:
            contributions = adapter.contributions(instance)
        except ValueError:
            return ()
        executable: dict[str, set[ToolAction]] = {}
        for contribution in (*contributions.tools, *adapter.shared_tools):
            if contribution.authorization_factory is None:
                continue
            for action in contribution.actions:
                executable.setdefault(action.capability, set()).add(action)
        if any(
            executable.get(action.capability) != {action}
            for action in adapter.resource_actions
        ):
            return ()
        return adapter.resource_actions

    async def execute(self, call: ToolCall, context: ToolContext) -> ToolResult:
        definition = self._definitions.get(call.name)
        if definition is None:
            return ToolResult.error("tool.unknown")
        return await self._execute_definition(call, context, BoundTool(definition))

    async def _execute_definition(
        self, call: ToolCall, context: ToolContext, tool: BoundTool
    ) -> ToolResult:
        from mato_loop_backend.tools.executor import execute_definition

        return await execute_definition(self, call, context, tool)
