from __future__ import annotations

import asyncio
import hashlib
import logging
import posixpath
from uuid import UUID

from anyio import CancelScope
from pydantic import ValidationError
from sqlalchemy import and_, select

import mato_loop_backend.files.access as files_access
import mato_loop_backend.tools.access as tools_access
import mato_loop_backend.tools.administrator as administrator
from mato_loop_backend.execution import (
    ExecutionClientError,
    WorkspaceDeleteResult,
    WorkspaceGlobResult,
    WorkspaceGrepResult,
    WorkspaceListResult,
    WorkspacePatchResult,
    WorkspacePathResult,
    WorkspaceReadResult,
    WorkspaceStatResult,
    WorkspaceWriteResult,
)
from mato_loop_backend.files.archives import MAX_EXPORT_BYTES
from mato_loop_backend.files.search import RootFileSearch
from mato_loop_backend.files.service import (
    FileRoot,
    RootFileError,
)
from mato_loop_backend.media.attachments import (
    MAX_IMAGE_ATTACHMENT_BYTES,
    AttachmentService,
    image_mime_type,
    processing_limits,
    validate_image,
)
from mato_loop_backend.media.pdf import initial_pdf_pages, render_pdf_page
from mato_loop_backend.media.processing import AttachmentProcessingError, extract_pdf_pages
from mato_loop_backend.models import Message, MessageAttachment
from mato_loop_backend.permissions.policy import administrator_permission
from mato_loop_backend.providers.base import MAX_PROVIDER_IMAGE_BYTES, ProviderError
from mato_loop_backend.providers.service import normalize_provider_image
from mato_loop_backend.settings import InstanceLimits, SettingsStore, ToolExecutionPolicy
from mato_loop_backend.tools.actions import (
    _FILE_PROVIDE,
    _FILESYSTEM_DELETE,
    _FILESYSTEM_READ,
    _FILESYSTEM_WRITE,
    _SESSION_FILES_DELETE,
    _SESSION_FILES_READ,
    _SESSION_FILES_WRITE,
)
from mato_loop_backend.tools.arguments import (
    ApplyPatchArguments,
    CopyPathArguments,
    CreateDirectoryArguments,
    DeletePathArguments,
    EditFileArguments,
    GlobArguments,
    GrepArguments,
    MovePathArguments,
    ProvideFileArguments,
    ReadArguments,
    WriteFileArguments,
)
from mato_loop_backend.tools.context import ToolContext as ToolContext
from mato_loop_backend.tools.contracts import ToolAttachment as ToolAttachment
from mato_loop_backend.tools.contracts import ToolDefinition as ToolDefinition
from mato_loop_backend.tools.contracts import ToolFileAction as ToolFileAction
from mato_loop_backend.tools.contracts import ToolResult as ToolResult
from mato_loop_backend.tools.secrets import resolve_protected_text
from mato_loop_backend.tools.validation import argument_error
from mato_loop_backend.web.fetch import WebFetchError

logger = logging.getLogger("mato_loop_backend.agent.tools")


ALL_TOOL_EXECUTION_POLICIES = tuple(ToolExecutionPolicy)


