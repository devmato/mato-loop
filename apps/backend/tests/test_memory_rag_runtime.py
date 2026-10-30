import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend.agent.context import ContextManager
from mato_loop_backend.agent.memory_context import (
    historical_memory_contexts,
    memory_context_snapshot,
)
from mato_loop_backend.agent.projection import ContextError, ContextProjection
from mato_loop_backend.agent.runtime import AgentRuntime
from mato_loop_backend.agent.runtime_memory import (
    MEMORY_OVERVIEW_MAX_CHARACTERS,
    _automatic_context_character_budget,
    _automatic_context_suppressed,
    _bounded_automatic_payload,
    _bounded_memory_overview,
    _episode_reference_request_text,
    _memory_overview_requested,
    _memory_rag_query,
    _recent_episode_overview_requested,
)
from mato_loop_backend.agent.session_settings import RunSessionSettings
from mato_loop_backend.events import run_events
from mato_loop_backend.memory.embeddings import EmbeddingError, embedding_client
from mato_loop_backend.memory.episode_retrieval import (
    EpisodeExcerptMessage,
    EpisodeReference,
    EpisodeRetrieval,
    EpisodeRetrievalService,
    ProjectSessionIndex,
    ProjectSessionIndexItem,
)
from mato_loop_backend.memory.retrieval import (
    MemoryRetrieval,
    MemoryRetrievalService,
    MemoryRetrievalStrategy,
    MemoryRetrieveMode,
)
from mato_loop_backend.memory.service import MemoryScope, MemoryService
from mato_loop_backend.models import (
    AgentRun,
    ChatSession,
    MemoryRecord,
    Message,
    Project,
)
from mato_loop_backend.providers.base import ProviderResult, ToolCall
from mato_loop_backend.providers.output_budget import raw_input_budget
from mato_loop_backend.providers.service import ProviderService
from mato_loop_backend.settings import (
    AgentIdentitySettings,
    AgentRuntimeSettings,
    AiProviderSettings,
    AiSettings,
    InstanceLimits,
    IntegrationSettings,
    MemorySettings,
    SettingsStore,
    SkillsSettings,
    ToolSettings,
    TrustLevel,
    UserProfileSettings,
)
from mato_loop_backend.settings.models import LlmProviderConfig, ModelConfig
from mato_loop_backend.tools.contracts import ToolResult
from mato_loop_backend.tools.registry import EffectiveToolSet

pytestmark = pytest.mark.usefixtures("mock_db_settings")


@pytest.mark.parametrize("change", [None, "revision", "memory_access", "session_access", "project", "episode_project", "trust", "compacted"])
async def test_historical_memory_context_revalidates_sources(change):
    session = ChatSession(id=uuid4(), user_id=uuid4(), project_id=uuid4())
    memory_id, source_id, source_message_id = uuid4(), uuid4(), uuid4()
    source = json.dumps({
        "durableMemories": [{"id": str(memory_id), "revision": 1}],
        "priorSessionEpisodes": [{"sessionId": str(source_id), "sessionReadArguments": {
            "around_message_id": str(source_message_id),
        }}],
        "projectSessionIndex": {"sessions": [{"sessionReadArguments": {
            "session_id": str(source_id), "around_message_id": str(source_message_id),
        }}]},
    })
    if change == "episode_project":
        payload = json.loads(source)
        payload.pop("projectSessionIndex")
        source = json.dumps(payload)
    user = Message(id=uuid4(), session_id=session.id, role="user", content="Earlier request", provider_data={
        "_matoloop": {"memoryContext": memory_context_snapshot("Private reference", source, session, TrustLevel.PRIVATE)},
    })
    memories = [] if change == "memory_access" else [(memory_id, 2 if change == "revision" else 1)]
    sources = [] if change == "session_access" else [(
        source_message_id, source_id, uuid4() if change in {"project", "episode_project"} else session.project_id,
    )]
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = user.id if change == "compacted" else None
    db.execute.side_effect = [SimpleNamespace(all=lambda: memories), SimpleNamespace(all=lambda: sources)]

    contexts = await historical_memory_contexts(
        db, session, [user, Message(id=uuid4(), session_id=session.id, role="user", content="Current request")],
        trust_level=TrustLevel.PUBLIC if change == "trust" else TrustLevel.PRIVATE,
        excluded_content_hashes=frozenset(),
    )

    assert contexts == ({user.id: "Private reference"} if change is None else {})


@pytest.mark.parametrize(
    ("manual", "discovered", "reserve"),
    [(None, None, 2_048), (8_000, None, 2_048), (128, None, 128), (None, 64, 64)],
)
def test_automatic_memory_uses_projection_generation_reserve(manual, discovered, reserve):
    model = ModelConfig(
        provider_id=uuid4(), model_id="model", context_window_tokens=10_000,
        max_output_tokens=manual, discovered_max_output_tokens=discovered, characters_per_token=4,
    )
    assert _automatic_context_character_budget(
        model, AgentRuntimeSettings(safety_reserve_tokens=512),
        MemorySettings(automatic_context_max_tokens=2_000),
    ) == int(raw_input_budget(10_000 - reserve - 512) * 0.1) * 4


def test_automatic_memory_raw_capacity_keeps_configured_limit_and_handles_no_room():
    model = ModelConfig(provider_id=uuid4(), model_id="model", characters_per_token=4)
    runtime = AgentRuntimeSettings()
    memory = MemorySettings(automatic_context_max_tokens=128)
    assert _automatic_context_character_budget(model, runtime, memory) == 512
    model.context_window_tokens = 4096
    assert _automatic_context_character_budget(model, runtime, memory) == 0


def test_short_follow_up_builds_one_contextual_retrieval_query() -> None:
    session_id = uuid4()
    records = [
        Message(session_id=session_id, role="user", content="PostgreSQL migration"),
        Message(session_id=session_id, role="assistant", content="Okay"),
        Message(session_id=session_id, role="user", content="What did we decide?"),
    ]

    assert _memory_rag_query(records) == "PostgreSQL migration\nWhat did we decide?"


def test_recent_episode_overview_intent_is_host_detected() -> None:
    session_id = uuid4()
    assert _recent_episode_overview_requested(
        [
            Message(
                session_id=session_id,
                role="user",
                content="Was haben wir in der letzten Session besprochen?",
            )
        ]
    )
    assert not _recent_episode_overview_requested(
        [
            Message(
                session_id=session_id,
                role="user",
                content="Erkläre mir PostgreSQL-Indizes.",
            )
        ]
    )


