import asyncio
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import mato_loop_backend.agent.loop as mock_agent_loop
import mato_loop_backend.agent.runtime as mock_agent_runtime
import mato_loop_backend.agent.summarization as mock_agent_summarization
import mato_loop_backend.agent.titles as mock_agent_titles
from mato_loop_backend.agent.projection import compaction_messages, estimate_tokens
from mato_loop_backend.agent.runtime import AgentRuntime
from mato_loop_backend.agent.title_generation import (
    generated_session_title as _generated_session_title,
)
from mato_loop_backend.agent.title_generation import (
    initial_title_output_budget,
    repair_title_output_budget,
    title_input,
)
from mato_loop_backend.events import run_events
from mato_loop_backend.models import ChatSession, Message, MessageAttachment, TitleGenerationStatus
from mato_loop_backend.providers import service as service_module
from mato_loop_backend.providers.base import ProviderError, ProviderResult, PublicReasoning
from mato_loop_backend.providers.output_budget import allocate_output_tokens, guarded_input_tokens
from mato_loop_backend.providers.service import ProviderService
from mato_loop_backend.settings import (
    AgentRuntimeSettings,
    AiProviderSettings,
    AiSettings,
    ReasoningEffort,
    SettingsStore,
)
from mato_loop_backend.settings.models import (
    LlmProviderConfig,
    ModelConfig,
    ModelDefaults,
    ModelDefaultsSettings,
)

_VALID_SUMMARY = """Goal
Continue.

Facts
Known facts.

Decisions
None.

Tool effects
None.

Open items
None.

Source message IDs
None.
"""


@pytest.fixture(autouse=True)
def model_defaults(monkeypatch):
    async def load_optional(db, settings_type, **_kwargs):
        if settings_type is not ModelDefaultsSettings:
            return None
        ai = await SettingsStore.load(db, AiProviderSettings)
        return ModelDefaultsSettings(**ai.model_dump(include=set(ModelDefaults.model_fields)))

    monkeypatch.setattr(SettingsStore, "load_optional", load_optional)


@pytest.fixture
def compaction_db(monkeypatch):
    db = AsyncMock(spec=AsyncSession)
    db.__aenter__.return_value = db

    async def close(*_args):
        await db.close()

    db.__aexit__.side_effect = close
    def _mock_value():
        return db
    for _mock_target in (mock_agent_runtime, mock_agent_summarization, mock_agent_titles,):
        monkeypatch.setattr(_mock_target, 'AsyncSessionFactory', _mock_value)
    return db


@pytest.mark.parametrize(
    ("manual", "discovered", "expected"),
    [(None, None, 512), (128, None, 128), (None, 64, 64), (128, 64, 64)],
)
@pytest.mark.parametrize("request_timeout", [10, 300])
async def test_compaction_uses_global_model_before_session_model(
    monkeypatch, compaction_db, manual, discovered, expected, request_timeout,
) -> None:
    provider = LlmProviderConfig(name="Local", adapter_type="openai-compatible")
    active = ModelConfig(provider_id=provider.id, model_id="agent")
    compaction = ModelConfig(
        provider_id=provider.id, model_id="compact",
        max_output_tokens=manual, discovered_max_output_tokens=discovered,
    )
    settings = AiSettings(
        providers=[provider],
        models=[active, compaction],
        compaction_model_id=compaction.id,
    )
    complete = AsyncMock(return_value=SimpleNamespace(content=_VALID_SUMMARY))
    monkeypatch.setattr(ProviderService, "complete", complete)
    timeout = Mock(wraps=asyncio.timeout)
    monkeypatch.setattr(mock_agent_runtime.asyncio, "timeout", timeout)

    result = await AgentRuntime._summarize(
        "USER: Test",
        512,
        ai_settings=settings,
        active_model=active,
        runtime_settings=AgentRuntimeSettings(request_timeout_seconds=request_timeout),
    )

    assert result.content == _VALID_SUMMARY.strip()
    assert result.is_fallback is False
    assert result.model_config_id == compaction.id
    assert complete.await_args.args == (compaction_db,)
    compaction_db.close.assert_awaited_once()
    timeout.assert_called_once_with(request_timeout)
    assert complete.await_args.kwargs["timeout"] == request_timeout
    assert complete.await_args.kwargs["model"] is compaction
    assert complete.await_args.kwargs["tools"] == []
    assert complete.await_args.kwargs["max_output_tokens"] == expected
    assert complete.await_args.kwargs["reserved_output_tokens"] == expected
    assert "estimated_input_tokens" not in complete.await_args.kwargs


