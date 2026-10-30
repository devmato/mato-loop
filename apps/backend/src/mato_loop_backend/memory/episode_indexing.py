from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from datetime import UTC, datetime, timedelta
from time import monotonic
from uuid import UUID

from sqlalchemy import JSON, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend.config import get_runtime_config
from mato_loop_backend.db.session import AsyncSessionFactory
from mato_loop_backend.memory.embeddings import (
    ACTIVE_EMBEDDING_PROFILE,
    EmbeddingError,
    embedding_client,
)
from mato_loop_backend.models import ChatSession, MemoryEmbeddingStatus, Message, MessageSearchChunk

logger = logging.getLogger(__name__)
TARGET_CHUNK_CHARACTERS = 1_200
MAX_CHUNK_CHARACTERS = 1_500
CHUNK_OVERLAP_CHARACTERS = 120
STALE_CLAIM_AFTER = timedelta(minutes=5)
RETRY_DELAYS = (5, 30, 120, 600, 3600)
RECONCILE_INTERVAL_SECONDS = 3_600


def chunk_message_content(content: str) -> list[str]:
    text = content.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []
    chunks: list[str] = []
    start = 0
    while start < len(text):
        hard_end = min(len(text), start + MAX_CHUNK_CHARACTERS)
        if hard_end == len(text):
            end = hard_end
        else:
            target = min(hard_end, start + TARGET_CHUNK_CHARACTERS)
            boundaries = [
                match.end()
                for match in re.finditer(r"\n\s*\n", text[start + 1 : hard_end])
                if match.end() >= TARGET_CHUNK_CHARACTERS // 2
            ]
            end = (
                start + 1 + min(boundaries, key=lambda value: abs(start + 1 + value - target))
                if boundaries
                else target
            )
            if end == target:
                word_end = text.rfind(
                    " ",
                    start + TARGET_CHUNK_CHARACTERS // 2,
                    min(hard_end, target + 100),
                )
                if word_end > start:
                    end = word_end
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        next_start = max(start + 1, end - CHUNK_OVERLAP_CHARACTERS)
        whitespace = text.find(" ", next_start, end)
        start = whitespace + 1 if whitespace >= 0 else next_start
    return chunks


def _eligible_filters() -> list[object]:
    return [
        Message.chat_session.has(ChatSession.parent_session_id.is_(None)),
        Message.role.in_(("user", "assistant")),
        Message.generation_status == "completed",
        Message.error_code.is_(None),
        func.length(func.btrim(Message.content)) > 0,
        or_(
            Message.role == "user",
            Message.tool_calls.is_(None),
            Message.tool_calls == JSON.NULL,
        ),
    ]


def _eligible(message: Message) -> bool:
    return (
        message.role in {"user", "assistant"}
        and message.generation_status == "completed"
        and message.error_code is None
        and bool(message.content.strip())
        and (message.role == "user" or message.tool_calls is None)
    )


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def message_search_chunks(message: Message) -> list[MessageSearchChunk]:
    """Build queued search chunks without database effects."""
    if not _eligible(message):
        return []
    return [
        MessageSearchChunk(
            message_id=message.id,
            chunk_index=index,
            content=content,
            content_hash=_content_hash(content),
            profile_fingerprint=ACTIVE_EMBEDDING_PROFILE.fingerprint,
            status=MemoryEmbeddingStatus.QUEUED.value,
        )
        for index, content in enumerate(chunk_message_content(message.content))
    ]