async def _read(arguments: ReadArguments, context: ToolContext) -> ToolResult:
    if files_access.delegated_session_files(context, arguments.root):
        return ToolResult.error("tool.unavailable")
    if arguments.elevated:
        client = files_access.workspace_client(context)
        if not client.supports("administrator.run"):
            return ToolResult.error("executionAgent.operationUnavailable")
        path = files_access.execution_path(context, arguments.path)
        permission = administrator_permission(client.agent_id, client.current_directory, capability="filesystem.read", path=path)
        if not await administrator.authorize(context, permission, arguments.sudo_password_ref):
            return ToolResult.error("permission.denied")
        result = await administrator.invoke(context, "filesystem.read", {
            "path": path, "offset": arguments.offset, "limit": arguments.limit, "maxLineLength": 2000,
        }, WorkspaceReadResult, arguments.sudo_password_ref)
        if isinstance(result, ToolResult):
            return result
        return _read_text_result(arguments, context.session.id, result)
    selected = await files_access.project_read_files(arguments, context)
    if selected is None:
        return ToolResult.error("permission.denied")
    root_files, target_session_id = selected
    request = files_access.filesystem_permission(
        context,
        root=arguments.root,
        path=arguments.path,
        capability="filesystem.read",
        action_kind="read",
        action_label="Pfad lesen",
        root_files=root_files,
        target_session_id=target_session_id,
    )
    if not await tools_access.allow(context, request):
        return ToolResult.error("permission.denied")
    if target_session_id != context.session.id and await files_access.project_read_files(arguments, context) is None:
        return ToolResult.error("permission.denied")
    normalized_path = files_access.file_path(
        context, arguments.root, arguments.path, root_files=root_files,
    )
    if arguments.root == "workspace":
        stat_result = await files_access.workspace_request(
            context,
            "filesystem.stat",
            {"path": normalized_path},
            WorkspaceStatResult,
        )
        files_access.require_workspace_path(stat_result.path, normalized_path)
        kind = stat_result.kind
    else:
        kind = root_files.path_kind(arguments.root, normalized_path)
    if kind == "directory":
        if arguments.root == "workspace":
            result = await files_access.workspace_request(
                context,
                "filesystem.list",
                {
                    "path": normalized_path,
                    "offset": arguments.offset - 1,
                    "limit": arguments.limit,
                },
                WorkspaceListResult,
            )
        else:
            result = root_files.list_directory(
                arguments.root,
                arguments.path,
                offset=arguments.offset - 1,
                limit=arguments.limit,
            )
        return ToolResult.json(
            {
                "root": arguments.root,
                "sessionId": str(target_session_id),
                "path": arguments.path,
                "entries": [
                    {
                        "name": entry.name,
                        "path": entry.path,
                        "kind": entry.kind,
                        "size": entry.size,
                        "modifiedAt": entry.modified_at,
                    }
                    for entry in result.entries
                ],
                "hasMore": result.has_more,
                "nextOffset": (
                    result.next_offset + 1 if result.next_offset is not None else None
                ),
                "limit": arguments.limit,
            }
        )
    if kind == "symlink":
        return ToolResult.error("file.symlinkForbidden")
    if kind != "file":
        return ToolResult.error("file.unsupportedType")
    if normalized_path.lower().endswith(".pdf"):
        return await _read_pdf(arguments, context, root_files, normalized_path)
    if arguments.root == "session" and target_session_id == context.session.id:
        try:
            selected_revision = await asyncio.to_thread(root_files.working_revision, normalized_path)
        except RootFileError as exc:
            if exc.code != "file.revisionNotFound":
                raise
            selected_revision = None
        revision_filter = and_(
            selected_revision is not None,
            MessageAttachment.file_id
            == (selected_revision.file_id if selected_revision else None),
            MessageAttachment.file_revision
            == (selected_revision.revision if selected_revision else None),
        )
        filters = [
            revision_filter,
            MessageAttachment.kind == "image",
            MessageAttachment.processing_status == "ready",
            Message.session_id == context.session.id,
            Message.error_code.is_(None),
        ]
        attachment = await context.db.scalar(
            select(MessageAttachment)
            .join(Message, Message.id == MessageAttachment.message_id)
            .where(*filters)
        )
        if selected_revision is not None and selected_revision.revision is None:
            attachment = next((item for item in await AttachmentService(run_id=root_files.run_id).pending_for_run(context.db, context.session.id)
                               if item.file_id == selected_revision.file_id and item.sha256 == selected_revision.sha256
                               and item.kind == "image"), None)
        if attachment is not None:
            return ToolResult.json(
                {
                    "root": "session",
                    "path": normalized_path,
                    "imageAttachment": {
                        "id": str(attachment.id),
                        "filename": attachment.filename,
                        "kind": "image",
                        "mimeType": attachment.mime_type,
                    },
                    "contentIsUntrusted": True,
                }
            )
    if normalized_path.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
        data = await files_access.read_bytes(
            context, arguments.root, normalized_path,
            limit=MAX_IMAGE_ATTACHMENT_BYTES, root_files=root_files,
        )
        validated = validate_image(data, image_mime_type(data) or "")
        image, mime_type = normalize_provider_image(data, validated)
        result = ToolResult.json({
            "root": arguments.root,
            "path": normalized_path,
            "image": {"filename": posixpath.basename(normalized_path), "mimeType": mime_type},
            "contentIsUntrusted": True,
        })
        result.attachments = (ToolAttachment(
            image, mime_type, posixpath.basename(normalized_path),
            presentation="internal", ephemeral=True,
        ),)
        return result
    if arguments.root == "workspace":
        result = await files_access.workspace_request(
            context,
            "filesystem.read",
            {
                "path": normalized_path,
                "offset": arguments.offset,
                "limit": arguments.limit,
                "maxLineLength": 2_000,
            },
            WorkspaceReadResult,
        )
    else:
        result = root_files.read_file(
            arguments.root,
            arguments.path,
            offset=arguments.offset,
            limit=arguments.limit,
        )
    tool_result = _read_text_result(arguments, target_session_id, result)
    if arguments.root == "workspace":
        client = files_access.workspace_client(context)
        tool_result.metadata["computerRead"] = {
            "executionAgentId": str(client.agent_id), "currentDirectory": client.current_directory,
            "path": normalized_path,
        }
    return tool_result


async def _read_pdf(arguments: ReadArguments, context: ToolContext, root_files, path: str) -> ToolResult:
    limits = await SettingsStore.load(context.db, InstanceLimits)
    data = await files_access.read_bytes(
        context, arguments.root, path, limit=limits.max_upload_bytes, root_files=root_files,
    )
    try:
        pages, metadata = await asyncio.to_thread(extract_pdf_pages, data, limits=processing_limits(limits))
        requested = arguments.pdf_pages or initial_pdf_pages(len(pages))
        if any(number > len(pages) for number in requested):
            return ToolResult.error("attachment.pdfPageInvalid")
        images = []
        image_bytes = 0
        failed_pages = []
        if context.pdf_images_enabled or context.pdf_vision_handler is not None:
            for number in requested:
                try:
                    image = await asyncio.to_thread(render_pdf_page, data, number)
                except AttachmentProcessingError:
                    failed_pages.append(number)
                    continue
                if image_bytes + len(image) > MAX_PROVIDER_IMAGE_BYTES:
                    break
                images.append((number, image))
                image_bytes += len(image)
        descriptions = {}
        visual_status = "failed" if failed_pages else "available" if images else "unavailable"
        if images and not context.pdf_images_enabled:
            try:
                descriptions = await context.pdf_vision_handler(images, context.model_trust_level.value == "private")
            except (WebFetchError, ProviderError):
                visual_status = "failed"
        lines = "\n".join(f"--- Page {number} ---\n{text}" for number, text in enumerate(pages, 1))
        selected_lines = lines.splitlines()[arguments.offset - 1:arguments.offset - 1 + arguments.limit]
        result = ToolResult.json({
            "root": arguments.root, "path": path, "pageCount": len(pages),
            "imagePages": metadata["imagePages"], "content": "\n".join(selected_lines)[:24_000],
            "visualPages": [number for number, _image in images],
            "visualDescriptions": descriptions, "requestedPages": requested,
            "failedPages": failed_pages,
            "visualStatus": visual_status,
            "note": "Visual coverage requires supplied images or successful visualDescriptions. Use pdf_pages for other pages. Text uses line offset/limit and may be truncated.",
            "contentIsUntrusted": True,
        })
        if context.pdf_images_enabled:
            result.attachments = tuple(ToolAttachment(
                image, "image/jpeg", f"{posixpath.basename(path)}-page-{number}.jpg", presentation="internal", ephemeral=True,
                metadata={"pageNumber": number},
            ) for number, image in images)
        return result
    except AttachmentProcessingError as exc:
        return ToolResult.error(exc.code)


