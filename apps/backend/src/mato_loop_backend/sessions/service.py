from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4, uuid5

from sqlalchemy import (
    JSON,
    String,
    and_,
    delete,
    func,
    literal_column,
    or_,
    select,
    tuple_,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, selectinload

from mato_loop_backend.agent.run_contracts import AgentRunError
from mato_loop_backend.agent.run_timing import run_timing_payload
from mato_loop_backend.agent.runtime import AgentRuntime
from mato_loop_backend.agent.session_secrets import session_secrets
from mato_loop_backend.agent.session_settings import RunSessionSettings
from mato_loop_backend.agent.todos import todo_snapshot
from mato_loop_backend.automations import automation_impact_token
from mato_loop_backend.chat_history import ChatHistoryService
from mato_loop_backend.execution import (
    DirectoryResult,
    ExecutionClient,
    ExecutionClientError,
    execution_agent_connections,
)
from mato_loop_backend.execution.client import _absolute_path
from mato_loop_backend.execution.paths import within
from mato_loop_backend.files.service import RootFileError, RootFileService
from mato_loop_backend.files.storage import SessionMoveReceipt, SessionStorageError
from mato_loop_backend.media.attachments import AttachmentInput, AttachmentService
from mato_loop_backend.memory.episode_indexing import EpisodeIndexService
from mato_loop_backend.models import (
    AgentRun,
    ApprovalRequest,
    Automation,
    AutomationRun,
    ChatSession,
    ContextSummary,
    ExecutionAgent,
    MemoryMode,
    Message,
    MessageAttachment,
    PermissionGrant,
    Project,
    QuestionRequest,
    SettingRecord,
    TitleGenerationStatus,
    UserAccount,
)
from mato_loop_backend.permissions.policy import execution_directory_scope
from mato_loop_backend.permissions.resolution import tool_execution_policy
from mato_loop_backend.settings import (
    AgentProfileUnavailable,
    AiSettings,
    InstanceLimits,
    SettingsStore,
    ToolExecutionPolicy,
    apply_session_ai,
    effective_model_trust,
    load_agent_profile,
    load_agent_profile_tools,
    load_effective_ai,
)
from mato_loop_backend.settings.effective import default_agent_profile_id
from mato_loop_backend.settings.models import ModelAvailability, ModelCapability
from mato_loop_backend.settings.session_profiles import SessionProfileSnapshot

from .cleanup import descendant_sessions

logger = logging.getLogger(__name__)
VISIBLE_MESSAGE_ROLES = ("user", "assistant")
ACTIVE_RUN_STATUSES = ("queued", "running", "waiting_approval", "waiting_input")


PayloadSerializer = Callable[..., Awaitable[dict[str, Any]]]
MessageSerializer = Callable[..., dict[str, Any]]


class SessionServiceError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        *,
        field: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.field = field
        self.params = params


def _latest_model_context(provider_data: Any) -> dict[str, int] | None:
    runtime_data = provider_data.get("_matoloop") if isinstance(provider_data, dict) else None
    performance = runtime_data.get("performance") if isinstance(runtime_data, dict) else None
    if not isinstance(performance, dict):
        return None
    estimated = performance.get("estimatedInputTokens")
    budget = performance.get("inputBudgetTokens")
    if type(estimated) is not int or estimated < 0 or type(budget) is not int or budget <= 0:
        return None
    return {"estimatedInputTokens": estimated, "inputBudgetTokens": budget}


def model_available(ai: AiSettings, model_id: UUID | None) -> bool:
    if model_id is None:
        return False
    model = next(
        (
            item
            for item in ai.models
            if item.id == model_id
            and item.enabled
            and item.availability != ModelAvailability.MISSING
            and ModelCapability.CHAT in item.capabilities
        ),
        None,
    )
    if model is None:
        return False
    provider = next(
        (item for item in ai.providers if item.id == model.provider_id and item.enabled), None
    )
    if provider is None:
        return False
    return provider.adapter_type != "openai-codex" or provider.credential_id is not None


def model_supports_tools(ai: AiSettings, model_id: UUID | None) -> bool:
    return any(
        item.id == model_id
        and ModelCapability.CHAT in item.capabilities
        and item.supports_tools
        for item in ai.models
    )


def model_for_session(ai: AiSettings, model_id: UUID | None):
    return next(
        (
            item
            for item in ai.models
            if item.id == model_id and ModelCapability.CHAT in item.capabilities
        ),
        None,
    )


def provider_for_session(ai: AiSettings, model):
    if model is None:
        return None
    return next((item for item in ai.providers if item.id == model.provider_id), None)


async def validate_agent_profile(
    db: AsyncSession, user_id: UUID, profile_id: UUID | None
) -> None:
    try:
        profile = await load_agent_profile(db, user_id, profile_id, for_update=True)
        if not profile.enabled:
            raise AgentProfileUnavailable
    except AgentProfileUnavailable as exc:
        raise SessionServiceError(
            409,
            "agentProfile.selectedUnavailable",
            field="agentProfileId",
        ) from exc


async def execution_home(
    db: AsyncSession, agent: ExecutionAgent
) -> str | None:
    del db
    if agent.home_directory is None:
        return None
    snapshot = execution_agent_connections.snapshot(agent.id)
    if snapshot is not None and "filesystem.resolve" in snapshot:
        try:
            result = await ExecutionClient(agent.id, snapshot).request(
                "filesystem.resolve", {"path": agent.home_directory}, DirectoryResult
            )
        except ExecutionClientError:
            return None
        assert isinstance(result, DirectoryResult)
        return result.path if result.path == agent.home_directory else None
    return agent.home_directory


def _path_is_within(path: str, parent: str) -> bool:
    return within(path, parent)


def execution_filesystem_policy(agent: ExecutionAgent, path: str) -> str:
    at_home = agent.home_directory is not None and _path_is_within(path, agent.home_directory)
    return agent.home_filesystem_policy if at_home else agent.external_filesystem_policy


async def add_directory_grant(
    db: AsyncSession,
    session_id: UUID,
    agent_id: UUID | None,
    path: str | None,
    policy: str | None,
    agent_profile_id: UUID | None,
    trust_level: str | None,
) -> None:
    if agent_id is None or path is None or policy not in {"auto", "ask"} or trust_level is None:
        return
    scope = execution_directory_scope(agent_id, path)
    existing = await db.scalar(
        select(PermissionGrant.id).where(
            PermissionGrant.session_id == session_id,
            PermissionGrant.run_id.is_(None),
            PermissionGrant.capability == "execution.directory",
            PermissionGrant.resource_type == "execution_directory",
            PermissionGrant.resource_scope == scope,
            PermissionGrant.agent_profile_id == agent_profile_id,
            PermissionGrant.trust_level == trust_level,
        )
    )
    if existing is None:
        db.add(
            PermissionGrant(
                session_id=session_id,
                capability="execution.directory",
                resource_type="execution_directory",
                resource_scope=scope,
                agent_profile_id=agent_profile_id,
                trust_level=trust_level,
            )
        )


async def validate_execution_preferences(
    db: AsyncSession,
    user_id: UUID,
    agent_id: UUID | None,
    current_directory: str | None,
    *,
    defer_unavailable: bool = False,
) -> tuple[str | None, str | None]:
    if agent_id is None:
        if current_directory is not None:
            raise SessionServiceError(
                400, "session.invalidWorkingDirectory", field="currentDirectory"
            )
        return None, None
    agent = await db.scalar(
        select(ExecutionAgent).where(
            ExecutionAgent.id == agent_id, ExecutionAgent.user_id == user_id
        )
    )
    if agent is None:
        raise SessionServiceError(
            404, "executionAgent.notFound", field="preferredExecutionAgentId"
        )
    if agent.revoked_at is not None and not defer_unavailable:
        raise SessionServiceError(
            409, "executionAgent.revoked", field="preferredExecutionAgentId"
        )
    selected = current_directory or (agent.home_directory if agent.revoked_at is not None else await execution_home(db, agent))
    if selected is None:
        return None, None
    try:
        selected = _absolute_path(selected)
    except ValueError as exc:
        raise SessionServiceError(400, "session.invalidWorkingDirectory", field="currentDirectory") from exc
    if agent.revoked_at is not None:
        return selected, None
    snapshot = execution_agent_connections.snapshot(agent.id)
    if snapshot is None or "filesystem.resolve" not in snapshot:
        if defer_unavailable:
            return selected, None
        raise SessionServiceError(409, "executionAgent.unavailable", field="currentDirectory")
    try:
        result = await ExecutionClient(agent.id, snapshot).request(
            "filesystem.resolve", {"path": selected}, DirectoryResult
        )
    except ExecutionClientError as exc:
        if defer_unavailable:
            return selected, None
        raise SessionServiceError(
            400, "session.invalidWorkingDirectory", field="currentDirectory"
        ) from exc
    assert isinstance(result, DirectoryResult)
    policy = execution_filesystem_policy(agent, result.path)
    if policy == "deny":
        raise SessionServiceError(403, "permission.denied", field="currentDirectory")
    return result.path, policy


async def workspace_context(db: AsyncSession, item: ChatSession) -> dict[str, Any]:
    agent_id = item.preferred_execution_agent_id
    context = {
        "executionAgentId": str(agent_id) if agent_id else None,
        "connected": False,
        "operations": [],
    }
    if agent_id is None:
        return context
    agent = await db.scalar(
        select(ExecutionAgent).where(
            ExecutionAgent.id == agent_id,
            ExecutionAgent.user_id == item.user_id,
            ExecutionAgent.revoked_at.is_(None),
        )
    )
    snapshot = execution_agent_connections.snapshot(agent_id)
    if agent is None or snapshot is None:
        return context
    return {**context, "connected": True, "operations": sorted(snapshot)}


async def owned_session(
    db: AsyncSession,
    session_id: UUID,
    user_id: UUID,
    *,
    for_update: bool = False,
    include_deleted: bool = False,
) -> ChatSession | None:
    statement = select(ChatSession).where(
        ChatSession.id == session_id,
        ChatSession.user_id == user_id,
        ChatSession.session_type == "chat",
    )
    if not include_deleted:
        from mato_loop_backend.sessions.state import deleted_session_clause

        statement = statement.where(~deleted_session_clause())
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    return await db.scalar(statement)


async def get_owned_session(
    db: AsyncSession,
    session_id: UUID,
    user_id: UUID,
    *,
    for_update: bool = False,
) -> ChatSession | None:
    from mato_loop_backend.sessions.state import require_session_available

    item = await owned_session(
        db, session_id, user_id, for_update=for_update, include_deleted=True
    )
    if item is not None and item.parent_session_id is not None:
        raise SessionServiceError(403, "session.readOnly")
    if item is not None:
        await require_session_available(db, item)
    return item


def unread_response_clause():
    tool_message = aliased(Message, name="silence_message")
    # Keep the partial-index predicate static even in generic prepared plans.
    runtime = "silence_message.provider_data['_matoloop']"
    silenced = select(tool_message.id).where(
        tool_message.session_id == Message.session_id,
        tool_message.role == literal_column("'tool'"),
        literal_column(f"{runtime} ->> 'runId'", String)
        == Message.provider_data["_matoloop"]["runId"].as_string(),
        literal_column(f"{runtime} ->> 'toolName'", String) == literal_column("'silence'"),
        literal_column(f"CAST(({runtime} ->> 'suppressResponseNotification') AS BOOLEAN)").is_(True),
    ).correlate(Message).exists()
    return and_(
        Message.role == "assistant",
        or_(Message.error_code.is_not(None), ~silenced),
    )


def has_unread_messages(
    item: ChatSession, last_assistant_message_at: datetime | None
) -> bool:
    return bool(
        last_assistant_message_at
        and (item.last_read_at is None or last_assistant_message_at > item.last_read_at)
    )


async def session_detail_payload(
    db: AsyncSession,
    item: ChatSession,
    *,
    session_serializer: PayloadSerializer,
    message_serializer: MessageSerializer,
    context_summary_serializer: MessageSerializer,
) -> dict[str, Any]:
    messages, context_summaries, next_message_cursor = await session_message_page(
        db,
        item.id,
        page_size=20,
        cursor=None,
    )
    attributions = await message_attributions(db, messages)
    latest_run = await db.scalar(
        select(AgentRun)
        .where(
            AgentRun.session_id == item.id,
            AgentRun.parent_run_id.is_(None),
        )
        .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
        .limit(1)
    )
    active_run = latest_run if latest_run and latest_run.status in ACTIVE_RUN_STATUSES else None
    if active_run is None or active_run.status in {"waiting_approval", "waiting_input"}:
        active_run = await db.scalar(select(AgentRun).where(
            AgentRun.session_id == item.id, AgentRun.parent_run_id.is_(None),
            AgentRun.status.in_(ACTIVE_RUN_STATUSES),
        ).order_by(AgentRun.status.in_(("running", "queued")).desc(),
                   AgentRun.created_at.desc(), AgentRun.id.desc()).limit(1))
    retry_message_id = None
    if item.parent_session_id is None and active_run is None and messages:
        retry_source = await ChatHistoryService.retry_source(
            db,
            session_id=item.id,
            messages=messages,
            message_id=messages[-1].id,
        )
        if retry_source is not None:
            retry_message_id = messages[-1].id
        elif next_message_cursor is not None:
            target = messages[-1]
            if target.role == "assistant" and target.error_code and target.error_run_id:
                failed_run = await db.get(AgentRun, target.error_run_id)
                if failed_run and failed_run.session_id == item.id and failed_run.status == "failed":
                    input_row = (await db.execute(select(Message.created_at, Message.id).where(
                        Message.id == failed_run.input_message_id,
                        Message.session_id == item.id, Message.role == "user",
                    ))).one_or_none()
                    if input_row is not None:
                        # Check the omitted suffix without loading message bodies. The
                        # retry action still revalidates the complete locked history.
                        run_id = Message.provider_data["_matoloop"]["runId"].as_string()
                        continuation = Message.provider_data["_matoloop"]["continuationRunId"].as_string()
                        conflict = await db.scalar(select(select(Message.id).where(
                            Message.session_id == item.id,
                            tuple_(Message.created_at, Message.id) > (input_row.created_at, input_row.id),
                            or_(
                                and_(run_id.is_not(None), run_id != str(failed_run.id)),
                                and_(Message.error_run_id.is_not(None), Message.error_run_id != failed_run.id),
                                and_(Message.role == "user", continuation.is_distinct_from(str(failed_run.id))),
                            ),
                        ).exists()))
                        if conflict is False:
                            retry_message_id = target.id
    performance_message = aliased(Message)
    latest_performance_provider_data = (
        select(performance_message.provider_data)
        .where(
            performance_message.session_id == item.id,
            performance_message.role == "assistant",
            performance_message.provider_data["_matoloop"].op("?")("performance"),
        )
        .order_by(performance_message.created_at.desc(), performance_message.id.desc())
        .limit(1)
        .scalar_subquery()
    )
    (
        last_message_at,
        last_assistant_message_at,
        user_request_count,
        latest_performance_data,
        last_user_message_at,
    ) = (
        await db.execute(
            select(
                func.max(Message.created_at).filter(
                    Message.role.in_(VISIBLE_MESSAGE_ROLES)
                ),
                func.max(Message.created_at).filter(unread_response_clause()),
                func.count(Message.id).filter(Message.role == "user"),
                latest_performance_provider_data,
                func.max(Message.created_at).filter(Message.role == "user"),
            ).where(Message.session_id == item.id)
        )
    ).one()
    todos = []
    snapshots = await db.stream_scalars(
        select(Message).where(
            Message.session_id == item.id,
            Message.role == "tool",
            Message.provider_data["_matoloop"]["toolName"].as_string() == "todo_write",
        ).order_by(Message.created_at.desc(), Message.id.desc())
        .execution_options(yield_per=20)
    )
    try:
        async for message in snapshots:
            snapshot = todo_snapshot(message, latest_run_id=latest_run.id if latest_run else None)
            if snapshot is not None:
                todos = snapshot
                break
    finally:
        await snapshots.close()
    return {
        **(
            await session_serializer(
                db,
                item,
                last_message_at=last_message_at,
                has_unread_messages=has_unread_messages(item, last_assistant_message_at),
            )
        ),
        "lastUserMessageAt": last_user_message_at.isoformat() if last_user_message_at else None,
        "messages": [
            message_serializer(
                message,
                retry_available=message.id == retry_message_id,
                attribution=attributions.get(message.id),
                attribution_resolved=True,
            )
            for message in messages
        ],
        "todos": todos,
        "latestRunId": str(latest_run.id) if latest_run else None,
        "contextSummaries": [
            context_summary_serializer(summary) for summary in context_summaries
        ],
        "nextMessageCursor": next_message_cursor,
        "userRequestCount": user_request_count,
        "latestModelContext": _latest_model_context(latest_performance_data),
        "activeRun": (
            {
                "id": str(active_run.id),
                "status": active_run.status,
                "modelName": active_run.model_name,
                "agentProfileId": (
                    str(active_run.agent_profile_id)
                    if active_run.agent_profile_id
                    else None
                ),
                "createdAt": active_run.created_at.isoformat() if active_run.created_at else None,
                **run_timing_payload(active_run),
            }
            if active_run
            else None
        ),
    }


async def session_message_page(
    db: AsyncSession,
    session_id: UUID,
    *,
    page_size: int,
    cursor: UUID | None,
) -> tuple[list[Message], list[ContextSummary], str | None]:
    upper_bound: tuple[datetime, UUID] | None = None
    if cursor is not None:
        cursor_row = (
            await db.execute(
                select(Message.created_at, Message.id).where(
                    Message.id == cursor,
                    Message.session_id == session_id,
                    Message.role != "tool",
                )
            )
        ).one_or_none()
        if cursor_row is None:
            raise SessionServiceError(400, "validation.invalid", field="cursor")
        upper_bound = (cursor_row.created_at, cursor_row.id)

    # Page by individual visible messages, not user turns. Keep each tool-call
    # carrier with its results so the transcript never receives orphan results.
    anchors_statement = select(
        Message.created_at, Message.id,
        (func.octet_length(Message.content)
         + func.least(func.coalesce(func.octet_length(Message.tool_calls.cast(String)), 0), 4_000)
         + func.coalesce(func.octet_length(Message.public_reasoning.cast(String)), 0)
         + 2_000).label("payload_bytes"),
    ).where(
        Message.session_id == session_id,
        Message.role != "tool",
    )
    if upper_bound is not None:
        anchors_statement = anchors_statement.where(
            tuple_(Message.created_at, Message.id) < upper_bound
        )
    anchors = (
        await db.execute(
            anchors_statement.order_by(Message.created_at.desc(), Message.id.desc()).limit(
                page_size + 1
            )
        )
    ).all()
    page_anchors = anchors[:page_size]
    # This is an approximate visible-message budget, not a response-size cap.
    # A carrier and its parallel results remain atomic; tool bodies/arguments are lazy.
    size = 0
    for index, anchor in enumerate(page_anchors):
        size += getattr(anchor, "payload_bytes", 0)
        if index and size > 192_000:
            page_anchors = page_anchors[:index]
            break
    if not page_anchors:
        # Legacy tool-only histories have no carrier to render safely.
        return [], [], None

    lower_bound = (page_anchors[-1].created_at, page_anchors[-1].id)
    messages_statement = select(Message).where(
        Message.session_id == session_id,
        Message.role != "tool",
        Message.id.in_([anchor.id for anchor in page_anchors]),
    )
    messages = list(
        (
            await db.scalars(
                messages_statement.options(selectinload(Message.attachments)).order_by(
                    Message.created_at, Message.id
                )
            )
        ).all()
    )
    call_ids = {
        call["id"] for message in messages if message.role == "assistant"
        for call in (
            *(message.tool_calls or []),
            *(message.provider_data or {}).get("_matoloop", {}).get("scriptToolCalls", {}).values(),
        )
        if isinstance(call, dict) and isinstance(call.get("id"), str)
    }
    if call_ids:
        carrier = aliased(Message)
        carrier_run = carrier.provider_data["_matoloop"]["runId"].as_string()
        result_run = Message.provider_data["_matoloop"]["runId"].as_string()
        # Reused legacy call IDs belong to the nearest compatible carrier, not
        # every page containing that ID. Results may follow a continuation user.
        result_carrier = select(carrier.id).where(
            carrier.session_id == Message.session_id,
            carrier.role == "assistant",
            or_(
                carrier.tool_calls.contains(func.jsonb_build_array(
                    func.jsonb_build_object("id", Message.tool_call_id),
                )),
                carrier.provider_data["_matoloop"]["scriptToolCalls"].op("?")(Message.tool_call_id),
            ),
            tuple_(carrier.created_at, carrier.id) < tuple_(Message.created_at, Message.id),
            or_(carrier_run.is_(None), result_run.is_(None), carrier_run == result_run),
        ).order_by(carrier.created_at.desc(), carrier.id.desc()).limit(1).correlate(Message).scalar_subquery()
        results = (await db.scalars(select(Message).where(
            Message.session_id == session_id,
            Message.role == "tool",
            Message.tool_call_id.in_(call_ids),
            result_carrier.in_([message.id for message in messages if message.role == "assistant"]),
        ).options(selectinload(Message.attachments)))).all()
        messages.extend(results)
        messages.sort(key=lambda message: (message.created_at, message.id))
    summaries_statement = select(ContextSummary).where(
        ContextSummary.session_id == session_id,
        tuple_(ContextSummary.created_at, ContextSummary.id) >= lower_bound,
    )
    if upper_bound is not None:
        summaries_statement = summaries_statement.where(
            tuple_(ContextSummary.created_at, ContextSummary.id) < upper_bound
        )
    context_summaries = list(
        (
            await db.scalars(
                summaries_statement.order_by(
                    ContextSummary.created_at, ContextSummary.id
                )
            )
        ).all()
    )
    next_cursor = str(lower_bound[1]) if len(anchors) > len(page_anchors) else None
    return messages, context_summaries, next_cursor


async def message_attributions(
    db: AsyncSession, messages: list[Message]
) -> dict[UUID, tuple[UUID, UUID | None]]:
    assistants = [message for message in messages if message.role == "assistant"]
    if not assistants:
        return {}
    stamped_messages: set[UUID] = set()
    stamped_run_ids: dict[UUID, UUID] = {}
    stamped_profile_ids: dict[UUID, UUID | None] = {}
    invalid_stamps: set[UUID] = set()
    for message in assistants:
        metadata = (message.provider_data or {}).get("_matoloop", {})
        if not isinstance(metadata, dict) or not (
            "runId" in metadata or "agentProfileId" in metadata
        ):
            continue
        stamped_messages.add(message.id)
        if "runId" not in metadata or "agentProfileId" not in metadata:
            invalid_stamps.add(message.id)
            continue
        try:
            stamped_run_id = UUID(metadata["runId"])
            stamped_profile_id = (
                UUID(metadata["agentProfileId"])
                if metadata.get("agentProfileId") is not None
                else None
            )
        except (AttributeError, KeyError, TypeError, ValueError):
            invalid_stamps.add(message.id)
            continue
        stamped_run_ids[message.id] = stamped_run_id
        stamped_profile_ids[message.id] = stamped_profile_id
    message_ids = {message.id for message in assistants}
    sessions_by_message = {message.id: message.session_id for message in assistants}
    session_ids = set(sessions_by_message.values())
    candidates: dict[UUID, set[UUID]] = {message.id: set() for message in assistants}
    error_run_ids = {
        message.error_run_id for message in assistants if message.error_run_id is not None
    }
    runs = list((await db.execute(select(
        AgentRun.id,
        AgentRun.agent_profile_id,
        AgentRun.result_message_id,
        AgentRun.session_id,
        Message.provider_data.label("input_provider_data"),
    ).outerjoin(Message, Message.id == AgentRun.input_message_id).where(
        or_(
            AgentRun.result_message_id.in_(message_ids),
            AgentRun.id.in_(error_run_ids | set(stamped_run_ids.values())),
        ),
        AgentRun.session_id.in_(session_ids),
    ))).all())
    run_sessions = {row.id: row.session_id for row in runs}
    profiles = {row.id: row.agent_profile_id for row in runs}
    historical_profiles = {
        row.id: ((getattr(row, "input_provider_data", None) or {}).get("_matoloop", {})
                 .get("agentProfileIdsByRun", {}).get(str(row.id), []))
        for row in runs
    }
    for message in assistants:
        if (
            message.id not in stamped_messages
            and message.error_run_id is not None
            and run_sessions.get(message.error_run_id) == message.session_id
        ):
            candidates[message.id].add(message.error_run_id)
    for row in runs:
        if (
            row.result_message_id in candidates
            and row.result_message_id not in stamped_messages
            and row.session_id == sessions_by_message[row.result_message_id]
        ):
            candidates[row.result_message_id].add(row.id)

    automation_rows = list((await db.execute(
        select(
            AutomationRun.output_message_id,
            AgentRun.id,
            AgentRun.agent_profile_id,
            AgentRun.session_id,
        )
        .join(AgentRun, AgentRun.id == AutomationRun.agent_run_id)
        .where(
            AutomationRun.output_message_id.in_(message_ids),
        )
    )).all())
    automation_links: dict[UUID, set[UUID]] = {}
    for row in automation_rows:
        automation_links.setdefault(row.output_message_id, set()).add(row.id)
        if row.output_message_id not in stamped_messages:
            candidates[row.output_message_id].add(row.id)
        profiles[row.id] = row.agent_profile_id
        run_sessions[row.id] = row.session_id

    resolved = {}
    for message_id, run_id in stamped_run_ids.items():
        automation_run_ids = automation_links.get(message_id)
        valid_run = (
            automation_run_ids == {run_id}
            if automation_run_ids
            else run_sessions.get(run_id) == sessions_by_message[message_id]
        )
        stamped_profile = stamped_profile_ids[message_id]
        if valid_run and (profiles.get(run_id) == stamped_profile
                          or str(stamped_profile) in historical_profiles.get(run_id, [])):
            resolved[message_id] = (run_id, stamped_profile)
        else:
            invalid_stamps.add(message_id)

    calls_by_message = {
        message.id: {
            str(call["id"])
            for call in message.tool_calls or []
            if isinstance(call, dict) and call.get("id")
        }
        for message in assistants
        if message.id not in stamped_messages
    }
    tool_call_ids = set().union(*calls_by_message.values())
    if tool_call_ids:
        tool_rows = list((await db.execute(
            select(Message.session_id, Message.tool_call_id, Message.provider_data).where(
                Message.role == "tool",
                Message.session_id.in_({message.session_id for message in assistants}),
                Message.tool_call_id.in_(tool_call_ids),
            )
        )).all())
        tool_runs: dict[tuple[UUID, str], set[UUID]] = {}
        for row in tool_rows:
            raw_run_id = (row.provider_data or {}).get("_matoloop", {}).get("runId")
            try:
                linked_run_id = UUID(raw_run_id) if isinstance(raw_run_id, str) else None
            except ValueError:
                linked_run_id = None
            if linked_run_id is not None and row.tool_call_id is not None:
                tool_runs.setdefault((row.session_id, row.tool_call_id), set()).add(linked_run_id)
        linked_run_ids = set().union(*tool_runs.values()) if tool_runs else set()
        if linked_run_ids:
            profile_rows = list((await db.execute(
                select(AgentRun.id, AgentRun.agent_profile_id, AgentRun.session_id).where(
                    AgentRun.id.in_(linked_run_ids),
                    AgentRun.session_id.in_(session_ids),
                )
            )).all())
            profiles.update({row.id: row.agent_profile_id for row in profile_rows})
            run_sessions.update({row.id: row.session_id for row in profile_rows})
        for message in assistants:
            if message.id in stamped_messages:
                continue
            for call_id in calls_by_message[message.id]:
                candidates[message.id].update(
                    run_id
                    for run_id in tool_runs.get((message.session_id, call_id), ())
                    if run_sessions.get(run_id) == message.session_id
                )

    resolved.update({
        message_id: (run_id, profiles.get(run_id))
        for message_id, run_ids in candidates.items()
        if message_id not in invalid_stamps and len(run_ids) == 1
        for run_id in run_ids
    })
    return resolved


async def latest_user_message(db: AsyncSession, session_id: UUID) -> Message:
    message = await db.scalar(
        select(Message)
        .where(Message.session_id == session_id, Message.role == "user")
        .options(selectinload(Message.attachments))
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(1)
    )
    if message is None:
        raise RuntimeError("Persisted user message is missing after agent start")
    return message


async def find_message_submission(
    db: AsyncSession, user_id: UUID, request_id: UUID,
) -> tuple[UUID, UUID] | None:
    from mato_loop_backend.sessions.state import deleted_session_clause

    row = (await db.execute(
        select(AgentRun.session_id, Message.id)
        .join(ChatSession, ChatSession.id == AgentRun.session_id)
        .join(Message, (Message.id == AgentRun.input_message_id) & (Message.session_id == ChatSession.id))
        .where(
            AgentRun.id == request_id,
            AgentRun.parent_run_id.is_(None),
            ChatSession.parent_session_id.is_(None),
            ChatSession.user_id == user_id,
            ChatSession.session_type == "chat",
            ~deleted_session_clause(),
            Message.role == "user",
        )
    )).one_or_none()
    return (row[0], row[1]) if row is not None else None


async def duplicate_submission_response(
    db: AsyncSession,
    run_id: UUID | None,
    user_id: UUID,
    *,
    requested_session_id: UUID | None = None,
    session_serializer: PayloadSerializer,
    message_serializer: MessageSerializer,
) -> dict[str, Any] | None:
    if run_id is None:
        return None
    run = await db.scalar(
        select(AgentRun)
        .join(ChatSession, ChatSession.id == AgentRun.session_id)
        .where(
            AgentRun.id == run_id,
            AgentRun.parent_run_id.is_(None),
            ChatSession.user_id == user_id,
            ChatSession.session_type == "chat",
        )
    )
    if run is None:
        return None
    item = await get_owned_session(db, run.session_id, user_id)
    if requested_session_id is not None and run.session_id != requested_session_id:
        raise SessionServiceError(409, "http.conflict")
    message = await db.scalar(
        select(Message)
        .where(
            Message.id == run.input_message_id,
            Message.session_id == run.session_id,
            Message.role == "user",
        )
        .options(selectinload(Message.attachments))
    )
    if item is None or message is None:
        raise RuntimeError("Persisted agent run input is missing")
    if run.status == "queued":
        AgentRuntime._schedule_run(run)
    response = {
        "runId": str(run.id),
        "agentProfileId": str(run.agent_profile_id) if run.agent_profile_id else None,
        "modelName": run.model_name,
        **run_timing_payload(run),
        "message": message_serializer(message),
    }
    if requested_session_id is not None:
        return response
    return {
        "session": await session_serializer(db, item, last_message_at=message.created_at),
        **response,
    }


def _encode_session_cursor(
    user_id: UUID,
    scope: str,
    priority: bool,
    activity_at: datetime,
    session_id: UUID,
) -> str:
    value = json.dumps(
        [str(user_id), scope, priority, activity_at.isoformat(), str(session_id)],
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _decode_session_cursor(
    value: str, user_id: UUID, scope: str
) -> tuple[bool, datetime, UUID]:
    try:
        decoded = base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
        )
        payload = json.loads(decoded)
        if not (
            isinstance(payload, list)
            and len(payload) == 5
            and all(
                isinstance(item, str)
                for item in (payload[0], payload[1], payload[3], payload[4])
            )
            and payload[1] == scope
            and type(payload[2]) is bool
            and UUID(payload[0]) == user_id
        ):
            raise ValueError
        activity_at = datetime.fromisoformat(payload[3])
        if activity_at.tzinfo is None:
            raise ValueError
        return payload[2], activity_at, UUID(payload[4])
    except (binascii.Error, UnicodeDecodeError, ValueError, TypeError) as exc:
        raise SessionServiceError(400, "validation.invalid", field="cursor") from exc


async def list_sessions(
    db: AsyncSession,
    user_id: UUID,
    *,
    page_size: int,
    cursor: str | None,
    project_id: UUID | None,
    standalone: bool,
    session_serializer_factory: Callable[
        [AsyncSession, list[ChatSession]], Awaitable[PayloadSerializer]
    ],
    archived: bool = False,
    include_archived: bool = False,
    sort: Literal["pinned", "recent"] = "pinned",
    session_ids: list[UUID] | None = None,
    home_session_ids: list[UUID] | None = None,
) -> dict[str, Any]:
    from mato_loop_backend.sessions.archive import active_session_clause
    from mato_loop_backend.sessions.state import deleted_session_clause

    if project_id is not None and standalone:
        raise SessionServiceError(400, "validation.invalid", field="projectId")
    if project_id is not None:
        owned_project = await db.scalar(
            select(Project.id).where(
                Project.id == project_id, Project.user_id == user_id, Project.delete_at.is_(None)
            )
        )
        if owned_project is None:
            raise SessionServiceError(404, "project.notFound", field="projectId")
    scope = str(project_id) if project_id is not None else "standalone" if standalone else "all"
    if archived or include_archived:
        scope += ":archived" if archived else ":including-archived"
    scope += f":{sort}:" + ("relevant-activity" if sort == "recent" else "user-message")
    if session_ids is not None:
        scope += ":ids:" + ",".join(sorted({str(item) for item in session_ids}))
    active_run_status = (
        select(AgentRun.status)
        .where(
            AgentRun.session_id == ChatSession.id,
            AgentRun.parent_run_id.is_(None),
            AgentRun.status.in_(ACTIVE_RUN_STATUSES),
        )
        .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
        .limit(1)
        .correlate(ChatSession)
        .scalar_subquery()
    )
    session_filters = [
        ChatSession.user_id == user_id,
        ChatSession.session_type == "chat",
        ChatSession.parent_session_id.is_(None),
        ~deleted_session_clause(),
    ]
    if session_ids is not None:
        session_filters.append(ChatSession.id.in_(session_ids))
    else:
        session_filters.append(~select(SettingRecord.id).where(
            SettingRecord.user_id == user_id,
            SettingRecord.key == "assistant",
            SettingRecord.data["session_id"].astext == ChatSession.id.cast(String),
        ).correlate(ChatSession).exists())
    if project_id is not None:
        session_filters.append(ChatSession.project_id == project_id)
    elif standalone:
        session_filters.append(ChatSession.project_id.is_(None))
    if not include_archived:
        session_filters.append(~active_session_clause() if archived else active_session_clause())
        if archived and project_id is None:
            session_filters.append(~select(Project.id).where(
                Project.id == ChatSession.project_id, Project.archived_at.is_not(None),
            ).correlate(ChatSession).exists())
    # Seek the newest matching row in the session/creation index instead of
    # aggregating every message before the session page can be selected.
    def latest_message_at(condition):
        return (
            select(Message.created_at)
            .where(Message.session_id == ChatSession.id, condition)
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(1)
            .correlate(ChatSession)
            .scalar_subquery()
        )

    last_message_at = latest_message_at(Message.role.in_(VISIBLE_MESSAGE_ROLES))
    last_assistant_message_at = latest_message_at(unread_response_clause())
    last_user_message_at = latest_message_at(Message.role == "user")
    activity_at = func.coalesce(last_user_message_at, ChatSession.created_at)
    if sort == "recent":
        legacy_response = aliased(Message)
        legacy_response_id = (
            select(legacy_response.id)
            .where(
                legacy_response.session_id == AgentRun.session_id,
                legacy_response.role == "assistant",
                legacy_response.created_at >= AgentRun.created_at,
                legacy_response.created_at <= AgentRun.completed_at,
                legacy_response.generation_status == "completed",
                or_(legacy_response.tool_calls.is_(None), legacy_response.tool_calls == JSON.NULL, legacy_response.tool_calls == []),
                legacy_response.provider_data["_matoloop"]["runId"].as_string().is_(None),
                legacy_response.provider_data["_matoloop"]["continuationSegment"].as_boolean().is_not(True),
            )
            .order_by(legacy_response.created_at.desc(), legacy_response.id.desc())
            .limit(1)
            .correlate(AgentRun)
            .scalar_subquery()
        )
        last_response_at = (
            select(AgentRun.completed_at)
            .join(Message, Message.id == func.coalesce(AgentRun.result_message_id, legacy_response_id))
            .where(
                AgentRun.session_id == ChatSession.id,
                AgentRun.parent_run_id.is_(None),
                AgentRun.status.in_(("completed", "failed", "cancelled")),
                AgentRun.completed_at.is_not(None),
                unread_response_clause(),
            )
            .order_by(AgentRun.completed_at.desc())
            .limit(1)
            .correlate(ChatSession)
            .scalar_subquery()
        )
        last_question_at = (
            select(func.max(QuestionRequest.created_at))
            .where(QuestionRequest.session_id == ChatSession.id)
            .correlate(ChatSession)
            .scalar_subquery()
        )
        last_approval_at = (
            select(func.max(ApprovalRequest.created_at))
            .where(ApprovalRequest.session_id == ChatSession.id, ApprovalRequest.user_id == user_id)
            .correlate(ChatSession)
            .scalar_subquery()
        )
        activity_at = func.greatest(activity_at, last_response_at, last_question_at, last_approval_at)
    priority = ChatSession.is_pinned
    statement = (
        select(
            ChatSession,
            last_message_at.label("last_message_at"),
            last_assistant_message_at.label("last_assistant_message_at"),
            active_run_status.label("active_run_status"),
            activity_at.label("activity_at"),
            last_user_message_at.label("last_user_message_at"),
        )
        .where(*session_filters)
    )
    if home_session_ids is not None:
        pending_approval = select(ApprovalRequest.id).where(
            ApprovalRequest.session_id == ChatSession.id,
            ApprovalRequest.user_id == user_id,
            ApprovalRequest.status == "pending",
            ApprovalRequest.action_kind != "terminal_command",
        ).correlate(ChatSession).exists()
        pending_question = select(QuestionRequest.id).where(
            QuestionRequest.session_id == ChatSession.id,
            QuestionRequest.status == "pending",
        ).correlate(ChatSession).exists()
        candidates = statement.add_columns(
            func.row_number().over(order_by=(activity_at.desc(), ChatSession.id.desc())).label("home_rank"),
            or_(
                active_run_status.is_not(None), pending_approval, pending_question,
                and_(
                    last_assistant_message_at.is_not(None),
                    or_(ChatSession.last_read_at.is_(None),
                        last_assistant_message_at > ChatSession.last_read_at),
                ),
            ).label("home_relevant"),
        ).cte("home_sessions")
        home_session = aliased(ChatSession, candidates)
        rows = (await db.execute(select(
            home_session, candidates.c.last_message_at, candidates.c.last_assistant_message_at,
            candidates.c.active_run_status, candidates.c.activity_at, candidates.c.last_user_message_at,
        ).where(or_(
            candidates.c.home_rank <= page_size, candidates.c.home_relevant,
            candidates.c.id.in_(home_session_ids),
        )).order_by(candidates.c.home_rank))).all()
    elif cursor is not None:
        cursor_priority, cursor_activity_at, cursor_id = _decode_session_cursor(
            cursor, user_id, scope
        )
        activity_tail = or_(
            activity_at < cursor_activity_at,
            and_(activity_at == cursor_activity_at, ChatSession.id < cursor_id),
        )
        if sort == "pinned":
            activity_tail = (
                or_(priority.is_(False), and_(priority.is_(True), activity_tail))
                if cursor_priority
                else and_(priority.is_(False), activity_tail)
            )
        statement = statement.where(activity_tail)
    if home_session_ids is None:
        if sort == "pinned":
            statement = statement.order_by(priority.desc())
        rows = (
            await db.execute(
                statement.order_by(
                    activity_at.desc(),
                    ChatSession.id.desc(),
                ).limit(page_size + 1)
            )
        ).all()
    page = rows if home_session_ids is not None else rows[:page_size]
    if not page:
        return {"items": [], "nextCursor": None}
    session_serializer = await session_serializer_factory(db, [row[0] for row in page])
    items = [
        {
            **(
                await session_serializer(
                    db,
                    item,
                    last_message_at=last_message_at,
                    has_unread_messages=has_unread_messages(
                        item, last_assistant_message_at
                    ),
                )
            ),
            "activeRunStatus": active_status,
            **({"activityAt": _activity_at.isoformat()} if sort == "recent" else {}),
            "lastUserMessageAt": last_user_message_at.isoformat() if last_user_message_at else None,
        }
        for item, last_message_at, last_assistant_message_at, active_status, _activity_at, last_user_message_at in page
    ]
    next_cursor = None
    if home_session_ids is None and len(rows) > page_size:
        item, _last_message_at, _last_assistant_message_at, _active_status, item_activity_at, _last_user_message_at = page[-1]
        next_cursor = _encode_session_cursor(
            user_id, scope,
            bool(item.is_pinned) if sort == "pinned" else False,
            item_activity_at, item.id,
        )
    return {"items": items, "nextCursor": next_cursor}


async def create_session(
    payload,
    user,
    db: AsyncSession,
    *,
    attachments: list[AttachmentInput] | None,
    storage_factory,
    session_serializer: PayloadSerializer,
    message_serializer: MessageSerializer,
    duplicate_submission,
    commit: bool = True,
) -> dict[str, Any]:
    duplicate = await duplicate_submission(db, payload.run_id, user.id)
    if duplicate is not None:
        return duplicate
    content = payload.content.strip() if payload.content is not None else None
    from mato_loop_backend.sessions.commands import command_profile_id, parse_command

    command = parse_command(content) if content else None
    if content == "" and not attachments:
        raise SessionServiceError(422, "validation.required", field="content")
    await db.execute(select(UserAccount.id).where(UserAccount.id == user.id).with_for_update(key_share=True))
    project = None
    if payload.project_id is not None:
        project = await db.scalar(
            select(Project)
            .where(Project.id == payload.project_id, Project.user_id == user.id)
            .with_for_update()
        )
        if project is None:
            raise SessionServiceError(404, "project.notFound", field="projectId")
        if project.delete_at is not None:
            raise SessionServiceError(409, "project.inTrash")
        if project.archived_at is not None:
            raise SessionServiceError(409, "project.archived")
        if payload.run_id is not None:
            duplicate = await duplicate_submission(db, payload.run_id, user.id)
            if duplicate is not None:
                return duplicate
    from mato_loop_backend.sessions.templates import (
        load_template,
        validate_chat_model,
        validate_session_profile,
    )
    from mato_loop_backend.settings.session_profiles import ChatModelOverride

    template = await load_template(db, user.id, payload.session_template_id or (project.default_session_template_id if project else None))
    configuration = payload.session_profile or template.snapshot()
    # Initial client notes cannot self-declare public classification or a host revision.
    configuration.context_state = configuration.context_state.model_copy(update={"trust_level": "private", "revision": 0})
    configuration.template_id = template.id
    configuration.worktrees_enabled = template.worktrees_enabled
    agent_profile_id = command_profile_id(command, payload.agent_profile_id or template.start_agent_id)
    if configuration.agent_ids is not None and agent_profile_id not in configuration.agent_ids:
        raise SessionServiceError(422, "agentProfile.selectedUnavailable")
    fields = payload.model_fields_set
    configured_model = payload.model_id if "model_id" in fields else template.model_id
    configured_reasoning = payload.reasoning_effort if "reasoning_effort" in fields else template.reasoning_effort
    ai = await validate_session_profile(db, user.id, configuration, model_id=configured_model, defer_unavailable=True)
    validate_chat_model(ai, ChatModelOverride(model_id=configured_model, reasoning_effort=configured_reasoning), defer_unavailable=True)
    await validate_agent_profile(db, user.id, agent_profile_id)
    agent_id = (
        payload.preferred_execution_agent_id
        if "preferred_execution_agent_id" in fields or project is None
        else project.default_execution_agent_id
    )
    requested_directory = (
        payload.current_directory
        if "current_directory" in fields or project is None
        else (
            project.default_current_directory
            if "preferred_execution_agent_id" not in fields
            else None
        )
    )
    workspace_mode = (command.initial_workspace_mode if command else None) or payload.workspace_mode or (project.git_workspace_mode if project and configuration.worktrees_enabled else None) or "local"
    if workspace_mode == "worktree" and not configuration.worktrees_enabled:
        raise SessionServiceError(409, "sessionTemplate.worktreesDisabled")
    item = ChatSession(
        id=uuid5(user.id, str(payload.run_id)) if payload.run_id and workspace_mode == "worktree" else uuid4(),
        user_id=user.id,
        project_id=payload.project_id,
        title="Neue Session",
        title_generation_status=TitleGenerationStatus.PENDING.value,
        model_config_id=configured_model,
        trust_level=payload.trust_level.value if payload.trust_level else None,
        session_profile=configuration.model_dump(mode="json"),
        agent_profile_id=agent_profile_id,
        reasoning_effort=configured_reasoning.value if configured_reasoning else None,
        memory_mode=payload.memory_mode.value if "memory_mode" in fields else template.memory_mode,
        web_search_policy=payload.web_search_policy.value,
        preferred_execution_agent_id=agent_id,
        workspace_mode=workspace_mode,
        control_center=payload.control_center.model_dump(mode="json"),
    )
    ai = apply_session_ai(ai, item)
    model_id = ai.default_model_id
    current_directory, directory_policy = await validate_execution_preferences(
        db, user.id, agent_id, requested_directory, defer_unavailable=content is not None,
    )
    item.current_directory = current_directory
    if item.workspace_mode == "worktree":
        if payload.git_fetch_before_actions is not None:
            item.coding_workspace = {
                "executionAgentId": str(agent_id), "repository": current_directory, "path": current_directory,
                "fetchBeforeActions": payload.git_fetch_before_actions,
            }
        from mato_loop_backend.sessions.coding import select_session_workspace

        if payload.workspace_path is None and project and current_directory != project.default_current_directory:
            raise SessionServiceError(409, "git.projectWorkspaceRequired")
        directory_policy = await select_session_workspace(db, item, project, payload.workspace_path, create=payload.workspace_path is None)
        current_directory = item.current_directory
    elif (project and current_directory
          and agent_id == project.default_execution_agent_id
          and current_directory == project.default_current_directory):
        item.coding_workspace = {
            "mode": "local", "repository": current_directory, "path": current_directory,
            "remote": project.git_remote, "targetBranch": project.git_target_branch,
            "executionAgentId": str(agent_id),
        }
    if payload.git_fetch_before_actions is not None:
        item.coding_workspace = {
            **(item.coding_workspace or {
                "executionAgentId": str(agent_id), "repository": current_directory, "path": current_directory,
            }),
            "fetchBeforeActions": payload.git_fetch_before_actions,
        }
    try:
        db.add(item)
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        duplicate = await duplicate_submission(db, payload.run_id, user.id)
        if duplicate is not None:
            return duplicate
        raise SessionServiceError(409, "http.conflict") from exc
    from mato_loop_backend.sessions.coding import persist_worktree_warning

    await persist_worktree_warning(db, item)
    item_id = item.id
    item_project_id = item.project_id
    session_storage = storage_factory()
    if model_available(ai, model_id):
        selected_model = model_for_session(ai, model_id)
        selected_provider = provider_for_session(ai, selected_model)
        assert selected_model is not None and selected_provider is not None
        trust_level = effective_model_trust(selected_provider, selected_model)
        if content is not None:
            await add_directory_grant(
                db,
                item.id,
                agent_id,
                current_directory,
                directory_policy,
                item.agent_profile_id,
                trust_level.value,
            )
        if (project and project.default_execution_agent_id is not None
                and project.default_execution_agent_id == agent_id and project.default_current_directory):
            tools, _ = await load_agent_profile_tools(db, user.id, item.agent_profile_id, trust_level)
            if (tools.tool_availability.get("read", True)
                    and tool_execution_policy(tools, "filesystem.read") == ToolExecutionPolicy.AUTO
                    and tool_execution_policy(tools, "working_directory.external") != ToolExecutionPolicy.DISABLED):
                db.add(PermissionGrant(
                    session_id=item.id,
                    capability="category.read",
                    resource_type="execution_path",
                    resource_scope=execution_directory_scope(agent_id, project.default_current_directory, recursive=True),
                    agent_profile_id=item.agent_profile_id,
                    trust_level=trust_level.value,
                ))
    request_id = payload.run_id
    await db.flush()
    try:
        await asyncio.to_thread(session_storage.provision, item_id, item_project_id, user_id=user.id)
        if content is None:
            if commit:
                await ChatHistoryService._commit(db)
            else:
                await db.flush()
        else:
            run = await AgentRuntime.start(
                db,
                item,
                content,
                run_id=request_id,
                attachments=attachments,
                active_file=await active_file_payload(payload.active_file, item, db),
            )
    except (OSError, SessionStorageError) as exc:
        await db.rollback()
        logger.exception("Session storage could not be provisioned")
        raise SessionServiceError(500, "session.storageUnavailable") from exc
    except AgentRunError as exc:
        await db.rollback()
        try:
            await asyncio.to_thread(
                session_storage.remove_session, item_id, item_project_id
            )
        except Exception:
            logger.exception("Unpersisted session storage could not be removed")
        raise SessionServiceError(409, exc.code, params=exc.params) from exc
    except IntegrityError as exc:
        await db.rollback()
        try:
            await asyncio.to_thread(
                session_storage.remove_session, item_id, item_project_id
            )
        except Exception:
            logger.exception("Unpersisted session storage could not be removed")
        duplicate = await duplicate_submission(db, payload.run_id, user.id)
        if duplicate is not None:
            return duplicate
        raise SessionServiceError(409, "http.conflict") from exc
    except Exception:
        await db.rollback()
        raise
    await db.refresh(item)
    if content is None:
        return {"session": await session_serializer(db, item, last_message_at=None)}
    user_message = await latest_user_message(db, item.id)
    return {
        "session": await session_serializer(
            db, item, last_message_at=user_message.created_at
        ),
        "message": message_serializer(user_message),
        "runId": str(run.id),
        "agentProfileId": str(run.agent_profile_id) if run.agent_profile_id else None,
        "modelName": run.model_name,
        **run_timing_payload(run),
    }


async def active_file_payload(
    active_file, session: ChatSession, db: AsyncSession
) -> dict[str, Any] | None:
    if active_file is None:
        return None
    if active_file.root == "session":
        try:
            identity = UUID(active_file.root_identity)
            path = RootFileService(UUID(int=0)).normalize_relative_path(active_file.path)
        except (RootFileError, TypeError, ValueError) as exc:
            raise SessionServiceError(422, "validation.invalid", field="activeFile") from exc
        if str(identity) != active_file.root_identity or path == ".":
            raise SessionServiceError(422, "validation.invalid", field="activeFile")
        if identity != session.id and (
            session.project_id is None
            or await db.scalar(
                select(ChatSession.id)
                .where(
                    ChatSession.id == identity,
                    ChatSession.user_id == session.user_id,
                    ChatSession.project_id == session.project_id,
                    ChatSession.session_type == "chat",
                )
                .with_for_update()
            )
            is None
        ):
            raise SessionServiceError(422, "validation.invalid", field="activeFile")
    else:
        try:
            path = _absolute_path(active_file.path)
        except ValueError as exc:
            raise SessionServiceError(422, "validation.invalid", field="activeFile") from exc
        if (
            active_file.root_identity is not None
            or active_file.execution_agent_id != session.preferred_execution_agent_id
        ):
            raise SessionServiceError(422, "validation.invalid", field="activeFile")
    return {
        "root": active_file.root,
        **(
            {"rootIdentity": active_file.root_identity}
            if active_file.root == "session"
            else {"executionAgentId": str(active_file.execution_agent_id)}
        ),
        "path": path,
        "sha256": active_file.sha256,
        "dirty": active_file.dirty,
    }


async def continue_run(
    session_id: UUID,
    run_id: UUID,
    payload,
    user,
    db: AsyncSession,
    *,
    attachments: list[AttachmentInput] | None,
    owned_session_query,
    message_serializer: MessageSerializer,
    allow_pending_interactions: bool = True,
) -> dict[str, Any]:
    session = await owned_session_query(db, session_id, user.id, for_update=True)
    if session is None:
        raise SessionServiceError(404, "session.notFound")
    from mato_loop_backend.sessions.archive import is_session_archived

    if await is_session_archived(db, session):
        raise SessionServiceError(409, "session.archived")
    message = await db.scalar(
        select(Message).where(Message.id == payload.message_id).options(selectinload(Message.attachments))
    )
    if message is not None:
        if message.session_id != session_id or (message.provider_data or {}).get("_matoloop", {}).get("continuationRunId") != str(run_id):
            raise SessionServiceError(409, "http.conflict")
        return {"message": message_serializer(message), "runId": str(run_id)}
    content = payload.content.strip()
    from mato_loop_backend.sessions.commands import parse_command

    if parse_command(content) is not None:
        raise SessionServiceError(409, "session.runAlreadyActive")
    if not content and not attachments:
        raise SessionServiceError(422, "validation.required", field="content")
    run = await db.scalar(select(AgentRun).where(
        AgentRun.id == run_id,
        AgentRun.session_id == session_id,
        AgentRun.parent_run_id.is_(None),
    ).with_for_update().execution_options(populate_existing=True))
    if run is None or run.status not in ACTIVE_RUN_STATUSES:
        raise SessionServiceError(409, "session.runInactive")
    if not allow_pending_interactions and run.status in {"waiting_input", "waiting_approval"}:
        return {"requiresUi": True}
    requested_profile = getattr(payload, "agent_profile_id", None)
    if requested_profile is not None:
        from mato_loop_backend.settings.effective import session_agent_allowed

        await validate_agent_profile(db, user.id, requested_profile)
        if not session_agent_allowed(session, requested_profile):
            raise SessionServiceError(409, "agentProfile.selectedUnavailable", field="agentProfileId")
    reservation = AgentRuntime.reserve_continuation(run_id, queued=run.status == "queued")
    if reservation is None:
        raise SessionServiceError(409, "session.runInactive")
    attachment_service = AttachmentService()
    created = []
    accepted = False
    try:
        attachment_service.settings = await SettingsStore.load(db, InstanceLimits)
        active_file = await active_file_payload(payload.active_file, session, db)
        message = Message(
            id=payload.message_id,
            session_id=session_id,
            role="user",
            content=content,
            trust_level=run.trust_level,
            source_type="app",
            provider_data={"_matoloop": {
                "continuationRunId": str(run_id),
                **({"requestedAgentProfileId": str(requested_profile)} if requested_profile is not None else {}),
                **({"activeFile": active_file} if active_file is not None else {}),
            }},
        )
        db.add(message)
        await db.flush()
        await EpisodeIndexService.enqueue(db, message)
        for position, attachment in enumerate(attachments or []):
            created.append(await attachment_service.create_attachment(
                db, message, attachment.data, attachment.mime_type,
                kind=attachment.kind, filename=attachment.filename,
                position=position, metadata=attachment.metadata, category="user_upload",
            ))
        await db.commit()
        accepted = True
    except BaseException:
        await db.rollback()
        for item in created:
            try:
                await attachment_service.discard(item, session_id)
            except BaseException:
                logger.exception("Could not discard failed continuation attachment")
        raise
    finally:
        AgentRuntime.finish_continuation(run_id, reservation, accepted=accepted)
    await db.refresh(message, ["attachments"])
    return {"message": message_serializer(message), "runId": str(run_id)}


async def start_message(db: AsyncSession, item: ChatSession, content: str, profile_id: UUID, **runtime_options) -> AgentRun:
    await validate_agent_profile(db, item.user_id, profile_id)
    ai = apply_session_ai(await load_effective_ai(db, item.user_id), item, profile_id)
    if await db.scalar(select(Message.id).where(Message.session_id == item.id).limit(1)) is None:
        directory, directory_policy = await validate_execution_preferences(
            db, item.user_id, item.preferred_execution_agent_id, item.current_directory, defer_unavailable=True,
        )
        model = model_for_session(ai, ai.default_model_id)
        provider = provider_for_session(ai, model)
        await add_directory_grant(
            db, item.id, item.preferred_execution_agent_id, directory, directory_policy,
            profile_id, effective_model_trust(provider, model).value if provider and model else None,
        )
    return await AgentRuntime.start(db, item, content, **runtime_options)


async def send_message(
    session_id: UUID,
    payload,
    user,
    db: AsyncSession,
    *,
    attachments: list[AttachmentInput] | None,
    owned_session_query,
    duplicate_submission,
    message_serializer: MessageSerializer,
) -> dict[str, Any]:
    await db.execute(select(UserAccount.id).where(UserAccount.id == user.id).with_for_update(key_share=True))
    item = await owned_session_query(db, session_id, user.id, for_update=True)
    if item is None:
        raise SessionServiceError(404, "session.notFound")
    duplicate = await duplicate_submission(
        db, payload.run_id, user.id, requested_session_id=session_id
    )
    if duplicate is not None:
        return duplicate
    content = payload.content.strip()
    from mato_loop_backend.voice.admission import active_connection

    if await active_connection(db, session_id):
        raise SessionServiceError(409, "voice.busy")
    if not content and not attachments:
        raise SessionServiceError(422, "validation.required", field="content")
    after_run_id = getattr(payload, "after_run_id", None)
    if after_run_id is not None and not getattr(payload, "enqueue", False):
        raise SessionServiceError(422, "validation.invalid", field="after_run_id")
    if getattr(payload, "enqueue", False):
        from mato_loop_backend.sessions.message_queue import enqueue, schedule_delivery

        request_id = payload.run_id or uuid4()
        entry = await enqueue(db, item, request_id, {
            "content": content,
            **({"agent_profile_id": str(payload.agent_profile_id)} if payload.agent_profile_id else {}),
            "active_file": await active_file_payload(payload.active_file, item, db),
        }, attachments, after_run_id=after_run_id)
        await db.commit()
        if entry is not None:
            schedule_delivery(session_id)
        return {"queued": True, "runId": str(request_id)}
    active = await db.scalar(
        select(AgentRun).where(
            AgentRun.session_id == session_id,
            AgentRun.parent_run_id.is_(None),
            AgentRun.status.in_(ACTIVE_RUN_STATUSES),
        )
    )
    if active:
        raise SessionServiceError(409, "session.runAlreadyActive")
    requested_profile = (
        payload.agent_profile_id or await default_agent_profile_id(db)
        if "agent_profile_id" in payload.model_fields_set
        else item.agent_profile_id
    )
    try:
        run = await start_message(
            db,
            item,
            content,
            requested_profile,
            run_id=payload.run_id,
            attachments=attachments,
            active_file=await active_file_payload(payload.active_file, item, db),
            **(
                {"agent_profile_id_override": requested_profile}
                if "agent_profile_id" in payload.model_fields_set
                else {}
            ),
        )
    except AgentRunError as exc:
        raise SessionServiceError(409, exc.code, params=exc.params) from exc
    except IntegrityError as exc:
        await db.rollback()
        duplicate = await duplicate_submission(
            db, payload.run_id, user.id, requested_session_id=session_id
        )
        if duplicate is not None:
            return duplicate
        raise SessionServiceError(409, "http.conflict") from exc
    user_message = await latest_user_message(db, item.id)
    return {
        "runId": str(run.id),
        "agentProfileId": str(run.agent_profile_id) if run.agent_profile_id else None,
        "modelName": run.model_name,
        **run_timing_payload(run),
        "message": message_serializer(user_message),
    }


async def update_session(
    session_id: UUID,
    payload,
    user,
    db: AsyncSession,
    *,
    storage_factory,
    owned_session_query,
    session_serializer: PayloadSerializer,
) -> dict[str, Any]:
    fields = payload.model_fields_set
    execution_fields = {
        "preferred_execution_agent_id",
        "current_directory",
    }
    policy_fields = {
        "session_template_id",
        "session_profile",
        "project_id",
        "memory_mode",
        "model_id",
        "agent_profile_id",
        "reasoning_effort",
        "web_search_policy",
        "preferred_execution_agent_id",
        "current_directory",
    }
    if fields & (policy_fields | {"control_center", "todos_collapsed", "trust_level"}):
        if fields & execution_fields:
            await db.execute(select(UserAccount.id).where(UserAccount.id == user.id).with_for_update(key_share=True))
        await db.scalar(
            select(ChatSession.id)
            .where(ChatSession.id == session_id, ChatSession.user_id == user.id)
            .with_for_update()
        )
    item = await owned_session_query(db, session_id, user.id)
    if item is None:
        raise SessionServiceError(404, "session.notFound")
    from mato_loop_backend.voice.admission import active_connection

    if fields & (policy_fields | {"trust_level"}) and await active_connection(db, session_id):
        raise SessionServiceError(409, "voice.busy")
    if "session_template_id" in fields:
        from mato_loop_backend.sessions.templates import load_template

        template = await load_template(db, user.id, payload.session_template_id)
        payload = payload.model_copy(update={
            "session_profile": template.snapshot(),
            "agent_profile_id": template.start_agent_id,
            "model_id": template.model_id,
            "reasoning_effort": template.reasoning_effort,
            "memory_mode": MemoryMode(template.memory_mode),
        })
        fields = payload.model_fields_set
    target_profile_id = (
        payload.agent_profile_id or await default_agent_profile_id(db)
        if "agent_profile_id" in fields
        else item.agent_profile_id
    )
    original_settings = None
    active_run_ids = []
    if fields & policy_fields:
        from mato_loop_backend.sessions.archive import is_session_archived

        if await is_session_archived(db, item):
            raise SessionServiceError(409, "session.archived")
        active_run_ids = list((await db.scalars(
            select(AgentRun.id).where(
                AgentRun.session_id == session_id,
                AgentRun.parent_run_id.is_(None),
                AgentRun.status.in_(ACTIVE_RUN_STATUSES),
            )
        )).all())
        if active_run_ids:
            if "project_id" in fields:
                raise SessionServiceError(409, "session.runAlreadyActive")
            original_settings = RunSessionSettings.model_validate(item)
    source_project_id = item.project_id
    target_project_id = (
        payload.project_id if "project_id" in fields else source_project_id
    )
    if target_project_id is not None and target_project_id != source_project_id:
        project = await db.scalar(
            select(Project.id)
            .where(
                Project.id == target_project_id,
                Project.user_id == user.id,
                Project.archived_at.is_(None),
                Project.delete_at.is_(None),
            )
            .with_for_update()
        )
        if project is None:
            raise SessionServiceError(404, "project.notFound", field="projectId")
    if target_project_id != source_project_id:
        await _ensure_session_move_has_no_external_file_references(db, session_id)
    if target_project_id != source_project_id:
        for grant in list(
            (
                await db.scalars(
                    select(PermissionGrant).where(
                        PermissionGrant.session_id == session_id
                    )
                )
            ).all()
        ):
            await db.delete(grant)
    if (
        "agent_profile_id" in fields
        and target_profile_id
        != item.agent_profile_id
    ) or (
        "session_profile" in fields
        and payload.session_profile is not None
        and payload.session_profile.model_dump(exclude={"message_size", "agent_models", "context_state", "assistant_context", "keyboard_agent_ids"})
        != SessionProfileSnapshot.model_validate(item.session_profile or {}).model_dump(exclude={"message_size", "agent_models", "context_state", "assistant_context", "keyboard_agent_ids"})
    ):
        await db.execute(
            delete(PermissionGrant).where(
                PermissionGrant.session_id == session_id,
                PermissionGrant.run_id.is_(None),
            )
        )
    if "title" in fields and payload.title is not None:
        item.title = payload.title
        item.title_generation_status = TitleGenerationStatus.SKIPPED.value
    if "pinned" in fields and payload.pinned is not None:
        item.is_pinned = payload.pinned
    if "control_center" in fields:
        control_center = payload.control_center.model_dump(mode="json")
        if "todos_collapsed" in (item.control_center or {}):
            control_center["todos_collapsed"] = item.control_center["todos_collapsed"]
        item.control_center = control_center
    if "todos_collapsed" in fields and payload.todos_collapsed is not None:
        item.control_center = {
            **(item.control_center or {}), "todos_collapsed": payload.todos_collapsed,
        }
    if "session_profile" in fields and payload.session_profile is not None:
        from mato_loop_backend.sessions.templates import validate_session_profile

        await validate_session_profile(
            db, user.id, payload.session_profile,
            model_id=payload.model_id if "model_id" in fields else item.model_config_id,
            previous=SessionProfileSnapshot.model_validate(item.session_profile or {}),
        )
        # Context changes use revision-checked state, not profile metadata.
        # update_session holds the owner-bound session row lock before profile effects.
        await db.refresh(item, attribute_names=["session_profile"])
        stored_context = SessionProfileSnapshot.model_validate(item.session_profile or {}).context_state
        item.session_profile = {**payload.session_profile.model_dump(mode="json"), "context_state": stored_context.model_dump(mode="json")}
    if fields & {"session_profile", "agent_profile_id"}:
        from mato_loop_backend.settings.effective import session_agent_allowed

        if not session_agent_allowed(item, target_profile_id):
            raise SessionServiceError(422, "agentProfile.selectedUnavailable")
    if fields & {"model_id", "agent_profile_id", "session_profile"}:
        ai = await load_effective_ai(db, user.id)
        requested_model_id = (
            payload.model_id if "model_id" in fields else item.model_config_id
        )
        if "model_id" in fields and requested_model_id is not None and not model_available(ai, requested_model_id):
            raise SessionServiceError(400, "session.modelUnavailable")
        if "model_id" in fields:
            item.model_config_id = payload.model_id
        ai = apply_session_ai(ai, item, target_profile_id)
        effective_model_id = ai.default_model_id
        if not model_available(ai, effective_model_id):
            raise SessionServiceError(409, "session.modelUnavailable")
        if not model_supports_tools(ai, effective_model_id):
            raise SessionServiceError(409, "session.modelToolsUnavailable")
    if "agent_profile_id" in fields:
        agent_profile_id = target_profile_id
        await validate_agent_profile(db, user.id, agent_profile_id)
        item.agent_profile_id = agent_profile_id
    if "reasoning_effort" in fields:
        item.reasoning_effort = (
            payload.reasoning_effort.value if payload.reasoning_effort else None
        )
    if "memory_mode" in fields and payload.memory_mode is not None:
        item.memory_mode = payload.memory_mode.value
    if "trust_level" in fields:
        if payload.trust_level is None:
            raise SessionServiceError(422, "validation.required", field="trustLevel")
        item.trust_level = payload.trust_level.value
    if "web_search_policy" in fields and payload.web_search_policy is not None:
        if payload.web_search_policy.value == "required":
            ai = apply_session_ai(await load_effective_ai(db, user.id), item, target_profile_id)
            model_id = ai.default_model_id
            if not model_supports_tools(ai, model_id):
                raise SessionServiceError(409, "session.modelToolsUnavailable")
        item.web_search_policy = payload.web_search_policy.value
    if fields & execution_fields:
        ai = apply_session_ai(await load_effective_ai(db, user.id), item, target_profile_id)
        model = model_for_session(ai, ai.default_model_id)
        provider = provider_for_session(ai, model)
        agent_id = (
            payload.preferred_execution_agent_id
            if "preferred_execution_agent_id" in fields
            else item.preferred_execution_agent_id
        )
        current_directory = (
            payload.current_directory
            if "current_directory" in fields
            else item.current_directory
        )
        selected_new_agent = (
            "preferred_execution_agent_id" in fields
            and agent_id != item.preferred_execution_agent_id
        )
        if selected_new_agent:
            current_directory = (
                payload.current_directory if "current_directory" in fields else None
            )
            item.coding_workspace = None
        item.current_directory, directory_policy = await validate_execution_preferences(
            db, user.id, agent_id, current_directory
        )
        item.preferred_execution_agent_id = agent_id
        await add_directory_grant(
            db,
            item.id,
            agent_id,
            item.current_directory,
            directory_policy,
            item.agent_profile_id,
            (
                effective_model_trust(provider, model).value
                if model is not None and provider is not None
                else None
            ),
        )
    storage = storage_factory()
    move_receipt: SessionMoveReceipt | None = None

    async def restore_storage() -> None:
        nonlocal move_receipt
        if move_receipt is None:
            return
        receipt, move_receipt = move_receipt, None
        try:
            await asyncio.to_thread(storage.rollback_session_move, receipt)
        except BaseException:
            logger.exception("Session storage could not be restored after project move")

    async def finalize_storage() -> None:
        nonlocal move_receipt
        if move_receipt is None:
            return
        receipt, move_receipt = move_receipt, None
        try:
            await asyncio.to_thread(storage.finalize_session_move, receipt)
        except BaseException:
            logger.exception("Session storage move could not be finalized")

    async def resolve_storage_after_commit_error() -> None:
        nonlocal move_receipt
        await db.rollback()
        if move_receipt is None:
            return
        receipt, move_receipt = move_receipt, None
        try:
            row = (
                await db.execute(
                    select(ChatSession.project_id)
                    .where(ChatSession.id == item.id)
                    .with_for_update()
                )
            ).one_or_none()
            persisted_project_id = row[0] if row is not None else None
        except BaseException:
            try:
                await db.rollback()
            except BaseException:
                logger.exception("Database rollback failed while resolving session move")
            await asyncio.to_thread(storage.abandon_session_move, receipt)
            logger.exception(
                "Session storage move outcome remains journaled after commit failure"
            )
            return
        try:
            if row is not None and persisted_project_id == receipt.source_project_id:
                await asyncio.to_thread(storage.rollback_session_move, receipt)
            else:
                await asyncio.to_thread(storage.finalize_session_move, receipt)
        except BaseException:
            logger.exception("Session storage move could not be reconciled after commit failure")
        finally:
            await db.rollback()

    commit_task: asyncio.Task[None] | None = None
    try:
        if target_project_id != source_project_id:
            stage_task = asyncio.create_task(
                asyncio.to_thread(
                    storage.stage_session_move,
                    item.id,
                    source_project_id,
                    target_project_id,
                )
            )
            try:
                move_receipt = await asyncio.shield(stage_task)
            except asyncio.CancelledError:
                move_receipt = await stage_task
                raise
            item.project_id = target_project_id
        await db.flush()
        await db.refresh(item)
        for settings_run_id in active_run_ids:
            active_task = AgentRuntime._tasks.get(settings_run_id)
            if original_settings is not None and active_task is not None and not active_task.done():
                AgentRuntime._session_settings.setdefault(settings_run_id, original_settings)
        commit_task = asyncio.create_task(db.commit())
        await asyncio.shield(commit_task)
        await finalize_storage()
    except asyncio.CancelledError:
        if commit_task is None:
            await db.rollback()
            await restore_storage()
            raise
        try:
            await commit_task
        except BaseException:
            await resolve_storage_after_commit_error()
        else:
            await finalize_storage()
        raise
    except SessionStorageError as exc:
        await db.rollback()
        await restore_storage()
        if exc.code == "file.pathAlreadyExists":
            raise SessionServiceError(409, exc.code) from exc
        if exc.code == "file.userQuotaExceeded":
            raise SessionServiceError(507, exc.code) from exc
        raise SessionServiceError(500, "session.storageUnavailable") from exc
    except OSError as exc:
        await db.rollback()
        await restore_storage()
        raise SessionServiceError(500, "session.storageUnavailable") from exc
    except BaseException:
        if commit_task is None:
            await db.rollback()
            await restore_storage()
        else:
            await resolve_storage_after_commit_error()
        raise
    return await session_serializer(db, item)


async def session_delete_impact(
    session_id: UUID,
    user_id: UUID,
    db: AsyncSession,
    *,
    owned_session_query,
) -> dict[str, Any]:
    from mato_loop_backend.sessions.coding import (
        scheduled_worktree_is_missing,
        session_worktree,
        sessions_in_worktree,
        worktree_registration,
        worktree_removal_token,
    )

    item = await owned_session_query(db, session_id, user_id)
    if item is None:
        raise SessionServiceError(404, "session.notFound")
    linked = list(
        (
            await db.scalars(
                select(Automation).where(
                    Automation.user_id == user_id,
                    Automation.execution_session_id == session_id,
                )
            )
        ).all()
    )
    deleted_session_ids = [
        session_id,
        *(child_id for child_id, _ in await descendant_sessions(db, [session_id])),
    ]
    scheduled = item.trash_worktree_removal
    impact = {
        "automationCount": len(linked),
        "requiredAutomationCount": len(linked),
        "automationImpact": automation_impact_token(linked),
        "hasWorktree": bool(scheduled) or (item.coding_workspace or {}).get("mode") == "worktree",
        "worktree": {"path": scheduled["path"], "scheduled": True,
                     "removalError": {"code": "git.worktreeChanged"}} if scheduled else None,
        "fileReferenceCount": await _external_file_reference_count_for_roots(
            db, deleted_session_ids,
        ),
    }
    try:
        async with asyncio.timeout(3):
            if scheduled and await scheduled_worktree_is_missing(db, item, scheduled):
                impact.update(hasWorktree=False, worktree=None)
                return impact
            info = await session_worktree(db, item, check_removal=True)
            if info is not None:
                affected = await sessions_in_worktree(db, user_id, item.preferred_execution_agent_id, info.repository)
                error = info.removal_error if info.removal_checked else {"code": "executionAgent.updateRequired"}
                registered_at = await worktree_registration(db, item, info)
                token = worktree_removal_token(item, info, affected, registered_at)
                if scheduled and scheduled["impact"] != token:
                    error = {"code": "git.worktreeChanged"}
                impact["worktree"] = {"path": scheduled["path"] if scheduled else info.repository,
                                     "impact": token, "removalError": error,
                                     **({"scheduled": True} if scheduled else {}),
                                     "sessions": [{"id": str(other.id), "title": other.title} for other in affected if other.id not in deleted_session_ids]}
    except (SessionServiceError, TimeoutError) as exc:
        # Session-only deletion must not depend on optional computer discovery.
        if scheduled:
            impact["worktree"]["removalError"] = {
                "code": exc.code if isinstance(exc, SessionServiceError) else "executionAgent.unavailable",
            }
    return impact


async def _external_file_reference_count_for_roots(
    db: AsyncSession, session_ids: list[UUID]
) -> int:
    return int(
        await db.scalar(
            select(func.count())
            .select_from(MessageAttachment)
            .join(Message, Message.id == MessageAttachment.message_id)
            .where(
                MessageAttachment.file_root_id.in_(session_ids),
                Message.session_id.not_in(session_ids),
            )
        )
        or 0
    )


async def _ensure_session_move_has_no_external_file_references(
    db: AsyncSession, session_id: UUID
) -> None:
    count = int(
        await db.scalar(
            select(func.count())
            .select_from(MessageAttachment)
            .join(Message, Message.id == MessageAttachment.message_id)
            .where(
                or_(
                    and_(
                        MessageAttachment.file_root_id == session_id,
                        Message.session_id != session_id,
                    ),
                    and_(
                        Message.session_id == session_id,
                        MessageAttachment.file_root_id.is_not(None),
                        MessageAttachment.file_root_id != session_id,
                    ),
                )
            )
        )
        or 0
    )
    if count:
        raise SessionServiceError(
            409, "session.filesReferenced", params={"referenceCount": count}
        )


async def delete_session(
    session_id: UUID,
    user_id: UUID,
    db: AsyncSession,
    *,
    automation_mode: Literal["unlink", "deleteAutomations"] | None,
    automation_impact: str | None,
    storage_factory,
    owned_session_query,
    worktree_impact: str | None = None,
    keep_worktree: bool = False,
    expired_only: bool = False,
) -> None:
    item = await db.scalar(
        select(ChatSession).where(
            ChatSession.id == session_id,
            ChatSession.user_id == user_id,
            ChatSession.session_type == "chat",
        )
    )
    if item is None:
        raise SessionServiceError(404, "session.notFound")
    if item.parent_session_id is not None:
        raise SessionServiceError(403, "session.readOnly")
    from mato_loop_backend.voice.admission import active_connection

    if expired_only and (item.delete_at is None or item.delete_at > datetime.now(UTC)):
        return

    if await active_connection(db, session_id):
        raise SessionServiceError(409, "voice.busy")
    initial_descendants = await descendant_sessions(db, [session_id])
    initial_reference_count = await _external_file_reference_count_for_roots(
        db, [session_id, *(child_id for child_id, _ in initial_descendants)]
    )
    if initial_reference_count:
        raise SessionServiceError(
            409,
            "session.filesReferenced",
            params={"referenceCount": initial_reference_count},
        )
    linked_automations = list(
        (
            await db.scalars(
                select(Automation).where(
                    Automation.user_id == user_id,
                    Automation.execution_session_id == session_id,
                )
            )
        ).all()
    )
    required_count = len(linked_automations)
    if automation_mode is not None and automation_impact != automation_impact_token(
        linked_automations
    ):
        raise SessionServiceError(409, "session.automationImpactChanged")
    if required_count and automation_mode != "deleteAutomations":
        raise SessionServiceError(
            409,
            "session.automationOutputRequired",
            params={
                "automationCount": required_count,
                "totalAutomationCount": len(linked_automations),
            },
        )
    active_runs = list(
        (
            await db.scalars(
                select(AgentRun).where(
                    AgentRun.session_id == session_id,
                    AgentRun.parent_run_id.is_(None),
                    AgentRun.status.in_(ACTIVE_RUN_STATUSES),
                )
            )
        ).all()
    )
    active_run_ids = [run.id for run in active_runs]
    descendant_roots = [session_id]
    if active_run_ids:
        await db.rollback()
        for run_id in active_run_ids:
            await AgentRuntime.cancel_and_wait(run_id)
    await db.rollback()
    for root_id in descendant_roots:
        await AgentRuntime.cancel_descendants(root_id)
    # The pending selection can change while cancellation releases the session lock.
    await db.execute(select(UserAccount.id).where(UserAccount.id == user_id).with_for_update(key_share=True))
    linked_automations = list(
        (
            await db.scalars(
                select(Automation)
                .where(
                    Automation.user_id == user_id,
                    Automation.execution_session_id == session_id,
                )
                .with_for_update()
            )
        ).all()
    )
    item = await owned_session_query(db, session_id, user_id, for_update=True)
    if item is None:
        raise SessionServiceError(404, "session.notFound")
    if expired_only and (item.delete_at is None or item.delete_at > datetime.now(UTC)):
        return
    if await active_connection(db, session_id):
        raise SessionServiceError(409, "voice.busy")
    linked_automations = list(
        (
            await db.scalars(
                select(Automation)
                .where(
                    Automation.user_id == user_id,
                    Automation.execution_session_id == session_id,
                )
                .with_for_update()
            )
        ).all()
    )
    required_count = len(linked_automations)
    if automation_mode is not None and automation_impact != automation_impact_token(
        linked_automations
    ):
        raise SessionServiceError(409, "session.automationImpactChanged")
    if required_count and automation_mode != "deleteAutomations":
        raise SessionServiceError(
            409,
            "session.automationOutputRequired",
            params={
                "automationCount": required_count,
                "totalAutomationCount": len(linked_automations),
            },
        )
    if automation_mode == "deleteAutomations" and linked_automations:
        late_agent_run_ids = list(
            (
                await db.scalars(
                    select(AgentRun.id)
                    .join(AutomationRun, AutomationRun.agent_run_id == AgentRun.id)
                    .where(
                        AutomationRun.automation_id.in_(
                            [automation.id for automation in linked_automations]
                        ),
                        AgentRun.status.in_(ACTIVE_RUN_STATUSES),
                    )
                )
            ).all()
        )
        if late_agent_run_ids:
            await db.rollback()
            for run_id in late_agent_run_ids:
                await AgentRuntime.cancel_and_wait(run_id)
            return await delete_session(
                session_id,
                user_id,
                db,
                automation_mode=automation_mode,
                automation_impact=automation_impact,
                storage_factory=storage_factory,
                owned_session_query=owned_session_query,
                worktree_impact=worktree_impact,
                keep_worktree=keep_worktree,
                expired_only=expired_only,
            )
    session_storage = storage_factory()
    storage_sessions = [
        (session_id, item.project_id),
    ]
    storage_sessions.extend(
        await descendant_sessions(db, [storage_id for storage_id, _ in storage_sessions])
    )
    storage_session_ids = [storage_id for storage_id, _ in storage_sessions]
    await db.execute(
        select(ChatSession.id)
        .where(ChatSession.id.in_(storage_session_ids))
        .with_for_update()
    )
    reference_count = await _external_file_reference_count_for_roots(
        db, storage_session_ids
    )
    if reference_count:
        raise SessionServiceError(
            409,
            "session.filesReferenced",
            params={"referenceCount": reference_count},
        )
    if (await db.scalars(
        select(AgentRun.id).where(
            AgentRun.session_id.in_(storage_session_ids),
            AgentRun.status.in_(ACTIVE_RUN_STATUSES),
        ).limit(1)
    )).all():
        raise SessionServiceError(409, "session.runAlreadyActive")
    trashed_trees = []
    try:
        for storage_session_id, project_id in storage_sessions:
            trashed_trees.append(
                await asyncio.to_thread(
                    session_storage.trash_session, storage_session_id, project_id
                )
            )
    except (OSError, SessionStorageError) as exc:
        for (storage_session_id, project_id), trashed in zip(
            storage_sessions, trashed_trees, strict=False
        ):
            if trashed is not None:
                try:
                    await asyncio.to_thread(
                        session_storage.restore_session,
                        storage_session_id,
                        trashed,
                        project_id,
                    )
                except Exception:
                    logger.exception("Partially trashed session storage could not be restored")
        logger.exception("Session storage could not be moved to trash")
        raise SessionServiceError(500, "session.storageUnavailable") from exc

    async def restore_tree() -> None:
        for (storage_session_id, project_id), trashed in zip(
            storage_sessions, trashed_trees, strict=False
        ):
            if trashed is None:
                continue
            try:
                await asyncio.to_thread(
                    session_storage.restore_session,
                    storage_session_id,
                    trashed,
                    project_id,
                )
            except Exception:
                logger.exception("Session storage could not be restored after database failure")

    async def purge_tree() -> None:
        await asyncio.to_thread(session_storage.forget_sessions, storage_session_ids)
        for trashed in trashed_trees:
            try:
                await asyncio.to_thread(session_storage.purge_trashed, trashed)
            except Exception:
                logger.exception("Deleted session storage could not be removed from trash")

    commit_task: asyncio.Task[None] | None = None
    try:
        if not keep_worktree and (item.trash_worktree_removal or worktree_impact is not None):
            from mato_loop_backend.sessions.coding import remove_session_worktree

            await remove_session_worktree(db, item, impact=worktree_impact, scheduled=item.trash_worktree_removal)
        for automation in linked_automations:
            if automation_mode == "deleteAutomations":
                await db.delete(automation)
        if automation_mode == "deleteAutomations":
            await db.flush()
        from mato_loop_backend.assistant import clear_assistant_binding

        await clear_assistant_binding(db, user_id, session_ids=[session_id])
        await db.delete(item)
        commit_task = asyncio.create_task(db.commit())
        await asyncio.shield(commit_task)
    except asyncio.CancelledError:
        if commit_task is None:
            await db.rollback()
            await restore_tree()
            raise
        try:
            await commit_task
        except BaseException:
            await db.rollback()
            await restore_tree()
        else:
            session_secrets.clear_sessions(storage_session_ids)
            await purge_tree()
        raise
    except BaseException:
        await db.rollback()
        await restore_tree()
        raise
    session_secrets.clear_sessions(storage_session_ids)
    await purge_tree()
