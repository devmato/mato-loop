import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend.api.errors import ApiError
from mato_loop_backend.api.routes.memories import (
    DreamRunRequest,
    MemoryAcknowledgementRequest,
    MemoryRequest,
    MemorySettingsRequest,
    MemoryUpdateRequest,
    acknowledge_memories,
    delete_memory,
    dream_run_payload,
    memory_payload,
    put_memory_settings,
    start_dream_runs,
    update_memory,
)
from mato_loop_backend.files.service import RootFileService
from mato_loop_backend.files.storage import SessionStorage
from mato_loop_backend.memory.dreaming import (
    DREAMING_RECONCILIATION_OUTPUT_TOKENS,
    DREAMING_SYSTEM_PROMPT,
    RECONCILIATION_SYSTEM_PROMPT,
    DreamCandidates,
    DreamingContractError,
    DreamingService,
    DreamReconciliation,
    _dreaming_reasoning_effort,
    _extract_candidates,
    _fit_message_batch,
    _memory_read_trust,
    _reconcile_candidates,
    _reconciliation_batches,
    _reconciliation_source,
    _reconciliation_tool,
    _record_candidates_tool,
    _source_character_limit,
    dreaming_availability,
)
from mato_loop_backend.memory.service import (
    MemoryApprovalPayload,
    MemoryApprovalTarget,
    MemoryDuplicateError,
    MemoryMutation,
    MemoryService,
    memory_reference_timestamps,
    normalize_content,
)
from mato_loop_backend.models import (
    ApprovalRequest,
    ChatSession,
    MemoryAudience,
    MemoryCategory,
    MemoryDreamRun,
    MemoryDreamState,
    MemoryDreamTrigger,
    MemoryRecord,
    MemorySourceMessage,
    MemorySourceType,
    Message,
    UserAccount,
)
from mato_loop_backend.permissions.policy import PermissionDecision
from mato_loop_backend.providers.base import ProviderError, ProviderResult, ToolCall
from mato_loop_backend.providers.output_budget import allocate_output_tokens, raw_input_budget
from mato_loop_backend.providers.service import ProviderService
from mato_loop_backend.settings import (
    AgentRuntimeSettings,
    AiSettings,
    GeneralSettings,
    IntegrationSettings,
    MemorySettings,
    ReasoningEffort,
    SettingsStore,
    ToolExecutionPolicy,
    TrustLevel,
    UserProfileSettings,
)
from mato_loop_backend.settings.models import (
    LlmProviderConfig,
    ModelConfig,
    effective_model_pseudonymization,
    effective_model_trust,
)
from mato_loop_backend.tools.arguments import MemoryWriteArguments
from mato_loop_backend.tools.catalog import tool_registry
from mato_loop_backend.tools.context import ToolContext


def _root_files(tmp_path, session: ChatSession) -> RootFileService:
    storage = SessionStorage(tmp_path / "sessions")
    storage.provision(session.id)
    return RootFileService(
        session.id,
        session_storage=storage,
    )


def test_trust_defaults_and_inheritance() -> None:
    provider = LlmProviderConfig(name="Public", adapter_type="openai-compatible")
    model = ModelConfig(provider_id=uuid4(), model_id="public")

    assert provider.trust_level == TrustLevel.PUBLIC
    assert model.trust_level is None
    assert effective_model_trust(provider, model) == TrustLevel.PUBLIC
    provider.trust_level = TrustLevel.PRIVATE
    assert effective_model_trust(provider, model) == TrustLevel.PRIVATE
    model.trust_level = TrustLevel.PUBLIC
    assert effective_model_trust(provider, model) == TrustLevel.PUBLIC
    assert effective_model_pseudonymization(provider, model) is True
    model.trust_level = TrustLevel.PRIVATE
    assert effective_model_pseudonymization(provider, model) is True


@pytest.mark.parametrize(
    ("input_trust", "model_trust", "expected"),
    [
        (TrustLevel.PUBLIC, TrustLevel.PUBLIC, TrustLevel.PUBLIC),
        (TrustLevel.PUBLIC, TrustLevel.PRIVATE, TrustLevel.PUBLIC),
        (TrustLevel.PRIVATE, TrustLevel.PUBLIC, TrustLevel.PUBLIC),
        (TrustLevel.PRIVATE, TrustLevel.PRIVATE, TrustLevel.PRIVATE),
    ],
)
def test_dreaming_memory_reads_use_intersection_of_input_and_model_trust(
    input_trust: TrustLevel,
    model_trust: TrustLevel,
    expected: TrustLevel,
) -> None:
    assert _memory_read_trust(input_trust, model_trust) == expected


def test_dreaming_batch_requires_user_and_assistant_capacity() -> None:
    with pytest.raises(ValidationError):
        MemorySettingsRequest(
            enabled=True,
            automatic_dreaming_enabled=True,
            dreaming_interval_minutes=60,
            dreaming_idle_minutes=10,
            dreaming_max_batch_messages=1,
            dreaming_max_candidates=10,
        )

    with pytest.raises(ValidationError):
        MemorySettingsRequest(enabled=None)
    with pytest.raises(ValidationError):
        MemorySettingsRequest(dreamingMaxPinnedMemories=10)
    with pytest.raises(ValidationError):
        MemorySettingsRequest(automaticMemorySnippetLimit=6)
    with pytest.raises(ValidationError):
        MemorySettingsRequest(automaticContextMaxTokens=127)


def test_automatic_dreaming_is_independent_from_manual_dreaming() -> None:
    provider = LlmProviderConfig(name="Local", adapter_type="openai-compatible")
    model = ModelConfig(provider_id=provider.id, model_id="model")
    ai = AiSettings(
        providers=[provider], models=[model], default_model_id=model.id
    )
    settings = MemorySettings(enabled=True, automatic_dreaming_enabled=False)

    assert dreaming_availability(settings, ai, automatic=True) == (
        False,
        "memory.dreamingDisabled",
    )
    assert dreaming_availability(settings, ai, automatic=False) == (True, None)

    settings.enabled = False
    assert dreaming_availability(settings, ai, automatic=False) == (
        False,
        "memory.disabled",
    )


def test_dreaming_disables_reasoning_only_when_the_model_supports_it() -> None:
    assert _dreaming_reasoning_effort(
        ModelConfig(provider_id=uuid4(), model_id="Qwen/Qwen3.8-27B-Q4_K_M.gguf")
    ) == ReasoningEffort.NONE
    assert _dreaming_reasoning_effort(
        ModelConfig(
            provider_id=uuid4(),
            model_id="reasoning-only",
            discovery_metadata={
                "reasoning_efforts": ["medium", "high"],
                "default_reasoning_effort": "medium",
            },
        )
    ) is None


async def test_provider_rejects_model_from_another_provider_before_database_access() -> None:
    db = AsyncMock(spec=AsyncSession)
    provider = LlmProviderConfig(
        name="Provider A",
        adapter_type="openai-compatible",
        config={"base_url": "http://local.test/v1"},
    )
    model = ModelConfig(provider_id=uuid4(), model_id="foreign")

    with pytest.raises(ProviderError) as exc_info:
        await ProviderService.complete(
            db,
            provider=provider,
            model=model,
            messages=[],
            tools=[],
        )

    assert exc_info.value.public_code == "provider.configurationInvalid"
    db.get.assert_not_awaited()


@pytest.mark.usefixtures("mock_db_settings")
async def test_provider_allows_chat_text_for_public_pseudonymized_request(
    monkeypatch,
) -> None:
    db = AsyncMock(spec=AsyncSession)
    provider = LlmProviderConfig(
        name="Local",
        adapter_type="openai-compatible",
        config={"base_url": "http://local.test/v1"},
    )
    model = ModelConfig(provider_id=provider.id, model_id="public")

    complete = AsyncMock(return_value=ProviderResult(content="OK"))
    monkeypatch.setattr(
        "mato_loop_backend.providers.service.chat_completions", complete
    )

    result = await ProviderService.complete(
        db,
        provider=provider,
        model=model,
        messages=[],
        tools=[],
    )

    assert result.content == "OK"
    db.get.assert_not_awaited()


def test_memory_normalization_is_whitespace_and_case_stable() -> None:
    first, first_hash = normalize_content("  Ein\n  FAKT  ")
    second, second_hash = normalize_content("ein fakt")

    assert first == "ein fakt"
    assert first_hash == second_hash


def test_memory_schema_has_no_pin_kind_contract() -> None:
    request = MemoryRequest(content="Ich heiße Lara.", category="identity")
    assert request.audience == MemoryAudience.BOTH
    with pytest.raises(ValidationError):
        MemoryRequest(
            content="Ich heiße Lara.",
            category="identity",
            pinKind="preferred_name",
        )
    with pytest.raises(ValidationError):
        MemoryUpdateRequest(
            content="Ich heiße Lara.",
            category="identity",
            audience="both",
            expectedRevision=1,
            pinned=True,
            pinKind="preferred_name",
        )


async def test_memory_crud_and_dedupe_queries_bind_each_owner() -> None:
    first_owner = uuid4()
    second_owner = uuid4()
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = None

    await MemoryService._duplicate_by_hash(
        db, owner_id=first_owner, content_hash="same"
    )
    await MemoryService._duplicate_by_hash(
        db, owner_id=second_owner, content_hash="same"
    )
    await MemoryService.get(
        db, owner_id=first_owner, memory_id=uuid4()
    )
    await MemoryService.get(
        db, owner_id=second_owner, memory_id=uuid4()
    )

    statements = [call.args[0] for call in db.scalar.await_args_list]
    assert first_owner in statements[0].compile().params.values()
    assert second_owner in statements[1].compile().params.values()
    assert first_owner in statements[2].compile().params.values()
    assert second_owner in statements[3].compile().params.values()
    assert all("memories.user_id" in str(statement) for statement in statements)


async def test_manual_memory_mutations_reject_stale_revisions() -> None:
    user = UserAccount(id=uuid4(), username="test", password_hash="hash")
    item = MemoryRecord(
        id=uuid4(),
        user_id=user.id,
        content="Aktuell",
        category="fact",
        audience="both",
        pinned=False,
        revision=3,
    )
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = item
    request = MemoryUpdateRequest(
        content="Veraltet",
        category="fact",
        audience="both",
        pinned=False,
        expectedRevision=2,
    )

    with pytest.raises(ApiError) as update_error:
        await update_memory(item.id, request, user, db)
    with pytest.raises(ApiError) as delete_error:
        await delete_memory(item.id, user, db, expected_revision=2)

    assert update_error.value.code == "memory.changed"
    assert delete_error.value.code == "memory.changed"
    assert update_error.value.params == {"currentRevision": 3}
    assert delete_error.value.params == {"currentRevision": 3}
    db.delete.assert_not_awaited()
    db.commit.assert_not_awaited()


def test_dream_run_payload_includes_removed_count() -> None:
    run = MemoryDreamRun(
        id=uuid4(),
        session_id=uuid4(),
        source_from_message_id=uuid4(),
        source_to_message_id=uuid4(),
        removed_memory_count=2,
    )
    run.source_session = None

    assert dream_run_payload(run)["removedMemories"] == 2


async def test_same_content_is_created_once_per_owner(monkeypatch) -> None:
    first_owner = uuid4()
    second_owner = uuid4()
    db = AsyncMock(spec=AsyncSession)

    @asynccontextmanager
    async def nested():
        yield

    db.begin_nested = Mock(side_effect=nested)
    duplicate = AsyncMock(return_value=None)
    create = AsyncMock(
        side_effect=lambda _db, *, owner_id, **_kwargs: MemoryRecord(
            id=uuid4(), user_id=owner_id
        )
    )
    monkeypatch.setattr(MemoryService, "_duplicate_by_hash", duplicate)
    monkeypatch.setattr(MemoryService, "_create", create)

    for owner_id in (first_owner, second_owner):
        await MemoryService.create_manual(
            db,
            owner_id=owner_id,
            content="Shared normalized content",
            category=MemoryCategory.FACT,
            audience=MemoryAudience.BOTH,
        )

    assert [call.kwargs["owner_id"] for call in duplicate.await_args_list] == [
        first_owner,
        second_owner,
    ]
    assert [call.kwargs["owner_id"] for call in create.await_args_list] == [
        first_owner,
        second_owner,
    ]


async def test_memory_mutations_reject_another_owner() -> None:
    owner_id = uuid4()
    item = MemoryRecord(id=uuid4(), user_id=owner_id, pinned=False, revision=1)
    db = AsyncMock(spec=AsyncSession)

    with pytest.raises(ValueError, match="owner mismatch"):
        await MemoryService.delete(db, item, owner_id=uuid4())
    with pytest.raises(ValueError, match="owner mismatch"):
        await MemoryService.set_pinned(
            db, item, owner_id=uuid4(), pinned=True
        )

    db.delete.assert_not_awaited()
    db.flush.assert_not_awaited()


