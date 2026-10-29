from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend.agent import run_support
from mato_loop_backend.agent.media_model_helpers import media_model_handlers
from mato_loop_backend.agent.openscad_preview import verify_preview
from mato_loop_backend.agent.projection import (
    ContextProjection,
    _tool_result_reference,
)
from mato_loop_backend.agent.repository_instructions import (
    check_repository_instruction_targets,
)
from mato_loop_backend.agent.research import _add_research_sources
from mato_loop_backend.agent.run_contracts import AgentRunError
from mato_loop_backend.agent.run_support import (
    _RESEARCH_TOOL_NAMES,
    MEMORY_WRITE_OUTCOME_PRIORITY,
)
from mato_loop_backend.agent.session_settings import apply_run_session_settings
from mato_loop_backend.agent.subagents import run_tasks
from mato_loop_backend.diagnostics.service import schedule_diagnostic_event
from mato_loop_backend.error_contract import error_payload
from mato_loop_backend.events import run_events
from mato_loop_backend.execution import ExecutionClient
from mato_loop_backend.files.service import RootFileService
from mato_loop_backend.media.attachments import AttachmentError, AttachmentService
from mato_loop_backend.memory import policy as memory_policy
from mato_loop_backend.models import AgentRun, ChatSession, Message, WebSearchPolicy
from mato_loop_backend.permissions.policy import PermissionDecision, PermissionRequest
from mato_loop_backend.providers.base import (
    MAX_PROVIDER_IMAGE_BYTES,
    MAX_PROVIDER_IMAGES,
    ProviderImageInput,
    ToolCall,
)
from mato_loop_backend.settings import (
    AgentRuntimeSettings,
    AiSettings,
    InstanceLimits,
    IntegrationSettings,
    MemorySettings,
    SettingsStore,
    ToolSettings,
)
from mato_loop_backend.settings.models import (
    EffectiveAgentIdentity,
    LlmProviderConfig,
    ModelConfig,
    TrustLevel,
)
from mato_loop_backend.skills import RunSkillState
from mato_loop_backend.tools.context import ToolContext
from mato_loop_backend.tools.contracts import (
    RepositoryInstructionsChanged,
    ToolAttachment,
    ToolCallSuperseded,
    ToolFileAction,
    ToolResult,
)
from mato_loop_backend.tools.registry import EffectiveToolSet
from mato_loop_backend.tools.secrets import effective_sudo_reference