def _read_text_result(arguments: ReadArguments, target_session_id: UUID, result) -> ToolResult:
    content = result.content
    notices = []
    if result.truncated_lines:
        lines = ", ".join(str(line) for line in result.truncated_lines)
        notices.append(f"[Lines {lines} were truncated after 2000 characters.]")
    if result.has_more:
        notices.append(
            f"[More lines are available. Continue with offset={result.next_offset}.]"
        )
    if notices:
        content += ("\n\n" if content else "") + "\n".join(notices)
    return ToolResult(
        content,
        {
            "root": arguments.root,
            "sessionId": str(target_session_id),
            "path": arguments.path,
            "offset": arguments.offset,
            "limit": arguments.limit,
            "linesRead": result.lines_read,
            "hasMore": result.has_more,
            "nextOffset": result.next_offset,
            "truncatedLines": list(result.truncated_lines),
            "sha256": result.sha256,
        },
    )


async def _glob(arguments: GlobArguments, context: ToolContext) -> ToolResult:
    if files_access.delegated_session_files(context, arguments.root):
        return ToolResult.error("tool.unavailable")
    selected = await files_access.project_read_files(arguments, context)
    if selected is None:
        return ToolResult.error("permission.denied")
    root_files, target_session_id = selected
    request = files_access.filesystem_permission(
        context,
        root=arguments.root,
        path=arguments.path,
        capability="filesystem.read",
        action_kind="glob",
        action_label="Dateien suchen",
        root_files=root_files,
        target_session_id=target_session_id,
    )
    if not await tools_access.allow(context, request):
        return ToolResult.error("permission.denied")
    if target_session_id != context.session.id and await files_access.project_read_files(arguments, context) is None:
        return ToolResult.error("permission.denied")
    if arguments.root == "workspace":
        result = await files_access.workspace_request(
            context,
            "filesystem.glob",
            {
                "path": files_access.execution_path(context, arguments.path),
                "pattern": arguments.pattern,
            },
            WorkspaceGlobResult,
        )
        payload = result.model_dump(mode="json", by_alias=True)
    else:
        payload = await RootFileSearch(root_files).glob(
            arguments.root, arguments.path, arguments.pattern
        )
    return ToolResult.json(
        {
            "root": arguments.root,
            "sessionId": str(target_session_id),
            "path": arguments.path,
            **payload,
        }
    )


async def _grep(arguments: GrepArguments, context: ToolContext) -> ToolResult:
    if files_access.delegated_session_files(context, arguments.root):
        return ToolResult.error("tool.unavailable")
    selected = await files_access.project_read_files(arguments, context)
    if selected is None:
        return ToolResult.error("permission.denied")
    root_files, target_session_id = selected
    request = files_access.filesystem_permission(
        context,
        root=arguments.root,
        path=arguments.path,
        capability="filesystem.read",
        action_kind="grep",
        action_label="Dateiinhalte durchsuchen",
        root_files=root_files,
        target_session_id=target_session_id,
    )
    if not await tools_access.allow(context, request):
        return ToolResult.error("permission.denied")
    if target_session_id != context.session.id and await files_access.project_read_files(arguments, context) is None:
        return ToolResult.error("permission.denied")
    if arguments.root == "workspace":
        result = await files_access.workspace_request(
            context,
            "filesystem.grep",
            {
                "path": files_access.execution_path(context, arguments.path),
                "pattern": arguments.pattern,
                **({"include": arguments.include} if arguments.include is not None else {}),
            },
            WorkspaceGrepResult,
        )
        payload = result.model_dump(mode="json", by_alias=True)
    else:
        payload = await RootFileSearch(root_files).grep(
            arguments.root, arguments.path, arguments.pattern, arguments.include
        )
    return ToolResult.json(
        {
            "root": arguments.root,
            "sessionId": str(target_session_id),
            "path": arguments.path,
            **payload,
        }
    )


async def _provide_file(
    arguments: ProvideFileArguments, context: ToolContext
) -> ToolResult:
    if context.session.parent_session_id is not None:
        return ToolResult.error("tool.unavailable")
    selected = await files_access.project_read_files(arguments, context)
    if selected is None:
        return ToolResult.error("permission.denied")
    root_files, target_session_id = selected
    if arguments.root == "workspace":
        client = files_access.workspace_client(context)
        if not all(
            client.supports(operation)
            for operation in ("filesystem.stat", "filesystem.export")
        ):
            return ToolResult.error("executionAgent.operationUnavailable")
    paths = list(dict.fromkeys(
        files_access.execution_path(context, path)
        if arguments.root == "workspace"
        else root_files.normalize_relative_path(path)
        for path in (arguments.paths or [arguments.path])
    ))
    async def authorize(path: str) -> bool:
        request = files_access.filesystem_permission(
            context,
            root=arguments.root,
            path=path,
            capability="file.provide",
            action_kind="provide_file",
            action_label="Datei bereitstellen",
            root_files=root_files,
            target_session_id=target_session_id,
        )
        return await tools_access.allow(context, request)

    for source in paths:
        if not await authorize(source):
            return ToolResult.error("permission.denied")

    if target_session_id != context.session.id and await files_access.project_read_files(arguments, context) is None:
        return ToolResult.error("permission.denied")
    revision = None
    size = None
    for path in paths:
        if arguments.root == "workspace":
            stat_result = await client.request(
                "filesystem.stat", {"path": path}, WorkspaceStatResult
            )
            files_access.require_workspace_path(stat_result.path, path)
            kind, size = stat_result.kind, stat_result.size
        else:
            kind = await asyncio.to_thread(root_files.path_kind, arguments.root, path)
            if kind == "file" and arguments.paths is None:
                revision = await asyncio.to_thread(root_files.working_revision, path)
                size = revision.size_bytes
        if arguments.paths is not None and kind != "file":
            return ToolResult.error("file.notAFile", field="paths")
        if kind not in {"file", "directory"}:
            return ToolResult.error("file.notAFile", field="path")
    if arguments.paths is None and kind == "file" and size == 0:
        return ToolResult.error("attachment.empty")
    kind = "collection" if arguments.paths is not None else kind
    path = paths[0]
    filename = "files.zip" if kind == "collection" else path.rsplit("/", 1)[-1] or "files"
    if path == ".":
        filename = "files"
    if revision is not None and target_session_id == context.session.id:
        service = AttachmentService(root_files.session_storage, run_id=context.run_id)
        if await service.result_available(
            context.db, context.session.id, UUID(root_files.root_identity("session")), revision,
        ):
            return ToolResult.json({
                "root": arguments.root, "path": path, "filename": filename,
                "provided": False, "alreadyAvailable": True,
                "message": "This result is already displayed with download controls. No additional provision is needed.",
                "sessionId": str(target_session_id),
            })
    action = ToolFileAction(
        operation="provide", root=arguments.root,
        root_identity=root_files.root_identity("session") if arguments.root == "session" else None,
        path=path, filename=filename, size_bytes=size if kind == "file" else None,
        kind=kind,
        execution_agent_id=str(client.agent_id) if arguments.root == "workspace" else None,
        current_directory=client.current_directory if arguments.root == "workspace" else None,
        paths=tuple(paths) if kind == "collection" else None,
        archive_root_contents=arguments.path == ".",
        file_id=str(revision.file_id) if revision else None,
        file_revision=revision.revision if revision else None,
    )
    return ToolResult.json(
        {"root": arguments.root, "path": path, "filename": filename, "provided": True,
         "sizeBytes": action.size_bytes, "sessionId": str(target_session_id)},
        file_actions=(action,),
    )