async def test_compaction_fit_includes_system_message_and_envelope(monkeypatch, compaction_db) -> None:
    provider = LlmProviderConfig(
        name="Local", adapter_type="openai-compatible", pseudonymization_enabled=False,
        config={"base_url": "https://local.test/v1"},
    )
    source = "USER: Test"
    output_tokens = 128
    prompt_tokens = estimate_tokens(compaction_messages(source)[1]["content"], 4)
    compaction = ModelConfig(
        provider_id=provider.id, model_id="compact", max_output_tokens=None,
        context_window_tokens=prompt_tokens + output_tokens, characters_per_token=4,
    )
    active = ModelConfig(provider_id=provider.id, model_id="agent", max_output_tokens=None)
    complete = AsyncMock(wraps=ProviderService.complete)
    transport = AsyncMock(return_value=ProviderResult(content=_VALID_SUMMARY))
    monkeypatch.setattr(service_module, "chat_completions", transport)
    monkeypatch.setattr(ProviderService, "complete", complete)

    result = await AgentRuntime._summarize(
        source, output_tokens,
        ai_settings=AiSettings(
            providers=[provider], models=[active, compaction], compaction_model_id=compaction.id,
        ),
        active_model=active, runtime_settings=AgentRuntimeSettings(safety_reserve_tokens=0),
    )

    assert result.model_config_id == active.id
    assert complete.await_count == 2
    transport.assert_awaited_once()
    assert complete.await_args.kwargs["model"] is active


@pytest.mark.parametrize("headroom", [-1, 0])
@pytest.mark.parametrize("output_tokens", [7, 512])
async def test_compaction_candidate_fit_includes_shared_slack(
    monkeypatch, compaction_db, headroom, output_tokens,
):
    provider = LlmProviderConfig(
        name="Local", adapter_type="openai-compatible", pseudonymization_enabled=False,
        config={"base_url": "https://local.test/v1"},
    )
    source = "x" * 1000
    compaction = ModelConfig(
        provider_id=provider.id, model_id="compact", characters_per_token=4,
    )
    prepared = await ProviderService.prepare_request(
        provider=provider, model=compaction, messages=compaction_messages(source), tools=[],
    )
    raw_estimate = prepared.estimated_input_tokens
    compaction.context_window_tokens = (
        guarded_input_tokens(raw_estimate) + output_tokens + 4096 + headroom
    )
    active = ModelConfig(provider_id=provider.id, model_id="agent")
    complete = AsyncMock(wraps=ProviderService.complete)
    transport = AsyncMock(return_value=ProviderResult(content=_VALID_SUMMARY))
    monkeypatch.setattr(service_module, "chat_completions", transport)
    monkeypatch.setattr(ProviderService, "complete", complete)
    result = await AgentRuntime._summarize(
        source, output_tokens,
        ai_settings=AiSettings(
            providers=[provider], models=[active, compaction], compaction_model_id=compaction.id,
        ),
        active_model=active, runtime_settings=AgentRuntimeSettings(),
    )
    assert result.model_config_id == (compaction.id if headroom == 0 else active.id)
    assert complete.await_count == (1 if headroom == 0 else 2)
    transport.assert_awaited_once()
    request = complete.await_args.kwargs
    assert "estimated_input_tokens" not in request
    assert allocate_output_tokens(
        request["model"], raw_estimate,
        requested=request["max_output_tokens"],
        reserved_output_tokens=request["reserved_output_tokens"],
        safety_reserve_tokens=request["safety_reserve_tokens"],
    ) == output_tokens


async def test_compaction_falls_back_to_active_model_after_specialist_failure(
    monkeypatch, compaction_db,
) -> None:
    provider = LlmProviderConfig(name="Local", adapter_type="openai-compatible")
    active = ModelConfig(provider_id=provider.id, model_id="agent")
    compaction = ModelConfig(provider_id=provider.id, model_id="compact")
    settings = AiSettings(
        providers=[provider],
        models=[active, compaction],
        compaction_model_id=compaction.id,
    )
    complete = AsyncMock(
        side_effect=[RuntimeError("kaputt"), SimpleNamespace(content=_VALID_SUMMARY)]
    )
    monkeypatch.setattr(ProviderService, "complete", complete)

    result = await AgentRuntime._summarize(
        "USER: Test",
        512,
        ai_settings=settings,
        active_model=active,
        runtime_settings=AgentRuntimeSettings(),
    )

    assert result.model_config_id == active.id
    compaction_db.rollback.assert_awaited_once()
    assert result.is_fallback is False
    assert [call.kwargs["model"] for call in complete.await_args_list] == [
        compaction,
        active,
    ]


