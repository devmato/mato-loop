import asyncio
import json
import os
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload
from tool_helpers import _context

from mato_loop_backend.agent.projection import message_dict
from mato_loop_backend.db.session import AsyncSessionFactory, engine
from mato_loop_backend.memory.embeddings import (
    ACTIVE_EMBEDDING_PROFILE,
    EmbeddingError,
    embedding_client,
)
from mato_loop_backend.memory.episode_indexing import EpisodeIndexService
from mato_loop_backend.memory.episode_retrieval import EpisodeRetrievalService
from mato_loop_backend.memory.retrieval import (
    MemoryRetrievalService,
    MemoryRetrievalStrategy,
    MemoryRetrieveMode,
)
from mato_loop_backend.memory.service import MemoryService, normalize_content
from mato_loop_backend.models import (
    AgentRun,
    ChatSession,
    MemoryCategory,
    MemoryEmbedding,
    MemoryEmbeddingStatus,
    MemoryRecord,
    MemorySourceMessage,
    Message,
    MessageSearchChunk,
    Project,
    UserAccount,
)
from mato_loop_backend.providers.base import ToolCall
from mato_loop_backend.settings import TrustLevel
from mato_loop_backend.tools import context_search
from mato_loop_backend.tools.catalog import tool_registry

pytestmark = pytest.mark.skipif(
    os.getenv("MATO_LOOP_RAG_INTEGRATION") != "1",
    reason="requires the local pgvector and Infinity stack",
)


@pytest.fixture(autouse=True)
async def _dispose_database_pool_after_test():
    yield
    await embedding_client.close()
    await engine.dispose()


async def test_local_rag_finds_cross_language_memory_without_leaking_private() -> None:
    public_content = "Der Nutzer bevorzugt bei der Arbeit kurze und direkte Antworten."
    private_content = "The user prefers detailed private financial reports."
    unrelated_content = "Der Nutzer interessiert sich für das Wetter in Bayern."
    contents = [public_content, private_content, unrelated_content]
    vectors = await embedding_client.embed_documents(contents)

    async with AsyncSessionFactory() as db:
        user = UserAccount(username=f"memory-{uuid4().hex}", password_hash="test")
        db.add(user)
        await db.flush()
        for content, audience, vector in zip(
            contents,
            ["both", "private_only", "both"],
            vectors,
            strict=True,
        ):
            _, content_hash = normalize_content(content)
            memory = MemoryRecord(
                id=uuid4(),
                user_id=user.id,
                source_type="manual",
                pinned=False,
                audience=audience,
                category="preference",
                content=content,
                normalized_content_hash=content_hash,
            )
            db.add(memory)
            await db.flush()
            db.add(
                MemoryEmbedding(
                    memory_id=memory.id,
                    profile_fingerprint=ACTIVE_EMBEDDING_PROFILE.fingerprint,
                    content_hash=content_hash,
                    status=MemoryEmbeddingStatus.READY.value,
                    vector=vector,
                )
            )
        await db.flush()

        result = await MemoryRetrievalService.retrieve(
            db,
            owner_id=user.id,
            mode=MemoryRetrieveMode.SEARCH,
            query="How does the user prefer answers while working?",
            readable_by=TrustLevel.PUBLIC,
            limit=2,
        )

        assert result.items[0].content == public_content
        assert all(item.audience in {"both", "public_only"} for item in result.items)
        await db.rollback()