async def _write_file(arguments: WriteFileArguments, context: ToolContext) -> ToolResult:
    if files_access.delegated_session_files(context, arguments.root):
        return ToolResult.error("tool.unavailable")
    if isinstance(arguments.content, str) and _omitted_file_content(arguments.content):
        return ToolResult.error("file.omittedContent", field="content")
    path = (
        files_access.execution_path(context, arguments.path)
        if arguments.root == "workspace"
        else context.root_files.normalize_relative_path(arguments.path)
    )
    if await files_access.active_file_is_dirty(context, arguments.root, path):
        return ToolResult.error("file.activeDirty", field="path")
    capability = (
        "session.files.write" if arguments.root == "session" else "filesystem.write"
    )
    request = files_access.filesystem_permission(
        context,
        root=arguments.root,
        path=path,
        capability=capability,
        action_kind="write_file",
        action_label="Datei schreiben",
    )
    if arguments.elevated:
        client = files_access.workspace_client(context)
        if not client.supports("administrator.run"):
            return ToolResult.error("executionAgent.operationUnavailable")
        request = administrator_permission(client.agent_id, client.current_directory, capability=capability, path=path)
        allowed = await administrator.authorize(context, request, arguments.sudo_password_ref)
    else:
        allowed = await tools_access.allow(context, request)
    if not allowed:
        return ToolResult.error("permission.denied")
    resolved = await resolve_protected_text(
        context, arguments.content if arguments.content is not None else arguments.protected_content,
    )
    if isinstance(resolved, ToolResult):
        return resolved
    content, _protected = resolved
    try:
        WriteFileArguments.model_validate({
            **arguments.model_dump(by_alias=True, exclude={"protected_content"}), "content": content,
        })
    except ValidationError as exc:
        return argument_error(exc, {"content": content})
    if _omitted_file_content(content):
        return ToolResult.error("file.omittedContent", field="content")
    if arguments.root == "workspace":
        result = await (administrator.invoke if arguments.elevated else files_access.workspace_request)(
            context, "filesystem.upsert",
            {
                "path": path,
                "content": content,
            },
            WorkspaceWriteResult,
            **({"reference": arguments.sudo_password_ref} if arguments.elevated else {}),
        )
        if isinstance(result, ToolResult):
            return result
        assert isinstance(result, WorkspaceWriteResult)
        files_access.require_workspace_path(result.path, path)
        return ToolResult.json(
            {
                "root": arguments.root,
                "written": result.path,
                "sizeBytes": result.size,
                "sha256": result.sha256,
            },
            file_actions=files_access.workspace_file_action(
                context, "write", result.path, result.size
            ),
        )
    written = await asyncio.to_thread(
        context.root_files.upsert_text_file,
        arguments.root,
        path,
        content,
    )
    encoded = content.encode("utf-8")
    return ToolResult.json(
        {
            "root": arguments.root,
            "written": written,
            "sizeBytes": len(encoded),
            "sha256": hashlib.sha256(encoded).hexdigest(),
        },
        file_actions=files_access.regular_file_action(
            context, "write", arguments.root, written
        ),
    )


def _omitted_file_content(content: str) -> bool:
    return content.startswith(
        ("[Persisted file content omitted: ", "[Persisted edit text omitted: ")
    )


async def _edit_file(arguments: EditFileArguments, context: ToolContext) -> ToolResult:
    if files_access.delegated_session_files(context, arguments.root):
        return ToolResult.error("tool.unavailable")
    if isinstance(arguments.old_text, str) and _omitted_file_content(arguments.old_text):
        return ToolResult.error("file.omittedContent", field="oldString")
    if isinstance(arguments.new_text, str) and _omitted_file_content(arguments.new_text):
        return ToolResult.error("file.omittedContent", field="newString")
    path = (
        files_access.execution_path(context, arguments.path)
        if arguments.root == "workspace"
        else context.root_files.normalize_relative_path(arguments.path)
    )
    if await files_access.active_file_is_dirty(context, arguments.root, path):
        return ToolResult.error("file.activeDirty", field="path")
    request = files_access.filesystem_permission(
        context,
        root=arguments.root,
        path=path,
        capability=(
            "session.files.write"
            if arguments.root == "session"
            else "filesystem.write"
        ),
        action_kind="edit_file",
        action_label="Datei bearbeiten",
    )
    if arguments.elevated:
        client = files_access.workspace_client(context)
        if not client.supports("administrator.run"):
            return ToolResult.error("executionAgent.operationUnavailable")
        request = administrator_permission(client.agent_id, client.current_directory, capability="filesystem.write", path=path)
        allowed = await administrator.authorize(context, request, arguments.sudo_password_ref)
    else:
        allowed = await tools_access.allow(context, request)
    if not allowed:
        return ToolResult.error("permission.denied")
    old = await resolve_protected_text(
        context, arguments.old_text if arguments.old_text is not None else arguments.protected_old_text,
    )
    if isinstance(old, ToolResult):
        return old
    new = await resolve_protected_text(
        context, arguments.new_text if arguments.new_text is not None else arguments.protected_new_text,
    )
    if isinstance(new, ToolResult):
        return new
    old_text, new_text = old[0], new[0]
    try:
        EditFileArguments.model_validate({
            **arguments.model_dump(by_alias=True, exclude={"protected_old_text", "protected_new_text"}),
            "oldString": old_text, "newString": new_text,
        })
    except ValidationError as exc:
        return argument_error(exc, {"oldString": old_text, "newString": new_text})
    if _omitted_file_content(old_text) or _omitted_file_content(new_text):
        return ToolResult.error("file.omittedContent")
    if arguments.root == "workspace":
        result = await (administrator.invoke if arguments.elevated else files_access.workspace_request)(
            context, "filesystem.edit",
            {
                "path": path,
                "oldText": old_text,
                "newText": new_text,
                "replaceAll": arguments.replace_all,
            },
            WorkspaceWriteResult,
            **({"reference": arguments.sudo_password_ref} if arguments.elevated else {}),
        )
        if isinstance(result, ToolResult):
            return result
        assert isinstance(result, WorkspaceWriteResult)
        files_access.require_workspace_path(result.path, path)
        return ToolResult.json(
            {
                "root": arguments.root,
                "edited": result.path,
                "sizeBytes": result.size,
                "sha256": result.sha256,
            },
            file_actions=files_access.workspace_file_action(
                context, "edit", result.path, result.size
            ),
        )
    edited = await asyncio.to_thread(
        context.root_files.edit_text_file,
        arguments.root,
        path,
        old_text,
        new_text,
        replace_all=arguments.replace_all,
    )
    edited_bytes = context.root_files.read_bytes(
        arguments.root, edited, limit=context.root_files.max_editable_text_bytes
    )
    return ToolResult.json(
        {
            "root": arguments.root,
            "edited": edited,
            "sizeBytes": len(edited_bytes),
            "sha256": hashlib.sha256(edited_bytes).hexdigest(),
        },
        file_actions=files_access.regular_file_action(context, "edit", arguments.root, edited),
    )