async def test_memory_evidence_rejects_foreign_owner_message() -> None:
    owner_id = uuid4()
    owned_message_id = uuid4()
    foreign_message_id = uuid4()
    rows = Mock()
    rows.all.return_value = [owned_message_id]
    db = AsyncMock(spec=AsyncSession)
    db.scalars.return_value = rows

    with pytest.raises(ValueError, match="evidence owner mismatch"):
        await MemoryService._validate_evidence_owner(
            db,
            owner_id=owner_id,
            message_ids=[owned_message_id, foreign_message_id],
        )

    statement = db.scalars.await_args.args[0]
    assert owner_id in statement.compile().params.values()
    assert "chat_sessions.user_id" in str(statement)
    db.execute.assert_not_awaited()


def test_memory_reference_timestamps_distinguish_source_and_record_lifecycle() -> None:
    source_at = datetime(2026, 7, 20, 10, tzinfo=UTC)
    created_at = datetime(2026, 7, 20, 11, tzinfo=UTC)
    updated_at = datetime(2026, 7, 21, 12, tzinfo=UTC)
    item = MemoryRecord(created_at=created_at, updated_at=updated_at)
    item.source_message = Message(created_at=source_at)

    assert memory_reference_timestamps(item) == {
        "sourceMessageAt": source_at.isoformat(),
        "memoryCreatedAt": created_at.isoformat(),
        "memoryUpdatedAt": updated_at.isoformat(),
    }


async def test_dreaming_create_uses_first_evidence_as_current_source(monkeypatch) -> None:
    owner_id = uuid4()
    evidence_id = uuid4()
    item = MemoryRecord(id=uuid4(), user_id=owner_id, content="Dauerhafter Fakt")
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = None

    @asynccontextmanager
    async def nested():
        yield

    db.begin_nested = Mock(return_value=nested())
    create = AsyncMock(return_value=item)
    monkeypatch.setattr(MemoryService, "_create", create)

    result = await MemoryService.create_dreaming(
        db,
        owner_id=owner_id,
        content=item.content,
        category=MemoryCategory.FACT,
        trust_level=TrustLevel.PRIVATE,
        session_id=uuid4(),
        run_id=uuid4(),
        evidence_message_ids=[evidence_id],
    )

    assert result.created is True
    assert create.await_args.kwargs["message_id"] == evidence_id
    assert "pinned" not in create.await_args.kwargs


async def test_new_dreaming_memory_is_unpinned(monkeypatch) -> None:
    monkeypatch.setattr(
        "mato_loop_backend.memory.service.MemoryIndexService.enqueue", AsyncMock()
    )
    db = AsyncMock(spec=AsyncSession)

    item = await MemoryService._create(
        db,
        owner_id=uuid4(),
        content="Dauerhafter Fakt",
        category=MemoryCategory.FACT,
        source_type=MemorySourceType.DREAMING,
        audience=MemoryAudience.BOTH,
    )

    assert item.pinned is False


async def test_dreaming_remove_requires_user_evidence_and_hard_deletes(
    monkeypatch,
) -> None:
    owner_id = uuid4()
    item = MemoryRecord(
        id=uuid4(),
        user_id=owner_id,
        source_type="manual",
        pinned=True,
        audience="both",
        category="fact",
        revision=4,
        content="Der Nutzer lebt in Berlin.",
        normalized_content_hash="hash",
    )
    snapshot = MemoryApprovalTarget(
        id=item.id,
        revision=4,
        content=item.content,
        category="fact",
        audience="both",
        pinned=True,
        evidenceHash="0" * 64,
    )
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = item
    monkeypatch.setattr(MemoryService, "_validate_source_references", AsyncMock())
    evidence = AsyncMock(return_value=True)
    monkeypatch.setattr(MemoryService, "_valid_explicit_user_evidence", evidence)
    monkeypatch.setattr(MemoryService, "_snapshot_matches", AsyncMock(return_value=True))
    evidence_id = uuid4()
    session_id = uuid4()

    mutation = await MemoryService.remove_dreaming(
        db,
        owner_id=owner_id,
        snapshot=snapshot,
        trust_level=TrustLevel.PUBLIC,
        session_id=session_id,
        dream_run_id=uuid4(),
        evidence_message_ids=[evidence_id],
    )

    assert mutation.item is item
    db.delete.assert_awaited_once_with(item)
    evidence.assert_awaited_once_with(
        db,
        owner_id=owner_id,
        source_session_id=session_id,
        evidence_message_ids=[evidence_id],
    )


async def test_private_dreaming_candidate_upgrades_public_duplicate(monkeypatch) -> None:
    owner_id = uuid4()
    existing = MemoryRecord(
        id=uuid4(),
        user_id=owner_id,
        source_type="dreaming",
        pinned=False,
        audience="both",
        category="fact",
        content="Dauerhafter Fakt",
    )
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = existing
    linked = Mock()
    linked.all.return_value = []
    db.scalars.return_value = linked
    evidence_id = uuid4()
    add_evidence = AsyncMock()
    monkeypatch.setattr(MemoryService, "_add_evidence", add_evidence)

    created = await MemoryService.create_dreaming(
        db,
        owner_id=owner_id,
        content="Dauerhafter Fakt",
        category=MemoryCategory.FACT,
        trust_level=TrustLevel.PRIVATE,
        session_id=uuid4(),
        run_id=uuid4(),
        evidence_message_ids=[evidence_id],
    )

    assert created.duplicate is True
    assert existing.audience == "private_only"
    add_evidence.assert_awaited_once_with(
        db,
        owner_id=owner_id,
        memory_id=existing.id,
        message_ids=[evidence_id],
    )


async def test_manual_memory_rejects_normalized_duplicate() -> None:
    owner_id = uuid4()
    existing = MemoryRecord(
        id=uuid4(),
        user_id=owner_id,
        source_type="manual",
        pinned=False,
        audience="both",
        category="fact",
        content="Ein Fakt",
    )
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = existing

    with pytest.raises(MemoryDuplicateError):
        await MemoryService.create_manual(
            db,
            owner_id=owner_id,
            content="  EIN\nFAKT ",
            category=MemoryCategory.FACT,
            audience=MemoryAudience.BOTH,
        )

    db.add.assert_not_called()


async def test_manual_memory_update_loads_target_for_update() -> None:
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = None

    await MemoryService.get(
        db, owner_id=uuid4(), memory_id=uuid4(), for_update=True
    )

    statement = db.scalar.await_args.args[0]
    assert "FOR UPDATE" in str(statement)
    assert statement.get_execution_options()["populate_existing"] is True


def test_memory_write_arguments_require_explicit_update_target() -> None:
    valid = MemoryWriteArguments.model_validate(
        {
            "action": "update",
            "memory_id": str(uuid4()),
            "expected_revision": 3,
            "content": "Aktualisierter Fakt",
            "category": "fact",
            "pin": False,
        }
    )

    assert valid.category == MemoryCategory.FACT
    assert valid.pin is False
    with pytest.raises(ValidationError):
        MemoryWriteArguments.model_validate(
            {"content": "Der Nutzer kommt aus Ascholding.", "category": "identity"}
        )
    create = MemoryWriteArguments.model_validate(
        {
            "action": "create",
            "memory_id": str(uuid4()),
            "expected_revision": 7,
            "content": "Der Nutzer kommt aus Ascholding.",
            "category": "identity",
            "pin": False,
        }
    )
    assert create.memory_id is None
    assert create.expected_revision is None
    assert MemoryWriteArguments.model_validate(
        {
            "action": "create",
            "content": "Kurze Antworten",
            "category": "preference",
            "pin": True,
        }
    ).pin is True
    with pytest.raises(ValidationError):
        MemoryWriteArguments.model_validate(
            {"action": "update", "content": "Fakt", "category": "fact"}
        )
    forget = MemoryWriteArguments.model_validate(
        {
            "action": "forget",
            "memory_id": str(uuid4()),
            "expected_revision": 2,
            "pin": False,
        }
    )
    assert forget.content is None
    assert forget.category is None
    with pytest.raises(ValidationError):
        MemoryWriteArguments.model_validate(
            {"action": "forget", "memory_id": str(uuid4())}
        )
    with pytest.raises(ValidationError):
        MemoryWriteArguments.model_validate(
            {
                "action": "forget",
                "memory_id": str(uuid4()),
                "expected_revision": 2,
                "pin": True,
            }
        )


async def test_private_tool_update_upgrades_trust_and_adds_evidence(monkeypatch) -> None:
    owner_id = uuid4()
    item = MemoryRecord(
        id=uuid4(),
        user_id=owner_id,
        source_type="manual",
        pinned=False,
        audience="both",
        category="fact",
        content="Alter Fakt",
        revision=3,
    )
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = item
    update_memory = AsyncMock(return_value=item)
    monkeypatch.setattr(MemoryService, "update", update_memory)
    monkeypatch.setattr(MemoryService, "_validate_source_references", AsyncMock())
    evidence_id = uuid4()

    result = await MemoryService.update_tool(
        db,
        owner_id=owner_id,
        memory_id=item.id,
        expected_revision=3,
        content="Neuer Fakt",
        category=MemoryCategory.FACT,
        readable_by=TrustLevel.PUBLIC,
        write_trust_level=TrustLevel.PRIVATE,
        session_id=uuid4(),
        evidence_message_id=evidence_id,
        run_id=uuid4(),
        tool_call_id="memory",
    )

    assert result.updated is True
    assert (
        update_memory.await_args.kwargs["audience"]
        == MemoryAudience.PRIVATE_ONLY
    )
    assert update_memory.await_args.kwargs["evidence_message_ids"] == [evidence_id]


async def test_tool_update_rejects_stale_memory_snapshot(monkeypatch) -> None:
    owner_id = uuid4()
    item = MemoryRecord(
        id=uuid4(),
        user_id=owner_id,
        source_type="manual",
        pinned=False,
        audience="both",
        category="fact",
        content="Inzwischen geändert",
        revision=4,
    )
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = item
    update_memory = AsyncMock(return_value=item)
    monkeypatch.setattr(MemoryService, "update", update_memory)
    monkeypatch.setattr(MemoryService, "_validate_source_references", AsyncMock())

    result = await MemoryService.update_tool(
        db,
        owner_id=owner_id,
        memory_id=item.id,
        expected_revision=3,
        content="Veraltetes Update",
        category=MemoryCategory.FACT,
        readable_by=TrustLevel.PUBLIC,
        write_trust_level=TrustLevel.PUBLIC,
        session_id=uuid4(),
        evidence_message_id=uuid4(),
        run_id=uuid4(),
        tool_call_id="memory",
    )

    assert result.conflict is True
    update_memory.assert_not_awaited()


async def test_tool_update_merges_exact_duplicate_and_preserves_evidence(
    monkeypatch,
) -> None:
    owner_id = uuid4()
    source_message_id = uuid4()
    linked_evidence_id = uuid4()
    current_evidence_id = uuid4()
    stale = MemoryRecord(
        id=uuid4(),
        user_id=owner_id,
        source_message_id=source_message_id,
        source_type="dreaming",
        pinned=False,
        audience="private_only",
        category="fact",
        content="Der Benutzer kommt aus Aschodling.",
        revision=1,
    )
    canonical = MemoryRecord(
        id=uuid4(),
        user_id=owner_id,
        source_type="tool_call",
        pinned=False,
        audience="both",
        category="context",
        content="Der Benutzer kommt aus Wolfratshausen.",
        revision=2,
    )
    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = [stale, canonical]
    linked = Mock()
    linked.all.return_value = [linked_evidence_id]
    db.scalars.return_value = linked
    update_memory = AsyncMock(side_effect=MemoryDuplicateError)
    monkeypatch.setattr(MemoryService, "update", update_memory)
    monkeypatch.setattr(MemoryService, "_validate_source_references", AsyncMock())
    add_evidence = AsyncMock()
    monkeypatch.setattr(MemoryService, "_add_evidence", add_evidence)

    result = await MemoryService.update_tool(
        db,
        owner_id=owner_id,
        memory_id=stale.id,
        expected_revision=1,
        content=canonical.content,
        category=MemoryCategory.FACT,
        readable_by=TrustLevel.PRIVATE,
        write_trust_level=TrustLevel.PRIVATE,
        session_id=uuid4(),
        evidence_message_id=current_evidence_id,
        run_id=uuid4(),
        tool_call_id="memory",
    )

    assert result.item is canonical
    assert result.updated is True
    assert result.duplicate is False
    assert canonical.audience == MemoryAudience.PRIVATE_ONLY.value
    assert canonical.revision == 3
    assert canonical.source_type == "tool_call"
    assert canonical.source_message_id == current_evidence_id
    assert set(add_evidence.await_args.kwargs["message_ids"]) == {
        linked_evidence_id,
        source_message_id,
        current_evidence_id,
    }
    assert add_evidence.await_args.kwargs["owner_id"] == owner_id
    assert add_evidence.await_args.kwargs["memory_id"] == canonical.id
    db.delete.assert_awaited_once_with(stale)