async def test_compaction_streams_visible_output_and_resets_failed_candidate(
    monkeypatch, compaction_db,
) -> None:
    provider = LlmProviderConfig(name="Local", adapter_type="openai-compatible")
    active = ModelConfig(provider_id=provider.id, model_id="agent")
    compaction = ModelConfig(provider_id=provider.id, model_id="compact")
    settings = AiSettings(
        providers=[provider],
        models=[active, compaction],
        compaction_model_id=compaction.id,
    )
    calls = 0

    async def complete(_db, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            await kwargs["on_delta"]("Discarded draft")
            raise RuntimeError("failed candidate")
        await kwargs["on_delta"](_VALID_SUMMARY[:20])
        await kwargs["on_delta"](_VALID_SUMMARY[20:])
        return ProviderResult(content=_VALID_SUMMARY)

    monkeypatch.setattr(ProviderService, "complete", complete)
    updates = []

    async def on_update(content, is_fallback):
        updates.append((content, is_fallback))

    result = await AgentRuntime._summarize(
        "USER: Test",
        512,
        ai_settings=settings,
        active_model=active,
        runtime_settings=AgentRuntimeSettings(),
        on_update=on_update,
    )

    assert result.model_config_id == active.id
    assert ("Discarded draft", False) in updates
    discarded_index = updates.index(("Discarded draft", False))
    assert ("", False) in updates[discarded_index + 1 :]
    assert updates[-1] == (_VALID_SUMMARY.strip(), False)


async def test_compaction_disables_optional_reasoning(monkeypatch, compaction_db) -> None:
    provider = LlmProviderConfig(name="Local", adapter_type="openai-compatible")
    model = ModelConfig(provider_id=provider.id, model_id="Qwen3.8-27B-GGUF-UD-Q4_K_XL")
    settings = AiSettings(providers=[provider], models=[model])
    complete = AsyncMock(return_value=ProviderResult(content=_VALID_SUMMARY))
    monkeypatch.setattr(ProviderService, "complete", complete)

    await AgentRuntime._summarize(
        "USER: Test",
        512,
        ai_settings=settings,
        active_model=model,
        runtime_settings=AgentRuntimeSettings(),
    )

    assert complete.await_args.kwargs["reasoning_effort"] == ReasoningEffort.NONE


async def test_compaction_marks_host_fallback_after_empty_visible_output(
    monkeypatch, compaction_db,
) -> None:
    provider = LlmProviderConfig(name="Local", adapter_type="openai-compatible")
    model = ModelConfig(provider_id=provider.id, model_id="compact")
    settings = AiSettings(providers=[provider], models=[model])
    monkeypatch.setattr(
        ProviderService,
        "complete",
        AsyncMock(return_value=ProviderResult(reasoning=PublicReasoning("Only reasoning"))),
    )

    result = await AgentRuntime._summarize(
        "USER: Test",
        512,
        ai_settings=settings,
        active_model=model,
        runtime_settings=AgentRuntimeSettings(),
    )

    assert result.is_fallback is True
    assert result.model_config_id is None
    assert "USER: Test" in result.content


@pytest.mark.parametrize(
    ("stall", "expected_models"),
    [
        ("backoff", ["compact"]),
        ("retry", ["compact", "compact"]),
        ("active", ["compact", "compact", "agent"]),
    ],
)
async def test_compaction_deadline_covers_retries_and_candidates(
    monkeypatch, compaction_db, stall, expected_models,
) -> None:
    provider = LlmProviderConfig(
        name="Local", adapter_type="openai-compatible", pseudonymization_enabled=False,
        config={"base_url": "https://local.test/v1"},
    )
    active = ModelConfig(provider_id=provider.id, model_id="agent")
    compaction = ModelConfig(provider_id=provider.id, model_id="compact")
    models = []
    cancelled = []

    async def transport(**kwargs):
        model = kwargs["request"].model
        models.append(model)
        if len(models) == 1:
            raise ProviderError(
                "Transient failure", status_code=503,
                retry_after=30 if stall == "backoff" else 0,
            )
        if model == "compact" and stall == "active":
            return ProviderResult()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(model)
            raise

    monkeypatch.setattr(service_module, "chat_completions", transport)
    runtime_settings = AgentRuntimeSettings().model_copy(
        update={"request_timeout_seconds": 0.05}
    )
    timeout = Mock(wraps=asyncio.timeout)
    monkeypatch.setattr(mock_agent_runtime.asyncio, "timeout", timeout)
    on_update = AsyncMock()

    result = await asyncio.wait_for(
        AgentRuntime._summarize(
            "USER: Test", 512,
            ai_settings=AiSettings(
                providers=[provider], models=[active, compaction], compaction_model_id=compaction.id,
            ),
            active_model=active, runtime_settings=runtime_settings, on_update=on_update,
        ),
        timeout=1,
    )

    timeout.assert_called_once_with(0.05)
    compaction_db.close.assert_awaited_once()
    assert models == expected_models
    assert cancelled == ([] if stall == "backoff" else [expected_models[-1]])
    assert result.is_fallback is True
    assert result.model_config_id is None
    assert result.content == (
        "Goal\nContinue the conversation using the retained recent messages.\n\n"
        "Facts and earlier context\nUSER: Test"
    )
    assert on_update.await_args.args == (result.content, True)


async def test_compaction_disposes_cancelled_credential_session_before_fallback(
    monkeypatch, compaction_db,
) -> None:
    provider = LlmProviderConfig(
        name="Local", adapter_type="openai-compatible", pseudonymization_enabled=False,
        config={"base_url": "https://local.test/v1"}, credential_id=uuid4(),
    )
    model = ModelConfig(provider_id=provider.id, model_id="compact")
    events = []

    async def credential_read(*_args, **_kwargs):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            compaction_db.is_active = False
            events.append("invalidated")
            raise

    async def close():
        assert compaction_db.is_active is False
        await asyncio.sleep(0)
        events.append("closed")

    async def on_update(_content, is_fallback):
        if is_fallback:
            assert events == ["invalidated", "closed"]
            events.append("fallback")

    compaction_db.get.side_effect = credential_read
    compaction_db.close.side_effect = close
    transport = AsyncMock()
    monkeypatch.setattr(service_module, "chat_completions", transport)
    runtime_settings = AgentRuntimeSettings().model_copy(
        update={"request_timeout_seconds": 0.05}
    )
    factory = Mock(side_effect=[AsyncSession(), compaction_db])
    _mock_value = factory
    for _mock_target in (mock_agent_runtime, mock_agent_summarization, mock_agent_titles,):
        monkeypatch.setattr(_mock_target, 'AsyncSessionFactory', _mock_value)

    async with mock_agent_runtime.AsyncSessionFactory() as parent_db:
        record = ChatSession(id=uuid4(), title="Pending parent context")
        parent_db.add(record)
        monkeypatch.setattr(parent_db, "rollback", AsyncMock(wraps=parent_db.rollback))
        monkeypatch.setattr(parent_db, "commit", AsyncMock(wraps=parent_db.commit))
        monkeypatch.setattr(parent_db, "flush", AsyncMock(wraps=parent_db.flush))

        result = await asyncio.wait_for(
            AgentRuntime._summarize(
                "USER: Test", 512,
                ai_settings=AiSettings(providers=[provider], models=[model]),
                active_model=model, runtime_settings=runtime_settings, on_update=on_update,
            ),
            timeout=1,
        )

        assert result.is_fallback is True
        assert events == ["invalidated", "closed", "fallback"]
        assert factory.call_count == 2
        compaction_db.get.assert_awaited_once()
        compaction_db.close.assert_awaited_once()
        transport.assert_not_awaited()
        parent_db.rollback.assert_not_awaited()
        parent_db.commit.assert_not_awaited()
        parent_db.flush.assert_not_awaited()
        assert parent_db.is_active and parent_db.in_transaction()
        assert record in parent_db.new
        assert record.title == "Pending parent context"


async def test_compaction_propagates_external_cancellation(monkeypatch, compaction_db) -> None:
    provider = LlmProviderConfig(name="Local", adapter_type="openai-compatible")
    active = ModelConfig(provider_id=provider.id, model_id="agent")
    compaction = ModelConfig(provider_id=provider.id, model_id="compact")
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def complete(_db, **_kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    complete_mock = AsyncMock(side_effect=complete)
    monkeypatch.setattr(ProviderService, "complete", complete_mock)
    on_update = AsyncMock()
    task = asyncio.create_task(AgentRuntime._summarize(
        "USER: Test", 512,
        ai_settings=AiSettings(
            providers=[provider], models=[active, compaction], compaction_model_id=compaction.id,
        ),
        active_model=active, runtime_settings=AgentRuntimeSettings(), on_update=on_update,
    ))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert cancelled.is_set()
    compaction_db.close.assert_awaited_once()
    complete_mock.assert_awaited_once()
    assert complete_mock.await_args.args == (compaction_db,)
    on_update.assert_awaited_once_with("", False)


@pytest.mark.parametrize(("context_window", "safety", "forked"), [(131_072, 4_096, False), (4_096, 0, True)])
async def test_title_generation_falls_back_to_session_model(monkeypatch, context_window, safety, forked) -> None:
    provider = LlmProviderConfig(
        name="Local", adapter_type="openai-compatible", pseudonymization_enabled=False,
        config={"base_url": "http://local.test/v1"},
    )
    agent = ModelConfig(provider_id=provider.id, model_id="agent")
    session_model = ModelConfig(
        provider_id=provider.id, model_id="session", context_window_tokens=context_window,
    )
    settings = AiSettings(
        providers=[provider],
        models=[agent, session_model],
        default_model_id=agent.id,
    )
    session = ChatSession(
        id=uuid4(),
        title=("Kopie: " if forked else "") + "Default Models in Settings einbauen",
        model_config_id=session_model.id,
        title_generation_status=TitleGenerationStatus.RUNNING.value,
    )
    db = AsyncMock(spec=AsyncSession)
    message = Message(id=uuid4(), session_id=session.id, role="user", content="Mach weiter" if forked else session.title)
    db.get.side_effect = [session, message]

    async def load(_db, settings_type):
        return settings if settings_type is AiProviderSettings else AgentRuntimeSettings(safety_reserve_tokens=safety)

    monkeypatch.setattr(SettingsStore, "load", load)
    def _mock_value():
        return nullcontext(db)
    for _mock_target in (mock_agent_runtime, mock_agent_summarization, mock_agent_titles,):
        monkeypatch.setattr(_mock_target, 'AsyncSessionFactory', _mock_value)
    transport = AsyncMock(return_value=ProviderResult(content="Standardmodelle konfigurieren"))
    monkeypatch.setattr(service_module, "chat_completions", transport)
    complete = AsyncMock(wraps=ProviderService.complete)
    monkeypatch.setattr(ProviderService, "complete", complete)
    publish = AsyncMock()
    monkeypatch.setattr(run_events, "publish", publish)
    db.scalar.return_value = session.id
    run_id = uuid4()

    await AgentRuntime._generate_title(
        run_id,
        session.id,
        message.id,
        session.title,
    )

    assert complete.await_args.kwargs["model"].id == session_model.id
    assert complete.await_args.kwargs["messages"][1]["content"] == (
        "Previous conversation title: Default Models in Settings einbauen\n\nMach weiter"
        if forked else "Default Models in Settings einbauen"
    )
    assert complete.await_args.kwargs["max_output_tokens"] == 2_048
    assert complete.await_args.kwargs["safety_reserve_tokens"] == safety
    assert transport.await_args.kwargs["request"].max_output_tokens == 2_048
    statement = db.scalar.await_args.args[0]
    assert "chat_sessions.title" in str(statement)
    assert "Standardmodelle konfigurieren" in statement.compile().params.values()
    assert TitleGenerationStatus.COMPLETED.value in statement.compile().params.values()
    db.commit.assert_awaited_once()
    publish.assert_awaited_once_with(
        run_id,
        {
            "type": "session.title.updated",
            "sessionId": str(session.id),
            "title": "Standardmodelle konfigurieren",
        },
    )


async def test_title_generation_waits_for_agent_run(monkeypatch) -> None:
    release_run = asyncio.Event()
    factory_calls = 0
    db = AsyncMock(spec=AsyncSession)
    db.get.return_value = None

    async def run_worker() -> None:
        await release_run.wait()

    def session_factory():
        nonlocal factory_calls
        factory_calls += 1
        return nullcontext(db)

    _mock_value = session_factory
    for _mock_target in (mock_agent_runtime, mock_agent_summarization, mock_agent_titles,):
        monkeypatch.setattr(_mock_target, 'AsyncSessionFactory', _mock_value)
    run_task = asyncio.create_task(run_worker())
    title_task = asyncio.create_task(
        AgentRuntime._generate_title(
            uuid4(), uuid4(), uuid4(), "Erste Nachricht", after=run_task
        )
    )
    await asyncio.sleep(0)

    assert factory_calls == 0

    release_run.set()
    await title_task
    assert factory_calls == 1


async def test_title_generation_skips_cancelled_agent_run(monkeypatch) -> None:
    session = ChatSession(
        title="Erste Nachricht",
        title_generation_status=TitleGenerationStatus.RUNNING.value,
    )
    db = AsyncMock(spec=AsyncSession)
    db.get.return_value = session
    def _mock_value():
        return nullcontext(db)
    for _mock_target in (mock_agent_runtime, mock_agent_summarization, mock_agent_titles,):
        monkeypatch.setattr(_mock_target, 'AsyncSessionFactory', _mock_value)
    complete = AsyncMock()
    monkeypatch.setattr(ProviderService, "complete", complete)
    run_task = asyncio.create_task(asyncio.sleep(10))
    run_task.cancel()
    await asyncio.gather(run_task, return_exceptions=True)

    await AgentRuntime._generate_title(
        uuid4(), session.id, uuid4(), "Erste Nachricht", after=run_task
    )

    complete.assert_not_awaited()
    assert session.title_generation_status == TitleGenerationStatus.SKIPPED.value
    db.commit.assert_awaited_once()


async def test_title_generation_does_not_overwrite_manual_title(monkeypatch) -> None:
    session = ChatSession(
        title="Manuell gesetzt",
        title_generation_status=TitleGenerationStatus.RUNNING.value,
    )
    db = AsyncMock(spec=AsyncSession)
    db.get.return_value = session
    def _mock_value():
        return nullcontext(db)
    for _mock_target in (mock_agent_runtime, mock_agent_summarization, mock_agent_titles,):
        monkeypatch.setattr(_mock_target, 'AsyncSessionFactory', _mock_value)
    complete = AsyncMock()
    monkeypatch.setattr(ProviderService, "complete", complete)

    await AgentRuntime._generate_title(
        uuid4(),
        session.id,
        uuid4(),
        "Erste Nachricht",
    )

    complete.assert_not_awaited()
    db.execute.assert_not_awaited()
    assert session.title_generation_status == TitleGenerationStatus.SKIPPED.value
    db.commit.assert_awaited_once()


def test_generated_title_is_normalized_and_bounded() -> None:
    assert _generated_session_title('  "Kurzer   Titel"  ') == "Kurzer Titel"
    assert _generated_session_title("x" * 100) == "x" * 80
    assert _generated_session_title("<think>Nur Reasoning</think>") is None


def test_title_repair_budget_uses_provider_signals_and_model_limit() -> None:
    reasoning_only = ProviderResult(
        reasoning=PublicReasoning("Analyse"),
        usage={"output_tokens": 512, "reasoning_tokens": 512},
        finish_reason="length",
    )

    assert repair_title_output_budget(1_024, 16_384, reasoning_only) == 4_096
    assert repair_title_output_budget(1_024, 2_048, reasoning_only) == 2_048
    assert repair_title_output_budget(256, 300, ProviderResult()) == 300
    assert initial_title_output_budget(None) == 2_048
    assert initial_title_output_budget(64) == 64
    assert repair_title_output_budget(2_048, None, reasoning_only) == 4_096
    assert repair_title_output_budget(64, 64, reasoning_only) == 64


@pytest.mark.parametrize(
    ("manual", "discovered", "expected"),
    [(None, None, [2_048, 4_096]), (128, None, [128, 128]), (None, 64, [64, 64]), (128, 64, [64, 64])],
)
async def test_title_generation_repairs_empty_visible_output(
    monkeypatch, manual, discovered, expected,
) -> None:
    provider = LlmProviderConfig(name="Local", adapter_type="openai-compatible")
    model = ModelConfig(
        provider_id=provider.id, model_id="title",
        max_output_tokens=manual, discovered_max_output_tokens=discovered,
    )
    settings = AiSettings(
        providers=[provider], models=[model], title_generation_model_id=model.id
    )
    session = ChatSession(
        id=uuid4(),
        title="Ausgangstitel",
        title_generation_status=TitleGenerationStatus.RUNNING.value,
    )
    db = AsyncMock(spec=AsyncSession)
    message = Message(id=uuid4(), session_id=session.id, role="user", content=session.title)
    db.get.side_effect = [session, message]
    db.scalar.return_value = session.id

    async def load(_db, settings_type):
        return settings if settings_type is AiProviderSettings else AgentRuntimeSettings(safety_reserve_tokens=128)

    complete = AsyncMock(
        side_effect=[
            ProviderResult(
                reasoning=PublicReasoning("Nur Reasoning"),
                usage={"output_tokens": 1_024, "reasoning_tokens": 1_024},
                finish_reason="length",
            ),
            ProviderResult(content="Reparierter Titel", finish_reason="stop"),
        ]
    )
    monkeypatch.setattr(SettingsStore, "load", load)
    def _mock_value():
        return nullcontext(db)
    for _mock_target in (mock_agent_runtime, mock_agent_summarization, mock_agent_titles,):
        monkeypatch.setattr(_mock_target, 'AsyncSessionFactory', _mock_value)
    monkeypatch.setattr(ProviderService, "complete", complete)
    monkeypatch.setattr(run_events, "publish", AsyncMock())

    await AgentRuntime._generate_title(
        uuid4(), session.id, message.id, "Ausgangstitel"
    )

    assert complete.await_count == 2
    assert [call.kwargs["safety_reserve_tokens"] for call in complete.await_args_list] == [128, 128]
    assert [
        call.kwargs["max_output_tokens"] for call in complete.await_args_list
    ] == expected
    repair_prompt = complete.await_args.kwargs["messages"][0]["content"]
    assert "previous attempt" in repair_prompt
    assert "only the final title" in repair_prompt
    statement = db.scalar.await_args.args[0]
    assert "Reparierter Titel" in statement.compile().params.values()


async def test_title_generation_persists_failed_status(monkeypatch) -> None:
    provider = LlmProviderConfig(name="Local", adapter_type="openai-compatible")
    model = ModelConfig(provider_id=provider.id, model_id="title")
    settings = AiSettings(
        providers=[provider], models=[model], title_generation_model_id=model.id
    )
    session = ChatSession(
        id=uuid4(),
        title="Fehlerfall",
        title_generation_status=TitleGenerationStatus.RUNNING.value,
    )
    db = AsyncMock(spec=AsyncSession)
    message = Message(id=uuid4(), session_id=session.id, role="user", content=session.title)
    db.get.side_effect = [session, message]

    async def load(_db, settings_type):
        return settings if settings_type is AiProviderSettings else AgentRuntimeSettings()

    monkeypatch.setattr(SettingsStore, "load", load)
    def _mock_value():
        return nullcontext(db)
    for _mock_target in (mock_agent_runtime, mock_agent_summarization, mock_agent_titles,):
        monkeypatch.setattr(_mock_target, 'AsyncSessionFactory', _mock_value)
    monkeypatch.setattr(
        ProviderService, "complete", AsyncMock(side_effect=RuntimeError("kaputt"))
    )

    await AgentRuntime._generate_title(
        uuid4(), session.id, message.id, "Fehlerfall"
    )

    db.rollback.assert_awaited_once()
    statement = db.execute.await_args.args[0]
    assert TitleGenerationStatus.FAILED.value in statement.compile().params.values()
    db.commit.assert_awaited_once()


def test_title_input_uses_extracted_text_and_bounds_filename_fallback() -> None:
    assert "Quarterly revenue" in title_input(
        "", [("report.pdf", "Quarterly revenue increased")]
    )
    fallback = title_input("", [("a" * 3_000, None), ("photo.png", None)])
    assert fallback.startswith("Attachments: ")
    assert len(fallback) <= 4_000
    assert fallback.endswith("photo.png")
    mixed = title_input(
        "", [("report.pdf", "Quarterly revenue increased"), ("recording.wav", None)]
    )
    assert "Quarterly revenue" in mixed
    assert "recording.wav" in mixed
    long_mixed = title_input(
        "", [("report.pdf", "x" * 5_000), ("recording.wav", None)]
    )
    assert len(long_mixed) <= 4_000
    assert long_mixed.endswith("recording.wav")
    many_filenames = title_input(
        "User question",
        [(f"{index}-{'x' * 990}.txt", None) for index in range(8)],
    )
    assert many_filenames.startswith("User question")
    assert "7-" in many_filenames


@pytest.mark.parametrize(
    ("title_supports_images", "fallback_enabled", "expected_model_id"),
    [
        (True, True, "title"),
        (False, True, "vision"),
        (False, False, "title"),
    ],
)
async def test_title_generation_routes_current_images_in_one_request(
    monkeypatch,
    title_supports_images: bool,
    fallback_enabled: bool,
    expected_model_id: str,
) -> None:
    provider = LlmProviderConfig(name="Public", adapter_type="openai-compatible")
    title_model = ModelConfig(
        provider_id=provider.id,
        model_id="title",
        supports_image_input=title_supports_images,
    )
    vision_model = ModelConfig(
        provider_id=provider.id,
        model_id="vision",
        supports_image_input=True,
    )
    settings = AiSettings(
        providers=[provider],
        models=[title_model, vision_model],
        title_generation_model_id=title_model.id,
        vision_model_id=vision_model.id,
        title_vision_fallback_enabled=fallback_enabled,
    )
    session = ChatSession(
        id=uuid4(),
        user_id=uuid4(),
        title="report.pdf, photo.png",
        title_generation_status=TitleGenerationStatus.RUNNING.value,
    )
    message = Message(
        id=uuid4(),
        session_id=session.id,
        role="user",
        content="",
        trust_level="private",
    )
    document = MessageAttachment(
        id=uuid4(),
        message_id=message.id,
        position=0,
        kind="document",
        mime_type="application/pdf",
        size_bytes=100,
        sha256="a" * 64,
        file_path=f"attachments/{uuid4()}/report.pdf",
        filename="report.pdf",
        processing_status="ready",
    )
    image = MessageAttachment(
        id=uuid4(),
        message_id=message.id,
        position=1,
        kind="image",
        mime_type="image/png",
        size_bytes=100,
        sha256="b" * 64,
        file_path=f"attachments/{uuid4()}/photo.png",
        filename="photo.png",
        processing_status="ready",
    )
    message.attachments = [document, image]
    db = AsyncMock(spec=AsyncSession)
    db.get.side_effect = [session, message]
    db.scalar.return_value = session.id

    async def load(_db, settings_type):
        return settings if settings_type is AiProviderSettings else AgentRuntimeSettings()

    attachment_service = SimpleNamespace(
        read_derived_text=lambda _attachment, _session_id: "Quarterly revenue increased",
        read_image=AsyncMock(return_value=b"verified image"),
    )
    complete = AsyncMock(return_value=ProviderResult(content="Quarterly report"))
    monkeypatch.setattr(SettingsStore, "load", load)
    def _mock_value():
        return nullcontext(db)
    for _mock_target in (mock_agent_runtime, mock_agent_summarization, mock_agent_titles,):
        monkeypatch.setattr(_mock_target, 'AsyncSessionFactory', _mock_value)
    def _mock_value():
        return attachment_service
    for _mock_target in (mock_agent_runtime, mock_agent_loop, mock_agent_titles,):
        monkeypatch.setattr(_mock_target, 'AttachmentService', _mock_value)
    monkeypatch.setattr(ProviderService, "complete", complete)
    monkeypatch.setattr(run_events, "publish", AsyncMock())

    await AgentRuntime._generate_title(
        uuid4(), session.id, message.id, session.title
    )

    assert complete.await_count == 1
    assert complete.await_args.kwargs["model"].model_id == expected_model_id
    assert "Quarterly revenue increased" in complete.await_args.kwargs["messages"][1][
        "content"
    ]
    image_inputs = complete.await_args.kwargs["image_inputs"]
    if title_supports_images or fallback_enabled:
        assert len(image_inputs) == 1
        assert image_inputs[0].trust_level == "private"
        attachment_service.read_image.assert_awaited_once_with(image, session.id, current_only=True)
    else:
        assert image_inputs == []
        attachment_service.read_image.assert_not_awaited()