def test_episode_context_respects_explicit_opt_out() -> None:
    session_id = uuid4()
    records = [
        Message(
            session_id=session_id,
            role="user",
            content="Do not use the previous session for this answer.",
        )
    ]
    assert _automatic_context_suppressed(records)
    assert not _episode_reference_request_text(records)
    assert _episode_reference_request_text(
        [Message(session_id=session_id, role="user", content="Machen wir weiter.")]
    )


@pytest.mark.parametrize(
    "content",
    [
        "Translate this text: I live in Munich.",
        "Summarize the following text: I live in Munich.",
        "Do not use memories or prior context for this answer.",
        "Nutze keine Erinnerungen oder früheren Chats.",
    ],
)
def test_automatic_context_suppression_covers_transforms_and_memory_opt_out(
    content: str,
) -> None:
    records = [Message(session_id=uuid4(), role="user", content=content)]

    assert _automatic_context_suppressed(records)


@pytest.mark.parametrize(
    "content",
    [
        "Summarize this project using our previous decisions.",
        "I have no context about the failure.",
    ],
)
def test_automatic_context_suppression_keeps_project_context_requests(
    content: str,
) -> None:
    records = [Message(session_id=uuid4(), role="user", content=content)]

    assert not _automatic_context_suppressed(records)


def test_attachment_only_turn_does_not_reuse_the_previous_memory_query() -> None:
    session_id = uuid4()
    records = [
        Message(session_id=session_id, role="user", content="Vorherige Frage"),
        Message(session_id=session_id, role="assistant", content="Vorherige Antwort"),
        Message(session_id=session_id, role="user", content=""),
    ]

    assert _memory_rag_query(records) is None
    assert _episode_reference_request_text(records) == ""


def test_automatic_payload_budgets_serialized_metadata_and_deduplicates_sessions() -> None:
    now = datetime.now(UTC)
    references = [
        EpisodeReference(
            session_id=uuid4(),
            session_title=f"Session {index}",
            anchor_message_id=uuid4(),
            anchor_created_at=now,
            anchor_role="assistant",
            anchor_content=character * 10,
            excerpt=(
                EpisodeExcerptMessage(
                    id=uuid4(), role="user", content=character * 30, created_at=now
                ),
            ),
        )
        for index, character in enumerate(("a", "b"))
    ]

    memories, episodes, project_index = _bounded_automatic_payload(
        [], references, [references[0]], max_characters=1200
    )

    serialized = json.dumps(
        {"durableMemories": memories, "priorSessionEpisodes": episodes},
        ensure_ascii=False,
    )
    assert len(serialized) <= 1200
    assert project_index is None
    assert len(episodes) == 2
    assert len({item["sessionId"] for item in episodes}) == 2
    recent = next(item for item in episodes if item["sessionId"] == str(references[0].session_id))
    assert recent["source"] == "recent"
    assert recent["sessionReadArguments"] == {
        "session_id": str(references[0].session_id),
        "around_message_id": str(references[0].anchor_message_id),
    }

    recent_only = _bounded_automatic_payload(
        [], [], [references[0]], max_characters=10_000
    )
    tight_budget = len(
        json.dumps(
            {
                "durableMemories": recent_only[0],
                "priorSessionEpisodes": recent_only[1],
            },
            ensure_ascii=False,
        )
    )
    _, tight_episodes, _ = _bounded_automatic_payload(
        [], [references[1]], [references[0]], max_characters=tight_budget
    )
    assert [item["source"] for item in tight_episodes] == ["recent"]


def test_project_payload_prioritizes_relevant_session_and_includes_index() -> None:
    now = datetime.now(UTC)
    session_id = uuid4()
    relevant = EpisodeReference(
        session_id=session_id,
        session_title="Project session",
        anchor_message_id=uuid4(),
        anchor_created_at=now,
        anchor_role="user",
        anchor_content="Strongest topical anchor",
        excerpt=(),
    )
    latest_message_id = uuid4()
    project_session = ProjectSessionIndexItem(
        session_id=session_id,
        session_title="Project session",
        latest_message_id=latest_message_id,
    )
    memory = MemoryRecord(
        id=uuid4(),
        content="Lower-priority durable memory",
        category="fact",
        revision=1,
    )
    _, relevant_only, project_index = _bounded_automatic_payload(
        [],
        [relevant],
        [],
        max_characters=10_000,
        project_session=True,
        project_sessions=[project_session],
    )
    budget = len(
        json.dumps(
            {
                "durableMemories": [],
                "priorSessionEpisodes": relevant_only,
                "projectSessionIndex": project_index,
            },
            ensure_ascii=False,
        )
    )

    memories, episodes, project_index = _bounded_automatic_payload(
        [memory],
        [relevant],
        [relevant],
        max_characters=budget,
        project_session=True,
        project_sessions=[project_session],
    )

    assert memories == []
    assert [item["source"] for item in episodes] == ["relevant"]
    assert project_index == {
        "sessions": [
            {
                "title": "Project session",
                "sessionReadArguments": {
                    "session_id": str(session_id),
                    "around_message_id": str(latest_message_id),
                },
            }
        ],
        "hasMore": False,
    }
    assert len(json.dumps(project_index, ensure_ascii=False)) <= budget // 2
    assert episodes[0]["sessionReadArguments"] == {
        "session_id": str(session_id),
        "around_message_id": str(relevant.anchor_message_id),
    }


