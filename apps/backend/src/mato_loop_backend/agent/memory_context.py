from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend.agent.run_support import (
    AUTOMATIC_EPISODE_EXCERPT_MAX_CHARACTERS,
    AUTOMATIC_EPISODE_MAX_DISTANCE,
    MEMORY_OVERVIEW_MAX_ITEMS,
)
from mato_loop_backend.agent.runtime_memory import (
    _automatic_context_character_budget,
    _automatic_context_suppressed,
    _bounded_automatic_payload,
    _bounded_memory_overview,
    _cross_session_context_text,
    _durable_memory_payload,
    _memory_overview_continuation,
    _memory_overview_requested,
    _memory_rag_query,
    _recent_episode_overview_requested,
    _session_bootstrap_requested,
)
from mato_loop_backend.memory import policy as memory_policy
from mato_loop_backend.memory.embeddings import EmbeddingError, embedding_client
from mato_loop_backend.memory.episode_retrieval import (
    EpisodeReference,
    EpisodeRetrievalService,
)
from mato_loop_backend.memory.retrieval import (
    MemoryRetrievalService,
    MemoryRetrieveMode,
)
from mato_loop_backend.memory.service import MemoryScope, MemoryService
from mato_loop_backend.models import ChatSession, MemoryRecord, Message
from mato_loop_backend.settings import AgentRuntimeSettings, MemorySettings
from mato_loop_backend.settings.models import ModelConfig, TrustLevel


@dataclass
class MemoryCache:
    automatic_memories: list[MemoryRecord] | None = None
    relevant_episodes: list[EpisodeReference] | None = None
    recent_episodes: list[EpisodeReference] | None = None
    scope: MemoryScope | None = None
    overview_required: bool | None = None

    def invalidate(self, *, continuation: bool = False) -> None:
        self.automatic_memories = None
        self.relevant_episodes = None
        self.recent_episodes = None
        self.scope = None
        if continuation:
            self.overview_required = None


@dataclass
class MemoryTurn:
    candidates: list[tuple[str | None, int, int]]
    scope: MemoryScope | None = None
    overview_prefetched: bool = False
    overview_has_more: bool = False
    overview_runtime_state: dict[str, Any] | None = None


