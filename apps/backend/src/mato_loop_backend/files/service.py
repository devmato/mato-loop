from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import logging
import os
import shutil
import stat
import threading
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from io import BytesIO, TextIOWrapper
from pathlib import Path, PurePosixPath
from typing import BinaryIO, TextIO
from uuid import UUID, uuid4

from mato_loop_backend.files.storage import SessionStorage, SessionStorageError

logger = logging.getLogger("mato_loop_backend.workspace")


@dataclass(frozen=True, slots=True)
class FileReadResult:
    content: str
    lines_read: int
    has_more: bool
    next_offset: int | None
    truncated_lines: tuple[int, ...]
    sha256: str


class FileRoot(StrEnum):
    SESSION = "session"
    WORKSPACE = "workspace"


@dataclass(frozen=True, slots=True)
class RootFileEntry:
    name: str
    path: str
    kind: str
    size: int | None
    modified_at: float


@dataclass(frozen=True, slots=True)
class RootFileListResult:
    entries: tuple[RootFileEntry, ...]
    has_more: bool
    next_offset: int | None


@dataclass(frozen=True, slots=True)
class TreeUsage:
    entries: int
    bytes: int


@dataclass(frozen=True, slots=True)
class FileRevision:
    file_id: UUID
    revision: int
    path: str
    size_bytes: int
    sha256: str
    created_at: str
    current: bool


@dataclass(frozen=True, slots=True)
class WorkingFileRevision:
    file_id: UUID
    run_id: UUID
    path: str
    size_bytes: int
    sha256: str
    created_at: str
    revision: int | None = None
    current: bool = True


@dataclass(frozen=True, slots=True)
class RevisionActivationResult:
    file_id: UUID
    current_revision: int
    sha256: str


class RootFileError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _UploadTooLarge(Exception):
    pass


MAX_EDITABLE_TEXT_BYTES = 10 * 1024 * 1024
_RENAME_NOREPLACE = 1
_RENAME_AT2 = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
if _RENAME_AT2 is not None:
    _RENAME_AT2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    _RENAME_AT2.restype = ctypes.c_int


def validate_upload_filename(filename: str | None, *, max_bytes: int | None = 240) -> str:
    value = unicodedata.normalize("NFKC", filename or "")
    if (
        value in {"", ".", ".."}
        or "/" in value
        or "\\" in value
        or value.endswith((" ", "."))
        or any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in value)
        or (max_bytes is not None and len(value.encode("utf-8")) > max_bytes)
    ):
        raise RootFileError("file.invalidName", "Invalid file name")
    return value


def _stage_upload(
    parent_descriptor: int,
    filename: str,
    source: BinaryIO,
    *,
    max_bytes: int,
) -> tuple[str, int]:
    temporary = f".upload-{uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600, dir_fd=parent_descriptor)
    size = 0
    try:
        source.seek(0)
        with os.fdopen(descriptor, "wb") as handle:
            while chunk := source.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise _UploadTooLarge
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        return temporary, size
    except BaseException:
        try:
            os.unlink(temporary, dir_fd=parent_descriptor)
        except FileNotFoundError:
            pass
        raise