async def _runtime_setup(
    monkeypatch,
    results: list[ProviderResult],
    *,
    records: list[Message],
    automatic_items: list[MemoryRecord] | None = None,
    memory_mode: str = "full",
    project_session: bool = False,
):
    provider = LlmProviderConfig(
        name="Local",
        adapter_type="openai-compatible",
        trust_level=TrustLevel.PRIVATE,
        config={"base_url": "https://llm.example.test/v1"},
    )
    model = ModelConfig(provider_id=provider.id, model_id="local-model")
    ai_settings = AiSettings(
        providers=[provider], models=[model], default_model_id=model.id
    )
    runtime_settings = AgentRuntimeSettings()
    session = ChatSession(
        id=records[-1].session_id,
        user_id=uuid4(),
        title="Memory",
        model_config_id=model.id,
        memory_mode=memory_mode,
        web_search_policy="available",
        project_id=uuid4() if project_session else None,
    )
    project = (
        Project(
            id=session.project_id,
            user_id=session.user_id,
            name="Project",
            instructions="",
        )
        if session.project_id is not None
        else None
    )
    run = AgentRun(
        id=uuid4(),
        session_id=session.id,
        status="running",
        model_config_id=model.id,
        trust_level="private",
    )
    db = AsyncMock(spec=AsyncSession)
    db.get.return_value = session
    if project is not None:
        db.get.side_effect = lambda model, _id: project if model is Project else session

    async def load_settings(_db, settings_type, **_kwargs):
        return {
            AiProviderSettings: ai_settings,
            AgentIdentitySettings: AgentIdentitySettings(),
            AgentRuntimeSettings: runtime_settings,
            InstanceLimits: InstanceLimits(),
            MemorySettings: MemorySettings(enabled=True),
            SkillsSettings: SkillsSettings(),
            IntegrationSettings: IntegrationSettings(),
            ToolSettings: ToolSettings(),
            UserProfileSettings: UserProfileSettings(),
        }[settings_type]

    projections = []

    async def build_context(*_args, **kwargs):
        projections.append(kwargs)
        messages = [{"role": "system", "content": kwargs["system_prompt"]}]
        if kwargs.get("memory_context") is not None:
            messages.append({"role": "assistant", "content": kwargs["memory_context"]})
        messages.append({"role": "user", "content": records[-1].content})
        return ContextProjection(
            messages=messages,
            estimated_input_tokens=await kwargs["estimate_request"](messages),
            input_budget_tokens=1_000,
        )

    requests = []
    queue = list(results)

    async def complete(_db, **kwargs):
        requests.append(kwargs)
        result = queue.pop(0)
        if result.content and kwargs.get("on_delta"):
            await kwargs["on_delta"](result.content)
        return result

    retrieve = AsyncMock(
        return_value=MemoryRetrieval(
            items=automatic_items or [],
            mode=MemoryRetrieveMode.SEARCH,
            strategy=MemoryRetrievalStrategy.HYBRID,
            readable_by=TrustLevel.PRIVATE,
            offset=0,
            limit=2,
            has_more=False,
        )
    )
    execute_tool = AsyncMock(
        return_value=ToolResult.json(
            {
                "mode": "browse",
                "items": [],
                "hasMore": False,
                "nextCursor": None,
            },
            metadata={"hasMore": False, "nextCursor": None},
        )
    )
    monkeypatch.setattr(SettingsStore, "load", load_settings)
    monkeypatch.setattr(AgentRuntime, "_records", AsyncMock(return_value=records))
    monkeypatch.setattr("mato_loop_backend.automations.AutomationService.session_definitions", AsyncMock(return_value=[]))
    monkeypatch.setattr(AgentRuntime, "_finalize_run_files", AsyncMock())
    monkeypatch.setattr(ContextManager, "build", build_context)
    monkeypatch.setattr(ProviderService, "complete", staticmethod(complete))
    monkeypatch.setattr(MemoryService, "pinned_context", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        MemoryService,
        "automatic_context_by_ids",
        AsyncMock(return_value=automatic_items or []),
    )
    monkeypatch.setattr(
        MemoryService,
        "scope",
        AsyncMock(return_value=MemoryScope(total=15, pinned=2)),
    )
    monkeypatch.setattr(MemoryRetrievalService, "retrieve", retrieve)
    monkeypatch.setattr(
        EpisodeRetrievalService,
        "retrieve",
        AsyncMock(
            return_value=EpisodeRetrieval(
                references=[], strategy=MemoryRetrievalStrategy.HYBRID
            )
        ),
    )
    monkeypatch.setattr(
        EpisodeRetrievalService,
        "revalidate",
        AsyncMock(side_effect=lambda _db, references, **_kwargs: references),
    )
    monkeypatch.setattr(
        EpisodeRetrievalService,
        "recent",
        AsyncMock(
            return_value=EpisodeRetrieval(
                references=[], strategy=MemoryRetrievalStrategy.BROWSE
            )
        ),
    )
    monkeypatch.setattr(
        EpisodeRetrievalService,
        "project_index",
        AsyncMock(return_value=ProjectSessionIndex(items=[], has_more=False)),
    )
    monkeypatch.setattr(embedding_client, "embed_query", AsyncMock(return_value=[0.0]))
    monkeypatch.setattr(
        embedding_client,
        "embed_queries",
        AsyncMock(return_value=[[0.0], [1.0]]),
    )
    monkeypatch.setattr(EffectiveToolSet, "execute", execute_tool)
    monkeypatch.setattr(run_events, "publish", AsyncMock())
    return db, run, requests, projections, retrieve, execute_tool


@pytest.mark.parametrize(
    ("memory_mode", "automatic_enabled"),
    [("read_only", True), ("disabled", False)],
)
async def test_session_memory_mode_controls_automatic_context(
    monkeypatch, memory_mode: str, automatic_enabled: bool
) -> None:
    session_id = uuid4()
    user = Message(session_id=session_id, role="user", content="Was ist relevant?")
    db, run, _requests, _projections, retrieve, execute = await _runtime_setup(
        monkeypatch,
        [ProviderResult(tool_calls=[ToolCall("search", "context_search", {
            "query": "previous conversation", "scope": "sessions",
        })]), ProviderResult(content="Antwort", finish_reason="stop")],
        records=[user],
        memory_mode=memory_mode,
    )

    await AgentRuntime._execute_loop(db, run)

    assert retrieve.await_count == int(automatic_enabled)
    assert EpisodeRetrievalService.retrieve.await_count == int(automatic_enabled)
    assert EpisodeRetrievalService.recent.await_count == int(automatic_enabled)
    execute.assert_awaited_once()