async def test_memory_write_reports_duplicate_without_storing(monkeypatch, tmp_path) -> None:
    duplicate = MemoryRecord(
        id=uuid4(),
        source_type="manual",
        pinned=False,
        audience="both",
        category="preference",
        content="Kurze Antworten",
    )
    create_tool = AsyncMock(
        return_value=MemoryMutation(duplicate, duplicate=True)
    )
    monkeypatch.setattr(MemoryService, "create_tool", create_tool)
    set_pinned = AsyncMock(
        side_effect=lambda _db, item, **_kwargs: setattr(item, "pinned", True) or item
    )
    monkeypatch.setattr(MemoryService, "set_pinned", set_pinned)
    call = ToolCall(
        id="memory",
        name="memory_write",
        arguments={
            "_reason": "Die Erinnerung soll dauerhaft gespeichert werden.",
            "action": "create",
            "content": "Kurze Antworten",
            "category": "preference",
            "pin": True,
        },
    )
    session = ChatSession(id=uuid4(), title="Test")
    context = ToolContext(
        db=AsyncMock(spec=AsyncSession),
        run_id=uuid4(),
        session=session,
        tool_call=call,
        root_files=_root_files(tmp_path, session),
        integration_settings=IntegrationSettings(),
        ask_handler=AsyncMock(return_value=PermissionDecision.ALLOW),
        assistant_message_id=uuid4(),
        memory_read_enabled=True,
        memory_write_enabled=True,
    )
    monkeypatch.setattr(
        SettingsStore, "load", AsyncMock(side_effect=lambda _db, kind, **kwargs: kind())
    )

    result = await tool_registry.execute(call, context)

    assert json.loads(result.content) == {
        "stored": False,
        "updated": False,
        "duplicate": True,
        "pinned": True,
    }
    context.db.refresh.assert_awaited_once_with(session, ["trust_level"], with_for_update=True)
    set_pinned.assert_awaited_once()


def test_memory_tools_are_gated_independently_from_workspace_write_mode() -> None:
    without_memory = tool_registry.schemas(IntegrationSettings())
    with_read_memory = tool_registry.schemas(
        IntegrationSettings(), memory_enabled=True
    )
    with_full_memory = tool_registry.schemas(
        IntegrationSettings(), memory_enabled=True, memory_write_enabled=True
    )

    assert not {"memory_write", "memory_browse"} & {
        item["name"] for item in without_memory
    }
    assert {item["name"] for item in with_read_memory} & {
        "memory_write", "memory_browse"
    } == {"memory_browse"}
    assert {"memory_write", "memory_browse", "context_search"}.issubset(
        {item["name"] for item in with_full_memory}
    )


async def test_public_model_can_read_another_session_after_approval(
    tmp_path,
) -> None:
    source = ChatSession(
        id=uuid4(),
        user_id=uuid4(),
        title="Public",
    )
    target = ChatSession(
        id=uuid4(),
        user_id=source.user_id,
        title="Privat",
    )
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = target
    records = Mock()
    records.all.return_value = []
    db.scalars.return_value = records
    call = ToolCall(
        id="read", name="session_read", arguments={"session_id": str(target.id)}
    )
    context = ToolContext(
        db=db,
        run_id=uuid4(),
        session=source,
        tool_call=call,
        root_files=_root_files(tmp_path, source),
        integration_settings=IntegrationSettings(),
        ask_handler=AsyncMock(return_value=PermissionDecision.ALLOW),
    )

    result = await tool_registry.execute(call, context)

    assert json.loads(result.content)["session"]["id"] == str(target.id)
    db.scalars.assert_awaited_once()


def test_memory_payload_contains_camel_case_provenance() -> None:
    message_id = uuid4()
    seen_at = datetime.now(UTC)
    session = ChatSession(id=uuid4(), title="Quelle")
    item = MemoryRecord(
        id=uuid4(),
        source_type="dreaming",
        pinned=True,
        audience="private_only",
        category="fact",
        content="Fakt",
        seen_at=seen_at,
    )
    item.source_session = session
    item.source_messages = [
        MemorySourceMessage(memory_id=item.id, message_id=message_id)
    ]

    payload = memory_payload(item)

    assert payload["sourceType"] == "dreaming"
    assert payload["pinned"] is True
    assert payload["audience"] == "private_only"
    assert payload["category"] == "fact"
    assert payload["seenAt"] == seen_at.isoformat()
    assert payload["sourceSession"] == {"id": str(session.id), "title": "Quelle"}
    assert payload["sourceMessageIds"] == [str(message_id)]


def test_memory_payload_uses_dream_run_provenance() -> None:
    dream_run_id = uuid4()
    item = MemoryRecord(
        id=uuid4(),
        source_type="dreaming",
        pinned=False,
        audience="both",
        category="fact",
        content="Fakt",
        source_dream_run_id=dream_run_id,
    )
    item.source_session = None
    item.source_messages = []

    assert memory_payload(item)["sourceRunId"] == str(dream_run_id)


async def test_memory_acknowledgement_marks_exact_ids_and_commits(monkeypatch) -> None:
    user = UserAccount(id=uuid4(), username="test", password_hash="hash")
    memory_ids = [uuid4(), uuid4()]
    mark_seen = AsyncMock(return_value=2)
    monkeypatch.setattr(MemoryService, "mark_seen", mark_seen)
    db = AsyncMock(spec=AsyncSession)

    result = await acknowledge_memories(
        MemoryAcknowledgementRequest(memoryIds=memory_ids), user, db
    )

    assert result == {"acknowledged": 2}
    mark_seen.assert_awaited_once_with(
        db, owner_id=user.id, memory_ids=memory_ids
    )
    db.commit.assert_awaited_once()


async def test_mark_seen_updates_only_unseen_memories_without_touching_updated_at() -> None:
    owner_id = uuid4()
    memory_ids = [uuid4(), uuid4()]
    rows = Mock()
    rows.all.return_value = memory_ids
    db = AsyncMock(spec=AsyncSession)
    db.scalars.return_value = rows

    acknowledged = await MemoryService.mark_seen(
        db,
        owner_id=owner_id,
        memory_ids=[memory_ids[0], memory_ids[1], memory_ids[0]],
    )

    statement = db.scalars.await_args.args[0]
    sql = str(statement)
    assert acknowledged == 2
    assert "memories.seen_at IS NULL" in sql
    assert "memories.user_id" in sql
    assert "updated_at=memories.updated_at" in sql


async def test_memory_settings_update_locks_saves_and_commits(monkeypatch) -> None:
    settings = MemorySettings()
    load = AsyncMock(return_value=settings)
    load_ai = AsyncMock(return_value=AiSettings())
    save = AsyncMock()
    monkeypatch.setattr(SettingsStore, "load", load)
    monkeypatch.setattr(SettingsStore, "save", save)
    monkeypatch.setattr(
        "mato_loop_backend.api.routes.memories.load_effective_ai", load_ai
    )
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = 4
    payload = MemorySettingsRequest(
        enabled=True,
        automatic_dreaming_enabled=True,
        dreaming_instructions="Nur bestätigte Präferenzen",
        dreaming_interval_minutes=30,
        dreaming_idle_minutes=10,
        dreaming_max_batch_messages=25,
        dreaming_max_candidates=5,
        dreaming_update_policy="ask",
        dreaming_remove_policy="auto",
        automatic_memory_snippet_limit=3,
        automatic_session_snippet_limit=4,
        recent_session_snippet_limit=5,
        automatic_context_max_tokens=1_536,
    )

    user = UserAccount(id=uuid4(), username="test", password_hash="hash")
    response = await put_memory_settings(payload, user, db)

    assert load.await_args_list[0].kwargs == {
        "user_id": user.id,
        "for_update": True,
    }
    load_ai.assert_awaited_once_with(db, user.id)
    assert settings.dreaming_interval_minutes == 30
    assert settings.dreaming_update_policy == ToolExecutionPolicy.ASK
    save.assert_awaited_once_with(db, settings, user_id=user.id)
    db.commit.assert_awaited_once()
    assert response["effectiveAutomaticDreamingEnabled"] is False
    assert response["manualDreamingAvailable"] is False
    assert response["dreamingUpdatePolicy"] == "ask"
    assert response["dreamingRemovePolicy"] == "auto"
    assert "dreamingMaxPinnedMemories" not in response
    assert response["automaticMemorySnippetLimit"] == 3
    assert response["automaticSessionSnippetLimit"] == 4
    assert response["recentSessionSnippetLimit"] == 5
    assert response["automaticContextMaxTokens"] == 1_536
    assert "pinnedMemoryCount" not in response


def test_memory_policy_request_accepts_camel_case_contract() -> None:
    payload = MemorySettingsRequest.model_validate(
        {
            "dreamingUpdatePolicy": "auto",
            "dreamingRemovePolicy": "ask",
            "automaticMemorySnippetLimit": 2,
            "automaticSessionSnippetLimit": 1,
            "recentSessionSnippetLimit": 2,
            "automaticContextMaxTokens": 1024,
        }
    )

    assert payload.dreaming_update_policy == ToolExecutionPolicy.AUTO
    assert payload.dreaming_remove_policy == ToolExecutionPolicy.ASK
    assert payload.automatic_memory_snippet_limit == 2
    assert payload.automatic_session_snippet_limit == 1
    assert payload.recent_session_snippet_limit == 2
    assert payload.automatic_context_max_tokens == 1_024


async def test_dreaming_restart_requeues_running_without_own_commit() -> None:
    db = AsyncMock(spec=AsyncSession)

    await DreamingService.recover(db)

    assert db.execute.await_count == 2
    parameters = [
        list(call.args[0].compile().params.values())
        for call in db.execute.await_args_list
    ]
    assert any("running" in params and "queued" in params for params in parameters)
    assert any("running" in params and "failed" in params for params in parameters)
    assert all("memory.backendRestarted" in params for params in parameters)
    db.commit.assert_not_awaited()


async def test_dreaming_source_query_excludes_runtime_error_messages() -> None:
    rows = Mock()
    rows.all.return_value = []
    db = AsyncMock(spec=AsyncSession)
    db.scalars.return_value = rows

    await DreamingService._next_messages(db, uuid4(), None, 20)

    statement = db.scalars.await_args.args[0]
    assert "messages.error_code IS NULL" in str(statement)


async def test_dreaming_queue_queries_exclude_automation_sessions() -> None:
    rows = Mock()
    rows.all.return_value = []
    db = AsyncMock(spec=AsyncSession)
    db.scalars.return_value = rows

    assert await DreamingService.enqueue(
        db, trigger=MemoryDreamTrigger.MANUAL, user_id=uuid4()
    ) == []

    enqueue_sql = str(db.scalars.await_args.args[0])
    assert "chat_sessions.memory_mode" in enqueue_sql
    assert "chat_sessions.session_type" in enqueue_sql

    blocked_rows = Mock()
    blocked_rows.all.return_value = []
    db.execute.return_value = blocked_rows
    await DreamingService.requeue_compatible_blocked(db)

    requeue_sql = str(db.execute.await_args.args[0])
    assert "chat_sessions.session_type" in requeue_sql