class EpisodeIndexService:
    @staticmethod
    async def enqueue(db: AsyncSession, message: Message) -> int:
        await db.execute(
            delete(MessageSearchChunk).where(
                MessageSearchChunk.message_id == message.id
            )
        )
        if not _eligible(message):
            return 0
        if await db.scalar(
            select(ChatSession.parent_session_id).where(ChatSession.id == message.session_id)
        ) is not None:
            return 0
        chunks = message_search_chunks(message)
        db.add_all(chunks)
        await db.flush()
        return len(chunks)

    @staticmethod
    async def reconcile(db: AsyncSession, *, retry_failed: bool = False) -> int:
        eligible = select(Message.id).where(
            Message.id == MessageSearchChunk.message_id, *_eligible_filters()
        )
        removed = await db.execute(
            delete(MessageSearchChunk).where(~eligible.exists())
        )
        changed = int(removed.rowcount or 0)
        messages = list((await db.scalars(select(Message).where(*_eligible_filters()))).all())
        existing_by_message: dict[UUID, list[MessageSearchChunk]] = {}
        for chunk in (
            await db.scalars(
                select(MessageSearchChunk).order_by(
                    MessageSearchChunk.message_id,
                    MessageSearchChunk.chunk_index,
                )
            )
        ).all():
            existing_by_message.setdefault(chunk.message_id, []).append(chunk)
        for message in messages:
            expected = chunk_message_content(message.content)
            existing = existing_by_message.get(message.id, [])
            valid = len(existing) == len(expected) and all(
                row.chunk_index == index
                and row.content == content
                and row.content_hash == _content_hash(content)
                and row.profile_fingerprint == ACTIVE_EMBEDDING_PROFILE.fingerprint
                for index, (row, content) in enumerate(zip(existing, expected, strict=True))
            )
            if valid:
                for row in existing:
                    if not retry_failed or row.status != MemoryEmbeddingStatus.FAILED.value:
                        continue
                    row.status = MemoryEmbeddingStatus.QUEUED.value
                    row.vector = None
                    row.attempts = 0
                    row.next_attempt_at = None
                    row.claimed_at = None
                    row.error_code = None
                    row.updated_at = datetime.now(UTC)
                    changed += 1
                continue
            await EpisodeIndexService.enqueue(db, message)
            changed += max(len(existing), len(expected))
        await db.commit()
        return changed

    @staticmethod
    async def recover(db: AsyncSession) -> int:
        result = await db.execute(
            update(MessageSearchChunk)
            .where(
                MessageSearchChunk.status == MemoryEmbeddingStatus.RUNNING.value,
                or_(
                    MessageSearchChunk.claimed_at.is_(None),
                    MessageSearchChunk.claimed_at
                    <= datetime.now(UTC) - STALE_CLAIM_AFTER,
                ),
            )
            .values(
                status=MemoryEmbeddingStatus.QUEUED.value,
                vector=None,
                claimed_at=None,
                next_attempt_at=None,
                error_code="memory.embeddingInterrupted",
                updated_at=datetime.now(UTC),
            )
        )
        await db.commit()
        return int(result.rowcount or 0)

    @staticmethod
    async def process_next(db: AsyncSession) -> bool:
        config = get_runtime_config()
        if not config.embedding_enabled:
            return False
        now = datetime.now(UTC)
        rows = list(
            (
                await db.scalars(
                    select(MessageSearchChunk)
                    .join(Message, Message.id == MessageSearchChunk.message_id)
                    .where(
                        MessageSearchChunk.status == MemoryEmbeddingStatus.QUEUED.value,
                        or_(
                            MessageSearchChunk.next_attempt_at.is_(None),
                            MessageSearchChunk.next_attempt_at <= now,
                        ),
                        *_eligible_filters(),
                    )
                    .order_by(
                        MessageSearchChunk.next_attempt_at,
                        MessageSearchChunk.updated_at,
                    )
                    .with_for_update(skip_locked=True)
                    .limit(config.embedding_batch_size)
                )
            ).all()
        )
        if not rows:
            await db.rollback()
            return False
        snapshots = [
            (row.id, row.message_id, row.chunk_index, row.content_hash, row.profile_fingerprint)
            for row in rows
        ]
        contents = [row.content for row in rows]
        for row in rows:
            row.status = MemoryEmbeddingStatus.RUNNING.value
            row.vector = None
            row.claimed_at = now
            row.next_attempt_at = None
            row.error_code = None
            row.attempts += 1
        await db.commit()

        try:
            vectors = await embedding_client.embed_documents(contents)
        except EmbeddingError as exc:
            await db.rollback()
            await EpisodeIndexService._record_failure(db, snapshots, exc)
            return True

        for snapshot, vector in zip(snapshots, vectors, strict=True):
            chunk_id, message_id, chunk_index, content_hash, profile = snapshot
            row = await db.get(MessageSearchChunk, chunk_id, with_for_update=True)
            message = await db.get(Message, message_id)
            expected = chunk_message_content(message.content) if message and _eligible(message) else []
            if (
                row is None
                or message is None
                or chunk_index >= len(expected)
                or expected[chunk_index] != row.content
                or row.content_hash != content_hash
                or row.profile_fingerprint != profile
                or profile != ACTIVE_EMBEDDING_PROFILE.fingerprint
            ):
                if row is not None:
                    await db.delete(row)
                continue
            row.status = MemoryEmbeddingStatus.READY.value
            row.vector = vector
            row.claimed_at = None
            row.next_attempt_at = None
            row.error_code = None
            row.updated_at = datetime.now(UTC)
        await db.commit()
        return True

    @staticmethod
    async def _record_failure(
        db: AsyncSession,
        snapshots: list[tuple[UUID, UUID, int, str, str]],
        error: EmbeddingError,
    ) -> None:
        now = datetime.now(UTC)
        for chunk_id, _message_id, _chunk_index, content_hash, profile in snapshots:
            row = await db.get(MessageSearchChunk, chunk_id, with_for_update=True)
            if (
                row is None
                or row.content_hash != content_hash
                or row.profile_fingerprint != profile
            ):
                continue
            row.vector = None
            row.claimed_at = None
            row.error_code = error.code
            row.updated_at = now
            if error.transient:
                delay = RETRY_DELAYS[min(max(row.attempts - 1, 0), len(RETRY_DELAYS) - 1)]
                row.status = MemoryEmbeddingStatus.QUEUED.value
                row.next_attempt_at = now + timedelta(seconds=delay)
            else:
                row.status = MemoryEmbeddingStatus.FAILED.value
                row.next_attempt_at = None
        await db.commit()


class EpisodeIndexScheduler:
    _task: asyncio.Task[None] | None = None

    @classmethod
    def start(cls) -> None:
        if cls._task is None or cls._task.done():
            cls._task = asyncio.create_task(cls._loop())

    @classmethod
    async def stop(cls) -> None:
        if cls._task is None:
            return
        cls._task.cancel()
        await asyncio.gather(cls._task, return_exceptions=True)
        cls._task = None

    @classmethod
    async def _loop(cls) -> None:
        last_reconcile = 0.0
        while True:
            try:
                async with AsyncSessionFactory() as db:
                    if monotonic() - last_reconcile >= RECONCILE_INTERVAL_SECONDS:
                        await EpisodeIndexService.recover(db)
                        await EpisodeIndexService.reconcile(db)
                        last_reconcile = monotonic()
                    while await EpisodeIndexService.process_next(db):
                        pass
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Episode embedding scheduler iteration failed")
            await asyncio.sleep(5)