async def test_automatic_retrieval_falls_back_to_one_lexical_query(monkeypatch) -> None:
    session_id = uuid4()
    previous = Message(
        session_id=session_id,
        role="user",
        content="PostgreSQL Migration Entscheidung",
    )
    current = Message(session_id=session_id, role="user", content="Und jetzt?")
    query = f"{previous.content}\n{current.content}"
    db, run, _requests, _projections, retrieve, _execute = await _runtime_setup(
        monkeypatch,
        [ProviderResult(content="Antwort", finish_reason="stop")],
        records=[previous, current],
    )
    embedding_client.embed_query.side_effect = EmbeddingError(
        "memory.embeddingUnavailable", transient=True
    )

    await AgentRuntime._execute_loop(db, run)

    embedding_client.embed_query.assert_awaited_once_with(query)
    assert retrieve.await_args.kwargs["query"] == query
    assert retrieve.await_args.kwargs["semantic_enabled"] is False
    assert EpisodeRetrievalService.retrieve.await_args.kwargs["query"] == query
    assert EpisodeRetrievalService.retrieve.await_args.kwargs["semantic_enabled"] is False


async def test_ordinary_turn_automatically_injects_unpinned_memories(
    monkeypatch,
) -> None:
    session_id = uuid4()
    source_at = datetime(2026, 7, 20, 10, tzinfo=UTC)
    created_at = datetime(2026, 7, 20, 11, tzinfo=UTC)
    updated_at = datetime(2026, 7, 21, 12, tzinfo=UTC)
    user = Message(
        id=uuid4(), session_id=session_id, role="user", content="Wie alt ist mein Sohn?"
    )
    relevant = MemoryRecord(
        id=uuid4(),
        content="Der Sohn des Nutzers ist neun Monate alt.",
        category="fact",
        pinned=False,
        audience="private_only",
        revision=1,
        created_at=created_at,
        updated_at=updated_at,
    )
    relevant.source_message = Message(created_at=source_at)
    db, run, requests, projections, retrieve, _ = await _runtime_setup(
        monkeypatch,
        [ProviderResult(content="Neun Monate.", finish_reason="stop")],
        records=[user],
        automatic_items=[relevant],
    )

    await AgentRuntime._execute_loop(db, run)

    retrieve.assert_awaited_once()
    assert retrieve.await_args.kwargs["pinned"] is False
    assert retrieve.await_args.kwargs["limit"] == 2
    assert "source_types" not in retrieve.await_args.kwargs
    MemoryService.pinned_context.assert_awaited_once()
    assert (
        MemoryService.pinned_context.await_args.kwargs["owner_id"]
        == db.get.return_value.user_id
    )
    assert '"source": "retrieved"' in projections[0]["memory_context"]
    assert relevant.content in projections[0]["memory_context"]
    assert "15 total, 2 pinned" in projections[0]["memory_context"]
    assert relevant.content not in projections[0]["system_prompt"]
    assert [message["role"] for message in requests[0]["messages"][:2]] == [
        "system",
        "assistant",
    ]
    assert requests[0]["tool_choice"] == "auto"
    embedding_client.embed_query.assert_awaited_once_with(user.content)
    embedding_client.embed_queries.assert_not_awaited()
    EpisodeRetrievalService.retrieve.assert_awaited_once()
    assert EpisodeRetrievalService.retrieve.await_args.kwargs["limit"] == 1
    assert EpisodeRetrievalService.retrieve.await_args.kwargs["full_sessions_only"] is True
    EpisodeRetrievalService.recent.assert_awaited_once()


async def test_cached_automatic_context_is_revalidated_without_reembedding(
    monkeypatch,
) -> None:
    session_id = uuid4()
    user = Message(
        id=uuid4(),
        session_id=session_id,
        role="user",
        content="Welche Deployment-Regel gilt für dieses Projekt?",
    )
    relevant = MemoryRecord(
        id=uuid4(),
        content="Deployments use the private production host.",
        category="context",
        source_type="manual",
        pinned=False,
        audience="private_only",
        revision=1,
    )
    db, run, _requests, projections, retrieve, _execute = await _runtime_setup(
        monkeypatch,
        [
            ProviderResult(
                tool_calls=[
                    ToolCall(
                        id="lookup",
                        name="context_search",
                        arguments={
                            "query": "Deployment-Regel",
                        },
                    )
                ]
            ),
            ProviderResult(content="Die Regel ist nicht mehr sichtbar.", finish_reason="stop"),
        ],
        records=[user],
        automatic_items=[relevant],
        project_session=True,
    )
    MemoryService.automatic_context_by_ids.return_value = []
    episodes = [
        EpisodeReference(
            session_id=uuid4(),
            project_id=project_id,
            session_title="Deployment",
            anchor_message_id=uuid4(),
            anchor_created_at=datetime.now(UTC),
            anchor_role="user",
            anchor_content="Deployment context from another session.",
            excerpt=(),
        )
        for project_id in (db.get.return_value.project_id, db.get.return_value.project_id)
    ]
    EpisodeRetrievalService.retrieve.return_value = EpisodeRetrieval(
        references=episodes[:1], strategy=MemoryRetrievalStrategy.HYBRID
    )
    EpisodeRetrievalService.recent.return_value = EpisodeRetrieval(
        references=episodes[1:], strategy=MemoryRetrievalStrategy.BROWSE
    )

    await AgentRuntime._execute_loop(db, run)

    retrieve.assert_awaited_once()
    embedding_client.embed_query.assert_awaited_once()
    MemoryService.automatic_context_by_ids.assert_awaited_once()
    assert relevant.content in projections[0]["memory_context"]
    assert relevant.content not in projections[1]["memory_context"]
    EpisodeRetrievalService.retrieve.assert_awaited_once()
    EpisodeRetrievalService.recent.assert_awaited_once()
    assert EpisodeRetrievalService.revalidate.await_count == 2
    for call, episode in zip(
        EpisodeRetrievalService.revalidate.await_args_list, episodes, strict=True
    ):
        assert call.args[1] == [episode]
        assert call.kwargs["project_id"] == db.get.return_value.project_id
        assert call.kwargs["full_sessions_only"] is True
        for projection in projections:
            assert str(episode.session_id) in projection["memory_context"]