@pytest.mark.parametrize("safety", [0, 4_096])
async def test_dreaming_queue_fits_sources_using_loaded_runtime_safety(monkeypatch, safety):
    session = ChatSession(id=uuid4(), user_id=uuid4(), memory_mode="full", session_type="chat")
    provider = LlmProviderConfig(name="Local", adapter_type="openai-compatible")
    model = ModelConfig(provider_id=provider.id, model_id="model", context_window_tokens=13_000)
    ai = AiSettings(providers=[provider], models=[model], default_model_id=model.id)
    messages = [
        Message(id=uuid4(), role="user", content="x" * 10_000, created_at=datetime.now(UTC)),
        Message(id=uuid4(), role="assistant", content="Answer", created_at=datetime.now(UTC)),
    ]
    db = AsyncMock(spec=AsyncSession)
    db.scalars.return_value = Mock(all=Mock(return_value=[session]))
    db.scalar.side_effect = [None, None, MemoryDreamState(session_id=session.id), None]
    monkeypatch.setattr(DreamingService, "_next_messages", AsyncMock(return_value=messages))
    monkeypatch.setattr(
        "mato_loop_backend.memory.dreaming.load_effective_ai",
        AsyncMock(return_value=ai),
    )
    load = AsyncMock(side_effect=lambda _db, kind, **_kwargs: {
        MemorySettings: MemorySettings(),
        AgentRuntimeSettings: AgentRuntimeSettings(safety_reserve_tokens=safety),
    }[kind])
    monkeypatch.setattr(SettingsStore, "load", load)

    runs = await DreamingService.enqueue(db, trigger=MemoryDreamTrigger.MANUAL)

    assert len(runs) == (1 if safety == 0 else 0)
    assert any(call.args[1] is AgentRuntimeSettings for call in load.await_args_list)
    if runs:
        assert runs[0].source_to_message_id == messages[-1].id
        assert runs[0].source_message_count == 2


async def test_stale_automation_dreaming_run_is_cancelled_before_model_work(
    monkeypatch,
) -> None:
    session = ChatSession(
        id=uuid4(), user_id=uuid4(), session_type="automation", memory_mode="full"
    )
    run = MemoryDreamRun(
        id=uuid4(),
        session_id=session.id,
        status="running",
        trigger="manual",
        next_attempt_at=datetime.now(UTC),
    )
    monkeypatch.setattr(
        DreamingService, "_cancel_if_inactive", AsyncMock(return_value=False)
    )
    load = AsyncMock()
    monkeypatch.setattr(SettingsStore, "load", load)
    provider = AsyncMock()
    monkeypatch.setattr(
        "mato_loop_backend.memory.dreaming.load_effective_ai", provider
    )
    db = AsyncMock(spec=AsyncSession)

    await DreamingService._process_active(db, run, session)

    assert run.status == "cancelled"
    assert run.error == "memory.invalidSource"
    assert run.completed_at is not None
    assert run.next_attempt_at is None
    db.commit.assert_awaited_once()
    load.assert_not_awaited()
    provider.assert_not_awaited()


async def test_manual_dreaming_api_rejects_automation_source(monkeypatch) -> None:
    user = UserAccount(id=uuid4(), username="test", password_hash="hash")
    session = ChatSession(
        id=uuid4(), user_id=user.id, session_type="automation", memory_mode="full"
    )
    provider = LlmProviderConfig(name="Local", adapter_type="openai-compatible")
    model = ModelConfig(provider_id=provider.id, model_id="model")
    ai = AiSettings(providers=[provider], models=[model], default_model_id=model.id)
    monkeypatch.setattr(SettingsStore, "load", AsyncMock(return_value=MemorySettings()))
    monkeypatch.setattr(
        "mato_loop_backend.api.routes.memories.load_effective_ai",
        AsyncMock(return_value=ai),
    )
    enqueue = AsyncMock()
    monkeypatch.setattr(DreamingService, "enqueue", enqueue)
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = session

    with pytest.raises(ApiError) as exc_info:
        await start_dream_runs(user, db, DreamRunRequest(sessionId=session.id))

    assert exc_info.value.code == "memory.invalidSource"
    enqueue.assert_not_awaited()


async def test_dreaming_failure_reloads_run_by_stable_id_after_rollback(
    monkeypatch,
) -> None:
    run = MemoryDreamRun(
        id=uuid4(),
        session_id=uuid4(),
        status="queued",
        trigger="manual",
        source_from_message_id=uuid4(),
        source_to_message_id=uuid4(),
        source_message_count=2,
        attempts=0,
    )
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = run
    db.get.return_value = run
    monkeypatch.setattr(
        DreamingService,
        "_process",
        AsyncMock(side_effect=RuntimeError("memory.dreamingOutputInvalid")),
    )

    assert await DreamingService.process_next(db) is True

    db.get.assert_awaited_once_with(MemoryDreamRun, run.id)
    assert run.status == "queued"
    assert run.error == "memory.dreamingFailed"
    assert run.next_attempt_at is not None
    assert db.commit.await_count == 2


def test_dreaming_output_accepts_empty_and_validates_evidence_shape() -> None:
    assert DreamCandidates.model_validate({"candidates": []}).candidates == []
    message_id = uuid4()
    output = DreamCandidates.model_validate(
        {
            "candidates": [
                {
                    "content": "Dauerhafter Fakt",
                    "category": "fact",
                    "evidenceMessageIds": [str(message_id)],
                }
            ]
        }
    )

    assert output.candidates[0].evidence_message_ids == [message_id]
    assert output.candidates[0].category == MemoryCategory.FACT
    assert output.candidates[0].remove is False
    removal = DreamCandidates.model_validate(
        {
            "candidates": [
                {
                    "memory": "Der Nutzer lebt in Berlin.",
                    "category": "identity",
                    "evidence": [str(message_id)],
                    "remove": True,
                }
            ]
        }
    ).candidates[0]
    assert removal.remove is True
    memory_id = uuid4()
    second_memory_id = uuid4()
    remove = DreamReconciliation.model_validate(
        {
            "decisions": [
                {
                    "index": 0,
                    "action": "remove",
                    "memoryIds": [str(memory_id)],
                    "memory": "Der Nutzer lebt in Berlin.",
                    "category": "identity",
                }
            ]
        }
    ).decisions[0]
    assert remove.content is None
    assert remove.category is None
    assert remove.memory_ids == [memory_id]
    with pytest.raises(ValidationError):
        DreamReconciliation.model_validate(
            {
                "decisions": [
                    {
                        "index": 0,
                        "action": "remove",
                        "memoryIds": [str(memory_id), str(second_memory_id)],
                    }
                ]
            }
        )

    with pytest.raises(ValueError):
        DreamCandidates.model_validate(
            {
                "candidates": [
                    {
                        "content": "   ",
                        "category": "fact",
                        "evidenceMessageIds": [str(message_id)],
                    }
                ]
            }
        )
    with pytest.raises(ValidationError):
        DreamCandidates.model_validate(
            {
                "candidates": [
                    {
                        "memory": "Der Nutzer lebt in Berlin.",
                        "category": "identity",
                        "evidence": [str(message_id)],
                        "remove": True,
                        "pinKind": "primary_residence",
                    }
                ]
            }
        )
    with pytest.raises(ValidationError):
        DreamCandidates.model_validate(
            {
                "candidates": [
                    {
                        "memory": "Der Nutzer isst kein Hähnchen.",
                        "category": "preference",
                        "evidence": [str(message_id)],
                        "pin": True,
                    }
                ]
            }
        )


def test_dreaming_candidate_schema_uses_small_model_friendly_fields() -> None:
    schema = DreamCandidates.model_json_schema(by_alias=True)
    candidate = schema["$defs"]["DreamCandidate"]

    assert set(candidate["required"]) == {
        "memory",
        "category",
        "evidence",
    }
    assert candidate["properties"]["remove"]["default"] is False
    assert "pin" not in candidate["properties"]
    assert "pinKind" not in candidate["properties"]
    assert DreamCandidates.model_validate(
        {
            "candidates": [
                {
                    "memory": "Der Nutzer ist verheiratet.",
                    "category": "identity",
                    "evidence": [str(uuid4())],
                }
            ]
        }
    ).candidates[0].content == "Der Nutzer ist verheiratet."


def test_dreaming_tool_schemas_are_inline_for_local_models() -> None:
    candidate_schema = _record_candidates_tool()["parameters"]
    candidate = candidate_schema["properties"]["candidates"]["items"]
    reconciliation_schema = _reconciliation_tool(2)["parameters"]
    decisions = reconciliation_schema["properties"]["decisions"]

    assert "$defs" not in candidate_schema
    assert "$ref" not in json.dumps(candidate_schema)
    assert candidate_schema["required"] == ["candidates"]
    assert candidate_schema["additionalProperties"] is False
    assert candidate_schema["properties"]["candidates"]["maxItems"] == 100
    assert candidate["additionalProperties"] is False
    assert candidate["properties"]["memory"]["maxLength"] == 20_000
    assert "exact proposition to remove" in candidate["properties"]["memory"]["description"]
    assert candidate["properties"]["evidence"]["maxItems"] == 100
    assert set(candidate["required"]) == {
        "memory",
        "category",
        "evidence",
        "remove",
    }
    assert candidate["properties"]["remove"]["default"] is False
    assert "$defs" not in reconciliation_schema
    assert "$ref" not in json.dumps(reconciliation_schema)
    assert reconciliation_schema["required"] == ["decisions"]
    assert "minItems" not in decisions
    assert decisions["maxItems"] == 2
    assert decisions["items"]["properties"]["index"] == {
        "type": "integer",
        "minimum": 0,
        "maximum": 1,
    }
    assert set(decisions["items"]["required"]) == {
        "index",
        "action",
        "memoryIds",
        "memory",
        "category",
    }
    assert decisions["items"]["properties"]["memory"]["maxLength"] == 20_000
    assert "independent or uncertain facts" in decisions["items"]["properties"]["action"][
        "description"
    ]
    assert "pin" not in decisions["items"]["properties"]
    assert "pinKind" not in decisions["items"]["properties"]
    assert "anyOf" not in json.dumps(reconciliation_schema)
    assert decisions["items"]["additionalProperties"] is False


@pytest.mark.parametrize(
    ("manual", "discovered", "expected"),
    [(None, None, 4_096), (128, None, 128), (None, 64, 64), (128, 64, 64)],
)
async def test_dreaming_repairs_invalid_candidate_tool_arguments(
    monkeypatch, manual, discovered, expected,
) -> None:
    evidence_id = uuid4()
    provider = LlmProviderConfig(
        name="Lokal", adapter_type="openai-compatible", trust_level="private"
    )
    model = ModelConfig(
        provider_id=provider.id, model_id="model",
        max_output_tokens=manual, discovered_max_output_tokens=discovered,
    )
    complete = AsyncMock(
        side_effect=[
            ProviderResult(
                tool_calls=[
                    ToolCall(
                        id="invalid",
                        name="record_memory_candidates",
                        arguments={
                            "candidates": [
                                {
                                    "category": "identity",
                                    "statement": "Der Nutzer ist Ehemann und Vater.",
                                }
                            ]
                        },
                    )
                ]
            ),
            ProviderResult(
                tool_calls=[
                    ToolCall(
                        id="valid",
                        name="record_memory_candidates",
                        arguments={
                            "candidates": [
                                {
                                    "memory": "Der Nutzer ist verheiratet und hat ein Kind.",
                                    "category": "identity",
                                    "evidence": [str(evidence_id)],
                                }
                            ]
                        },
                    )
                ]
            ),
        ]
    )
    monkeypatch.setattr(ProviderService, "complete", complete)

    output = await _extract_candidates(
        AsyncMock(spec=AsyncSession),
        provider=provider,
        model=model,
        system_prompt=DREAMING_SYSTEM_PROMPT,
        safety_reserve_tokens=128,
        source=f"MESSAGE {evidence_id} [USER]\nmeine Frau und unser Kind",
        max_candidates=10,
        evidence_ids={evidence_id},
        user_evidence_ids={evidence_id},
    )

    assert output.candidates[0].content == "Der Nutzer ist verheiratet und hat ein Kind."
    assert complete.await_count == 2
    assert [call.kwargs["max_output_tokens"] for call in complete.await_args_list] == [expected] * 2
    assert [call.kwargs["safety_reserve_tokens"] for call in complete.await_args_list] == [128] * 2
    retry_messages = complete.await_args_list[1].kwargs["messages"]
    assert retry_messages[-1]["role"] == "tool"
    assert "memory, category, evidence, and remove" in retry_messages[-1]["content"]
    assert "usage attribution" not in retry_messages[-1]["content"]


async def test_dreaming_stops_before_a_repair_when_user_becomes_inactive(
    monkeypatch,
) -> None:
    evidence_id = uuid4()
    provider = LlmProviderConfig(name="Lokal", adapter_type="openai-compatible")
    model = ModelConfig(provider_id=provider.id, model_id="model")
    complete = AsyncMock(return_value=ProviderResult(tool_calls=[]))
    active_check = AsyncMock(
        side_effect=[None, DreamingContractError("auth.accountDisabled")]
    )
    monkeypatch.setattr(ProviderService, "complete", complete)

    with pytest.raises(DreamingContractError, match="auth.accountDisabled"):
        await _extract_candidates(
            AsyncMock(spec=AsyncSession),
            provider=provider,
            model=model,
            system_prompt=DREAMING_SYSTEM_PROMPT,
            safety_reserve_tokens=128,
            source=f"MESSAGE {evidence_id} [USER]\nTest",
            max_candidates=10,
            evidence_ids={evidence_id},
            user_evidence_ids={evidence_id},
            active_check=active_check,
        )

    assert active_check.await_count == 2
    complete.assert_awaited_once()