async def test_public_model_episode_rag_excludes_private_sessions() -> None:
    async with AsyncSessionFactory() as db:
        user = UserAccount(username=f"episode-{uuid4().hex}", password_hash="test")
        db.add(user)
        await db.flush()
        public_session = ChatSession(user_id=user.id, title="Architektur", trust_level="public")
        private_session = ChatSession(user_id=user.id, title="Vertraulich")
        current_session = ChatSession(user_id=user.id, title="Aktuell")
        db.add_all([public_session, private_session, current_session])
        await db.flush()
        public_message = Message(
            session_id=public_session.id,
            role="assistant",
            content="Wir entschieden uns für PostgreSQL als zentrale Datenbank.",
            trust_level=TrustLevel.PUBLIC.value,
            created_at=datetime(2026, 8, 1, 10, tzinfo=UTC),
        )
        latest_user = Message(
            session_id=public_session.id,
            role="user",
            content="Die Migration ist noch nicht gestartet.",
            trust_level=TrustLevel.PUBLIC.value,
            created_at=datetime(2026, 8, 2, 10, tzinfo=UTC),
        )
        latest_assistant = Message(
            session_id=public_session.id,
            role="assistant",
            content="Als Nächstes erstellen wir zuerst ein Backup.",
            trust_level=TrustLevel.PUBLIC.value,
            created_at=datetime(2026, 8, 2, 11, tzinfo=UTC),
        )
        private_message = Message(
            session_id=private_session.id,
            role="assistant",
            content="Die geheime Alternative wäre eine private Oracle-Instanz.",
            trust_level=TrustLevel.PRIVATE.value,
        )
        db.add_all([public_message, latest_user, latest_assistant, private_message])
        await db.flush()
        await EpisodeIndexService.enqueue(db, public_message)
        await EpisodeIndexService.enqueue(db, private_message)
        chunks = list(
            (
                await db.scalars(
                    select(MessageSearchChunk).where(
                        MessageSearchChunk.message_id.in_(
                            [public_message.id, private_message.id]
                        )
                    )
                )
            ).all()
        )
        vectors = await embedding_client.embed_documents(
            [chunk.content for chunk in chunks]
        )
        for chunk, vector in zip(chunks, vectors, strict=True):
            chunk.status = MemoryEmbeddingStatus.READY.value
            chunk.vector = vector
        await db.flush()

        result = await EpisodeRetrievalService.retrieve(
            db,
            owner_id=user.id,
            current_session_id=current_session.id,
            readable_by=TrustLevel.PUBLIC,
            query="PostgreSQL Datenbank",
            limit=2,
            max_semantic_distance=0.25,
            latest_messages=2,
        )

        assert result.references
        assert {reference.session_id for reference in result.references} == {public_session.id}
        await db.rollback()


async def test_episode_rag_is_immediate_without_embedding_or_dreaming() -> None:
    marker = f"Sofortkennung-{uuid4().hex}"
    async with AsyncSessionFactory() as db:
        user = UserAccount(username=f"immediate-{uuid4().hex}", password_hash="test")
        db.add(user)
        await db.flush()
        prior_session = ChatSession(user_id=user.id, title="Frueher", trust_level="public")
        current_session = ChatSession(user_id=user.id, title="Aktuell")
        db.add_all([prior_session, current_session])
        await db.flush()
        message = Message(
            session_id=prior_session.id,
            role="user",
            content=f"Die projektweite Referenz lautet {marker}.",
            trust_level=TrustLevel.PUBLIC.value,
        )
        db.add(message)
        await db.flush()
        await EpisodeIndexService.enqueue(db, message)
        chunk = await db.scalar(
            select(MessageSearchChunk).where(MessageSearchChunk.message_id == message.id)
        )

        result = await EpisodeRetrievalService.retrieve(
            db,
            owner_id=user.id,
            current_session_id=current_session.id,
            readable_by=TrustLevel.PUBLIC,
            query=marker,
            semantic_enabled=False,
            limit=1,
        )

        assert chunk is not None
        assert chunk.status == MemoryEmbeddingStatus.QUEUED.value
        assert result.references[0].session_id == prior_session.id
        assert marker in result.references[0].anchor_content
        another = ChatSession(user_id=user.id, title="Other conversation")
        db.add(another)
        await db.flush()
        detail = Message(session_id=prior_session.id, role="user", content=f"{marker}: http://192.0.2.73:5080")
        distraction = Message(session_id=another.id, role="user", content=marker)
        db.add_all([detail, distraction])
        await db.flush()
        for item in (detail, distraction):
            await EpisodeIndexService.enqueue(db, item)
        scoped = await EpisodeRetrievalService.retrieve(
            db, owner_id=user.id, current_session_id=current_session.id, readable_by=TrustLevel.PUBLIC,
            query=marker, limit=10, semantic_enabled=False, anchor_only=True,
            session_id=prior_session.id, distinct_sessions=False,
        )
        assert {item.anchor_message_id for item in scoped.references} == {message.id, detail.id}
        exact_ip = await EpisodeRetrievalService.retrieve(
            db, owner_id=user.id, current_session_id=current_session.id, readable_by=TrustLevel.PUBLIC,
            query="192.0.2.73", semantic_enabled=False, anchor_only=True, distinct_sessions=False,
        )
        assert [item.anchor_message_id for item in exact_ip.references] == [detail.id]
        await db.rollback()