async def test_project_session_uses_configured_relevant_session_limit(monkeypatch) -> None:
    session_id = uuid4()
    user = Message(session_id=session_id, role="user", content="What is relevant?")
    db, run, _requests, projections, _retrieve, _execute = await _runtime_setup(
        monkeypatch,
        [ProviderResult(content="Answer", finish_reason="stop")],
        records=[user],
        project_session=True,
    )
    historical_session_id = uuid4()
    latest_message_id = uuid4()
    EpisodeRetrievalService.project_index.return_value = ProjectSessionIndex(
        items=[
            ProjectSessionIndexItem(
                session_id=historical_session_id,
                session_title="Mealie deployment",
                latest_message_id=latest_message_id,
            )
        ],
        has_more=True,
    )

    await AgentRuntime._execute_loop(db, run)

    assert EpisodeRetrievalService.retrieve.await_args.kwargs["limit"] == 3
    assert EpisodeRetrievalService.retrieve.await_args.kwargs["project_id"] == db.get.return_value.project_id
    assert EpisodeRetrievalService.retrieve.await_args.kwargs["full_sessions_only"] is True
    EpisodeRetrievalService.project_index.assert_awaited_once_with(
        db,
        owner_id=db.get.return_value.user_id,
        current_session_id=session_id,
        project_id=db.get.return_value.project_id,
        readable_by=TrustLevel.PRIVATE,
    )
    prompt = projections[0]["memory_context"]
    assert '"title": "Mealie deployment"' in prompt
    assert f'"session_id": "{historical_session_id}"' in prompt
    assert f'"around_message_id": "{latest_message_id}"' in prompt
    assert '"hasMore": true' in prompt
    assert (
        "Before asking the user to repeat information or claiming that prior context is unavailable, "
        "read missing details from matching supplied references with session_read. "
        "For the latest state, prefer the reference in projectSessionIndex over an older topical anchor."
    ) in prompt


async def test_ordinary_first_turn_includes_two_latest_session_tails(monkeypatch) -> None:
    session_id = uuid4()
    now = datetime(2026, 8, 19, 10, tzinfo=UTC)
    latest_user = EpisodeExcerptMessage(
        id=uuid4(), role="user", content="Herr Simon hat noch nicht geantwortet.", created_at=now
    )
    latest_assistant = EpisodeExcerptMessage(
        id=uuid4(), role="assistant", content="Wir warten auf sein Angebot.", created_at=now
    )
    reference = EpisodeReference(
        session_id=uuid4(),
        session_title="Klimaanlage",
        anchor_message_id=latest_assistant.id,
        anchor_created_at=now,
        anchor_role="assistant",
        anchor_content=latest_assistant.content,
        excerpt=(latest_user, latest_assistant),
    )
    second_reference = EpisodeReference(
        session_id=uuid4(),
        session_title="Docker-Struktur",
        anchor_message_id=uuid4(),
        anchor_created_at=now,
        anchor_role="assistant",
        anchor_content="Stacks liegen unter /srv/<stackname>/.",
        excerpt=(),
    )
    user = Message(
        id=uuid4(), session_id=session_id, role="user", content="Machen wir weiter."
    )
    db, run, _requests, projections, _retrieve, _execute = await _runtime_setup(
        monkeypatch,
        [ProviderResult(content="Gern.", finish_reason="stop")],
        records=[user],
    )
    EpisodeRetrievalService.recent.return_value = EpisodeRetrieval(
        references=[reference, second_reference], strategy=MemoryRetrievalStrategy.BROWSE
    )
    await AgentRuntime._execute_loop(db, run)

    EpisodeRetrievalService.recent.assert_awaited_once()
    assert EpisodeRetrievalService.recent.await_args.kwargs["limit"] == 2
    assert EpisodeRetrievalService.recent.await_args.kwargs["full_sessions_only"] is True
    EpisodeRetrievalService.retrieve.assert_awaited_once()
    prompt = projections[0]["memory_context"]
    assert '"title": "Klimaanlage"' in prompt
    assert '"title": "Docker-Struktur"' in prompt
    assert latest_user.content in prompt
    assert latest_assistant.content in prompt
    assert '"sessionReadArguments"' in prompt


@pytest.mark.parametrize(
    "content",
    [
        "Do not use memories or the previous session for this answer.",
        "Translate this text: I live in Munich.",
    ],
)
async def test_suppression_skips_all_automatic_retrieval(monkeypatch, content: str) -> None:
    session_id = uuid4()
    user = Message(
        id=uuid4(),
        session_id=session_id,
        role="user",
        content=content,
    )
    db, run, _requests, _projections, retrieve, _execute = await _runtime_setup(
        monkeypatch,
        [ProviderResult(content="Verstanden.", finish_reason="stop")],
        records=[user],
    )

    await AgentRuntime._execute_loop(db, run)

    retrieve.assert_not_awaited()
    embedding_client.embed_query.assert_not_awaited()
    EpisodeRetrievalService.recent.assert_not_awaited()
    EpisodeRetrievalService.retrieve.assert_not_awaited()


async def test_explicit_latest_session_request_includes_a_bounded_excerpt(monkeypatch) -> None:
    session_id = uuid4()
    reference = EpisodeReference(
        session_id=uuid4(),
        session_title="Vorheriger Chat",
        anchor_message_id=uuid4(),
        anchor_created_at=datetime(2026, 8, 19, 10, tzinfo=UTC),
        anchor_role="assistant",
        anchor_content="Wir haben das Deployment vorbereitet.",
        excerpt=(),
    )
    user = Message(
        id=uuid4(),
        session_id=session_id,
        role="user",
        content="Was haben wir in der letzten Session besprochen?",
    )
    db, run, _requests, projections, _retrieve, _execute = await _runtime_setup(
        monkeypatch,
        [ProviderResult(content="Deployment.", finish_reason="stop")],
        records=[user],
    )
    EpisodeRetrievalService.recent.return_value = EpisodeRetrieval(
        references=[reference], strategy=MemoryRetrievalStrategy.BROWSE
    )

    await AgentRuntime._execute_loop(db, run)

    EpisodeRetrievalService.recent.assert_awaited_once()
    EpisodeRetrievalService.retrieve.assert_awaited_once()
    prompt = projections[0]["memory_context"]
    assert '"title": "Vorheriger Chat"' in prompt
    assert f'"sessionId": "{reference.session_id}"' in prompt
    assert '"sessionReadArguments"' in prompt