async def test_dreaming_repairs_assistant_only_evidence(monkeypatch) -> None:
    user_id = uuid4()
    assistant_id = uuid4()
    provider = LlmProviderConfig(
        name="Lokal", adapter_type="openai-compatible", trust_level="private"
    )
    model = ModelConfig(provider_id=provider.id, model_id="model")
    complete = AsyncMock(
        side_effect=[
            ProviderResult(
                tool_calls=[
                    ToolCall(
                        id="assistant-only",
                        name="record_memory_candidates",
                        arguments={
                            "candidates": [
                                {
                                    "memory": "Der Nutzer bevorzugt kurze Antworten.",
                                    "category": "preference",
                                    "evidence": [str(assistant_id)],
                                }
                            ]
                        },
                    )
                ]
            ),
            ProviderResult(
                tool_calls=[
                    ToolCall(
                        id="user-supported",
                        name="record_memory_candidates",
                        arguments={
                            "candidates": [
                                {
                                    "memory": "Der Nutzer bevorzugt kurze Antworten.",
                                    "category": "preference",
                                    "evidence": [str(user_id)],
                                }
                            ]
                        },
                    )
                ]
            ),
        ]
    )
    monkeypatch.setattr(ProviderService, "complete", complete)

    output = await _extract_candidates(
        AsyncMock(spec=AsyncSession),
        provider=provider,
        model=model,
        system_prompt=DREAMING_SYSTEM_PROMPT,
        safety_reserve_tokens=128,
        source=(
            f"MESSAGE {user_id} [USER]\nBitte antworte kurz.\n\n"
            f"MESSAGE {assistant_id} [ASSISTANT]\nVerstanden."
        ),
        max_candidates=10,
        evidence_ids={user_id, assistant_id},
        user_evidence_ids={user_id},
    )

    assert output.candidates[0].evidence_message_ids == [user_id]
    assert complete.await_count == 2
    assert "at least one USER message UUID" in complete.await_args_list[1].kwargs[
        "messages"
    ][-1]["content"]


async def test_dreaming_removal_requires_only_direct_user_evidence(monkeypatch) -> None:
    user_id = uuid4()
    assistant_id = uuid4()
    provider = LlmProviderConfig(name="Lokal", adapter_type="openai-compatible")
    model = ModelConfig(provider_id=provider.id, model_id="model")
    candidate = {
        "memory": "Der Nutzer lebt in Berlin.",
        "category": "identity",
        "remove": True,
    }
    complete = AsyncMock(
        side_effect=[
            ProviderResult(
                tool_calls=[
                    ToolCall(
                        id="assistant-evidence",
                        name="record_memory_candidates",
                        arguments={
                            "candidates": [
                                {
                                    **candidate,
                                    "evidence": [str(user_id), str(assistant_id)],
                                }
                            ]
                        },
                    )
                ]
            ),
            ProviderResult(
                tool_calls=[
                    ToolCall(
                        id="direct-user-evidence",
                        name="record_memory_candidates",
                        arguments={
                            "candidates": [
                                {**candidate, "evidence": [str(user_id)]}
                            ]
                        },
                    )
                ]
            ),
        ]
    )
    monkeypatch.setattr(ProviderService, "complete", complete)

    output = await _extract_candidates(
        AsyncMock(spec=AsyncSession),
        provider=provider,
        model=model,
        system_prompt=DREAMING_SYSTEM_PROMPT,
        safety_reserve_tokens=128,
        source=(
            f"MESSAGE {user_id} [USER]\nVergiss, dass ich in Berlin lebe.\n\n"
            f"MESSAGE {assistant_id} [ASSISTANT]\nVerstanden."
        ),
        max_candidates=10,
        evidence_ids={user_id, assistant_id},
        user_evidence_ids={user_id},
    )

    assert output.candidates[0].remove is True
    assert output.candidates[0].evidence_message_ids == [user_id]
    assert complete.await_count == 2


async def test_dreaming_extraction_uses_source_only_and_merges_candidate_calls(
    monkeypatch,
) -> None:
    first_evidence_id = uuid4()
    second_evidence_id = uuid4()
    provider = LlmProviderConfig(
        name="Lokal", adapter_type="openai-compatible", trust_level="private"
    )
    model = ModelConfig(provider_id=provider.id, model_id="model")
    pinned = MemoryRecord(
        id=uuid4(),
        source_type="manual",
        pinned=True,
        audience="private_only",
        category="identity",
        revision=1,
        content="Der Nutzer hat eine Familie.",
    )
    complete = AsyncMock(
        return_value=ProviderResult(
            tool_calls=[
                ToolCall(
                    id="record-1",
                    name="record_memory_candidates",
                    arguments={
                        "candidates": [
                            {
                                "memory": "Der Nutzer hat eine Familie.",
                                "category": "identity",
                                "evidence": [str(first_evidence_id)],
                            }
                        ]
                    },
                ),
                ToolCall(
                    id="record-2",
                    name="record_memory_candidates",
                    arguments={
                        "candidates": [
                            {
                                "memory": " der nutzer hat eine familie. ",
                                "category": "identity",
                                "evidence": [str(second_evidence_id)],
                            }
                        ]
                    },
                ),
            ]
        )
    )
    monkeypatch.setattr(ProviderService, "complete", complete)
    db = AsyncMock(spec=AsyncSession)

    output = await _extract_candidates(
        db,
        provider=provider,
        model=model,
        system_prompt=DREAMING_SYSTEM_PROMPT,
        safety_reserve_tokens=128,
        source=(
            f"MESSAGE {first_evidence_id} [USER]\nMeine Familie ist wichtig.\n\n"
            f"MESSAGE {second_evidence_id} [ASSISTANT]\nVerstanden."
        ),
        max_candidates=10,
        evidence_ids={first_evidence_id, second_evidence_id},
        user_evidence_ids={first_evidence_id},
    )

    assert len(output.candidates) == 1
    assert output.candidates[0].content == "Der Nutzer hat eine Familie."
    assert output.candidates[0].evidence_message_ids == [
        first_evidence_id,
        second_evidence_id,
    ]
    request = complete.await_args.kwargs
    assert pinned.content not in request["messages"][1]["content"]
    assert request["messages"][1]["content"].startswith("SOURCE MESSAGE BATCH")
    assert [tool["name"] for tool in request["tools"]] == [
        "record_memory_candidates"
    ]


@pytest.mark.parametrize(
    ("manual", "discovered", "expected"),
    [(None, None, 4_096), (128, None, 128), (None, 64, 64), (128, 64, 64)],
)
async def test_dreaming_reconciliation_repairs_invalid_output(
    monkeypatch, manual, discovered, expected,
) -> None:
    memory_id = uuid4()
    provider = LlmProviderConfig(
        name="Lokal", adapter_type="openai-compatible", trust_level="private"
    )
    model = ModelConfig(
        provider_id=provider.id, model_id="model",
        max_output_tokens=manual, discovered_max_output_tokens=discovered,
    )
    complete = AsyncMock(
        side_effect=[
            ProviderResult(
                tool_calls=[
                    ToolCall(
                        id="invalid",
                        name="reconcile_memory_candidates",
                        arguments={"decisions": [{"index": 0, "action": "update"}]},
                    )
                ]
            ),
            ProviderResult(
                tool_calls=[
                    ToolCall(
                        id="valid",
                        name="reconcile_memory_candidates",
                        arguments={
                            "decisions": [
                                {
                                    "index": 0,
                                    "action": "unchanged",
                                    "memoryIds": [str(memory_id)],
                                    "memory": "Mein Name ist Lara.",
                                    "category": "identity",
                                }
                            ]
                        },
                    )
                ]
            ),
        ]
    )
    monkeypatch.setattr(ProviderService, "complete", complete)

    result = await _reconcile_candidates(
        AsyncMock(spec=AsyncSession),
        provider=provider,
        model=model,
        source="{}",
        candidate_count=1,
        candidate_local_ids=[{memory_id}],
        candidate_remove=[False],
        safety_reserve_tokens=128,
    )

    assert result.decisions[0].action == "unchanged"
    assert complete.await_count == 2
    assert [call.kwargs["max_output_tokens"] for call in complete.await_args_list] == [expected] * 2
    assert [call.kwargs["safety_reserve_tokens"] for call in complete.await_args_list] == [128] * 2
    assert complete.await_args_list[0].kwargs["messages"][0]["content"] == (
        RECONCILIATION_SYSTEM_PROMPT
    )


async def test_dreaming_removal_reconciliation_allows_only_exact_remove(
    monkeypatch,
) -> None:
    memory_id = uuid4()
    provider = LlmProviderConfig(name="Lokal", adapter_type="openai-compatible")
    model = ModelConfig(provider_id=provider.id, model_id="model")
    complete = AsyncMock(
        side_effect=[
            ProviderResult(
                tool_calls=[
                    ToolCall(
                        id="invalid-create",
                        name="reconcile_memory_candidates",
                        arguments={
                            "decisions": [
                                {
                                    "index": 0,
                                    "action": "create",
                                    "memoryIds": [],
                                    "memory": "Mein Name ist Lara.",
                                    "category": "identity",
                                }
                            ]
                        },
                    )
                ]
            ),
            ProviderResult(
                tool_calls=[
                    ToolCall(
                        id="valid-remove",
                        name="reconcile_memory_candidates",
                        arguments={
                            "decisions": [
                                {
                                    "index": 0,
                                    "action": "remove",
                                    "memoryIds": [str(memory_id)],
                                    "memory": "Mein Name ist Lara.",
                                    "category": "identity",
                                }
                            ]
                        },
                    )
                ]
            ),
        ]
    )
    monkeypatch.setattr(ProviderService, "complete", complete)

    result = await _reconcile_candidates(
        AsyncMock(spec=AsyncSession),
        provider=provider,
        model=model,
        source="{}",
        candidate_count=1,
        candidate_local_ids=[{memory_id}],
        candidate_remove=[True],
        safety_reserve_tokens=128,
    )

    assert result.decisions[0].action == "remove"
    assert result.decisions[0].memory_ids == [memory_id]
    assert complete.await_count == 2


async def test_dreaming_reconciliation_merges_parallel_outputs(monkeypatch) -> None:
    memory_id = uuid4()
    provider = LlmProviderConfig(
        name="Lokal", adapter_type="openai-compatible", trust_level="private"
    )
    model = ModelConfig(provider_id=provider.id, model_id="model")
    complete = AsyncMock(
        return_value=ProviderResult(
            tool_calls=[
                ToolCall(
                    id="decision-1",
                    name="reconcile_memory_candidates",
                    arguments={
                        "decisions": [
                            {
                                "index": 0,
                                "action": "unchanged",
                                "memoryIds": [str(memory_id)],
                                "memory": "Mein Name ist Lara.",
                                "category": "identity",
                            }
                        ]
                    },
                ),
                ToolCall(
                    id="decision-2",
                    name="reconcile_memory_candidates",
                    arguments={
                        "decisions": [
                            {
                                "index": 1,
                                "action": "create",
                                "memoryIds": [],
                                "memory": "Ich spiele Klavier.",
                                "category": "identity",
                            }
                        ]
                    },
                ),
            ]
        )
    )
    monkeypatch.setattr(ProviderService, "complete", complete)

    result = await _reconcile_candidates(
        AsyncMock(spec=AsyncSession),
        provider=provider,
        model=model,
        source="{}",
        candidate_count=2,
        candidate_local_ids=[{memory_id}, set()],
        candidate_remove=[False, False],
        safety_reserve_tokens=128,
    )

    assert [decision.candidate_index for decision in result.decisions] == [0, 1]
    assert [decision.action for decision in result.decisions] == ["unchanged", "create"]
    complete.assert_awaited_once()