async def test_context_search_finds_an_owned_title_without_a_message_match(tmp_path, monkeypatch) -> None:
    call = ToolCall("search", "context_search", {"query": "Aurora", "scope": "sessions"})
    context = _context(tmp_path, call)
    monkeypatch.setattr(context_search.access, "profile_tool_settings", AsyncMock(return_value=context.tool_settings))
    monkeypatch.setattr(context_search, "get_runtime_config", lambda: SimpleNamespace(embedding_enabled=False))
    async with AsyncSessionFactory() as db:
        owner = UserAccount(username=f"title-{uuid4().hex}", password_hash="test")
        foreign = UserAccount(username=f"foreign-title-{uuid4().hex}", password_hash="test")
        db.add_all([owner, foreign])
        await db.flush()
        context.db = db
        context.session.user_id = owner.id
        owned = ChatSession(user_id=owner.id, title="Project Aurora")
        other = ChatSession(user_id=foreign.id, title="Project Aurora")
        db.add_all([context.session, owned, other])
        await db.flush()
        message = Message(session_id=owned.id, role="user", content="The database endpoint is configured.")
        db.add(message)
        await db.flush()
        await EpisodeIndexService.enqueue(db, message)

        result = json.loads((await tool_registry.execute(call, context)).content)

        assert result["sessions"] == [{
            "sessionId": str(owned.id), "title": owned.title,
            "projectId": str(owned.project_id) if owned.project_id else None,
            "sessionReadArguments": {"session_id": str(owned.id)},
        }]
        await db.rollback()