def _workspace_patch(
    context: ToolContext,
    patch: str,
) -> tuple[str, list[dict[str, str | None]], set[str]]:
    operations: list[dict[str, str | None]] = []
    edited_sources: set[str] = set()
    rewritten = []
    prefixes = {
        "*** Add File: ": "add",
        "*** Update File: ": "update",
        "*** Delete File: ": "delete",
    }
    for line in patch.split("\n"):
        matched = next((prefix for prefix in prefixes if line.startswith(prefix)), None)
        if matched is not None:
            path = files_access.execution_path(context, line.removeprefix(matched))
            operations.append(
                {"operation": prefixes[matched], "path": path, "destination": None}
            )
            rewritten.append(f"{matched}{path}")
        elif line.startswith("*** Move to: "):
            if not operations or operations[-1]["operation"] != "update":
                raise ExecutionClientError("tool.invalidArguments", {"field": "patchText", "reason": "invalid_format", "hint": "Place *** Move to: immediately after an update header, once per operation."})
            destination = files_access.execution_path(
                context, line.removeprefix("*** Move to: ")
            )
            operations[-1]["operation"] = "move"
            operations[-1]["destination"] = destination
            rewritten.append(f"*** Move to: {destination}")
        else:
            if (operations and operations[-1]["operation"] in {"update", "move"}
                    and line.startswith(("@@", " ", "+", "-"))):
                edited_sources.add(str(operations[-1]["path"]))
            rewritten.append(line)
    if not operations:
        raise ExecutionClientError("tool.invalidArguments", {"field": "patchText", "reason": "missing", "hint": "Include at least one file operation."})
    return "\n".join(rewritten), operations, edited_sources


async def _apply_patch(
    arguments: ApplyPatchArguments, context: ToolContext
) -> ToolResult:
    patch, operations, edited_sources = _workspace_patch(context, arguments.patch)
    context.repository_target_paths = tuple(
        str(path) for operation in operations
        for path in (operation["path"], operation["destination"]) if path is not None
    )
    for operation in operations:
        source = str(operation["path"])
        if operation["operation"] != "add" and await files_access.active_file_is_dirty(
            context, "workspace", source
        ):
            return ToolResult.error("file.activeDirty", field="patchText")
        requests = []
        if operation["operation"] in {"delete", "move"}:
            requests.append(
                files_access.filesystem_permission(
                    context,
                    root="workspace",
                    path=source,
                    capability="filesystem.delete",
                    action_kind="apply_patch",
                    action_label="Datei ändern",
                )
            )
        if operation["operation"] in {"add", "update"} or source in edited_sources:
            requests.append(
                files_access.filesystem_permission(
                    context,
                    root="workspace",
                    path=source,
                    capability="filesystem.write",
                    action_kind="apply_patch",
                    action_label="Datei ändern",
                )
            )
        if operation["destination"] is not None:
            requests.append(
                files_access.filesystem_permission(
                    context,
                    root="workspace",
                    path=str(operation["destination"]),
                    capability="filesystem.write",
                    action_kind="apply_patch",
                    action_label="Datei verschieben",
                )
            )
        for request in requests:
            if not await tools_access.allow(context, request):
                return ToolResult.error("permission.denied")
    result = await files_access.workspace_request(
        context,
        "filesystem.patch",
        {"patch": patch},
        WorkspacePatchResult,
    )
    assert isinstance(result, WorkspacePatchResult)
    if len(result.files) != len(operations) or any(
        file.operation != operation["operation"]
        or file.path != operation["path"]
        or file.destination != operation["destination"]
        for file, operation in zip(result.files, operations, strict=True)
    ):
        raise ExecutionClientError("executionAgent.protocolError")
    file_actions = tuple(
        files_access.workspace_file_action(
            context,
            {"add": "create", "update": "edit", "delete": "delete", "move": "move"}[file.operation],
            file.destination or file.path,
            file.size,
            source_path=file.path if file.operation == "move" else None,
            source_root="workspace" if file.operation == "move" else None,
        )[0]
        for file in result.files
    )
    return ToolResult.json(
        result.model_dump(mode="json", by_alias=True), file_actions=file_actions
    )