def test_dreaming_system_prompt_remains_isolated_from_agent_context() -> None:
    assert "durable user-specific knowledge" in DREAMING_SYSTEM_PROMPT
    assert "retrieval-only memories" in DREAMING_SYSTEM_PROMPT
    assert "those belong to the maintained user profile" in DREAMING_SYSTEM_PROMPT
    assert "user's full name" in DREAMING_SYSTEM_PROMPT
    assert "owned equipment or lasting technical capabilities" in DREAMING_SYSTEM_PROMPT
    assert "current tasks, implementation details, or work state" in DREAMING_SYSTEM_PROMPT
    assert "temporary or borrowed equipment" in DREAMING_SYSTEM_PROMPT
    assert "empty candidates array" in DREAMING_SYSTEM_PROMPT
    assert "one-off questions" in DREAMING_SYSTEM_PROMPT
    assert "single request does not establish" in DREAMING_SYSTEM_PROMPT
    assert "at least one user message ID" in DREAMING_SYSTEM_PROMPT
    assert "memory_retrieve" not in DREAMING_SYSTEM_PROMPT
    assert "record_memory_candidates" in DREAMING_SYSTEM_PROMPT
    assert "retrieval level" not in DREAMING_SYSTEM_PROMPT
    assert "web_search" not in DREAMING_SYSTEM_PROMPT


def test_dreaming_reconciliation_requires_complete_safe_updates() -> None:
    memory_id = uuid4()
    second_memory_id = uuid4()
    create = DreamReconciliation.model_validate(
        {
            "decisions": [
                {
                    "index": 0,
                    "action": "create",
                    "memoryIds": [],
                    "memory": "Neue Erinnerung",
                    "category": "fact",
                }
            ]
        }
    ).decisions[0]
    result = DreamReconciliation.model_validate(
        {
            "decisions": [
                {
                    "candidateIndex": 0,
                    "action": "update",
                    "memoryIds": [str(memory_id)],
                    "content": "Aktuelle Präferenz",
                    "category": "preference",
                }
            ]
        }
    )

    assert create.content is None
    assert result.decisions[0].memory_ids == [memory_id]
    assert "Reference only memory IDs supplied" in RECONCILIATION_SYSTEM_PROMPT
    assert "default to create" in RECONCILIATION_SYSTEM_PROMPT
    assert "Never infer that a new statement replaces" in RECONCILIATION_SYSTEM_PROMPT
    assert "usefully connects" not in RECONCILIATION_SYSTEM_PROMPT
    merge = DreamReconciliation.model_validate(
        {
            "decisions": [
                {
                    "index": 0,
                    "action": "merge",
                    "memoryIds": [str(memory_id), str(second_memory_id)],
                    "memory": "Ich heiße Mara.",
                    "category": "identity",
                }
            ]
        }
    ).decisions[0]
    assert merge.memory_ids == [memory_id, second_memory_id]
    with pytest.raises(ValidationError):
        DreamReconciliation.model_validate(
            {
                "decisions": [
                    {
                        "candidateIndex": 0,
                        "action": "update",
                        "memoryIds": [str(memory_id)],
                    }
                ]
            }
        )
    with pytest.raises(ValidationError):
        DreamReconciliation.model_validate(
            {
                "decisions": [
                    {
                        "index": 0,
                        "action": "merge",
                        "memoryIds": [str(memory_id)],
                        "memory": "Zusammengeführt",
                        "category": "fact",
                    }
                ]
            }
        )


@pytest.mark.parametrize(
    ("manual", "discovered", "expected"),
    [(None, None, 4_096), (8_192, None, 4_096), (128, None, 128), (None, 64, 64)],
)
@pytest.mark.parametrize("safety", [0, 128, 4_096, 8_192])
def test_dreaming_source_budget_uses_request_caps(manual, discovered, expected, safety):
    model = ModelConfig(
        provider_id=uuid4(), model_id="model", context_window_tokens=32_768,
        max_output_tokens=manual, discovered_max_output_tokens=discovered, characters_per_token=4,
    )
    raw_capacity = raw_input_budget(32_768 - expected - safety)
    limit = _source_character_limit(model, safety_reserve_tokens=safety)
    assert limit == raw_capacity * 4 - 16_000
    assert allocate_output_tokens(
        model, (limit + 16_000) // 4,
        requested=expected, reserved_output_tokens=expected, safety_reserve_tokens=safety,
    ) == expected
    model.context_window_tokens = 4_096
    assert _source_character_limit(model, safety_reserve_tokens=safety) == 0
    assert _fit_message_batch([
        Message(id=uuid4(), role="user", content="Question"),
        Message(id=uuid4(), role="assistant", content="Answer"),
    ], model, safety_reserve_tokens=safety) == []


def test_dreaming_fit_keeps_only_complete_serialized_turns() -> None:
    model = ModelConfig(
        provider_id=uuid4(),
        model_id="small",
        context_window_tokens=13_000,
        max_output_tokens=8_192,
        characters_per_token=4.0,
    )
    first = Message(id=uuid4(), role="user", content="short", created_at=datetime.now(UTC))
    second = Message(
        id=uuid4(), role="assistant", content="complete", created_at=datetime.now(UTC)
    )
    omitted = Message(
        id=uuid4(), role="user", content="x" * 2_000, created_at=datetime.now(UTC)
    )

    fitted = _fit_message_batch([first, second, omitted], model, safety_reserve_tokens=4_096)

    assert fitted == [first, second]
    assert omitted.id not in {item.id for item in fitted}


def test_reconciliation_batches_never_slice_candidate_content() -> None:
    model = ModelConfig(
        provider_id=uuid4(),
        model_id="small",
        context_window_tokens=16_384,
        max_output_tokens=8_192,
        characters_per_token=4.0,
    )
    first = DreamCandidates.model_validate(
        {
            "candidates": [
                {
                    "memory": "a" * 8_000,
                    "category": "fact",
                    "evidence": [str(uuid4())],
                },
                {
                    "memory": "b" * 8_000,
                    "category": "fact",
                    "evidence": [str(uuid4())],
                },
            ]
        }
    ).candidates

    evidence = {
        message_id: "x" * 80_000
        for candidate in first
        for message_id in candidate.evidence_message_ids
    }
    batches = _reconciliation_batches(
        [(index, candidate, []) for index, candidate in enumerate(first)],
        model,
        evidence,
        safety_reserve_tokens=4_096,
    )

    assert [entry[1].content for batch in batches for entry in batch] == [
        "a" * 8_000,
        "b" * 8_000,
    ]
    assert all(
        len(_reconciliation_source([(entry[1], entry[2]) for entry in batch], evidence))
        <= _source_character_limit(model, DREAMING_RECONCILIATION_OUTPUT_TOKENS, safety_reserve_tokens=4_096)
        for batch in batches
    )
    assert "x" * 1_000 not in _reconciliation_source(
        [(entry[1], entry[2]) for batch in batches for entry in batch], evidence
    )


async def test_contract_exhaustion_fails_persistent_run_without_retry(monkeypatch) -> None:
    run = MemoryDreamRun(
        id=uuid4(),
        session_id=uuid4(),
        status="queued",
        trigger="manual",
        source_from_message_id=uuid4(),
        source_to_message_id=uuid4(),
        source_message_count=2,
        attempts=0,
    )
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = run
    db.get.return_value = run
    monkeypatch.setattr(
        DreamingService,
        "_process",
        AsyncMock(side_effect=DreamingContractError("memory.dreamingOutputInvalid")),
    )

    assert await DreamingService.process_next(db) is True

    assert run.status == "failed"
    assert run.error == "memory.dreamingOutputInvalid"
    assert run.next_attempt_at is None


@pytest.mark.parametrize(
    ("exact", "remove_candidate", "latest_enabled", "latest_mode"),
    [
        pytest.param(True, False, True, "full", id="exact-profile-duplicate"),
        pytest.param(False, False, True, "full", id="new-profile"),
        pytest.param(False, False, False, "full", id="memory-disabled"),
        pytest.param(False, False, True, "read_only", id="session-read-only"),
        pytest.param(True, True, True, "full", id="exact-remove"),
        pytest.param(False, True, True, "full", id="related-remove-skipped"),
    ],
)
async def test_host_matching_routes_profile_and_remove_candidates(
    monkeypatch, exact, remove_candidate, latest_enabled, latest_mode
) -> None:
    monkeypatch.setattr(
        DreamingService, "_cancel_if_inactive", AsyncMock(return_value=False)
    )
    user_id = uuid4()
    session = ChatSession(
        id=uuid4(), user_id=user_id, title="Test", memory_mode="full", session_type="chat", trust_level="public"
    )
    locked_session = ChatSession(
        id=session.id,
        user_id=user_id,
        title="Test",
        memory_mode=latest_mode,
        session_type="chat",
        trust_level="public",
    )
    user_message = Message(
        id=uuid4(),
        session_id=session.id,
        role="user",
        content=(
            "Vergiss, dass ich kein Hähnchen esse."
            if remove_candidate
            else "Ich esse kein Hähnchen."
        ),
        trust_level="public",
        created_at=datetime.now(UTC),
    )
    assistant_message = Message(
        id=uuid4(),
        session_id=session.id,
        role="assistant",
        content="Verstanden.",
        trust_level="public",
        created_at=datetime.now(UTC),
    )
    run = MemoryDreamRun(
        id=uuid4(),
        session_id=session.id,
        status="running",
        trigger="manual",
        source_from_message_id=user_message.id,
        source_to_message_id=assistant_message.id,
        source_message_count=2,
        attempts=1,
    )
    state = MemoryDreamState(session_id=session.id)
    existing_content = (
        "Ich esse kein Hähnchen."
        if exact
        else "Ich esse gelegentlich Hähnchen."
        if remove_candidate
        else "Der Nutzer spielt Klavier."
    )
    _, content_hash = normalize_content(existing_content)
    existing = MemoryRecord(
        id=uuid4(),
        user_id=user_id,
        source_type="manual",
        pinned=not exact,
        audience="both",
        category="preference",
        content=existing_content,
        normalized_content_hash=content_hash,
        revision=2,
    )
    provider = LlmProviderConfig(name="Lokal", adapter_type="openai-compatible")
    model = ModelConfig(provider_id=provider.id, model_id="model")
    ai = AiSettings(providers=[provider], models=[model], default_model_id=model.id)
    complete = AsyncMock(
        return_value=ProviderResult(
            tool_calls=[
                ToolCall(
                    id="extract",
                    name="record_memory_candidates",
                    arguments={
                        "candidates": [
                            {
                                "memory": "Ich esse kein Hähnchen.",
                                "category": "preference",
                                "evidence": [str(user_message.id)],
                                "remove": remove_candidate,
                            }
                        ]
                    },
                )
            ]
        )
    )
    monkeypatch.setattr(ProviderService, "complete", complete)
    monkeypatch.setattr(
        "mato_loop_backend.memory.dreaming.load_effective_ai",
        AsyncMock(return_value=ai),
    )
    load_settings = AsyncMock(
        side_effect=lambda _db, kind, **kwargs: (
            MemorySettings(
                enabled=latest_enabled,
                dreaming_update_policy="ask",
                dreaming_remove_policy="auto",
            )
            if kind is MemorySettings and kwargs.get("for_update")
            else MemorySettings(dreaming_update_policy="auto")
            if kind is MemorySettings
            else UserProfileSettings()
            if kind is UserProfileSettings
            else AgentRuntimeSettings(safety_reserve_tokens=128)
            if kind is AgentRuntimeSettings
            else GeneralSettings()
            if kind is GeneralSettings
            else ai
        )
    )
    monkeypatch.setattr(
        SettingsStore,
        "load",
        load_settings,
    )
    reconciliation_candidates = AsyncMock(
        return_value=[
            [existing] if exact or remove_candidate else []
        ]
    )
    monkeypatch.setattr(
        MemoryService, "reconciliation_candidates", reconciliation_candidates
    )
    reconcile = AsyncMock(
        return_value=DreamReconciliation.model_validate(
            {
                "decisions": [
                    {
                        "index": 0,
                        "action": "remove" if remove_candidate else "unchanged",
                        "memoryIds": [str(existing.id)],
                        "memory": existing.content,
                        "category": existing.category,
                    }
                ]
            }
        )
    )
    monkeypatch.setattr(
        "mato_loop_backend.memory.dreaming._reconcile_candidates", reconcile
    )
    keep = AsyncMock(return_value=MemoryMutation(existing))
    created_item = MemoryRecord(
        id=uuid4(),
        user_id=user_id,
        source_type="dreaming",
        pinned=True,
        audience="both",
        category="preference",
        content="Ich esse kein Hähnchen.",
        normalized_content_hash=normalize_content("Ich esse kein Hähnchen.")[1],
        revision=1,
    )
    create = AsyncMock(return_value=MemoryMutation(created_item, created=True))
    remove = AsyncMock(return_value=MemoryMutation(existing, updated=True))
    snapshot = MemoryApprovalTarget(
        id=existing.id,
        revision=existing.revision,
        content=existing.content,
        category=MemoryCategory(existing.category),
        audience=MemoryAudience(existing.audience),
        pinned=existing.pinned,
        evidenceHash="0" * 64,
    )
    monkeypatch.setattr(MemoryService, "keep_dreaming", keep)
    monkeypatch.setattr(MemoryService, "create_dreaming", create)
    monkeypatch.setattr(MemoryService, "remove_dreaming", remove)
    monkeypatch.setattr(
        MemoryService, "_approval_target", AsyncMock(return_value=snapshot)
    )
    approval = AsyncMock()
    monkeypatch.setattr(MemoryService, "request_dreaming_approval", approval)
    pinned_context = AsyncMock(return_value=[existing])
    monkeypatch.setattr(MemoryService, "pinned_context", pinned_context)
    monkeypatch.setattr(
        "mato_loop_backend.memory.dreaming.get_runtime_config",
        lambda: Mock(embedding_enabled=False),
    )
    db = AsyncMock(spec=AsyncSession)

    async def get(model_type, item_id, **_kwargs):
        if model_type is ChatSession and _kwargs.get("with_for_update"):
            return locked_session
        return {
            (MemoryDreamRun, run.id): run,
            (ChatSession, session.id): session,
            (Message, user_message.id): user_message,
            (Message, assistant_message.id): assistant_message,
        }.get((model_type, item_id))

    db.get.side_effect = get
    db.scalar.side_effect = [None, state]
    rows = Mock()
    rows.all.return_value = [user_message, assistant_message]
    db.scalars.return_value = rows

    await DreamingService._process(db, run.id)

    complete.assert_awaited_once()
    assert complete.await_args.kwargs["safety_reserve_tokens"] == 128
    assert reconciliation_candidates.await_args.kwargs["owner_id"] == user_id
    blocked = not latest_enabled or latest_mode != "full"
    if blocked:
        keep.assert_not_awaited()
        create.assert_not_awaited()
        remove.assert_not_awaited()
        approval.assert_not_awaited()
        assert state.last_processed_message_id is None
        assert run.status == "blocked"
        return
    if remove_candidate:
        assert reconcile.await_count == int(exact)
        keep.assert_not_awaited()
        create.assert_not_awaited()
        approval.assert_not_awaited()
        assert remove.await_count == int(exact)
        assert run.removed_memory_count == int(exact)
        if exact:
            assert remove.await_args.kwargs["evidence_message_ids"] == [
                user_message.id
            ]
    elif exact:
        reconcile.assert_not_awaited()
        keep.assert_awaited_once()
        assert keep.await_args.kwargs["owner_id"] == user_id
        assert keep.await_args.kwargs["evidence_message_ids"] == [user_message.id]
        create.assert_not_awaited()
    elif not remove_candidate:
        create.assert_awaited_once()
        assert create.await_args.kwargs["owner_id"] == user_id
        keep.assert_not_awaited()
    approval.assert_not_awaited()
    assert any(
        call.args[1] is MemorySettings and call.kwargs.get("for_update") is True
        for call in load_settings.await_args_list
    )
    pinned_context.assert_not_awaited()
    assert state.last_processed_message_id == assistant_message.id
    assert run.status == "completed"


