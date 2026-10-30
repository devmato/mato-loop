from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mato_loop_backend.tools.registry import BoundTool

import inspect
from collections import deque
from dataclasses import replace
from datetime import UTC, datetime
from traceback import walk_tb
from uuid import UUID

from jsonschema import SchemaError
from jsonschema import ValidationError as JsonSchemaValidationError
from pydantic import ValidationError
from referencing.exceptions import Unresolvable
from sqlalchemy.exc import SQLAlchemyError

from mato_loop_backend.agent.audio_analysis import audio_analysis_available
from mato_loop_backend.agent.video_analysis import video_analysis_available
from mato_loop_backend.diagnostics.service import schedule_diagnostic_event
from mato_loop_backend.execution import ExecutionClientError
from mato_loop_backend.files.service import RootFileError
from mato_loop_backend.integrations.arcane.client import ArcaneError
from mato_loop_backend.integrations.contracts import IntegrationResource
from mato_loop_backend.integrations.mail.client import MailError
from mato_loop_backend.integrations.registry import integration_adapter
from mato_loop_backend.integrations.search import SearchIntegrationError
from mato_loop_backend.integrations.tbs_desk.client import TbsDeskError
from mato_loop_backend.media.attachments import AttachmentError
from mato_loop_backend.permissions.policy import PermissionDecision, PermissionRequest
from mato_loop_backend.providers.base import ProviderError, ToolCall
from mato_loop_backend.providers.media import available_media_capabilities
from mato_loop_backend.security import load_credential, read_credential
from mato_loop_backend.settings import (
    AgentProfileUnavailable,
    ToolExecutionPolicy,
    load_agent_profile_tools,
    load_effective_ai,
)
from mato_loop_backend.settings.effective import filter_agent_profile_integrations
from mato_loop_backend.tools.context import ToolContext
from mato_loop_backend.tools.contracts import (
    DynamicToolArgumentsError,
    RepositoryInstructionsChanged,
    ToolArgumentsEdited,
    ToolCallSuperseded,
    ToolResult,
)
from mato_loop_backend.tools.references import InvalidToolReference, resolve_tool_references
from mato_loop_backend.tools.snapshots import ToolSnapshotState, snapshot_revision
from mato_loop_backend.web.fetch import WebFetchError


def _rejected_arguments(*, field: str | None = None, params: dict[str, str] | None = None) -> ToolResult:
    result = ToolResult.error("tool.invalidArguments", field=field, params=params)
    result.metadata["argumentsRejected"] = True
    return result