@pytest.mark.parametrize("receiver_project", ["A", None], ids=["project-A", "standalone"])
@pytest.mark.parametrize("full_sessions_only", [True, False], ids=["automatic", "unrestricted"])
async def test_episode_retrieval_respects_project_scope_before_ranking(
    receiver_project: str | None, full_sessions_only: bool
) -> None:
    marker = f"Episodescope{uuid4().hex}"
    async with AsyncSessionFactory() as db:
        owner = UserAccount(username=f"scope-{uuid4().hex}", password_hash="test")
        foreign_owner = UserAccount(username=f"foreign-{uuid4().hex}", password_hash="test")
        db.add_all([owner, foreign_owner])
        await db.flush()
        projects = {name: Project(user_id=owner.id, name=name) for name in ("A", "B")}
        db.add_all(projects.values())
        await db.flush()
        current = ChatSession(
            user_id=owner.id,
            project_id=projects[receiver_project].id if receiver_project else None,
        )
        sources = [
            ChatSession(
                user_id=owner.id,
                project_id=project_id,
                memory_mode=mode,
                trust_level="public",
            )
            for project_id in (projects["A"].id, projects["B"].id, None)
            for mode in ("full", "read_only", "disabled")
        ]
        foreign = ChatSession(user_id=foreign_owner.id)
        db.add_all([current, foreign, *sources])
        await db.flush()
        for index, session in enumerate([*sources, foreign, current], start=1):
            message = Message(
                session_id=session.id,
                role="user",
                content=marker,
                created_at=datetime(2026, 8, index, tzinfo=UTC),
            )
            db.add(message)
            await db.flush()
            assert await EpisodeIndexService.enqueue(db, message) == 1

        lexical = await EpisodeRetrievalService.retrieve(
            db,
            owner_id=owner.id,
            current_session_id=current.id,
            readable_by=TrustLevel.PUBLIC,
            query=marker,
            semantic_enabled=False,
            anchor_only=True,
            full_sessions_only=full_sessions_only,
            project_id=current.project_id if full_sessions_only else None,
            limit=10,
        )
        recent = await EpisodeRetrievalService.recent(
            db,
            owner_id=owner.id,
            current_session_id=current.id,
            readable_by=TrustLevel.PUBLIC,
            full_sessions_only=full_sessions_only,
            project_id=current.project_id if full_sessions_only else None,
            limit=10,
        )

        expected_ids = [
            session.id
            for session in reversed(sources)
            if not full_sessions_only or session.memory_mode == "full"
            if not full_sessions_only or current.project_id is None or session.project_id == current.project_id
        ]
        assert lexical.strategy == MemoryRetrievalStrategy.LEXICAL
        assert recent.strategy == MemoryRetrievalStrategy.BROWSE
        for result in (lexical, recent):
            assert [item.session_id for item in result.references] == expected_ids

        for change in ("initial", "move_project", "disable"):
            if change == "move_project":
                sources[0].project_id = projects["B"].id
                if full_sessions_only and current.project_id is not None:
                    expected_ids.remove(sources[0].id)
            elif change == "disable":
                sources[0].memory_mode = "disabled"
                if full_sessions_only and sources[0].id in expected_ids:
                    expected_ids.remove(sources[0].id)
            await db.flush()
            for result in (lexical, recent):
                validated = await EpisodeRetrievalService.revalidate(
                    db,
                    result.references,
                    owner_id=owner.id,
                    current_session_id=current.id,
                    full_sessions_only=full_sessions_only,
                    project_id=current.project_id if full_sessions_only else None,
                )
                assert [item.session_id for item in validated] == expected_ids, change
        await db.rollback()


async def test_local_rag_falls_back_to_postgres_fts(monkeypatch) -> None:
    content = f"Fallbackkennung {uuid4().hex}"
    query = content.split()[-1]
    _, content_hash = normalize_content(content)

    async def unavailable(_text: str) -> list[float]:
        raise EmbeddingError("memory.embeddingUnavailable", transient=True)

    monkeypatch.setattr(embedding_client, "embed_query", unavailable)
    async with AsyncSessionFactory() as db:
        user = UserAccount(username=f"fallback-{uuid4().hex}", password_hash="test")
        db.add(user)
        await db.flush()
        memory = MemoryRecord(
            id=uuid4(),
            user_id=user.id,
            source_type="manual",
            pinned=False,
            audience="both",
            category="fact",
            content=content,
            normalized_content_hash=content_hash,
        )
        db.add(memory)
        await db.flush()

        result = await MemoryRetrievalService.retrieve(
            db,
            owner_id=user.id,
            mode=MemoryRetrieveMode.SEARCH,
            query=f"{'-' * 40} {query}",
            readable_by=TrustLevel.PUBLIC,
            limit=1,
        )

        assert result.strategy == MemoryRetrievalStrategy.LEXICAL
        assert result.items[0].id == memory.id
        await db.rollback()


async def test_automatic_memory_retrieval_includes_every_source() -> None:
    marker = f"Sourcefilter-{uuid4().hex}"
    async with AsyncSessionFactory() as db:
        user = UserAccount(username=f"sources-{uuid4().hex}", password_hash="test")
        db.add(user)
        await db.flush()
        memories = []
        for source_type in ("dreaming", "manual", "tool_call"):
            content = f"{marker} {source_type}"
            _, content_hash = normalize_content(content)
            memory = MemoryRecord(
                user_id=user.id,
                source_type=source_type,
                pinned=False,
                audience="both",
                category="fact",
                content=content,
                normalized_content_hash=content_hash,
            )
            memories.append(memory)
            db.add(memory)
        await db.flush()

        automatic = await MemoryRetrievalService.retrieve(
            db,
            owner_id=user.id,
            mode=MemoryRetrieveMode.SEARCH,
            query=marker,
            readable_by=TrustLevel.PUBLIC,
            semantic_enabled=False,
            limit=5,
        )

        assert {item.id for item in automatic.items} == {item.id for item in memories}
        await db.rollback()