async def _create_directory(
    arguments: CreateDirectoryArguments, context: ToolContext
) -> ToolResult:
    if files_access.delegated_session_files(context, arguments.root):
        return ToolResult.error("tool.unavailable")
    request = files_access.filesystem_permission(
        context,
        root=arguments.root,
        path=arguments.path,
        capability=(
            "session.files.write" if arguments.root == "session" else "filesystem.write"
        ),
        action_kind="create_directory",
        action_label="Ordner erstellen",
    )
    if not await tools_access.allow(context, request):
        return ToolResult.error("permission.denied")
    if arguments.root == "workspace":
        path = files_access.execution_path(context, arguments.path)
        result = await files_access.workspace_request(
            context,
            "filesystem.mkdir",
            {
                "path": path,
                "parents": False,
            },
            WorkspacePathResult,
        )
        assert isinstance(result, WorkspacePathResult)
        files_access.require_workspace_path(result.path, path)
        return ToolResult.json(
            {"root": arguments.root, "created": result.path},
            file_actions=files_access.workspace_file_action(context, "mkdir", result.path, kind="directory"),
        )
    created = await asyncio.to_thread(
        context.root_files.create_directory, arguments.root, arguments.path
    )
    return ToolResult.json(
        {"root": arguments.root, "created": created},
        file_actions=files_access.regular_file_action(context, "mkdir", arguments.root, created),
    )


async def _delete_path(arguments: DeletePathArguments, context: ToolContext) -> ToolResult:
    if files_access.delegated_session_files(context, arguments.root):
        return ToolResult.error("tool.unavailable")
    request = files_access.filesystem_permission(
        context,
        root=arguments.root,
        path=arguments.path,
        capability="filesystem.delete",
        action_kind="delete_path",
        action_label="Pfad löschen",
    )
    if arguments.elevated:
        client = files_access.workspace_client(context)
        if not client.supports("administrator.run"):
            return ToolResult.error("executionAgent.operationUnavailable")
        request = administrator_permission(
            client.agent_id, client.current_directory, capability="filesystem.delete",
            path=files_access.execution_path(context, arguments.path),
        )
        allowed = await administrator.authorize(context, request, arguments.sudo_password_ref)
    else:
        allowed = await tools_access.allow(context, request)
    if not allowed:
        return ToolResult.error("permission.denied")
    if arguments.root == "workspace":
        result = await (administrator.invoke if arguments.elevated else files_access.workspace_request)(
            context,
            "filesystem.delete",
            {
                "path": files_access.execution_path(context, arguments.path),
                "recursive": arguments.recursive,
            },
            WorkspaceDeleteResult,
            **({"reference": arguments.sudo_password_ref} if arguments.elevated else {}),
        )
        if isinstance(result, ToolResult):
            return result
    else:
        await asyncio.to_thread(
            context.root_files.delete_path,
            arguments.root,
            arguments.path,
            recursive=arguments.recursive,
        )
    return ToolResult.json(
        {"root": arguments.root, "deleted": arguments.path},
        file_actions=(
            files_access.workspace_file_action(
                context, "delete", files_access.execution_path(context, arguments.path), kind="path"
            ) if arguments.root == "workspace" else
            files_access.regular_file_action(context, "delete", arguments.root, arguments.path)
        ),
    )


async def _copy_path(arguments: CopyPathArguments, context: ToolContext) -> ToolResult:
    if files_access.delegated_session_files(
        context, arguments.source_root, arguments.destination_root
    ):
        return ToolResult.error("tool.unavailable")
    context.repository_target_paths = tuple(
        files_access.execution_path(context, path)
        for root, path in ((arguments.source_root, arguments.source_path),
                           (arguments.destination_root, arguments.destination_path))
        if root == "workspace"
    )
    cross_root = arguments.source_root != arguments.destination_root
    if cross_root:
        client = files_access.workspace_client(context)
        required = (
            ("filesystem.stat", "filesystem.export")
            if arguments.source_root == "workspace"
            else ("filesystem.import",)
        )
        if not all(client.supports(operation) for operation in required):
            return ToolResult.error("executionAgent.operationUnavailable")
    source_request = files_access.filesystem_permission(
        context,
        root=arguments.source_root,
        path=arguments.source_path,
        capability="filesystem.read",
        action_kind="copy_path",
        action_label="Quelle kopieren",
    )
    destination_request = files_access.filesystem_permission(
        context,
        root=arguments.destination_root,
        path=arguments.destination_path,
        capability=(
            "session.files.write"
            if arguments.destination_root == "session"
            else "filesystem.write"
        ),
        action_kind="copy_path",
        action_label="Ziel erstellen",
    )
    if not await tools_access.allow(context, source_request) or not await tools_access.allow(context, destination_request):
        return ToolResult.error("permission.denied")
    if arguments.source_root == arguments.destination_root == "workspace":
        destination = files_access.execution_path(context, arguments.destination_path)
        result = await files_access.workspace_request(
            context,
            "filesystem.copy",
            {
                "path": files_access.execution_path(context, arguments.source_path),
                "destination": destination,
            },
            WorkspacePathResult,
        )
        assert isinstance(result, WorkspacePathResult)
        files_access.require_workspace_path(result.path, destination)
        source = files_access.execution_path(context, arguments.source_path)
        file_actions = files_access.workspace_file_action(
            context, "copy", result.path, kind="path", source_path=source, source_root="workspace"
        )
        client = files_access.workspace_client(context)
        if client.supports("filesystem.stat"):
            try:
                stat_result = await client.request(
                    "filesystem.stat", {"path": result.path}, WorkspaceStatResult
                )
                if (
                    isinstance(stat_result, WorkspaceStatResult)
                    and stat_result.path == result.path
                ):
                    file_actions = files_access.workspace_file_action(
                        context, "copy", result.path, stat_result.size, kind=stat_result.kind,
                        source_path=source, source_root="workspace",
                    )
            except ExecutionClientError:
                pass
        return ToolResult.json(
            {"root": arguments.destination_root, "copied": result.path},
            file_actions=file_actions,
        )
    if cross_root:
        source = files_access.file_path(context, arguments.source_root, arguments.source_path)
        destination = files_access.file_path(context, arguments.destination_root, arguments.destination_path)
        client = files_access.workspace_client(context)
        if arguments.source_root == "workspace":
            with await client.open_export(source, limit=MAX_EXPORT_BYTES) as stream:
                copying = asyncio.create_task(asyncio.to_thread(
                    context.root_files.create_file, FileRoot.SESSION, destination, stream,
                    max_bytes=MAX_EXPORT_BYTES,
                ))
                try:
                    copied = await asyncio.shield(copying)
                except asyncio.CancelledError:
                    while not copying.done():
                        try:
                            with CancelScope(shield=True):
                                await asyncio.shield(copying)
                        except asyncio.CancelledError:
                            continue
                    copying.result()
                    raise
            return ToolResult.json(
                {"root": arguments.destination_root, "copied": copied.path},
                file_actions=files_access.regular_file_action(
                    context, "copy", arguments.destination_root, copied.path,
                    source_path=source, source_root=arguments.source_root,
                ),
            )
        with context.root_files.open_file(FileRoot.SESSION, source) as handle:
            copied = await client.write_bytes(
                destination,
                handle,
                limit=MAX_EXPORT_BYTES,
            )
        return ToolResult.json(
            {"root": arguments.destination_root, "copied": copied.path},
            file_actions=files_access.workspace_file_action(
                context, "copy", copied.path, copied.size,
                source_path=source, source_root=arguments.source_root,
            ),
        )
    copied = await asyncio.to_thread(
        context.root_files.copy_path,
        arguments.source_root,
        arguments.source_path,
        arguments.destination_root,
        arguments.destination_path,
    )
    return ToolResult.json(
        {"root": arguments.destination_root, "copied": copied},
        file_actions=files_access.regular_file_action(
            context, "copy", arguments.destination_root, copied,
            source_path=arguments.source_path, source_root=arguments.source_root,
        ),
    )


