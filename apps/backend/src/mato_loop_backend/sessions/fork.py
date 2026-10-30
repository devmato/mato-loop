"""Independent conversation snapshots without copying execution state or grants."""

import asyncio
import logging
import re
from copy import deepcopy
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend.agent.projection import (
    _SUMMARY_EVIDENCE_MARKER,
    COMPACTION_HEADINGS,
)
from mato_loop_backend.chat_history import ChatHistoryService
from mato_loop_backend.files.service import RootFileError, RootFileService
from mato_loop_backend.files.storage import SessionStorage, SessionStorageError
from mato_loop_backend.media.attachments import AttachmentError, AttachmentService
from mato_loop_backend.memory.episode_indexing import message_search_chunks
from mato_loop_backend.models import (
    ChatSession,
    ContextSummary,
    Message,
    TitleGenerationStatus,
    UserAccount,
)
from mato_loop_backend.sessions.archive import is_session_archived
from mato_loop_backend.sessions.service import SessionServiceError, get_owned_session
from mato_loop_backend.settings import InstanceLimits, SettingsStore

logger = logging.getLogger(__name__)


async def fork_session(db: AsyncSession, session_id: UUID, user_id: UUID) -> ChatSession:
    # Finish the transaction and file cleanup even when the HTTP request disconnects.
    task = asyncio.create_task(_fork_session(db, session_id, user_id))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


async def _fork_session(db: AsyncSession, session_id: UUID, user_id: UUID) -> ChatSession:
    await db.execute(select(UserAccount.id).where(UserAccount.id == user_id).with_for_update(key_share=True))
    source = await get_owned_session(db, session_id, user_id, for_update=True)
    if source is None:
        raise SessionServiceError(404, "session.notFound")
    if await is_session_archived(db, source):
        raise SessionServiceError(409, "session.archived")
    if await ChatHistoryService._active_run(db, source.id) is not None:
        raise SessionServiceError(409, "session.runAlreadyActive")
    messages = await ChatHistoryService._messages(db, source.id)
    summaries = list((await db.scalars(select(ContextSummary).where(ContextSummary.session_id == source.id))).all())
    target = ChatSession(
        id=uuid4(), user_id=user_id, project_id=source.project_id,
        title="Kopie: " + source.title[:233], title_generation_status=TitleGenerationStatus.PENDING.value,
        workspace_mode="local",
        coding_workspace={**deepcopy(source.coding_workspace), "mode": "local"} if source.coding_workspace else None,
        **{key: deepcopy(getattr(source, key)) for key in (
            "model_config_id", "session_profile", "agent_profile_id", "reasoning_effort", "memory_mode",
            "trust_level", "web_search_policy", "preferred_execution_agent_id", "current_directory",
            "private_integration_data_used",
        )},
    )
    history = sorted([*messages, *summaries], key=lambda item: (item.created_at, item.id))
    # Preserve chronological tie-breaking when several messages share a transaction timestamp.
    ids = {source.id: target.id, **dict(zip((item.id for item in history), sorted(uuid4() for _ in history), strict=True))}
    references = {str(old): str(new) for old, new in ids.items()}
    uuid_pattern = re.compile(r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b")
    storage = SessionStorage()
    limits = await SettingsStore.load(db, InstanceLimits)
    source_files = RootFileService(source.id, project_id=source.project_id, session_storage=storage, user_id=user_id)
    target_files = RootFileService(target.id, project_id=target.project_id, session_storage=storage,
                                  user_id=user_id, user_quota_bytes=limits.user_quota_bytes)
    attachments = AttachmentService(storage)
    imported = {}
    copied_attachments = []
    target_id, project_id = target.id, target.project_id
    commit_attempted = False
    try:
        db.add(target)
        await db.flush()
        await asyncio.to_thread(storage.provision, target.id, target.project_id, user_id=user_id)
        await asyncio.to_thread(target_files.copy_session_from, source_files)
        for original in messages:
            data = deepcopy(original.provider_data or {})
            data.pop("continuation_scope", None)
            runtime = data.setdefault("_matoloop", {})
            # Historical run markers keep tool batches ordered; no run records are copied.
            runtime.pop("pendingAttachments", None)
            if original.error_code:
                runtime["historicalError"] = {"code": original.error_code, "params": original.error_params}
            message = Message(
                id=ids[original.id], session_id=target.id, content=original.content,
                provider_data=data,
                **{key: deepcopy(getattr(original, key)) for key in (
                    "role", "model_name", "generation_status", "trust_level", "tool_call_id",
                    "source_type", "source_provider", "source_endpoint_id", "public_reasoning", "created_at",
                )},
                tool_calls=deepcopy(original.tool_calls),
            )
            db.add(message)
            db.add_all(message_search_chunks(message))
            for attachment in original.attachments:
                copied = await asyncio.to_thread(attachments.copy_attachment, attachment, source.id,
                                                 target_files, message.id, imported)
                copied_attachments.append((attachment, copied))
                db.add(copied)
            for action in runtime.get("fileActions", []):
                if not isinstance(action, dict) or action.get("root") != "session":
                    continue
                if action.get("rootIdentity") == str(source.id):
                    action["rootIdentity"] = str(target.id)
                elif copied_file := imported.get((action.get("rootIdentity"), action.get("fileId"), action.get("fileRevision"))):
                    action.update(rootIdentity=str(target.id), fileId=str(copied_file.file_id),
                                  fileRevision=copied_file.revision, path=copied_file.path,
                                  filename=copied_file.path.rsplit("/", 1)[-1])
        attachment_ids = {str(original.id): str(copied.id) for original, copied in copied_attachments}
        for _, copied in copied_attachments:
            for key in ("sourceAttachmentIds", "authorizationSourceAttachmentIds"):
                if key in copied.metadata_json:
                    copied.metadata_json[key] = [attachment_ids.get(value, value) for value in copied.metadata_json[key]]
        await db.flush()
        copied_summaries = []
        headings = {name.casefold() for name in COMPACTION_HEADINGS}
        for original in summaries:
            parts = []
            source_ids = False
            for line in original.content.splitlines(keepends=True):
                heading = line.lstrip("#").strip().strip("*").removesuffix(":").casefold()
                if line.strip() == _SUMMARY_EVIDENCE_MARKER.strip():
                    source_ids = True
                elif heading in headings:
                    source_ids = heading == "source message ids"
                parts.append(uuid_pattern.sub(lambda match: references.get(match[0], match[0]), line) if source_ids else line)
            summary = ContextSummary(
                id=ids[original.id], session_id=target.id,
                covered_through_message_id=ids[original.covered_through_message_id],
                content="".join(parts),
                **{key: getattr(original, key) for key in (
                    "estimated_tokens", "model_config_id", "is_fallback", "created_at", "updated_at",
                )},
            )
            db.add(summary)
            copied_summaries.append((summary, ids.get(original.previous_summary_id)))
        await db.flush()
        for summary, previous_id in copied_summaries:
            summary.previous_summary_id = previous_id
        commit_attempted = True
        await db.commit()
    except BaseException as exc:
        await db.rollback()
        # A failed COMMIT may have persisted; startup reconciliation resolves leftover trees.
        if not commit_attempted:
            await asyncio.to_thread(storage.remove_session, target_id, project_id)
        if isinstance(exc, (RootFileError, AttachmentError)):
            raise SessionServiceError(409, exc.code) from exc
        if isinstance(exc, (OSError, SessionStorageError)):
            logger.exception("Session fork storage could not be copied")
            raise SessionServiceError(500, "session.storageUnavailable") from exc
        raise
    return target
