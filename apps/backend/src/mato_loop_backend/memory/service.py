from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Load, load_only, selectinload

from mato_loop_backend.memory.indexing import MemoryIndexService
from mato_loop_backend.models import (
    AgentRun,
    ApprovalRequest,
    ChatSession,
    MemoryAudience,
    MemoryCategory,
    MemoryDreamRun,
    MemoryMode,
    MemoryRecord,
    MemorySourceMessage,
    MemorySourceType,
    Message,
)
from mato_loop_backend.notifications import NotificationService
from mato_loop_backend.settings import (
    MemorySettings,
    SettingsStore,
    ToolExecutionPolicy,
    TrustLevel,
)

_MAX_LEXICAL_TERMS = 20
_MAX_LEXICAL_TERM_CHARACTERS = 256
_MEMORY_APPROVAL_SCHEMA_VERSION = 3


class MemoryApprovalTarget(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    id: UUID
    revision: int = Field(ge=1)
    content: str = Field(min_length=1, max_length=20_000)
    category: MemoryCategory
    audience: MemoryAudience
    pinned: bool
    evidence_hash: str = Field(
        pattern=r"^[0-9a-f]{64}$",
        validation_alias=AliasChoices("evidenceHash", "evidence_hash"),
        serialization_alias="evidenceHash",
    )


class MemoryApprovalAfter(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    content: str = Field(min_length=1, max_length=20_000)
    category: MemoryCategory
    audience: MemoryAudience


class MemoryApprovalEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    content: str = Field(min_length=1, max_length=20_000)
    category: MemoryCategory
    audience: MemoryAudience
    pinned: bool = Field(strict=True)


class MemoryApprovalPayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    schema_version: Literal[3] = Field(
        validation_alias=AliasChoices("schemaVersion", "schema_version"),
        serialization_alias="schemaVersion",
    )
    target_memories: list[MemoryApprovalTarget] = Field(
        min_length=1,
        max_length=50,
        validation_alias=AliasChoices("targetMemories", "target_memories"),
        serialization_alias="targetMemories",
    )
    after: MemoryApprovalAfter | None = None
    source_dream_run_id: UUID = Field(
        validation_alias=AliasChoices("sourceDreamRunId", "source_dream_run_id"),
        serialization_alias="sourceDreamRunId",
    )
    source_session_id: UUID = Field(
        validation_alias=AliasChoices("sourceSessionId", "source_session_id"),
        serialization_alias="sourceSessionId",
    )
    evidence_message_ids: list[UUID] = Field(
        min_length=1,
        max_length=100,
        validation_alias=AliasChoices("evidenceMessageIds", "evidence_message_ids"),
        serialization_alias="evidenceMessageIds",
    )
    trust_level: TrustLevel = Field(
        validation_alias=AliasChoices("trustLevel", "trust_level"),
        serialization_alias="trustLevel",
    )
    readable_by: TrustLevel = Field(
        validation_alias=AliasChoices("readableBy", "readable_by"),
        serialization_alias="readableBy",
    )
    update_policy: ToolExecutionPolicy = Field(
        validation_alias=AliasChoices("updatePolicy", "update_policy"),
        serialization_alias="updatePolicy",
    )
    remove_policy: ToolExecutionPolicy = Field(
        validation_alias=AliasChoices("removePolicy", "remove_policy"),
        serialization_alias="removePolicy",
    )

    @model_validator(mode="after")
    def validate_targets(self) -> MemoryApprovalPayload:
        if len(self.target_memories) != len({item.id for item in self.target_memories}):
            raise ValueError("targetMemories must be unique")
        if len(self.evidence_message_ids) != len(set(self.evidence_message_ids)):
            raise ValueError("evidenceMessageIds must be unique")
        return self


def normalize_content(content: str) -> tuple[str, str]:
    normalized = re.sub(r"\s+", " ", content.strip()).casefold()
    return normalized, hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def memory_content_hash_exclusion(content_hashes: frozenset[str] | set[str]) -> object:
    return MemoryRecord.normalized_content_hash.not_in(sorted(content_hashes))


def memory_visible_to(
    audience: MemoryAudience | str, readable_by: TrustLevel
) -> bool:
    audience = MemoryAudience(audience)
    return audience == MemoryAudience.BOTH or audience.value.endswith(
        f"{readable_by.value}_only"
    )


def memory_audience_for_source(trust_level: TrustLevel) -> MemoryAudience:
    return (
        MemoryAudience.PRIVATE_ONLY
        if trust_level == TrustLevel.PRIVATE
        else MemoryAudience.BOTH
    )


def memory_audience_intersection(
    audiences: Iterable[MemoryAudience | str],
) -> MemoryAudience | None:
    visible_to = {TrustLevel.PUBLIC, TrustLevel.PRIVATE}
    for audience in audiences:
        visible_to = {
            trust for trust in visible_to if memory_visible_to(audience, trust)
        }
    if visible_to == {TrustLevel.PUBLIC, TrustLevel.PRIVATE}:
        return MemoryAudience.BOTH
    if visible_to == {TrustLevel.PUBLIC}:
        return MemoryAudience.PUBLIC_ONLY
    if visible_to == {TrustLevel.PRIVATE}:
        return MemoryAudience.PRIVATE_ONLY
    return None


def memory_visibility_filter(readable_by: TrustLevel) -> object:
    return MemoryRecord.audience.in_(
        [MemoryAudience.BOTH.value, f"{readable_by.value}_only"]
    )


def memory_lexical_terms(content: str) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for term in re.findall(r"\w+", content.casefold()):
        if len(term) > _MAX_LEXICAL_TERM_CHARACTERS or term in seen:
            continue
        seen.add(term)
        terms.append(term)
        if len(terms) == _MAX_LEXICAL_TERMS:
            break
    return terms


def memory_reference_options(owner_id: UUID) -> tuple[Load, Load, Load]:
    owned_message_ids = select(Message.id).join(ChatSession).where(ChatSession.user_id == owner_id)
    return (
        selectinload(
            MemoryRecord.source_session.and_(ChatSession.user_id == owner_id)
        ),
        selectinload(
            MemoryRecord.source_message.and_(Message.id.in_(owned_message_ids))
        ).options(
            load_only(Message.created_at),
            selectinload(Message.attachments),
        ),
        selectinload(
            MemoryRecord.source_messages.and_(
                MemorySourceMessage.message_id.in_(owned_message_ids)
            )
        ),
    )


def memory_reference_timestamps(item: MemoryRecord) -> dict[str, str | None]:
    return {
        "sourceMessageAt": (
            item.source_message.created_at.isoformat()
            if item.source_message and item.source_message.created_at
            else None
        ),
        "memoryCreatedAt": item.created_at.isoformat() if item.created_at else None,
        "memoryUpdatedAt": item.updated_at.isoformat() if item.updated_at else None,
    }


@dataclass(frozen=True, slots=True)
class MemoryList:
    items: list[MemoryRecord]
    total: int
    offset: int
    limit: int


@dataclass(frozen=True, slots=True)
class MemoryMutation:
    item: MemoryRecord | None
    created: bool = False
    updated: bool = False
    duplicate: bool = False
    conflict: bool = False


@dataclass(frozen=True, slots=True)
class MemoryScope:
    total: int
    pinned: int


class MemoryDuplicateError(Exception):
    pass


class MemoryService:
    @staticmethod
    async def create_manual(
        db: AsyncSession,
        *,
        owner_id: UUID,
        content: str,
        category: MemoryCategory,
        audience: MemoryAudience,
    ) -> MemoryRecord:
        _, content_hash = normalize_content(content)
        if await MemoryService._duplicate_by_hash(db, owner_id=owner_id, content_hash=content_hash) is not None:
            raise MemoryDuplicateError
        try:
            async with db.begin_nested():
                item = await MemoryService._create(
                    db,
                    owner_id=owner_id,
                    content=content,
                    category=category,
                    source_type=MemorySourceType.MANUAL,
                    audience=audience,
                )
                return item
        except IntegrityError as exc:
            duplicate_content = await MemoryService._duplicate_by_hash(
                db, owner_id=owner_id, content_hash=content_hash
            )
            if duplicate_content is None:
                raise
            raise MemoryDuplicateError from exc

    @staticmethod
    async def create_tool(
        db: AsyncSession,
        *,
        owner_id: UUID,
        content: str,
        category: MemoryCategory,
        trust_level: TrustLevel,
        session_id: UUID,
        message_id: UUID,
        run_id: UUID,
        tool_call_id: str,
    ) -> MemoryMutation:
        existing = await db.scalar(
            select(MemoryRecord).where(
                MemoryRecord.user_id == owner_id,
                MemoryRecord.source_type == MemorySourceType.TOOL_CALL.value,
                MemoryRecord.source_run_id == run_id,
                MemoryRecord.source_tool_call_id == tool_call_id,
            )
        )
        if existing is not None:
            return MemoryMutation(existing, created=True)
        _, content_hash = normalize_content(content)
        duplicate = await MemoryService._duplicate_by_hash(
            db, owner_id=owner_id, content_hash=content_hash
        )
        if duplicate is not None:
            return MemoryMutation(duplicate, duplicate=True)
        try:
            async with db.begin_nested():
                item = await MemoryService._create(
                    db,
                    owner_id=owner_id,
                    content=content,
                    category=category,
                    source_type=MemorySourceType.TOOL_CALL,
                    audience=memory_audience_for_source(trust_level),
                    session_id=session_id,
                    message_id=message_id,
                    run_id=run_id,
                    tool_call_id=tool_call_id,
                    evidence_message_ids=[message_id],
                )
                return MemoryMutation(item, created=True)
        except IntegrityError:
            replay = await db.scalar(
                select(MemoryRecord).where(
                    MemoryRecord.user_id == owner_id,
                    MemoryRecord.source_type == MemorySourceType.TOOL_CALL.value,
                    MemoryRecord.source_run_id == run_id,
                    MemoryRecord.source_tool_call_id == tool_call_id,
                )
            )
            if replay is not None:
                return MemoryMutation(replay, created=True)
            duplicate = await MemoryService._duplicate_by_hash(
                db, owner_id=owner_id, content_hash=content_hash
            )
            if duplicate is None:
                raise
            return MemoryMutation(duplicate, duplicate=True)

    @staticmethod
    async def update_tool(
        db: AsyncSession,
        *,
        owner_id: UUID,
        memory_id: UUID,
        expected_revision: int,
        content: str,
        category: MemoryCategory,
        readable_by: TrustLevel,
        write_trust_level: TrustLevel,
        session_id: UUID,
        evidence_message_id: UUID,
        run_id: UUID,
        tool_call_id: str,
    ) -> MemoryMutation:
        await MemoryService._validate_source_references(
            db,
            owner_id=owner_id,
            session_id=session_id,
            message_id=evidence_message_id,
            run_id=run_id,
            dream_run_id=None,
        )
        item = await MemoryService._get_for_automatic_update(
            db, owner_id=owner_id, memory_id=memory_id, readable_by=readable_by
        )
        if item is None:
            return MemoryMutation(None)
        if item.revision != expected_revision:
            return MemoryMutation(item, conflict=True)
        target_audience = memory_audience_intersection(
            [item.audience, memory_audience_for_source(write_trust_level)]
        )
        if target_audience is None:
            return MemoryMutation(item, conflict=True)
        try:
            await MemoryService.update(
                db,
                item,
                owner_id=owner_id,
                content=content,
                category=category,
                audience=target_audience,
                evidence_message_ids=[evidence_message_id],
                source_type=MemorySourceType.TOOL_CALL,
                source_session_id=session_id,
                source_message_id=evidence_message_id,
                source_run_id=run_id,
                source_tool_call_id=tool_call_id,
            )
        except MemoryDuplicateError:
            _, content_hash = normalize_content(content)
            duplicate = await MemoryService._duplicate_by_hash(
                db,
                owner_id=owner_id,
                content_hash=content_hash,
                exclude_id=item.id,
                for_update=True,
            )
            if duplicate is None:
                raise
            source_evidence = list(
                (
                    await db.scalars(
                        select(MemorySourceMessage.message_id).where(
                            MemorySourceMessage.memory_id == item.id,
                            MemorySourceMessage.message_id.in_(
                                select(Message.id)
                                .join(ChatSession)
                                .where(ChatSession.user_id == owner_id)
                            ),
                        )
                    )
                ).all()
            )
            if item.source_message_id is not None:
                source_evidence.append(item.source_message_id)
            source_evidence.append(evidence_message_id)
            merged_audience = memory_audience_intersection(
                [item.audience, duplicate.audience, target_audience]
            )
            if merged_audience is None:
                return MemoryMutation(item, conflict=True)
            await MemoryService._merge_automatic_metadata(
                db,
                duplicate,
                owner_id=owner_id,
                source_audience=merged_audience,
                evidence_message_ids=source_evidence,
            )
            duplicate.source_type = MemorySourceType.TOOL_CALL.value
            duplicate.source_session_id = session_id
            duplicate.source_message_id = evidence_message_id
            duplicate.source_run_id = run_id
            duplicate.source_dream_run_id = None
            duplicate.source_tool_call_id = tool_call_id
            await db.delete(item)
            await db.flush()
            return MemoryMutation(duplicate, updated=True)
        return MemoryMutation(item, updated=True)

    @staticmethod
    async def create_dreaming(
        db: AsyncSession,
        *,
        owner_id: UUID,
        content: str,
        category: MemoryCategory,
        trust_level: TrustLevel,
        session_id: UUID,
        run_id: UUID,
        evidence_message_ids: list[UUID],
    ) -> MemoryMutation:
        _, content_hash = normalize_content(content)
        existing = await MemoryService._duplicate_by_hash(
            db, owner_id=owner_id, content_hash=content_hash, for_update=True
        )
        if existing is not None:
            await MemoryService._merge_automatic_metadata(
                db,
                existing,
                owner_id=owner_id,
                source_audience=memory_audience_for_source(trust_level),
                evidence_message_ids=evidence_message_ids,
            )
            return MemoryMutation(existing, duplicate=True)
        try:
            async with db.begin_nested():
                item = await MemoryService._create(
                    db,
                    owner_id=owner_id,
                    content=content,
                    category=category,
                    source_type=MemorySourceType.DREAMING,
                    audience=memory_audience_for_source(trust_level),
                    session_id=session_id,
                    message_id=(evidence_message_ids[0] if evidence_message_ids else None),
                    dream_run_id=run_id,
                    evidence_message_ids=evidence_message_ids,
                )
                return MemoryMutation(item, created=True)
        except IntegrityError:
            duplicate = await MemoryService._duplicate_by_hash(
                db, owner_id=owner_id, content_hash=content_hash, for_update=True
            )
            if duplicate is None:
                raise
            await MemoryService._merge_automatic_metadata(
                db,
                duplicate,
                owner_id=owner_id,
                source_audience=memory_audience_for_source(trust_level),
                evidence_message_ids=evidence_message_ids,
            )
            return MemoryMutation(duplicate, duplicate=True)

    @staticmethod
    async def update_dreaming(
        db: AsyncSession,
        *,
        owner_id: UUID,
        memory_id: UUID,
        expected_revision: int,
        content: str,
        category: MemoryCategory,
        trust_level: TrustLevel,
        session_id: UUID,
        dream_run_id: UUID,
        evidence_message_ids: list[UUID],
    ) -> MemoryMutation:
        item = await MemoryService._get_for_automatic_update(
            db, owner_id=owner_id, memory_id=memory_id, readable_by=trust_level
        )
        if item is None:
            return MemoryMutation(None)
        if item.revision != expected_revision:
            return MemoryMutation(item, conflict=True)
        target_audience = memory_audience_intersection(
            [item.audience, memory_audience_for_source(trust_level)]
        )
        if target_audience is None:
            return MemoryMutation(item, conflict=True)
        try:
            await MemoryService.update(
                db,
                item,
                owner_id=owner_id,
                content=content,
                category=category,
                audience=target_audience,
                evidence_message_ids=evidence_message_ids,
                source_type=MemorySourceType.DREAMING,
                source_session_id=session_id,
                source_message_id=(evidence_message_ids[0] if evidence_message_ids else None),
                source_dream_run_id=dream_run_id,
            )
        except MemoryDuplicateError:
            return MemoryMutation(item, conflict=True)
        return MemoryMutation(item, updated=True)

    @staticmethod
    async def merge_dreaming(
        db: AsyncSession,
        *,
        owner_id: UUID,
        memory_ids: list[UUID],
        expected_revisions: dict[UUID, int],
        expected_audience: dict[UUID, MemoryAudience],
        content: str,
        category: MemoryCategory,
        trust_level: TrustLevel,
        readable_by: TrustLevel,
        session_id: UUID,
        dream_run_id: UUID,
        evidence_message_ids: list[UUID],
    ) -> MemoryMutation:
        unique_ids = sorted(set(memory_ids), key=str)
        if len(unique_ids) < 2 or len(unique_ids) > 50:
            return MemoryMutation(None, conflict=True)
        items = list(
            (
                await db.scalars(
                    select(MemoryRecord)
                    .where(
                        MemoryRecord.user_id == owner_id,
                        MemoryRecord.id.in_(unique_ids),
                    )
                    .order_by(MemoryRecord.id)
                    .execution_options(populate_existing=True)
                    .with_for_update()
                )
            ).all()
        )
        if {item.id for item in items} != set(unique_ids) or any(
            item.revision != expected_revisions.get(item.id)
            or MemoryAudience(item.audience) != expected_audience.get(item.id)
            or not memory_visible_to(item.audience, readable_by)
            for item in items
        ):
            return MemoryMutation(None, conflict=True)

        cleaned = content.strip()
        if not cleaned:
            raise ValueError("memory content must not be empty")
        await MemoryService._validate_source_references(
            db,
            owner_id=owner_id,
            session_id=session_id,
            message_id=evidence_message_ids[0] if evidence_message_ids else None,
            run_id=None,
            dream_run_id=dream_run_id,
        )
        _, content_hash = normalize_content(cleaned)
        matching = [item for item in items if item.normalized_content_hash == content_hash]
        survivor = min(matching or items, key=lambda item: str(item.id))
        duplicate = await MemoryService._duplicate_by_hash(
            db,
            owner_id=owner_id,
            content_hash=content_hash,
            exclude_id=survivor.id,
            for_update=True,
        )
        if duplicate is not None and duplicate.id not in set(unique_ids):
            return MemoryMutation(survivor, conflict=True)

        linked_ids = list(
            (
                await db.scalars(
                    select(MemorySourceMessage.message_id).where(
                        MemorySourceMessage.memory_id.in_(unique_ids),
                        MemorySourceMessage.message_id.in_(
                            select(Message.id)
                            .join(ChatSession)
                            .where(ChatSession.user_id == owner_id)
                        ),
                    )
                )
            ).all()
        )
        all_evidence = [
            *linked_ids,
            *(item.source_message_id for item in items if item.source_message_id is not None),
            *evidence_message_ids,
        ]
        content_changed = survivor.normalized_content_hash != content_hash
        resulting_audience = memory_audience_intersection(
            [
                *(item.audience for item in items),
                memory_audience_for_source(trust_level),
            ]
        )
        if resulting_audience is None:
            return MemoryMutation(survivor, conflict=True)
        survivor.content = cleaned
        survivor.normalized_content_hash = content_hash
        survivor.category = category.value
        survivor.audience = resulting_audience.value
        survivor.pinned = any(item.pinned for item in items)
        survivor.source_type = MemorySourceType.DREAMING.value
        survivor.source_session_id = session_id
        survivor.source_message_id = evidence_message_ids[0] if evidence_message_ids else None
        survivor.source_run_id = None
        survivor.source_dream_run_id = dream_run_id
        survivor.source_tool_call_id = None
        survivor.revision = (survivor.revision or 1) + 1
        await db.flush()
        await MemoryService._add_evidence(
            db, owner_id=owner_id, memory_id=survivor.id, message_ids=all_evidence
        )
        if content_changed:
            await MemoryIndexService.enqueue(db, survivor)
        for item in items:
            if item.id != survivor.id:
                await db.delete(item)
        await db.flush()
        return MemoryMutation(survivor, updated=True)

    @staticmethod
    async def keep_dreaming(
        db: AsyncSession,
        *,
        owner_id: UUID,
        memory_id: UUID,
        expected_revision: int,
        trust_level: TrustLevel,
        evidence_message_ids: list[UUID],
    ) -> MemoryMutation:
        item = await MemoryService._get_for_automatic_update(
            db, owner_id=owner_id, memory_id=memory_id, readable_by=trust_level
        )
        if item is None:
            return MemoryMutation(None)
        if item.revision != expected_revision:
            return MemoryMutation(item, conflict=True)
        await MemoryService._merge_automatic_metadata(
            db,
            item,
            owner_id=owner_id,
            source_audience=memory_audience_for_source(trust_level),
            evidence_message_ids=evidence_message_ids,
        )
        return MemoryMutation(item)

    @staticmethod
    async def remove_dreaming(
        db: AsyncSession,
        *,
        owner_id: UUID,
        snapshot: MemoryApprovalTarget,
        trust_level: TrustLevel,
        session_id: UUID,
        dream_run_id: UUID,
        evidence_message_ids: list[UUID],
    ) -> MemoryMutation:
        await MemoryService._validate_source_references(
            db,
            owner_id=owner_id,
            session_id=session_id,
            message_id=evidence_message_ids[0] if evidence_message_ids else None,
            run_id=None,
            dream_run_id=dream_run_id,
        )
        if not await MemoryService._valid_explicit_user_evidence(
            db,
            owner_id=owner_id,
            source_session_id=session_id,
            evidence_message_ids=evidence_message_ids,
        ):
            return MemoryMutation(None, conflict=True)
        item = await db.scalar(
            select(MemoryRecord)
            .where(
                MemoryRecord.id == snapshot.id,
                MemoryRecord.user_id == owner_id,
                memory_visibility_filter(trust_level),
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if item is None or not await MemoryService._snapshot_matches(
            db, item, snapshot=snapshot, owner_id=owner_id
        ):
            return MemoryMutation(item, conflict=True)
        await db.delete(item)
        await db.flush()
        return MemoryMutation(item, updated=True)

    @staticmethod
    async def reconciliation_candidates(
        db: AsyncSession,
        *,
        owner_id: UUID,
        candidates: list[tuple[str, MemoryCategory]],
        readable_by: TrustLevel,
        excluded_content_hashes: frozenset[str] | set[str] = frozenset(),
        max_items_per_candidate: int = 5,
        max_characters: int = 40_000,
    ) -> list[list[MemoryRecord]]:
        trust_filters = [
            MemoryRecord.user_id == owner_id,
            memory_visibility_filter(readable_by),
        ]
        if excluded_content_hashes:
            trust_filters.append(memory_content_hash_exclusion(excluded_content_hashes))
        result: list[list[MemoryRecord]] = []
        used_characters = 0
        for content, _category in candidates:
            selected: dict[UUID, MemoryRecord] = {}
            _, content_hash = normalize_content(content)
            exact = await db.scalar(
                select(MemoryRecord).where(
                    MemoryRecord.normalized_content_hash == content_hash,
                    *trust_filters,
                ).options(*memory_reference_options(owner_id))
            )
            if exact is not None:
                result.append([exact])
                continue

            terms = memory_lexical_terms(content)
            if terms:
                tsquery = func.to_tsquery("simple", " | ".join(terms))
                lexical = list(
                    (
                        await db.scalars(
                            select(MemoryRecord)
                            .options(*memory_reference_options(owner_id))
                            .where(
                                MemoryRecord.search_vector.op("@@")(tsquery),
                                *trust_filters,
                            )
                            .order_by(
                                func.ts_rank_cd(MemoryRecord.search_vector, tsquery).desc(),
                                MemoryRecord.updated_at.desc(),
                            )
                            .limit(5)
                        )
                    ).all()
                )
                selected.update((item.id, item) for item in lexical)
            local: list[MemoryRecord] = []
            for item in selected.values():
                if len(local) >= max_items_per_candidate:
                    break
                item_characters = len(item.content) + 200
                if used_characters + item_characters > max_characters:
                    continue
                local.append(item)
                used_characters += item_characters
            result.append(local)
        return result

    @staticmethod
    async def _evidence_hash(
        db: AsyncSession, item: MemoryRecord, *, owner_id: UUID
    ) -> str:
        if item.user_id != owner_id:
            raise ValueError("memory owner mismatch")
        message_ids = list(
            (
                await db.scalars(
                    select(MemorySourceMessage.message_id).where(
                        MemorySourceMessage.memory_id == item.id,
                        MemorySourceMessage.message_id.in_(
                            select(Message.id)
                            .join(ChatSession)
                            .where(ChatSession.user_id == owner_id)
                        ),
                    )
                )
            ).all()
        )
        if item.source_message_id is not None and await db.scalar(
            select(Message.id)
            .join(ChatSession)
            .where(
                Message.id == item.source_message_id,
                ChatSession.user_id == owner_id,
            )
        ):
            message_ids.append(item.source_message_id)
        serialized = "\n".join(sorted({str(message_id) for message_id in message_ids}))
        return hashlib.sha256(serialized.encode("ascii")).hexdigest()

    @staticmethod
    async def _approval_target(
        db: AsyncSession, item: MemoryRecord, *, owner_id: UUID
    ) -> MemoryApprovalTarget:
        return MemoryApprovalTarget(
            id=item.id,
            revision=item.revision,
            content=item.content,
            category=MemoryCategory(item.category),
            audience=MemoryAudience(item.audience),
            pinned=item.pinned,
            evidenceHash=await MemoryService._evidence_hash(
                db, item, owner_id=owner_id
            ),
        )

    @staticmethod
    async def _snapshot_matches(
        db: AsyncSession,
        item: MemoryRecord,
        *,
        snapshot: MemoryApprovalTarget,
        owner_id: UUID,
    ) -> bool:
        return (
            item.user_id == owner_id
            and item.id == snapshot.id
            and item.revision == snapshot.revision
            and item.content == snapshot.content
            and item.category == snapshot.category.value
            and item.audience == snapshot.audience.value
            and item.pinned == snapshot.pinned
            and await MemoryService._evidence_hash(db, item, owner_id=owner_id)
            == snapshot.evidence_hash
        )

    @staticmethod
    async def request_dreaming_approval(
        db: AsyncSession,
        *,
        action: Literal["update", "merge", "remove"],
        owner_id: UUID,
        source_session_id: UUID,
        source_dream_run_id: UUID,
        targets: list[MemoryRecord],
        after_content: str | None,
        after_category: MemoryCategory | None,
        trust_level: TrustLevel,
        readable_by: TrustLevel,
        evidence_message_ids: list[UUID],
        update_policy: ToolExecutionPolicy,
        remove_policy: ToolExecutionPolicy,
        created: list[ApprovalRequest] | None = None,
    ) -> ApprovalRequest:
        ordered_targets = sorted(targets, key=lambda item: str(item.id))
        if not ordered_targets or any(item.user_id != owner_id for item in ordered_targets):
            raise ValueError("memory owner mismatch")
        if (action in {"update", "remove"} and len(ordered_targets) != 1) or (
            action == "merge" and len(ordered_targets) < 2
        ):
            raise ValueError("invalid memory approval targets")
        await MemoryService._validate_source_references(
            db,
            owner_id=owner_id,
            session_id=source_session_id,
            message_id=None,
            run_id=None,
            dream_run_id=source_dream_run_id,
        )
        await MemoryService._validate_evidence_owner(
            db, owner_id=owner_id, message_ids=evidence_message_ids
        )
        snapshots = [
            await MemoryService._approval_target(db, item, owner_id=owner_id)
            for item in ordered_targets
        ]
        resulting_audience = memory_audience_intersection(
            [
                *(item.audience for item in ordered_targets),
                memory_audience_for_source(trust_level),
            ]
        )
        if resulting_audience is None:
            raise ValueError("memory audiences do not intersect")
        payload = MemoryApprovalPayload(
            schemaVersion=_MEMORY_APPROVAL_SCHEMA_VERSION,
            targetMemories=snapshots,
            after=(
                MemoryApprovalAfter(
                    content=after_content,
                    category=after_category,
                    audience=resulting_audience,
                )
                if after_content is not None and after_category is not None
                else None
            ),
            sourceDreamRunId=source_dream_run_id,
            sourceSessionId=source_session_id,
            evidenceMessageIds=list(dict.fromkeys(evidence_message_ids)),
            trustLevel=trust_level,
            readableBy=readable_by,
            updatePolicy=update_policy,
            removePolicy=remove_policy,
        )
        action_kind = f"memory_{action}"
        target_key = [(str(item.id), item.revision) for item in snapshots]
        pending = list(
            (
                await db.scalars(
                    select(ApprovalRequest).where(
                        ApprovalRequest.user_id == owner_id,
                        ApprovalRequest.request_type == "memory_change",
                        ApprovalRequest.action_kind == action_kind,
                        ApprovalRequest.status == "pending",
                    )
                )
            ).all()
        )
        serialized_payload = payload.model_dump(by_alias=True, mode="json", exclude_none=True)
        for existing in pending:
            try:
                existing_payload = MemoryApprovalPayload.model_validate(existing.payload)
            except ValidationError:
                continue
            if (
                [
                    (str(item.id), item.revision)
                    for item in existing_payload.target_memories
                ]
                == target_key
                and existing_payload.model_dump(by_alias=True, mode="json", exclude_none=True)
                == serialized_payload
            ):
                return existing

        labels = {
            "update": ("Erinnerung ändern", "Dreaming schlägt eine Aktualisierung vor."),
            "merge": (
                "Erinnerungen zusammenführen",
                "Dreaming schlägt eine Zusammenführung vor.",
            ),
            "remove": (
                "Erinnerung entfernen",
                "Dreaming schlägt vor, diese Erinnerung zu entfernen.",
            ),
        }
        title, summary = labels[action]
        approval = ApprovalRequest(
            request_type="memory_change",
            user_id=owner_id,
            session_id=source_session_id,
            run_id=None,
            action_kind=action_kind,
            risk_level="high" if action in {"merge", "remove"} else "medium",
            title=title,
            reason="Von Dreaming vorgeschlagene Memory-Änderung",
            summary=summary,
            capability=f"memory.{action}",
            resource_type="memory",
            resource_scope=",".join(str(item.id) for item in snapshots),
            payload=serialized_payload,
            available_decisions=["approve", "deny"],
        )
        db.add(approval)
        await db.flush()
        await NotificationService.create_for_approval(db, approval)
        if created is not None:
            created.append(approval)
        return approval

    @staticmethod
    async def _valid_explicit_user_evidence(
        db: AsyncSession,
        *,
        owner_id: UUID,
        source_session_id: UUID,
        evidence_message_ids: list[UUID],
    ) -> bool:
        unique_ids = set(evidence_message_ids)
        if not unique_ids or len(unique_ids) != len(evidence_message_ids):
            return False
        messages = list(
            (
                await db.scalars(
                    select(Message)
                    .join(ChatSession)
                    .where(
                        Message.id.in_(unique_ids),
                        ChatSession.user_id == owner_id,
                    )
                )
            ).all()
        )
        return len(messages) == len(unique_ids) and all(
            item.session_id == source_session_id
            and item.role == "user"
            and item.content != ""
            and item.error_code is None
            and item.generation_status == "completed"
            for item in messages
        )

    @staticmethod
    async def _valid_source_evidence(
        db: AsyncSession, payload: MemoryApprovalPayload, *, owner_id: UUID
    ) -> bool:
        messages = list(
            (
                await db.scalars(
                    select(Message)
                    .join(ChatSession)
                    .where(
                        Message.id.in_(payload.evidence_message_ids),
                        ChatSession.user_id == owner_id,
                    )
                )
            ).all()
        )
        return (
            len(messages) == len(set(payload.evidence_message_ids))
            and any(item.role == "user" for item in messages)
            and all(
                item.session_id == payload.source_session_id
                and item.role in {"user", "assistant"}
                and item.content != ""
                and item.error_code is None
                and item.generation_status == "completed"
                for item in messages
            )
        )

    @staticmethod
    async def apply_approved_change(
        db: AsyncSession,
        approval: ApprovalRequest,
        *,
        owner_id: UUID,
        memory_edit: MemoryApprovalEdit | None = None,
    ) -> Literal["applied", "target_changed", "policy_changed"]:
        if approval.user_id != owner_id or approval.request_type != "memory_change":
            return "target_changed"
        try:
            payload = MemoryApprovalPayload.model_validate(approval.payload)
        except ValidationError:
            return "target_changed"
        action = {
            "memory_update": "update",
            "memory_merge": "merge",
            "memory_remove": "remove",
        }.get(approval.action_kind)
        if action is None:
            return "target_changed"
        if memory_edit is not None and action not in {"update", "merge"}:
            return "target_changed"
        session = await db.get(
            ChatSession, payload.source_session_id, with_for_update=True, populate_existing=True
        )
        dream_run = await db.get(
            MemoryDreamRun, payload.source_dream_run_id, with_for_update=True
        )
        if (
            session is None
            or session.user_id != owner_id
            or session.session_type != "chat"
            or session.parent_session_id is not None
            or dream_run is None
            or dream_run.session_id != session.id
        ):
            return "target_changed"
        from mato_loop_backend.sessions.trust import effective_session_trust

        if await effective_session_trust(db, session) != payload.trust_level:
            return "target_changed"
        evidence_valid = (
            await MemoryService._valid_explicit_user_evidence(
                db,
                owner_id=owner_id,
                source_session_id=payload.source_session_id,
                evidence_message_ids=payload.evidence_message_ids,
            )
            if action == "remove"
            else await MemoryService._valid_source_evidence(
                db, payload, owner_id=owner_id
            )
        )
        if not evidence_valid:
            return "target_changed"

        settings = await SettingsStore.load(
            db, MemorySettings, user_id=owner_id, for_update=True
        )
        if not settings.enabled or session.memory_mode != MemoryMode.FULL.value:
            return "policy_changed"
        policies_match = (
            settings.dreaming_update_policy == payload.update_policy
            and settings.dreaming_remove_policy == payload.remove_policy
        )
        approval_still_required = (
            action == "update"
            and settings.dreaming_update_policy == ToolExecutionPolicy.ASK
            or action == "remove"
            and settings.dreaming_remove_policy == ToolExecutionPolicy.ASK
            or action == "merge"
            and ToolExecutionPolicy.DISABLED
            not in {
                settings.dreaming_update_policy,
                settings.dreaming_remove_policy,
            }
            and ToolExecutionPolicy.ASK
            in {
                settings.dreaming_update_policy,
                settings.dreaming_remove_policy,
            }
        )
        if not policies_match or not approval_still_required:
            return "policy_changed"

        target_ids = sorted(
            {item.id for item in payload.target_memories}, key=str
        )
        targets = list(
            (
                await db.scalars(
                    select(MemoryRecord)
                    .where(
                        MemoryRecord.user_id == owner_id,
                        MemoryRecord.id.in_(target_ids),
                    )
                    .order_by(MemoryRecord.id)
                    .execution_options(populate_existing=True)
                    .with_for_update()
                )
            ).all()
        )
        snapshots = {item.id: item for item in payload.target_memories}
        if len(targets) != len(target_ids):
            return "target_changed"
        for item in targets:
            snapshot = snapshots[item.id]
            if (
                not memory_visible_to(item.audience, payload.readable_by)
                or not await MemoryService._snapshot_matches(
                    db, item, snapshot=snapshot, owner_id=owner_id
                )
            ):
                return "target_changed"

        if action == "remove":
            if len(targets) != 1 or payload.after is not None:
                return "target_changed"
            await db.delete(targets[0])
            await db.flush()
            dream_run.removed_memory_count = (dream_run.removed_memory_count or 0) + 1
            return "applied"

        if payload.after is None:
            return "target_changed"
        expected_audience = memory_audience_intersection(
            [
                *(item.audience for item in targets),
                memory_audience_for_source(payload.trust_level),
            ]
        )
        if payload.after.audience != expected_audience:
            return "target_changed"
        after = memory_edit if memory_edit is not None else payload.after
        try:
            async with db.begin_nested():
                if action == "update":
                    if len(targets) != 1:
                        return "target_changed"
                    mutation = await MemoryService.update_dreaming(
                        db,
                        owner_id=owner_id,
                        memory_id=targets[0].id,
                        expected_revision=targets[0].revision,
                        content=after.content,
                        category=after.category,
                        trust_level=payload.trust_level,
                        session_id=session.id,
                        dream_run_id=dream_run.id,
                        evidence_message_ids=payload.evidence_message_ids,
                    )
                else:
                    mutation = await MemoryService.merge_dreaming(
                        db,
                        owner_id=owner_id,
                        memory_ids=target_ids,
                        expected_revisions={item.id: item.revision for item in targets},
                        expected_audience={
                            item.id: MemoryAudience(item.audience) for item in targets
                        },
                        content=after.content,
                        category=after.category,
                        trust_level=payload.trust_level,
                        readable_by=payload.readable_by,
                        session_id=session.id,
                        dream_run_id=dream_run.id,
                        evidence_message_ids=payload.evidence_message_ids,
                    )
                if mutation.item is None or mutation.conflict:
                    return "target_changed"
                if memory_edit is not None:
                    # Owner-only overrides; model mutations keep their audience and pin rules.
                    # Duplicate protection is owner-wide, independent of the final audience.
                    mutation.item.audience = memory_edit.audience.value
                    mutation.item.pinned = memory_edit.pinned
                await db.flush()
        except IntegrityError:
            _, content_hash = normalize_content(after.content)
            duplicate = await MemoryService._duplicate_by_hash(
                db, owner_id=owner_id, content_hash=content_hash
            )
            if duplicate is None or duplicate.id in target_ids:
                raise
            return "target_changed"
        dream_run.updated_memory_count = (dream_run.updated_memory_count or 0) + 1
        if action == "merge":
            dream_run.removed_memory_count = (
                dream_run.removed_memory_count or 0
            ) + len(targets) - 1
        return "applied"

    @staticmethod
    async def _create(
        db: AsyncSession,
        *,
        owner_id: UUID,
        content: str,
        category: MemoryCategory,
        source_type: MemorySourceType,
        audience: MemoryAudience,
        session_id: UUID | None = None,
        message_id: UUID | None = None,
        run_id: UUID | None = None,
        dream_run_id: UUID | None = None,
        tool_call_id: str | None = None,
        evidence_message_ids: list[UUID] | None = None,
    ) -> MemoryRecord:
        cleaned = content.strip()
        if not cleaned:
            raise ValueError("memory content must not be empty")
        _, content_hash = normalize_content(cleaned)
        await MemoryService._validate_source_references(
            db,
            owner_id=owner_id,
            session_id=session_id,
            message_id=message_id,
            run_id=run_id,
            dream_run_id=dream_run_id,
        )
        item = MemoryRecord(
            user_id=owner_id,
            source_session_id=session_id,
            source_message_id=message_id,
            source_run_id=run_id,
            source_dream_run_id=dream_run_id,
            source_tool_call_id=tool_call_id,
            source_type=source_type.value,
            pinned=False,
            audience=audience.value,
            category=category.value,
            content=cleaned,
            normalized_content_hash=content_hash,
            seen_at=datetime.now(UTC) if source_type == MemorySourceType.MANUAL else None,
        )
        db.add(item)
        await db.flush()
        await MemoryService._add_evidence(
            db,
            owner_id=owner_id,
            memory_id=item.id,
            message_ids=evidence_message_ids or [],
        )
        await MemoryIndexService.enqueue(db, item)
        return item

    @staticmethod
    async def _duplicate_by_hash(
        db: AsyncSession,
        *,
        owner_id: UUID,
        content_hash: str,
        exclude_id: UUID | None = None,
        for_update: bool = False,
    ) -> MemoryRecord | None:
        statement = select(MemoryRecord).where(
            MemoryRecord.user_id == owner_id,
            MemoryRecord.normalized_content_hash == content_hash
        )
        if exclude_id is not None:
            statement = statement.where(MemoryRecord.id != exclude_id)
        if for_update:
            statement = statement.execution_options(populate_existing=True).with_for_update()
        return await db.scalar(statement)

    @staticmethod
    async def _get_for_automatic_update(
        db: AsyncSession,
        *,
        owner_id: UUID,
        memory_id: UUID,
        readable_by: TrustLevel,
    ) -> MemoryRecord | None:
        statement = select(MemoryRecord).where(
            MemoryRecord.user_id == owner_id,
            MemoryRecord.id == memory_id,
        )
        statement = statement.where(memory_visibility_filter(readable_by))
        return await db.scalar(
            statement.execution_options(populate_existing=True).with_for_update()
        )

    @staticmethod
    async def _validate_source_references(
        db: AsyncSession,
        *,
        owner_id: UUID,
        session_id: UUID | None,
        message_id: UUID | None,
        run_id: UUID | None,
        dream_run_id: UUID | None,
    ) -> None:
        if session_id is not None and not await db.scalar(
            select(ChatSession.id).where(
                ChatSession.id == session_id,
                ChatSession.user_id == owner_id,
            )
        ):
            raise ValueError("memory source session owner mismatch")
        if message_id is not None and not await db.scalar(
            select(Message.id)
            .join(ChatSession)
            .where(
                Message.id == message_id,
                ChatSession.user_id == owner_id,
                *( [Message.session_id == session_id] if session_id is not None else [] ),
            )
        ):
            raise ValueError("memory source message owner mismatch")
        if run_id is not None and not await db.scalar(
            select(AgentRun.id)
            .join(ChatSession, ChatSession.id == AgentRun.session_id)
            .where(
                AgentRun.id == run_id,
                AgentRun.parent_run_id.is_(None),
                ChatSession.user_id == owner_id,
                *( [AgentRun.session_id == session_id] if session_id is not None else [] ),
            )
        ):
            raise ValueError("memory source run owner mismatch")
        if dream_run_id is not None and not await db.scalar(
            select(MemoryDreamRun.id)
            .join(ChatSession, ChatSession.id == MemoryDreamRun.session_id)
            .where(
                MemoryDreamRun.id == dream_run_id,
                ChatSession.user_id == owner_id,
                *(
                    [MemoryDreamRun.session_id == session_id]
                    if session_id is not None
                    else []
                ),
            )
        ):
            raise ValueError("memory source dream run owner mismatch")

    @staticmethod
    async def _add_evidence(
        db: AsyncSession,
        *,
        owner_id: UUID,
        memory_id: UUID,
        message_ids: list[UUID],
    ) -> None:
        unique_ids = list(dict.fromkeys(message_ids))
        if not unique_ids:
            return
        await MemoryService._validate_evidence_owner(
            db, owner_id=owner_id, message_ids=unique_ids
        )
        if not await db.scalar(
            select(MemoryRecord.id).where(
                MemoryRecord.id == memory_id,
                MemoryRecord.user_id == owner_id,
            )
        ):
            raise ValueError("memory owner mismatch")
        await db.execute(
            postgresql_insert(MemorySourceMessage)
            .values(
                [
                    {"memory_id": memory_id, "message_id": message_id}
                    for message_id in unique_ids
                ]
            )
            .on_conflict_do_nothing(index_elements=["memory_id", "message_id"])
        )

    @staticmethod
    async def _validate_evidence_owner(
        db: AsyncSession, *, owner_id: UUID, message_ids: list[UUID]
    ) -> None:
        unique_ids = set(message_ids)
        if not unique_ids:
            return
        valid_ids = set(
            (
                await db.scalars(
                    select(Message.id)
                    .join(ChatSession)
                    .where(
                        Message.id.in_(unique_ids),
                        ChatSession.user_id == owner_id,
                    )
                )
            ).all()
        )
        if valid_ids != unique_ids:
            raise ValueError("memory evidence owner mismatch")

    @staticmethod
    async def _merge_automatic_metadata(
        db: AsyncSession,
        item: MemoryRecord,
        *,
        owner_id: UUID,
        source_audience: MemoryAudience,
        evidence_message_ids: list[UUID],
    ) -> None:
        if item.user_id != owner_id:
            raise ValueError("memory owner mismatch")
        audience = memory_audience_intersection([item.audience, source_audience])
        if audience is None:
            return
        item.audience = audience.value
        item.revision = (item.revision or 1) + 1
        await MemoryService._add_evidence(
            db,
            owner_id=owner_id,
            memory_id=item.id,
            message_ids=evidence_message_ids,
        )
        await db.flush()

    @staticmethod
    async def get(
        db: AsyncSession,
        *,
        owner_id: UUID,
        memory_id: UUID,
        for_update: bool = False,
    ) -> MemoryRecord | None:
        statement = (
            select(MemoryRecord)
            .where(
                MemoryRecord.user_id == owner_id,
                MemoryRecord.id == memory_id,
            )
            .options(*memory_reference_options(owner_id))
        )
        if for_update:
            statement = statement.execution_options(populate_existing=True).with_for_update()
        return await db.scalar(statement)

    @staticmethod
    async def update(
        db: AsyncSession,
        item: MemoryRecord,
        *,
        owner_id: UUID,
        content: str,
        category: MemoryCategory,
        audience: MemoryAudience,
        evidence_message_ids: list[UUID] | None = None,
        source_type: MemorySourceType | None = None,
        source_session_id: UUID | None = None,
        source_message_id: UUID | None = None,
        source_run_id: UUID | None = None,
        source_dream_run_id: UUID | None = None,
        source_tool_call_id: str | None = None,
        pinned: bool | None = None,
    ) -> MemoryRecord:
        if item.user_id != owner_id:
            raise ValueError("memory owner mismatch")
        cleaned = content.strip()
        if not cleaned:
            raise ValueError("memory content must not be empty")
        _, content_hash = normalize_content(cleaned)
        content_changed = content_hash != item.normalized_content_hash
        if await MemoryService._duplicate_by_hash(
            db, owner_id=owner_id, content_hash=content_hash, exclude_id=item.id
        ) is not None:
            raise MemoryDuplicateError
        item_id = item.id
        try:
            async with db.begin_nested():
                item.content = cleaned
                item.category = category.value
                item.audience = audience.value
                if pinned is not None:
                    item.pinned = pinned
                item.normalized_content_hash = content_hash
                item.revision = (item.revision or 1) + 1
                if content_changed:
                    if source_type is not None:
                        await MemoryService._validate_source_references(
                            db,
                            owner_id=owner_id,
                            session_id=source_session_id,
                            message_id=source_message_id,
                            run_id=source_run_id,
                            dream_run_id=source_dream_run_id,
                        )
                        item.source_type = source_type.value
                        item.source_session_id = source_session_id
                        item.source_message_id = source_message_id
                        item.source_run_id = source_run_id
                        item.source_dream_run_id = source_dream_run_id
                        item.source_tool_call_id = source_tool_call_id
                await db.flush()
                await MemoryService._add_evidence(
                    db,
                    owner_id=owner_id,
                    memory_id=item.id,
                    message_ids=evidence_message_ids or [],
                )
                if content_changed:
                    await MemoryIndexService.enqueue(db, item)
                await db.flush()
        except IntegrityError as exc:
            duplicate_content = await MemoryService._duplicate_by_hash(
                db,
                owner_id=owner_id,
                content_hash=content_hash,
                exclude_id=item_id,
            )
            if duplicate_content is None:
                raise
            raise MemoryDuplicateError from exc
        return item

    @staticmethod
    async def delete(
        db: AsyncSession, item: MemoryRecord, *, owner_id: UUID
    ) -> None:
        if item.user_id != owner_id:
            raise ValueError("memory owner mismatch")
        await db.delete(item)
        await db.flush()

    @staticmethod
    async def mark_seen(
        db: AsyncSession, *, owner_id: UUID, memory_ids: list[UUID]
    ) -> int:
        unique_ids = list(dict.fromkeys(memory_ids))
        if not unique_ids:
            return 0
        result = await db.scalars(
            update(MemoryRecord)
            .where(
                MemoryRecord.user_id == owner_id,
                MemoryRecord.id.in_(unique_ids),
                MemoryRecord.seen_at.is_(None),
            )
            .values(
                seen_at=func.now(),
                updated_at=MemoryRecord.updated_at,
            )
            .returning(MemoryRecord.id)
        )
        return len(result.all())

    @staticmethod
    async def list(
        db: AsyncSession,
        *,
        owner_id: UUID,
        query: str | None = None,
        source_type: MemorySourceType | None = None,
        category: MemoryCategory | None = None,
        audience: MemoryAudience | None = None,
        pinned: bool | None = None,
        readable_by: TrustLevel | None = None,
        excluded_content_hashes: frozenset[str] | set[str] = frozenset(),
        cursor_updated_at: datetime | None = None,
        cursor_id: UUID | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> MemoryList:
        if (cursor_updated_at is None) != (cursor_id is None):
            raise ValueError("both cursor fields are required together")
        filters = [MemoryRecord.user_id == owner_id]
        if source_type is not None:
            filters.append(MemoryRecord.source_type == source_type.value)
        if category is not None:
            filters.append(MemoryRecord.category == category.value)
        if audience is not None:
            filters.append(MemoryRecord.audience == audience.value)
        if pinned is not None:
            filters.append(MemoryRecord.pinned.is_(pinned))
        if readable_by is not None:
            filters.append(memory_visibility_filter(readable_by))
        if excluded_content_hashes:
            filters.append(memory_content_hash_exclusion(excluded_content_hashes))
        cleaned_query = (query or "").strip()
        rank = None
        if cleaned_query:
            tsquery = func.plainto_tsquery(
                "simple", " ".join(memory_lexical_terms(cleaned_query))
            )
            filters.append(MemoryRecord.search_vector.op("@@")(tsquery))
            rank = func.ts_rank_cd(MemoryRecord.search_vector, tsquery)
        total = int(
            await db.scalar(select(func.count(MemoryRecord.id)).where(*filters)) or 0
        )
        if cursor_updated_at is not None and cursor_id is not None:
            filters.append(
                or_(
                    MemoryRecord.updated_at < cursor_updated_at,
                    and_(
                        MemoryRecord.updated_at == cursor_updated_at,
                        MemoryRecord.id < cursor_id,
                    ),
                )
            )
        statement = (
            select(MemoryRecord)
            .where(*filters)
            .options(
                *memory_reference_options(owner_id),
            )
        )
        if rank is not None:
            statement = statement.order_by(
                rank.desc(), MemoryRecord.updated_at.desc(), MemoryRecord.id.desc()
            )
        else:
            statement = statement.order_by(
                MemoryRecord.updated_at.desc(), MemoryRecord.id.desc()
            )
        items = list((await db.scalars(statement.offset(offset).limit(limit))).all())
        return MemoryList(items, total, offset, limit)

    @staticmethod
    async def scope(
        db: AsyncSession,
        *,
        owner_id: UUID,
        readable_by: TrustLevel,
        excluded_content_hashes: frozenset[str] | set[str] = frozenset(),
    ) -> MemoryScope:
        filters = [MemoryRecord.user_id == owner_id]
        filters.append(memory_visibility_filter(readable_by))
        if excluded_content_hashes:
            filters.append(memory_content_hash_exclusion(excluded_content_hashes))
        total, pinned = (
            await db.execute(
                select(
                    func.count(MemoryRecord.id),
                    func.count(MemoryRecord.id).filter(MemoryRecord.pinned.is_(True)),
                ).where(*filters)
            )
        ).one()
        return MemoryScope(int(total), int(pinned))

    @staticmethod
    async def pinned_context(
        db: AsyncSession,
        *,
        owner_id: UUID,
        input_trust_level: TrustLevel,
        excluded_content_hashes: frozenset[str] | set[str] = frozenset(),
    ) -> list[MemoryRecord]:
        filters = [
            MemoryRecord.user_id == owner_id,
            MemoryRecord.pinned.is_(True),
        ]
        filters.append(memory_visibility_filter(input_trust_level))
        if excluded_content_hashes:
            filters.append(memory_content_hash_exclusion(excluded_content_hashes))
        items = list(
            (
                await db.scalars(
                    select(MemoryRecord)
                    .where(*filters)
                    .options(*memory_reference_options(owner_id))
                    .order_by(MemoryRecord.created_at, MemoryRecord.id)
                    .execution_options(populate_existing=True)
                )
            ).all()
        )
        return items

    @staticmethod
    async def automatic_context_by_ids(
        db: AsyncSession,
        *,
        owner_id: UUID,
        memory_ids: list[UUID],
        readable_by: TrustLevel,
        excluded_content_hashes: frozenset[str] | set[str] = frozenset(),
    ) -> list[MemoryRecord]:
        if not memory_ids:
            return []
        filters = [
            MemoryRecord.user_id == owner_id,
            MemoryRecord.id.in_(memory_ids),
            MemoryRecord.pinned.is_(False),
            memory_visibility_filter(readable_by),
        ]
        if excluded_content_hashes:
            filters.append(memory_content_hash_exclusion(excluded_content_hashes))
        items = list(
            (
                await db.scalars(
                    select(MemoryRecord)
                    .where(*filters)
                    .options(*memory_reference_options(owner_id))
                    .execution_options(populate_existing=True)
                )
            ).all()
        )
        by_id = {item.id: item for item in items}
        return [by_id[memory_id] for memory_id in memory_ids if memory_id in by_id]

    @staticmethod
    async def set_pinned(
        db: AsyncSession,
        item: MemoryRecord,
        *,
        owner_id: UUID,
        pinned: bool,
    ) -> MemoryRecord:
        if item.user_id != owner_id:
            raise ValueError("memory owner mismatch")
        if item.pinned != pinned:
            item.pinned = pinned
            item.revision = (item.revision or 1) + 1
            await db.flush()
        return item