async def test_project_session_index_uses_latest_eligible_message() -> None:
    async with AsyncSessionFactory() as db:
        owner = UserAccount(username=f"index-{uuid4().hex}", password_hash="test")
        db.add(owner)
        await db.flush()
        project = Project(user_id=owner.id, name="Homelab")
        other_project = Project(user_id=owner.id, name="Other")
        db.add_all([project, other_project])
        await db.flush()
        current = ChatSession(user_id=owner.id, project_id=project.id, title="Current")
        corrected = ChatSession(
            user_id=owner.id, project_id=project.id, title="Mealie deployment"
        )
        older = ChatSession(user_id=owner.id, project_id=project.id, title="Older")
        read_only = ChatSession(
            user_id=owner.id,
            project_id=project.id,
            title="Read only",
            memory_mode="read_only",
        )
        foreign_project = ChatSession(
            user_id=owner.id, project_id=other_project.id, title="Other project"
        )
        overflow_sessions = [
            ChatSession(
                user_id=owner.id,
                project_id=project.id,
                title=f"Overflow {index}",
            )
            for index in range(9)
        ]
        db.add_all(
            [
                current,
                corrected,
                older,
                read_only,
                foreign_project,
                *overflow_sessions,
            ]
        )
        await db.flush()
        old_anchor = Message(
            session_id=corrected.id,
            role="assistant",
            content="Use /opt/mealie.",
            created_at=datetime(2026, 8, 1, tzinfo=UTC),
        )
        corrected_anchor = Message(
            session_id=corrected.id,
            role="assistant",
            content="Use /srv/stacks/mealie.",
            created_at=datetime(2026, 8, 3, tzinfo=UTC),
        )
        tool_preamble = Message(
            session_id=corrected.id,
            role="assistant",
            content="I will inspect it.",
            tool_calls=[{"id": "inspect", "name": "file_read", "arguments": {}}],
            created_at=datetime(2026, 8, 4, tzinfo=UTC),
        )
        db.add_all(
            [
                old_anchor,
                corrected_anchor,
                tool_preamble,
                Message(
                    session_id=older.id,
                    role="user",
                    content="Older project context",
                    created_at=datetime(2026, 8, 2, tzinfo=UTC),
                ),
                Message(session_id=current.id, role="user", content="Current"),
                Message(session_id=read_only.id, role="user", content="Read only"),
                Message(
                    session_id=foreign_project.id,
                    role="user",
                    content="Other project",
                ),
                *(
                    Message(
                        session_id=session.id,
                        role="user",
                        content=f"Overflow context {index}",
                        created_at=datetime(2026, 7, index + 1, tzinfo=UTC),
                    )
                    for index, session in enumerate(overflow_sessions)
                ),
            ]
        )
        await db.flush()

        result = await EpisodeRetrievalService.project_index(
            db,
            owner_id=owner.id,
            current_session_id=current.id,
            project_id=project.id,
        )

        assert [item.session_id for item in result.items[:2]] == [corrected.id, older.id]
        assert len(result.items) == 10
        assert result.items[0].latest_message_id == corrected_anchor.id
        assert result.has_more
        await db.rollback()


