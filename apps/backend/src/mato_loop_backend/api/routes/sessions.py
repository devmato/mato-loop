from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import replace
from functools import partial
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Query,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    ValidationError,
)
from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from mato_loop_backend.agent.run_contracts import AgentRunError
from mato_loop_backend.agent.run_timing import run_timing_payload
from mato_loop_backend.agent.runtime import AgentRuntime
from mato_loop_backend.api.errors import ApiError
from mato_loop_backend.chat_history import ChatHistoryError, ChatHistoryService
from mato_loop_backend.db.session import get_db_session
from mato_loop_backend.error_contract import describe_error
from mato_loop_backend.events import run_events
from mato_loop_backend.execution import execution_agent_connections
from mato_loop_backend.files.service import RootFileError
from mato_loop_backend.files.storage import SessionStorage
from mato_loop_backend.media.attachments import (
    AttachmentError,
    AttachmentInput,
    AttachmentService,
    attachment_processing_candidate,
    classify_attachment,
    image_mime_type,
    processing_limits,
    safe_filename,
)
from mato_loop_backend.memory import effective_memory
from mato_loop_backend.models import (
    AgentRun,
    ChatSession,
    ExecutionAgent,
    MemoryMode,
    Message,
    MessageAttachment,
    Project,
    QueuedAttachment,
    QueuedMessage,
    UserAccount,
    WebSearchPolicy,
)
from mato_loop_backend.security import require_user
from mato_loop_backend.sessions import service as session_service
from mato_loop_backend.sessions.archive import ArchiveRequest, is_session_archived, set_archived
from mato_loop_backend.sessions.contracts import (
    ActiveFileRequest,
    ControlCenterState,
    SessionCreateRequest,
    SessionOptionsRequest,
    default_control_center_state,
)
from mato_loop_backend.sessions.contracts import (
    ControlCenterFileTarget as ControlCenterFileTarget,
)
from mato_loop_backend.sessions.contracts import (
    ControlCenterFileView as ControlCenterFileView,
)
from mato_loop_backend.sessions.contracts import (
    ControlCenterFileViews as ControlCenterFileViews,
)
from mato_loop_backend.sessions.contracts import (
    ControlCenterTabState as ControlCenterTabState,
)
from mato_loop_backend.sessions.serialization import (
    LAST_MESSAGE_AT_UNSET as _LAST_MESSAGE_AT_UNSET,
)
from mato_loop_backend.sessions.serialization import (
    context_summary_payload,
    message_payload,
    run_payload,
)
from mato_loop_backend.sessions.serialization import (
    session_payload as _serialize_session_payload,
)
from mato_loop_backend.sessions.state import deleted_session_clause
from mato_loop_backend.sessions.templates import load_template
from mato_loop_backend.settings import (
    AiSettings,
    InstanceLimits,
    MemorySettings,
    SettingsStore,
    UserPreferences,
    apply_session_ai,
    load_effective_ai,
)
from mato_loop_backend.settings.models import ReasoningEffort, TrustLevel
from mato_loop_backend.settings.session_profiles import SessionProfileSnapshot

router = APIRouter(prefix="/sessions", dependencies=[Depends(require_user)])
logger = logging.getLogger(__name__)

_model_available = session_service.model_available
_model_supports_tools = session_service.model_supports_tools
_model_for_session = session_service.model_for_session
_provider_for_session = session_service.provider_for_session
_execution_home = session_service.execution_home
_owned_session = session_service.owned_session


def _raise_api_error(exc: session_service.SessionServiceError) -> None:
    raise ApiError(
        exc.status_code,
        exc.code,
        field=exc.field,
        params=exc.params,
    ) from exc


async def _mutable_session(
    db: AsyncSession, session_id: UUID, user_id: UUID, *, for_update: bool = False
) -> ChatSession | None:
    try:
        return await session_service.get_owned_session(
            db, session_id, user_id, for_update=for_update
        )
    except session_service.SessionServiceError as exc:
        _raise_api_error(exc)


async def _deletable_session(
    db: AsyncSession, session_id: UUID, user_id: UUID, *,
    for_update: bool = False, allow_deleted: bool = True,
) -> ChatSession | None:
    item = await _owned_session(
        db, session_id, user_id, for_update=for_update, include_deleted=True
    )
    if item is not None and item.parent_session_id is not None:
        raise session_service.SessionServiceError(403, "session.readOnly")
    if item is not None and not allow_deleted:
        from mato_loop_backend.sessions.state import is_session_deleted

        if await is_session_deleted(db, item):
            raise session_service.SessionServiceError(409, "session.inTrash")
    return item


class SessionUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_template_id: UUID | None = None
    session_profile: SessionProfileSnapshot | None = None
    title: str | None = Field(default=None, max_length=240)
    pinned: bool | None = None
    todos_collapsed: StrictBool | None = None
    project_id: UUID | None = None
    model_id: UUID | None = None
    agent_profile_id: UUID | None = Field(
        default=None,
        validation_alias=AliasChoices("agentProfileId", "agent_profile_id"),
    )
    reasoning_effort: ReasoningEffort | None = None
    memory_mode: MemoryMode | None = None
    trust_level: TrustLevel | None = None
    web_search_policy: WebSearchPolicy | None = None
    preferred_execution_agent_id: UUID | None = None
    current_directory: str | None = Field(default=None, max_length=4_096)
    control_center: ControlCenterState = Field(default=None)


class SendMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(max_length=200_000)
    enqueue: StrictBool = False
    after_run_id: UUID | None = None
    voice_attachment_indices: list[StrictInt] = Field(
        default_factory=list, alias="voiceAttachmentIndices"
    )
    run_id: UUID | None = None
    agent_profile_id: UUID | None = Field(
        default=None, validation_alias=AliasChoices("agentProfileId", "agent_profile_id")
    )
    active_file: ActiveFileRequest | None = Field(
        default=None, validation_alias=AliasChoices("active_file", "activeFile")
    )


class ContinueRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(max_length=200_000)
    message_id: UUID
    agent_profile_id: UUID | None = Field(
        default=None, validation_alias=AliasChoices("agentProfileId", "agent_profile_id")
    )
    voice_attachment_indices: list[StrictInt] = Field(
        default_factory=list, alias="voiceAttachmentIndices"
    )
    active_file: ActiveFileRequest | None = Field(
        default=None, validation_alias=AliasChoices("active_file", "activeFile")
    )


class QueueUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paused: StrictBool


class SessionReadRequest(BaseModel):
    through_message_id: UUID | None = Field(default=None, alias="throughMessageId")


def _multipart_payload(value: str, model: type[BaseModel]) -> BaseModel:
    if len(value) > 1_000_000:
        raise ApiError(413, "validation.tooLong", field="payload")
    try:
        return model.model_validate_json(value)
    except ValidationError as exc:
        error = exc.errors(include_url=False)[0] if exc.errors() else {}
        field = next(
            (part for part in reversed(error.get("loc") or ()) if isinstance(part, str)),
            "payload",
        )
        raise ApiError(422, "validation.invalid", field=field) from exc


def _attachment_api_error(exc: AttachmentError) -> ApiError:
    status_code = 413 if exc.code in {
        "attachment.userQuotaExceeded",
        "attachment.textTooLarge",
        "attachment.tooLarge",
    } else 400
    return ApiError(status_code, exc.code)


async def _attachment_uploads(
    files: list[UploadFile], settings: InstanceLimits | None = None,
    voice_attachment_indices: list[int] | None = None,
) -> list[AttachmentInput]:
    """Return processed inputs or borrowed upload streams, closed by the route."""
    settings = settings or InstanceLimits()
    if not files:
        raise ApiError(422, "validation.required", field="attachments")
    attachments = []
    retained = set()
    try:
        indices = voice_attachment_indices or []
        if len(set(indices)) != len(indices) or any(
            type(index) is not int or not 0 <= index < len(files) for index in indices
        ):
            raise ApiError(422, "validation.invalid", field="voiceAttachmentIndices")
        if len(files) > settings.max_attachments_per_message:
            raise ApiError(
                422,
                "validation.tooManyItems",
                field="attachments",
                params={"maxItems": settings.max_attachments_per_message},
            )
        total_bytes = 0
        limits = processing_limits(settings)
        for index, upload in enumerate(files):
            size = await asyncio.to_thread(upload.file.seek, 0, os.SEEK_END)
            await upload.seek(0)
            total_bytes += size
            if size > settings.max_upload_bytes or total_bytes > settings.max_message_upload_bytes:
                raise ApiError(413, "attachment.tooLarge")
            header = await upload.read(64 * 1024)
            await upload.seek(0)
            mime_type = (upload.content_type or "").split(";", 1)[0].strip().lower()
            if size and not attachment_processing_candidate(header, mime_type, upload.filename):
                if index in indices:
                    raise ApiError(422, "validation.invalid", field="voiceAttachmentIndices")
                attachments.append(AttachmentInput(
                    upload.file, "application/octet-stream", safe_filename(upload.filename), "file",
                ))
                retained.add(id(upload))
                continue
            data = await upload.read(settings.max_upload_bytes + 1)
            try:
                attachments.append(
                    classify_attachment(
                        data,
                        mime_type,
                        upload.filename,
                        max_bytes=settings.max_upload_bytes,
                        limits=limits,
                    )
                )
                if index in indices:
                    if attachments[-1].kind != "audio":
                        raise ApiError(422, "validation.invalid", field="voiceAttachmentIndices")
                    attachments[-1] = replace(
                        attachments[-1], metadata={"voiceMessage": True}
                    )
            except AttachmentError as exc:
                if exc.code == "attachment.invalidImage":
                    filename = safe_filename(upload.filename)
                    suffix = (
                        f".{filename.rsplit('.', 1)[1].casefold()}" if "." in filename else ""
                    )
                    logger.warning(
                        "Attachment image validation failed index=%d size_bytes=%d "
                        "content_type=%r detected_type=%r suffix=%r diagnostic=%r",
                        index,
                        len(data),
                        mime_type,
                        image_mime_type(data),
                        suffix,
                        exc.diagnostic,
                    )
                raise _attachment_api_error(exc) from exc
    except BaseException:
        retained.clear()
        raise
    finally:
        for upload in files:
            if id(upload) not in retained:
                await upload.close()
    return attachments


def session_payload(
    item: ChatSession,
    *,
    effective_memory_mode: MemoryMode | None = None,
    memory_tools_available: bool | None = None,
    memory_unavailable_reason: str | None = None,
    workspace_context: dict[str, Any] | None = None,
    last_message_at: Any = _LAST_MESSAGE_AT_UNSET,
    has_unread_messages: bool | None = None,
) -> dict[str, Any]:
    try:
        control_center = ControlCenterState.model_validate(
            {key: value for key, value in item.control_center.items()
             if key not in {"terminal_auto_attach", "todos_collapsed", "conversation_fork"}}
            if item.control_center else default_control_center_state()
        ).model_dump(mode="json")
    except (RootFileError, ValidationError):
        control_center = default_control_center_state().model_dump(mode="json")
    return _serialize_session_payload(
        item,
        control_center,
        effective_memory_mode=effective_memory_mode,
        memory_tools_available=memory_tools_available,
        memory_unavailable_reason=memory_unavailable_reason,
        workspace_context=workspace_context,
        last_message_at=last_message_at,
        has_unread_messages=has_unread_messages,
    )


