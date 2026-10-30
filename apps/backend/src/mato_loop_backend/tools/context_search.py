from __future__ import annotations

from mato_loop_backend.config import get_runtime_config
from mato_loop_backend.memory.embeddings import EmbeddingError, embedding_client
from mato_loop_backend.memory.episode_retrieval import EpisodeRetrievalService
from mato_loop_backend.memory.retrieval import MemoryRetrievalService, MemoryRetrieveMode
from mato_loop_backend.memory.service import memory_reference_timestamps
from mato_loop_backend.permissions.policy import (
    PermissionDecision,
    memory_read_permission,
    session_search_permission,
)
from mato_loop_backend.permissions.resolution import tool_execution_policy
from mato_loop_backend.search import GlobalSearchService
from mato_loop_backend.settings import ToolExecutionPolicy, ToolSettings, TrustLevel
from mato_loop_backend.tools import access
from mato_loop_backend.tools.actions import _MEMORY_READ, _SESSION_SEARCH
from mato_loop_backend.tools.arguments import ContextSearchArguments
from mato_loop_backend.tools.context import ToolContext
from mato_loop_backend.tools.contracts import ToolDefinition, ToolResult
from mato_loop_backend.tools.sessions import session_read_permission


def source_available(settings: ToolSettings, source: str, *, memory_enabled: bool = True) -> bool:
    if settings._agent_profile_unavailable:
        return False
    name, action = (
        ("memory_retrieve", _MEMORY_READ) if source == "memories" else ("session_search", _SESSION_SEARCH)
    )
    if source == "memories" and not memory_enabled:
        return False
    if not settings.tool_availability.get(name, True):
        return False
    ceiling = settings._delegation_ceiling
    while ceiling is not None:
        if source not in ceiling.get("contextSources", ()):
            return False
        ceiling = ceiling.get("offeredCeiling")
    return tool_execution_policy(settings, action.capability, action.default_policy(settings)) != ToolExecutionPolicy.DISABLED


async def session_context_permission(arguments: ContextSearchArguments, context: ToolContext):
    if arguments.session_id is not None:
        _target, permission = await session_read_permission(context, arguments.session_id)
        if permission.capability == "session.read_sensitive":
            return permission
    return session_search_permission(include_private=arguments.include_private, trust_level=context.model_trust_level)


def requested_sources(arguments: ContextSearchArguments) -> tuple[str, ...]:
    return (
        ("sessions",) if arguments.session_id is not None else
        ("memories", "sessions") if arguments.scope == "all" else (arguments.scope,)
    )


async def context_search(arguments: ContextSearchArguments, context: ToolContext) -> ToolResult:
    settings = await access.profile_tool_settings(context)
    requested = requested_sources(arguments)
    statuses = {}
    pending = []
    session_readable_by = context.model_trust_level
    for source in requested:
        statuses[source] = "unavailable"
        if source_available(settings, source, memory_enabled=context.memory_read_enabled):
            permission = (
                memory_read_permission(context.session.user_id)
                if source == "memories" else await session_context_permission(arguments, context)
            )
            if source == "sessions" and (
                permission.capability == "session.read_sensitive" or arguments.session_id == context.session.id
            ):
                session_readable_by = TrustLevel.PRIVATE
            decision = await context.ask_handler(permission, check_only=True)
            if decision == PermissionDecision.ASK:
                pending.append((source, permission))
            else:
                statuses[source] = "searched" if decision == PermissionDecision.ALLOW else "denied"
    # Retain already-authorized sources when a later approval is refused.
    for source, permission in pending:
        statuses[source] = "searched" if await access.allow(context, permission) else "denied"
    if not any(status == "searched" for status in statuses.values()):
        return ToolResult.error("permission.denied")
    vector = None
    semantic_enabled = get_runtime_config().embedding_enabled
    if semantic_enabled:
        try:
            vector = await embedding_client.embed_query(arguments.query)
        except EmbeddingError:
            semantic_enabled = False
    memories = []
    sessions = []
    if statuses.get("memories") == "searched":
        result = await MemoryRetrievalService.retrieve(
            context.db, owner_id=context.session.user_id, mode=MemoryRetrieveMode.SEARCH,
            query=arguments.query, readable_by=context.memory_trust_level, limit=arguments.limit,
            query_embedding=vector, semantic_enabled=semantic_enabled,
            excluded_content_hashes=context.excluded_memory_content_hashes,
        )
        memories = [
            {
                "id": str(item.id), "content": item.content, "revision": item.revision,
                "category": item.category, "audience": item.audience, "pinned": item.pinned,
                **memory_reference_timestamps(item),
                "sourceSession": {"id": str(item.source_session.id)} if item.source_session else None,
            }
            for item in result.items
        ]
    if statuses.get("sessions") == "searched":
        project_id = context.session.project_id if not arguments.all_projects and arguments.session_id is None else None
        result = await EpisodeRetrievalService.retrieve(
            context.db, owner_id=context.session.user_id,
            current_session_id=None if arguments.session_id is not None else context.session.id,
            readable_by=session_readable_by, query=arguments.query, limit=arguments.limit,
            query_embedding=vector, semantic_enabled=semantic_enabled, max_semantic_distance=0.35,
            anchor_only=True, session_id=arguments.session_id, distinct_sessions=False,
            project_id=project_id,
        )
        sessions = [
            {
                "sessionId": str(item.session_id), "title": item.session_title,
                "projectId": str(item.project_id) if item.project_id else None,
                "passage": item.anchor_content, "messageId": str(item.anchor_message_id),
                "role": item.anchor_role, "createdAt": item.anchor_created_at.isoformat(),
                "sessionReadArguments": {
                    "session_id": str(item.session_id), "around_message_id": str(item.anchor_message_id),
                },
            }
            for item in result.references
        ]
        if arguments.session_id is None:
            titles = await GlobalSearchService.session_titles(
                context.db, owner_id=context.session.user_id, query=arguments.query,
                limit=arguments.limit, exclude_session_id=context.session.id,
                readable_by=session_readable_by,
                project_id=project_id,
            )
            passage_sessions = {item["sessionId"] for item in sessions}
            sessions = [
                {"sessionId": str(session.id), "title": session.title,
                 "projectId": str(session.project_id) if session.project_id else None,
                 "sessionReadArguments": {"session_id": str(session.id)}}
                for session, _project_name in titles if str(session.id) not in passage_sessions
            ] + sessions
            sessions = sessions[:arguments.limit]
    return ToolResult.json({
        "memories": memories, "sessions": sessions, "sources": statuses,
        "readableBy": context.memory_trust_level.value, "contentIsUntrusted": True,
    })


CONTEXT_SEARCH = ToolDefinition(
    "context_search",
    "Search memories and conversations. Session scope: current project when selected; all_projects "
    "widens; session_id targets one. Unavailable/denied sources are not empty results.",
    ContextSearchArguments, context_search, group="memory", label="Kontext durchsuchen", icon="manage_search",
    actions=(_MEMORY_READ, _SESSION_SEARCH),
    alternative_action_groups=(frozenset({"memory.read", "session.search"}),),
)
