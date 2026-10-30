"""Validation and persistent storage for general session attachments."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import unicodedata
import warnings
from copy import deepcopy
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import BinaryIO, Literal
from uuid import UUID, uuid4

from anyio import CancelScope
from PIL import Image
from sqlalchemy import String, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, joinedload, selectinload

from mato_loop_backend.files.archives import MAX_EXPORT_BYTES
from mato_loop_backend.files.service import (
    FileRevision,
    FileRoot,
    RootFileError,
    RootFileService,
    WorkingFileRevision,
)
from mato_loop_backend.files.storage import SessionStorage, SessionStorageError
from mato_loop_backend.media.processing import (
    AttachmentProcessingError,
    AttachmentProcessingLimits,
    audio_mime_type,
    claimed_audio_format,
    claimed_document_format,
    document_mime_type,
    process_attachment,
    signed_audio_format,
    signed_document_format,
    validate_pcm_wav,
)
from mato_loop_backend.media.video_processing import (
    VideoProcessingError,
    probe_media,
    video_container_candidate,
)
from mato_loop_backend.models import ChatSession, Message, MessageAttachment
from mato_loop_backend.settings import InstanceLimits

MAX_FILE_ATTACHMENT_BYTES = 64 * 1024 * 1024
logger = logging.getLogger("mato_loop_backend.attachments")
ABSOLUTE_MAX_FILE_ATTACHMENT_BYTES = 256 * 1024 * 1024
MAX_IMAGE_ATTACHMENT_BYTES = MAX_FILE_ATTACHMENT_BYTES
MAX_AUDIO_ATTACHMENT_BYTES = MAX_FILE_ATTACHMENT_BYTES
MAX_ATTACHMENTS_PER_MESSAGE = 8

_FORMAT_MIME_TYPES = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "MPO": "image/jpeg",
    "WEBP": "image/webp",
    "GIF": "image/gif",
}
_TEXT_EXTENSIONS = {
    ".c",
    ".cc",
    ".conf",
    ".cpp",
    ".css",
    ".csv",
    ".go",
    ".h",
    ".hpp",
    ".html",
    ".ini",
    ".java",
    ".js",
    ".json",
    ".jsx",
    ".log",
    ".md",
    ".php",
    ".properties",
    ".py",
    ".rb",
    ".rs",
    ".sh",
    ".sql",
    ".svg",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".vue",
    ".xml",
    ".yaml",
    ".yml",
}
_CATEGORY_DIRECTORIES = {
    "user_upload": "attachments",
    "generated": "generated",
    "downloaded": "downloads",
}


class AttachmentError(Exception):
    def __init__(self, code: str, *, diagnostic: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.diagnostic = diagnostic


@dataclass(frozen=True)
class ValidatedImage:
    mime_type: str
    width: int
    height: int
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class ValidatedAudio:
    mime_type: str
    duration_ms: int
    channels: int
    sample_rate: int
    sample_width: int
    frames: int
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class AttachmentInput:
    data: bytes | BinaryIO
    mime_type: str
    filename: str | None = None
    kind: str = "file"
    metadata: dict | None = None


def _magic_mime_type(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    return None


def image_mime_type(data: bytes) -> str | None:
    return _magic_mime_type(data)


def validate_image(
    data: bytes, mime_type: str, *, max_bytes: int = MAX_IMAGE_ATTACHMENT_BYTES
) -> ValidatedImage:
    if not data:
        raise AttachmentError("attachment.empty")
    if len(data) > max_bytes:
        raise AttachmentError("attachment.tooLarge")
    claimed = mime_type.strip().lower()
    magic = _magic_mime_type(data)
    if magic is None or claimed not in _FORMAT_MIME_TYPES.values():
        raise AttachmentError("attachment.unsupportedType")
    if claimed != magic:
        raise AttachmentError("attachment.mimeMismatch")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        try:
            with Image.open(BytesIO(data)) as image:
                pillow_mime = _FORMAT_MIME_TYPES.get(image.format or "")
                width, height = image.size
                image.verify()
        except (
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
            OSError,
            SyntaxError,
            ValueError,
        ) as exc:
            raise AttachmentError(
                "attachment.invalidImage",
                diagnostic=f"verify:{type(exc).__name__}:{str(exc)[:200]}",
            ) from exc
        try:
            with Image.open(BytesIO(data)) as image:
                image.load()
        except (
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
            OSError,
            SyntaxError,
            ValueError,
        ) as exc:
            raise AttachmentError(
                "attachment.invalidImage",
                diagnostic=f"decode:{type(exc).__name__}:{str(exc)[:200]}",
            ) from exc
    if pillow_mime != magic or width <= 0 or height <= 0:
        raise AttachmentError("attachment.invalidImage", diagnostic="metadata")
    return ValidatedImage(magic, width, height, len(data), hashlib.sha256(data).hexdigest())


def validate_audio(
    data: bytes,
    mime_type: str,
    *,
    max_bytes: int = MAX_AUDIO_ATTACHMENT_BYTES,
    limits: AttachmentProcessingLimits | None = None,
) -> ValidatedAudio:
    if not data:
        raise AttachmentError("attachment.empty")
    if len(data) > max_bytes:
        raise AttachmentError("attachment.tooLarge")
    if mime_type.strip().lower() not in {"audio/wav", "audio/x-wav", "audio/wave"}:
        raise AttachmentError("attachment.unsupportedType")
    try:
        info = validate_pcm_wav(data, limits=limits)
    except AttachmentProcessingError as exc:
        raise AttachmentError(exc.code) from exc
    return ValidatedAudio(
        "audio/wav",
        info.duration_ms,
        info.channels,
        info.sample_rate,
        info.sample_width,
        info.frames,
        len(data),
        hashlib.sha256(data).hexdigest(),
    )


def safe_filename(filename: str | None, *, fallback: str = "attachment") -> str:
    value = unicodedata.normalize("NFKC", filename or "")
    value = value.replace("\\", "/").rsplit("/", 1)[-1]
    value = "".join(character for character in value if ord(character) >= 32 and ord(character) != 127)
    value = re.sub(r"[<>:\"|?*]", "_", value).strip(" .")
    if value in {"", ".", ".."}:
        value = fallback
    value = value[:255].rstrip(" .")
    while len(value.encode("utf-8")) > 240:
        value = value[:-1]
    return value or fallback


def _safe_metadata(metadata: dict | None) -> dict:
    if not metadata:
        return {}
    safe = {}
    for key, value in metadata.items():
        if key not in {
            "channels",
            "created",
            "background",
            "modelId",
            "modelName",
            "pageNumber",
            "providerAdapter",
            "providerName",
            "protectedInput",
            "quality",
            "sampleRate",
            "sampleWidth",
            "size",
            "authorizationSourceAttachmentIds",
            "sourceAttachmentIds",
            "sourceFiles",
            "authorizationSourceFiles",
            "sourceSha256",
            "targetTrustLevel",
            "toolOutputPresentation",
            "voiceMessage",
        }:
            continue
        if isinstance(value, str):
            safe[key] = value[:500]
        elif key in {
            "authorizationSourceAttachmentIds",
            "sourceAttachmentIds",
        } and isinstance(value, list):
            limit = 100 if key == "authorizationSourceAttachmentIds" else 5
            safe[key] = [item[:100] for item in value[:limit] if isinstance(item, str)]
        elif key in {"sourceFiles", "authorizationSourceFiles"} and isinstance(value, list):
            limit = 100 if key == "authorizationSourceFiles" else 5
            safe[key] = [
                {name: text[:2_000] for name, text in item.items()
                 if name in {"root", "path", "sha256", "executionAgentId"} and isinstance(text, str)}
                for item in value[:limit] if isinstance(item, dict)
            ]
        elif isinstance(value, (bool, int, float)) or value is None:
            safe[key] = value
        if len(safe) == 32:
            break
    return safe


def processing_limits(settings: InstanceLimits) -> AttachmentProcessingLimits:
    return AttachmentProcessingLimits(
        max_media_bytes=settings.max_upload_bytes,
        max_extracted_characters=settings.max_extracted_characters,
        max_pdf_pages=settings.max_pdf_pages,
        max_workbook_sheets=settings.max_workbook_sheets,
        max_workbook_cells=settings.max_workbook_cells,
        max_presentation_slides=settings.max_presentation_slides,
        max_zip_entries=settings.max_archive_entries,
        max_zip_uncompressed_bytes=settings.max_archive_uncompressed_bytes,
        max_zip_entry_bytes=settings.max_archive_entry_bytes,
        max_zip_compression_ratio=settings.max_archive_compression_ratio,
        max_audio_duration_seconds=settings.max_audio_duration_seconds,
        max_video_duration_seconds=settings.max_video_duration_seconds,
    )


def attachment_processing_candidate(data: bytes, browser_mime_type: str, filename: str | None) -> bool:
    claimed = browser_mime_type.split(";", 1)[0].strip().lower()
    suffix = Path(safe_filename(filename)).suffix.lower()
    return bool(
        _magic_mime_type(data)
        or signed_audio_format(data)
        or claimed_audio_format(claimed, suffix)
        or video_container_candidate(data, claimed, suffix)
        or b"%PDF-" in data[:1024]
        or data.startswith(b"PK\x03\x04")
        or claimed_document_format(claimed, suffix)
        or claimed.startswith("text/")
        or suffix in _TEXT_EXTENSIONS
    )


def classify_attachment(
    data: bytes,
    browser_mime_type: str,
    filename: str | None,
    *,
    max_bytes: int = MAX_FILE_ATTACHMENT_BYTES,
    limits: AttachmentProcessingLimits | None = None,
) -> AttachmentInput:
    if not data:
        raise AttachmentError("attachment.empty")
    if len(data) > max_bytes:
        raise AttachmentError("attachment.tooLarge")
    cleaned_name = safe_filename(filename)
    image_mime = _magic_mime_type(data)
    if image_mime is not None:
        validate_image(data, image_mime, max_bytes=max_bytes)
        return AttachmentInput(data, image_mime, cleaned_name, "image")
    claimed = browser_mime_type.split(";", 1)[0].strip().lower()
    suffix = Path(cleaned_name).suffix.lower()
    video_candidate = video_container_candidate(data, claimed, suffix)
    if video_candidate:
        try:
            media_info = (
                probe_media(data)
                if limits is None and max_bytes == MAX_FILE_ATTACHMENT_BYTES
                else probe_media(
                    data,
                    max_bytes=max_bytes,
                    max_duration_seconds=(
                        limits.max_video_duration_seconds
                        if limits is not None
                        else 15 * 60
                    ),
                )
            )
        except VideoProcessingError as exc:
            raise AttachmentError(exc.code) from exc
        if media_info.has_video:
            return AttachmentInput(data, media_info.mime_type, cleaned_name, "video")
    signed_audio = signed_audio_format(data)
    audio_format = signed_audio or claimed_audio_format(claimed, suffix)
    if video_candidate and media_info.has_audio and audio_format is None:
        audio_format = "m4a" if media_info.format == "mp4" else "webm"
    if audio_format is not None:
        return AttachmentInput(
            data, audio_mime_type(audio_format), cleaned_name, "audio"
        )
    signed_document = signed_document_format(data, limits=limits)
    document_format = signed_document or claimed_document_format(claimed, suffix)
    if document_format is not None:
        return AttachmentInput(
            data, document_mime_type(document_format), cleaned_name, "document"
        )
    is_text_candidate = claimed.startswith("text/") or suffix in _TEXT_EXTENSIONS
    if is_text_candidate:
        try:
            data.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise AttachmentError("attachment.invalidText") from exc
        mime_type = claimed if claimed.startswith("text/") else "text/plain"
        return AttachmentInput(data, mime_type, cleaned_name, "text")
    return AttachmentInput(data, "application/octet-stream", cleaned_name, "file")


@dataclass(frozen=True)
class _ManagedWrite:
    source: FileRevision | WorkingFileRevision
    companion: FileRevision | WorkingFileRevision | None
    created_directories: tuple[str, ...]
    source_created: bool


def pending_attachments(message: Message) -> list[SimpleNamespace]:
    if message.role != "tool":
        return []
    state = (message.provider_data or {}).get("_matoloop", {})
    if state.get("fileOutputsPending") is not True or state.get("fileOutputsHidden"):
        return []
    result = []
    for item in state.get("pendingAttachments", []):
        if (
            item.get("run_id") != state.get("runId")
            or item.get("session_id") != str(message.session_id)
            or item.get("message_id") != str(message.id)
        ):
            continue
        values = dict(item)
        for key in ("id", "message_id", "session_id", "file_root_id", "file_id", "run_id"):
            values[key] = UUID(values[key])
        result.append(SimpleNamespace(**values, message=message))
    return result


def model_attachments(message: Message, *, include_pending: bool = True) -> list:
    if message.role == "tool" and (message.provider_data or {}).get("_matoloop", {}).get("fileOutputsHidden"):
        return []
    return [*message.attachments, *(pending_attachments(message) if include_pending else [])]


def latest_image_references(messages: list[dict], *, run_id: UUID | None = None) -> list[dict]:
    """Exclude superseded identities before budgeting; loading still verifies current hashes."""
    latest = {}
    superseded = set()
    for message_index, message in enumerate(messages):
        for attachment_index, item in enumerate(message.get("attachments") or []):
            if not isinstance(item, dict) or item.get("kind") != "image":
                continue
            if (
                run_id is not None
                and item.get("file_revision") is None
                and item.get("run_id") not in {None, str(run_id)}
            ):
                superseded.add((message_index, attachment_index))
                continue
            identity = tuple(item.get(key) for key in ("session_id", "file_root_id", "file_id"))
            if not all(isinstance(value, str) and value for value in identity):
                continue
            source_order = item.get("source_order")
            rank = (
                source_order if type(source_order) is int else message_index,
                message_index,
                attachment_index,
            )
            previous = latest.get(identity)
            if previous is None or rank > previous:
                if previous is not None:
                    superseded.add(previous[1:])
                latest[identity] = rank
            else:
                superseded.add(rank[1:])
    if not superseded:
        return messages
    return [
        {
            **message,
            "attachments": [
                item
                for attachment_index, item in enumerate(message.get("attachments") or [])
                if (message_index, attachment_index) not in superseded
            ],
        }
        if "attachments" in message
        else message
        for message_index, message in enumerate(messages)
    ]


class AttachmentService:
    def __init__(
        self,
        session_storage: SessionStorage | Path | None = None,
        *,
        settings: InstanceLimits | None = None,
        run_id: UUID | None = None,
    ) -> None:
        self.sessions = (
            SessionStorage(session_storage)
            if isinstance(session_storage, Path)
            else session_storage or SessionStorage()
        )
        self.settings = settings
        self.run_id = run_id

    def _root_files(self, session_id: UUID, file_root_id: UUID, *, run_id: UUID | None = None) -> RootFileService:
        try:
            session_project_id = self.sessions.session_project_id(session_id)
            project_id = self.sessions.session_project_id(file_root_id)
            if file_root_id != session_id and (
                project_id is None or project_id != session_project_id
            ):
                raise SessionStorageError("sessionStorage.sessionNotFound", "Session storage not found")
            service = RootFileService(
                file_root_id,
                project_id=project_id,
                session_storage=self.sessions,
                run_id=run_id,
            )
            if UUID(service.root_identity(FileRoot.SESSION)) != file_root_id:
                raise RootFileError("file.rootChanged", "File root changed")
            return service
        except (RootFileError, SessionStorageError) as exc:
            raise AttachmentError("attachment.notFound") from exc

    @staticmethod
    def _create_file(
        root_files: RootFileService, path: str, data: bytes | BinaryIO
    ) -> tuple[FileRevision | WorkingFileRevision, list[str]]:
        parts = PurePosixPath(path).parts
        created_directories: list[str] = []
        for index in range(1, len(parts)):
            directory = "/".join(parts[:index])
            try:
                root_files.create_directory(FileRoot.SESSION, directory)
            except RootFileError as exc:
                if exc.code != "file.pathAlreadyExists" or root_files.path_kind(
                    FileRoot.SESSION, directory
                ) != "directory":
                    raise
            else:
                created_directories.append(directory)
        try:
            root_files.create_file(
                FileRoot.SESSION,
                path,
                BytesIO(data) if isinstance(data, bytes) else data,
                max_bytes=MAX_EXPORT_BYTES,
            )
        except BaseException:
            for directory in reversed(created_directories):
                try:
                    root_files.delete_path(
                        FileRoot.SESSION, directory, recursive=False
                    )
                except RootFileError:
                    pass
            raise
        return root_files.working_revision(path), created_directories

    @staticmethod
    def _discard_write(root_files: RootFileService, written: _ManagedWrite) -> None:
        if root_files.run_id is not None:
            # Remove new allocations, but retain existing run edits without deleting old history.
            if written.companion is not None and written.companion.file_id != written.source.file_id:
                root_files.delete_path(FileRoot.SESSION, written.companion.path, recursive=False)
            if written.source_created:
                root_files.delete_path(FileRoot.SESSION, written.source.path, recursive=False)
        else:
            revisions = [written.companion, written.source]
            seen: set[tuple[UUID, int]] = set()
            for revision in revisions:
                if revision is None or (revision.file_id, revision.revision) in seen:
                    continue
                seen.add((revision.file_id, revision.revision))
                root_files.discard_output_revision(revision.file_id, revision.revision)
        for directory in reversed(written.created_directories):
            try:
                root_files.delete_path(FileRoot.SESSION, directory, recursive=False)
            except RootFileError:
                pass

    @staticmethod
    def _companion_metadata(root_id: UUID, revision: FileRevision | WorkingFileRevision) -> dict:
        return {
            "companionFileRootId": str(root_id),
            "companionFileId": str(revision.file_id),
            "companionFileRevision": revision.revision,
            "companionFilePath": revision.path,
            "companionSizeBytes": revision.size_bytes,
            "companionSha256": revision.sha256,
        }

    def copy_attachment(
        self, attachment: MessageAttachment, source_session_id: UUID,
        target: RootFileService, message_id: UUID,
        imported: dict[tuple[str, str, int], FileRevision],
    ) -> MessageAttachment:
        """Rebind a copied attachment, importing foreign revisions without processing media again."""
        def copy_file(root_id: UUID, file_id: UUID, revision: int, path: str) -> tuple[UUID, int, str]:
            if root_id == source_session_id:
                return file_id, revision, path
            key = str(root_id), str(file_id), revision
            if key not in imported:
                files = self._root_files(source_session_id, root_id)
                files.file_revision(file_id, revision)
                imported.update({(str(root_id), str(file_id), number): item
                                 for number, item in target.copy_file_from(files, file_id).items()})
                if key not in imported:
                    raise AttachmentError("attachment.notFound")
            copied = imported[key]
            return copied.file_id, copied.revision, copied.path

        metadata = deepcopy(attachment.metadata_json or {})
        file_id, revision, path = copy_file(
            attachment.file_root_id, attachment.file_id, attachment.file_revision, attachment.file_path,
        )
        if metadata.get("companionFileRootId"):
            companion_id, companion_revision, companion_path = copy_file(
                UUID(metadata["companionFileRootId"]), UUID(metadata["companionFileId"]),
                metadata["companionFileRevision"], metadata["companionFilePath"],
            )
            metadata.update(companionFileRootId=str(target.session_id), companionFileId=str(companion_id),
                            companionFileRevision=companion_revision, companionFilePath=companion_path)
            if attachment.file_root_id != source_session_id and companion_id != file_id:
                files = self._root_files(source_session_id, attachment.file_root_id)
                ownership = files._derived_links().get(attachment.metadata_json["companionFileId"])
                binding = {"source": attachment.file_revision, "derived": attachment.metadata_json["companionFileRevision"]}
                if ownership and ownership["sourceFileId"] == str(attachment.file_id) and binding in ownership["bindings"]:
                    target.register_derived_file(file_id, companion_id, source_revision=revision, derived_revision=companion_revision)
        return MessageAttachment(
            id=uuid4(), message_id=message_id, file_root_id=target.session_id,
            file_id=file_id, file_revision=revision, file_path=path, metadata_json=metadata,
            **{key: getattr(attachment, key) for key in (
                "position", "kind", "category", "purpose", "processing_status", "mime_type",
                "size_bytes", "sha256", "filename", "width", "height", "duration_ms", "created_at",
            )},
        )

    async def create_image(self, db: AsyncSession, message: Message, data: bytes, mime_type: str, **kwargs) -> MessageAttachment | SimpleNamespace:
        return await self.create_attachment(db, message, data, mime_type, kind="image", **kwargs)

    async def create_audio(self, db: AsyncSession, message: Message, data: bytes, mime_type: str, **kwargs) -> MessageAttachment | SimpleNamespace:
        return await self.create_attachment(db, message, data, mime_type, kind="audio", **kwargs)

    async def create_reference(
        self,
        db: AsyncSession,
        message: Message,
        *,
        file_root_id: UUID,
        file_id: UUID,
        file_revision: int | None,
        file_path: str,
        filename: str,
        position: int,
        category: str,
        mime_type: str = "application/octet-stream",
        metadata: dict | None = None,
    ) -> MessageAttachment | SimpleNamespace:
        if file_revision is None and self.run_id is None:
            raise AttachmentError("attachment.notFound")
        locked_session = await db.scalar(
            select(ChatSession)
            .join(Message, Message.session_id == ChatSession.id)
            .where(ChatSession.id == message.session_id, Message.id == message.id)
            .with_for_update()
        )
        if locked_session is None:
            raise AttachmentError("attachment.messageNotFound")
        root_session = locked_session
        if file_root_id != locked_session.id:
            if locked_session.project_id is None:
                raise AttachmentError("attachment.notFound")
            root_session = await db.scalar(
                select(ChatSession).where(
                    ChatSession.id == file_root_id,
                    ChatSession.user_id == locked_session.user_id,
                    ChatSession.project_id == locked_session.project_id,
                    ChatSession.session_type == "chat",
                ).with_for_update()
            )
            if root_session is None:
                raise AttachmentError("attachment.notFound")
        root_files = RootFileService(
            root_session.id,
            project_id=root_session.project_id,
            session_storage=self.sessions,
            run_id=self.run_id,
            user_id=root_session.user_id,
            user_quota_bytes=self.settings.user_quota_bytes if self.settings else None,
        )
        try:
            revision = await asyncio.to_thread(root_files.working_revision, file_path) if file_revision is None else await asyncio.to_thread(
                root_files.file_revision, file_id, file_revision
            )
            if file_revision is None:
                with await asyncio.to_thread(root_files.open_working_file, file_id, expected_sha256=revision.sha256):
                    pass
        except RootFileError as exc:
            raise AttachmentError("attachment.notFound") from exc
        if revision.path != file_path or revision.file_id != file_id:
            raise AttachmentError("attachment.notFound")
        kind = "file"
        width = height = None
        image_limit = self.settings.max_upload_bytes if self.settings else MAX_IMAGE_ATTACHMENT_BYTES
        if revision.size_bytes <= image_limit:
            try:
                handle = (
                    await asyncio.to_thread(root_files.open_working_file, file_id, expected_sha256=revision.sha256)
                    if file_revision is None
                    else await asyncio.to_thread(root_files.open_revision_by_id, file_id, file_revision)
                )
                with handle:
                    detected_mime = image_mime_type(handle.read(12))
                    handle.seek(0)
                    data = handle.read(image_limit + 1) if detected_mime else b""
                if detected_mime:
                    image = validate_image(data, detected_mime, max_bytes=image_limit)
                    kind, mime_type = "image", image.mime_type
                    width, height = image.width, image.height
            except (AttachmentError, RootFileError):
                pass
        values = dict(
            id=uuid4(),
            message_id=message.id,
            position=position,
            kind=kind,
            category=category,
            purpose="artifact",
            processing_status="ready",
            mime_type=mime_type,
            size_bytes=revision.size_bytes,
            sha256=revision.sha256,
            file_root_id=file_root_id,
            file_id=file_id,
            file_revision=file_revision,
            file_path=file_path,
            filename=filename,
            metadata_json=_safe_metadata(metadata),
            width=width, height=height, duration_ms=None,
        )
        if self.run_id is not None:
            return self._persist_pending(message, values)
        attachment = MessageAttachment(**values)
        db.add(attachment)
        await db.flush()
        return attachment

    async def create_attachment(
        self,
        db: AsyncSession,
        message: Message,
        data: bytes | BinaryIO,
        mime_type: str,
        *,
        kind: str,
        filename: str | None = None,
        position: int | None = None,
        metadata: dict | None = None,
        category: str = "user_upload",
        purpose: Literal["attachment", "artifact"] = "attachment",
        processing_status: str | None = "ready",
        source_file_root_id: UUID | None = None,
        file_id: UUID | None = None,
    ) -> MessageAttachment | SimpleNamespace:
        if not isinstance(data, bytes) and (kind != "file" or purpose != "attachment" or file_id is not None or source_file_root_id is not None):
            raise AttachmentError("attachment.unsupportedType")
        size = len(data) if isinstance(data, bytes) else data.seek(0, 2)
        if not isinstance(data, bytes):
            data.seek(0)
        if not size:
            raise AttachmentError("attachment.empty")
        max_bytes = (
            self.settings.max_upload_bytes
            if self.settings is not None and purpose == "attachment"
            else MAX_FILE_ATTACHMENT_BYTES
        )
        limits = (
            processing_limits(self.settings)
            if self.settings is not None and purpose == "attachment"
            else None
        )
        if size > max_bytes:
            raise AttachmentError("attachment.tooLarge")
        if purpose not in {"attachment", "artifact"}:
            raise AttachmentError("attachment.invalidPurpose")
        attachment_id = uuid4()
        cleaned_filename = safe_filename(filename, fallback=f"attachment-{attachment_id}")
        width = height = duration_ms = None
        safe_metadata = _safe_metadata(metadata)
        derived_files: dict[str, bytes] = {}
        if kind == "image":
            validated_image = validate_image(data, mime_type, max_bytes=max_bytes)
            mime_type = validated_image.mime_type
            width, height = validated_image.width, validated_image.height
        elif kind in {"audio", "video", "text", "document"}:
            try:
                processed = await asyncio.to_thread(
                    process_attachment,
                    data,
                    kind=kind,
                    mime_type=mime_type,
                    filename=cleaned_filename,
                    limits=limits,
                )
            except AttachmentProcessingError as exc:
                raise AttachmentError(exc.code) from exc
            if processed is None:
                raise AttachmentError("attachment.unsupportedType")
            mime_type = processed.mime_type
            duration_ms = processed.duration_ms
            width, height = processed.width, processed.height
            derived_files = {} if kind == "audio" else processed.derived_files
            safe_metadata.update(processed.metadata)
            safe_metadata["format"] = processed.format
            if kind == "audio":
                safe_metadata.update(
                    channels=processed.channels,
                    sampleRate=processed.sample_rate,
                    sampleWidth=processed.sample_width,
                )
        elif kind == "file":
            if purpose == "attachment":
                mime_type = "application/octet-stream"
        else:
            raise AttachmentError("attachment.unsupportedType")
        if kind == "audio" and duration_ms is None:
            raise AttachmentError("attachment.invalidAudio")
        if kind == "video" and (
            duration_ms is None or width is None or height is None or derived_files
        ):
            raise AttachmentError("attachment.invalidVideo")
        if kind == "audio":
            safe_metadata.update(
                channels=safe_metadata.get("channels"),
                sampleRate=safe_metadata.get("sampleRate"),
                sampleWidth=safe_metadata.get("sampleWidth"),
            )
        if category not in _CATEGORY_DIRECTORIES:
            raise AttachmentError("attachment.invalidCategory")
        if purpose == "attachment" and (
            category != "user_upload"
            or source_file_root_id is not None
            or file_id is not None
        ):
            raise AttachmentError("attachment.invalidCategory")
        if processing_status not in {None, "pending", "ready", "failed"}:
            raise AttachmentError("attachment.invalidProcessingStatus")

        locked_session = await db.scalar(
            select(ChatSession)
            .join(Message, Message.session_id == ChatSession.id)
            .where(ChatSession.id == message.session_id, Message.id == message.id)
            .with_for_update()
        )
        if locked_session is None:
            raise AttachmentError("attachment.messageNotFound")
        root_files = RootFileService(
            message.session_id,
            project_id=locked_session.project_id,
            session_storage=self.sessions,
            user_id=locked_session.user_id,
            user_quota_bytes=self.settings.user_quota_bytes if self.settings else None,
            run_id=self.run_id,
        )
        if position is None:
            maximum_position = await db.scalar(
                select(func.max(MessageAttachment.position)).where(
                    MessageAttachment.message_id == message.id
                )
            )
            position = int(maximum_position) + 1 if maximum_position is not None else 0
            if self.run_id is not None:
                position = max(position, max((item.position + 1 for item in pending_attachments(message)), default=0))
        if isinstance(position, bool) or position < 0:
            raise AttachmentError("attachment.invalidPosition")

        def write_managed() -> _ManagedWrite:
            root_id = UUID(root_files.root_identity(FileRoot.SESSION))
            source_created = file_id is None or source_file_root_id != root_id
            created_directories: list[str] = []
            source: FileRevision | WorkingFileRevision | None = None
            companion: FileRevision | WorkingFileRevision | None = None
            try:
                if not source_created:
                    source = root_files.append_output_revision(file_id, data)
                else:
                    directory = _CATEGORY_DIRECTORIES[category]
                    source, directories = self._create_file(
                        root_files,
                        f"{directory}/{attachment_id}/{cleaned_filename}",
                        data,
                    )
                    created_directories.extend(directories)

                if kind == "text":
                    companion = source
                elif derived_files:
                    name, companion_data = next(iter(derived_files.items()))
                    suffix = PurePosixPath(name).suffix
                    source_path = PurePosixPath(source.path)
                    candidate = str(source_path.with_name(f"{source_path.stem}{suffix}"))
                    if candidate == source.path:
                        candidate = str(
                            source_path.with_name(
                                f"{source_path.stem}-{attachment_id.hex[:8]}{suffix}"
                            )
                        )
                    try:
                        companion, directories = self._create_file(
                            root_files, candidate, companion_data
                        )
                    except RootFileError as exc:
                        if exc.code != "file.pathAlreadyExists":
                            raise
                        candidate = str(
                            source_path.with_name(
                                f"{source_path.stem}-{attachment_id.hex[:8]}{suffix}"
                            )
                        )
                        companion, directories = self._create_file(
                            root_files, candidate, companion_data
                        )
                    created_directories.extend(directories)
                    if source.file_id != companion.file_id:
                        root_files.register_derived_file(source.file_id, companion.file_id)
                return _ManagedWrite(source, companion, tuple(created_directories), source_created)
            except BaseException:
                if source is not None:
                    self._discard_write(
                        root_files,
                        _ManagedWrite(source, companion, tuple(created_directories), source_created),
                    )
                raise

        write_task = asyncio.create_task(asyncio.to_thread(write_managed))
        try:
            written = await asyncio.shield(write_task)
        except asyncio.CancelledError:
            while not write_task.done():
                try:
                    with CancelScope(shield=True):
                        await asyncio.shield(write_task)
                except asyncio.CancelledError:
                    continue
            written = write_task.result()
            if root_files.run_id is None:
                await asyncio.to_thread(self._discard_write, root_files, written)
            raise
        except RootFileError as exc:
            if exc.code == "file.userQuotaExceeded":
                raise AttachmentError("attachment.userQuotaExceeded") from exc
            raise AttachmentError("attachment.storageInvalid") from exc

        root_id = UUID(root_files.root_identity(FileRoot.SESSION))
        if written.companion is not None:
            safe_metadata.update(
                self._companion_metadata(root_id, written.companion)
            )
        values = dict(
            id=attachment_id,
            message_id=message.id,
            position=position,
            kind=kind,
            category=category,
            purpose=purpose,
            processing_status=processing_status,
            mime_type=mime_type,
            size_bytes=written.source.size_bytes,
            sha256=written.source.sha256,
            file_root_id=root_id,
            file_id=written.source.file_id,
            file_revision=written.source.revision,
            file_path=written.source.path,
            filename=cleaned_filename,
            width=width,
            height=height,
            duration_ms=duration_ms,
            metadata_json=safe_metadata,
        )
        if self.run_id is not None:
            return self._persist_pending(message, values)
        attachment = MessageAttachment(**values)
        db.add(attachment)
        try:
            await db.flush()
        except BaseException:
            await asyncio.to_thread(self._discard_write, root_files, written)
            raise
        return attachment

    def _persist_pending(self, message: Message, values: dict) -> SimpleNamespace:
        values = {**values, "session_id": message.session_id, "run_id": self.run_id}
        item = {
            key: str(value) if isinstance(value, UUID) else value for key, value in values.items()
        }
        data = dict(message.provider_data or {})
        state = dict(data.get("_matoloop", {}))
        state.update(
            runId=str(self.run_id),
            fileOutputsPending=True,
            pendingAttachments=[*state.get("pendingAttachments", []), item],
        )
        message.provider_data = {**data, "_matoloop": state}
        return SimpleNamespace(**values, message=message)

    async def pending_for_run(self, db: AsyncSession, session_id: UUID) -> list[SimpleNamespace]:
        if self.run_id is None:
            return []
        messages = (
            await db.scalars(
                select(Message).where(
                    Message.session_id == session_id,
                    Message.role == "tool",
                    Message.provider_data["_matoloop"]["runId"].astext == str(self.run_id),
                )
            )
        ).all()
        return [
            item
            for message in messages
            for item in pending_attachments(message)
            if item.run_id == self.run_id and item.session_id == session_id
        ]

    async def resolve_path(
        self, db: AsyncSession, root_files: RootFileService, path: str,
    ) -> MessageAttachment | SimpleNamespace | None:
        """Find metadata for the current file content, including this run's outputs."""
        try:
            revision = await asyncio.to_thread(root_files.working_revision, path)
        except RootFileError as exc:
            if exc.code == "file.revisionNotFound":
                return None
            raise
        root_id = UUID(root_files.root_identity(FileRoot.SESSION))
        if revision.revision is None:
            if revision.run_id != self.run_id:
                raise AttachmentError("attachment.notFound")
            return next((
                item for item in await self.pending_for_run(db, root_files.session_id)
                if item.file_root_id == root_id and item.file_id == revision.file_id
                and item.sha256 == revision.sha256
            ), None)
        return await db.scalar(
            select(MessageAttachment)
            .join(Message)
            .options(joinedload(MessageAttachment.message))
            .where(
                Message.session_id == root_files.session_id,
                MessageAttachment.file_root_id == root_id,
                MessageAttachment.file_id == revision.file_id,
                MessageAttachment.sha256 == revision.sha256,
            )
            .order_by(Message.created_at.desc(), MessageAttachment.position.desc())
            .limit(1)
        )

    def read_source(self, attachment, session_id: UUID, *, current_only: bool = False) -> bytes:
        if not 0 <= attachment.size_bytes <= ABSOLUTE_MAX_FILE_ATTACHMENT_BYTES:
            raise AttachmentError("attachment.notFound")
        run_id = getattr(attachment, "run_id", None)
        if self.run_id is not None and run_id is not None and run_id != self.run_id:
            raise AttachmentError("attachment.notFound")
        try:
            if run_id is not None and attachment.file_revision is None:
                if attachment.session_id != session_id:
                    raise AttachmentError("attachment.notFound")
                root_files = self._root_files(session_id, attachment.file_root_id, run_id=run_id)
                with root_files.open_working_file(
                    attachment.file_id, expected_sha256=attachment.sha256
                ) as source:
                    data = source.read(ABSOLUTE_MAX_FILE_ATTACHMENT_BYTES + 1)
            else:
                path = self.revision_path(
                    session_id,
                    attachment.file_root_id,
                    attachment.file_id,
                    attachment.file_revision,
                    expected_size=attachment.size_bytes,
                    expected_sha256=attachment.sha256,
                    current_only=current_only,
                )
                with path.open("rb") as source:
                    data = source.read(ABSOLUTE_MAX_FILE_ATTACHMENT_BYTES + 1)
            if (
                len(data) != attachment.size_bytes
                or hashlib.sha256(data).hexdigest() != attachment.sha256
            ):
                raise AttachmentError("attachment.notFound")
            return data
        except (RootFileError, OSError, ValueError, TypeError) as exc:
            raise AttachmentError("attachment.notFound") from exc

    async def backfill_derived_files(self, db: AsyncSession) -> int:
        """Register verified legacy provenance without changing attachment rows or file contents."""
        # Shared legacy companions must not be assigned to whichever owner is scanned first.
        other = aliased(MessageAttachment)
        shared = (
            select(other.id)
            .where(
                other.file_root_id == MessageAttachment.file_root_id,
                other.file_id != MessageAttachment.file_id,
                func.lower(other.metadata_json["companionFileRootId"].astext)
                == cast(MessageAttachment.file_root_id, String),
                func.lower(other.metadata_json["companionFileId"].astext)
                == func.lower(MessageAttachment.metadata_json["companionFileId"].astext),
            )
            .exists()
        )
        rows = await db.stream(
            select(MessageAttachment, Message.session_id, shared)
            .join(Message, Message.id == MessageAttachment.message_id)
            .where(MessageAttachment.metadata_json["companionFileId"].astext.is_not(None))
            .execution_options(yield_per=100)
        )

        def register(attachment: MessageAttachment, session_id: UUID) -> bool:
            metadata = attachment.metadata_json or {}
            companion_id = UUID(metadata["companionFileId"])
            if companion_id == attachment.file_id:
                return False
            self.read_source(attachment, session_id)
            if self._verified_companion_bytes(attachment, session_id) is None:
                return False
            files = self._root_files(session_id, attachment.file_root_id)
            files.register_derived_file(
                attachment.file_id,
                companion_id,
                source_revision=attachment.file_revision,
                derived_revision=metadata["companionFileRevision"],
            )
            return True

        verified = 0
        try:
            async for attachment, session_id, is_shared in rows:
                try:
                    if is_shared:
                        raise AttachmentError("file.changed")
                    verified += await asyncio.to_thread(register, attachment, session_id)
                except (AttachmentError, RootFileError, SessionStorageError) as exc:
                    logger.warning("Attachment provenance backfill skipped (%s)", exc.code)
                except (KeyError, TypeError, ValueError, AttributeError):
                    logger.warning("Attachment provenance backfill skipped (attachment.notFound)")
                except OSError:
                    logger.warning(
                        "Attachment provenance backfill skipped (attachment.storageInvalid)"
                    )
        finally:
            await rows.close()
        return verified

    async def finalize_run(self, db: AsyncSession, session: ChatSession, run_id: UUID) -> None:
        root_files = RootFileService(
            session.id, project_id=session.project_id, session_storage=self.sessions,
            user_id=session.user_id,
            user_quota_bytes=self.settings.user_quota_bytes if self.settings else None,
        )
        revisions = {
            item.file_id: item for item in await asyncio.to_thread(root_files.finalize_run, run_id)
        }
        messages = list(
            (
                await db.scalars(
                    select(Message)
                    .options(selectinload(Message.attachments))
                    .where(
                        Message.session_id == session.id,
                        Message.role == "tool",
                        Message.provider_data["_matoloop"]["runId"].astext == str(run_id),
                    )
                    .order_by(Message.created_at, Message.id)
                )
            ).all()
        )
        seen_files, seen_actions = set(), set()
        for message in reversed(messages):
            data = dict(message.provider_data or {})
            state = dict(data.get("_matoloop", {}))
            if state.get("fileOutputsPending") is not True:
                seen_files.update(item.file_id for item in message.attachments)
                seen_actions.update(
                    (
                        action.get("root"),
                        action.get("rootIdentity"),
                        action.get("executionAgentId"),
                        action.get("currentDirectory"),
                        action.get("path"),
                    )
                    for action in state.get("fileActions", [])
                )
                continue
            outputs = []
            for item in reversed(state.get("pendingAttachments", [])):
                if (
                    item.get("run_id") != str(run_id)
                    or item.get("session_id") != str(session.id)
                    or item.get("message_id") != str(message.id)
                    or item.get("file_root_id") != root_files.root_identity(FileRoot.SESSION)
                ):
                    continue
                file_id = UUID(item["file_id"])
                if file_id in seen_files:
                    continue
                revision = revisions.get(file_id)
                if revision is None:
                    if item["file_revision"] is None:
                        continue
                    try:
                        revision = await asyncio.to_thread(root_files.file_revision, file_id, item["file_revision"])
                    except RootFileError:
                        continue
                if revision.revision is None or revision.file_id != file_id or revision.size_bytes <= 0:
                    continue
                values = {
                    key: value for key, value in item.items() if key not in {"session_id", "run_id"}
                }
                for key in ("id", "message_id", "file_root_id", "file_id"):
                    values[key] = UUID(values[key])
                values.update(
                    file_revision=revision.revision,
                    file_path=revision.path,
                    size_bytes=revision.size_bytes,
                    sha256=revision.sha256,
                )
                metadata = dict(values.get("metadata_json") or {})
                if revision.sha256 != item["sha256"]:
                    # Bytes changed after the attachment was described; do not publish stale media metadata.
                    values.update(
                        kind="file",
                        mime_type="application/octet-stream",
                        width=None,
                        height=None,
                        duration_ms=None,
                    )
                    metadata = {
                        "toolOutputPresentation": metadata["toolOutputPresentation"]
                    } if "toolOutputPresentation" in metadata else {}
                if metadata.get("companionFileRevision") is None and metadata.get(
                    "companionFileId"
                ):
                    companion = revisions.get(UUID(metadata["companionFileId"]))
                    if companion is not None:
                        metadata.update(
                            self._companion_metadata(UUID(item["file_root_id"]), companion)
                        )
                values["metadata_json"] = metadata
                db.add(MessageAttachment(**values))
                outputs.append(item)
                seen_files.add(file_id)
            actions = []
            for action in reversed(state.get("fileActions", [])):
                if "kind" in action:
                    # Operation receipts survive deletion and repeated edits. Only provided
                    # session references need their final published revision attached.
                    if action.get("operation") == "provide" and action.get("fileId"):
                        current = revisions.get(UUID(action["fileId"]))
                        if current is not None and action.get("rootIdentity") == str(session.id):
                            action = {**action, "fileRevision": current.revision, "sizeBytes": current.size_bytes}
                    actions.append(action)
                    continue
                key = (
                    action.get("root"),
                    action.get("rootIdentity"),
                    action.get("executionAgentId"),
                    action.get("currentDirectory"),
                    action.get("path"),
                )
                if key not in seen_actions:
                    if action.get("root") == "session":
                        if action.get("rootIdentity") != root_files.root_identity(FileRoot.SESSION):
                            continue
                        try:
                            current = next(
                                (item for item in revisions.values() if item.path == action["path"]),
                                None,
                            ) or await asyncio.to_thread(root_files.working_revision, action["path"])
                        except RootFileError:
                            continue
                        if current.revision is None or current.file_id in seen_files:
                            continue
                        seen_files.add(current.file_id)
                        action = {**action, "sizeBytes": current.size_bytes}
                    actions.append(action)
                    seen_actions.add(key)
            state["fileActions"] = list(reversed(actions))
            state.pop("pendingAttachments", None)
            state["fileOutputsPending"] = False
            if not outputs and not actions:
                state["fileOutputsHidden"] = True
            message.provider_data = {**data, "_matoloop": state}
        await db.flush()

    async def verified_path(
        self, attachment: MessageAttachment, session_id: UUID, *, current_only: bool = False
    ) -> Path:
        path = await asyncio.to_thread(
            self.revision_path,
            session_id,
            attachment.file_root_id,
            attachment.file_id,
            attachment.file_revision,
            expected_size=attachment.size_bytes,
            expected_sha256=attachment.sha256,
            current_only=current_only,
        )

        def verify() -> bool:
            digest = hashlib.sha256()
            size = 0
            with path.open("rb") as file:
                while chunk := file.read(1024 * 1024):
                    size += len(chunk)
                    if size > attachment.size_bytes:
                        return False
                    digest.update(chunk)
            return size == attachment.size_bytes and digest.hexdigest() == attachment.sha256

        try:
            valid = await asyncio.to_thread(verify)
        except OSError as exc:
            raise AttachmentError("attachment.notFound") from exc
        if not valid:
            raise AttachmentError("attachment.notFound")
        return path

    def revision_path(
        self,
        session_id: UUID,
        file_root_id: UUID,
        file_id: UUID,
        file_revision: int,
        *,
        expected_path: str | None = None,
        expected_size: int | None = None,
        expected_sha256: str | None = None,
        current_only: bool = False,
    ) -> Path:
        try:
            root_files = self._root_files(session_id, file_root_id)
            revision = root_files.file_revision(file_id, file_revision)
            if current_only:
                current = root_files.working_revision(revision.path)
                if current.file_id != file_id or current.sha256 != revision.sha256:
                    raise AttachmentError("attachment.notFound")
            if (
                expected_path is not None
                and revision.path != expected_path
                or expected_size is not None
                and revision.size_bytes != expected_size
                or expected_sha256 is not None
                and revision.sha256 != expected_sha256
            ):
                raise AttachmentError("attachment.notFound")
            return root_files.revision_path(file_id, file_revision)
        except (RootFileError, TypeError, ValueError) as exc:
            raise AttachmentError("attachment.notFound") from exc

    async def read_audio(
        self, attachment: MessageAttachment, session_id: UUID, *, current_only: bool = False
    ) -> bytes:
        if (
            attachment.kind != "audio"
            or attachment.processing_status != "ready"
            or attachment.size_bytes > ABSOLUTE_MAX_FILE_ATTACHMENT_BYTES
        ):
            raise AttachmentError("attachment.notFound")
        data = await asyncio.to_thread(self.read_source, attachment, session_id, current_only=current_only)
        try:
            processed = await asyncio.to_thread(
                process_attachment,
                data,
                kind="audio",
                mime_type=attachment.mime_type,
                filename=attachment.filename or "audio",
                limits=AttachmentProcessingLimits(
                    max_media_bytes=ABSOLUTE_MAX_FILE_ATTACHMENT_BYTES,
                    max_audio_duration_seconds=3_600,
                ),
            )
        except AttachmentProcessingError as exc:
            raise AttachmentError("attachment.notFound") from exc
        if processed is None or (
            attachment.duration_ms is not None
            and processed.duration_ms != attachment.duration_ms
        ):
            raise AttachmentError("attachment.notFound")
        return processed.derived_files.get("audio.wav", data)

    async def read_image(
        self, attachment: MessageAttachment, session_id: UUID, *, current_only: bool = False
    ) -> bytes:
        if (
            attachment.kind != "image"
            or attachment.processing_status != "ready"
            or attachment.size_bytes > ABSOLUTE_MAX_FILE_ATTACHMENT_BYTES
        ):
            raise AttachmentError("attachment.notFound")
        try:
            data = await asyncio.to_thread(self.read_source, attachment, session_id, current_only=current_only)
            validated = validate_image(
                data,
                attachment.mime_type,
                max_bytes=ABSOLUTE_MAX_FILE_ATTACHMENT_BYTES,
            )
        except (OSError, AttachmentError) as exc:
            raise AttachmentError("attachment.notFound") from exc
        if (
            (attachment.width is not None and validated.width != attachment.width)
            or (attachment.height is not None and validated.height != attachment.height)
        ):
            raise AttachmentError("attachment.notFound")
        return data

    def _verified_companion_bytes(
        self,
        attachment: MessageAttachment,
        session_id: UUID,
        *,
        current_only: bool = False,
    ) -> bytes | None:
        metadata = attachment.metadata_json or {}
        values = {
            "root": metadata.get("companionFileRootId"),
            "file": metadata.get("companionFileId"),
            "revision": metadata.get("companionFileRevision"),
            "path": metadata.get("companionFilePath"),
            "size": metadata.get("companionSizeBytes"),
            "sha256": metadata.get("companionSha256"),
        }
        if not any(value is not None for value in values.values()):
            return None
        if (
            not isinstance(values["root"], str)
            or not isinstance(values["file"], str)
            or not (
                values["revision"] is None and getattr(attachment, "run_id", None) is not None
                or type(values["revision"]) is int and values["revision"] >= 1
            )
            or not isinstance(values["path"], str)
            or not isinstance(values["size"], int)
            or isinstance(values["size"], bool)
            or values["size"] < 0
            or not isinstance(values["sha256"], str)
        ):
            raise AttachmentError("attachment.notFound")
        try:
            root_id = UUID(values["root"])
            file_id = UUID(values["file"])
            if root_id != attachment.file_root_id:
                raise AttachmentError("attachment.notFound")
            return self.read_source(
                SimpleNamespace(
                    session_id=session_id,
                    run_id=getattr(attachment, "run_id", None),
                    file_root_id=root_id,
                    file_id=file_id,
                    file_revision=values["revision"],
                    size_bytes=values["size"],
                    sha256=values["sha256"],
                ),
                session_id,
                current_only=current_only,
            )
        except (OSError, TypeError, ValueError) as exc:
            raise AttachmentError("attachment.notFound") from exc

    def read_derived_text(
        self, attachment: MessageAttachment, session_id: UUID, *, current_only: bool = False
    ) -> str:
        if attachment.kind not in {"text", "document"} or attachment.processing_status != "ready":
            raise AttachmentError("attachment.notFound")
        self.read_source(attachment, session_id, current_only=current_only)
        data = self._verified_companion_bytes(
            attachment, session_id, current_only=current_only
        )
        if data is None:
            raise AttachmentError("attachment.notFound")
        try:
            return data.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise AttachmentError("attachment.notFound") from exc

    async def discard(self, attachment: MessageAttachment, session_id: UUID) -> None:
        if getattr(attachment, "run_id", None) is not None:
            return
        root_files = self._root_files(session_id, attachment.file_root_id)
        metadata = attachment.metadata_json or {}
        companion_id = metadata.get("companionFileId")
        companion_revision = metadata.get("companionFileRevision")

        def discard() -> None:
            if isinstance(companion_id, str) and isinstance(companion_revision, int):
                parsed = UUID(companion_id)
                if (parsed, companion_revision) != (
                    attachment.file_id,
                    attachment.file_revision,
                ):
                    root_files.discard_output_revision(parsed, companion_revision)
            root_files.discard_output_revision(
                attachment.file_id, attachment.file_revision
            )
            parent = PurePosixPath(attachment.file_path).parent
            if parent.name == str(attachment.id):
                try:
                    root_files.delete_path(
                        FileRoot.SESSION, str(parent), recursive=False
                    )
                except RootFileError:
                    pass

        await asyncio.to_thread(discard)