async def test_explicit_earlier_work_request_uses_lexical_episode_search(
    monkeypatch,
) -> None:
    session_id = uuid4()
    reference = EpisodeReference(
        session_id=uuid4(),
        session_title="Datenbankwahl",
        anchor_message_id=uuid4(),
        anchor_created_at=datetime(2026, 8, 19, 10, tzinfo=UTC),
        anchor_role="assistant",
        anchor_content="Wir haben PostgreSQL gewählt.",
        excerpt=(),
    )
    user = Message(
        id=uuid4(),
        session_id=session_id,
        role="user",
        content="Wie wir zuvor besprochen haben: Welche Datenbank nutzen wir?",
    )
    db, run, _requests, projections, _retrieve, _execute = await _runtime_setup(
        monkeypatch,
        [ProviderResult(content="PostgreSQL.", finish_reason="stop")],
        records=[user],
    )
    EpisodeRetrievalService.retrieve.return_value = EpisodeRetrieval(
        references=[reference], strategy=MemoryRetrievalStrategy.LEXICAL
    )

    await AgentRuntime._execute_loop(db, run)

    EpisodeRetrievalService.recent.assert_awaited_once()
    EpisodeRetrievalService.retrieve.assert_awaited_once()
    kwargs = EpisodeRetrievalService.retrieve.await_args.kwargs
    assert kwargs["query"] == user.content
    assert kwargs["semantic_enabled"] is True
    assert kwargs["max_semantic_distance"] == 0.25
    assert kwargs["anchor_only"] is True
    assert kwargs["full_sessions_only"] is True
    assert '"title": "Datenbankwahl"' in projections[0]["memory_context"]


async def test_ordinary_follow_up_reuses_one_query_for_durable_and_episode_context(
    monkeypatch,
) -> None:
    session_id = uuid4()
    previous = Message(
        id=uuid4(),
        session_id=session_id,
        role="user",
        content="Ich möchte mich als Techniker bewerben.",
    )
    current = Message(
        id=uuid4(),
        session_id=session_id,
        role="user",
        content="du hast dohc meine daten?",
    )
    db, run, _requests, projections, retrieve, _execute = await _runtime_setup(
        monkeypatch,
        [ProviderResult(content="Ja, deine Daten sind gespeichert.", finish_reason="stop")],
        records=[previous, current],
    )
    await AgentRuntime._execute_loop(db, run)

    retrieve.assert_awaited_once()
    expected_query = f"{previous.content}\n{current.content}"
    assert retrieve.await_args.kwargs["query"] == expected_query
    embedding_client.embed_query.assert_awaited_once_with(expected_query)
    embedding_client.embed_queries.assert_not_awaited()
    EpisodeRetrievalService.recent.assert_not_awaited()
    EpisodeRetrievalService.retrieve.assert_awaited_once()
    assert EpisodeRetrievalService.retrieve.await_args.kwargs["query_embedding"] == [0.0]
    system_prompt = projections[0]["system_prompt"]
    assert "claim that no user data is stored while visible entries are reported" in system_prompt


async def test_context_overflow_drops_session_bootstrap_before_failing(
    monkeypatch,
) -> None:
    session_id = uuid4()
    user = Message(
        id=uuid4(), session_id=session_id, role="user", content="Was ist relevant?"
    )
    reference = EpisodeReference(
        session_id=uuid4(),
        session_title="Vorheriger Chat",
        anchor_message_id=uuid4(),
        anchor_created_at=datetime(2026, 8, 19, 10, tzinfo=UTC),
        anchor_role="assistant",
        anchor_content="Automatisch geladener Session-Kontext",
        excerpt=(),
    )
    db, run, _requests, _projections, _retrieve, _execute = await _runtime_setup(
        monkeypatch,
        [ProviderResult(content="Antwort", finish_reason="stop")],
        records=[user],
    )
    EpisodeRetrievalService.recent.return_value = EpisodeRetrieval(
        references=[reference], strategy=MemoryRetrievalStrategy.BROWSE
    )
    fallback_projection = ContextProjection(
        messages=[
            {"role": "system", "content": "fallback"},
            {"role": "user", "content": user.content},
        ],
        estimated_input_tokens=100,
        input_budget_tokens=1_000,
    )
    build = AsyncMock(
        side_effect=[
            ContextError("agent.contextWindowExceeded"),
            fallback_projection,
        ]
    )
    monkeypatch.setattr(ContextManager, "build", build)

    await AgentRuntime._execute_loop(db, run)

    assert build.await_count == 2
    assert reference.anchor_content in build.await_args_list[0].kwargs["memory_context"]
    assert reference.anchor_content not in build.await_args_list[1].kwargs["memory_context"]
    assert (
        build.await_args_list[0].kwargs["system_prompt"]
        == build.await_args_list[1].kwargs["system_prompt"]
    )