async def _session_payload(
    db: AsyncSession,
    item: ChatSession,
    *,
    last_message_at: Any = _LAST_MESSAGE_AT_UNSET,
    has_unread_messages: bool | None = None,
    base_ai: AiSettings | None = None,
    memory_settings: MemorySettings | None = None,
    archived: bool | None = None,
    workspace_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if archived is None:
        archived = await is_session_archived(db, item)
    if workspace_context is None:
        workspace_context = await session_service.workspace_context(db, item)
    if item.memory_mode == MemoryMode.DISABLED.value:
        return {**session_payload(
            item,
            effective_memory_mode=MemoryMode.DISABLED,
            memory_tools_available=False,
            memory_unavailable_reason="memory.sessionDisabled",
            workspace_context=workspace_context,
            last_message_at=last_message_at,
            has_unread_messages=has_unread_messages,
        ), "archived": archived}
    ai = apply_session_ai(
        base_ai if base_ai is not None else await load_effective_ai(db, item.user_id), item
    )
    memory = memory_settings if memory_settings is not None else await SettingsStore.load(
        db, MemorySettings, user_id=item.user_id
    )
    model_id = ai.default_model_id
    model = _model_for_session(ai, model_id) if _model_available(ai, model_id) else None
    state = effective_memory(item, memory, _provider_for_session(ai, model), model)
    return {**session_payload(
        item,
        effective_memory_mode=state.mode,
        memory_tools_available=state.tools_enabled,
        memory_unavailable_reason=state.unavailable_reason,
        workspace_context=workspace_context,
        last_message_at=last_message_at,
        has_unread_messages=has_unread_messages,
    ), "archived": archived}


async def _session_list_serializer(
    db: AsyncSession, items: list[ChatSession]
) -> session_service.PayloadSerializer:
    """Prepare shared payload inputs for this page only, without caching authorization."""
    user_id = items[0].user_id
    project_ids = {item.project_id for item in items if item.project_id is not None}
    archived_project_ids = set((await db.scalars(select(Project.id).where(
        Project.id.in_(project_ids), Project.archived_at.is_not(None),
    ))).all()) if project_ids else set()
    agent_ids = {
        item.preferred_execution_agent_id for item in items
        if item.preferred_execution_agent_id is not None
    }
    available_agent_ids = (await db.scalars(select(ExecutionAgent.id).where(
        ExecutionAgent.id.in_(agent_ids),
        ExecutionAgent.user_id == user_id,
        ExecutionAgent.revoked_at.is_(None),
    ))).all() if agent_ids else []
    snapshots = {
        agent_id: execution_agent_connections.snapshot(agent_id)
        for agent_id in available_agent_ids
    }
    ai = memory = None
    if any(item.memory_mode != MemoryMode.DISABLED.value for item in items):
        ai = await load_effective_ai(db, user_id)
        memory = await SettingsStore.load(db, MemorySettings, user_id=user_id)

    async def serialize(db: AsyncSession, item: ChatSession, **kwargs) -> dict[str, Any]:
        agent_id = item.preferred_execution_agent_id
        snapshot = snapshots.get(agent_id)
        return await _session_payload(
            db, item,
            base_ai=ai,
            memory_settings=memory,
            archived=item.archived_at is not None or item.project_id in archived_project_ids,
            workspace_context={
                "executionAgentId": str(agent_id) if agent_id else None,
                "connected": snapshot is not None,
                "operations": sorted(snapshot) if snapshot is not None else [],
            },
            **kwargs,
        )

    return serialize


async def _duplicate_submission_response(
    db: AsyncSession,
    run_id: UUID | None,
    user_id: UUID,
    *,
    requested_session_id: UUID | None = None,
) -> dict[str, Any] | None:
    try:
        return await session_service.duplicate_submission_response(
            db,
            run_id,
            user_id,
            requested_session_id=requested_session_id,
            session_serializer=_session_payload,
            message_serializer=message_payload,
        )
    except session_service.SessionServiceError as exc:
        _raise_api_error(exc)


@router.get("")
async def list_sessions(
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    page_size: Annotated[int, Query(alias="pageSize", ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query()] = None,
    project_id: Annotated[UUID | None, Query(alias="projectId")] = None,
    standalone: Annotated[bool, Query()] = False,
    archived: bool = False,
    include_archived: Annotated[bool, Query(alias="includeArchived")] = False,
    sort: Literal["pinned", "recent"] = "pinned",
    session_ids: Annotated[list[UUID] | None, Query(alias="sessionIds", max_length=100)] = None,
    view: Literal["full", "status"] = "full",
) -> dict[str, Any]:
    try:
        return await session_service.list_sessions(
            db,
            user.id,
            page_size=page_size,
            cursor=cursor,
            project_id=project_id,
            standalone=standalone,
            session_serializer_factory=_session_list_serializer,
            archived=archived,
            include_archived=include_archived,
            sort=sort,
            session_ids=session_ids,
            status_only=view == "status",
        )
    except session_service.SessionServiceError as exc:
        _raise_api_error(exc)


class HomeSessionsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    retained_ids: list[UUID] = Field(default_factory=list, alias="retainedIds")


@router.post("/home")
async def home_sessions(
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    payload: HomeSessionsRequest,
) -> dict[str, Any]:
    return await session_service.list_sessions(
        db, user.id, page_size=6, cursor=None, project_id=None, standalone=False,
        session_serializer_factory=_session_list_serializer, sort="recent", home_session_ids=payload.retained_ids,
    )


@router.post("/{session_id}/archive")
async def archive_session(
    session_id: UUID,
    payload: ArchiveRequest,
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, Any]:
    item = await _mutable_session(db, session_id, user.id, for_update=True)
    if item is None:
        raise ApiError(404, "session.notFound")
    try:
        await set_archived(db, item, payload.archived)
    except session_service.SessionServiceError as exc:
        _raise_api_error(exc)
    await db.commit()
    return await _session_payload(db, item)


async def _create_session(
    payload: SessionCreateRequest,
    user: UserAccount,
    db: AsyncSession,
    attachments: list[AttachmentInput] | None = None,
) -> dict:
    try:
        return await session_service.create_session(
            payload,
            user,
            db,
            attachments=attachments,
            storage_factory=SessionStorage,
            session_serializer=_session_payload,
            message_serializer=message_payload,
            duplicate_submission=_duplicate_submission_response,
        )
    except session_service.SessionServiceError as exc:
        _raise_api_error(exc)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_session(
    payload: SessionCreateRequest,
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict:
    if payload.voice_attachment_indices:
        raise ApiError(422, "validation.invalid", field="voiceAttachmentIndices")
    if payload.content is None and (payload.run_id is not None or payload.active_file is not None):
        raise ApiError(422, "validation.invalid", field="content")
    return await _create_session(payload, user, db)


@router.post("/with-attachments", status_code=status.HTTP_201_CREATED)
async def create_session_with_attachments(
    payload: Annotated[str, Form()],
    attachments: Annotated[list[UploadFile], File()],
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict:
    request = _multipart_payload(payload, SessionCreateRequest)
    assert isinstance(request, SessionCreateRequest)
    if request.content is None:
        raise ApiError(422, "validation.required", field="content")
    duplicate = await _duplicate_submission_response(db, request.run_id, user.id)
    if duplicate is not None:
        return duplicate
    instance_limits = await SettingsStore.load(db, InstanceLimits)
    try:
        return await _create_session(
            request,
            user,
            db,
            await _attachment_uploads(
                attachments, instance_limits, request.voice_attachment_indices
            ),
        )
    except AttachmentError as exc:
        raise _attachment_api_error(exc) from exc
    finally:
        for upload in attachments:
            await upload.close()


@router.post("/defaults")
async def session_defaults(
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    payload: SessionOptionsRequest,
) -> dict:
    memory = await SettingsStore.load(db, MemorySettings, user_id=user.id)
    preferences = await SettingsStore.load(db, UserPreferences, user_id=user.id)
    project = None
    if payload.project_id is not None:
        project = await db.scalar(
            select(Project).where(
                Project.id == payload.project_id,
                Project.user_id == user.id,
                Project.delete_at.is_(None),
            )
        )
        if project is None:
            raise ApiError(404, "project.notFound", field="projectId")
    try:
        template = await load_template(db, user.id, payload.session_template_id or (project.default_session_template_id if project else None))
        selected_agent_profile_id = payload.agent_profile_id or template.start_agent_id
        await session_service.validate_agent_profile(
            db, user.id, selected_agent_profile_id
        )
    except session_service.SessionServiceError as exc:
        _raise_api_error(exc)
    ai = await load_effective_ai(db, user.id)

    configuration = payload.session_profile or template.snapshot()
    configuration.worktrees_enabled = template.worktrees_enabled
    configured_model = payload.model_id if "model_id" in payload.model_fields_set else template.model_id
    configured_reasoning = payload.reasoning_effort if "reasoning_effort" in payload.model_fields_set else template.reasoning_effort
    memory_mode = payload.memory_mode or MemoryMode(template.memory_mode)
    draft = ChatSession(user_id=user.id, agent_profile_id=selected_agent_profile_id, model_config_id=configured_model, reasoning_effort=configured_reasoning, session_profile=configuration.model_dump(mode="json"), memory_mode=memory_mode.value)
    ai = apply_session_ai(ai, draft)
    selected_model_id = ai.default_model_id
    model = (
        _model_for_session(ai, selected_model_id)
        if _model_available(ai, selected_model_id)
        else None
    )
    state = effective_memory(
        draft, memory, _provider_for_session(ai, model), model
    )
    preferred_agent_id = preferences.default_execution_agent_id
    current_directory = None
    if project is not None:
        if project.default_execution_agent_id is not None:
            preferred_agent_id = project.default_execution_agent_id
            current_directory = project.default_current_directory
    if preferred_agent_id is not None:
        agent = await db.scalar(
            select(ExecutionAgent).where(
                ExecutionAgent.id == preferred_agent_id,
                ExecutionAgent.user_id == user.id,
                ExecutionAgent.revoked_at.is_(None),
            )
        )
        if agent is not None and current_directory is None:
            current_directory = await _execution_home(db, agent)
    return {
        "modelId": str(configured_model) if configured_model else None,
        "effectiveModelId": str(selected_model_id) if selected_model_id else None,
        "sessionTemplateId": str(template.id),
        "sessionProfile": configuration.model_dump(mode="json", by_alias=True),
        "agentProfileId": str(selected_agent_profile_id),
        "reasoningEffort": configured_reasoning,
        "effectiveReasoningEffort": ai.reasoning_effort,
        "modelAvailable": bool(model and model.supports_tools),
        "modelUnavailableReason": (
            None
            if model and model.supports_tools
            else (
                "session.modelToolsUnavailable"
                if model is not None
                else "session.modelUnavailable"
            )
        ),
        "memoryMode": memory_mode.value,
        "effectiveMemoryMode": state.mode.value,
        "memoryToolsAvailable": state.tools_enabled,
        "memoryUnavailableReason": state.unavailable_reason,
        "webSearchPolicy": WebSearchPolicy.AVAILABLE.value,
        "preferredExecutionAgentId": str(preferred_agent_id) if preferred_agent_id else None,
        "currentDirectory": current_directory,
        "workspaceMode": (project.git_workspace_mode if project and configuration.worktrees_enabled else None) or "local",
    }


@router.post("/{session_id}/fork", status_code=status.HTTP_201_CREATED)
async def fork_session(
    session_id: UUID,
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict:
    from mato_loop_backend.sessions.fork import fork_session as copy_session

    try:
        item = await copy_session(db, session_id, user.id)
    except session_service.SessionServiceError as exc:
        _raise_api_error(exc)
    return {"id": str(item.id)}


@router.get("/message-queue")
async def message_queue(
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    session_ids: Annotated[list[UUID] | None, Query(alias="sessionIds", max_length=100)] = None,
) -> dict:
    from mato_loop_backend.sessions.message_queue import list_entries

    return {"items": await list_entries(db, user.id, session_ids=session_ids)}


@router.get("/message-submissions/{request_id}")
async def get_message_submission(
    request_id: UUID,
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, str]:
    submission = await session_service.find_message_submission(db, user.id, request_id)
    if submission is None:
        raise ApiError(404, "session.messageNotFound")
    session_id, message_id = submission
    return {"sessionId": str(session_id), "messageId": str(message_id)}


async def _queue_session(db, session_id, user_id):
    await db.execute(select(UserAccount.id).where(UserAccount.id == user_id).with_for_update(key_share=True))
    session = await _mutable_session(db, session_id, user_id, for_update=True)
    if session is None:
        raise ApiError(404, "session.notFound")
    return session


async def _raise_if_queue_admitted(db, session_id: UUID, request_id: UUID) -> None:
    run = await db.get(AgentRun, request_id)
    if run is not None and run.session_id == session_id and run.parent_run_id is None:
        params = {"messageId": str(run.input_message_id)} if run.input_message_id else None
        raise ApiError(409, "session.runAlreadyActive", params=params)


@router.delete("/{session_id}/queue/{request_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_queued_message(
    session_id: UUID, request_id: UUID,
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    require_present: bool = False,
) -> Response:
    from mato_loop_backend.sessions.message_queue import remove_entry

    await _queue_session(db, session_id, user.id)
    entry = await db.get(QueuedMessage, request_id)
    if entry is None or entry.session_id != session_id:
        await _raise_if_queue_admitted(db, session_id, request_id)
        if require_present:
            raise ApiError(404, "session.messageNotFound")
    await remove_entry(db, session_id, request_id)
    await db.commit()
    return Response(status_code=204)


@router.patch("/{session_id}/queue/{request_id}", status_code=status.HTTP_204_NO_CONTENT)
async def update_queued_message(
    session_id: UUID, request_id: UUID, payload: QueueUpdateRequest,
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> Response:
    await _queue_session(db, session_id, user.id)
    entry = await db.get(QueuedMessage, request_id)
    if entry is None or entry.session_id != session_id:
        raise ApiError(404, "session.notFound")
    entry.paused, entry.error_code = payload.paused, None
    await db.commit()
    return Response(status_code=204)


@router.get("/{session_id}/queue/{request_id}/attachments/{position}")
async def queued_attachment(
    session_id: UUID, request_id: UUID, position: int,
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> Response:
    if await _owned_session(db, session_id, user.id) is None:
        raise ApiError(404, "session.notFound")
    data = await db.scalar(select(QueuedAttachment.data).join(QueuedMessage).where(
        QueuedMessage.id == request_id, QueuedMessage.session_id == session_id, QueuedAttachment.position == position,
    ))
    if data is None:
        await _raise_if_queue_admitted(db, session_id, request_id)
        raise ApiError(404, "attachment.notFound")
    return Response(data, media_type="application/octet-stream", headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})


@router.get("/{session_id}")
async def get_session(
    session_id: UUID,
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    history_preview: Annotated[bool, Query(alias="historyPreview")] = False,
) -> dict:
    item = await _owned_session(db, session_id, user.id)
    if item is None:
        raise ApiError(404, "session.notFound")
    return await session_service.session_detail_payload(
        db,
        item,
        session_serializer=_session_payload,
        message_serializer=partial(message_payload, compact=True),
        context_summary_serializer=context_summary_payload,
        history_preview=history_preview,
    )


@router.get("/{session_id}/messages")
async def get_session_messages(
    session_id: UUID,
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    page_size: Annotated[int, Query(alias="pageSize", ge=1, le=100)] = 20,
    cursor: Annotated[UUID | None, Query()] = None,
) -> dict[str, Any]:
    if await _owned_session(db, session_id, user.id) is None:
        raise ApiError(404, "session.notFound")
    try:
        messages, context_summaries, next_cursor = await session_service.session_message_page(
            db,
            session_id,
            page_size=page_size,
            cursor=cursor,
        )
    except session_service.SessionServiceError as exc:
        _raise_api_error(exc)
    attributions = await session_service.message_attributions(db, messages)
    return {
        "items": [
            message_payload(
                message,
                attribution=attributions.get(message.id),
                attribution_resolved=True,
                compact=True,
            )
            for message in messages
        ],
        "contextSummaries": [
            context_summary_payload(summary) for summary in context_summaries
        ],
        "nextCursor": next_cursor,
    }


@router.get("/{session_id}/messages/{message_id}/window")
async def get_session_message_window(
    session_id: UUID,
    message_id: UUID,
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, Any]:
    if await _owned_session(db, session_id, user.id) is None:
        raise ApiError(404, "session.notFound")
    try:
        window = await session_service.session_message_window(db, session_id, message_id)
    except session_service.SessionServiceError as exc:
        _raise_api_error(exc)
    if window is None:
        return {"requiresPaging": True}
    messages, summaries, next_cursor, has_newer = window
    attributions = await session_service.message_attributions(db, messages)
    return {
        "items": [
            message_payload(
                message, attribution=attributions.get(message.id),
                attribution_resolved=True, compact=True,
            )
            for message in messages
        ],
        "contextSummaries": [context_summary_payload(summary) for summary in summaries],
        "nextCursor": next_cursor,
        "hasNewer": has_newer,
    }


@router.get("/{session_id}/children")
async def get_session_children(
    session_id: UUID,
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[dict[str, Any]]:
    if await _owned_session(db, session_id, user.id) is None:
        raise ApiError(404, "session.notFound")
    latest_run_id = (
        select(AgentRun.id)
        .where(AgentRun.session_id == ChatSession.id, AgentRun.parent_run_id.is_(None))
        .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
        .limit(1)
        .correlate(ChatSession)
        .scalar_subquery()
    )
    rows = (
        await db.execute(
            select(ChatSession, AgentRun)
            .outerjoin(AgentRun, AgentRun.id == latest_run_id)
            .where(
                ChatSession.parent_session_id == session_id,
                ChatSession.user_id == user.id,
                ~deleted_session_clause(),
            )
            .order_by(ChatSession.created_at, ChatSession.spawn_index, ChatSession.id)
        )
    ).all()
    return [
        {
            "id": str(child.id),
            "title": child.title,
            "parentSessionId": str(child.parent_session_id),
            "spawnedByRunId": str(child.spawned_by_run_id),
            "spawnedByToolCallId": child.spawned_by_tool_call_id,
            "spawnIndex": child.spawn_index,
            "run": run_payload(run) if run is not None else None,
        }
        for child, run in rows
    ]


@router.get("/{session_id}/messages/{message_id}")
async def get_session_message(
    session_id: UUID,
    message_id: UUID,
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    compact: bool = False,
) -> dict[str, Any]:
    if await _owned_session(db, session_id, user.id) is None:
        raise ApiError(404, "session.notFound")
    message = await db.scalar(
        select(Message)
        .where(Message.id == message_id, Message.session_id == session_id)
        .options(selectinload(Message.attachments))
    )
    if message is None:
        raise ApiError(404, "session.messageNotFound")
    attributions = await session_service.message_attributions(db, [message])
    return message_payload(
        message,
        attribution=attributions.get(message.id),
        attribution_resolved=True,
        compact=compact,
    )


@router.post("/{session_id}/read")
async def mark_session_read(
    session_id: UUID,
    payload: SessionReadRequest,
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, bool]:
    item = await _mutable_session(db, session_id, user.id, for_update=True)
    if item is None:
        raise ApiError(404, "session.notFound")
    read_at = None
    if payload.through_message_id is not None:
        read_at = await db.scalar(
            select(Message.created_at).where(
                Message.id == payload.through_message_id,
                Message.session_id == session_id,
                Message.role == "assistant",
            )
        )
        if read_at is None:
            raise ApiError(400, "validation.invalid", field="throughMessageId")
        await db.execute(
            update(ChatSession)
            .where(
                ChatSession.id == session_id,
                ChatSession.last_read_at < read_at,
            )
            .values(last_read_at=read_at, updated_at=ChatSession.updated_at)
            .execution_options(matoloop_session_read=True)
        )
    from mato_loop_backend.notifications import NotificationService

    await NotificationService.mark_session_read(db, user.id, session_id, read_at)
    await db.commit()
    return {"read": True}


@router.patch("/{session_id}")
async def update_session(
    session_id: UUID,
    payload: SessionUpdateRequest,
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict:
    try:
        return await session_service.update_session(
            session_id,
            payload,
            user,
            db,
            storage_factory=SessionStorage,
            owned_session_query=_mutable_session,
            session_serializer=_session_payload,
        )
    except session_service.SessionServiceError as exc:
        _raise_api_error(exc)


@router.get("/{session_id}/delete-impact")
async def session_delete_impact(
    session_id: UUID,
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, Any]:
    try:
        return await session_service.session_delete_impact(
            session_id,
            user.id,
            db,
            owned_session_query=_deletable_session,
        )
    except session_service.SessionServiceError as exc:
        _raise_api_error(exc)


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    session_id: UUID,
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    automation_mode: Annotated[
        Literal["unlink", "deleteAutomations"] | None,
        Query(alias="automationMode"),
    ] = None,
    automation_impact: Annotated[
        str | None,
        Query(alias="automationImpact", min_length=64, max_length=64),
    ] = None,
    worktree_impact: Annotated[
        str | None,
        Query(alias="worktreeImpact", min_length=64, max_length=64),
    ] = None,
    keep_worktree: Annotated[bool, Query(alias="keepWorktree")] = False,
    permanent: bool | None = None,
) -> None:
    try:
        if not permanent:
            from mato_loop_backend.sessions.state import is_session_deleted
            from mato_loop_backend.sessions.trash import move_to_trash

            item = await _deletable_session(db, session_id, user.id)
            if item is None:
                raise session_service.SessionServiceError(404, "session.notFound")
            preferences = await SettingsStore.load(db, UserPreferences, user_id=user.id)
            if permanent is False or preferences.trash_enabled:
                await move_to_trash(db, item, worktree_impact=worktree_impact)
                return
            if await is_session_deleted(db, item):
                raise session_service.SessionServiceError(409, "session.inTrash")
        await session_service.delete_session(
            session_id,
            user.id,
            db,
            automation_mode=automation_mode,
            automation_impact=automation_impact,
            storage_factory=SessionStorage,
            owned_session_query=partial(_deletable_session, allow_deleted=bool(permanent)),
            worktree_impact=worktree_impact,
            keep_worktree=keep_worktree,
        )
    except session_service.SessionServiceError as exc:
        _raise_api_error(exc)


@router.post("/{session_id}/messages/{message_id}/undo")
async def undo_user_message(
    session_id: UUID,
    message_id: UUID,
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, Any]:
    if await _mutable_session(db, session_id, user.id) is None:
        raise ApiError(404, "session.notFound")
    from mato_loop_backend.sessions.message_queue import hold_delivery_for_undo

    try:
        with hold_delivery_for_undo(session_id):
            result = await ChatHistoryService.undo_user_message(
                db,
                session_id=session_id,
                user_id=user.id,
                message_id=message_id,
            )
    except ChatHistoryError as exc:
        raise ApiError(exc.status_code, exc.code) from exc
    return {
        "draft": {"content": result.content},
    }


@router.post(
    "/{session_id}/messages/{message_id}/retry",
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_failed_message(
    session_id: UUID,
    message_id: UUID,
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, Any]:
    if await _mutable_session(db, session_id, user.id) is None:
        raise ApiError(404, "session.notFound")
    try:
        result = await ChatHistoryService.retry_failed_message(
            db,
            session_id=session_id,
            user_id=user.id,
            message_id=message_id,
        )
    except ChatHistoryError as exc:
        raise ApiError(exc.status_code, exc.code) from exc
    except AgentRunError as exc:
        raise ApiError(409, exc.code, params=exc.params) from exc
    return {
        "runId": str(result.run.id),
        "agentProfileId": (
            str(result.run.agent_profile_id) if result.run.agent_profile_id else None
        ),
        "modelName": result.run.model_name,
        "createdAt": result.run.created_at.isoformat() if result.run.created_at else None,
        **run_timing_payload(result.run),
    }


@router.get(
    "/{session_id}/messages/{message_id}/attachments/{attachment_id}",
    response_class=FileResponse,
)
async def download_attachment(
    session_id: UUID,
    message_id: UUID,
    attachment_id: UUID,
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    playback: bool = False,
) -> Response:
    attachment = await db.scalar(
        select(MessageAttachment)
        .join(Message, Message.id == MessageAttachment.message_id)
        .where(
            MessageAttachment.id == attachment_id,
            Message.id == message_id,
            Message.session_id == session_id,
            Message.chat_session.has(
                and_(
                    ChatSession.user_id == user.id,
                    ChatSession.session_type == "chat",
                )
            ),
        )
    )
    if attachment is None:
        raise ApiError(404, "attachment.notFound")
    try:
        if playback:
            return Response(
                await AttachmentService().read_audio(attachment, session_id),
                media_type="audio/wav",
                headers={
                    "Content-Disposition": "inline",
                    "X-Content-Type-Options": "nosniff",
                    "Cross-Origin-Resource-Policy": "same-origin",
                    "Cache-Control": "private, no-store",
                },
            )
        path = await AttachmentService().verified_path(attachment, session_id)
    except AttachmentError as exc:
        logger.warning("Attachment file for existing metadata was not found")
        raise ApiError(404, "attachment.notFound") from exc
    filename = safe_filename(attachment.filename)
    inline = attachment.kind in {"image", "audio"}
    return FileResponse(
        path,
        media_type=attachment.mime_type,
        filename=filename,
        content_disposition_type="inline" if inline else "attachment",
        headers={
            "X-Content-Type-Options": "nosniff",
            "Cross-Origin-Resource-Policy": "same-origin",
            "Cache-Control": "private, no-store",
        },
    )


async def _send_message(
    session_id: UUID,
    payload: SendMessageRequest,
    user: UserAccount,
    db: AsyncSession,
    attachments: list[AttachmentInput] | None = None,
) -> dict[str, Any]:
    try:
        return await session_service.send_message(
            session_id,
            payload,
            user,
            db,
            attachments=attachments,
            owned_session_query=_mutable_session,
            duplicate_submission=_duplicate_submission_response,
            message_serializer=message_payload,
        )
    except session_service.SessionServiceError as exc:
        _raise_api_error(exc)


@router.post("/{session_id}/messages", status_code=status.HTTP_202_ACCEPTED)
async def send_message(
    session_id: UUID,
    payload: SendMessageRequest,
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, Any]:
    if payload.voice_attachment_indices:
        raise ApiError(422, "validation.invalid", field="voiceAttachmentIndices")
    return await _send_message(session_id, payload, user, db)


@router.post("/{session_id}/messages/with-attachments", status_code=status.HTTP_202_ACCEPTED)
async def send_message_with_attachments(
    session_id: UUID,
    payload: Annotated[str, Form()],
    attachments: Annotated[list[UploadFile], File()],
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, Any]:
    if await _mutable_session(db, session_id, user.id) is None:
        raise ApiError(404, "session.notFound")
    request = _multipart_payload(payload, SendMessageRequest)
    assert isinstance(request, SendMessageRequest)
    duplicate = await _duplicate_submission_response(
        db, request.run_id, user.id, requested_session_id=session_id
    )
    if duplicate is not None:
        return duplicate
    instance_limits = await SettingsStore.load(db, InstanceLimits)
    try:
        return await _send_message(
            session_id,
            request,
            user,
            db,
            await _attachment_uploads(
                attachments, instance_limits, request.voice_attachment_indices
            ),
        )
    except AttachmentError as exc:
        raise _attachment_api_error(exc) from exc
    finally:
        for upload in attachments:
            await upload.close()


async def _continue_run_message(
    session_id: UUID,
    run_id: UUID,
    payload: ContinueRunRequest,
    user: UserAccount,
    db: AsyncSession,
    attachments: list[AttachmentInput] | None = None,
) -> dict[str, Any]:
    try:
        return await session_service.continue_run(
            session_id, run_id, payload, user, db,
            attachments=attachments,
            owned_session_query=_mutable_session,
            message_serializer=message_payload,
        )
    except session_service.SessionServiceError as exc:
        _raise_api_error(exc)


@router.post("/{session_id}/runs/{run_id}/messages", status_code=status.HTTP_202_ACCEPTED)
async def continue_run_message(
    session_id: UUID,
    run_id: UUID,
    payload: ContinueRunRequest,
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, Any]:
    if payload.voice_attachment_indices:
        raise ApiError(422, "validation.invalid", field="voiceAttachmentIndices")
    return await _continue_run_message(session_id, run_id, payload, user, db)


@router.post("/{session_id}/runs/{run_id}/messages/with-attachments", status_code=status.HTTP_202_ACCEPTED)
async def continue_run_message_with_attachments(
    session_id: UUID,
    run_id: UUID,
    payload: Annotated[str, Form()],
    attachments: Annotated[list[UploadFile], File()],
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, Any]:
    if await _mutable_session(db, session_id, user.id) is None:
        raise ApiError(404, "session.notFound")
    request = _multipart_payload(payload, ContinueRunRequest)
    assert isinstance(request, ContinueRunRequest)
    instance_limits = await SettingsStore.load(db, InstanceLimits)
    try:
        return await _continue_run_message(
            session_id, run_id, request, user, db,
            await _attachment_uploads(attachments, instance_limits, request.voice_attachment_indices),
        )
    except AttachmentError as exc:
        raise _attachment_api_error(exc) from exc
    finally:
        for upload in attachments:
            await upload.close()


@router.get("/{session_id}/runs/{run_id}")
async def get_run(
    session_id: UUID,
    run_id: UUID,
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict:
    if await _owned_session(db, session_id, user.id) is None:
        raise ApiError(404, "session.runNotFound")
    run = await db.get(AgentRun, run_id)
    if run is None or run.session_id != session_id or run.parent_run_id is not None:
        raise ApiError(404, "session.runNotFound")
    return run_payload(run)


@router.post(
    "/{session_id}/runs/{run_id}/cancel",
    status_code=status.HTTP_202_ACCEPTED,
)
async def cancel_run(
    session_id: UUID,
    run_id: UUID,
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    redirect: bool = False,
) -> dict:
    if await _mutable_session(db, session_id, user.id) is None:
        raise ApiError(404, "session.runNotFound")
    run = await db.get(AgentRun, run_id)
    if run is None or run.session_id != session_id or run.parent_run_id is not None:
        raise ApiError(404, "session.runNotFound")
    if run.status in {"completed", "failed", "cancelled"}:
        return run_payload(run)
    if not AgentRuntime.cancel(run_id, redirected=redirect):
        raise ApiError(409, "session.runInactive")
    return run_payload(run)


@router.get("/{session_id}/runs/{run_id}/events")
async def stream_run_events(
    session_id: UUID,
    run_id: UUID,
    user: Annotated[UserAccount, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> StreamingResponse:
    if await _owned_session(db, session_id, user.id) is None:
        raise ApiError(404, "session.runNotFound")
    run = await db.get(AgentRun, run_id)
    if run is None or run.session_id != session_id or run.parent_run_id is not None:
        raise ApiError(404, "session.runNotFound")
    run_status = run.status
    run_error = run.error
    run_agent_profile_id = (
        str(run.agent_profile_id) if run.agent_profile_id else None
    )
    run_timing = run_timing_payload(run)
    error_message = (
        await db.scalar(
            select(Message)
            .where(Message.error_run_id == run_id)
            .options(selectinload(Message.attachments))
            .limit(1)
        )
        if run_status == "failed"
        else None
    )
    error_params = error_message.error_params if error_message is not None else None
    error_message_payload = (
        message_payload(
            error_message,
            attribution=(run.id, run.agent_profile_id),
            attribution_resolved=True,
        )
        if error_message is not None
        else None
    )
    await db.rollback()

    async def events():
        if run_status in {"completed", "failed", "cancelled"}:
            if run_status == "failed":
                error = describe_error(
                    run_error or "agent.failed",
                    params=error_params,
                )
                if error_message_payload is not None:
                    event = {
                        "type": "message.completed",
                        "message": error_message_payload,
                    }
                    yield f"event: message.completed\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                event = {
                    "type": "run.failed",
                    "runId": str(run_id),
                    "agentProfileId": run_agent_profile_id,
                    "error": error.payload(),
                    **run_timing,
                }
            else:
                event = {
                    "type": f"run.{run_status}",
                    "runId": str(run_id),
                    "agentProfileId": run_agent_profile_id,
                    **run_timing,
                }
            yield f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
            return
        async for event in run_events.subscribe(run_id):
            event_type = event.get("type", "message")
            yield f"event: {event_type}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
            if event_type in {"run.completed", "run.failed", "run.cancelled"}:
                break

    return StreamingResponse(events(), media_type="text/event-stream")
