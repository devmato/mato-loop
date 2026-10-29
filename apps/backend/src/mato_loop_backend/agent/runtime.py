from __future__ import annotations

import asyncio
import json
import traceback
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID, uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

import mato_loop_backend.agent.run_support as agent_run_support
from mato_loop_backend.agent.activity import run_activity_label
from mato_loop_backend.agent.projection import (
    SummaryResult,
)
from mato_loop_backend.agent.run_contracts import (
    AgentRunError,
    ApprovalResolution,
    _VisibleResponse,
)
from mato_loop_backend.agent.run_support import (
    logger,
)
from mato_loop_backend.agent.run_timing import run_timing_payload, transition_run_status
from mato_loop_backend.agent.session_settings import RunSessionSettings, apply_run_session_settings
from mato_loop_backend.agent.subagents import (
    ceiling_settings,
    delegation_snapshot,
    integration_ceiling_ids,
    permission_origin,
    session_write_paths_ceiling,
    temporary_files_ceiling,
)
from mato_loop_backend.agent.title_generation import initial_session_title as _initial_session_title
from mato_loop_backend.db.session import AsyncSessionFactory
from mato_loop_backend.diagnostics.service import (
    diagnostic_access_active,
    schedule_diagnostic_event,
)
from mato_loop_backend.error_contract import ErrorDescriptor, describe_error, error_payload
from mato_loop_backend.events import run_events
from mato_loop_backend.execution import (
    DirectoryResult,
    ExecutionClient,
    ExecutionClientError,
    execution_agent_connections,
)
from mato_loop_backend.files.service import RootFileError, RootFileService
from mato_loop_backend.files.storage import SessionStorageError
from mato_loop_backend.integrations.registry import effective_execution_integration_settings
from mato_loop_backend.media.attachments import (
    AttachmentError,
    AttachmentInput,
    AttachmentService,
)
from mato_loop_backend.memory.episode_indexing import EpisodeIndexService
from mato_loop_backend.message_content import canonical_assistant_content
from mato_loop_backend.models import (
    AgentRun,
    ApprovalRequest,
    AutomationRun,
    ChatSession,
    ExecutionAgent,
    Message,
    PermissionGrant,
    QuestionRequest,
    TitleGenerationStatus,
    UserAccount,
)
from mato_loop_backend.notifications import NotificationService
from mato_loop_backend.permissions.coordinator import approval_coordinator, question_coordinator
from mato_loop_backend.permissions.policy import (
    PermissionDecision,
    PermissionPolicy,
    PermissionRequest,
)
from mato_loop_backend.providers.base import (
    ProviderImageInput,
    ToolCall,
)
from mato_loop_backend.providers.media import MediaService
from mato_loop_backend.settings import (
    AgentProfileUnavailable,
    AgentRuntimeSettings,
    AiSettings,
    InstanceLimits,
    SettingsStore,
    ToolExecutionPolicy,
    apply_session_ai,
    effective_model_pseudonymization,
    effective_model_trust,
    load_agent_profile,
    load_agent_profile_tools,
    load_effective_ai,
    load_effective_integrations,
    load_effective_tools,
)
from mato_loop_backend.settings.effective import filter_agent_profile_integrations
from mato_loop_backend.settings.models import (
    BUILD_PROFILE_ID,
    PLAN_PROFILE_ID,
    ModelConfig,
    TrustLevel,
)
from mato_loop_backend.tools.arguments import (
    ProtectedQuestionResolution,
    QuestionRequestArguments,
)
from mato_loop_backend.tools.catalog import (
    tool_registry,
)
from mato_loop_backend.tools.contracts import (
    ToolArgumentsEdited,
)
from mato_loop_backend.tools.display import tool_display
from mato_loop_backend.tools.registry import _tool_available