async def test_memory_write_workflow_discards_drafts_until_write_resolves(
    monkeypatch,
) -> None:
    session_id = uuid4()
    user = Message(
        id=uuid4(),
        session_id=session_id,
        role="user",
        content="Ich komme aus Ascholding.",
    )
    results = [
        ProviderResult(
            tool_calls=[
                ToolCall(
                    id="invalid-write",
                    name="memory_write",
                    arguments={
                        "action": "update",
                        "memory_id": str(uuid4()),
                        "content": "Der Nutzer kommt aus Ascholding.",
                        "category": "identity",
                    },
                )
            ]
        ),
        ProviderResult(content="Ascholding ist gespeichert.", finish_reason="stop"),
        ProviderResult(
            tool_calls=[
                ToolCall(
                    id="lookup",
                    name="context_search",
                    arguments={
                        "scope": "memories",
                        "query": "Herkunft Ascholding",
                    },
                )
            ]
        ),
        ProviderResult(
            tool_calls=[
                ToolCall(
                    id="valid-write",
                    name="memory_write",
                    arguments={
                        "action": "create",
                        "content": "Der Nutzer kommt aus Ascholding.",
                        "category": "identity",
                    },
                ),
                ToolCall(
                    id="duplicate-write",
                    name="memory_write",
                    arguments={
                        "action": "create",
                        "content": "Der Nutzer kommt aus Ascholding.",
                        "category": "identity",
                    },
                ),
            ]
        ),
        ProviderResult(content="Ich habe deine Herkunft gespeichert.", finish_reason="stop"),
    ]
    db, run, requests, projections, retrieve, execute_tool = await _runtime_setup(
        monkeypatch,
        results,
        records=[user],
    )
    monkeypatch.setattr(AgentRuntime, "_session_settings", {run.id: RunSessionSettings(
        model_config_id=run.model_config_id, session_profile={}, memory_mode="full", web_search_policy="available",
    )})
    db.refresh.side_effect = lambda item: setattr(item, "memory_mode", "disabled") if isinstance(item, ChatSession) else None
    refreshed_pin = MemoryRecord(
        id=uuid4(),
        content="Der Nutzer kommt aus Ascholding.",
        category="identity",
        pinned=True,
        audience="private_only",
        revision=1,
    )
    MemoryService.pinned_context.side_effect = [[], [], [], [], [refreshed_pin]]
    execute_tool.side_effect = [
        ToolResult.error("tool.invalidArguments", field="expected_revision"),
        ToolResult.json({"memories": []}),
        ToolResult.json(
            {"stored": True, "updated": False},
            metadata={"memoryWriteOutcome": "stored"},
        ),
        ToolResult.json(
            {"stored": False, "updated": False, "duplicate": True},
            metadata={"memoryWriteOutcome": "duplicate"},
        ),
    ]

    await AgentRuntime._execute_loop(db, run)

    assert retrieve.await_count == 2
    assert embedding_client.embed_query.await_count == 2
    assert EpisodeRetrievalService.retrieve.await_count == 2
    assert EpisodeRetrievalService.recent.await_count == 2
    assert MemoryService.pinned_context.await_count == len(results)
    assert MemoryService.scope.await_count == 2
    assert [request["tool_choice"] for request in requests] == [
        "auto",
        "required",
        "required",
        "required",
        "auto",
    ]
    assert all(
        {tool["name"] for tool in request["tools"]}
        == {"context_search", "memory_browse", "memory_write"}
        for request in requests[1:4]
    )
    assert "Required memory mutation" in projections[1]["system_prompt"]
    assert "Host-confirmed memory outcome" in projections[-1]["system_prompt"]
    assert "memory_write succeeded" in projections[-1]["system_prompt"]
    assert refreshed_pin.content in projections[-1]["memory_context"]
    assert "already stored" not in projections[-1]["system_prompt"]
    persisted_assistant_texts = [
        call.args[0].content
        for call in db.add.call_args_list
        if isinstance(call.args[0], Message) and call.args[0].role == "assistant"
    ]
    assert "Ascholding ist gespeichert." not in persisted_assistant_texts
    assert persisted_assistant_texts[-1] == "Ich habe deine Herkunft gespeichert."
    assert execute_tool.await_count == 4


@pytest.mark.parametrize(
    ("outcome", "expected_retrievals"),
    [("forgotten", 2), ("duplicate", 1)],
)
async def test_memory_write_outcome_controls_automatic_context_cache(
    monkeypatch, outcome: str, expected_retrievals: int
) -> None:
    session_id = uuid4()
    user = Message(
        id=uuid4(),
        session_id=session_id,
        role="user",
        content="Merke dir, dass ich aus Ascholding komme.",
    )
    results = [
        ProviderResult(
            tool_calls=[
                ToolCall(
                    id="duplicate",
                    name="memory_write",
                    arguments={
                        "action": "create",
                        "content": "Der Nutzer kommt aus Ascholding.",
                        "category": "identity",
                    },
                )
            ]
        ),
        ProviderResult(content="Das war bereits gespeichert.", finish_reason="stop"),
    ]
    db, run, _requests, _projections, retrieve, execute_tool = await _runtime_setup(
        monkeypatch,
        results,
        records=[user],
    )
    execute_tool.return_value = ToolResult.json(
        {"stored": False, "updated": False, outcome: True},
        metadata={"memoryWriteOutcome": outcome},
    )

    await AgentRuntime._execute_loop(db, run)

    assert retrieve.await_count == expected_retrievals
    assert embedding_client.embed_query.await_count == expected_retrievals
    assert EpisodeRetrievalService.retrieve.await_count == expected_retrievals
    assert EpisodeRetrievalService.recent.await_count == expected_retrievals
    assert MemoryService.pinned_context.await_count == len(results)
    assert MemoryService.scope.await_count == expected_retrievals


def test_memory_overview_stops_before_exceeding_its_character_budget() -> None:
    items = [
        MemoryRecord(
            id=uuid4(),
            content=character * 7_000,
            category="fact",
            pinned=False,
            audience="private_only",
            revision=1,
        )
        for character in ("a", "b")
    ]

    payload, last_item, omitted_items = _bounded_memory_overview(items)

    assert sum(len(item["content"]) for item in payload) <= (
        MEMORY_OVERVIEW_MAX_CHARACTERS
    )
    assert len(payload) == 1
    assert last_item is items[0]
    assert omitted_items is True


async def test_memory_overview_is_prefetched_by_the_host(monkeypatch) -> None:
    session_id = uuid4()
    source_at = datetime(2026, 7, 20, 10, tzinfo=UTC)
    created_at = datetime(2026, 7, 20, 11, tzinfo=UTC)
    updated_at = datetime(2026, 7, 23, 12, tzinfo=UTC)
    user = Message(
        id=uuid4(),
        session_id=session_id,
        role="user",
        content="Was hast du alles in deiner Erinnerung?",
    )
    memory = MemoryRecord(
        id=uuid4(),
        content="Der Nutzer bevorzugt lokale Modelle.",
        category="preference",
        pinned=False,
        audience="private_only",
        revision=1,
        created_at=created_at,
        updated_at=updated_at,
    )
    memory.source_message = Message(created_at=source_at)
    shared_identity = MemoryRecord(
        id=uuid4(),
        content="Der gemeinsame Name ist Alt.",
        category="identity",
        pinned=True,
        audience="both",
        revision=1,
        created_at=created_at,
        updated_at=updated_at,
    )
    pinned = MemoryRecord(
        id=uuid4(),
        content="Der private Name ist Neu.",
        category="identity",
        pinned=True,
        audience="private_only",
        revision=1,
        created_at=created_at,
        updated_at=updated_at,
    )
    db, run, requests, projections, retrieve, execute_tool = await _runtime_setup(
        monkeypatch,
        [ProviderResult(content="Übersicht", finish_reason="stop")],
        records=[user],
    )
    MemoryService.pinned_context.return_value = [shared_identity, pinned]
    retrieve.return_value = MemoryRetrieval(
        items=[shared_identity, memory],
        mode=MemoryRetrieveMode.BROWSE,
        strategy=MemoryRetrievalStrategy.BROWSE,
        readable_by=TrustLevel.PRIVATE,
        offset=0,
        limit=10,
        has_more=False,
        total=2,
    )

    await AgentRuntime._execute_loop(db, run)

    assert [request["tool_choice"] for request in requests] == ["auto"]
    assert "host prefetched a persistent-memory overview page" in projections[0][
        "memory_context"
    ]
    assert memory.content in projections[0]["memory_context"]
    assert pinned.content in projections[0]["memory_context"]
    assert projections[0]["memory_context"].count(shared_identity.content) == 1
    assert f'"sourceMessageAt": "{source_at.isoformat()}"' in projections[0][
        "memory_context"
    ]
    assert f'"memoryCreatedAt": "{created_at.isoformat()}"' in projections[0][
        "memory_context"
    ]
    assert f'"memoryUpdatedAt": "{updated_at.isoformat()}"' in projections[0][
        "memory_context"
    ]
    assert retrieve.await_args.kwargs["mode"] == MemoryRetrieveMode.BROWSE
    assert retrieve.await_args.kwargs["limit"] == 10
    execute_tool.assert_not_awaited()
    persisted = [
        call.args[0]
        for call in db.add.call_args_list
        if isinstance(call.args[0], Message)
    ]
    assistant = next(item for item in persisted if item.role == "assistant")
    assert assistant.provider_data["_matoloop"]["memoryOverview"] == {
        "hasMore": False,
        "nextCursor": None,
    }