@pytest.mark.parametrize("policy", list(ToolExecutionPolicy))
async def test_dreaming_update_obeys_auto_ask_and_disabled(monkeypatch, policy) -> None:
    monkeypatch.setattr(
        DreamingService, "_cancel_if_inactive", AsyncMock(return_value=False)
    )
    session = ChatSession(
        id=uuid4(),
        user_id=uuid4(),
        title="Test",
        memory_mode="full",
        session_type="chat",
        trust_level="private",
    )
    messages = [
        Message(
            id=uuid4(),
            session_id=session.id,
            role="user",
            content="Ich heiße jetzt Mara.",
            trust_level="public",
            created_at=datetime.now(UTC),
        ),
        Message(
            id=uuid4(),
            session_id=session.id,
            role="assistant",
            content="Verstanden.",
            trust_level="public",
            created_at=datetime.now(UTC),
        ),
    ]
    run = MemoryDreamRun(
        id=uuid4(),
        session_id=session.id,
        status="running",
        trigger="manual",
        source_from_message_id=messages[0].id,
        source_to_message_id=messages[1].id,
        source_message_count=2,
        attempts=1,
    )
    state = MemoryDreamState(session_id=session.id)
    existing = MemoryRecord(
        id=uuid4(),
        user_id=session.user_id,
        source_type="manual",
        pinned=False,
        audience="both",
        category="identity",
        content="Ich heiße Lara.",
        normalized_content_hash=normalize_content("Ich heiße Lara.")[1],
        revision=3,
    )
    provider = LlmProviderConfig(name="Lokal", adapter_type="openai-compatible")
    model = ModelConfig(provider_id=provider.id, model_id="model")
    ai = AiSettings(providers=[provider], models=[model], default_model_id=model.id)
    monkeypatch.setattr(
        "mato_loop_backend.memory.dreaming.load_effective_ai",
        AsyncMock(return_value=ai),
    )
    initial_settings = MemorySettings(dreaming_update_policy="auto")
    settings = MemorySettings(dreaming_update_policy=policy)
    load_settings = AsyncMock(
        side_effect=lambda _db, kind, **kwargs: (
            settings
            if kind is MemorySettings and kwargs.get("for_update")
            else initial_settings
            if kind is MemorySettings
            else UserProfileSettings()
            if kind is UserProfileSettings
            else AgentRuntimeSettings(safety_reserve_tokens=128)
            if kind is AgentRuntimeSettings
            else GeneralSettings()
            if kind is GeneralSettings
            else ai
        )
    )
    monkeypatch.setattr(
        SettingsStore,
        "load",
        load_settings,
    )
    candidate = DreamCandidates.model_validate(
        {
            "candidates": [
                {
                    "memory": "Ich heiße jetzt Mara.",
                    "category": "identity",
                    "evidence": [str(messages[0].id)],
                }
            ]
        }
    )
    monkeypatch.setattr(
        "mato_loop_backend.memory.dreaming._extract_candidates",
        AsyncMock(return_value=candidate),
    )
    reconciliation_candidates = AsyncMock(return_value=[[existing]])
    monkeypatch.setattr(
        MemoryService, "reconciliation_candidates", reconciliation_candidates
    )
    reconciliation = DreamReconciliation.model_validate(
        {
            "decisions": [
                {
                    "index": 0,
                    "action": "update",
                    "memoryIds": [str(existing.id)],
                    "memory": "Ich heiße Mara.",
                    "category": "identity",
                }
            ]
        }
    )
    monkeypatch.setattr(
        "mato_loop_backend.memory.dreaming._reconcile_candidates",
        AsyncMock(return_value=reconciliation),
    )
    update = AsyncMock(return_value=MemoryMutation(existing, updated=True))
    approval = AsyncMock()
    monkeypatch.setattr(MemoryService, "update_dreaming", update)
    monkeypatch.setattr(MemoryService, "request_dreaming_approval", approval)
    monkeypatch.setattr(
        "mato_loop_backend.memory.dreaming.get_runtime_config",
        lambda: Mock(embedding_enabled=False),
    )
    db = AsyncMock(spec=AsyncSession)

    async def get(model_type, item_id, **_kwargs):
        return {
            (MemoryDreamRun, run.id): run,
            (ChatSession, session.id): session,
            (Message, messages[0].id): messages[0],
            (Message, messages[1].id): messages[1],
        }.get((model_type, item_id))

    db.get.side_effect = get
    db.scalar.side_effect = [None, state]
    rows = Mock()
    rows.all.return_value = messages
    db.scalars.return_value = rows

    await DreamingService._process(db, run.id)

    assert (
        reconciliation_candidates.await_args.kwargs["owner_id"]
        == session.user_id
    )
    assert update.await_count == (1 if policy == ToolExecutionPolicy.AUTO else 0)
    assert approval.await_count == (1 if policy == ToolExecutionPolicy.ASK else 0)
    mutation = update if policy == ToolExecutionPolicy.AUTO else approval
    if policy != ToolExecutionPolicy.DISABLED:
        assert mutation.await_args.kwargs["owner_id"] == session.user_id
        assert mutation.await_args.kwargs["trust_level"] == TrustLevel.PRIVATE
    assert any(
        call.args[1] is MemorySettings and call.kwargs.get("for_update") is True
        for call in load_settings.await_args_list
    )
    assert run.status == "completed"


def test_memory_approval_payload_rejects_obsolete_and_pin_bearing_schemas() -> None:
    base = {
        "schemaVersion": 3,
        "targetMemories": [
            {
                "id": str(uuid4()),
                "revision": 1,
                "content": "Ich heiße Lara.",
                "category": "identity",
                "audience": "both",
                "pinned": False,
                "evidenceHash": "0" * 64,
            }
        ],
        "sourceDreamRunId": str(uuid4()),
        "sourceSessionId": str(uuid4()),
        "evidenceMessageIds": [str(uuid4())],
        "trustLevel": "public",
        "readableBy": "public",
        "updatePolicy": "ask",
        "removePolicy": "ask",
    }

    with pytest.raises(ValidationError):
        MemoryApprovalPayload.model_validate({**base, "schemaVersion": 2})
    with pytest.raises(ValidationError):
        MemoryApprovalPayload.model_validate({**base, "requestedPin": False})
    with pytest.raises(ValidationError):
        MemoryApprovalPayload.model_validate({**base, "pinKind": "preferred_name"})
    with pytest.raises(ValidationError):
        MemoryApprovalPayload.model_validate(
            {**base, "targetMemories": base["targetMemories"] * 51}
        )


async def test_automation_backed_approval_is_rejected_before_mutation() -> None:
    user_id = uuid4()
    session = ChatSession(
        id=uuid4(), user_id=user_id, session_type="automation", memory_mode="full"
    )
    run = MemoryDreamRun(id=uuid4(), session_id=session.id)
    approval = ApprovalRequest(
        request_type="memory_change",
        user_id=user_id,
        action_kind="memory_update",
        payload={
            "schemaVersion": 3,
            "targetMemories": [
                {
                    "id": str(uuid4()),
                    "revision": 1,
                    "content": "Fakt",
                    "category": "fact",
                    "audience": "both",
                    "pinned": False,
                    "evidenceHash": "0" * 64,
                }
            ],
            "after": {
                "content": "Neuer Fakt",
                "category": "fact",
                "audience": "both",
            },
            "sourceDreamRunId": str(run.id),
            "sourceSessionId": str(session.id),
            "evidenceMessageIds": [str(uuid4())],
            "trustLevel": "public",
            "readableBy": "public",
            "updatePolicy": "ask",
            "removePolicy": "ask",
        },
    )
    db = AsyncMock(spec=AsyncSession)
    db.get.side_effect = [session, run]

    assert (
        await MemoryService.apply_approved_change(db, approval, owner_id=user_id)
        == "target_changed"
    )
    db.scalars.assert_not_awaited()
    db.delete.assert_not_awaited()
    db.flush.assert_not_awaited()


async def test_merge_uses_matching_survivor_and_preserves_all_metadata(monkeypatch) -> None:
    owner_id = uuid4()
    first_source = uuid4()
    second_source = uuid4()
    current_evidence = uuid4()
    canonical = "Ich heiße Mara."
    first = MemoryRecord(
        id=uuid4(),
        user_id=owner_id,
        source_message_id=first_source,
        source_type="manual",
        pinned=True,
        audience="both",
        category="identity",
        content="Ich heiße Lara.",
        normalized_content_hash=normalize_content("Ich heiße Lara.")[1],
        revision=2,
    )
    second = MemoryRecord(
        id=uuid4(),
        user_id=owner_id,
        source_message_id=second_source,
        source_type="dreaming",
        pinned=False,
        audience="private_only",
        category="identity",
        content=canonical,
        normalized_content_hash=normalize_content(canonical)[1],
        revision=4,
    )
    item_rows = Mock()
    item_rows.all.return_value = [first, second]
    evidence_id = uuid4()
    evidence_rows = Mock()
    evidence_rows.all.return_value = [evidence_id]
    db = AsyncMock(spec=AsyncSession)
    db.scalars.side_effect = [item_rows, evidence_rows]
    db.scalar.return_value = None
    enqueue = AsyncMock()
    monkeypatch.setattr(
        "mato_loop_backend.memory.service.MemoryIndexService.enqueue", enqueue
    )
    monkeypatch.setattr(MemoryService, "_validate_source_references", AsyncMock())
    add_evidence = AsyncMock()
    monkeypatch.setattr(MemoryService, "_add_evidence", add_evidence)

    mutation = await MemoryService.merge_dreaming(
        db,
        owner_id=owner_id,
        memory_ids=[first.id, second.id],
        expected_revisions={first.id: 2, second.id: 4},
        expected_audience={
            first.id: MemoryAudience.BOTH,
            second.id: MemoryAudience.PRIVATE_ONLY,
        },
        content=canonical,
        category=MemoryCategory.IDENTITY,
        trust_level=TrustLevel.PUBLIC,
        readable_by=TrustLevel.PRIVATE,
        session_id=uuid4(),
        dream_run_id=uuid4(),
        evidence_message_ids=[current_evidence],
    )

    assert mutation.item is second
    assert second.audience == "private_only"
    assert second.pinned is True
    assert second.revision == 5
    db.delete.assert_awaited_once_with(first)
    enqueue.assert_not_awaited()
    assert set(add_evidence.await_args.kwargs["message_ids"]) == {
        evidence_id,
        first_source,
        second_source,
        current_evidence,
    }