async def prepare_memory_context(
    db: AsyncSession,
    session: ChatSession,
    records: list[Message],
    *,
    model: ModelConfig,
    trust_level: TrustLevel,
    memory_state: memory_policy.EffectiveMemory,
    settings: MemorySettings,
    runtime_settings: AgentRuntimeSettings,
    excluded_content_hashes: frozenset[str],
    embedding_enabled: Callable[[], bool],
    cache: MemoryCache,
) -> MemoryTurn:
    if cache.overview_required is None:
        cache.overview_required = _memory_overview_requested(records)
    turn = MemoryTurn(candidates=[])
    if memory_state.read_enabled:
        if cache.scope is None:
            cache.scope = await MemoryService.scope(
                db, owner_id=session.user_id, readable_by=trust_level,
                excluded_content_hashes=excluded_content_hashes,
            )
        turn.scope = cache.scope
        pinned_memories = await MemoryService.pinned_context(
            db, owner_id=session.user_id, input_trust_level=trust_level,
            excluded_content_hashes=excluded_content_hashes,
        )
        suppressed = _automatic_context_suppressed(records)
        project_sessions = None
        has_more_project_sessions = False
        if session.project_id is not None and not suppressed:
            index = await EpisodeRetrievalService.project_index(
                db, owner_id=session.user_id, current_session_id=session.id,
                project_id=session.project_id,
                readable_by=trust_level,
            )
            project_sessions = index.items
            has_more_project_sessions = index.has_more
        budget = _automatic_context_character_budget(model, runtime_settings, settings)
        _, _, bounded_project_index = _bounded_automatic_payload(
            [], [], [], max_characters=budget,
            project_sessions=project_sessions,
            has_more_project_sessions=has_more_project_sessions,
        )
        if cache.overview_required:
            continuation = _memory_overview_continuation(records)
            should_retrieve = continuation is None or bool(continuation.get("hasMore"))
            cursor_updated_at = None
            cursor_id = None
            if continuation is not None and should_retrieve:
                cursor = continuation.get("nextCursor")
                if isinstance(cursor, dict):
                    try:
                        cursor_updated_at = datetime.fromisoformat(str(cursor["updatedAt"]))
                        cursor_id = UUID(str(cursor["id"]))
                    except (KeyError, TypeError, ValueError):
                        cursor_updated_at = None
                        cursor_id = None
            items = []
            retrieval_has_more = False
            if should_retrieve:
                overview_result = await MemoryRetrievalService.retrieve(
                    db, owner_id=session.user_id, mode=MemoryRetrieveMode.BROWSE,
                    query=None, readable_by=trust_level,
                    cursor_updated_at=cursor_updated_at, cursor_id=cursor_id,
                    limit=MEMORY_OVERVIEW_MAX_ITEMS,
                    excluded_content_hashes=excluded_content_hashes,
                )
                items = overview_result.items
                retrieval_has_more = overview_result.has_more
            overview_payload, last_item, omitted_items = _bounded_memory_overview(items)
            pinned_ids = {item.id for item in pinned_memories}
            overview_payload = [
                item for item in overview_payload if UUID(item["id"]) not in pinned_ids
            ]
            pinned_references = [("pinned", item) for item in pinned_memories]
            overview_context = {
                "durableMemories": _durable_memory_payload(pinned_references),
                "memoryOverview": overview_payload,
                "priorSessionEpisodes": [],
            }
            if bounded_project_index is not None:
                overview_context["projectSessionIndex"] = bounded_project_index
            memory_text = json.dumps(overview_context, ensure_ascii=False)
            turn.overview_has_more = bool(
                last_item is not None and (retrieval_has_more or omitted_items)
            )
            next_cursor = (
                {"updatedAt": last_item.updated_at.isoformat(), "id": str(last_item.id)}
                if turn.overview_has_more and last_item is not None else None
            )
            turn.overview_runtime_state = {
                "hasMore": turn.overview_has_more, "nextCursor": next_cursor,
            }
            turn.overview_prefetched = True
            turn.candidates.append((memory_text, len(pinned_memories) + len(overview_payload), 0))
        else:
            rag_query = _memory_rag_query(records)
            if cache.automatic_memories is None:
                cache.automatic_memories = []
                cache.relevant_episodes = []
                cache.recent_episodes = []
                episode_query_enabled = bool(rag_query) and not suppressed
                semantic_enabled = embedding_enabled()
                query_embedding = None
                if rag_query and not suppressed and semantic_enabled and (
                    settings.automatic_memory_snippet_limit > 0
                    or (episode_query_enabled and settings.automatic_session_snippet_limit > 0)
                ):
                    try:
                        query_embedding = await embedding_client.embed_query(rag_query)
                    except EmbeddingError:
                        semantic_enabled = False
                if rag_query and not suppressed and settings.automatic_memory_snippet_limit > 0:
                    cache.automatic_memories = (
                        await MemoryRetrievalService.retrieve(
                            db, owner_id=session.user_id, mode=MemoryRetrieveMode.SEARCH,
                            query=rag_query, readable_by=trust_level, pinned=False,
                            limit=settings.automatic_memory_snippet_limit,
                            query_embedding=query_embedding, semantic_enabled=semantic_enabled,
                            max_semantic_distance=AUTOMATIC_EPISODE_MAX_DISTANCE,
                            excluded_content_hashes=excluded_content_hashes,
                        )
                    ).items
                if rag_query and episode_query_enabled and settings.automatic_session_snippet_limit > 0:
                    cache.relevant_episodes = (
                        await EpisodeRetrievalService.retrieve(
                            db, owner_id=session.user_id, current_session_id=session.id,
                            readable_by=trust_level, query=rag_query,
                            limit=(settings.automatic_session_snippet_limit if session.project_id is not None
                                   else min(1, settings.automatic_session_snippet_limit)),
                            query_embedding=query_embedding, semantic_enabled=semantic_enabled,
                            max_semantic_distance=AUTOMATIC_EPISODE_MAX_DISTANCE,
                            anchor_only=True, full_sessions_only=True,
                        )
                    ).references
                if episode_query_enabled and settings.recent_session_snippet_limit > 0 and (
                    _session_bootstrap_requested(records)
                    or _recent_episode_overview_requested(records)
                ):
                    cache.recent_episodes = (
                        await EpisodeRetrievalService.recent(
                            db, owner_id=session.user_id, current_session_id=session.id,
                            readable_by=trust_level, limit=settings.recent_session_snippet_limit,
                            excerpt_messages=4,
                            max_excerpt_characters=AUTOMATIC_EPISODE_EXCERPT_MAX_CHARACTERS,
                            full_sessions_only=True,
                        )
                    ).references
            else:
                cache.automatic_memories = (
                    await MemoryService.automatic_context_by_ids(
                        db, owner_id=session.user_id,
                        memory_ids=[item.id for item in cache.automatic_memories],
                        readable_by=trust_level,
                        excluded_content_hashes=excluded_content_hashes,
                    )
                )[:settings.automatic_memory_snippet_limit]
                cache.relevant_episodes = (
                    await EpisodeRetrievalService.revalidate(
                        db, cache.relevant_episodes or [], owner_id=session.user_id,
                        current_session_id=session.id, readable_by=trust_level, full_sessions_only=True,
                    )
                )[:settings.automatic_session_snippet_limit]
                cache.recent_episodes = (
                    await EpisodeRetrievalService.revalidate(
                        db, cache.recent_episodes or [], owner_id=session.user_id,
                        current_session_id=session.id, readable_by=trust_level, full_sessions_only=True,
                    )
                )[:settings.recent_session_snippet_limit]
            pinned_ids = {item.id for item in pinned_memories}
            automatic_payload, episode_payload, bounded_project_index = _bounded_automatic_payload(
                [item for item in cache.automatic_memories if item.id not in pinned_ids],
                cache.relevant_episodes or [], cache.recent_episodes or [],
                max_characters=budget, project_session=session.project_id is not None,
                project_sessions=project_sessions, has_more_project_sessions=has_more_project_sessions,
            )
            pinned_references = [("pinned", item) for item in pinned_memories]
            memory_text = _cross_session_context_text(
                pinned_references, episode_payload, automatic_payload, bounded_project_index,
            )
            turn.candidates.append((
                memory_text,
                len(pinned_references) + len(automatic_payload),
                len(episode_payload),
            ))
            if automatic_payload or episode_payload or bounded_project_index is not None:
                turn.candidates.append((
                    _cross_session_context_text(pinned_references, []),
                    len(pinned_references), 0,
                ))
    if not turn.candidates:
        turn.candidates.append((None, 0, 0))
    return turn