async def _move_path(arguments: MovePathArguments, context: ToolContext) -> ToolResult:
    if files_access.delegated_session_files(
        context, arguments.source_root, arguments.destination_root
    ):
        return ToolResult.error("tool.unavailable")
    if arguments.source_root != arguments.destination_root:
        return ToolResult.error("file.crossBoundaryMoveUnavailable")
    if arguments.source_root == "workspace":
        context.repository_target_paths = tuple(
            files_access.execution_path(context, path)
            for path in (arguments.source_path, arguments.destination_path)
        )
    source_request = files_access.filesystem_permission(
        context,
        root=arguments.source_root,
        path=arguments.source_path,
        capability=(
            "session.files.write"
            if arguments.source_root == "session"
            else "filesystem.delete"
        ),
        action_kind="move_path",
        action_label="Quelle verschieben",
    )
    destination_request = files_access.filesystem_permission(
        context,
        root=arguments.destination_root,
        path=arguments.destination_path,
        capability=(
            "session.files.write"
            if arguments.destination_root == "session"
            else "filesystem.write"
        ),
        action_kind="move_path",
        action_label="Ziel erstellen",
    )
    if not await tools_access.allow(context, source_request) or not await tools_access.allow(context, destination_request):
        return ToolResult.error("permission.denied")
    if arguments.source_root == arguments.destination_root == "workspace":
        destination = files_access.execution_path(context, arguments.destination_path)
        result = await files_access.workspace_request(
            context,
            "filesystem.move",
            {
                "path": files_access.execution_path(context, arguments.source_path),
                "destination": destination,
            },
            WorkspacePathResult,
        )
        assert isinstance(result, WorkspacePathResult)
        files_access.require_workspace_path(result.path, destination)
        source = files_access.execution_path(context, arguments.source_path)
        file_actions = files_access.workspace_file_action(
            context, "move", result.path, kind="path", source_path=source, source_root="workspace"
        )
        client = files_access.workspace_client(context)
        if client.supports("filesystem.stat"):
            try:
                stat_result = await client.request(
                    "filesystem.stat", {"path": result.path}, WorkspaceStatResult
                )
                if (
                    isinstance(stat_result, WorkspaceStatResult)
                    and stat_result.path == result.path
                ):
                    file_actions = files_access.workspace_file_action(
                        context, "move", result.path, stat_result.size, kind=stat_result.kind,
                        source_path=source, source_root="workspace",
                    )
            except ExecutionClientError:
                pass
        return ToolResult.json(
            {"root": arguments.destination_root, "moved": result.path},
            file_actions=file_actions,
        )
    moved = await asyncio.to_thread(
        context.root_files.move_path,
        arguments.source_root,
        arguments.source_path,
        arguments.destination_root,
        arguments.destination_path,
    )
    return ToolResult.json(
        {"root": arguments.destination_root, "moved": moved},
        file_actions=files_access.regular_file_action(
            context, "move", arguments.destination_root, moved,
            source_path=arguments.source_path, source_root=arguments.source_root,
        ),
    )


READ = ToolDefinition(
            "read",
            "Read directories, UTF-8 text, PDFs, or images directly. "
            "offset/limit paginate text/directories; pdf_pages selects visual PDF pages. "
            "Text is not visual evidence. elevated=true: UTF-8 computer files; optional "
            "sudo_password_ref, else reuse/request authentication.",
            ReadArguments,
            _read,
            tool_type="shared",
            group="filesystem",
            label="Pfad lesen",
            icon="description",
            actions=(_SESSION_FILES_READ, _FILESYSTEM_READ),
            alternative_action_groups=(
                frozenset({"session.files.read", "filesystem.read"}),
            ),
        )

GLOB = ToolDefinition(
            "glob",
            "Glob files recursively, max 100. Skip hidden/.git/symlinks. "
            "root=computer honors ignores; root=files overrides them. truncated=true: more/size limit.",
            GlobArguments,
            _glob,
            tool_type="shared",
            group="filesystem",
            label="Dateien suchen",
            icon="folder_open",
            actions=(_SESSION_FILES_READ, _FILESYSTEM_READ),
            alternative_action_groups=(
                frozenset({"session.files.read", "filesystem.read"}),
            ),
        )

GREP = ToolDefinition(
            "grep",
            "Regex in a file or recursively in a directory; include glob filters directories only. "
            "Explicit files bypass hidden/ignore discovery filters. Max 100 lines, 2000 characters/line. "
            "Skip .git/binary/symlinks. Directory root=computer skips hidden/ignored; "
            "root=files includes hidden; include overrides ignores. "
            "truncated=true: more/clipped text/size limit.",
            GrepArguments,
            _grep,
            tool_type="shared",
            group="filesystem",
            label="Dateiinhalte durchsuchen",
            icon="manage_search",
            actions=(_SESSION_FILES_READ, _FILESYSTEM_READ),
            alternative_action_groups=(
                frozenset({"session.files.read", "filesystem.read"}),
            ),
        )