def _publish_upload(
    parent_descriptor: int, temporary: str, target: str
) -> os.stat_result:
    published = False
    try:
        os.link(
            temporary,
            target,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        published = True
        os.fsync(parent_descriptor)
        return os.stat(target, dir_fd=parent_descriptor, follow_symlinks=False)
    except BaseException:
        if published:
            os.unlink(target, dir_fd=parent_descriptor)
        raise
    finally:
        try:
            os.unlink(temporary, dir_fd=parent_descriptor)
        except FileNotFoundError:
            pass


@contextmanager
def _directory_descriptor(
    root: Path,
    parts: tuple[str, ...],
    error: type[RootFileError],
) -> Iterator[int]:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(root, flags)
        for part in parts:
            metadata = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode):
                raise error("file.symlinkForbidden", "Symlinks are not followed")
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
    except RootFileError:
        if descriptor is not None:
            os.close(descriptor)
            descriptor = None
        raise
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
            descriptor = None
        raise error("file.parentNotFound", "Parent directory is missing") from exc
    try:
        yield descriptor
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _delete_at(
    parent_descriptor: int,
    name: str,
    *,
    recursive: bool,
    error: type[RootFileError],
) -> None:
    try:
        metadata = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            flags = (
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            child_descriptor = os.open(name, flags, dir_fd=parent_descriptor)
            try:
                if recursive:
                    for child in os.listdir(child_descriptor):
                        _delete_at(
                            child_descriptor,
                            child,
                            recursive=True,
                            error=error,
                        )
            finally:
                os.close(child_descriptor)
            try:
                os.rmdir(name, dir_fd=parent_descriptor)
            except OSError as exc:
                raise error(
                    "file.directoryNotEmpty", "Directory could not be removed"
                ) from exc
        elif stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            os.unlink(name, dir_fd=parent_descriptor)
        else:
            raise error("file.unsupportedType", "Unsupported file type")
        os.fsync(parent_descriptor)
    except FileNotFoundError as exc:
        raise error("file.pathNotFound", "Path not found") from exc


def _rename_at(
    parent_descriptor: int,
    source: str,
    destination: str,
    error: type[RootFileError],
    *,
    destination_descriptor: int | None = None,
) -> os.stat_result:
    destination_descriptor = parent_descriptor if destination_descriptor is None else destination_descriptor
    try:
        metadata = os.stat(source, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise error("file.pathNotFound", "Path not found") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise error("file.symlinkForbidden", "Symlinks cannot be renamed")
    if not (stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)):
        raise error("file.unsupportedType", "Unsupported file type")
    if source == destination and parent_descriptor == destination_descriptor:
        return metadata
    if _RENAME_AT2 is None:
        raise OSError(errno.ENOSYS, "renameat2 is unavailable")
    result = _RENAME_AT2(
        parent_descriptor,
        os.fsencode(source),
        destination_descriptor,
        os.fsencode(destination),
        _RENAME_NOREPLACE,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        if error_number == errno.EEXIST:
            raise error("file.pathAlreadyExists", "Destination path already exists")
        if error_number == errno.ENOENT:
            raise error("file.pathNotFound", "Path not found")
        raise OSError(error_number, os.strerror(error_number))
    os.fsync(parent_descriptor)
    os.fsync(destination_descriptor)
    return os.stat(destination, dir_fd=destination_descriptor, follow_symlinks=False)


def _open_regular_file(
    root: Path,
    parts: tuple[str, ...],
    error: type[RootFileError],
) -> BinaryIO:
    if not parts:
        raise error("file.fileNotFound", "File not found")
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    try:
        with _directory_descriptor(root, parts[:-1], error) as parent_descriptor:
            descriptor = os.open(parts[-1], flags, dir_fd=parent_descriptor)
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise OSError
            return os.fdopen(descriptor, "rb")
    except OSError as exc:
        if "descriptor" in locals():
            os.close(descriptor)
        raise error("file.fileNotFound", "File not found") from exc


def _read_bounded_line(handle: TextIO, max_line_length: int) -> tuple[str, bool] | None:
    chunk = handle.readline(max_line_length + 2)
    if chunk == "":
        return None
    has_newline = chunk.endswith("\n")
    value = chunk[:-1] if has_newline else chunk
    if value.endswith("\r"):
        value = value[:-1]
    truncated = len(value) > max_line_length
    while not has_newline and len(chunk) == max_line_length + 2:
        chunk = handle.readline(max_line_length + 2)
        if chunk == "":
            break
        has_newline = chunk.endswith("\n")
    return value[:max_line_length], truncated


class RootFileService:
    """Access the backend-local root of one persisted session."""

    MAX_LIST_LIMIT = 1_000
    MAX_OPERATION_ENTRIES = 10_000
    MAX_OPERATION_BYTES = 256 * 1024 * 1024

    def __init__(
        self,
        session_id: UUID,
        *,
        session_storage: SessionStorage | None = None,
        project_id: UUID | None = None,
        run_id: UUID | None = None,
        user_id: UUID | None = None,
        user_quota_bytes: int | None = None,
        max_editable_text_bytes: int = MAX_EDITABLE_TEXT_BYTES,
    ) -> None:
        if not isinstance(session_id, UUID):
            raise TypeError("session_id must be supplied by the host as a UUID")
        self.session_id = session_id
        if project_id is not None and not isinstance(project_id, UUID):
            raise TypeError("project_id must be supplied by the host as a UUID")
        self.project_id = project_id
        if run_id is not None and not isinstance(run_id, UUID):
            raise TypeError("run_id must be supplied by the host as a UUID")
        self.run_id = run_id
        self._operation_state = threading.local()
        self.session_storage = session_storage or SessionStorage()
        if user_id is not None and not isinstance(user_id, UUID):
            raise TypeError("user_id must be supplied by the host as a UUID")
        if user_quota_bytes is not None and user_id is None:
            raise ValueError("A user quota requires an owner")
        self.user_id = user_id
        self.user_quota_bytes = user_quota_bytes
        self.max_editable_text_bytes = max_editable_text_bytes

    @staticmethod
    def _parts(relative_path: str) -> tuple[str, ...]:
        if not isinstance(relative_path, str):
            raise RootFileError("file.invalidPath", "Invalid relative path")
        if relative_path == ".":
            return ()
        if not relative_path or "\\" in relative_path:
            raise RootFileError("file.invalidPath", "Invalid relative POSIX path")
        if any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in relative_path):
            raise RootFileError("file.invalidPath", "Control characters are not allowed in paths")
        parsed = PurePosixPath(relative_path)
        parts = tuple(relative_path.split("/"))
        if parsed.is_absolute() or any(part in {"", ".", ".."} for part in parts):
            raise RootFileError("file.invalidPath", "Invalid relative POSIX path")
        return parts

    @staticmethod
    def _relative_path(parts: tuple[str, ...]) -> str:
        return "/".join(parts) if parts else "."

    def normalize_relative_path(self, relative_path: str) -> str:
        return self._relative_path(self._parts(relative_path))

    def path_parts(self, relative_path: str) -> tuple[str, ...]:
        return self._parts(relative_path)

    def root_identity(self, root: FileRoot | str) -> str:
        self._selected_root(root)
        self._root_path(FileRoot.SESSION)
        return str(self.session_id)

    @staticmethod
    def _metadata(path: Path) -> os.stat_result:
        try:
            return path.lstat()
        except FileNotFoundError as exc:
            raise RootFileError("file.pathNotFound", "Path not found") from exc

    @classmethod
    def _ensure_directory(cls, path: Path, *, code: str = "file.directoryNotFound") -> None:
        try:
            metadata = path.lstat()
        except FileNotFoundError as exc:
            raise RootFileError(code, "Directory not found") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise RootFileError("file.symlinkForbidden", "Symlinks are not followed")
        if not stat.S_ISDIR(metadata.st_mode):
            raise RootFileError(code, "Directory not found")

    @classmethod
    def _ensure_no_symlinks(
        cls,
        root: Path,
        parts: tuple[str, ...],
        *,
        allow_missing_final: bool = False,
        allow_final_symlink: bool = False,
    ) -> Path:
        cls._ensure_directory(root)
        current = root
        for index, part in enumerate(parts):
            current = current / part
            final = index == len(parts) - 1
            try:
                metadata = current.lstat()
            except FileNotFoundError:
                if allow_missing_final and final:
                    return current
                raise RootFileError("file.pathNotFound", "Path not found") from None
            if stat.S_ISLNK(metadata.st_mode) and not (allow_final_symlink and final):
                raise RootFileError("file.symlinkForbidden", "Symlinks are not followed")
            if not final and not stat.S_ISDIR(metadata.st_mode):
                raise RootFileError("file.pathNotFound", "Path not found")
        return current

    def _root_path(self, root: FileRoot | str) -> Path:
        self._selected_root(root)
        try:
            return self.session_storage.logical_root_path(
                self.session_id, self.project_id, require_exists=True
            )
        except SessionStorageError as exc:
            raise RootFileError("file.sessionNotProvisioned", "Session root is missing") from exc

    def _files_path(self, root: FileRoot | str = FileRoot.SESSION) -> Path:
        path = self._root_path(root) / "files"
        self._ensure_directory(path)
        return path

    def _derived_links(self) -> dict[str, dict]:
        directory = self._revisions_path() / ".ownership"
        if not directory.exists() and not directory.is_symlink():
            return {}
        self._ensure_directory(directory)
        links = {}
        for path in directory.glob("*.json"):
            try:
                child = str(UUID(path.stem))
                link = json.loads(self._read_private_file(path))
                if child != path.stem or str(UUID(link["sourceFileId"])) != link["sourceFileId"]:
                    raise ValueError("Invalid ownership identity")
                if not isinstance(link["bindings"], list) or not link["bindings"]:
                    raise ValueError("Missing derivation bindings")
                for binding in link["bindings"]:
                    for key in ("source", "derived"):
                        value = binding[key]
                        if type(value) is int and value >= 1:
                            continue
                        if not isinstance(value, str) or str(UUID(value)) != value:
                            raise ValueError("Invalid derivation reference")
            except (KeyError, TypeError, ValueError) as exc:
                raise RootFileError("file.revisionInvalid", "Invalid derived file ownership") from exc
            links[child] = link
        for child in links:
            seen = {child}
            ancestor = links[child]["sourceFileId"]
            while ancestor in links:
                if ancestor in seen:
                    raise RootFileError("file.revisionInvalid", "Cyclic derived file ownership")
                seen.add(ancestor)
                ancestor = links[ancestor]["sourceFileId"]
        return links

    def _complete_derived_binding(self, file_id: str, run_id: str, revision: int | None) -> None:
        for child, link in self._derived_links().items():
            changed = False
            for binding in link["bindings"]:
                for key, identity in (("source", link["sourceFileId"]), ("derived", child)):
                    if identity == file_id and binding[key] == run_id and revision is not None:
                        binding[key] = revision
                        changed = True
            if changed:
                target = self._revisions_path() / ".ownership" / f"{child}.json"
                self._replace_private_file(target, self._index_bytes(link))
                self._fsync_directory(target.parent)

    def register_derived_file(
        self, source_file_id: UUID, derived_file_id: UUID, *,
        source_revision: int | None = None, derived_revision: int | None = None,
    ) -> None:
        """Register single-owner provenance. Explicit revision pairs support verified backfills."""
        if not isinstance(source_file_id, UUID) or not isinstance(derived_file_id, UUID):
            raise TypeError("File IDs must be supplied by the host as UUIDs")
        if (source_revision is None) != (derived_revision is None):
            raise RootFileError("file.revisionNotFound", "Both derivation revisions are required")
        with self._session_operation_lock(FileRoot.SESSION, quota_check=True):
            links = self._derived_links()
            source_id, child_id = str(source_file_id), str(derived_file_id)
            link = links.get(child_id, {"sourceFileId": source_id, "bindings": []})
            if link["sourceFileId"] != source_id:
                raise RootFileError("file.changed", "Derived file already has another owner")
            ancestor = source_id
            seen = {child_id}
            while True:
                if ancestor in seen:
                    raise RootFileError("file.changed", "Derived file ownership must not contain cycles")
                seen.add(ancestor)
                if ancestor not in links:
                    break
                ancestor = links[ancestor]["sourceFileId"]
            binding = {}
            for key, file_id, explicit in (
                ("source", source_file_id, source_revision),
                ("derived", derived_file_id, derived_revision),
            ):
                history = self._history_for_id(file_id)
                if history is None or history[1].get("deleted", False):
                    raise RootFileError("file.revisionNotFound", "Derived file reference not found")
                self._assert_available(history[1]["path"])
                owner = history[1].get("run", {})
                if explicit is None and owner and owner["state"] != "final":
                    binding[key] = owner["runId"]
                    continue
                revision = explicit if explicit is not None else history[1]["currentRevision"]
                if type(revision) is not int or not any(item["revision"] == revision for item in history[1]["revisions"]):
                    raise RootFileError("file.revisionNotFound", "Derived file revision not found")
                self._ensure_no_symlinks(self._root_path(FileRoot.SESSION) / "files", self._parts(history[1]["path"]))
                data = self._read_private_file(history[0] / str(revision))
                item = next(item for item in history[1]["revisions"] if item["revision"] == revision)
                if hashlib.sha256(data).hexdigest() != item["sha256"]:
                    raise RootFileError("file.revisionInvalid", "Invalid derivation revision content")
                binding[key] = revision
            if binding in link["bindings"]:
                return
            directory = self._revisions_path() / ".ownership"
            directory.mkdir(mode=0o700, exist_ok=True)
            self._ensure_directory(directory)
            target = directory / f"{child_id}.json"
            content = self._index_bytes({**link, "bindings": [*link["bindings"], binding]})
            self._assert_user_quota(len(content), replaced_bytes=target.stat().st_size if target.exists() else 0)
            self._replace_private_file(target, content)
            self._fsync_directory(directory)
            self._fsync_directory(directory.parent)

    def _purge_owned_files(self, file_ids: set[str]) -> None:
        """Delete explicit identities, descendants first; never infer ownership from paths."""
        links = self._derived_links()
        pending = [(file_id, False) for file_id in file_ids]
        ordered = []
        seen = set()
        while pending:
            file_id, visited = pending.pop()
            if visited:
                ordered.append(file_id)
                continue
            if file_id in seen:
                continue
            seen.add(file_id)
            pending.append((file_id, True))
            pending.extend((child, False) for child, link in links.items() if link["sourceFileId"] == file_id)
        histories = {index["fileId"]: (directory, index) for directory, index in self._revision_indexes()}
        targets = {}
        for file_id in ordered:
            if file_id in histories:
                self._assert_available(histories[file_id][1]["path"])
                target = self._ensure_no_symlinks(
                    self._root_path(FileRoot.SESSION) / "files", self._parts(histories[file_id][1]["path"]),
                    allow_missing_final=True, allow_final_symlink=True,
                )
                if target.exists() or target.is_symlink():
                    mode = self._metadata(target).st_mode
                    if not (stat.S_ISREG(mode) or stat.S_ISLNK(mode)):
                        raise RootFileError("file.changed", "Owned file is no longer a regular file")
                targets[file_id] = target
        for file_id in ordered:
            if file_id in histories:
                target = targets[file_id]
                target.unlink(missing_ok=True)
                self._fsync_directory(target.parent)
                history = histories[file_id][0]
                trash = history.parent / f".delete-owned-{uuid4().hex}"
                history.rename(trash)
                self._fsync_directory(history.parent)
                shutil.rmtree(trash)
                self._fsync_directory(history.parent)
            if file_id in links:
                path = self._revisions_path() / ".ownership" / f"{file_id}.json"
                path.unlink()
                self._fsync_directory(path.parent)

    def _write_run_file(
        self, path: str, data: bytes, *, expected_sha256: str | None = None,
    ) -> FileRevision | WorkingFileRevision:
        path = self.normalize_relative_path(path)
        self._assert_mutable(FileRoot.SESSION, self._parts(path))
        if len(data) > self.MAX_OPERATION_BYTES:
            raise RootFileError("file.tooLarge", "File is too large")
        with self._session_operation_lock(FileRoot.SESSION, quota_check=True):
            self._assert_available(path)
            target = self._ensure_no_symlinks(self._files_path(), self._parts(path), allow_missing_final=True)
            current = self._read_private_file(target) if target.exists() else None
            current_hash = hashlib.sha256(current).hexdigest() if current is not None else None
            if expected_sha256 is None and current_hash is not None:
                raise RootFileError("file.pathAlreadyExists", "Destination path already exists")
            if expected_sha256 is not None and current_hash != expected_sha256:
                raise RootFileError("file.changed", "File changed since it was read")
            history = self._history_for_path(path)
            if history:
                directory, index = history
                staged = directory
            else:
                staged, directory, index = self._stage_initial_history(
                    path, current if current is not None else data, baseline=current is not None,
                )
            try:
                owner = index.get("run")
                if (owner and owner["runId"] == str(self.run_id) and owner["state"] != "pending") or (
                    f"{self.session_id}/{self.run_id}" in index.get("runReceipts", {})
                ) or any(item.get("runId") == str(self.run_id) for item in index["revisions"]):
                    raise RootFileError("file.changed", "Run file is finalized")
                if history and current == data and index["currentRevision"] is not None and (not owner or owner["state"] == "final"):
                    return self.working_revision(path)
                if not owner or owner["state"] == "final":
                    if owner:
                        self._complete_derived_binding(index["fileId"], owner["runId"], owner["revision"])
                    owner = {
                        "sessionId": str(self.session_id), "runId": str(self.run_id),
                        "state": "pending", "initialSha256": current_hash,
                    }
                # Staged baseline bytes already count toward usage; reserve the final
                # snapshot and index growth before publishing history or live bytes.
                owner = {**owner, "reserveBytes": max(len(current or b""), len(data)) + 1024}
                updated = {**index, "run": owner}
                old_index = self._read_private_file(staged / "index.json")
                old_reserve = index.get("run", {}).get("reserveBytes", 0) if history else 0
                self._assert_user_quota(
                    len(data) + len(self._index_bytes(updated)) + owner["reserveBytes"],
                    replaced_bytes=len(current or b"") + len(old_index) + old_reserve,
                )
                self._replace_private_file(staged / "index.json", self._index_bytes(updated))
                self._fsync_directory(staged)
                if not history:
                    staged.rename(directory)
                self._fsync_directory(directory.parent)
            finally:
                if not history and staged.exists():
                    shutil.rmtree(staged)
            # The durable owner precedes the atomic replacement. Recovery snapshots
            # whichever complete live version survived, never a second byte store.
            with _directory_descriptor(self._files_path(), self._parts(path)[:-1], RootFileError) as descriptor:
                temporary, _ = _stage_upload(descriptor, target.name, BytesIO(data), max_bytes=self.MAX_OPERATION_BYTES)
                try:
                    if current is None:
                        _publish_upload(descriptor, temporary, target.name)
                    else:
                        os.replace(temporary, target.name, src_dir_fd=descriptor, dst_dir_fd=descriptor)
                        os.fsync(descriptor)
                finally:
                    try:
                        os.unlink(temporary, dir_fd=descriptor)
                    except FileNotFoundError:
                        pass
            return self.working_revision(path)

    def working_revision(self, relative_path: str) -> FileRevision | WorkingFileRevision:
        """Return current metadata; pending references use run_id, never a revision number."""
        path = self.normalize_relative_path(relative_path)
        with self._session_operation_lock(FileRoot.SESSION):
            history = self._history_for_path(path)
            if history and (owner := history[1].get("run")) and owner["state"] != "final":
                with self.open_file(FileRoot.SESSION, path) as handle:
                    metadata = os.fstat(handle.fileno())
                    digest = hashlib.file_digest(handle, "sha256").hexdigest()
                return WorkingFileRevision(
                    UUID(history[1]["fileId"]), UUID(owner["runId"]), path,
                    metadata.st_size, digest, datetime.fromtimestamp(metadata.st_mtime, UTC).isoformat(),
                )
        revisions = self.list_revisions(path)
        current = next((item for item in revisions if item.current), None)
        if current is None:
            raise RootFileError("file.revisionNotFound", "Revision not found")
        return current

    def pending_run_ids(self) -> tuple[UUID, ...]:
        return self.session_storage.pending_run_ids(self.session_id, self.project_id)

    def open_working_file(
        self, file_id: UUID, *, expected_sha256: str | None = None,
    ) -> BinaryIO:
        """Read live bytes by identity, checking the caller's content hash."""
        if self.run_id is None:
            raise RootFileError("file.revisionNotFound", "A run-bound reference is required")
        with self._session_operation_lock(FileRoot.SESSION):
            history = self._history_for_id(file_id)
            if history:
                index = history[1]
                owner = index.get("run", {})
                if owner.get("runId") != str(self.run_id) or owner.get("sessionId") != str(self.session_id):
                    raise RootFileError("file.revisionNotFound", "Working file not found")
                handle = self.open_file(FileRoot.SESSION, index["path"])
                try:
                    if expected_sha256 is not None and hashlib.file_digest(handle, "sha256").hexdigest() != expected_sha256:
                        raise RootFileError("file.changed", "Working file changed")
                    handle.seek(0)
                    return handle
                except BaseException:
                    handle.close()
                    raise
        raise RootFileError("file.revisionNotFound", "Working file not found")

    def finalize_run(self, run_id: UUID) -> tuple[FileRevision, ...]:
        """Snapshot live bytes once per changed file; replay publication intents on retry."""
        if not isinstance(run_id, UUID):
            raise TypeError("run_id must be supplied by the host as a UUID")
        results = []
        with self._session_operation_lock(FileRoot.SESSION, quota_check=True):
            for directory, index in self._revision_indexes():
                owner = index.get("run", {})
                if owner.get("runId") == str(run_id) and owner.get("sessionId") == str(self.session_id):
                    if owner["state"] != "final":
                        target = self._ensure_no_symlinks(self._files_path(), self._parts(index["path"]), allow_missing_final=True)
                        if not target.exists():
                            if index["revisions"] or owner["initialSha256"] is not None:
                                raise RootFileError("file.changed", "Run file was removed outside storage")
                            shutil.rmtree(directory)
                            self._fsync_directory(directory.parent)
                            continue
                        with self.open_file(FileRoot.SESSION, index["path"]) as source:
                            metadata = os.fstat(source.fileno())
                            digest = hashlib.file_digest(source, "sha256").hexdigest()
                            source.seek(0)
                            item = owner.get("publication")
                            if item is None:
                                current = next((item for item in index["revisions"] if item["revision"] == index["currentRevision"]), None)
                                unchanged = digest == (current["sha256"] if current else owner["initialSha256"])
                                if not unchanged:
                                    item = {**self._revision_metadata(index["maxRevision"] + 1, metadata.st_size, digest),
                                            "runId": str(run_id), "sessionId": str(self.session_id)}
                                    self._assert_user_quota(
                                        metadata.st_size + 1024, replaced_bytes=owner["reserveBytes"],
                                    )
                                    owner = {**owner, "state": "publishing", "publication": item}
                                    index = {**index, "run": owner}
                                    self._replace_private_file(directory / "index.json", self._index_bytes(index))
                                    self._fsync_directory(directory)
                            if item is not None:
                                blob = directory / str(item["revision"])
                                if blob.exists() or blob.is_symlink():
                                    with _open_regular_file(directory, (str(item["revision"]),), RootFileError) as snapshot:
                                        snapshot_metadata = os.fstat(snapshot.fileno())
                                        snapshot_digest = hashlib.file_digest(snapshot, "sha256").hexdigest()
                                    if snapshot_metadata.st_size != item["sizeBytes"] or snapshot_digest != item["sha256"]:
                                        raise RootFileError("file.revisionInvalid", "Invalid publication snapshot")
                                else:
                                    if digest != item["sha256"] or metadata.st_size != item["sizeBytes"]:
                                        raise RootFileError("file.changed", "File changed during publication")
                                    self._copy_private_file(source, blob, item["sizeBytes"], item["sha256"])
                                    self._fsync_directory(directory)
                                index = {**index, "currentRevision": item["revision"], "maxRevision": item["revision"],
                                         "revisions": [*index["revisions"], item]}
                        owner = {"runId": str(run_id), "sessionId": str(self.session_id),
                                 "state": "final", "revision": index["currentRevision"]}
                        index = {**index, "run": owner, "runReceipts": {
                            **index.get("runReceipts", {}), f"{self.session_id}/{run_id}": index["currentRevision"],
                        }}
                        self._replace_private_file(directory / "index.json", self._index_bytes(index))
                        self._fsync_directory(directory)
                    self._complete_derived_binding(index["fileId"], str(run_id), owner["revision"])
                for item in index["revisions"]:
                    if (item.get("runId") == str(run_id) and item.get("sessionId") == str(self.session_id)) or (
                        index.get("runReceipts", {}).get(f"{self.session_id}/{run_id}") == item["revision"]
                    ):
                        results.append(FileRevision(
                            UUID(index["fileId"]), item["revision"], index["path"],
                            item["sizeBytes"], item["sha256"], item["createdAt"], item["revision"] == index["currentRevision"],
                        ))
        return tuple(sorted(results, key=lambda item: str(item.file_id)))

    def _revisions_path(self) -> Path:
        path = self._root_path(FileRoot.SESSION) / "revisions"
        self._ensure_directory(path)
        return path

    def _anchored_parts(
        self, root: FileRoot | str, parts: tuple[str, ...]
    ) -> tuple[Path, tuple[str, ...]]:
        self._selected_root(root)
        try:
            files_root = self._files_path(root)
        except SessionStorageError as exc:
            raise RootFileError(
                "file.sessionNotProvisioned", "Session root is missing"
            ) from exc
        return files_root, parts

    @staticmethod
    def _selected_root(root: FileRoot | str) -> FileRoot:
        try:
            selected = FileRoot(root)
        except ValueError as exc:
            raise RootFileError("file.invalidRoot", "Invalid file root") from exc
        if selected is FileRoot.WORKSPACE:
            raise RootFileError(
                "file.workspaceNotSelected", "No working directory is selected"
            )
        return selected

    @contextmanager
    def _session_operation_lock(
        self, *roots: FileRoot, quota_check: bool = False,
    ) -> Iterator[None]:
        if getattr(self._operation_state, "locked", False):
            yield
            return
        with self.session_storage.lock:
            for root in roots:
                self._root_path(root)
            self._operation_state.locked = True
            try:
                for trash in self._revisions_path().glob(".delete-owned-*"):
                    try:
                        SessionStorage._remove_path(trash)
                    except OSError:
                        logger.warning("File trash cleanup deferred", exc_info=True)
                if quota_check and self.user_quota_bytes is not None:
                    self._operation_state.initial_usage = self._user_usage(include_reservations=True)
                yield
            finally:
                self._operation_state.locked = False

    @staticmethod
    def _tree_usage(root: Path, *, operation_limits: bool) -> TreeUsage:
        entries = 0
        size_bytes = 0
        pending = [root]
        while pending:
            path = pending.pop()
            metadata = RootFileService._metadata(path)
            if stat.S_ISLNK(metadata.st_mode):
                if operation_limits:
                    raise RootFileError(
                        "file.symlinkForbidden", "Symlinks are not followed"
                    )
                size_bytes += metadata.st_size
            elif stat.S_ISREG(metadata.st_mode):
                size_bytes += metadata.st_size
            elif stat.S_ISDIR(metadata.st_mode):
                pending.extend(path.iterdir())
            elif operation_limits:
                raise RootFileError(
                    "file.unsupportedType", "Unsupported file type"
                )
            else:
                size_bytes += metadata.st_size
            entries += 1
            if operation_limits and (
                entries > RootFileService.MAX_OPERATION_ENTRIES
                or size_bytes > RootFileService.MAX_OPERATION_BYTES
            ):
                raise RootFileError(
                    "file.operationTooLarge",
                    "Source exceeds the file operation limit",
                )
        return TreeUsage(entries=entries, bytes=size_bytes)

    def _operation_usage(self, source: Path) -> TreeUsage:
        return self._tree_usage(source, operation_limits=True)

    def _session_usage(self) -> int:
        session_root = self._root_path(FileRoot.SESSION)
        return sum(
            self._tree_usage(session_root / name, operation_limits=False).bytes
            for name in ("files", "revisions")
        )

    def session_usage(self) -> int:
        """Return usage of the mutable file and revision store."""

        with self._session_operation_lock(FileRoot.SESSION):
            return self._session_usage()

    def _user_usage(self, *, include_reservations: bool = False) -> int:
        if self.user_id is None:
            raise ValueError("User storage usage requires an owner")
        total = 0
        for path in self.session_storage.user_session_roots(self.user_id):
            service = RootFileService(
                UUID(path.name), session_storage=self.session_storage,
                project_id=None if path.parent == self.session_storage.sessions_dir else UUID(path.parents[1].name),
            )
            total += service._session_usage()
            if include_reservations:
                total += sum(index.get("run", {}).get("reserveBytes", 0) for _, index in service._revision_indexes())
        return total

    def user_usage(self) -> int:
        """Return managed file and revision usage across all owned sessions."""
        with self._session_operation_lock(FileRoot.SESSION):
            return self._user_usage()

    def _assert_user_quota(
        self, additional_bytes: int, *, replaced_bytes: int = 0
    ) -> None:
        if self.user_quota_bytes is None:
            return
        used_bytes = self._user_usage(include_reservations=True)
        ceiling = max(self.user_quota_bytes, self._operation_state.initial_usage)
        if used_bytes - replaced_bytes + additional_bytes > ceiling:
            raise RootFileError(
                "file.userQuotaExceeded", "User storage limit has been reached"
            )

    def resolve(self, root: FileRoot | str, relative_path: str) -> Path:
        parts = self._parts(relative_path)
        return self._ensure_no_symlinks(self._files_path(root), parts)

    def path_kind(self, root: FileRoot | str, relative_path: str) -> str:
        metadata = self._metadata(self.resolve(root, relative_path))
        if stat.S_ISDIR(metadata.st_mode):
            return "directory"
        if stat.S_ISREG(metadata.st_mode):
            return "file"
        raise RootFileError("file.unsupportedType", "Unsupported file type")

    @contextmanager
    def open_directory_descriptor(
        self, root: FileRoot | str, relative_path: str
    ) -> Iterator[int]:
        parts = self._parts(relative_path)
        anchor, anchored_parts = self._anchored_parts(root, parts)
        with _directory_descriptor(anchor, anchored_parts, RootFileError) as descriptor:
            yield descriptor

    def _resolve_destination(self, root: FileRoot | str, relative_path: str) -> Path:
        parts = self._parts(relative_path)
        self._assert_mutable(root, parts)
        self._assert_available(self._relative_path(parts))
        if not parts:
            raise RootFileError("file.rootMutationForbidden", "A file root is immutable")
        root_path = self._files_path(root)
        parent = self._ensure_no_symlinks(root_path, parts[:-1])
        self._ensure_directory(parent, code="file.parentNotFound")
        target = parent / parts[-1]
        if target.exists() or target.is_symlink():
            raise RootFileError("file.pathAlreadyExists", "Destination path already exists")
        return target

    def _assert_available(self, path: str, *, allow_own: bool = True) -> None:
        """Check file/subtree ownership while holding the logical root lock."""
        for _, index in self._revision_indexes():
            owner = index.get("run")
            if not owner or owner["state"] == "final":
                continue
            owned_path = index["path"]
            if path == owned_path or path.startswith(owned_path + "/") or owned_path.startswith(path + "/"):
                if not (allow_own and owner["runId"] == str(self.run_id)
                        and owner["sessionId"] == str(self.session_id)):
                    raise RootFileError("file.changed", "File is owned by an active run")

    @staticmethod
    def _assert_mutable(root: FileRoot | str, parts: tuple[str, ...]) -> None:
        try:
            FileRoot(root)
        except ValueError as exc:
            raise RootFileError("file.invalidRoot", "Invalid file root") from exc
        if not parts:
            raise RootFileError("file.rootMutationForbidden", "A file root is immutable")

    def list_entries(
        self,
        root: FileRoot | str,
        relative_path: str = ".",
        *,
        offset: int = 0,
        limit: int = 100,
    ) -> RootFileListResult:
        if isinstance(offset, bool) or offset < 0:
            raise RootFileError("file.invalidPagination", "Invalid list offset")
        if isinstance(limit, bool) or not 1 <= limit <= self.MAX_LIST_LIMIT:
            raise RootFileError("file.invalidPagination", "Invalid list limit")
        parts = self._parts(relative_path)
        entries: list[RootFileEntry] = []
        anchor, anchored_parts = self._anchored_parts(root, parts)
        with _directory_descriptor(anchor, anchored_parts, RootFileError) as descriptor:
            for name in os.listdir(descriptor):
                metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if stat.S_ISLNK(metadata.st_mode):
                    kind = "symlink"
                    size: int | None = metadata.st_size
                elif stat.S_ISDIR(metadata.st_mode):
                    kind = "directory"
                    size = None
                elif stat.S_ISREG(metadata.st_mode):
                    kind = "file"
                    size = metadata.st_size
                else:
                    kind = "other"
                    size = metadata.st_size
                item_parts = (*parts, name)
                entries.append(
                    RootFileEntry(
                        name=name,
                        path=self._relative_path(item_parts),
                        kind=kind,
                        size=size,
                        modified_at=metadata.st_mtime,
                    )
                )
        entries.sort(key=lambda entry: (entry.kind != "directory", entry.name.casefold(), entry.name))
        page = tuple(entries[offset : offset + limit])
        next_offset = offset + len(page)
        has_more = next_offset < len(entries)
        return RootFileListResult(
            entries=page,
            has_more=has_more,
            next_offset=next_offset if has_more else None,
        )

    def list_directory(
        self,
        root: FileRoot | str,
        relative_path: str = ".",
        *,
        offset: int = 0,
        limit: int = 100,
    ) -> RootFileListResult:
        return self.list_entries(root, relative_path, offset=offset, limit=limit)

    def read_file(
        self,
        root: FileRoot | str,
        relative_path: str,
        *,
        offset: int = 1,
        limit: int = 2_000,
        max_line_length: int = 2_000,
    ) -> FileReadResult:
        if isinstance(offset, bool) or offset < 1 or isinstance(limit, bool) or limit < 1:
            raise RootFileError("file.invalidPagination", "Invalid read range")
        if isinstance(max_line_length, bool) or max_line_length < 1:
            raise RootFileError("file.invalidPagination", "Invalid line length")
        path = self.resolve(root, relative_path)
        metadata = self._metadata(path)
        if not stat.S_ISREG(metadata.st_mode):
            raise RootFileError("file.fileNotFound", "File not found")

        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise RootFileError("file.fileNotFound", "File not found") from exc
        try:
            with os.fdopen(descriptor, "rb") as binary_handle:
                return self._read_text_handle(
                    binary_handle,
                    offset=offset,
                    limit=limit,
                    max_line_length=max_line_length,
                )
        except UnicodeDecodeError as exc:
            raise RootFileError("file.invalidEncoding", "File is not readable as UTF-8") from exc

    @staticmethod
    def _read_text_handle(
        binary_handle: BinaryIO,
        *,
        offset: int,
        limit: int,
        max_line_length: int,
    ) -> FileReadResult:
        lines: list[str] = []
        truncated_lines: list[int] = []
        digest = hashlib.file_digest(binary_handle, "sha256").hexdigest()
        binary_handle.seek(0)
        with TextIOWrapper(binary_handle, encoding="utf-8") as handle:
            for _ in range(1, offset):
                if _read_bounded_line(handle, max_line_length) is None:
                    break
            for line_number in range(offset, offset + limit):
                line = _read_bounded_line(handle, max_line_length)
                if line is None:
                    break
                value, truncated = line
                lines.append(f"{line_number}: {value}")
                if truncated:
                    truncated_lines.append(line_number)
            has_more = _read_bounded_line(handle, max_line_length) is not None
        lines_read = len(lines)
        return FileReadResult(
            content="\n".join(lines),
            lines_read=lines_read,
            has_more=has_more,
            next_offset=offset + lines_read if has_more else None,
            truncated_lines=tuple(truncated_lines),
            sha256=digest,
        )

    @staticmethod
    def _read_private_file(path: Path) -> bytes:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise RootFileError("file.revisionInvalid", "Invalid revision history") from exc
        with os.fdopen(descriptor, "rb") as handle:
            metadata = os.fstat(handle.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise RootFileError("file.revisionInvalid", "Invalid revision history")
            return handle.read()

    def _replace_private_file(self, path: Path, content: bytes) -> None:
        temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
        try:
            self._write_private_file(temporary, content)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def write_file(
        self,
        root: FileRoot | str,
        relative_path: str,
        content: str,
        *,
        mode: str = "create",
        expected_sha256: str | None = None,
    ) -> str:
        if not isinstance(content, str):
            raise TypeError("content must be text")
        if mode not in {"create", "replace"}:
            raise RootFileError("file.invalidWriteMode", "Invalid write mode")
        if mode == "replace":
            if expected_sha256 is None:
                raise RootFileError("file.expectedSha256Required", "Expected SHA-256 is required")
            self.replace_text_file(root, relative_path, content, expected_sha256)
            return self.normalize_relative_path(relative_path)
        if expected_sha256 is not None:
            raise RootFileError("file.invalidWriteMode", "Create does not accept expected SHA-256")
        selected_root = self._selected_root(root)
        parts = self._parts(relative_path)
        self._assert_mutable(selected_root, parts)
        encoded_content = content.encode("utf-8")
        if self.run_id is not None:
            self._write_run_file(self._relative_path(parts), encoded_content)
            return self._relative_path(parts)
        with self._session_operation_lock(selected_root, quota_check=True):
            self._assert_available(self._relative_path(parts))
            root_path = self._files_path(selected_root)
            parent = self._ensure_no_symlinks(root_path, parts[:-1])
            self._ensure_directory(parent, code="file.parentNotFound")
            target = parent / parts[-1]
            if target.exists() or target.is_symlink():
                raise RootFileError("file.pathAlreadyExists", "Destination path already exists")

            temporary = parent / f".{parts[-1]}.{uuid4().hex}.tmp"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(temporary, flags, 0o600)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(encoded_content)
                    handle.flush()
                    os.fsync(handle.fileno())
                staged_history, history, _index = self._stage_initial_history(
                    self._relative_path(parts), encoded_content
                )
                self._assert_user_quota(0)
                staged_history.rename(history)
                try:
                    temporary.rename(target)
                except BaseException:
                    shutil.rmtree(history, ignore_errors=True)
                    raise
                target.chmod(0o600)
                self._fsync_directory(parent)
                self._fsync_directory(history.parent)
            except BaseException:
                temporary.unlink(missing_ok=True)
                if "staged_history" in locals():
                    shutil.rmtree(staged_history, ignore_errors=True)
                raise
        return self._relative_path(parts)

    def upsert_text_file(
        self,
        root: FileRoot | str,
        relative_path: str,
        content: str,
    ) -> str:
        path = self.normalize_relative_path(relative_path)
        encoded = content.encode("utf-8")
        if len(encoded) > self.max_editable_text_bytes:
            raise RootFileError("file.tooLarge", "File is too large")
        if "\0" in content:
            raise RootFileError("file.invalidEncoding", "File is not editable UTF-8 text")
        for _attempt in range(3):
            try:
                with self.open_file(root, path) as handle:
                    current = handle.read(self.max_editable_text_bytes + 1)
            except RootFileError as exc:
                if exc.code not in {"file.fileNotFound", "file.pathNotFound"}:
                    raise
                try:
                    return self.write_file(root, path, content)
                except RootFileError as create_error:
                    if create_error.code != "file.pathAlreadyExists":
                        raise
                    continue
            if len(current) > self.max_editable_text_bytes:
                raise RootFileError("file.tooLarge", "File is too large")
            try:
                self.replace_text_file(
                    root,
                    path,
                    content,
                    hashlib.sha256(current).hexdigest(),
                )
                return path
            except RootFileError as exc:
                if exc.code != "file.changed":
                    raise
        raise RootFileError("file.changed", "File changed while it was written")

    def edit_text_file(
        self,
        root: FileRoot | str,
        relative_path: str,
        old_text: str,
        new_text: str,
        *,
        replace_all: bool = False,
        expected_sha256: str | None = None,
    ) -> str:
        path = self.normalize_relative_path(relative_path)
        for _attempt in range(3):
            data = self.read_bytes(root, path, limit=self.max_editable_text_bytes + 1)
            if len(data) > self.max_editable_text_bytes:
                raise RootFileError("file.tooLarge", "File is too large")
            digest = hashlib.sha256(data).hexdigest()
            if expected_sha256 is not None and digest != expected_sha256:
                raise RootFileError("file.changed", "File changed since it was read")
            try:
                content = data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise RootFileError(
                    "file.invalidEncoding", "File is not editable UTF-8 text"
                ) from exc
            ending = "\r\n" if "\r\n" in content else "\n"
            old = old_text.replace("\r\n", "\n").replace("\n", ending)
            new = new_text.replace("\r\n", "\n").replace("\n", ending)
            index = content.find(old)
            if index < 0:
                raise RootFileError("file.editTextNotFound", "Edit text was not found")
            if not replace_all and content.find(old, index + 1) >= 0:
                raise RootFileError("file.editTextNotUnique", "Edit text is not unique")
            updated = (
                content.replace(old, new)
                if replace_all
                else content[:index] + new + content[index + len(old) :]
            )
            try:
                self.replace_text_file(
                    root,
                    path,
                    updated,
                    digest,
                )
                return path
            except RootFileError as exc:
                if exc.code != "file.changed":
                    raise
        raise RootFileError("file.changed", "File changed while it was edited")

    def _revision_indexes(self) -> list[tuple[Path, dict]]:
        revisions = self._revisions_path()
        result = []
        for directory in revisions.iterdir():
            if directory.name.startswith("."):
                continue
            metadata = self._metadata(directory)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise RootFileError("file.revisionInvalid", "Invalid revision history")
            index_path = directory / "index.json"
            try:
                data = self._read_private_file(index_path)
                index = json.loads(data.decode("utf-8"))
                file_id = UUID(index["fileId"])
                path = self.normalize_relative_path(index["path"])
                current = index["currentRevision"]
                items = index["revisions"]
                valid = (
                    str(file_id) == directory.name == index["fileId"]
                    and path == index["path"]
                    and path != "."
                    and isinstance(index.get("deleted", False), bool)
                    and isinstance(items, list)
                    and all(
                        isinstance(item, dict)
                        and isinstance(item.get("revision"), int)
                        and item["revision"] >= 1
                        and isinstance(item.get("createdAt"), str)
                        and isinstance(item.get("sizeBytes"), int)
                        and item["sizeBytes"] >= 0
                        and isinstance(item.get("sha256"), str)
                        and len(item["sha256"]) == 64
                        for item in items
                    )
                    and [item["revision"] for item in items]
                    == sorted({item["revision"] for item in items})
                    and all(
                        "previousRevision" not in item
                        or type(item["previousRevision"]) is int
                        and item["previousRevision"] != item["revision"]
                        and any(
                            candidate["revision"] == item["previousRevision"]
                            for candidate in items
                        )
                        for item in items
                    )
                    and (current is None and not items or type(current) is int and any(item["revision"] == current for item in items))
                    and (
                        "maxRevision" not in index
                        or isinstance(index["maxRevision"], int)
                        and not isinstance(index["maxRevision"], bool)
                        and index["maxRevision"] >= max((item["revision"] for item in items), default=0)
                    )
                )
            except (KeyError, OSError, TypeError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
                raise RootFileError("file.revisionInvalid", "Invalid revision history") from exc
            if not valid:
                raise RootFileError("file.revisionInvalid", "Invalid revision history")
            try:
                for key, revision in index.get("runReceipts", {}).items():
                    session_id, run_id = key.split("/")
                    if (str(UUID(session_id)) != session_id or str(UUID(run_id)) != run_id
                            or type(revision) is not int or not any(item["revision"] == revision for item in items)):
                        raise ValueError("Invalid run receipt")
            except (AttributeError, TypeError, ValueError) as exc:
                raise RootFileError("file.revisionInvalid", "Invalid run receipt") from exc
            owner = index.get("run")
            if owner is not None:
                try:
                    valid_owner = (
                        isinstance(owner["runId"], str) and str(UUID(owner["runId"])) == owner["runId"]
                        and isinstance(owner["sessionId"], str) and str(UUID(owner["sessionId"])) == owner["sessionId"]
                        and owner["state"] in {"pending", "publishing", "final"}
                        and type(owner.get("reserveBytes", 0)) is int
                        and owner.get("reserveBytes", 0) >= 0
                    )
                    if owner["state"] != "final":
                        initial = owner["initialSha256"]
                        valid_owner = valid_owner and owner["reserveBytes"] >= 1024 and (
                            initial is None or isinstance(initial, str) and len(initial) == 64
                        )
                    if owner["state"] == "publishing":
                        item = owner["publication"]
                        valid_owner = valid_owner and (
                            type(item["revision"]) is int and item["revision"] == index["maxRevision"] + 1
                            and isinstance(item["sha256"], str) and len(item["sha256"]) == 64
                            and type(item["sizeBytes"]) is int and item["sizeBytes"] >= 0
                            and isinstance(item["createdAt"], str)
                            and item["runId"] == owner["runId"] and item["sessionId"] == owner["sessionId"]
                        )
                    if owner["state"] == "final":
                        valid_owner = valid_owner and (owner["revision"] is None or any(item["revision"] == owner["revision"] for item in items))
                except (KeyError, TypeError, ValueError) as exc:
                    raise RootFileError("file.revisionInvalid", "Invalid run ownership") from exc
                if not valid_owner:
                    raise RootFileError("file.revisionInvalid", "Invalid run ownership")
            index.setdefault("maxRevision", max((item["revision"] for item in items), default=0))
            result.append((directory, index))
        return result

    def _history_for_path(self, path: str) -> tuple[Path, dict] | None:
        matches = [
            item
            for item in self._revision_indexes()
            if item[1]["path"] == path and not item[1].get("deleted", False)
        ]
        if len(matches) > 1:
            raise RootFileError("file.revisionInvalid", "Duplicate revision history")
        return matches[0] if matches else None

    def _history_for_id(self, file_id: UUID) -> tuple[Path, dict] | None:
        return next(
            (
                item
                for item in self._revision_indexes()
                if item[1]["fileId"] == str(file_id)
            ),
            None,
        )

    @staticmethod
    def _revision_item(revision: int, data: bytes) -> dict[str, str | int]:
        return RootFileService._revision_metadata(
            revision, len(data), hashlib.sha256(data).hexdigest()
        )

    @staticmethod
    def _revision_metadata(
        revision: int, size_bytes: int, sha256: str
    ) -> dict[str, str | int]:
        return {
            "revision": revision,
            "createdAt": datetime.now(UTC).isoformat(),
            "sizeBytes": size_bytes,
            "sha256": sha256,
        }

    @staticmethod
    def _copy_private_file(
        source: BinaryIO, target: Path, expected_size: int, expected_sha256: str
    ) -> None:
        temporary = target.parent / f".{target.name}.{uuid4().hex}.tmp"
        digest = hashlib.sha256()
        size = 0
        try:
            source.seek(0)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(temporary, flags, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                while chunk := source.read(1024 * 1024):
                    size += len(chunk)
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if size != expected_size or digest.hexdigest() != expected_sha256:
                raise RootFileError("file.changed", "File changed during publication")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _index_bytes(index: dict) -> bytes:
        if "runReceipts" in index:
            # Every history truncation invalidates receipts for removed revisions.
            retained = {item["revision"] for item in index["revisions"]}
            index = {**index, "runReceipts": {key: revision for key, revision in index["runReceipts"].items() if revision in retained}}
        return json.dumps(index, ensure_ascii=True, separators=(",", ":")).encode("utf-8")

    def _stage_initial_history(self, path: str, data: bytes | BinaryIO, *, baseline: bool = False) -> tuple[Path, Path, dict]:
        revisions = self._revisions_path()
        file_id = uuid4()
        target = revisions / str(file_id)
        staged = revisions / f".{file_id}.{uuid4().hex}.tmp"
        if isinstance(data, bytes):
            item = self._revision_item(1, data)
        else:
            size = data.seek(0, os.SEEK_END)
            data.seek(0)
            item = self._revision_metadata(1, size, hashlib.file_digest(data, "sha256").hexdigest())
        index = {
            "fileId": str(file_id),
            "path": path,
            "currentRevision": 1,
            "maxRevision": 1,
            "revisions": [item],
        }
        if self.run_id is not None and not baseline:
            index.update(currentRevision=None, maxRevision=0, revisions=[], run={
                "sessionId": str(self.session_id), "runId": str(self.run_id),
                "state": "pending", "initialSha256": None, "reserveBytes": item["sizeBytes"] + 1024,
            })
        staged.mkdir(mode=0o700)
        try:
            if self.run_id is None or baseline:
                if isinstance(data, bytes):
                    self._write_private_file(staged / "1", data)
                else:
                    self._copy_private_file(data, staged / "1", item["sizeBytes"], item["sha256"])
            self._write_private_file(staged / "index.json", self._index_bytes(index))
        except BaseException:
            shutil.rmtree(staged, ignore_errors=True)
            raise
        return staged, target, index

    def _publish_initial_history(self, path: str, data: bytes) -> tuple[Path, dict]:
        staged, target, index = self._stage_initial_history(path, data)
        try:
            self._assert_user_quota(index.get("run", {}).get("reserveBytes", 0))
            staged.rename(target)
            self._fsync_directory(target.parent)
        except BaseException:
            shutil.rmtree(staged, ignore_errors=True)
            raise
        return target, index

    def list_revisions(self, relative_path: str) -> tuple[FileRevision, ...]:
        path = self.normalize_relative_path(relative_path)
        with self._session_operation_lock(FileRoot.SESSION):
            history = self._history_for_path(path)
            if history is None:
                return ()
            directory, index = history
            file_id = UUID(index["fileId"])
            revisions = []
            for item in reversed(index["revisions"]):
                metadata = self._metadata(directory / str(item["revision"]))
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                    raise RootFileError("file.revisionInvalid", "Invalid revision history")
                revisions.append(
                    FileRevision(
                        file_id=file_id,
                        revision=item["revision"],
                        path=path,
                        size_bytes=item["sizeBytes"],
                        sha256=item["sha256"],
                        created_at=item["createdAt"],
                        current=item["revision"] == index["currentRevision"],
                    )
                )
            return tuple(revisions)

    def open_revision(self, relative_path: str, revision: int) -> BinaryIO:
        if isinstance(revision, bool) or revision < 1:
            raise RootFileError("file.revisionNotFound", "Revision not found")
        path = self.normalize_relative_path(relative_path)
        with self._session_operation_lock(FileRoot.SESSION):
            history = self._history_for_path(path)
            if history is None or not any(
                item["revision"] == revision for item in history[1]["revisions"]
            ):
                raise RootFileError("file.revisionNotFound", "Revision not found")
            return _open_regular_file(history[0], (str(revision),), RootFileError)

    def open_revision_by_id(self, file_id: UUID, revision: int) -> BinaryIO:
        if isinstance(revision, bool) or revision < 1:
            raise RootFileError("file.revisionNotFound", "Revision not found")
        with self._session_operation_lock(FileRoot.SESSION):
            history = self._history_for_id(file_id)
            if history is None or not any(
                item["revision"] == revision for item in history[1]["revisions"]
            ):
                raise RootFileError("file.revisionNotFound", "Revision not found")
            return _open_regular_file(history[0], (str(revision),), RootFileError)

    def revision_path(self, file_id: UUID, revision: int) -> Path:
        with self._session_operation_lock(FileRoot.SESSION):
            history = self._history_for_id(file_id)
            if history is None or not any(
                item["revision"] == revision for item in history[1]["revisions"]
            ):
                raise RootFileError("file.revisionNotFound", "Revision not found")
            path = history[0] / str(revision)
            metadata = self._metadata(path)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise RootFileError("file.revisionInvalid", "Invalid revision history")
            return path

    def write_revision_derivatives(
        self, file_id: UUID, revision: int, files: dict[str, bytes]
    ) -> dict[str, str]:
        if not files:
            return {}
        with self._session_operation_lock(FileRoot.SESSION, quota_check=True):
            history = self._history_for_id(file_id)
            if history is None or not any(
                item["revision"] == revision for item in history[1]["revisions"]
            ):
                raise RootFileError("file.revisionNotFound", "Revision not found")
            root = history[0] / "derived" / str(revision)
            if root.exists() or root.is_symlink():
                raise RootFileError("file.pathAlreadyExists", "Derived output already exists")
            root.mkdir(mode=0o700, parents=True)
            try:
                for name, data in files.items():
                    validate_upload_filename(name)
                    self._write_private_file(root / name, data)
                self._assert_user_quota(0)
            except BaseException:
                shutil.rmtree(root, ignore_errors=True)
                raise
        return {
            name: f"revisions/{file_id}/derived/{revision}/{name}"
            for name in files
        }

    def revision_derived_path(
        self, file_id: UUID, revision: int, name: str
    ) -> Path:
        validate_upload_filename(name)
        with self._session_operation_lock(FileRoot.SESSION):
            history = self._history_for_id(file_id)
            if history is None or not any(
                item["revision"] == revision for item in history[1]["revisions"]
            ):
                raise RootFileError("file.revisionNotFound", "Revision not found")
            path = history[0] / "derived" / str(revision) / name
            metadata = self._metadata(path)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise RootFileError("file.revisionInvalid", "Invalid revision derivative")
            return path

    def file_revision(self, file_id: UUID, revision: int) -> FileRevision:
        with self._session_operation_lock(FileRoot.SESSION):
            history = self._history_for_id(file_id)
            if history is None:
                raise RootFileError("file.revisionNotFound", "Revision not found")
            _directory, index = history
            item = next(
                (item for item in index["revisions"] if item["revision"] == revision),
                None,
            )
            if item is None:
                raise RootFileError("file.revisionNotFound", "Revision not found")
            return FileRevision(
                file_id=file_id,
                revision=revision,
                path=index["path"],
                size_bytes=item["sizeBytes"],
                sha256=item["sha256"],
                created_at=item["createdAt"],
                current=revision == index["currentRevision"],
            )

    def create_output_file(self, filename: str, data: bytes) -> FileRevision | WorkingFileRevision:
        return self.create_output_stream(
            filename, BytesIO(data), max_bytes=self.MAX_OPERATION_BYTES,
        )

    def create_output_stream(
        self, filename: str, source: BinaryIO, *, max_bytes: int
    ) -> FileRevision | WorkingFileRevision:
        name = validate_upload_filename(filename, max_bytes=None)
        stem = PurePosixPath(name).stem or "output"
        # Reserve nine bytes for uniqueness and at least one byte for the stem.
        suffix = PurePosixPath(name).suffix.encode("utf-8")[:230].decode("utf-8", errors="ignore").rstrip(" .")
        stem = stem.encode("utf-8")[:231 - len(suffix.encode("utf-8"))].decode("utf-8", errors="ignore") or "o"
        path = f"{stem}-{uuid4().hex[:8]}{suffix}"
        self.create_file(FileRoot.SESSION, path, source, max_bytes=max_bytes)
        return self.working_revision(path)

    def discard_run_output(self, file_id: UUID) -> None:
        if self.run_id is None:
            raise RootFileError("file.changed", "A run-bound output is required")
        with self._session_operation_lock(FileRoot.SESSION):
            history = self._history_for_id(file_id)
            if history is None:
                return
            owner = history[1].get("run", {})
            if (
                owner.get("runId") != str(self.run_id)
                or owner.get("sessionId") != str(self.session_id)
                or owner.get("state") == "final"
            ):
                raise RootFileError("file.changed", "Output is not owned by this run")
            self._purge_owned_files({str(file_id)})

    def _create_run_file_stream(
        self, path: str, source: BinaryIO, *, max_bytes: int
    ) -> RootFileEntry:
        parts = self._parts(path)
        with self._session_operation_lock(FileRoot.SESSION, quota_check=True):
            self._assert_available(path)
            target = self._resolve_destination(FileRoot.SESSION, path)
            with _directory_descriptor(
                self._files_path(FileRoot.SESSION), parts[:-1], RootFileError
            ) as descriptor:
                try:
                    temporary, size = _stage_upload(
                        descriptor, parts[-1], source, max_bytes=max_bytes
                    )
                except _UploadTooLarge as exc:
                    raise RootFileError("file.tooLarge", "File is too large") from exc
                try:
                    staged_handle = os.open(
                        temporary,
                        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=descriptor,
                    )
                    with os.fdopen(staged_handle, "rb") as handle:
                        digest = hashlib.file_digest(handle, "sha256").hexdigest()
                    revisions = self._revisions_path()
                    file_id = uuid4()
                    history = revisions / str(file_id)
                    staged_history = revisions / f".{file_id}.{uuid4().hex}.tmp"
                    staged_history.mkdir(mode=0o700)
                    index = {
                        "fileId": str(file_id),
                        "path": path,
                        "currentRevision": None,
                        "maxRevision": 0,
                        "revisions": [],
                        "run": {
                            "sessionId": str(self.session_id),
                            "runId": str(self.run_id),
                            "state": "pending",
                            "initialSha256": None,
                            "reserveBytes": size + 1024,
                        },
                    }
                    self._write_private_file(
                        staged_history / "index.json", self._index_bytes(index)
                    )
                    self._assert_user_quota(size + 1024)
                    staged_history.rename(history)
                    self._fsync_directory(revisions)
                    metadata = _publish_upload(descriptor, temporary, target.name)
                except BaseException:
                    if "history" in locals() and history.exists():
                        shutil.rmtree(history, ignore_errors=True)
                    if "staged_history" in locals():
                        shutil.rmtree(staged_history, ignore_errors=True)
                    try:
                        os.unlink(temporary, dir_fd=descriptor)
                    except FileNotFoundError:
                        pass
                    raise
        return RootFileEntry(parts[-1], path, "file", size, metadata.st_mtime)

    def append_output_revision(self, file_id: UUID, data: bytes) -> FileRevision | WorkingFileRevision:
        if not data or len(data) > self.MAX_OPERATION_BYTES:
            raise RootFileError("file.tooLarge", "File is too large")
        with self._session_operation_lock(FileRoot.SESSION, quota_check=True):
            history = self._history_for_id(file_id)
            if history is None or history[1].get("deleted", False):
                raise RootFileError("file.revisionNotFound", "Revision not found")
            directory, index = history
            path = index["path"]
            self._assert_available(path)
            target = self._ensure_no_symlinks(self._files_path(), self._parts(path))
            current = self._read_private_file(target)
            if self.run_id is not None:
                return self._write_run_file(path, data, expected_sha256=hashlib.sha256(current).hexdigest())
            if data == current and index["currentRevision"] is not None:
                return self.working_revision(path)
            revision = index["maxRevision"] + 1
            item = {
                **self._revision_item(revision, data),
                "previousRevision": index["currentRevision"],
            }
            revision_target = directory / str(revision)
            temporary = target.parent / f".{target.name}.{uuid4().hex}.tmp"
            index_temporary = directory / f".index.{uuid4().hex}.tmp"
            old_index = self._read_private_file(directory / "index.json")
            try:
                self._write_private_file(revision_target, data)
                self._write_private_file(temporary, data)
                self._write_private_file(
                    index_temporary,
                    self._index_bytes(
                        {
                            **index,
                            "currentRevision": revision,
                            "maxRevision": revision,
                            "revisions": [*index["revisions"], item],
                        }
                    ),
                )
                self._assert_user_quota(
                    0, replaced_bytes=len(current) + len(old_index)
                )
                os.replace(index_temporary, directory / "index.json")
                try:
                    os.replace(temporary, target)
                except BaseException:
                    self._replace_private_file(directory / "index.json", old_index)
                    revision_target.unlink(missing_ok=True)
                    raise
                self._fsync_directory(target.parent)
                self._fsync_directory(directory)
            except BaseException:
                temporary.unlink(missing_ok=True)
                index_temporary.unlink(missing_ok=True)
                if self._read_private_file(directory / "index.json") == old_index:
                    revision_target.unlink(missing_ok=True)
                raise
        return FileRevision(
            file_id=file_id,
            revision=revision,
            path=path,
            size_bytes=item["sizeBytes"],
            sha256=item["sha256"],
            created_at=item["createdAt"],
            current=True,
        )

    def discard_output_revision(self, file_id: UUID, revision: int) -> None:
        if self.run_id is not None:
            raise RootFileError("file.changed", "Runs must not discard published revisions")
        with self._session_operation_lock(FileRoot.SESSION):
            history = self._history_for_id(file_id)
            if history is None:
                return
            _directory, index = history
            self._assert_available(index["path"], allow_own=False)
            revisions = index["revisions"]
            if not revisions:
                return
            if revision != index["currentRevision"] or revision != revisions[-1]["revision"]:
                return
            if len(revisions) == 1:
                self.delete_path(FileRoot.SESSION, index["path"], recursive=False)
                return
            self._discard_latest_revision(
                index["path"],
                revisions[-1].get("previousRevision", revisions[-2]["revision"]),
                expected_file_id=file_id, expected_current_revision=revision,
            )

    @staticmethod
    def _assert_revision_preconditions(
        index: dict,
        current_data: bytes,
        expected_file_id: UUID,
        expected_current_revision: int,
        expected_current_sha256: str | None,
    ) -> dict:
        current = next(
            (item for item in index["revisions"] if item["revision"] == index["currentRevision"]),
            None,
        )
        if current is None:
            raise RootFileError("file.revisionNotFound", "Revision not found")
        current_sha256 = hashlib.sha256(current_data).hexdigest()
        if current_sha256 != current["sha256"]:
            raise RootFileError("file.revisionInvalid", "Current revision does not match file")
        if (
            index["fileId"] != str(expected_file_id)
            or index["currentRevision"] != expected_current_revision
            or expected_current_sha256 is not None
            and current_sha256 != expected_current_sha256
        ):
            raise RootFileError("file.changed", "File changed since it was read")
        return current

    def activate_revision(
        self,
        relative_path: str,
        revision: int,
        *,
        expected_file_id: UUID,
        expected_current_revision: int,
        expected_current_sha256: str | None = None,
    ) -> RevisionActivationResult:
        if isinstance(revision, bool) or revision < 1:
            raise RootFileError("file.revisionNotFound", "Revision not found")
        path = self.normalize_relative_path(relative_path)
        self._assert_mutable(FileRoot.SESSION, self._parts(path))
        with self._session_operation_lock(FileRoot.SESSION, quota_check=True):
            self._assert_available(path, allow_own=False)
            history = self._history_for_path(path)
            if history is None:
                raise RootFileError("file.changed", "File changed since it was read")
            directory, index = history
            file_id = UUID(index["fileId"])
            target = self._ensure_no_symlinks(self._files_path(), self._parts(path))
            current_data = self._read_private_file(target)
            self._assert_revision_preconditions(
                index,
                current_data,
                expected_file_id,
                expected_current_revision,
                expected_current_sha256,
            )
            selected = next(
                (item for item in index["revisions"] if item["revision"] == revision), None
            )
            if selected is None:
                raise RootFileError("file.revisionNotFound", "Revision not found")
            selected_data = self._read_private_file(directory / str(revision))
            if hashlib.sha256(selected_data).hexdigest() != selected["sha256"]:
                raise RootFileError("file.revisionInvalid", "Invalid revision history")
            if revision == index["currentRevision"]:
                return RevisionActivationResult(file_id, revision, selected["sha256"])

            old_index = self._read_private_file(directory / "index.json")
            updated = self._index_bytes({**index, "currentRevision": revision})
            temporary = target.parent / f".{target.name}.{uuid4().hex}.tmp"
            index_temporary = directory / f".index.{uuid4().hex}.tmp"
            index_replaced = False
            target_replaced = False
            try:
                self._write_private_file(temporary, selected_data)
                self._write_private_file(index_temporary, updated)
                self._assert_user_quota(
                    0, replaced_bytes=len(current_data) + len(old_index)
                )
                os.replace(index_temporary, directory / "index.json")
                index_replaced = True
                os.replace(temporary, target)
                target_replaced = True
                self._fsync_directory(target.parent)
                self._fsync_directory(directory)
            except BaseException:
                if target_replaced:
                    self._replace_private_file(target, current_data)
                if index_replaced:
                    self._replace_private_file(directory / "index.json", old_index)
                self._fsync_directory(target.parent)
                self._fsync_directory(directory)
                temporary.unlink(missing_ok=True)
                index_temporary.unlink(missing_ok=True)
                raise
            return RevisionActivationResult(file_id, revision, selected["sha256"])

    def _discard_latest_revision(
        self,
        relative_path: str,
        revision: int,
        *,
        expected_file_id: UUID,
        expected_current_revision: int,
        expected_current_sha256: str | None = None,
    ) -> None:
        if isinstance(revision, bool) or revision < 1:
            raise RootFileError("file.revisionNotFound", "Revision not found")
        path = self.normalize_relative_path(relative_path)
        self._assert_mutable(FileRoot.SESSION, self._parts(path))
        with self._session_operation_lock(FileRoot.SESSION):
            self._assert_available(path, allow_own=False)
            history = self._history_for_path(path)
            if history is None:
                raise RootFileError("file.changed", "File changed since it was read")
            directory, index = history
            target = self._ensure_no_symlinks(self._files_path(), self._parts(path))
            current_data = self._read_private_file(target)
            discarded = self._assert_revision_preconditions(
                index,
                current_data,
                expected_file_id,
                expected_current_revision,
                expected_current_sha256,
            )
            selected = next(
                (item for item in index["revisions"] if item["revision"] == revision), None
            )
            if selected is None:
                raise RootFileError("file.revisionNotFound", "Revision not found")
            selected_data = self._read_private_file(directory / str(revision))
            if hashlib.sha256(selected_data).hexdigest() != selected["sha256"]:
                raise RootFileError("file.revisionInvalid", "Invalid revision history")
            if revision == index["currentRevision"]:
                return

            revisions_root = self._revisions_path()
            trash = revisions_root / f".discard-{uuid4().hex}"
            staged_revisions = trash / "revisions"
            staged_derivatives = trash / "derived"
            staged_revisions.mkdir(mode=0o700, parents=True)
            staged_derivatives.mkdir(mode=0o700)
            old_index = self._read_private_file(directory / "index.json")
            index = {key: value for key, value in index.items() if key != "run"}
            temporary = target.parent / f".{target.name}.{uuid4().hex}.tmp"
            index_temporary = directory / f".index.{uuid4().hex}.tmp"
            moved: list[tuple[Path, Path]] = []
            index_replaced = False
            target_replaced = False
            try:
                self._write_private_file(temporary, selected_data)
                self._write_private_file(
                    index_temporary,
                    self._index_bytes(
                        {
                            **index,
                            "currentRevision": revision,
                            "revisions": [
                                item
                                for item in index["revisions"]
                                if item["revision"] != discarded["revision"]
                            ],
                        }
                    ),
                )
                derivatives = directory / "derived"
                if derivatives.exists() or derivatives.is_symlink():
                    self._ensure_directory(derivatives)
                for item in (discarded,):
                    source = directory / str(item["revision"])
                    metadata = self._metadata(source)
                    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                        raise RootFileError("file.revisionInvalid", "Invalid revision history")
                    staged = staged_revisions / source.name
                    source.rename(staged)
                    moved.append((source, staged))
                    derivative = directory / "derived" / source.name
                    if derivative.exists() or derivative.is_symlink():
                        metadata = self._metadata(derivative)
                        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                            raise RootFileError(
                                "file.revisionInvalid", "Invalid revision derivative"
                            )
                        staged = staged_derivatives / source.name
                        derivative.rename(staged)
                        moved.append((derivative, staged))
                os.replace(index_temporary, directory / "index.json")
                index_replaced = True
                os.replace(temporary, target)
                target_replaced = True
                self._fsync_directory(target.parent)
                self._fsync_directory(directory)
                self._fsync_directory(staged_revisions)
                self._fsync_directory(staged_derivatives)
            except BaseException:
                if target_replaced:
                    self._replace_private_file(target, current_data)
                if index_replaced:
                    self._replace_private_file(directory / "index.json", old_index)
                for source, staged in reversed(moved):
                    staged.rename(source)
                self._fsync_directory(target.parent)
                self._fsync_directory(directory)
                temporary.unlink(missing_ok=True)
                index_temporary.unlink(missing_ok=True)
                shutil.rmtree(trash, ignore_errors=True)
                raise
            # Only committed trash is eligible for cleanup on later file operations.
            try:
                disposable = revisions_root / f".delete-owned-{uuid4().hex}"
                trash.rename(disposable)
                self._fsync_directory(revisions_root)
                shutil.rmtree(disposable)
            except OSError:
                logger.warning("Discarded output revision; trash cleanup deferred", exc_info=True)

    def replace_text_file(
        self,
        root: FileRoot | str,
        relative_path: str,
        content: str,
        expected_revision: str,
    ) -> str:
        selected_root = self._selected_root(root)
        parts = self._parts(relative_path)
        self._assert_mutable(selected_root, parts)
        if not parts:
            raise RootFileError("file.notAFile", "Target is not a regular file")
        encoded = content.encode("utf-8")
        if len(encoded) > self.max_editable_text_bytes:
            raise RootFileError("file.tooLarge", "File is too large")
        if "\0" in content:
            raise RootFileError("file.invalidEncoding", "File is not editable UTF-8 text")
        if self.run_id is not None:
            return self._write_run_file(
                self._relative_path(parts), encoded, expected_sha256=expected_revision,
            ).sha256
        with self._session_operation_lock(selected_root, quota_check=True):
            self._assert_available(self._relative_path(parts))
            target = self._ensure_no_symlinks(self._files_path(selected_root), parts)
            metadata = self._metadata(target)
            if not stat.S_ISREG(metadata.st_mode):
                raise RootFileError("file.notAFile", "Target is not a regular file")
            current = self._read_private_file(target)
            if hashlib.sha256(current).hexdigest() != expected_revision:
                raise RootFileError("file.changed", "File changed since it was read")
            path = self._relative_path(parts)
            history = self._history_for_path(path)
            new_digest = hashlib.sha256(encoded).hexdigest()
            if current == encoded:
                return new_digest
            if history is None:
                file_id = uuid4()
                history_directory = self._revisions_path() / str(file_id)
                staged_history = history_directory.parent / f".{file_id}.{uuid4().hex}.tmp"
                staged_history.mkdir(mode=0o700)
                items = [self._revision_item(1, current), self._revision_item(2, encoded)]
                next_index = {
                    "fileId": str(file_id),
                    "path": path,
                    "currentRevision": 2,
                    "maxRevision": 2,
                    "revisions": items,
                }
                self._write_private_file(staged_history / "1", current)
                self._write_private_file(staged_history / "2", encoded)
                self._write_private_file(
                    staged_history / "index.json", self._index_bytes(next_index)
                )
                old_index = None
                revision_target = None
            else:
                history_directory, index = history
                current_item = next(
                    (item for item in index["revisions"] if item["revision"] == index["currentRevision"]), None,
                )
                if current_item is not None and current_item["sha256"] != expected_revision:
                    raise RootFileError("file.revisionInvalid", "Current revision does not match file")
                revision = index["maxRevision"] + 1
                next_index = {
                    **index,
                    "currentRevision": revision,
                    "maxRevision": revision,
                    "revisions": [*index["revisions"], self._revision_item(revision, encoded)],
                }
                old_index = self._read_private_file(history_directory / "index.json")
                revision_target = history_directory / str(revision)
                self._write_private_file(revision_target, encoded)
                staged_history = None
            temporary = target.parent / f".{target.name}.{uuid4().hex}.tmp"
            index_temporary = history_directory / f".index.{uuid4().hex}.tmp"
            try:
                self._write_private_file(temporary, encoded)
                if staged_history is None:
                    self._write_private_file(index_temporary, self._index_bytes(next_index))
                self._assert_user_quota(
                    0,
                    replaced_bytes=len(current) + (len(old_index) if old_index else 0),
                )
                if staged_history is not None:
                    staged_history.rename(history_directory)
                else:
                    os.replace(index_temporary, history_directory / "index.json")
                try:
                    os.replace(temporary, target)
                except BaseException:
                    if staged_history is not None:
                        shutil.rmtree(history_directory, ignore_errors=True)
                    else:
                        assert old_index is not None
                        self._replace_private_file(
                            history_directory / "index.json", old_index
                        )
                        assert revision_target is not None
                        revision_target.unlink(missing_ok=True)
                    raise
                self._fsync_directory(target.parent)
                self._fsync_directory(history_directory)
            except BaseException:
                temporary.unlink(missing_ok=True)
                index_temporary.unlink(missing_ok=True)
                if staged_history is not None:
                    shutil.rmtree(staged_history, ignore_errors=True)
                elif revision_target is not None and old_index is not None:
                    if self._read_private_file(history_directory / "index.json") == old_index:
                        revision_target.unlink(missing_ok=True)
                raise
            return new_digest

    def create_file(
        self,
        root: FileRoot | str,
        relative_path: str,
        source: BinaryIO,
        *,
        max_bytes: int,
    ) -> RootFileEntry:
        selected_root = self._selected_root(root)
        parts = self._parts(relative_path)
        validate_upload_filename(parts[-1] if parts else None)
        self._assert_mutable(selected_root, parts)
        if not parts:
            raise RootFileError("file.rootMutationForbidden", "A file root is immutable")
        if self.run_id is not None:
            source.seek(0)
            return self._create_run_file_stream(self._relative_path(parts), source, max_bytes=max_bytes)
        with self._session_operation_lock(selected_root, quota_check=True):
            self._assert_available(self._relative_path(parts))
            try:
                anchor, anchored_parts = self._anchored_parts(
                    selected_root, parts[:-1]
                )
                with _directory_descriptor(
                    anchor, anchored_parts, RootFileError
                ) as descriptor:
                    temporary, size = _stage_upload(
                        descriptor,
                        parts[-1],
                        source,
                        max_bytes=max_bytes,
                    )
                    try:
                        descriptor_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                        content_descriptor = os.open(
                            temporary, descriptor_flags, dir_fd=descriptor
                        )
                        with os.fdopen(content_descriptor, "rb") as content_handle:
                            staged_history, history, _index = self._stage_initial_history(
                                self._relative_path(parts), content_handle
                            )
                        self._assert_user_quota(0)
                        staged_history.rename(history)
                        metadata = _publish_upload(
                            descriptor, temporary, parts[-1]
                        )
                    except BaseException:
                        if "history" in locals() and history.exists():
                            shutil.rmtree(history, ignore_errors=True)
                        if "staged_history" in locals():
                            shutil.rmtree(staged_history, ignore_errors=True)
                        try:
                            os.unlink(temporary, dir_fd=descriptor)
                        except FileNotFoundError:
                            pass
                        raise
            except _UploadTooLarge as exc:
                raise RootFileError("file.tooLarge", "File is too large") from exc
            except FileExistsError as exc:
                raise RootFileError(
                    "file.pathAlreadyExists", "Destination path already exists"
                ) from exc
        return RootFileEntry(
            name=parts[-1],
            path=self._relative_path(parts),
            kind="file",
            size=size,
            modified_at=metadata.st_mtime,
        )

    def open_file(self, root: FileRoot | str, relative_path: str) -> BinaryIO:
        parts = self._parts(relative_path)
        anchor, anchored_parts = self._anchored_parts(root, parts)
        return _open_regular_file(anchor, anchored_parts, RootFileError)

    def read_bytes(self, root: FileRoot | str, relative_path: str, *, limit: int) -> bytes:
        with self.open_file(root, relative_path) as handle:
            return handle.read(limit + 1)

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _write_private_file(path: Path, content: bytes) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())

    def create_directory(self, root: FileRoot | str, relative_path: str) -> str:
        selected_root = self._selected_root(root)
        parts = self._parts(relative_path)
        self._assert_mutable(selected_root, parts)
        if not parts:
            raise RootFileError("file.rootMutationForbidden", "A file root is immutable")
        with self._session_operation_lock(selected_root, quota_check=True):
            self._resolve_destination(selected_root, relative_path)
            anchor, anchored_parts = self._root_path(selected_root) / "files", parts[:-1]
            created = False
            try:
                with _directory_descriptor(
                    anchor, anchored_parts, RootFileError
                ) as descriptor:
                    os.mkdir(parts[-1], mode=0o700, dir_fd=descriptor)
                    created = True
                    try:
                        self._assert_user_quota(0)
                        os.fsync(descriptor)
                    except BaseException:
                        if created:
                            os.rmdir(parts[-1], dir_fd=descriptor)
                        raise
            except FileExistsError as exc:
                raise RootFileError(
                    "file.pathAlreadyExists", "Destination path already exists"
                ) from exc
        return self._relative_path(parts)

    def _histories_under(self, path: str) -> list[tuple[Path, dict]]:
        prefix = path.rstrip("/") + "/"
        return [
            item
            for item in self._revision_indexes()
            if not item[1].get("deleted", False)
            and (item[1]["path"] == path or item[1]["path"].startswith(prefix))
        ]

    def _rewrite_history_paths(self, source: str, destination: str) -> None:
        histories = self._histories_under(source)
        self._assert_user_quota(sum(
            len(self._index_bytes({**index, "path": destination + index["path"][len(source):]}))
            - len(self._index_bytes(index)) for _, index in histories
        ))
        written: list[tuple[Path, bytes]] = []
        try:
            for directory, index in histories:
                old = self._read_private_file(directory / "index.json")
                next_path = destination + index["path"][len(source) :]
                temporary = directory / f".index.{uuid4().hex}.tmp"
                self._write_private_file(
                    temporary, self._index_bytes({**index, "path": next_path})
                )
                os.replace(temporary, directory / "index.json")
                written.append((directory, old))
                self._fsync_directory(directory)
        except BaseException:
            for directory, old in written:
                self._replace_private_file(directory / "index.json", old)
            raise

    def rename_path(
        self, root: FileRoot | str, relative_path: str, filename: str
    ) -> RootFileEntry:
        selected_root = self._selected_root(root)
        parts = self._parts(relative_path)
        self._assert_mutable(selected_root, parts)
        name = validate_upload_filename(filename)
        destination_parts = (*parts[:-1], name)
        self._assert_mutable(selected_root, destination_parts)
        with self._session_operation_lock(selected_root, quota_check=True):
            source_path = self._relative_path(parts)
            destination_path = self._relative_path(destination_parts)
            self._assert_available(source_path)
            self._assert_available(destination_path)
            if source_path != destination_path:
                self.move_path(selected_root, source_path, selected_root, destination_path)
            metadata = self._metadata(self.resolve(selected_root, destination_path))
        is_directory = stat.S_ISDIR(metadata.st_mode)
        return RootFileEntry(
            name=name,
            path=self._relative_path(destination_parts),
            kind="directory" if is_directory else "file",
            size=None if is_directory else metadata.st_size,
            modified_at=metadata.st_mtime,
        )

    def delete_path(
        self,
        root: FileRoot | str,
        relative_path: str,
        *,
        recursive: bool,
    ) -> None:
        selected_root = self._selected_root(root)
        parts = self._parts(relative_path)
        self._assert_mutable(selected_root, parts)
        with self._session_operation_lock(selected_root):
            self._assert_available(self._relative_path(parts))
            root_path = self._files_path(selected_root)
            target = self._ensure_no_symlinks(
                root_path, parts, allow_final_symlink=True, allow_missing_final=True
            )
            path = self._relative_path(parts)
            histories = self._histories_under(path)
            source_ids = {index["fileId"] for _, index in histories}
            if not target.exists() and not target.is_symlink():
                if source_ids:
                    self._purge_owned_files(source_ids)
                    return
                raise RootFileError("file.pathNotFound", "Path not found")
            metadata = self._metadata(target)
            if stat.S_ISDIR(metadata.st_mode) and not recursive and any(target.iterdir()):
                raise RootFileError("file.directoryNotEmpty", "Directory is not empty")
            self._purge_owned_files({child for child, link in self._derived_links().items() if link["sourceFileId"] in source_ids})
            histories = self._histories_under(path)
            revisions_root = self._revisions_path()
            trash = revisions_root / f".delete-{uuid4().hex}"
            staged_histories = trash / "histories"
            staged_histories.mkdir(mode=0o700, parents=True)
            staged_target = trash / "content"
            moved_histories: list[tuple[Path, Path]] = []
            try:
                target.rename(staged_target)
                for directory, _index in histories:
                    staged = staged_histories / directory.name
                    directory.rename(staged)
                    moved_histories.append((directory, staged))
            except BaseException:
                for directory, staged in reversed(moved_histories):
                    staged.rename(directory)
                if staged_target.exists() or staged_target.is_symlink():
                    staged_target.rename(target)
                shutil.rmtree(trash, ignore_errors=True)
                raise
            shutil.rmtree(trash)
            for file_id in source_ids:
                (self._revisions_path() / ".ownership" / f"{file_id}.json").unlink(missing_ok=True)
            ownership = self._revisions_path() / ".ownership"
            if ownership.exists():
                self._fsync_directory(ownership)
            self._fsync_directory(target.parent)
            self._fsync_directory(revisions_root)

    @staticmethod
    def _copy_regular_file(source: Path, destination: Path) -> None:
        source_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        destination_flags = (
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        )
        source_descriptor = os.open(source, source_flags)
        try:
            destination_descriptor = os.open(destination, destination_flags, 0o600)
            try:
                with os.fdopen(source_descriptor, "rb", closefd=False) as source_handle:
                    with os.fdopen(destination_descriptor, "wb") as destination_handle:
                        shutil.copyfileobj(source_handle, destination_handle)
                        destination_handle.flush()
                        os.fsync(destination_handle.fileno())
            except BaseException:
                destination.unlink(missing_ok=True)
                raise
        finally:
            os.close(source_descriptor)

    @classmethod
    def _copy_tree(cls, source: Path, destination: Path) -> None:
        metadata = cls._metadata(source)
        if stat.S_ISLNK(metadata.st_mode):
            raise RootFileError("file.symlinkForbidden", "Symlinks are not followed")
        if stat.S_ISREG(metadata.st_mode):
            cls._copy_regular_file(source, destination)
            return
        if not stat.S_ISDIR(metadata.st_mode):
            raise RootFileError("file.unsupportedType", "Unsupported file type")
        destination.mkdir(mode=0o700)
        try:
            for item in source.iterdir():
                cls._copy_tree(item, destination / item.name)
        except BaseException:
            shutil.rmtree(destination)
            raise

    def _copy_to_destination(
        self,
        source: Path,
        destination: Path,
        destination_root: FileRoot,
        *,
        released_session_bytes: int = 0,
    ) -> None:
        temporary = destination.parent / f".{destination.name}.{uuid4().hex}.tmp"
        try:
            self._copy_tree(source, temporary)
            self._operation_usage(temporary)
            if destination_root is FileRoot.SESSION:
                # The temporary copy is already part of session usage at this point.
                self._assert_user_quota(0, replaced_bytes=released_session_bytes)
            if destination.exists() or destination.is_symlink():
                raise RootFileError("file.pathAlreadyExists", "Destination path already exists")
            temporary.rename(destination)
            self._fsync_directory(destination.parent)
        finally:
            if temporary.exists() or temporary.is_symlink():
                if temporary.is_dir() and not temporary.is_symlink():
                    shutil.rmtree(temporary)
                else:
                    temporary.unlink(missing_ok=True)

    def copy_path(
        self,
        source_root: FileRoot | str,
        source_path: str,
        destination_root: FileRoot | str,
        destination_path: str,
    ) -> str:
        selected_source_root = self._selected_root(source_root)
        selected_destination_root = self._selected_root(destination_root)
        source_parts = self._parts(source_path)
        destination_parts = self._parts(destination_path)
        with self._session_operation_lock(selected_source_root, selected_destination_root, quota_check=True):
            source = self._ensure_no_symlinks(
                self._files_path(selected_source_root), source_parts
            )
            source_usage = self._operation_usage(source)
            destination = self._resolve_destination(
                selected_destination_root, destination_path
            )
            try:
                destination.relative_to(source)
            except ValueError:
                pass
            else:
                raise RootFileError(
                    "file.invalidDestination", "Destination is inside the source"
                )
            if selected_destination_root is FileRoot.SESSION:
                self._assert_user_quota(source_usage.bytes)
            self._copy_to_destination(source, destination, selected_destination_root)
            created_histories: list[Path] = []
            try:
                copied_files = (
                    [destination]
                    if destination.is_file()
                    else sorted(item for item in destination.rglob("*") if item.is_file())
                )
                root_path = self._files_path(selected_destination_root)
                for copied_file in copied_files:
                    copied_path = copied_file.relative_to(root_path).as_posix()
                    history, _index = self._publish_initial_history(
                        copied_path, copied_file.read_bytes()
                    )
                    created_histories.append(history)
            except BaseException:
                for history in created_histories:
                    shutil.rmtree(history, ignore_errors=True)
                if destination.is_dir():
                    shutil.rmtree(destination, ignore_errors=True)
                else:
                    destination.unlink(missing_ok=True)
                raise
        return self._relative_path(destination_parts)

    def copy_file_from(self, source: RootFileService, file_id: UUID) -> dict[int, FileRevision]:
        """Import one file with its revision identity and current state intact."""
        with self._session_operation_lock(FileRoot.SESSION, quota_check=True):
            history = source._history_for_id(file_id)
            if history is None:
                raise RootFileError("file.revisionNotFound", "Revision history not found")
            directory, index = history
            if index.get("run", {}).get("state", "final") != "final":
                raise RootFileError("file.changed", "File is owned by an active run")
            copied_id = uuid4()
            parent = f"import-{copied_id}"
            path = f"{parent}/{PurePosixPath(index['path']).name}"
            working = None if index.get("deleted", False) else source.resolve(FileRoot.SESSION, index["path"])
            self._assert_user_quota(self._tree_usage(directory, operation_limits=False).bytes
                                    + (self._tree_usage(working, operation_limits=False).bytes if working else 0))
            copied_directory = self._revisions_path() / str(copied_id)
            self._copy_tree(directory, copied_directory)
            copied_index = {**index, "fileId": str(copied_id), "path": path}
            copied_index.pop("run", None)
            copied_index.pop("runReceipts", None)
            self._replace_private_file(copied_directory / "index.json", self._index_bytes(copied_index))
            if working is not None:
                self.create_directory(FileRoot.SESSION, parent)
                self._copy_tree(working, self._files_path() / path)
                self._fsync_directory(self._files_path() / parent)
            self._fsync_directory(copied_directory)
            self._fsync_directory(copied_directory.parent)
            self._assert_user_quota(0)
            return {
                item["revision"]: FileRevision(
                    file_id=copied_id, revision=item["revision"], path=path,
                    size_bytes=item["sizeBytes"], sha256=item["sha256"], created_at=item["createdAt"],
                    current=item["revision"] == index["currentRevision"],
                ) for item in index["revisions"]
            }

    def copy_session_from(self, source: RootFileService) -> None:
        """Copy an idle session's files and revisions into an empty, independently owned store."""
        if self.session_id == source.session_id or self.user_id != source.user_id:
            raise RootFileError("file.invalidDestination", "Invalid session copy destination")
        with self._session_operation_lock(FileRoot.SESSION, quota_check=True):
            target = self._root_path(FileRoot.SESSION)
            if any(any((target / name).iterdir()) for name in ("files", "revisions")):
                raise RootFileError("file.pathAlreadyExists", "Session copy destination is not empty")
            origin = source.session_storage.session_path(source.session_id, project_id=source.project_id)
            if not origin.exists() and not origin.is_symlink():
                return
            source._root_path(FileRoot.SESSION)
            if source.session_storage.pending_run_ids(source.session_id, source.project_id):
                raise RootFileError("file.changed", "Session has pending run files")
            self._assert_user_quota(source._session_usage())
            for name in ("files", "revisions"):
                for entry in (origin / name).iterdir():
                    self._copy_tree(entry, target / name / entry.name)
            for directory, index in self._revision_indexes():
                index.pop("run", None)
                index.pop("runReceipts", None)
                self._replace_private_file(directory / "index.json", self._index_bytes(index))
            self._assert_user_quota(0)

    def move_path(
        self,
        source_root: FileRoot | str,
        source_path: str,
        destination_root: FileRoot | str,
        destination_path: str,
    ) -> str:
        selected_source_root = self._selected_root(source_root)
        selected_destination_root = self._selected_root(destination_root)
        source_parts = self._parts(source_path)
        self._assert_mutable(selected_source_root, source_parts)
        destination_parts = self._parts(destination_path)
        self._assert_mutable(selected_destination_root, destination_parts)
        source_path = self._relative_path(source_parts)
        destination_path = self._relative_path(destination_parts)
        if destination_parts[:len(source_parts)] == source_parts:
            raise RootFileError("file.invalidDestination", "Destination is inside the source")
        with self._session_operation_lock(selected_source_root, selected_destination_root, quota_check=True):
            self._assert_available(source_path)
            self._operation_usage(self.resolve(selected_source_root, source_path))
            destination = self._resolve_destination(
                selected_destination_root, destination_path
            )
            source = self.resolve(selected_source_root, source_path)
            with _directory_descriptor(self._files_path(), source_parts[:-1], RootFileError) as source_fd:
                with _directory_descriptor(self._files_path(), destination_parts[:-1], RootFileError) as destination_fd:
                    _rename_at(source_fd, source.name, destination.name, RootFileError,
                               destination_descriptor=destination_fd)
            try:
                self._rewrite_history_paths(source_path, destination_path)
            except BaseException:
                destination.rename(source)
                raise
            self._fsync_directory(source.parent)
            self._fsync_directory(destination.parent)
        return destination_path
