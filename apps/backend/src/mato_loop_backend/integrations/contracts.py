from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from pydantic import BaseModel

from mato_loop_backend.settings.models import AgentProfile, IntegrationInstance
from mato_loop_backend.tools.actions import ApprovalEditField, ToolAction
from mato_loop_backend.tools.snapshots import ToolSnapshotPolicy

ToolHandler = Callable[[Any, Any], Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class IntegrationResource:
    key: str
    label: str
    hard_unavailable_capabilities: frozenset[str] = frozenset()
    binding: object | None = field(default=None, repr=False, compare=False)
    applicable_capabilities: frozenset[str] | None = None
    group_key: str | None = None
    tool_name: str | None = None
    description: str = ""
    hints: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "hard_unavailable_capabilities",
            frozenset(self.hard_unavailable_capabilities),
        )
        if self.applicable_capabilities is not None:
            object.__setattr__(
                self,
                "applicable_capabilities",
                frozenset(self.applicable_capabilities),
            )


ResourceLoader = Callable[
    [IntegrationInstance, dict[str, Any] | None],
    Awaitable[tuple[IntegrationResource, ...]],
]


@dataclass(frozen=True, slots=True)
class ToolContribution:
    name: str
    description: str
    arguments_model: type[BaseModel] | None
    handler: ToolHandler | None
    requires_search: bool = False
    requires_webfetch: bool = False
    requires_memory: bool = False
    requires_memory_write: bool = False
    required_capability: str | None = None
    group: str = "integrations"
    label: str | None = None
    icon: str = "extension"
    actions: tuple[ToolAction, ...] = ()
    alternative_action_groups: tuple[frozenset[str], ...] = ()
    integration_id: str | None = None
    integration_ids: tuple[str, ...] = ()
    instance_argument: str | None = None
    resource_argument: str | None = None
    policy_resource_key: str | None = None
    releases_private_data: bool = False
    authorization_factory: Callable[[Any, Any], tuple[Any, ...] | Awaitable[tuple[Any, ...]]] | None = None
    execution_operation: str | None = None
    execution_config_arguments: tuple[str, ...] = ()
    required_execution_operations: tuple[str, ...] = ()
    result_model: type[BaseModel] | None = None
    prompt_fragments: tuple[str, ...] = ()
    arguments_schema: dict[str, Any] | None = None
    approval_edit_fields: tuple[ApprovalEditField, ...] = ()
    permission_reason_instruction: str | None = None
    snapshot_policy: ToolSnapshotPolicy | None = None


@dataclass(frozen=True, slots=True)
class ToolOptionContribution:
    tool_name: str
    option_name: str
    value: str
    label: str
    integration_id: str | None = None


@dataclass(frozen=True, slots=True)
class IntegrationContributions:
    tools: tuple[ToolContribution, ...] = ()
    tool_options: tuple[ToolOptionContribution, ...] = ()
    prompt_fragments: tuple[str, ...] = ()
    agent_profiles: tuple[AgentProfile, ...] = ()


@dataclass(frozen=True, slots=True)
class IntegrationAdapterDefinition:
    integration_type: str
    adapter_type: str
    config_model: type[BaseModel]
    contribution_factory: Callable[[IntegrationInstance], IntegrationContributions] = field(
        default=lambda _instance: IntegrationContributions()
    )
    resource_actions: tuple[ToolAction, ...] = ()
    resource_loader: ResourceLoader | None = None
    shared_tools: tuple[ToolContribution, ...] = ()
    execution_location: Literal["backend", "agent"] = "backend"
    execution_kind: str | None = None
    execution_tools: tuple[ToolContribution, ...] = ()
    personal_only: bool = False
    instance_only: bool = False
    catalog_visible: bool = True

    def __post_init__(self) -> None:
        if self.execution_location == "backend":
            if self.execution_kind is not None or self.execution_tools:
                raise ValueError("backend adapters cannot declare agent execution tools")
            return
        if self.execution_kind is None or re.fullmatch(
            r"[a-z][a-z0-9]*", self.execution_kind
        ) is None:
            raise ValueError("agent adapters require a valid execution kind")
        if self.shared_tools:
            raise ValueError("agent integration tools must be instance-bound")
        if any(
            tool.result_model is None
            or not isinstance(tool.result_model, type)
            or not issubclass(tool.result_model, BaseModel)
            for tool in self.execution_tools
        ):
            raise ValueError("agent integration tools require Pydantic result models")
        prefix = f"integration.{self.execution_kind}."
        operations = [tool.execution_operation for tool in self.execution_tools]
        if (
            not operations
            or any(
                operation is None
                or len(operation) > 64
                or not operation.startswith(prefix)
                or re.fullmatch(r"[a-z][a-z0-9]*", operation.removeprefix(prefix))
                is None
                for operation in operations
            )
        ):
            raise ValueError("agent integration operations must match their kind")
        names = [tool.name for tool in self.execution_tools]
        if len(names) != len(set(names)):
            raise ValueError("agent integration tool names must be unique")
        if any(
            len(tool.required_execution_operations)
            != len(set(tool.required_execution_operations))
            or tool.execution_operation in tool.required_execution_operations
            or any(
                len(operation) > 64
                or re.fullmatch(r"[a-z][a-z0-9]*(?:\.[a-z][a-z0-9]*)+", operation)
                is None
                for operation in tool.required_execution_operations
            )
            for tool in self.execution_tools
        ):
            raise ValueError("required agent operations must be unique protocol operations")

    def contributions(self, instance: IntegrationInstance) -> IntegrationContributions:
        config = self.config_model.model_validate(instance.config)
        contributions = self.contribution_factory(instance)
        if self.execution_location == "backend":
            return contributions
        if instance.execution_agent_id is None:
            raise ValueError("agent integrations require a MatoLoop Agent")
        if contributions.tools:
            raise ValueError("agent integration tools must use static execution tools")
        from mato_loop_backend.integrations.execution import execution_tool_handler

        tools = tuple(
            replace(
                tool,
                handler=tool.handler
                or execution_tool_handler(
                    instance,
                    tool.execution_operation,
                    tool.result_model,
                    {
                        name: value
                        for name, value in config.model_dump(mode="json", by_alias=True).items()
                        if name in tool.execution_config_arguments
                    },
                    tool.required_execution_operations,
                ),
            )
            for tool in self.execution_tools
            if instance._execution_operations is None
            or {
                tool.execution_operation,
                *tool.required_execution_operations,
            }
            <= instance._execution_operations
        )
        return IntegrationContributions(
            tools=(*contributions.tools, *tools),
            tool_options=contributions.tool_options,
            prompt_fragments=contributions.prompt_fragments,
            agent_profiles=contributions.agent_profiles,
        )

    async def resources(
        self, instance: IntegrationInstance, credential: dict[str, Any] | None
    ) -> tuple[IntegrationResource, ...]:
        self.config_model.model_validate(instance.config)
        if self.resource_loader is None:
            return ()
        resources = await self.resource_loader(instance, credential)
        capabilities = {action.capability for action in self.resource_actions}
        if any(
            not resource.hard_unavailable_capabilities <= capabilities
            or (
                resource.applicable_capabilities is not None
                and not resource.applicable_capabilities <= capabilities
            )
            for resource in resources
        ):
            raise ValueError("resource capabilities must be declared by the adapter")
        return resources