PROVIDE_FILE = ToolDefinition(
            "provide_file",
            "For explicit file, download or ZIP requests, reference a file/folder with path or files with paths. "
            "Skip attachments already shown with downloads. Pass source paths, not a new archive: folders/lists "
            "become ZIPs on download. References open current files; no copy to session storage.",
            ProvideFileArguments,
            _provide_file,
            tool_type="shared",
            group="filesystem",
            label="Datei bereitstellen",
            icon="download",
            actions=(_FILE_PROVIDE,),
        )

WRITE_FILE = ToolDefinition(
            "write_file",
            "Create/overwrite a UTF-8 file. root defaults to computer; use files for session storage. "
            "Parents must exist; otherwise use create_directory. Use edit_file for partial changes. "
            "Set exactly one: content (string) or protected_content (secret_input value_ref parts). Resolve after authorization. "
             "elevated=true uses administrator rights for root=computer; "
              "pass a matching sudo_password_ref or omit it for automatic authentication reuse or protected input.",
            WriteFileArguments,
            _write_file,
            tool_type="shared",
            group="filesystem",
            label="Datei schreiben",
            icon="edit_document",
            actions=(_SESSION_FILES_WRITE, _FILESYSTEM_WRITE),
            alternative_action_groups=(
                frozenset({"session.files.write", "filesystem.write"}),
            ),
        )

EDIT_FILE = ToolDefinition(
            "edit_file",
            "Replace exact text in a UTF-8 file. root defaults to computer; use files for session storage. "
             "oldString must be unique unless replaceAll=true. Preserve indentation; omit line-number prefixes. "
             "For each input set exactly one: plain string or protected_oldString/protected_newString with value_ref parts. "
              "elevated=true uses administrator rights for root=computer; "
              "pass a matching sudo_password_ref or omit it for automatic authentication reuse or protected input.",
            EditFileArguments,
            _edit_file,
            tool_type="shared",
            group="filesystem",
            label="Datei bearbeiten",
            icon="edit_note",
            actions=(_SESSION_FILES_WRITE, _FILESYSTEM_WRITE),
            alternative_action_groups=(
                frozenset({"session.files.write", "filesystem.write"}),
            ),
        )

CREATE_DIRECTORY = ToolDefinition(
            "create_directory",
            "Create one directory; its parent must exist.",
            CreateDirectoryArguments,
            _create_directory,
            tool_type="shared",
            group="filesystem",
            label="Ordner erstellen",
            icon="create_new_folder",
            actions=(_SESSION_FILES_WRITE, _FILESYSTEM_WRITE),
            alternative_action_groups=(
                frozenset({"session.files.write", "filesystem.write"}),
            ),
        )

DELETE_PATH = ToolDefinition(
            "delete_path",
            "Delete a path; recursive=true for non-empty directories. "
            "elevated=true: computer only; optional sudo_password_ref.",
            DeletePathArguments,
            _delete_path,
            tool_type="shared",
            group="filesystem",
            label="Pfad löschen",
            icon="delete_outline",
            actions=(_SESSION_FILES_DELETE, _FILESYSTEM_DELETE),
            alternative_action_groups=(
                frozenset({"session.files.delete", "filesystem.delete"}),
            ),
        )

COPY_PATH = ToolDefinition(
            "copy_path",
            "Copy a path to an exact new destination path; never overwrite.",
            CopyPathArguments,
            _copy_path,
            tool_type="shared",
            group="filesystem",
            label="Pfad kopieren",
            icon="content_copy",
            actions=(
                _SESSION_FILES_READ, _FILESYSTEM_READ,
                _SESSION_FILES_WRITE, _FILESYSTEM_WRITE,
            ),
            alternative_action_groups=(
                frozenset({"session.files.read", "filesystem.read"}),
                frozenset({"session.files.write", "filesystem.write"}),
            ),
        )

MOVE_PATH = ToolDefinition(
            "move_path",
            "Move a path to an exact new destination path; never overwrite.",
            MovePathArguments,
            _move_path,
            tool_type="shared",
            group="filesystem",
            label="Pfad verschieben",
            icon="drive_file_move",
            actions=(_FILESYSTEM_DELETE, _SESSION_FILES_WRITE, _FILESYSTEM_WRITE),
            alternative_action_groups=(
                frozenset({"session.files.write", "filesystem.delete"}),
                frozenset({"session.files.write", "filesystem.write"}),
            ),
        )

APPLY_PATCH = ToolDefinition(
            "apply_patch",
            "Apply one bounded patch to computer files in the current directory. Paths may be relative "
            "or absolute. Pass patchText using LF text enclosed by *** Begin Patch and "
            "*** End Patch. File headers are *** Add File: path (all lines prefixed +), "
            "*** Delete File: path (no body), or *** Update File: path. An update may include "
            "*** Move to: new-path immediately after its header, with or without content changes. "
            "Add/move destinations must not exist; parent directories must exist. "
            "In update hunks, prefix unchanged lines with a space, removals with -, additions with +. "
            "Separate hunks with @@; the first @@ is optional. @@ exact-line searches after that "
            "unique line. Context must match exactly and uniquely; hunks follow source order. "
            "Context-only hunks are allowed, but an update must change text or move the file. "
            "*** End of File anchors the last hunk at the file end. A hunk containing only + lines "
            "appends to the file, including an empty file; use context for other insertions. "
            "Example:\n*** Begin Patch\n*** Update File: app.py\n@@\n-old()\n+new()\n*** End Patch\n"
            "On file.patchIncomplete, reread affected source and destination paths before retrying; "
            "earlier changes may remain.",
            ApplyPatchArguments,
            _apply_patch,
            tool_type="computer",
            required_operation="filesystem.patch",
            group="filesystem",
            label="Patch anwenden",
            icon="difference",
            actions=(_FILESYSTEM_WRITE, _FILESYSTEM_DELETE),
            alternative_action_groups=(
                frozenset({"filesystem.write", "filesystem.delete"}),
            ),
        )