async def test_memory_overview_revalidates_between_model_steps(monkeypatch) -> None:
    session_id = uuid4()
    user = Message(
        id=uuid4(),
        session_id=session_id,
        role="user",
        content="Was hast du alles in deiner Erinnerung?",
    )
    pinned = MemoryRecord(
        id=uuid4(),
        content="Private pinned memory",
        category="fact",
        pinned=True,
        audience="private_only",
    )
    memory = MemoryRecord(
        id=uuid4(),
        content="Private browsed memory",
        category="fact",
        pinned=False,
        audience="private_only",
    )
    db, run, _requests, projections, retrieve, _execute_tool = await _runtime_setup(
        monkeypatch,
        [
            ProviderResult(
                tool_calls=[
                    ToolCall(
                        id="browse",
                        name="memory_browse",
                        arguments={},
                    )
                ],
                finish_reason="tool_calls",
            ),
            ProviderResult(content="Übersicht", finish_reason="stop"),
        ],
        records=[user],
    )
    MemoryService.pinned_context.side_effect = [[pinned], []]
    retrieve.side_effect = [
        MemoryRetrieval(
            items=[memory],
            mode=MemoryRetrieveMode.BROWSE,
            strategy=MemoryRetrievalStrategy.BROWSE,
            readable_by=TrustLevel.PRIVATE,
            offset=0,
            limit=10,
            has_more=False,
        ),
        MemoryRetrieval(
            items=[],
            mode=MemoryRetrieveMode.BROWSE,
            strategy=MemoryRetrievalStrategy.BROWSE,
            readable_by=TrustLevel.PRIVATE,
            offset=0,
            limit=10,
            has_more=False,
        ),
    ]

    await AgentRuntime._execute_loop(db, run)

    assert pinned.content in projections[0]["memory_context"]
    assert memory.content in projections[0]["memory_context"]
    assert pinned.content not in projections[1]["memory_context"]
    assert memory.content not in projections[1]["memory_context"]
    assert retrieve.await_count == 2


async def test_memory_overview_follow_up_uses_persisted_cursor(monkeypatch) -> None:
    session_id = uuid4()
    cursor_time = datetime(2026, 7, 23, 12, tzinfo=UTC)
    cursor_id = uuid4()
    previous_user = Message(
        id=uuid4(),
        session_id=session_id,
        role="user",
        content="Was weißt du über mich?",
    )
    previous_assistant = Message(
        id=uuid4(),
        session_id=session_id,
        role="assistant",
        content="Erste Seite",
        provider_data={
            "_matoloop": {
                "memoryOverview": {
                    "hasMore": True,
                    "nextCursor": {
                        "updatedAt": cursor_time.isoformat(),
                        "id": str(cursor_id),
                    },
                }
            }
        },
    )
    user = Message(
        id=uuid4(), session_id=session_id, role="user", content="Was noch?"
    )
    db, run, requests, projections, retrieve, execute_tool = await _runtime_setup(
        monkeypatch,
        [ProviderResult(content="Nächste Seite", finish_reason="stop")],
        records=[previous_user, previous_assistant, user],
    )
    retrieve.return_value = MemoryRetrieval(
        items=[],
        mode=MemoryRetrieveMode.BROWSE,
        strategy=MemoryRetrievalStrategy.BROWSE,
        readable_by=TrustLevel.PRIVATE,
        offset=0,
        limit=10,
        has_more=False,
        total=15,
    )

    await AgentRuntime._execute_loop(db, run)

    assert [request["tool_choice"] for request in requests] == ["auto"]
    assert retrieve.await_args.kwargs["cursor_updated_at"] == cursor_time
    assert retrieve.await_args.kwargs["cursor_id"] == cursor_id
    assert "exhausts the currently visible" in projections[0]["memory_context"]
    execute_tool.assert_not_awaited()


def test_memory_overview_follow_up_is_detected_after_browse() -> None:
    session_id = uuid4()
    records = [
        Message(
            id=uuid4(),
            session_id=session_id,
            role="user",
            content="Was weißt du über mich?",
        ),
        Message(
            id=uuid4(),
            session_id=session_id,
            role="assistant",
            content="",
            tool_calls=[
                {
                    "id": "browse",
                    "name": "memory_retrieve",
                    "arguments": {"mode": "browse", "offset": 0},
                }
            ],
        ),
        Message(
            id=uuid4(),
            session_id=session_id,
            role="tool",
            content=json.dumps({"items": [], "hasMore": False}),
            tool_call_id="browse",
        ),
        Message(
            id=uuid4(),
            session_id=session_id,
            role="user",
            content="Das ist im Kontext, aber was weißt du noch?",
        ),
    ]

    assert _memory_overview_requested(records) is True