async def test_automatic_rag_distance_excludes_unrelated_memory() -> None:
    relevant_content = "Das Kind des Testnutzers ist genau neun Monate alt."
    unrelated_content = "Eine Anleitung beschreibt das Backen von Sauerteigbrot."
    vectors = await embedding_client.embed_documents(
        [relevant_content, unrelated_content]
    )

    async with AsyncSessionFactory() as db:
        user = UserAccount(username=f"distance-{uuid4().hex}", password_hash="test")
        db.add(user)
        await db.flush()
        memory_ids = []
        for content, vector in zip(
            [relevant_content, unrelated_content], vectors, strict=True
        ):
            _, content_hash = normalize_content(content)
            memory = MemoryRecord(
                id=uuid4(),
                user_id=user.id,
                source_type="manual",
                pinned=False,
                audience="both",
                category="fact",
                content=content,
                normalized_content_hash=content_hash,
            )
            memory_ids.append(memory.id)
            db.add(memory)
            await db.flush()
            db.add(
                MemoryEmbedding(
                    memory_id=memory.id,
                    profile_fingerprint=ACTIVE_EMBEDDING_PROFILE.fingerprint,
                    content_hash=content_hash,
                    status=MemoryEmbeddingStatus.READY.value,
                    vector=vector,
                )
            )
        await db.flush()

        result = await MemoryRetrievalService.retrieve(
            db,
            owner_id=user.id,
            mode=MemoryRetrieveMode.SEARCH,
            query="Wie alt ist mein Sohn?",
            readable_by=TrustLevel.PUBLIC,
            limit=5,
            max_semantic_distance=0.21,
        )

        result_ids = {item.id for item in result.items}
        assert memory_ids[0] in result_ids
        assert memory_ids[1] not in result_ids
        await db.rollback()


async def test_browse_lists_pinned_and_unpinned_memories() -> None:
    async with AsyncSessionFactory() as db:
        user = UserAccount(username=f"browse-{uuid4().hex}", password_hash="test")
        db.add(user)
        await db.flush()
        created = []
        for pinned in [True, False, False]:
            content = f"Browse-{pinned}-{uuid4().hex}"
            _, content_hash = normalize_content(content)
            memory = MemoryRecord(
                id=uuid4(),
                user_id=user.id,
                source_type="manual",
                pinned=pinned,
                audience="both",
                category="fact",
                content=content,
                normalized_content_hash=content_hash,
                updated_at=datetime(2100, 1, 1, tzinfo=UTC),
            )
            created.append(memory.id)
            db.add(memory)
        await db.flush()

        result = await MemoryRetrievalService.retrieve(
            db,
            owner_id=user.id,
            mode=MemoryRetrieveMode.BROWSE,
            readable_by=TrustLevel.PUBLIC,
            limit=2,
        )

        assert result.has_more is True
        assert result.next_cursor_updated_at is not None
        assert result.next_cursor_id is not None
        continued = await MemoryRetrievalService.retrieve(
            db,
            owner_id=user.id,
            mode=MemoryRetrieveMode.BROWSE,
            readable_by=TrustLevel.PUBLIC,
            cursor_updated_at=result.next_cursor_updated_at,
            cursor_id=result.next_cursor_id,
            limit=2,
        )

        first_ids = {item.id for item in result.items}
        continued_ids = {item.id for item in continued.items}
        assert first_ids.isdisjoint(continued_ids)
        assert set(created).issubset(first_ids | continued_ids)
        assert result.total is not None
        assert continued.total == result.total
        await db.rollback()