async def execute_definition(
    self, call: ToolCall, context: ToolContext, tool: BoundTool, *, permissions_only: bool = False,
) -> ToolResult:
    from mato_loop_backend.tools.registry import _model_revision, _tool_available, logger
    definition = tool.definition
    context.bound_options = dict(tool.selected_options)
    browser_requests: tuple[PermissionRequest, ...] = ()

    if not _tool_available(definition.name, context.tool_settings):
        return ToolResult.error("tool.unavailable")

    async def integration_is_current(selected_id: str | None) -> bool:
        if selected_id is None:
            return True
        current_integrations = await context.current_integrations()
        current_instance = next(
            (
                instance
                for instance in current_integrations.instances
                if str(instance.id) == selected_id
            ),
            None,
        )
        current_revision = (
            _model_revision(current_instance)
            if current_instance is not None
            else None
        )
        expected_revision = (
            dict(tool.integration_revisions).get(selected_id)
            if tool.integration_revisions
            else tool.source_revision
        )
        return current_revision == expected_revision and any(
            contribution.name == definition.name
            and (
                contribution.integration_id == selected_id
                or selected_id in contribution.integration_ids
            )
            for contribution in self._contributions_resolver(
                current_integrations
            ).tools
        )

    async def resolve_resource(
        selected_id: str, resource_key: str, capabilities: set[str]
    ) -> IntegrationResource | None:
        current_integrations = await context.current_integrations()
        instance = next(
            (
                item
                for item in current_integrations.instances
                if str(item.id) == selected_id and item.enabled
            ),
            None,
        )
        if (
            instance is None
            or instance.owner_id not in (None, context.session.user_id)
            or _model_revision(instance) != context.bound_options["binding_revision"]
        ):
            return None
        adapter = integration_adapter(
            instance.integration_type, instance.adapter_type
        )
        if adapter is None or adapter.resource_loader is None:
            return None
        credential = None
        if instance.credential_id is not None:
            record = await load_credential(
                context.db,
                instance.credential_id,
                owner_id=instance.owner_id,
            )
            if record is None:
                return None
            credential = read_credential(record)
        try:
            resources = await adapter.resources(instance, credential)
        except Exception:
            logger.warning(
                "Resource discovery for integration %s failed",
                selected_id,
                exc_info=True,
            )
            return None
        resource = next((item for item in resources if item.key == resource_key), None)
        if (
            resource is None
            or (
                resource.applicable_capabilities is not None
                and not capabilities <= resource.applicable_capabilities
            )
            or capabilities & resource.hard_unavailable_capabilities
        ):
            return None
        return resource

    if tool.source_id is not None and not await integration_is_current(tool.source_id):
        return ToolResult.error("tool.unavailable")
    if definition.requires_memory and not context.memory_read_enabled:
        return ToolResult.error("tool.unavailable")
    if definition.requires_memory_write and not context.memory_write_enabled:
        return ToolResult.error("tool.unavailable")
    if definition.required_capability == "project.coding":
        if context.session.project_id is None:
            return ToolResult.error("tool.unavailable")
    elif definition.required_capability is not None:
        try:
            current_settings, _profile = await load_agent_profile_tools(
                context.db,
                context.session.user_id,
                context.agent_profile_id or context.session.agent_profile_id,
                context.model_trust_level,
            )
        except AgentProfileUnavailable:
            return ToolResult.error("tool.unavailable")
        from mato_loop_backend.agent.subagents import apply_delegation_ceiling

        context.tool_settings = apply_delegation_ceiling(
            current_settings, context.tool_settings._delegation_ceiling
        )
        ai_settings = await load_effective_ai(context.db, context.session.user_id)
        if self._capability_resolver is None:
            raise RuntimeError("tool registry capability resolver is not configured")
        if definition.required_capability not in self._capability_resolver(
            context.tool_settings,
            available_media_capabilities(
                ai_settings, active_model_id=context.model_id
            ),
            video_available=video_analysis_available(
                ai_settings, active_model_id=context.model_id
            ),
            audio_available=audio_analysis_available(
                ai_settings, active_model_id=context.model_id
            ),
        ):
            return ToolResult.error("tool.unavailable")
    raw_arguments = call.arguments
    if tool.default_arguments and tool.default_arguments.keys().isdisjoint(raw_arguments):
        raw_arguments = {**tool.default_arguments, **raw_arguments}
    try:
        raw_arguments = await resolve_tool_references(
            call.name,
            raw_arguments,
            context.db,
            context.session.id,
        )
    except InvalidToolReference as exc:
        return ToolResult.error("integration.staleReference", field=exc.field)
    try:
        arguments = definition.validate_arguments(raw_arguments)
    except JsonSchemaValidationError as exc:
        field_name = ".".join(str(part) for part in exc.absolute_path) or None
        return _rejected_arguments(field=field_name)
    except DynamicToolArgumentsError as exc:
        return _rejected_arguments(field=exc.field)
    except (SchemaError, Unresolvable):
        return _rejected_arguments()
    except ValidationError as exc:
        errors = exc.errors(include_url=False, include_input=False)
        for error in errors:
            if error["type"] == "personal_settings_invalid":
                return _rejected_arguments(field=error["ctx"]["field"])
            if error["type"] == "question_placeholder_not_allowed":
                return ToolResult.error(
                    "question.placeholderNotAllowed",
                    field=f"questions[{error['loc'][1]}].placeholder",
                )
        # Follow input containers, stopping before Pydantic's synthetic union-branch labels.
        field_parts = []
        value = raw_arguments
        for part in errors[0].get("loc") or ():
            if isinstance(value, dict) and part in value:
                field_parts.append(str(part))
                value = value[part]
            elif isinstance(value, list) and isinstance(part, int) and 0 <= part < len(value):
                field_parts.append(str(part))
                value = value[part]
            else:
                if not field_parts:
                    field_parts.append(str(part))
                break
        field_name = ".".join(field_parts) or None
        reason = {
            "missing": "missing", "uuid_parsing": "invalid_uuid", "uuid_type": "invalid_uuid",
            "literal_error": "invalid_choice", "enum": "invalid_choice",
        }.get(errors[0]["type"])
        return _rejected_arguments(
            field=field_name,
            params={"reason": reason} if reason else None,
        )
    selected_integration_id = tool.source_id
    aggregate_integration_ids: tuple[str, ...] = ()
    if definition.instance_argument is not None:
        try:
            selected_integration_id = str(
                UUID(str(getattr(arguments, definition.instance_argument)))
            )
        except (AttributeError, TypeError, ValueError):
            return _rejected_arguments(field=definition.instance_argument)
        if selected_integration_id not in dict(tool.integration_revisions):
            return ToolResult.error("tool.unavailable")
    elif tool.integration_revisions:
        aggregate_integration_ids = tuple(
            integration_id for integration_id, _revision in tool.integration_revisions
        )
        if not all(
            [
                await integration_is_current(integration_id)
                for integration_id in aggregate_integration_ids
            ]
        ):
            return ToolResult.error("tool.unavailable")
        context.bound_options["integration_ids"] = aggregate_integration_ids
    if definition.instance_argument is not None and not await integration_is_current(
        selected_integration_id
    ):
        return ToolResult.error("tool.unavailable")
    if selected_integration_id is not None:
        context.bound_options["integration_id"] = selected_integration_id
        context.bound_options["binding_revision"] = (
            dict(tool.integration_revisions).get(selected_integration_id)
            if tool.integration_revisions
            else tool.source_revision
        )
    try:
        if permissions_only and definition.authorization_factory is None:
            from mato_loop_backend.tools.permissions import (
                authorize_session_requests,
                builtin_permissions,
            )

            return await authorize_session_requests(context, await builtin_permissions(arguments, context, call.name))
        if definition.actions and (
            selected_integration_id is not None or aggregate_integration_ids
        ):
            if definition.authorization_factory is None:
                return ToolResult.error("tool.unavailable")
            resource_key = None
            if definition.resource_argument is not None:
                resource_key = getattr(arguments, definition.resource_argument, None)
                if not isinstance(resource_key, str) or not resource_key:
                    return _rejected_arguments(field=definition.resource_argument)
            from mato_loop_backend.tools.access import profile_tool_settings

            context.tool_settings = await profile_tool_settings(context)
            if (
                context.tool_settings._agent_profile_unavailable
                or not _tool_available(definition.name, context.tool_settings)
            ):
                return ToolResult.error("tool.unavailable")
            requests = definition.authorization_factory(arguments, context)
            if inspect.isawaitable(requests):
                requests = await requests
            if selected_integration_id is not None:
                requests = tuple(
                    replace(
                        request,
                        policy_integration_id=(
                            UUID(selected_integration_id)
                            if request.use_integration_policy or request.policy_integration_id is not None
                            else None
                        ),
                        policy_resource_key=(
                            resource_key
                            if request.use_integration_policy
                            and definition.resource_argument is not None
                            else request.policy_resource_key
                        ),
                        policy_hard_unavailable_capabilities=frozenset(),
                    )
                    for request in requests
                )
            declared = {action.capability for action in definition.actions}
            if not definition.actions_satisfied(
                {request.capability for request in requests}
            ):
                return ToolResult.error("tool.unavailable")
            if resource_key is not None:
                if self._policy_resolver is not None and any(
                    self._policy_resolver(
                        context.tool_settings,
                        request.capability,
                        next(
                            action.default_policy(context.tool_settings)
                            for action in definition.actions
                            if action.capability == request.capability
                        ),
                        integration_id=request.policy_integration_id,
                        resource_key=request.policy_resource_key,
                    ) == ToolExecutionPolicy.DISABLED
                    for request in requests
                ):
                    return ToolResult.error("permission.denied")
                # Discover host-only metadata, never resource content, before approval.
                resource = await resolve_resource(
                    selected_integration_id, resource_key, declared
                )
                if resource is None:
                    return ToolResult.error("tool.unavailable")
                requests = tuple(
                    replace(
                        request,
                        policy_hard_unavailable_capabilities=(
                            resource.hard_unavailable_capabilities
                            if request.use_integration_policy else frozenset()
                        ),
                    )
                    for request in requests
                )
            browser_requests = tuple(
                request for request in requests if request.action_kind.startswith("browser_")
            )
            if browser_requests:
                for key in tuple(context.approval_resolution):
                    if key.startswith("browser_approved:"):
                        context.approval_resolution.pop(key)
            if permissions_only:
                from mato_loop_backend.tools.permissions import authorize_session_requests

                result = await authorize_session_requests(context, requests)
                if result.is_error:
                    return result
            else:
                for request in requests:
                    if not await self.allow(context, request):
                        return ToolResult.error("permission.denied")
            if selected_integration_id is not None:
                if not await integration_is_current(selected_integration_id):
                    return ToolResult.error("tool.unavailable")
            elif not all(
                [
                    await integration_is_current(integration_id)
                    for integration_id in aggregate_integration_ids
                ]
            ):
                return ToolResult.error("tool.unavailable")
            if resource_key is not None:
                resource = await resolve_resource(
                    selected_integration_id, resource_key, declared
                )
                if resource is None:
                    return ToolResult.error("tool.unavailable")
                context.bound_resource = resource
        if tool.source_type == "integration":
            from mato_loop_backend.tools.access import profile_tool_settings

            # Aggregate handlers filter resource data using these policies after approval.
            context.tool_settings = await profile_tool_settings(context)
            if not _tool_available(definition.name, context.tool_settings):
                return ToolResult.error("tool.unavailable")
            context.integration_settings = await context.current_integrations()
            bound_ids = set(aggregate_integration_ids)
            if selected_integration_id is not None:
                bound_ids.add(selected_integration_id)
            context.integration_settings = filter_agent_profile_integrations(
                context.integration_settings, None, allowed_ids=bound_ids
            )
            if bound_ids != {str(item.id) for item in context.integration_settings.instances}:
                return ToolResult.error("tool.unavailable")
        if browser_requests and any(
            key.startswith("browser_approved:") for key in context.approval_resolution
        ):
            # An approval wait may invalidate earlier capabilities; never prompt in this fence.
            for request in requests:
                if await context.ask_handler(request, check_only=True) != PermissionDecision.ALLOW:
                    return ToolResult.error("permission.denied")
        if permissions_only:
            return ToolResult.json({"permissionsReady": True, "executed": False}) if (
                selected_integration_id is not None or aggregate_integration_ids
            ) else ToolResult.error("permission.preflightUnavailable")
        result = await definition.handler(arguments, context)
        if definition.releases_private_data and not result.is_error:
            result.metadata["privateIntegrationData"] = True
        if definition.snapshot_policy is not None and not result.is_error:
            result.metadata["toolSnapshot"] = ToolSnapshotState(
                arguments=arguments.model_dump(mode="json"),
                revision=snapshot_revision(tool, context.tool_settings),
                observed_at=datetime.now(UTC),
            ).model_dump(mode="json")
        return result
    except (ToolArgumentsEdited, ToolCallSuperseded, RepositoryInstructionsChanged):
        raise
    except (
        ArcaneError,
        AttachmentError,
        ExecutionClientError,
        MailError,
        RootFileError,
        SearchIntegrationError,
        TbsDeskError,
        WebFetchError,
    ) as exc:
        return ToolResult.error(
            exc.code,
            params=exc.params if isinstance(exc, (WebFetchError, ExecutionClientError)) else None,
        )
    except ProviderError as exc:
        return ToolResult.error(exc.public_code, params=exc.params)
    except SQLAlchemyError:
        raise
    except Exception as exc:
        logger.exception("Tool %s failed", call.name)
        schedule_diagnostic_event(
            context.session.user_id, "tool.failed", component="tool",
            session_id=context.session.id, run_id=context.run_id, correlation_id=call.id,
            payload={
                "name": call.name,
                "errorCode": "tool.executionFailed",
                "exceptionClass": type(exc).__name__,
                # Keep code locations only; exception text, source and locals may contain secrets.
                "stackTrace": [
                    {
                        "module": frame.f_globals.get("__name__"),
                        "function": frame.f_code.co_name,
                        "line": line,
                    }
                    for frame, line in deque(walk_tb(exc.__traceback__), maxlen=12)
                ],
            },
        )
        return ToolResult.error("tool.executionFailed")