async def persist_tool_result(
    db: AsyncSession,
    run: AgentRun,
    session: ChatSession,
    call: ToolCall,
    *,
    content: str,
    metadata: dict[str, Any],
    attachments: tuple[ToolAttachment, ...],
    file_actions: tuple[ToolFileAction, ...],
    trust_level: TrustLevel,
    ephemeral_output: bool,
    limits: InstanceLimits,
    attachment_service: type[AttachmentService],
    execution_binding: dict[str, Any] | None = None,
) -> tuple[Message, str, dict[str, Any]]:
    tool_message = Message(
        session_id=session.id,
        role="tool",
        content=content,
        tool_call_id=call.id,
        trust_level=trust_level.value,
        provider_data={
            "_matoloop": {
                "runId": str(run.id),
                "toolName": call.name,
                **(execution_binding or {}),
                **({"shellExecution": metadata["shellExecution"]}
                   if call.name == "shell_run" and isinstance(metadata.get("shellExecution"), dict) else {}),
                **({"userEditedArguments": True} if metadata.get("userEdited") is True else {}),
                **({"errorCode": metadata["errorCode"]} if isinstance(metadata.get("errorCode"), str) else {}),
                **({"toolSnapshot": metadata["toolSnapshot"]} if isinstance(metadata.get("toolSnapshot"), dict) else {}),
                **({"toolDiscovery": metadata["toolDiscovery"]}
                   if call.name == "search_tools" and not metadata.get("errorCode")
                   and isinstance(metadata.get("toolDiscovery"), dict) else {}),
                **({"computerRead": metadata["computerRead"]} if isinstance(metadata.get("computerRead"), dict) else {}),
                **({"repositoryInstructionTargets": metadata["repositoryInstructionTargets"]}
                   if isinstance(metadata.get("repositoryInstructionTargets"), dict) else {}),
                **({"preparedReply": metadata["preparedReply"]} if isinstance(metadata.get("preparedReply"), dict) else {}),
                **({"suppressResponseNotification": True}
                   if call.name in {"notify", "silence"}
                   and metadata.get("suppressResponseNotification") is True
                   and not metadata.get("errorCode") else {}),
                "fileOutputsPending": not ephemeral_output or bool(attachments or file_actions),
                "fileActions": [
                    {
                        "operation": action.operation,
                        "root": action.root,
                        **({"rootIdentity": action.root_identity} if action.root == "session" else {}),
                        "path": action.path,
                        "filename": action.filename,
                        "sizeBytes": action.size_bytes,
                        "kind": action.kind,
                        **({"sourcePath": action.source_path, "sourceRoot": action.source_root}
                           if action.source_path is not None else {}),
                        **({"paths": list(action.paths)} if action.paths is not None else {}),
                        **({"archiveRootContents": True} if action.archive_root_contents else {}),
                        **({"fileId": action.file_id, "fileRevision": action.file_revision}
                           if action.file_id is not None else {}),
                        **({
                            "executionAgentId": action.execution_agent_id,
                            "currentDirectory": action.current_directory,
                        } if action.root == "workspace" else {}),
                    }
                    for action in file_actions
                ],
            }
        },
    )
    db.add(tool_message)
    attachments_committed = False
    try:
        await db.flush()
        service = attachment_service(settings=limits, run_id=run.id)
        for position, attachment in enumerate(attachments):
            if (
                attachment.file_root_id is not None
                and attachment.file_id is not None
                and attachment.file_path is not None
            ):
                await service.create_reference(
                    db, tool_message,
                    file_root_id=attachment.file_root_id,
                    file_id=attachment.file_id,
                    file_revision=attachment.file_revision,
                    file_path=attachment.file_path,
                    filename=attachment.filename,
                    position=position,
                    category=attachment.category,
                    mime_type=attachment.mime_type,
                    metadata={
                        **attachment.metadata,
                        "toolOutputPresentation": attachment.presentation,
                    },
                )
            else:
                await service.create_attachment(
                    db, tool_message, attachment.data, attachment.mime_type,
                    kind=attachment.kind,
                    filename=attachment.filename,
                    position=position,
                    metadata={
                        **attachment.metadata,
                        "toolOutputPresentation": attachment.presentation,
                    },
                    category=attachment.category,
                    purpose="artifact",
                    source_file_root_id=attachment.file_root_id,
                    file_id=attachment.file_id,
                )
        commit_task = asyncio.create_task(db.commit())
        try:
            await asyncio.shield(commit_task)
        except asyncio.CancelledError:
            while not commit_task.done():
                try:
                    await asyncio.shield(commit_task)
                except asyncio.CancelledError:
                    continue
            await commit_task
            attachments_committed = True
            raise
        attachments_committed = True
        if metadata.get("executionAgentChanged") is True:
            await run_events.publish(run.id, {
                "type": "session.executionAgentChanged",
                "executionAgentId": str(run.execution_agent_id),
            })
    except AttachmentError as exc:
        await db.rollback()
        content = json.dumps(
            error_payload(exc.code, fallback_code="tool.executionFailed"),
            ensure_ascii=False,
        )
        metadata = {"errorCode": exc.code}
        tool_message = Message(
            session_id=session.id, role="tool", content=content,
            tool_call_id=call.id, trust_level=trust_level.value,
            provider_data={"_matoloop": {
                "runId": str(run.id), "toolName": call.name,
                **(execution_binding or {}),
                "errorCode": exc.code, "fileOutputsPending": True,
            }},
        )
        db.add(tool_message)
        await db.commit()
    except BaseException:
        if not attachments_committed:
            await db.rollback()
        raise
    return tool_message, content, metadata