async def test_tool_update_merges_exact_duplicate_and_removes_stale_embedding() -> None:
    stale_content = f"Alter Herkunftsort {uuid4().hex}"
    canonical_content = f"Neuer Herkunftsort {uuid4().hex}"
    _, stale_hash = normalize_content(stale_content)
    _, canonical_hash = normalize_content(canonical_content)

    async with AsyncSessionFactory() as db:
        user = UserAccount(username=f"merge-{uuid4().hex}", password_hash="test")
        db.add(user)
        await db.flush()
        session = ChatSession(user_id=user.id)
        db.add(session)
        await db.flush()
        source_message = Message(
            session_id=session.id,
            role="user",
            content=stale_content,
            trust_level=TrustLevel.PRIVATE.value,
        )
        correction_message = Message(
            session_id=session.id,
            role="user",
            content=canonical_content,
            trust_level=TrustLevel.PRIVATE.value,
        )
        db.add_all([source_message, correction_message])
        await db.flush()
        agent_run = AgentRun(
            session_id=session.id,
            status="completed",
            trust_level=TrustLevel.PRIVATE.value,
        )
        db.add(agent_run)
        await db.flush()
        stale = MemoryRecord(
            user_id=user.id,
            source_session_id=session.id,
            source_message_id=source_message.id,
            source_type="dreaming",
            pinned=False,
            audience="private_only",
            category="fact",
            revision=1,
            content=stale_content,
            normalized_content_hash=stale_hash,
        )
        canonical = MemoryRecord(
            user_id=user.id,
            source_session_id=session.id,
            source_message_id=correction_message.id,
            source_type="tool_call",
            pinned=False,
            audience="both",
            category="context",
            revision=2,
            content=canonical_content,
            normalized_content_hash=canonical_hash,
        )
        db.add_all([stale, canonical])
        await db.flush()
        db.add(
            MemorySourceMessage(memory_id=stale.id, message_id=source_message.id)
        )
        db.add(
            MemoryEmbedding(
                memory_id=stale.id,
                profile_fingerprint=ACTIVE_EMBEDDING_PROFILE.fingerprint,
                content_hash=stale_hash,
                status=MemoryEmbeddingStatus.READY.value,
                vector=[0.0] * ACTIVE_EMBEDDING_PROFILE.dimensions,
            )
        )
        await db.flush()

        result = await MemoryService.update_tool(
            db,
            owner_id=user.id,
            memory_id=stale.id,
            expected_revision=1,
            content=canonical_content,
            category=MemoryCategory.FACT,
            readable_by=TrustLevel.PRIVATE,
            write_trust_level=TrustLevel.PRIVATE,
            session_id=session.id,
            evidence_message_id=correction_message.id,
            run_id=agent_run.id,
            tool_call_id="correction",
        )

        assert result.item is canonical
        assert result.updated is True
        assert canonical.revision == 3
        assert canonical.audience == "private_only"
        remaining_ids = set(
            (
                await db.scalars(
                    select(MemoryRecord.id).where(
                        MemoryRecord.id.in_([stale.id, canonical.id])
                    )
                )
            ).all()
        )
        assert remaining_ids == {canonical.id}
        evidence_ids = set(
            (
                await db.scalars(
                    select(MemorySourceMessage.message_id).where(
                        MemorySourceMessage.memory_id == canonical.id
                    )
                )
            ).all()
        )
        assert evidence_ids == {source_message.id, correction_message.id}
        stale_embedding_count = await db.scalar(
            select(func.count())
            .select_from(MemoryEmbedding)
            .where(MemoryEmbedding.memory_id == stale.id)
        )
        assert stale_embedding_count == 0
        await db.rollback()


async def test_memory_refresh_keeps_source_message_attachment_collection_loaded() -> None:
    async with AsyncSessionFactory() as db:
        user = UserAccount(username=f"attachment-{uuid4().hex}", password_hash="test")
        db.add(user)
        await db.flush()
        session = ChatSession(user_id=user.id)
        db.add(session)
        await db.flush()
        message = Message(
            session_id=session.id,
            role="user",
            content="Remember this image.",
            trust_level=TrustLevel.PRIVATE.value,
        )
        db.add(message)
        await db.flush()
        _, content_hash = normalize_content("Pinned source")
        memory = MemoryRecord(
            user_id=user.id,
            source_session_id=session.id,
            source_message_id=message.id,
            source_type="manual",
            pinned=True,
            audience="private_only",
            category="fact",
            content="Pinned source",
            normalized_content_hash=content_hash,
        )
        db.add(memory)
        await db.flush()
        db.sync_session.expunge_all()

        loaded_message = await db.scalar(
            select(Message)
            .options(selectinload(Message.attachments))
            .where(Message.id == message.id)
        )
        assert loaded_message is not None
        assert loaded_message.attachments == []

        await MemoryService.pinned_context(
            db,
            owner_id=user.id,
            input_trust_level=TrustLevel.PRIVATE,
        )
        projected = await asyncio.to_thread(message_dict, loaded_message)

        assert projected["attachments"] == []
        await db.rollback()