async def test_merge_stale_snapshot_changes_nothing() -> None:
    owner_id = uuid4()
    first = MemoryRecord(
        id=uuid4(),
        user_id=owner_id,
        source_type="manual",
        pinned=False,
        audience="both",
        category="fact",
        content="A",
        normalized_content_hash=normalize_content("A")[1],
        revision=2,
    )
    second = MemoryRecord(
        id=uuid4(),
        user_id=owner_id,
        source_type="manual",
        pinned=False,
        audience="both",
        category="fact",
        content="B",
        normalized_content_hash=normalize_content("B")[1],
        revision=3,
    )
    rows = Mock()
    rows.all.return_value = [first, second]
    db = AsyncMock(spec=AsyncSession)
    db.scalars.return_value = rows

    mutation = await MemoryService.merge_dreaming(
        db,
        owner_id=owner_id,
        memory_ids=[first.id, second.id],
        expected_revisions={first.id: 1, second.id: 3},
        expected_audience={
            first.id: MemoryAudience.BOTH,
            second.id: MemoryAudience.BOTH,
        },
        content="C",
        category=MemoryCategory.FACT,
        trust_level=TrustLevel.PUBLIC,
        readable_by=TrustLevel.PUBLIC,
        session_id=uuid4(),
        dream_run_id=uuid4(),
        evidence_message_ids=[uuid4()],
    )

    assert mutation.conflict is True
    assert (first.content, first.revision, second.content, second.revision) == ("A", 2, "B", 3)
    db.delete.assert_not_awaited()
    db.flush.assert_not_awaited()


async def test_merge_audience_conflict_leaves_survivor_untouched(monkeypatch) -> None:
    owner_id = uuid4()
    first = MemoryRecord(
        id=uuid4(),
        user_id=owner_id,
        source_type="manual",
        pinned=False,
        audience="public_only",
        category="fact",
        content="A",
        normalized_content_hash=normalize_content("A")[1],
        revision=2,
    )
    second = MemoryRecord(
        id=uuid4(),
        user_id=owner_id,
        source_type="manual",
        pinned=False,
        audience="both",
        category="fact",
        content="B",
        normalized_content_hash=normalize_content("B")[1],
        revision=3,
    )
    rows = Mock()
    rows.all.return_value = [first, second]
    db = AsyncMock(spec=AsyncSession)
    db.scalars.return_value = rows
    db.scalar.return_value = None
    monkeypatch.setattr(MemoryService, "_validate_source_references", AsyncMock())
    before = [
        (
            item.content,
            item.normalized_content_hash,
            item.category,
            item.audience,
            item.revision,
        )
        for item in (first, second)
    ]

    mutation = await MemoryService.merge_dreaming(
        db,
        owner_id=owner_id,
        memory_ids=[first.id, second.id],
        expected_revisions={first.id: 2, second.id: 3},
        expected_audience={
            first.id: MemoryAudience.PUBLIC_ONLY,
            second.id: MemoryAudience.BOTH,
        },
        content="C",
        category=MemoryCategory.PREFERENCE,
        trust_level=TrustLevel.PRIVATE,
        readable_by=TrustLevel.PUBLIC,
        session_id=uuid4(),
        dream_run_id=uuid4(),
        evidence_message_ids=[uuid4()],
    )

    assert mutation.conflict is True
    assert [
        (
            item.content,
            item.normalized_content_hash,
            item.category,
            item.audience,
            item.revision,
        )
        for item in (first, second)
    ] == before
    db.flush.assert_not_awaited()
    db.delete.assert_not_awaited()


async def test_approved_update_increments_run_counter(monkeypatch) -> None:
    user_id = uuid4()
    session = ChatSession(
        id=uuid4(),
        user_id=user_id,
        title="Quelle",
        memory_mode="full",
        session_type="chat",
        trust_level="public",
    )
    run = MemoryDreamRun(id=uuid4(), session_id=session.id)
    evidence_id = uuid4()
    target = MemoryRecord(
        id=uuid4(),
        user_id=user_id,
        source_type="manual",
        pinned=True,
        audience="both",
        category="preference",
        content="Ich esse gelegentlich Hähnchen.",
        normalized_content_hash=normalize_content("Ich esse gelegentlich Hähnchen.")[1],
        revision=3,
    )
    payload = MemoryApprovalPayload.model_validate(
        {
            "schemaVersion": 3,
            "targetMemories": [
                {
                    "id": str(target.id),
                    "revision": 3,
                    "content": target.content,
                    "category": "preference",
                    "audience": "both",
                    "pinned": True,
                    "evidenceHash": "0" * 64,
                }
            ],
            "after": {
                "content": "Ich esse grundsätzlich kein Hähnchen.",
                "category": "preference",
                "audience": "both",
            },
            "sourceDreamRunId": str(run.id),
            "sourceSessionId": str(session.id),
            "evidenceMessageIds": [str(evidence_id)],
            "trustLevel": "public",
            "readableBy": "public",
            "updatePolicy": "ask",
            "removePolicy": "ask",
        }
    )
    approval = ApprovalRequest(
        request_type="memory_change",
        user_id=user_id,
        action_kind="memory_update",
        payload=payload.model_dump(by_alias=True, mode="json", exclude_none=True),
    )
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = 0

    async def get(model_type, _item_id, **_kwargs):
        return session if model_type is ChatSession else run

    db.get.side_effect = get
    rows = Mock()
    rows.all.return_value = [target]
    db.scalars.return_value = rows
    monkeypatch.setattr(
        MemoryService, "_valid_source_evidence", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        MemoryService, "_snapshot_matches", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        SettingsStore,
        "load",
        AsyncMock(side_effect=lambda _db, kind, **kwargs: MemorySettings(dreaming_update_policy="ask") if kind is MemorySettings else kind()),
    )
    update = AsyncMock(return_value=MemoryMutation(target, updated=True))
    monkeypatch.setattr(MemoryService, "update_dreaming", update)

    outcome = await MemoryService.apply_approved_change(
        db, approval=approval, owner_id=user_id
    )

    assert outcome == "applied"
    update.assert_awaited_once()
    assert target.pinned is True
    assert run.updated_memory_count == 1
    assert (run.removed_memory_count or 0) == 0
    db.commit.assert_not_awaited()


@pytest.mark.parametrize(
    ("snapshot_current", "expected"),
    [(True, "applied"), (False, "target_changed")],
)
async def test_approved_remove_revalidates_snapshot_and_updates_run_counter(
    monkeypatch, snapshot_current, expected
) -> None:
    user_id = uuid4()
    session = ChatSession(
        id=uuid4(), user_id=user_id, memory_mode="full", session_type="chat", trust_level="public"
    )
    run = MemoryDreamRun(id=uuid4(), session_id=session.id)
    target = MemoryRecord(
        id=uuid4(),
        user_id=user_id,
        source_type="manual",
        pinned=False,
        audience="both",
        category="fact",
        content="Der Nutzer lebt in Berlin.",
        revision=2,
    )
    payload = MemoryApprovalPayload.model_validate(
        {
            "schemaVersion": 3,
            "targetMemories": [
                {
                    "id": str(target.id),
                    "revision": 2,
                    "content": target.content,
                    "category": "fact",
                    "audience": "both",
                    "pinned": False,
                    "evidenceHash": "0" * 64,
                }
            ],
            "sourceDreamRunId": str(run.id),
            "sourceSessionId": str(session.id),
            "evidenceMessageIds": [str(uuid4())],
            "trustLevel": "public",
            "readableBy": "public",
            "updatePolicy": "auto",
            "removePolicy": "ask",
        }
    )
    approval = ApprovalRequest(
        request_type="memory_change",
        user_id=user_id,
        action_kind="memory_remove",
        payload=payload.model_dump(by_alias=True, mode="json", exclude_none=True),
    )
    db = AsyncMock(spec=AsyncSession)

    async def get(model_type, _item_id, **_kwargs):
        return session if model_type is ChatSession else run

    db.get.side_effect = get
    rows = Mock()
    rows.all.return_value = [target]
    db.scalars.return_value = rows
    monkeypatch.setattr(
        MemoryService, "_valid_explicit_user_evidence", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        MemoryService,
        "_snapshot_matches",
        AsyncMock(return_value=snapshot_current),
    )
    monkeypatch.setattr(
        SettingsStore,
        "load",
        AsyncMock(side_effect=lambda _db, kind, **kwargs: MemorySettings(dreaming_remove_policy="ask") if kind is MemorySettings else kind()),
    )

    outcome = await MemoryService.apply_approved_change(
        db, approval=approval, owner_id=user_id
    )

    assert outcome == expected
    assert db.delete.await_count == (1 if snapshot_current else 0)
    assert (run.updated_memory_count or 0) == 0
    assert (run.removed_memory_count or 0) == int(snapshot_current)
    db.commit.assert_not_awaited()


@pytest.mark.parametrize(
    ("update_policy", "remove_policy", "expected"),
    [
        ("auto", "auto", "policy_changed"),
        ("ask", "auto", "applied"),
        ("auto", "ask", "applied"),
        ("ask", "ask", "applied"),
        ("disabled", "auto", "policy_changed"),
        ("auto", "disabled", "policy_changed"),
    ],
)
async def test_approved_merge_revalidates_policy_and_updates_run_counters(
    monkeypatch, update_policy, remove_policy, expected
) -> None:
    user_id = uuid4()
    session = ChatSession(
        id=uuid4(), user_id=user_id, memory_mode="full", session_type="chat", trust_level="public"
    )
    run = MemoryDreamRun(id=uuid4(), session_id=session.id)
    targets = [
        MemoryRecord(
            id=uuid4(),
            user_id=user_id,
            source_type="manual",
            pinned=False,
            audience="both",
            category="fact",
            content=content,
            revision=1,
        )
        for content in ("A", "B")
    ]
    payload = MemoryApprovalPayload.model_validate(
        {
            "schemaVersion": 3,
            "targetMemories": [
                {
                    "id": str(item.id),
                    "revision": 1,
                    "content": item.content,
                    "category": "fact",
                    "audience": "both",
                    "pinned": False,
                    "evidenceHash": "0" * 64,
                }
                for item in targets
            ],
            "after": {
                "content": "A und B",
                "category": "fact",
                "audience": "both",
            },
            "sourceDreamRunId": str(run.id),
            "sourceSessionId": str(session.id),
            "evidenceMessageIds": [str(uuid4())],
            "trustLevel": "public",
            "readableBy": "public",
            "updatePolicy": update_policy,
            "removePolicy": remove_policy,
        }
    )
    approval = ApprovalRequest(
        request_type="memory_change",
        user_id=user_id,
        action_kind="memory_merge",
        payload=payload.model_dump(by_alias=True, mode="json", exclude_none=True),
    )
    db = AsyncMock(spec=AsyncSession)

    async def get(model_type, _item_id, **_kwargs):
        return session if model_type is ChatSession else run

    db.get.side_effect = get
    rows = Mock()
    rows.all.return_value = sorted(targets, key=lambda item: item.id)
    db.scalars.return_value = rows
    monkeypatch.setattr(
        MemoryService, "_valid_source_evidence", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        MemoryService, "_snapshot_matches", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        SettingsStore,
        "load",
        AsyncMock(
            side_effect=lambda _db, kind, **kwargs: MemorySettings(
                dreaming_update_policy=update_policy,
                dreaming_remove_policy=remove_policy,
            ) if kind is MemorySettings else kind()
        ),
    )
    merge = AsyncMock(return_value=MemoryMutation(targets[0], updated=True))
    monkeypatch.setattr(MemoryService, "merge_dreaming", merge)

    outcome = await MemoryService.apply_approved_change(
        db, approval=approval, owner_id=user_id
    )

    assert outcome == expected
    assert merge.await_count == (1 if expected == "applied" else 0)
    assert (run.updated_memory_count or 0) == int(expected == "applied")
    assert (run.removed_memory_count or 0) == int(expected == "applied")