class AgentRuntime:
    _tasks: dict[UUID, asyncio.Task[None]] = {}
    _session_settings: dict[UUID, RunSessionSettings] = {}
    _title_tasks: dict[asyncio.Task[None], UUID] = {}
    _cancellable_runs: set[UUID] = set()
    _user_cancelled_runs: set[UUID] = set()
    _redirected_runs: set[UUID] = set()
    _continuations: dict[UUID, set[asyncio.Event]] = {}
    _pending_continuations: set[UUID] = set()
    _waiting_interactions: dict[UUID, tuple[UUID | None, asyncio.Future[Any]]] = {}
    _provider_active_runs: set[UUID] = set()
    _tool_active_runs: set[UUID] = set()
    _interrupted_runs: set[UUID] = set()

    @staticmethod
    async def _ensure_user_active(
        db: AsyncSession, user_id: UUID, *, for_update: bool = False
    ) -> None:
        statement = select(UserAccount.is_active).where(UserAccount.id == user_id)
        if for_update:
            # Admission must serialize without blocking foreign-key inserts for personal settings.
            statement = statement.with_for_update(key_share=True)
        active = await db.scalar(statement)
        if active is False or active is None:
            raise AgentRunError("auth.accountDisabled")

    @classmethod
    def _schedule_run(cls, run: AgentRun) -> asyncio.Task[None]:
        run_id = run.id
        existing = cls._tasks.get(run_id)
        if existing is not None and not existing.done():
            return existing
        task = asyncio.create_task(cls._run(run_id))
        cls._tasks[run_id] = task

        def forget_run(_task: asyncio.Task[None]) -> None:
            cls._tasks.pop(run_id, None)
            cls._session_settings.pop(run_id, None)

        task.add_done_callback(forget_run)
        return task

    @classmethod
    async def start(
        cls,
        db: AsyncSession,
        chat_session: ChatSession,
        content: str,
        *,
        run_id: UUID | None = None,
        attachments: list[AttachmentInput] | None = None,
        automation_run: AutomationRun | None = None,
        model_id_override: UUID | None = None,
        agent_profile_id_override: UUID | None = None,
        active_file: dict[str, Any] | None = None,
        source_type: str = "app",
        source_provider: str | None = None,
        source_endpoint_id: UUID | None = None,
    ) -> AgentRun:
        if (source_type, source_provider, source_endpoint_id) != ("app", None, None) and (
            source_type != "endpoint"
            or not source_provider
            or source_endpoint_id is None
        ):
            raise ValueError("invalid message source")
        await db.execute(select(UserAccount.id).where(UserAccount.id == chat_session.user_id).with_for_update(key_share=True))
        locked_session = await db.scalar(
            select(ChatSession)
            .where(ChatSession.id == chat_session.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if locked_session is None:
            raise AgentRunError("session.notFound")
        chat_session = locked_session
        from mato_loop_backend.sessions.archive import is_session_archived

        if await is_session_archived(db, chat_session):
            raise AgentRunError("session.archived")
        active_run = await db.scalar(
            select(AgentRun.id).where(
                AgentRun.session_id == chat_session.id,
                AgentRun.parent_run_id.is_(None),
                AgentRun.status.in_(("queued", "running", "waiting_approval", "waiting_input")),
            )
        )
        if active_run is not None:
            raise AgentRunError("session.runAlreadyActive")
        from mato_loop_backend.sessions.commands import parse_command, prepare_command
        from mato_loop_backend.sessions.git_commands import check_workspace_admission
        from mato_loop_backend.sessions.service import SessionServiceError

        await cls._ensure_user_active(db, chat_session.user_id, for_update=True)
        selected_command = parse_command(content)
        command_data = None
        if selected_command is not None:
            try:
                command_data = await prepare_command(db, chat_session, selected_command, content)
            except SessionServiceError as exc:
                raise AgentRunError(exc.code, params=exc.params) from exc
            if selected_command.mutates_workspace and (
                agent_profile_id_override or chat_session.agent_profile_id
            ) == PLAN_PROFILE_ID:
                agent_profile_id_override = BUILD_PROFILE_ID
        profile_id = agent_profile_id_override or chat_session.agent_profile_id
        from mato_loop_backend.settings.effective import session_agent_allowed

        if not session_agent_allowed(chat_session, profile_id):
            raise AgentRunError("agentProfile.selectedUnavailable")
        try:
            profile = await load_agent_profile(
                db, chat_session.user_id, profile_id
            )
        except AgentProfileUnavailable as exc:
            raise AgentRunError("agentProfile.selectedUnavailable") from exc
        if not profile.enabled:
            raise AgentRunError("agentProfile.selectedUnavailable")
        ai_settings = apply_session_ai(
            await load_effective_ai(db, chat_session.user_id), chat_session, profile.id
        )
        instance_limits = await SettingsStore.load(db, InstanceLimits)
        model_id = model_id_override or ai_settings.default_model_id
        model = agent_run_support._model_for_id(ai_settings, model_id)
        provider = agent_run_support._provider_for_model(ai_settings, model) if model is not None else None
        trust_level = effective_model_trust(provider, model) if provider and model else TrustLevel.PUBLIC
        if chat_session.trust_level is None:
            chat_session.trust_level = trust_level.value if provider and model else TrustLevel.PRIVATE.value
        pseudonymization_enabled = (
            provider is not None and model is not None
            and effective_model_pseudonymization(provider, model)
        )
        execution_agent = None
        current_directory = chat_session.current_directory
        execution_operations: list[str] = []
        if chat_session.preferred_execution_agent_id is not None:
            execution_agent = await db.scalar(
                select(ExecutionAgent).where(
                    ExecutionAgent.id == chat_session.preferred_execution_agent_id,
                    ExecutionAgent.user_id == chat_session.user_id,
                )
            )
            snapshot = execution_agent_connections.snapshot(
                chat_session.preferred_execution_agent_id
            ) if execution_agent is not None and execution_agent.revoked_at is None else None
            if execution_agent is None:
                raise AgentRunError("executionAgent.unavailable")
            if snapshot is not None and current_directory is not None:
                try:
                    directory = await ExecutionClient(
                        execution_agent.id, snapshot
                    ).request(
                        "filesystem.resolve",
                        {"path": current_directory},
                        DirectoryResult,
                    )
                except ExecutionClientError:
                    snapshot = None
                else:
                    assert isinstance(directory, DirectoryResult)
                    current_directory = directory.path
            execution_operations = sorted(snapshot) if snapshot is not None else []
        try:
            await check_workspace_admission(
                db, chat_session.user_id, chat_session.preferred_execution_agent_id,
                current_directory, command_data,
                metadata_directory=((chat_session.coding_workspace or {}).get("commonDirectory")
                                    if (chat_session.coding_workspace or {}).get("executionAgentId") == str(chat_session.preferred_execution_agent_id) else None),
            )
        except SessionServiceError as exc:
            raise AgentRunError(exc.code) from exc
        if agent_profile_id_override is not None and profile_id != chat_session.agent_profile_id:
            await db.execute(
                delete(PermissionGrant).where(PermissionGrant.session_id == chat_session.id)
            )
            chat_session.agent_profile_id = profile_id
        run_id = run_id or uuid4()
        message = Message(
            session_id=chat_session.id,
            role="user",
            content=content,
            trust_level=trust_level.value,
            provider_data={"_matoloop": {
                "executionAgentId": str(execution_agent.id) if execution_agent else None,
                **({"codingCommand": command_data} if command_data else {}),
                **({"source": {
                    "type": "automationRun", "automationId": str(automation_run.automation_id),
                    "runId": str(automation_run.id), "trigger": automation_run.trigger,
                    "automationName": automation_run.definition_snapshot.get("name"),
                }} if automation_run is not None else {}),
                **({"activeFile": active_file} if active_file is not None else {}),
            }},
            source_type=source_type,
            source_provider=source_provider,
            source_endpoint_id=source_endpoint_id,
        )
        agent_run_support._snapshot_tool_call_limit(message, run_id, profile.max_tool_calls if profile is not None else 25)
        run = AgentRun(
            id=run_id,
            session_id=chat_session.id,
            automation_origin=automation_run is not None,
            agent_profile_id=profile_id,
            status="queued",
            model_config_id=model_id,
            model_name=(model.name or model.model_id) if model is not None else None,
            trust_level=trust_level.value,
            pseudonymization_enabled=pseudonymization_enabled,
            execution_agent_id=(execution_agent.id if execution_agent else None),
            execution_agent_revision=(execution_agent.revision if execution_agent else None),
            current_directory_snapshot=current_directory,
            execution_operations=execution_operations,
        )
        db.add_all([message, run])
        generate_title = (
            chat_session.title == "Neue Session"
            and chat_session.title_generation_status
            == TitleGenerationStatus.PENDING.value
        )
        if generate_title:
            chat_session.title_generation_status = TitleGenerationStatus.RUNNING.value
        created_attachments = []
        attachment_service = AttachmentService(settings=instance_limits)

        async def rollback_and_discard() -> None:
            try:
                await db.rollback()
            except BaseException:
                logger.exception("Could not roll back failed attachment transaction")
            for attachment in created_attachments:
                try:
                    await attachment_service.discard(attachment, chat_session.id)
                except BaseException:
                    logger.exception(
                        "Could not discard attachment %s after failed transaction",
                        attachment.id,
                    )

        async def rollback_after_uncertain_commit() -> None:
            try:
                await db.rollback()
            except BaseException:
                logger.exception("Could not roll back uncertain attachment transaction")

        try:
            await cls._ensure_user_active(db, chat_session.user_id, for_update=True)
            await db.flush()
            run.input_message_id = message.id
            if automation_run is not None:
                automation_run.agent_run_id = run.id
                automation_run.status = "running"
                automation_run.started_at = datetime.now(UTC)
            await EpisodeIndexService.enqueue(db, message)
            for position, attachment in enumerate(attachments or []):
                created_attachments.append(
                    await attachment_service.create_attachment(
                        db,
                        message,
                        attachment.data,
                        attachment.mime_type,
                        kind=attachment.kind,
                        filename=attachment.filename,
                        position=position,
                        metadata=attachment.metadata,
                        category="user_upload",
                    )
                )
            if chat_session.title == "Neue Session":
                initial_content = content.strip() or ", ".join(
                    attachment.filename or "attachment"
                    for attachment in created_attachments
                )
                chat_session.title = _initial_session_title(initial_content)
        except BaseException:
            await rollback_and_discard()
            raise
        commit_task = asyncio.get_running_loop().create_task(db.commit())
        cancelled = False
        try:
            await asyncio.shield(commit_task)
        except asyncio.CancelledError:
            try:
                await commit_task
            except BaseException:
                await rollback_after_uncertain_commit()
                raise
            cancelled = True
        except BaseException:
            await rollback_after_uncertain_commit()
            raise
        run_task = cls._schedule_run(run)
        if generate_title:
            title_task = asyncio.create_task(
                cls._generate_title(
                    run.id,
                    chat_session.id,
                    message.id,
                    chat_session.title,
                    after=run_task,
                )
            )
            cls._title_tasks[title_task] = chat_session.user_id
            title_task.add_done_callback(lambda task: cls._title_tasks.pop(task, None))
        if cancelled:
            raise asyncio.CancelledError
        return run

    @classmethod
    async def retry(
        cls,
        db: AsyncSession,
        chat_session: ChatSession,
        failed_run: AgentRun,
        *,
        before_execution: bool = False,
    ) -> AgentRun:
        from mato_loop_backend.sessions.archive import is_session_archived

        if await is_session_archived(db, chat_session):
            raise AgentRunError("session.archived")
        if failed_run.session_id != chat_session.id or failed_run.status != "failed":
            raise AgentRunError("session.retryUnavailable")
        await cls._ensure_user_active(db, chat_session.user_id, for_update=True)
        ai_settings = apply_session_ai(await load_effective_ai(db, chat_session.user_id), chat_session)
        model_id = ai_settings.default_model_id if before_execution else failed_run.model_config_id
        agent_id = chat_session.preferred_execution_agent_id if before_execution else failed_run.execution_agent_id
        model = agent_run_support._model_for_id(ai_settings, model_id)
        provider = agent_run_support._provider_for_model(ai_settings, model) if model is not None else None
        snapshot = (
            execution_agent_connections.snapshot(agent_id)
            if agent_id is not None
            else None
        )
        current_directory = chat_session.current_directory
        execution_agent = (
            await db.scalar(
                select(ExecutionAgent).where(
                    ExecutionAgent.id == agent_id,
                    ExecutionAgent.user_id == chat_session.user_id,
                )
            )
            if agent_id is not None
            else None
        )
        if agent_id is not None and execution_agent is None:
            raise AgentRunError("executionAgent.unavailable")
        if execution_agent is not None and execution_agent.revoked_at is not None:
            snapshot = None
        if snapshot is not None and current_directory is not None:
            try:
                directory = await ExecutionClient(
                    agent_id,
                    snapshot,
                ).request(
                    "filesystem.resolve",
                    {"path": current_directory},
                    DirectoryResult,
                )
            except ExecutionClientError:
                snapshot = None
            else:
                assert isinstance(directory, DirectoryResult)
                current_directory = directory.path
        run = AgentRun(
            id=uuid4(),
            session_id=chat_session.id,
            automation_origin=bool(failed_run.automation_origin),
            agent_profile_id=chat_session.agent_profile_id,
            input_message_id=failed_run.input_message_id,
            status="queued",
            model_config_id=model_id,
            model_name=(model.name or model.model_id) if before_execution and model else failed_run.model_name,
            trust_level=(effective_model_trust(provider, model).value if provider and model else TrustLevel.PUBLIC.value) if before_execution else failed_run.trust_level,
            pseudonymization_enabled=(effective_model_pseudonymization(provider, model) if provider and model else False) if before_execution else failed_run.pseudonymization_enabled,
            execution_agent_id=agent_id,
            execution_agent_revision=execution_agent.revision if before_execution and execution_agent else failed_run.execution_agent_revision,
            current_directory_snapshot=current_directory,
            execution_operations=sorted(snapshot) if snapshot is not None else [],
        )
        db.add(run)
        commit_task: asyncio.Task[None] | None = None
        try:
            await cls._ensure_user_active(db, chat_session.user_id, for_update=True)
            try:
                profile = (
                    await load_agent_profile(db, chat_session.user_id, run.agent_profile_id)
                    if run.agent_profile_id is not None else None
                )
            except AgentProfileUnavailable as exc:
                raise AgentRunError("agentProfile.selectedUnavailable") from exc
            if profile is not None and not profile.enabled:
                raise AgentRunError("agentProfile.selectedUnavailable")
            input_message = await db.get(Message, run.input_message_id) if run.input_message_id else None
            if not isinstance(input_message, Message):
                input_message = next((item for item in reversed(await cls._records(db, chat_session.id)) if item.role == "user"), None)
            if input_message is None:
                raise AgentRunError("session.retryUnavailable")
            metadata = (input_message.provider_data or {}).get("_matoloop", {})
            from mato_loop_backend.sessions.command_contracts import command_context
            from mato_loop_backend.sessions.commands import parse_command, prepare_command
            from mato_loop_backend.sessions.git_commands import check_workspace_admission
            from mato_loop_backend.sessions.service import SessionServiceError

            saved_command = metadata.get("codingCommand")
            try:
                if parse_command(input_message.content) is not None and not isinstance(saved_command, dict):
                    raise SessionServiceError(409, "git.worktreeChanged")
                if isinstance(saved_command, dict):
                    if agent_id != failed_run.execution_agent_id or current_directory != failed_run.current_directory_snapshot:
                        raise SessionServiceError(409, "git.worktreeChanged")
                    selected_command = parse_command("/" + saved_command["name"])
                    if selected_command is None:
                        raise SessionServiceError(409, "git.worktreeChanged")
                    fresh_command = await prepare_command(db, chat_session, selected_command, input_message.content)
                    saved_context = command_context(saved_command)
                    if not saved_context or any(fresh_command["context"].get(key) != value
                                                for key, value in saved_context.items()):
                        raise SessionServiceError(409, "git.worktreeChanged")
                    saved_command = {**saved_command, "name": selected_command.name, "resources": fresh_command["resources"],
                                     "context": fresh_command["context"]}
                    metadata = {**metadata, "codingCommand": saved_command}
                    input_message.provider_data = {**(input_message.provider_data or {}), "_matoloop": metadata}
                await check_workspace_admission(db, chat_session.user_id, agent_id, current_directory,
                                                saved_command, exclude_run_id=run.id,
                                                metadata_directory=((chat_session.coding_workspace or {}).get("commonDirectory")
                                                                    if (chat_session.coding_workspace or {}).get("executionAgentId") == str(agent_id) else None))
            except SessionServiceError as exc:
                raise AgentRunError(exc.code) from exc
            if not before_execution:
                input_message.provider_data = {
                    **(input_message.provider_data or {}),
                    "_matoloop": {**metadata, "executionStarted": True},
                }
            if "maxToolCalls" in metadata and str(failed_run.id) not in metadata.get("maxToolCallsByRun", {}):
                agent_run_support._snapshot_tool_call_limit(input_message, failed_run.id, metadata["maxToolCalls"])
            agent_run_support._snapshot_tool_call_limit(input_message, run.id, profile.max_tool_calls if profile is not None else 25)
            run.input_message_id = input_message.id
            await db.flush()
            commit_task = asyncio.create_task(db.commit())
            await asyncio.shield(commit_task)
        except asyncio.CancelledError:
            if commit_task is None:
                await db.rollback()
                raise
            try:
                await commit_task
            except BaseException:
                await db.rollback()
                raise
            cls._schedule_run(run)
            return run
        except BaseException:
            await db.rollback()
            raise
        cls._schedule_run(run)
        return run

    @classmethod
    def cancel(cls, run_id: UUID, *, redirected: bool = False) -> bool:
        task = cls._tasks.get(run_id)
        if task is None or task.done():
            return False
        cls._user_cancelled_runs.add(run_id)
        if redirected:
            cls._redirected_runs.add(run_id)
        if (
            run_id in cls._cancellable_runs
            and (not redirected or run_id not in cls._tool_active_runs)
            and task.cancelling() == 0
        ):
            task.cancel()
        return True

    @classmethod
    def reserve_continuation(cls, run_id: UUID, *, queued: bool = False) -> asyncio.Event | None:
        task = cls._tasks.get(run_id)
        if task is None or task.done() or run_id in cls._user_cancelled_runs:
            return None
        if queued or run_id in cls._cancellable_runs or run_id in cls._continuations:
            reservation = asyncio.Event()
            cls._continuations.setdefault(run_id, set()).add(reservation)
            return reservation
        return None

    @classmethod
    def finish_continuation(cls, run_id: UUID, reservation: asyncio.Event, *, accepted: bool) -> None:
        if accepted:
            cls._pending_continuations.add(run_id)
            for waiting_run_id, (parent_run_id, waiter) in tuple(cls._waiting_interactions.items()):
                if waiting_run_id == run_id and not waiter.done():
                    waiter.set_result("superseded")
                elif parent_run_id == run_id:
                    cls.cancel(waiting_run_id)
        cls._continuations.get(run_id, set()).discard(reservation)
        if not cls._continuations.get(run_id):
            cls._continuations.pop(run_id, None)
        reservation.set()
        task = cls._tasks.get(run_id)
        if accepted and run_id in cls._provider_active_runs and task and not task.done() and task.cancelling() == 0:
            task.cancel()

    @classmethod
    async def _await_continuations(cls, run_id: UUID) -> bool:
        reservations = tuple(cls._continuations.get(run_id, ()))
        if reservations:
            await asyncio.gather(*(reservation.wait() for reservation in reservations))
        if run_id in cls._pending_continuations:
            cls._pending_continuations.discard(run_id)
            return True
        return False

    @classmethod
    async def _resume_continuation(
        cls, db: AsyncSession, run: AgentRun, visible_response: _VisibleResponse
    ) -> None:
        run_id = run.id
        session_id = run.session_id
        await db.rollback()
        await db.refresh(run)
        session = await db.get(ChatSession, session_id)
        if session is not None:
            await db.refresh(session)
        content = canonical_assistant_content(visible_response.content)
        if content or visible_response.reasoning:
            received_at = await db.scalar(select(Message.created_at).where(
                Message.session_id == session_id,
                Message.role == "user",
                Message.provider_data["_matoloop"]["continuationRunId"].as_string() == str(run_id),
            ).order_by(Message.created_at.desc()).limit(1))
            assistant = Message(
                session_id=run.session_id,
                role="assistant",
                content=content,
                model_name=run.model_name,
                trust_level=run.trust_level,
                public_reasoning=visible_response.reasoning,
                provider_data={"_matoloop": agent_run_support._run_metadata(run, continuationSegment=True)},
                created_at=(received_at - timedelta(microseconds=1)
                            if isinstance(received_at, datetime) else datetime.now(UTC)),
            )
            db.add(assistant)
            await db.commit()
            await run_events.publish(run_id, {
                "type": "message.completed",
                "message": {
                    "id": str(assistant.id), "role": "assistant", "content": content,
                    "createdAt": assistant.created_at.isoformat(),
                    "modelName": assistant.model_name, "runId": str(run_id),
                    "agentProfileId": str(run.agent_profile_id) if run.agent_profile_id else None,
                    "continuationSegment": True,
                    "reasoning": assistant.public_reasoning,
                },
            })
        else:
            await run_events.publish(run_id, {"type": "message.continued"})
        visible_response.reset()
        await cls._transcribe_continuation_messages(db, run)

    @classmethod
    async def cancel_and_wait(cls, run_id: UUID) -> bool:
        task = cls._tasks.get(run_id)
        if task is None or task.done():
            return False
        cls._user_cancelled_runs.add(run_id)
        if run_id in cls._cancellable_runs and task.cancelling() == 0:
            task.cancel()
        await asyncio.shield(asyncio.gather(task, return_exceptions=True))
        return True

    @classmethod
    async def cancel_descendants(cls, session_id: UUID) -> None:
        from mato_loop_backend.sessions.cleanup import descendant_sessions

        async with AsyncSessionFactory() as db:
            descendants = await descendant_sessions(db, [session_id])
            if not descendants:
                return
            run_ids = list((await db.scalars(
                select(AgentRun.id).where(
                    AgentRun.session_id.in_([child_id for child_id, _ in descendants]),
                    AgentRun.parent_run_id.is_(None),
                    AgentRun.status.in_(("queued", "running", "waiting_approval", "waiting_input")),
                )
            )).all())
        # Close the read transaction before joining tasks that may need the same rows.
        joined = asyncio.gather(*(cls.cancel_and_wait(run_id) for run_id in run_ids))
        try:
            await asyncio.shield(joined)
        except asyncio.CancelledError:
            await joined
            raise
        async with AsyncSessionFactory() as db:
            for run_id in run_ids:
                run = await db.get(AgentRun, run_id)
                if run is None or run.status not in {"queued", "running", "waiting_approval", "waiting_input"}:
                    continue
                if run.agent_profile_id is None:
                    session = await db.get(ChatSession, run.session_id)
                    if isinstance(session, ChatSession):
                        run.agent_profile_id = session.agent_profile_id
                # An orphaned persisted run is terminalized, never scheduled or replayed.
                await cls._terminalize_incomplete_tool_calls(db, run.session_id, run_id=run.id)
                await cls._finalize_run_files(db, run, tolerate_conflict=True)
                transition_run_status(run, "cancelled")
                run.completed_at = datetime.now(UTC)
                message = Message(
                    session_id=run.session_id, role="assistant", content="",
                    model_name=run.model_name, trust_level=run.trust_level or "public",
                    generation_status="interrupted",
                    provider_data={"_matoloop": agent_run_support._run_metadata(run)},
                )
                db.add(message)
                await db.flush()
                run.result_message_id = message.id
                await cls._persist_run_duration(db, run)
                await db.execute(update(ApprovalRequest).where(
                    ApprovalRequest.run_id == run.id, ApprovalRequest.status == "pending"
                ).values(status="expired", resolved_at=datetime.now(UTC)))
                await db.execute(update(QuestionRequest).where(
                    QuestionRequest.run_id == run.id, QuestionRequest.status == "pending"
                ).values(status="expired"))
                await db.commit()
                await run_events.publish(run.id, {
                    "type": "run.cancelled",
                    "runId": str(run.id),
                    "agentProfileId": (
                        str(run.agent_profile_id) if run.agent_profile_id else None
                    ),
                    **run_timing_payload(run),
                })

    @classmethod
    async def cancel_and_wait_user_title_tasks(cls, user_id: UUID) -> bool:
        tasks = [
            task
            for task, owner_id in cls._title_tasks.items()
            if owner_id == user_id and not task.done()
        ]
        for task in tasks:
            if task.cancelling() == 0:
                task.cancel()
        if tasks:
            await asyncio.shield(asyncio.gather(*tasks, return_exceptions=True))
        return bool(tasks)

    @classmethod
    async def shutdown(cls) -> None:
        tasks = [
            task
            for task in [*cls._tasks.values(), *cls._title_tasks]
            if not task.done()
        ]
        for run_id, task in tuple(cls._tasks.items()):
            if not task.done():
                cls._interrupted_runs.add(run_id)
                if run_id in cls._cancellable_runs and task.cancelling() == 0:
                    task.cancel()
        for task in cls._title_tasks:
            if task.cancelling() == 0:
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    @classmethod
    async def _transcribe_voice_messages(
        cls, db: AsyncSession, run: AgentRun, message_id: UUID | None = None
    ) -> None:
        message_id = message_id or run.input_message_id
        if message_id is None:
            return
        message = await db.scalar(
            select(Message)
            .where(Message.id == message_id)
            .options(selectinload(Message.attachments))
        )
        if message is None or message.role != "user" or message.session_id != run.session_id:
            return
        voices = [
            attachment for attachment in message.attachments
            if attachment.kind == "audio"
            and (attachment.metadata_json or {}).get("voiceMessage") is True
            and not (attachment.metadata_json or {}).get("voiceTranscriptionStatus")
        ]
        if not voices:
            return
        chat_session = await db.get(ChatSession, run.session_id)
        if chat_session is None:
            raise AgentRunError("session.notFound")
        user_id = chat_session.user_id
        service = AttachmentService()
        for attachment in voices:
            await cls._ensure_user_active(db, user_id)
            tools = await load_effective_tools(db, user_id)
            await db.commit()
            text = ""
            status = "unavailable"
            # A marked recording is a user transcription request, like direct dictation.
            # Model-initiated transcription still uses the normal permission loop.
            if tools.tool_availability.get("transcribe_audio", True):
                status = "failed"
                try:
                    audio = await service.read_audio(attachment, run.session_id, current_only=True)
                    result = await MediaService.transcribe_audio(
                        db, audio=audio, language=None,
                        timeout=tools.transcribe_audio.timeout_seconds,
                        user_id=user_id,
                    )
                    text = result.text.strip()
                    if not text or len(message.content) + len(text) + 2 > 200_000:
                        text = ""
                    else:
                        status = "completed"
                except SQLAlchemyError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "Voice transcription failed for attachment %s (%s)",
                        attachment.id, type(exc).__name__,
                    )
            await cls._ensure_user_active(db, user_id, for_update=True)
            if text:
                message.content = "\n\n".join(part for part in (message.content, text) if part)
                await EpisodeIndexService.enqueue(db, message)
            # Commit the text and terminal status together; run retries never append twice.
            attachment.metadata_json = {
                **attachment.metadata_json, "voiceTranscriptionStatus": status,
            }
            await db.commit()
            await run_events.publish(
                run.id, {"type": "message.updated", "messageId": str(message.id)}
            )

    @classmethod
    async def _transcribe_continuation_messages(cls, db: AsyncSession, run: AgentRun) -> None:
        message_ids = (await db.scalars(select(Message.id).where(
            Message.session_id == run.session_id,
            Message.role == "user",
            Message.provider_data["_matoloop"]["continuationRunId"].as_string() == str(run.id),
        ))).all()
        for message_id in message_ids:
            await cls._transcribe_voice_messages(db, run, message_id)

    @classmethod
    async def _prepare_run(cls, db: AsyncSession, run: AgentRun) -> None:
        """Check execution prerequisites before any model or tool work."""
        session = await db.get(ChatSession, run.session_id)
        if session is None:
            raise AgentRunError("session.notFound")
        apply_run_session_settings(session, run)
        try:
            profile = await load_agent_profile(db, session.user_id, run.agent_profile_id or session.agent_profile_id)
        except AgentProfileUnavailable as exc:
            raise AgentRunError("agentProfile.selectedUnavailable") from exc
        if not profile.enabled:
            raise AgentRunError("agentProfile.selectedUnavailable")
        agent = await db.get(ExecutionAgent, run.execution_agent_id) if run.execution_agent_id is not None else None
        if run.execution_agent_id is not None and (agent is None or agent.user_id != session.user_id or agent.revoked_at is not None):
            raise AgentRunError("executionAgent.unavailable")
        if (profile.base_profile_id or profile.id) in {PLAN_PROFILE_ID, BUILD_PROFILE_ID}:
            if run.execution_agent_id is None:
                raise AgentRunError("session.computerRequired")
            snapshot = execution_agent_connections.snapshot(agent.id)
            if snapshot is None:
                raise AgentRunError("session.computerOffline")
            required = {"filesystem.resolve", "filesystem.read", "filesystem.glob", "filesystem.grep"}
            if (profile.base_profile_id or profile.id) == BUILD_PROFILE_ID:
                required.add("filesystem.write")
            if not required.issubset(snapshot):
                raise AgentRunError("session.computerOperationsMissing")
            if not run.current_directory_snapshot:
                raise AgentRunError("session.directoryRequired")
            try:
                directory = await ExecutionClient(agent.id, snapshot).request(
                    "filesystem.resolve", {"path": run.current_directory_snapshot}, DirectoryResult,
                )
            except ExecutionClientError as exc:
                raise AgentRunError("session.directoryUnavailable") from exc
            run.current_directory_snapshot = directory.path
            run.execution_operations = sorted(snapshot)
            from mato_loop_backend.sessions.service import execution_filesystem_policy

            if execution_filesystem_policy(agent, directory.path) == "deny":
                raise AgentRunError("permission.denied")
        ai = apply_session_ai(await load_effective_ai(db, session.user_id), session, profile.id)
        model = agent_run_support._model_for_id(ai, run.model_config_id or ai.default_model_id)
        if model is None or agent_run_support._provider_for_model(ai, model) is None:
            raise AgentRunError("session.modelUnavailable")
        if not model.supports_tools:
            raise AgentRunError("session.modelToolsUnavailable")

    @classmethod
    async def _run(cls, run_id: UUID) -> None:
        try:
            async with AsyncSessionFactory() as db:
                run = await db.get(AgentRun, run_id)
                if (
                    run is None
                    or run.parent_run_id is not None
                    or run.status not in {"queued", "running"}
                ):
                    return
                session_id = run.session_id
                diagnostic_owner = await db.scalar(
                    select(ChatSession.user_id).where(ChatSession.id == session_id)
                )
                diagnostics_active = isinstance(
                    diagnostic_owner, UUID
                ) and await diagnostic_access_active(db, diagnostic_owner)
                run._diagnostics_active = diagnostics_active
                if run.agent_profile_id is None:
                    session = await db.get(ChatSession, session_id)
                    if isinstance(session, ChatSession) and session.agent_profile_id is not None:
                        run.agent_profile_id = session.agent_profile_id
                        await db.commit()
                visible_response = _VisibleResponse()
                run_completed = False
                before_execution = True
                cls._cancellable_runs.add(run_id)
                try:
                    if run_id in cls._user_cancelled_runs:
                        raise asyncio.CancelledError
                    if run_id in cls._interrupted_runs:
                        raise asyncio.CancelledError
                    transition_run_status(run, "running")
                    await db.commit()
                    if diagnostics_active:
                        schedule_diagnostic_event(
                            diagnostic_owner,
                            "run.started",
                            component="agent",
                            session_id=session_id,
                            run_id=run.id,
                            payload={
                                "modelName": run.model_name,
                                "modelConfigId": str(run.model_config_id)
                                if run.model_config_id
                                else None,
                                "agentProfileId": str(run.agent_profile_id)
                                if run.agent_profile_id
                                else None,
                                "trustLevel": run.trust_level,
                                "executionAgentId": str(run.execution_agent_id)
                                if run.execution_agent_id
                                else None,
                                "currentDirectory": run.current_directory_snapshot,
                            },
                        )
                    await run_events.publish(
                        run_id,
                        {
                            "type": "run.started",
                            "runId": str(run_id),
                            "modelName": run.model_name,
                            "agentProfileId": (
                                str(run.agent_profile_id) if run.agent_profile_id else None
                            ),
                            "createdAt": run.created_at.isoformat() if run.created_at else None,
                            "activityLabel": run_activity_label("preparing_context"),
                            **run_timing_payload(run),
                        },
                    )
                    input_message = await db.get(Message, run.input_message_id) if run.input_message_id else None
                    input_metadata = (input_message.provider_data or {}).get("_matoloop", {}) if isinstance(input_message, Message) else {}
                    before_execution = input_metadata.get("executionStarted") is not True
                    await cls._prepare_run(db, run)
                    before_execution = False
                    if isinstance(input_message, Message):
                        input_message.provider_data = {
                            **(input_message.provider_data or {}),
                            "_matoloop": {**input_metadata, "executionStarted": True},
                        }
                        await db.commit()
                    await cls._transcribe_voice_messages(db, run)
                    await cls._execute_loop(db, run, visible_response)
                    await cls._finalize_run_files(db, run)
                    transition_run_status(run, "completed")
                    run.completed_at = datetime.now(UTC)
                    await cls._persist_run_duration(db, run)
                    from mato_loop_backend.automations import AutomationService

                    await AutomationService.enqueue_event(db, run, "agent_response")
                    if run.result_message_id is not None:
                        result = await db.get(Message, run.result_message_id)
                        if result is not None:
                            await NotificationService.create_for_message(db, result)
                    await db.commit()
                    run_completed = True
                    cls._cancellable_runs.discard(run_id)
                    if diagnostics_active:
                        schedule_diagnostic_event(
                            diagnostic_owner,
                            "run.completed",
                            component="agent",
                            session_id=session_id,
                            run_id=run.id,
                            payload=run_timing_payload(run),
                        )
                    await run_events.publish(
                        run_id,
                        {
                            "type": "run.completed",
                            "runId": str(run_id),
                            "agentProfileId": (
                                str(run.agent_profile_id) if run.agent_profile_id else None
                            ),
                            **run_timing_payload(run),
                        },
                    )
                    await NotificationService.deliver_message_after_commit(run.result_message_id)
                except asyncio.CancelledError:
                    if run_completed:
                        raise
                    user_cancelled = run_id in cls._user_cancelled_runs
                    await db.rollback()
                    await cls._terminalize_incomplete_tool_calls(db, session_id, run_id=run_id)
                    cancelled_run = await db.get(AgentRun, run_id)
                    interrupted_message = None
                    error_message = None
                    restart_error = None
                    if cancelled_run is not None:
                        await cls._finalize_run_files(db, cancelled_run, tolerate_conflict=True)
                        cancelled_run.completed_at = datetime.now(UTC)
                        if user_cancelled:
                            transition_run_status(cancelled_run, "cancelled")
                            reasoning = dict(visible_response.reasoning or {}) or None
                            if reasoning is not None:
                                reasoning["status"] = "interrupted"
                            metadata = agent_run_support._run_metadata(
                                cancelled_run, redirected=run_id in cls._redirected_runs
                            )
                            if visible_response.warnings:
                                metadata["warnings"] = list(visible_response.warnings)
                            interrupted_message = Message(
                                session_id=session_id,
                                role="assistant",
                                content=canonical_assistant_content(visible_response.content),
                                model_name=cancelled_run.model_name,
                                generation_status="interrupted",
                                trust_level=cancelled_run.trust_level,
                                public_reasoning=reasoning,
                                provider_data={"_matoloop": metadata},
                                created_at=datetime.now(UTC),
                            )
                            db.add(interrupted_message)
                            await db.flush()
                            cancelled_run.result_message_id = interrupted_message.id
                        else:
                            restart_error = describe_error("agent.backendRestarted")
                            transition_run_status(cancelled_run, "failed")
                            cancelled_run.error = restart_error.code
                            error_message = await cls._persist_error_message(
                                db, cancelled_run, restart_error
                            )
                        await cls._persist_run_duration(db, cancelled_run)
                        await db.commit()
                        if error_message is not None:
                            await NotificationService.deliver_message_after_commit(error_message.id)
                        if diagnostics_active:
                            schedule_diagnostic_event(
                                diagnostic_owner,
                                "run.cancelled" if user_cancelled else "run.failed",
                                component="agent",
                                session_id=session_id,
                                run_id=run_id,
                                payload={
                                    "error": None
                                    if user_cancelled
                                    else (
                                        restart_error
                                        or describe_error("agent.backendRestarted")
                                    ).payload()
                                },
                            )
                    if error_message is not None and restart_error is not None:
                        await run_events.publish(
                            run_id,
                            {
                                "type": "message.completed",
                                "message": cls._error_message_event(error_message, restart_error),
                            },
                        )
                    elif interrupted_message is not None:
                        await run_events.publish(
                            run_id,
                            {
                                "type": "message.interrupted",
                                "message": {
                                    "id": str(interrupted_message.id),
                                    "role": "assistant",
                                    "content": interrupted_message.content,
                                    "createdAt": interrupted_message.created_at.isoformat(),
                                    "modelName": interrupted_message.model_name,
                                    "generationStatus": "interrupted",
                                    "redirected": run_id in cls._redirected_runs,
                                    "toolCalls": None,
                                    "reasoning": interrupted_message.public_reasoning,
                                    "warnings": list(visible_response.warnings),
                                    "activeDurationMs": cancelled_run.active_duration_ms,
                                    "runId": str(cancelled_run.id),
                                    "agentProfileId": (
                                        str(cancelled_run.agent_profile_id)
                                        if cancelled_run.agent_profile_id
                                        else None
                                    ),
                                },
                            },
                        )
                    if user_cancelled:
                        await run_events.publish(
                            run_id,
                            {
                                "type": "run.cancelled",
                                "runId": str(run_id),
                                "agentProfileId": (
                                    str(cancelled_run.agent_profile_id)
                                    if cancelled_run is not None
                                    and cancelled_run.agent_profile_id
                                    else None
                                ),
                                **(
                                    run_timing_payload(cancelled_run)
                                    if cancelled_run is not None
                                    else {}
                                ),
                            },
                        )
                    else:
                        restart_error = restart_error or describe_error(
                            "agent.backendRestarted"
                        )
                        await run_events.publish(
                            run_id,
                            {
                                "type": "run.failed",
                                "runId": str(run_id),
                                "error": restart_error.payload(),
                                "agentProfileId": (
                                    str(cancelled_run.agent_profile_id)
                                    if cancelled_run is not None
                                    and cancelled_run.agent_profile_id
                                    else None
                                ),
                                **(
                                    run_timing_payload(cancelled_run)
                                    if cancelled_run is not None
                                    else {}
                                ),
                            },
                        )
                    raise
                except Exception as exc:
                    if run_completed:
                        logger.exception("Post-run task for agent run %s failed", run_id)
                        return
                    logger.exception("Agent run %s failed", run_id)
                    public_error = agent_run_support._public_run_error(exc)
                    error_message = None
                    failed_run = None
                    failure_committed = False
                    try:
                        await db.rollback()
                        await cls._terminalize_incomplete_tool_calls(db, session_id, run_id=run_id)
                        failed_run = await db.get(AgentRun, run_id)
                        if failed_run is not None:
                            await cls._finalize_run_files(db, failed_run, tolerate_conflict=True)
                            transition_run_status(failed_run, "failed")
                            failed_run.error = public_error.code
                            failed_run.completed_at = datetime.now(UTC)
                            error_message = await cls._persist_error_message(
                                db, failed_run, public_error
                            )
                            if before_execution:
                                error_message.provider_data = {"_matoloop": agent_run_support._run_metadata(failed_run, beforeExecution=True)}
                                session = await db.get(ChatSession, session_id)
                                if session is not None and session.title_generation_status == TitleGenerationStatus.RUNNING.value:
                                    session.title_generation_status = TitleGenerationStatus.SKIPPED.value
                            if visible_response.generation_diagnostics is not None:
                                diagnostics = visible_response.generation_diagnostics
                                runtime_data = diagnostics.get("_matoloop", {})
                                error_message.provider_data = {
                                    **diagnostics,
                                    "_matoloop": agent_run_support._run_metadata(failed_run, **runtime_data),
                                }
                            await cls._persist_run_duration(db, failed_run)
                            from mato_loop_backend.automations import AutomationService

                            await AutomationService.enqueue_event(db, failed_run, "agent_error")
                            await db.commit()
                            failure_committed = True
                            if diagnostics_active:
                                schedule_diagnostic_event(
                                    diagnostic_owner,
                                    "run.failed",
                                    component="agent",
                                    session_id=session_id,
                                    run_id=run_id,
                                    payload={
                                        "error": public_error.payload(),
                                        "exceptionClass": type(exc).__name__,
                                    },
                                )
                    except Exception:
                        logger.exception("Could not persist failure state for agent run %s", run_id)
                    if error_message is not None:
                        await run_events.publish(
                            run_id,
                            {
                                "type": "message.completed",
                                "message": cls._error_message_event(error_message, public_error),
                            },
                        )
                    await run_events.publish(
                        run_id,
                        {
                            "type": "run.failed",
                            "runId": str(run_id),
                            "error": public_error.payload(),
                            "agentProfileId": (
                                str(failed_run.agent_profile_id)
                                if failed_run is not None and failed_run.agent_profile_id
                                else None
                            ),
                            **(
                                run_timing_payload(failed_run)
                                if failed_run is not None
                                else {}
                            ),
                        },
                    )
                    if failure_committed and failed_run is not None:
                        await NotificationService.deliver_message_after_commit(failed_run.result_message_id)
        finally:
            cls._cancellable_runs.discard(run_id)
            cls._session_settings.pop(run_id, None)
            cls._user_cancelled_runs.discard(run_id)
            cls._redirected_runs.discard(run_id)
            cls._interrupted_runs.discard(run_id)
            cls._continuations.pop(run_id, None)
            cls._pending_continuations.discard(run_id)
            cls._provider_active_runs.discard(run_id)
            cls._tool_active_runs.discard(run_id)

    @classmethod
    async def _execute_loop(
        cls,
        db: AsyncSession,
        run: AgentRun,
        visible_response: _VisibleResponse | None = None,
    ) -> None:
        # Run-local only: newest eight images / 20 MiB, never restored on restart.
        ephemeral_images: dict[tuple[UUID, int, str, int], ProviderImageInput] = {}
        one_time_secrets: dict[UUID, str] = {}
        try:
            await cls._execute_loop_body(
                db, run, visible_response, ephemeral_images, one_time_secrets
            )
        except BaseException as exc:
            # Failed provider frames can retain raw and encoded image copies in locals.
            pending = [exc]
            seen = set()
            while pending:
                error = pending.pop()
                if id(error) in seen:
                    continue
                seen.add(id(error))
                traceback.clear_frames(error.__traceback__)
                pending.extend(
                    item for item in (error.__cause__, error.__context__) if item is not None
                )
            raise
        finally:
            for image in ephemeral_images.values():
                image.data = b""
            ephemeral_images.clear()
            one_time_secrets.clear()
            from mato_loop_backend.skill_resources import release_computers

            cleanup = asyncio.create_task(release_computers(run))
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    continue

    @classmethod
    async def _execute_loop_body(
        cls,
        db: AsyncSession,
        run: AgentRun,
        visible_response: _VisibleResponse | None,
        ephemeral_images: dict[tuple[UUID, int, str, int], ProviderImageInput],
        one_time_secrets: dict[UUID, str],
    ) -> None:
        from mato_loop_backend.agent.loop import execute_loop_body

        return await execute_loop_body(cls, db, run, visible_response, ephemeral_images, one_time_secrets)

    @staticmethod
    async def _records(db: AsyncSession, session_id: UUID) -> list[Message]:
        return list(
            (
                await db.scalars(
                    select(Message)
                    .options(selectinload(Message.attachments))
                    .where(
                        Message.session_id == session_id,
                        Message.error_code.is_(None),
                    )
                    .order_by(Message.created_at, Message.id)
                )
            ).all()
        )

    @staticmethod
    async def _terminalize_incomplete_tool_calls(
        db: AsyncSession, session_id: UUID, *, run_id: UUID
    ) -> None:
        assistant = await db.scalar(
            select(Message)
            .where(
                Message.session_id == session_id,
                Message.role == "assistant",
                Message.tool_calls.is_not(None),
            )
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(1)
        )
        if assistant is None or not assistant.tool_calls:
            return
        existing_ids = set(
            (
                await db.scalars(
                    select(Message.tool_call_id).where(
                        Message.session_id == session_id,
                        Message.role == "tool",
                        Message.tool_call_id.is_not(None),
                    )
                )
            ).all()
        )
        missing = [
            str(call["id"])
            for call in assistant.tool_calls
            if call.get("id") and str(call["id"]) not in existing_ids
        ]
        if not missing:
            return
        content = json.dumps(
            error_payload("tool.executionInterrupted"), ensure_ascii=False
        )
        db.add_all(
            [
                Message(
                    session_id=session_id,
                    role="tool",
                    content=content,
                    tool_call_id=tool_call_id,
                    trust_level=assistant.trust_level,
                    provider_data={"_matoloop": {"runId": str(run_id), "fileOutputsPending": True}},
                )
                for tool_call_id in missing
            ]
        )
        await db.flush()

    @staticmethod
    async def _persist_error_message(
        db: AsyncSession,
        run: AgentRun,
        error: ErrorDescriptor,
    ) -> Message:
        existing = await db.scalar(
            select(Message).where(Message.error_run_id == run.id).limit(1)
        )
        if existing is not None:
            provider_data = dict(existing.provider_data or {})
            runtime_data = provider_data.get("_matoloop")
            existing.provider_data = {
                **provider_data,
                "_matoloop": agent_run_support._run_metadata(
                    run,
                    **(runtime_data if isinstance(runtime_data, dict) else {}),
                ),
            }
            run.result_message_id = existing.id
            await db.flush()
            return existing
        message = Message(
            session_id=run.session_id,
            role="assistant",
            content="",
            model_name=run.model_name,
            trust_level=run.trust_level or TrustLevel.PUBLIC.value,
            error_code=error.code,
            error_params=error.params,
            error_run_id=run.id,
            provider_data={"_matoloop": agent_run_support._run_metadata(run)},
            created_at=datetime.now(UTC),
        )
        db.add(message)
        await db.flush()
        run.result_message_id = message.id
        await NotificationService.create_for_message(db, message)
        return message

    @staticmethod
    async def _persist_run_duration(db: AsyncSession, run: AgentRun) -> None:
        if run.result_message_id is None:
            return
        message = await db.get(Message, run.result_message_id)
        if not isinstance(message, Message):
            return
        provider_data = dict(message.provider_data or {})
        runtime_data = provider_data.get("_matoloop")
        runtime_data = dict(runtime_data) if isinstance(runtime_data, dict) else {}
        performance = runtime_data.get("performance")
        performance = dict(performance) if isinstance(performance, dict) else {}
        performance["activeDurationMs"] = max(0, run.active_duration_ms or 0)
        message.provider_data = {
            **provider_data,
            "_matoloop": {**runtime_data, "performance": performance},
        }

    @staticmethod
    def _error_message_event(message: Message, error: ErrorDescriptor) -> dict[str, Any]:
        return {
            "id": str(message.id),
            "role": "assistant",
            "kind": "runtime_error",
            "state": "error",
            "origin": "host",
            "content": error.message,
            "createdAt": message.created_at.isoformat(),
            "modelName": message.model_name,
            "generationStatus": message.generation_status or "completed",
            "toolCalls": None,
            "reasoning": None,
            "error": error.payload(),
            "runId": str(message.error_run_id) if message.error_run_id else None,
            "agentProfileId": (
                (message.provider_data or {}).get("_matoloop", {}).get("agentProfileId")
            ),
        }

    @classmethod
    async def _finalize_run_files(
        cls, db: AsyncSession, run: AgentRun, *, tolerate_conflict: bool = False
    ) -> bool:
        session = await db.get(ChatSession, run.session_id)
        if session is None:
            return True
        if session.parent_session_id is not None:
            return True
        if not tolerate_conflict:
            limits = await SettingsStore.load(db, InstanceLimits)
            await AttachmentService(settings=limits).finalize_run(db, session, run.id)
            return True
        try:
            async with db.begin_nested():
                limits = await SettingsStore.load(db, InstanceLimits)
                await AttachmentService(settings=limits).finalize_run(db, session, run.id)
        except (AttachmentError, RootFileError, SessionStorageError, OSError):
            logger.exception(
                "Run %s file finalization failed; attachment publication remains pending", run.id
            )
            return False
        return True

    @classmethod
    async def recover(cls, db: AsyncSession) -> int:
        runs = list(
            (
                await db.scalars(
                    select(AgentRun)
                    .where(
                        AgentRun.status.in_(
                            ["queued", "running", "waiting_approval", "waiting_input"]
                        )
                    )
                    .with_for_update()
                )
            ).all()
        )
        error = describe_error("agent.backendRestarted")
        finalized = set()
        for run in runs:
            if await cls._finalize_run_files(db, run, tolerate_conflict=True):
                finalized.add(run.id)
            duration_complete = run.status != "running"
            if run.parent_run_id is not None:
                run.active_started_at = None
                transition_run_status(run, "failed")
                run.error = error.code
                run.internal_result = json.dumps({"error": error.payload()}, ensure_ascii=False)
                run.completed_at = datetime.now(UTC)
                continue
            await cls._terminalize_incomplete_tool_calls(db, run.session_id, run_id=run.id)
            run.active_started_at = None
            transition_run_status(run, "failed")
            run.error = error.code
            run.completed_at = datetime.now(UTC)
            await cls._persist_error_message(db, run, error)
            if duration_complete:
                await cls._persist_run_duration(db, run)
        sessions = (await db.scalars(select(ChatSession))).all()
        for session in sessions:
            files = RootFileService(session.id, project_id=session.project_id)
            try:
                pending_ids = set(await asyncio.to_thread(files.pending_run_ids))
            except SessionStorageError as exc:
                if exc.code != "sessionStorage.sessionNotFound":
                    logger.warning(
                        "Cannot discover pending run files for session %s (%s)", session.id, exc.code
                    )
                pending_ids = set()
            pending_messages = (
                await db.scalars(
                    select(Message).where(
                        Message.session_id == session.id,
                        Message.provider_data["_matoloop"]["fileOutputsPending"].astext == "true",
                    )
                )
            ).all()
            for message in pending_messages:
                state = (message.provider_data or {}).get("_matoloop", {})
                if state.get("pendingAttachments") or state.get("fileActions"):
                    pending_ids.add(UUID(state["runId"]))
            for run_id in pending_ids:
                if run_id in finalized:
                    continue
                run = await db.get(AgentRun, run_id)
                if run is not None and run.session_id == session.id:
                    await cls._finalize_run_files(db, run, tolerate_conflict=True)
        await db.flush()
        return len(runs)

    @classmethod
    async def _summarize(
        cls,
        source: str,
        output_tokens: int,
        *,
        ai_settings: AiSettings,
        active_model: ModelConfig,
        runtime_settings: AgentRuntimeSettings,
        on_update: Callable[[str, bool], Awaitable[None]] | None = None,
    ) -> SummaryResult:
        from mato_loop_backend.agent.summarization import summarize

        return await summarize(cls, source, output_tokens, ai_settings=ai_settings, active_model=active_model, runtime_settings=runtime_settings, on_update=on_update)

    @staticmethod
    async def _generate_title(
        run_id: UUID,
        session_id: UUID,
        input_message_id: UUID,
        expected_title: str,
        *,
        after: asyncio.Task[None] | None = None,
    ) -> None:
        from mato_loop_backend.agent.titles import generate_title

        return await generate_title(run_id, session_id, input_message_id, expected_title, after=after)

    @staticmethod
    async def _fail_title_generation(db: AsyncSession, session_id: UUID) -> None:
        try:
            await db.rollback()
            await db.execute(
                update(ChatSession)
                .where(
                    ChatSession.id == session_id,
                    ChatSession.title_generation_status
                    == TitleGenerationStatus.RUNNING.value,
                )
                .values(title_generation_status=TitleGenerationStatus.FAILED.value)
            )
            await db.commit()
        except Exception:
            logger.warning(
                "Title status for session %s could not be saved",
                session_id,
                exc_info=True,
            )

    @classmethod
    async def _authorize(
        cls,
        db: AsyncSession,
        run: AgentRun,
        chat_session: ChatSession,
        call: ToolCall,
        request: PermissionRequest,
        root_files: RootFileService,
        approval_resolution: dict[str, str] | None = None,
        *,
        check_only: bool = False,
    ) -> PermissionDecision:
        apply_run_session_settings(chat_session, run)
        origin = await permission_origin(db, chat_session)
        ceiling = await delegation_snapshot(db, run, chat_session)
        if ceiling is not None and (call.name == "task" or call.name not in ceiling["toolNames"]):
            return PermissionDecision.DENY
        snapshot = ceiling
        while snapshot is not None:
            defaults = (
                snapshot["integrationDefaults"].get(str(request.policy_integration_id), {})
                if request.policy_integration_id is not None else snapshot["actionDefaults"]
            )
            if request.capability not in defaults:
                return PermissionDecision.DENY
            snapshot = snapshot.get("offeredCeiling")
        permission_namespace = (
            {"permission_session_id": origin.id} if origin.id != chat_session.id else {}
        )
        agent_profile_id = run.agent_profile_id or chat_session.agent_profile_id
        try:
            tool_settings, _profile = await load_agent_profile_tools(
                db,
                chat_session.user_id,
                agent_profile_id,
                TrustLevel(run.trust_level or TrustLevel.PUBLIC.value),
            )
        except AgentProfileUnavailable:
            return PermissionDecision.DENY
        if not _tool_available(call.name, tool_settings):
            return PermissionDecision.DENY
        integration_settings = await load_effective_integrations(
            db, chat_session.user_id
        )
        integration_settings = filter_agent_profile_integrations(
            integration_settings, _profile, allowed_ids=integration_ceiling_ids(ceiling)
        )
        session_write_paths = getattr(_profile, "session_write_paths", None)
        ceiling_paths = session_write_paths_ceiling(ceiling)
        if ceiling_paths is not None:
            session_write_paths = (
                ceiling_paths
                if session_write_paths is None
                else sorted(set(session_write_paths) & set(ceiling_paths))
            )
        policy = agent_run_support._tool_policy(tool_settings, integration_settings, request)
        external_working_directory_policy = agent_run_support._external_working_directory_policy(tool_settings)
        sensitive_request = replace(
            request, capability="sensitive_data.access", resource_type="sensitive_data",
            policy_integration_id=None, policy_resource_key=None,
        )
        sensitive_data_policy = agent_run_support._tool_policy(tool_settings, integration_settings, sensitive_request)
        if ceiling is not None:
            ceiling_policy = agent_run_support._tool_policy(
                ceiling_settings(ceiling, TrustLevel(run.trust_level)), integration_settings, request,
                snapshot=ceiling,
            )
            policies = (ToolExecutionPolicy.AUTO, ToolExecutionPolicy.ASK, ToolExecutionPolicy.DISABLED)
            policy = max((policy, ceiling_policy), key=policies.index)
            external_working_directory_policy = max(
                (
                    external_working_directory_policy,
                    agent_run_support._external_working_directory_policy(
                        ceiling_settings(ceiling, TrustLevel(run.trust_level)), snapshot=ceiling,
                    ),
                ),
                key=policies.index,
            )
            sensitive_data_policy = max((
                sensitive_data_policy,
                agent_run_support._tool_policy(
                    ceiling_settings(ceiling, TrustLevel(run.trust_level)), integration_settings,
                    sensitive_request, snapshot=ceiling,
                ),
            ), key=policies.index)
        if request.sensitive_access and sensitive_data_policy == ToolExecutionPolicy.ASK:
            request = replace(request, available_decisions=("allow_once", "deny"))
        decision = await PermissionPolicy.evaluate(
            db,
            chat_session,
            request,
            root_files,
            policy,
            external_working_directory_policy=external_working_directory_policy,
            sensitive_data_policy=sensitive_data_policy,
            agent_profile_id=agent_profile_id,
            trust_level=TrustLevel(run.trust_level or TrustLevel.PUBLIC.value),
            session_write_paths=session_write_paths,
            temporary_files_enabled=tool_settings._temporary_files_enabled and temporary_files_ceiling(ceiling),
            run_id=run.id,
            **permission_namespace,
        )
        if (
            decision == PermissionDecision.ASK and request.action_kind == "browser_action"
            and not check_only
        ):
            from mato_loop_backend.integrations.browser.adapter import capture_browser_approval

            request = await capture_browser_approval(
                request, call, chat_session, run, approval_resolution,
            )
        action_grant = dict(
            session_id=origin.id, run_id=run.id, capability=request.capability,
            resource_type="run_action",
            resource_scope=agent_run_support._run_action_scope(request, call),
            agent_profile_id=agent_profile_id,
            trust_level=run.trust_level or TrustLevel.PUBLIC.value,
        )
        action_approved = decision == PermissionDecision.ASK and isinstance(
            await db.scalar(select(PermissionGrant.id).filter_by(**action_grant)), UUID
        )
        browser_approval_key = None
        if approval_resolution is not None and (
            request.resource_type == "execution_administrator"
            or request.action_kind.startswith("browser_") and (check_only or decision == PermissionDecision.ASK)
        ):
            browser_approval_key = ("administrator_approved:" if request.resource_type == "execution_administrator" else "browser_approved:") + json.dumps([
                str(run.id), call.id, call.name, str(chat_session.id), str(chat_session.user_id),
                str(origin.id), str(agent_profile_id), run.trust_level or TrustLevel.PUBLIC.value,
                request.capability, request.resource_type, request.resource_scope,
                request.use_integration_policy, str(request.policy_integration_id),
                request.policy_resource_key, request.sensitive_access,
            ])
        if action_approved:
            decision = PermissionDecision.ALLOW
            if browser_approval_key is not None:
                approval_resolution[browser_approval_key] = "allow_once"
        edited_approval = (
            approval_resolution.get("argument_edit_approved")
            if approval_resolution is not None
            else None
        )
        try:
            edited_approval = json.loads(edited_approval) if edited_approval else None
        except (TypeError, ValueError):
            edited_approval = None
        if (
            not check_only
            and isinstance(edited_approval, dict)
            and edited_approval.get("arguments") == call.arguments
            and edited_approval.get("permission")
            == agent_run_support._argument_edit_permission_binding(request)
        ):
            approval_resolution.pop("argument_edit_approved", None)
            if decision == PermissionDecision.DENY:
                return PermissionDecision.DENY
            db.add(PermissionGrant(**action_grant))
            await db.commit()
            if browser_approval_key is not None:
                approval_resolution[browser_approval_key] = "allow_once"
            return PermissionDecision.ALLOW
        if (
            decision == PermissionDecision.ASK
            and (check_only or request.resource_type == "execution_administrator")
            and browser_approval_key is not None
            and approval_resolution.get(browser_approval_key) in {"allow_once", "allow_session"}
        ):
            return PermissionDecision.ALLOW
        if check_only:
            return decision
        if decision != PermissionDecision.ASK:
            draft_file = (request.approval_details or {}).get("draftFile")
            if (
                decision == PermissionDecision.ALLOW
                and approval_resolution is not None
                and isinstance(draft_file, dict)
                and isinstance(draft_file.get("initialRevision"), str)
            ):
                approval_resolution["expected_revision"] = draft_file["initialRevision"]
            return decision
        from mato_loop_backend.permissions.requests import prepare_file_grants

        request = await prepare_file_grants(request, root_files)
        definition = (
            tool_registry._combined_definitions(effective_execution_integration_settings(
                integration_settings, run.execution_agent_id,
                agent_run_support._effective_execution_operations(run),
            ))[0].get(call.name)
            or tool_registry._operation_definitions.get(call.name)
        )
        resolution = await cls._request_approval(
            db,
            run,
            chat_session,
            call,
            request,
            display=tool_display(
                call.name,
                agent_run_support._safe_tool_arguments(call.name, call.arguments),
                definition,
            ),
            argument_edit=agent_run_support._argument_edit_payload(definition, call.arguments),
        )
        if isinstance(resolution, ApprovalResolution) and resolution.argument_edits:
            raise ToolArgumentsEdited(
                resolution.argument_edits,
                agent_run_support._argument_edit_permission_binding(request),
            )
        approval_decision = (
            resolution.decision if isinstance(resolution, ApprovalResolution) else resolution
        )
        if approval_decision == "deny":
            return PermissionDecision.USER_DENIED
        if approval_decision not in {"allow_once", "allow_session"}:
            return PermissionDecision.DENY
        if (
            isinstance(resolution, ApprovalResolution)
            and resolution.expected_revision is not None
            and approval_resolution is not None
        ):
            approval_resolution["expected_revision"] = resolution.expected_revision
        await db.refresh(chat_session)
        apply_run_session_settings(chat_session, run)
        try:
            tool_settings, _profile = await load_agent_profile_tools(
                db,
                chat_session.user_id,
                agent_profile_id,
                TrustLevel(run.trust_level or TrustLevel.PUBLIC.value),
            )
        except AgentProfileUnavailable:
            return PermissionDecision.DENY
        if not _tool_available(call.name, tool_settings):
            return PermissionDecision.DENY
        integration_settings = await load_effective_integrations(
            db, chat_session.user_id
        )
        integration_settings = filter_agent_profile_integrations(
            integration_settings, _profile, allowed_ids=integration_ceiling_ids(ceiling)
        )
        session_write_paths = getattr(_profile, "session_write_paths", None)
        ceiling_paths = session_write_paths_ceiling(ceiling)
        if ceiling_paths is not None:
            session_write_paths = (
                ceiling_paths
                if session_write_paths is None
                else sorted(set(session_write_paths) & set(ceiling_paths))
            )
        current_policy = agent_run_support._tool_policy(tool_settings, integration_settings, request)
        current_external_working_directory_policy = agent_run_support._external_working_directory_policy(tool_settings)
        current_sensitive_data_policy = agent_run_support._tool_policy(tool_settings, integration_settings, sensitive_request)
        if ceiling is not None:
            ceiling_policy = agent_run_support._tool_policy(
                ceiling_settings(ceiling, TrustLevel(run.trust_level)), integration_settings, request,
                snapshot=ceiling,
            )
            current_policy = max((current_policy, ceiling_policy), key=policies.index)
            current_external_working_directory_policy = max(
                (
                    current_external_working_directory_policy,
                    agent_run_support._external_working_directory_policy(
                        ceiling_settings(ceiling, TrustLevel(run.trust_level)), snapshot=ceiling,
                    ),
                ),
                key=policies.index,
            )
            current_sensitive_data_policy = max((
                current_sensitive_data_policy,
                agent_run_support._tool_policy(
                    ceiling_settings(ceiling, TrustLevel(run.trust_level)), integration_settings,
                    sensitive_request, snapshot=ceiling,
                ),
            ), key=policies.index)
        current_decision = await PermissionPolicy.evaluate(
            db,
            chat_session,
            request,
            root_files,
            current_policy,
            external_working_directory_policy=current_external_working_directory_policy,
            sensitive_data_policy=current_sensitive_data_policy,
            agent_profile_id=agent_profile_id,
            trust_level=TrustLevel(run.trust_level or TrustLevel.PUBLIC.value),
            session_write_paths=session_write_paths,
            temporary_files_enabled=tool_settings._temporary_files_enabled and temporary_files_ceiling(ceiling),
            run_id=run.id,
            **permission_namespace,
        )
        if current_decision == PermissionDecision.ASK and "allow_session" in request.available_decisions and any(
            grant.resource_type in {"execution_path", "session_path"} for grant in request.effective_session_grants
        ):
            return PermissionDecision.DENY
        if current_decision != PermissionDecision.DENY and browser_approval_key is not None:
            approval_resolution[browser_approval_key] = approval_decision
        return (
            PermissionDecision.DENY
            if current_decision == PermissionDecision.DENY
            else PermissionDecision.ALLOW
        )

    @classmethod
    async def _request_approval(
        cls,
        db: AsyncSession,
        run: AgentRun,
        chat_session: ChatSession,
        call: ToolCall,
        permission: PermissionRequest,
        *,
        display: dict[str, str] | None = None,
        argument_edit: dict[str, Any] | None = None,
    ) -> str | ApprovalResolution:
        from mato_loop_backend.permissions.requests import request_approval

        return await request_approval(cls, db, run, chat_session, call, permission, display=display, argument_edit=argument_edit)

    @staticmethod
    def claim_approval_resolution(approval_id: UUID) -> bool:
        return approval_coordinator.claim_resolution(approval_id)

    @staticmethod
    def release_approval_resolution(approval_id: UUID) -> None:
        approval_coordinator.release_resolution(approval_id)

    @staticmethod
    def resolve_approval(
        approval_id: UUID, decision: str | ApprovalResolution
    ) -> bool:
        return approval_coordinator.complete_resolution(approval_id, decision)

    @classmethod
    async def _request_question(
        cls,
        db: AsyncSession,
        run: AgentRun,
        chat_session: ChatSession,
        call: ToolCall,
        questions: QuestionRequestArguments,
    ) -> (
        dict[str, list[str]]
        | ProtectedQuestionResolution
        | Literal["rejected"]
        | None
    ):
        from mato_loop_backend.agent.questions import request_question

        return await request_question(cls, db, run, chat_session, call, questions)

    @staticmethod
    def claim_question_resolution(question_id: UUID) -> bool:
        return question_coordinator.claim_resolution(question_id)

    @staticmethod
    def release_question_resolution(question_id: UUID) -> None:
        question_coordinator.release_resolution(question_id)

    @staticmethod
    def resolve_question(
        question_id: UUID, answers: dict[str, list[str]] | Literal["rejected"]
    ) -> bool:
        return question_coordinator.complete_resolution(question_id, answers)