@dataclass
class ToolBatchResult:
    research_tool_calls_used: int = 0
    research_used: bool = False
    openscad_render_pending: bool = False
    openscad_render_retries: int = 0
    openscad_source_review_pending: tuple[str, str] | None = None
    openscad_source_review_retries: int = 0
    repeated_tool_call_seen: bool = False
    repeated_tool_call_limit_reached: bool = False
    user_denied_batch: bool = False
    memory_workflow_started: bool = False
    memory_workflow_resolved: bool = False
    memory_write_attempted: bool = False
    resolved_memory_outcome: str | None = None
    required_search_called: bool = False
    required_search_succeeded: bool = False
    required_search_error: str | None = None
    skill_load_called: bool = False
    skill_load_succeeded: bool = False


async def execute_tool_batch(
    runtime,
    db: AsyncSession,
    run: AgentRun,
    session: ChatSession,
    assistant: Message,
    calls: list[ToolCall],
    serialized_calls: list[dict[str, Any]],
    *,
    toolset: EffectiveToolSet | None,
    offered_names: set[str],
    root_files: RootFileService,
    memory_state: memory_policy.EffectiveMemory,
    model: ModelConfig,
    provider: LlmProviderConfig,
    ai_settings: AiSettings,
    runtime_settings: AgentRuntimeSettings,
    tool_settings: ToolSettings,
    limits: InstanceLimits,
    integration_settings: IntegrationSettings,
    identity: EffectiveAgentIdentity,
    agent_profile_id: UUID | None,
    skill_state: RunSkillState,
    execution_client: ExecutionClient | None,
    records: list[Message],
    projection: ContextProjection,
    trust_level: TrustLevel,
    excluded_memory_content_hashes: frozenset[str],
    delegated: bool,
    max_tool_calls: int | None,
    tool_calls_used: int,
    execution_binding: dict[str, Any],
    rejected_batch_code: str | None,
    truncated_batch: bool,
    tool_batch_exceeds_budget: bool,
    repeated_tool_call_repair_available: bool,
    failed_tool_call_signatures: dict[str, str],
    retried_transient_reads: set[str],
    successful_research_calls: dict[str, dict[str, Any]],
    research_sources: dict[str, str],
    research_source_titles: dict[str, str],
    openscad_pending_paths: set[tuple[str, str]],
    session_read_cursors: dict[UUID, UUID | None],
    one_time_secrets: dict[UUID, str],
    run_secret_refs: dict[str, str],
    ephemeral_images: dict[tuple[UUID, int, str, int], ProviderImageInput],
    live_tool_calls: dict[UUID, list[dict[str, Any]]],
    model_request: int,
    diagnostics_active: bool,
    next_helper_request,
    count_helper_request,
    attachment_service: type[AttachmentService],
    research_tool_calls_used: int,
    research_used: bool,
    openscad_render_pending: bool,
    openscad_render_retries: int,
    openscad_source_review_pending: tuple[str, str] | None,
    openscad_source_review_retries: int,
    repository_entries: dict[str, dict] | None = None,
    delegation_tool_names: frozenset[str] | None = None,
    repository_authorize: Callable[[PermissionRequest], Awaitable[PermissionDecision]] | None = None,
) -> ToolBatchResult:
    state = ToolBatchResult(
        research_tool_calls_used=research_tool_calls_used,
        research_used=research_used,
        openscad_render_pending=openscad_render_pending,
        openscad_render_retries=openscad_render_retries,
        openscad_source_review_pending=openscad_source_review_pending,
        openscad_source_review_retries=openscad_source_review_retries,
    )
    superseded = False
    repository_deferred = False
    deferred_content = json.dumps({
        "executed": False,
        "repositoryInstructionsUpdated": True,
        "message": "This call and the remaining batch were not executed. Review the repository instructions in the next context and call again if still appropriate.",
    })
    invalid_question_batch = any(call.name in {"question", "secret_input"} for call in calls) and len(calls) != 1
    invalid_skill_batch = any(call.name == "load_skill" for call in calls) and len(calls) != 1
    invalid_agent_batch = any(call.name == "select_execution_agent" for call in calls) and len(calls) != 1
    exclusive_error_code = (
        "question.exclusiveCallRequired" if invalid_question_batch else
        "skill.loadMustBeExclusive" if invalid_skill_batch else
        "executionAgent.exclusiveCallRequired" if invalid_agent_batch else None
    )

    async def ask(
        context: ToolContext,
        authorize: Callable[..., Awaitable[PermissionDecision]],
        request: PermissionRequest,
        *, check_only: bool = False,
    ) -> PermissionDecision:
        decision = await authorize(request, check_only=check_only)
        if (decision not in {PermissionDecision.DENY, PermissionDecision.USER_DENIED}
                and repository_entries is not None and repository_authorize is not None
                and execution_client is not None):
            targets, changed = await check_repository_instruction_targets(
                request, execution_client, records, repository_authorize, repository_entries,
                target_paths=context.repository_target_paths,
            )
            if targets is not None:
                context.repository_target_paths = tuple(targets["paths"])
            if changed:
                raise RepositoryInstructionsChanged(targets)
        return decision

    for index, call in enumerate(calls):
        bound_tool = toolset.tools.get(call.name) if toolset is not None else None
        tool_label = bound_tool.definition.label if bound_tool else None
        display = serialized_calls[index]["toolDisplay"]
        await run_support._publish_run_phase(
            run.id, "processing_tool", tool=call.name, toolLabel=tool_label,
            toolDisplay=display, toolCallId=call.id, index=index, total=len(calls),
        )
        await db.refresh(session)
        apply_run_session_settings(session, run)
        attachments = ()
        file_actions = ()
        ephemeral_output = False
        memory_available_now = True
        memory_write_available_now = True
        current_memory_state = memory_state
        authentication_scope = ""
        if call.name == "shell_run" and call.arguments.get("elevated") is True and execution_client is not None:
            reference = await effective_sudo_reference(
                db, session, run_secret_refs, execution_client.agent_id, call.arguments.get("sudo_password_ref"),
            )
            authentication_scope = f":agent={execution_client.agent_id}:sudo={reference}"
        call_signature = run_support._tool_call_signature(call)
        call_signature += authentication_scope
        validation_signature = "arguments:" + run_support._tool_call_signature(call, include_reason=True)
        if call.arguments.get("root") in {"workspace", "computer"} or call.name in {"shell_run", "apply_patch"}:
            call_signature += f":directory={run.current_directory_snapshot}"
            validation_signature += f":directory={run.current_directory_snapshot}"
        is_research_call = (
            session.web_search_policy != WebSearchPolicy.DISABLED
            and call.name in _RESEARCH_TOOL_NAMES
        )
        repeated_research_call = is_research_call and call_signature in successful_research_calls
        if call.name in {"memory_write", "memory_retrieve", "memory_browse", "context_search"}:
            current_settings = await SettingsStore.load(db, MemorySettings, user_id=session.user_id)
            current_memory_state = memory_policy.effective_memory(
                session, current_settings, provider, model,
            )
            if delegated:
                current_memory_state = memory_policy.EffectiveMemory(False, False, False)
            memory_available_now = current_memory_state.read_enabled
            memory_write_available_now = current_memory_state.write_enabled
        if exclusive_error_code is not None:
            content = json.dumps(error_payload(exclusive_error_code), ensure_ascii=False)
            metadata = {"errorCode": exclusive_error_code}
        if not rejected_batch_code and not repository_deferred and is_research_call and not repeated_research_call and not state.user_denied_batch:
            state.research_tool_calls_used += 1
        tool_trust = trust_level
        if superseded:
            content = json.dumps(error_payload("tool.superseded"), ensure_ascii=False)
            metadata = {"errorCode": "tool.superseded"}
        elif rejected_batch_code is not None:
            rejected = error_payload(rejected_batch_code)
            if truncated_batch and not tool_batch_exceeds_budget:
                rejected["repair"] = (
                    "No calls in this output-truncated batch were executed. Return the complete batch "
                    "again with full schema-valid arguments; keep the response concise."
                )
            content = json.dumps(rejected, ensure_ascii=False)
            metadata = {"errorCode": rejected_batch_code}
        elif state.repeated_tool_call_limit_reached:
            content = json.dumps(error_payload("agent.repeatedToolCall"), ensure_ascii=False)
            metadata = {"errorCode": "agent.repeatedToolCall"}
        elif exclusive_error_code is not None:
            pass
        elif repository_deferred:
            content = deferred_content
            metadata = {"repositoryInstructionsDeferred": True}
        elif state.user_denied_batch:
            content = json.dumps(error_payload("tool.skippedAfterUserDenial"), ensure_ascii=False)
            metadata = {"errorCode": "tool.skippedAfterUserDenial"}
        elif call.name == "secret_input" and max_tool_calls is not None and tool_calls_used >= max_tool_calls:
            content = json.dumps(error_payload("agent.toolCallLimitReached"), ensure_ascii=False)
            metadata = {"errorCode": "agent.toolCallLimitReached"}
        elif repeated_research_call:
            state.repeated_tool_call_seen = True
            if not repeated_tool_call_repair_available:
                state.repeated_tool_call_limit_reached = True
            previous_metadata = successful_research_calls[call_signature]
            content = json.dumps({
                "alreadyCompleted": True,
                "message": "This exact research call already succeeded. Use the existing evidence and finalize the answer.",
                "toolResultReference": previous_metadata["toolResultReference"],
            }, ensure_ascii=False)
            metadata = {
                "alreadyCompleted": True, "sources": previous_metadata.get("sources", []),
            }
        elif (
            call_signature in failed_tool_call_signatures
            or validation_signature in failed_tool_call_signatures
        ) and not run_support._retryable_transient_read(
            call, call_signature, failed_tool_call_signatures, retried_transient_reads,
        ):
            state.repeated_tool_call_seen = True
            if not repeated_tool_call_repair_available:
                state.repeated_tool_call_limit_reached = True
            content = json.dumps(error_payload("agent.repeatedToolCall"), ensure_ascii=False)
            metadata = {"errorCode": "agent.repeatedToolCall"}
        elif call.name not in offered_names or (not memory_available_now and call.name != "context_search") or (
            call.name == "memory_write" and not memory_write_available_now
        ):
            content = json.dumps(error_payload("tool.unavailable"), ensure_ascii=False)
            metadata = {"errorCode": "tool.unavailable"}
        else:
            if call_signature in failed_tool_call_signatures:
                retried_transient_reads.add(call_signature)
            approval_resolution: dict[str, str] = {}
            authorize = partial(
                runtime._authorize, db, run, session, call,
                root_files=root_files, approval_resolution=approval_resolution,
            )

            question = partial(runtime._request_question, db, run, session, call)
            tool_phase = {
                "tool": call.name, "toolLabel": tool_label, "toolDisplay": display,
                "toolCallId": call.id, "index": index, "total": len(calls),
            }
            (
                pdf_vision_handler, video_analysis_handler, video_target,
                analyze_image_batch, fallback_vision_model,
                audio_analysis_handler, audio_target,
            ) = media_model_handlers(
                db=db, run=run, session=session, ai_settings=ai_settings,
                model=model, model_trust_level=trust_level,
                runtime_settings=runtime_settings, tool_settings=tool_settings,
                tool_phase=tool_phase,
                next_model_request=next_helper_request,
                count_model_request=count_helper_request,
            )
            tool_context = ToolContext(
                db=db, run_id=run.id, session=session, tool_call=call,
                root_files=root_files, integration_settings=integration_settings,
                ask_handler=authorize, question_handler=question, tool_settings=tool_settings,
                pdf_vision_handler=pdf_vision_handler,
                pdf_images_enabled=model.supports_image_input,
                video_analysis_handler=video_analysis_handler,
                audio_analysis_handler=audio_analysis_handler,
                audio_analysis_target=audio_target,
                video_analysis_target=video_target, assistant_message_id=assistant.id,
                memory_read_enabled=current_memory_state.read_enabled,
                memory_write_enabled=current_memory_state.write_enabled,
                memory_trust_level=trust_level,
                excluded_memory_content_hashes=excluded_memory_content_hashes,
                model_id=model.id, model_trust_level=trust_level,
                agent_persona_id=identity.persona.id, agent_profile_id=agent_profile_id,
                skill_state=skill_state,
                approval_resolution=approval_resolution, execution_client=execution_client,
                session_read_cursors=session_read_cursors, one_time_secrets=one_time_secrets,
                run_secret_refs=run_secret_refs,
                task_handler=(
                    partial(
                        run_tasks, runtime, db, run, session, tool_call_id=call.id,
                        tool_settings=tool_settings, tool_names=delegation_tool_names if delegation_tool_names is not None else offered_names,
                        integration_settings=integration_settings, max_tool_calls=max_tool_calls,
                    ) if not delegated else None
                ),
                arguments_edited_handler=partial(
                    run_support._persist_edited_tool_arguments,
                    db, run.id, assistant, serialized_calls, live_tool_calls[assistant.id],
                    index, call, bound_tool.definition if bound_tool else None,
                ),
            )
            tool_context.ask_handler = partial(ask, tool_context, authorize)
            assert toolset is not None
            started_at = time.monotonic()
            if diagnostics_active:
                schedule_diagnostic_event(
                    session.user_id, "tool.started", component="tool",
                    session_id=session.id, run_id=run.id, correlation_id=call.id,
                    payload={
                        "name": call.name, "display": display,
                        "argumentKeys": sorted(call.arguments),
                        "executionAgentId": str(run.execution_agent_id) if run.execution_agent_id else None,
                    },
                )
            try:
                tool_result = await toolset.execute(call, tool_context)
            except ToolCallSuperseded:
                superseded = True
                tool_result = ToolResult.error("tool.superseded")
            except RepositoryInstructionsChanged as exc:
                repository_deferred = True
                tool_result = ToolResult(deferred_content, {
                    "repositoryInstructionsDeferred": True,
                    "repositoryInstructionTargets": exc.targets,
                })
            except Exception as exc:
                if diagnostics_active:
                    schedule_diagnostic_event(
                        session.user_id, "tool.failed", component="tool",
                        session_id=session.id, run_id=run.id, correlation_id=call.id,
                        payload={
                            "name": call.name, "exceptionClass": type(exc).__name__,
                            "durationMs": round((time.monotonic() - started_at) * 1_000),
                        },
                    )
                raise
            if (repository_entries is not None and execution_client is not None
                    and tool_context.repository_target_paths
                    and not isinstance(tool_result.metadata.get("errorCode"), str)):
                tool_result.metadata["repositoryInstructionTargets"] = {
                    "executionAgentId": str(execution_client.agent_id),
                    "currentDirectory": execution_client.current_directory,
                    "paths": list(tool_context.repository_target_paths),
                }
            if authentication_scope and reference is None:
                reference = await effective_sudo_reference(
                    db, session, run_secret_refs, execution_client.agent_id, call.arguments.get("sudo_password_ref"),
                )
                authentication_scope = f":agent={execution_client.agent_id}:sudo={reference}"
            if tool_result.metadata.get("userEdited") is True or authentication_scope:
                call_signature = run_support._tool_call_signature(call)
                call_signature += authentication_scope
                if call.arguments.get("root") in {"workspace", "computer"} or call.name in {"shell_run", "apply_patch"}:
                    call_signature += f":directory={run.current_directory_snapshot}"
            if delegated:
                for attachment in tool_result.attachments:
                    attachment.ephemeral = True
            if tool_context.user_denied:
                if tool_result.metadata.get("errorCode") != "permission.userDenied":
                    tool_result = ToolResult.error("permission.userDenied")
                state.user_denied_batch = True
            content = tool_result.content
            metadata = tool_result.metadata
            if diagnostics_active:
                schedule_diagnostic_event(
                    session.user_id, "tool.completed", component="tool",
                    session_id=session.id, run_id=run.id, correlation_id=call.id,
                    payload={
                        "name": call.name, "errorCode": metadata.get("errorCode"),
                        "resultCharacters": len(content),
                        "attachmentCount": len(tool_result.attachments),
                        "durationMs": round((time.monotonic() - started_at) * 1_000),
                        "userDenied": tool_context.user_denied,
                    },
                )
            if metadata.get("privateIntegrationData") is True:
                session.private_integration_data_used = True
            attachments = tool_result.attachments
            file_actions = tool_result.file_actions
            tool_trust = (
                TrustLevel.PRIVATE if trust_level == TrustLevel.PRIVATE
                or tool_result.trust_level == TrustLevel.PRIVATE else TrustLevel.PUBLIC
            )
            if (call.name == "render_openscad" and call.arguments.get("output", "preview") == "preview"
                    and not isinstance(metadata.get("errorCode"), str)):
                content = await verify_preview(
                    content, attachments, run=run, session=session,
                    records=records, projected_messages=projection.messages,
                    model=model, provider=provider, ai_settings=ai_settings,
                    fallback_vision_model=fallback_vision_model,
                    model_trust_level=trust_level, tool_trust_level=tool_trust,
                    analyze_image_batch=analyze_image_batch,
                )
                # Preview bytes are consumed by inline verification, not session artifacts.
                attachments = ()
                file_actions = ()
            if (any(attachment.kind == "image" and not attachment.ephemeral for attachment in attachments)
                    and not model.supports_image_input):
                content += "\n\n[Image saved but not sent to the current text-only model.]"
            ephemeral_output = any(attachment.ephemeral for attachment in attachments)
            for position, attachment in enumerate(attachments):
                if not attachment.ephemeral:
                    continue
                if attachment.kind != "image" or not model.supports_image_input:
                    content += "\n\n[Ephemeral image not sent to this model and not saved.]"
                elif not 0 < len(attachment.data) <= MAX_PROVIDER_IMAGE_BYTES:
                    content += "\n\n[Ephemeral image exceeds the retention budget or is empty; not retained or saved.]"
                else:
                    # ponytail: bounded newest selection, at most eight / 20 MiB per run.
                    image_key = (assistant.id, model_request, call.id, position)
                    previous = ephemeral_images.pop(image_key, None)
                    if previous is not None:
                        previous.data = b""
                    while ephemeral_images and (
                        len(ephemeral_images) >= MAX_PROVIDER_IMAGES
                        or sum(len(item.data) for item in ephemeral_images.values())
                        + len(attachment.data) > MAX_PROVIDER_IMAGE_BYTES
                    ):
                        ephemeral_images.pop(next(iter(ephemeral_images))).data = b""
                    ephemeral_images[image_key] = ProviderImageInput(
                        message_index=0, data=attachment.data,
                        mime_type=attachment.mime_type, trust_level=tool_trust.value,
                        filename=attachment.filename,
                    )
                attachment.data = b""
            attachments = tuple(attachment for attachment in attachments if not attachment.ephemeral)
        tool_message, content, metadata = await persist_tool_result(
            db, run, session, call, content=content, metadata=metadata,
            attachments=attachments, file_actions=file_actions,
            trust_level=tool_trust, ephemeral_output=ephemeral_output,
            limits=limits, attachment_service=attachment_service,
            execution_binding=execution_binding,
        )
        error_code = metadata.get("errorCode")
        await run_events.publish(run.id, {
            "type": "tool.completed", "tool": call.name, "toolLabel": tool_label,
            "toolDisplay": display, "toolCallId": call.id,
            "output": content[:500], "metadata": metadata,
        })
        if error_code == "tool.superseded":
            continue
        if metadata.get("repositoryInstructionsDeferred") is True:
            continue
        if not isinstance(error_code, str):
            if call.name == "render_openscad" and call.arguments.get("output", "preview") == "preview":
                rendered_root = call.arguments.get("root")
                rendered_path = call.arguments.get("path")
                project_path = call.arguments.get("projectPath")
                was_pending = state.openscad_render_pending
                if isinstance(rendered_root, str) and isinstance(rendered_path, str) and openscad_pending_paths:
                    covered_paths = {
                        item for item in openscad_pending_paths
                        if item[0] == rendered_root and (
                            item[1] == rendered_path or (
                                isinstance(project_path, str) and (
                                    project_path == "." or item[1] == project_path
                                    or item[1].startswith(project_path.rstrip("/") + "/")
                                )
                            )
                        )
                    }
                    openscad_pending_paths.difference_update(covered_paths)
                    state.openscad_render_pending = bool(openscad_pending_paths)
                else:
                    state.openscad_render_pending = False
                if not state.openscad_render_pending:
                    state.openscad_render_retries = 0
                    if was_pending and isinstance(rendered_root, str) and isinstance(rendered_path, str):
                        state.openscad_source_review_pending = (rendered_root, rendered_path)
                        state.openscad_source_review_retries = 0
            elif (call.name in {"write_file", "edit_file"}
                  and isinstance(call.arguments.get("path"), str)
                  and call.arguments["path"].casefold().endswith(".scad")):
                state.openscad_render_pending = True
                root = call.arguments.get("root")
                path = call.arguments.get("path")
                if isinstance(root, str) and isinstance(path, str):
                    openscad_pending_paths.add((root, path))
                state.openscad_source_review_pending = None
            elif call.name == "read" and state.openscad_source_review_pending == (
                call.arguments.get("root"), call.arguments.get("path"),
            ):
                state.openscad_source_review_pending = None
                state.openscad_source_review_retries = 0
        if (call.name == "memory_retrieve" and metadata.get("memoryWritePending") is True
                and not isinstance(error_code, str)):
            state.memory_workflow_started = True
        if call.name == "memory_write":
            state.memory_workflow_started = True
            state.memory_write_attempted = True
            outcome: str | None = None
            if error_code in {"permission.denied", "permission.userDenied"}:
                state.memory_workflow_resolved = True
                outcome = "denied"
            elif error_code == "tool.skippedAfterUserDenial":
                state.memory_workflow_resolved = True
            elif not isinstance(error_code, str):
                state.memory_workflow_resolved = True
                reported = metadata.get("memoryWriteOutcome")
                outcome = reported if reported in {"stored", "forgotten", "duplicate"} else "stored"
            if outcome is not None and (
                state.resolved_memory_outcome is None
                or MEMORY_WRITE_OUTCOME_PRIORITY[outcome]
                > MEMORY_WRITE_OUTCOME_PRIORITY[state.resolved_memory_outcome]
            ):
                state.resolved_memory_outcome = outcome
        if rejected_batch_code or error_code == "agent.repeatedToolCall":
            pass
        elif error_code in {
            "question.exclusiveCallRequired", "skill.loadMustBeExclusive",
            "executionAgent.exclusiveCallRequired",
        }:
            failed_tool_call_signatures.pop(call_signature, None)
            retried_transient_reads.discard(call_signature)
        elif isinstance(error_code, str):
            # Validation repairs may change metadata; execution retries must change the action.
            failure_signature = validation_signature if error_code == "tool.invalidArguments" else call_signature
            failed_tool_call_signatures[failure_signature] = (
                "approval.targetChanged:notStarted" if metadata.get("retryAfterFreshApproval") is True else error_code
            )
        else:
            if bound_tool is not None and any(action.mutates for action in bound_tool.definition.actions):
                # A successful mutation changes preconditions, not earlier outcomes.
                failed_tool_call_signatures.clear()
                retried_transient_reads.clear()
            else:
                failed_tool_call_signatures.pop(call_signature, None)
                failed_tool_call_signatures.pop(validation_signature, None)
                retried_transient_reads.discard(call_signature)
            if call.name in _RESEARCH_TOOL_NAMES:
                state.research_used = True
                _add_research_sources(research_sources, metadata, research_source_titles)
                successful_research_calls[call_signature] = {
                    **metadata,
                    "toolResultReference": _tool_result_reference(tool_message.id, tool_message.content),
                }
        if call.name == "todo_write" and not isinstance(error_code, str):
            await run_events.publish(run.id, {
                "type": "todo.updated", "todos": json.loads(content)["todos"],
            })
        if error_code == "executionAgent.unavailable":
            raise AgentRunError(error_code)
        updated_identity = metadata.get("agentIdentity")
        if isinstance(updated_identity, dict):
            await run_events.publish(run.id, {
                "type": "agent.identity.updated", "identity": updated_identity,
            })
        if call.name == "web_search":
            state.required_search_called = True
            if isinstance(error_code, str):
                state.required_search_error = error_code
            else:
                state.required_search_succeeded = True
        if call.name == "load_skill":
            state.skill_load_called = True
            if not isinstance(metadata.get("errorCode"), str):
                state.skill_load_succeeded = True
    return state
