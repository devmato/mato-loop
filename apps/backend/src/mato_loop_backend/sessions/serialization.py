from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlencode
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

from mato_loop_backend.agent.run_timing import run_timing_payload
from mato_loop_backend.error_contract import describe_error
from mato_loop_backend.execution.paths import is_absolute_path
from mato_loop_backend.files.service import RootFileError, RootFileService
from mato_loop_backend.media.processing import claimed_audio_format
from mato_loop_backend.message_content import canonical_assistant_content
from mato_loop_backend.models import (
    AgentRun,
    ChatSession,
    ContextSummary,
    MemoryMode,
    Message,
    MessageAttachment,
    WebSearchPolicy,
)
from mato_loop_backend.settings.session_profiles import SessionProfileSnapshot
from mato_loop_backend.tools.display import enrich_tool_calls
from mato_loop_backend.web.fetch import WebFetchError, canonicalize_url

LAST_MESSAGE_AT_UNSET = object()
_PUBLIC_WARNING_CODES = {"privacy.placeholderRestorationIncomplete", "agent.toolCallLimitReached"}


class _StoredFileAction(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    operation: Literal["write", "create", "edit", "delete", "mkdir", "copy", "move", "provide"]
    root: str = Field(pattern=r"^(session|workspace)$")
    root_identity: str | None = Field(
        default=None, alias="rootIdentity", min_length=1, max_length=1_000
    )
    path: str = Field(min_length=1, max_length=4_096)
    filename: str = Field(min_length=1, max_length=1_000)
    size_bytes: int | None = Field(alias="sizeBytes", ge=0)
    kind: Literal["file", "directory", "collection", "path"] = "file"
    source_path: str | None = Field(default=None, alias="sourcePath", max_length=4_096)
    source_root: Literal["session", "workspace"] | None = Field(default=None, alias="sourceRoot")
    paths: list[str] | None = Field(default=None, min_length=1, max_length=1_000)
    archive_root_contents: bool = Field(default=False, alias="archiveRootContents")
    file_id: str | None = Field(default=None, alias="fileId")
    file_revision: int | None = Field(default=None, alias="fileRevision", ge=1)
    presentation: Literal["result", "internal"] = "result"
    execution_agent_id: str | None = Field(
        default=None, alias="executionAgentId", min_length=1, max_length=36
    )
    current_directory: str | None = Field(
        default=None, alias="currentDirectory", min_length=1, max_length=4_096
    )

    @model_validator(mode="after")
    def validate_paths(self) -> _StoredFileAction:
        service = RootFileService(UUID(int=0))
        if self.file_id is not None and (self.root != "session" or str(UUID(self.file_id)) != self.file_id):
            raise ValueError("file identity must be a canonical session file UUID")
        if self.paths is not None:
            if self.operation != "provide" or self.kind != "collection":
                raise ValueError("file lists are only supported for provided collections")
            for path in self.paths:
                if len(path) > 4_096 or (
                    self.root == "session" and (service.normalize_relative_path(path) != path or path == ".")
                ) or (self.root == "workspace" and not is_absolute_path(path)):
                    raise ValueError("invalid provided file path")
        if self.root == "session" and (
            service.normalize_relative_path(self.path) != self.path
            or (self.path == "." and not (self.operation == "provide" and self.kind == "directory"))
        ):
            raise ValueError("path must be a normalized file path")
        if self.root == "workspace" and not is_absolute_path(self.path):
            raise ValueError("workspace path must be absolute")
        if self.filename != self.path.rsplit("/", 1)[-1] and not (
            self.operation == "provide" and (
                (self.kind == "collection" and self.paths and self.filename == "files.zip")
                or (self.kind == "directory" and self.path in {".", "/"} and self.filename == "files")
            )
        ):
            raise ValueError("filename must match path")
        snapshot_fields = {"execution_agent_id", "current_directory"}
        if self.root == "session":
            if self.model_fields_set & snapshot_fields:
                raise ValueError("session actions cannot contain workspace snapshot fields")
            try:
                if self.root_identity is None:
                    raise ValueError
                if str(UUID(self.root_identity)) != self.root_identity:
                    raise ValueError("session identity must be a canonical UUID")
            except ValueError as exc:
                raise ValueError("session identity must be a canonical UUID") from exc
            return self
        if self.root_identity is not None or any(
            getattr(self, field) is None for field in snapshot_fields
        ):
            raise ValueError("workspace snapshot fields are required")
        assert self.execution_agent_id is not None
        assert self.current_directory is not None
        try:
            valid_id = str(UUID(self.execution_agent_id)) == self.execution_agent_id
        except ValueError:
            valid_id = False
        if not valid_id or not is_absolute_path(self.current_directory):
            raise ValueError("workspace identifiers must be canonical and absolute")
        return self


class _PreparedReplyOption(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str = Field(pattern=r"^[A-Za-z0-9_-]+$", min_length=1, max_length=100)
    content: str = Field(min_length=1, max_length=2_000)


class _PreparedReply(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    markdown: str = Field(min_length=1, max_length=50_000)
    options: list[_PreparedReplyOption] = Field(min_length=1, max_length=20)


def _public_research_payload(item: Message) -> dict[str, Any] | None:
    provider_data = item.provider_data if isinstance(item.provider_data, dict) else {}
    runtime_data = provider_data.get("_matoloop")
    if not isinstance(runtime_data, dict):
        return None
    research = runtime_data.get("research")
    if not isinstance(research, dict):
        return None
    policy = research.get("policy")
    used = research.get("used")
    if policy not in {item.value for item in WebSearchPolicy} or not isinstance(used, bool):
        return None
    linked_sources = []
    raw_sources = research.get("linkedSources")
    if isinstance(raw_sources, list):
        for source in raw_sources[:100]:
            if not isinstance(source, dict):
                continue
            title = source.get("title")
            url = source.get("url")
            if not isinstance(title, str) or not title.strip() or not isinstance(url, str):
                continue
            try:
                safe_url = canonicalize_url(url, preserve_fragment=True)
            except WebFetchError:
                continue
            linked_sources.append({"title": title.strip()[:500], "url": safe_url})
    return {"policy": policy, "used": used, "linkedSources": linked_sources}


def _public_warnings(item: Message) -> list[str]:
    if item.role != "assistant" or not isinstance(item.provider_data, dict):
        return []
    runtime_data = item.provider_data.get("_matoloop")
    warnings = runtime_data.get("warnings") if isinstance(runtime_data, dict) else None
    if not isinstance(warnings, list) or len(warnings) > 10:
        return []
    return list(
        dict.fromkeys(
            code
            for code in warnings
            if isinstance(code, str) and code in _PUBLIC_WARNING_CODES
        )
    )


def _public_active_duration_ms(item: Message) -> int | None:
    if item.role != "assistant" or not isinstance(item.provider_data, dict):
        return None
    runtime_data = item.provider_data.get("_matoloop")
    performance = runtime_data.get("performance") if isinstance(runtime_data, dict) else None
    duration = performance.get("activeDurationMs") if isinstance(performance, dict) else None
    return (
        duration
        if isinstance(duration, int) and not isinstance(duration, bool) and duration >= 0
        else None
    )


def public_file_actions(item: Message) -> list[dict[str, Any]]:
    if item.role != "tool" or not isinstance(item.provider_data, dict):
        return []
    runtime_data = item.provider_data.get("_matoloop")
    raw_actions = runtime_data.get("fileActions") if isinstance(runtime_data, dict) else None
    if isinstance(runtime_data, dict) and (
        runtime_data.get("fileOutputsPending") or runtime_data.get("fileOutputsHidden")
    ):
        return []
    if not isinstance(raw_actions, list):
        return []
    actions = []
    for raw_action in raw_actions:
        try:
            action = _StoredFileAction.model_validate(raw_action)
        except (RootFileError, ValidationError, ValueError):
            continue
        if action.presentation == "internal":
            continue
        scope = "session" if action.root == "session" else "filesystem"
        query = {
            "path": action.path,
            "scope": scope,
            "session_id": str(item.session_id),
            **(
                {"root_identity": action.root_identity}
                if action.root == "session"
                else {"execution_agent_id": action.execution_agent_id}
            ),
        }
        if action.root == "workspace":
            query.update(
                execution_agent_id=action.execution_agent_id,
                current_directory=action.current_directory,
            )
        reference_url = f"/api/workspace/files/provided/{item.session_id}/{item.id}/{len(actions)}"
        actions.append(
            {
                **action.model_dump(
                    mode="json", by_alias=True, exclude_none=True, exclude_unset=True, exclude={"presentation"}
                ),
                "downloadUrl": (
                    reference_url + "/download" if action.operation == "provide"
                    else "/api/workspace/files/download?" + urlencode(query)
                ),
            }
        )
    return actions


def _public_prepared_reply(item: Message) -> dict[str, Any] | None:
    if item.role != "tool" or not isinstance(item.provider_data, dict):
        return None
    runtime_data = item.provider_data.get("_matoloop")
    value = runtime_data.get("preparedReply") if isinstance(runtime_data, dict) else None
    try:
        reply = _PreparedReply.model_validate(value)
    except (ValidationError, ValueError):
        return None
    if len({option.id for option in reply.options}) != len(reply.options):
        return None
    return reply.model_dump(mode="json")


def _attachment_presentation(
    item: Message, attachment: MessageAttachment
) -> Literal["result", "internal"] | None:
    value = (attachment.metadata_json or {}).get("toolOutputPresentation")
    if value in {"result", "internal"}:
        return value
    if item.role != "tool":
        return "result"
    runtime_data = (item.provider_data or {}).get("_matoloop", {})
    tool_name = runtime_data.get("toolName") if isinstance(runtime_data, dict) else None
    if not tool_name:
        return None
    if (
        tool_name in {"browser", "browser_take_screenshot", "webfetch"}
        and attachment.kind == "image"
    ):
        return "internal"
    if tool_name == "render_openscad" and (
        attachment.kind == "image"
        or attachment.mime_type == "model/vnd.off"
        or str(attachment.filename or "").lower().endswith(".off")
    ):
        return "internal"
    return "result"


def session_payload(
    item: ChatSession,
    control_center: dict[str, Any],
    *,
    effective_memory_mode: MemoryMode | None = None,
    memory_tools_available: bool | None = None,
    memory_unavailable_reason: str | None = None,
    workspace_context: dict[str, Any] | None = None,
    default_channel: dict[str, Any] | None = None,
    last_message_at: datetime | None | object = LAST_MESSAGE_AT_UNSET,
    has_unread_messages: bool | None = None,
) -> dict[str, Any]:
    file_view = control_center.get("file_view")
    file_views = control_center.get("file_views")
    payload = {
        "id": str(item.id),
        "parentSessionId": str(item.parent_session_id) if item.parent_session_id else None,
        "spawnedByRunId": str(item.spawned_by_run_id) if item.spawned_by_run_id else None,
        "spawnedByToolCallId": item.spawned_by_tool_call_id,
        "spawnIndex": item.spawn_index,
        "readOnly": item.parent_session_id is not None,
        "archivedAt": item.archived_at.isoformat() if item.archived_at else None,
        "projectId": str(item.project_id) if item.project_id else None,
        "title": item.title,
        "titleGenerationStatus": item.title_generation_status,
        "pinned": bool(item.is_pinned),
        "todosCollapsed": (item.control_center or {}).get("todos_collapsed") is True,
        "controlCenter": {
            "tabs": [_control_center_tab_payload(tab) for tab in control_center["tabs"]],
            "activeAppId": control_center["active_app_id"],
            "open": bool(control_center.get("open", False)),
            "fileView": (
                {
                    "scope": file_view["scope"],
                    "selectedPath": file_view["selected_path"],
                    "selectedKind": file_view["selected_kind"],
                    "viewedFilePath": file_view.get("viewed_file_path"),
                    "rootIdentity": file_view.get("root_identity"),
                    "executionAgentId": file_view.get("execution_agent_id"),
                    "currentDirectory": file_view.get("current_directory"),
                }
                if isinstance(file_view, dict)
                else None
            ),
            "fileViews": {
                scope: _control_center_file_view_payload(
                    file_views.get(scope) if isinstance(file_views, dict) else None
                )
                for scope in ("session", "filesystem")
            },
        },
        "modelId": str(item.model_config_id) if item.model_config_id else None,
        "sessionProfile": SessionProfileSnapshot.model_validate(item.session_profile or {}).model_dump(mode="json", by_alias=True),
        "agentProfileId": (
            str(item.agent_profile_id) if item.agent_profile_id else None
        ),
        "reasoningEffort": item.reasoning_effort,
        "memoryMode": item.memory_mode,
        "trustLevel": item.trust_level,
        "webSearchPolicy": item.web_search_policy,
        "preferredExecutionAgentId": (
            str(item.preferred_execution_agent_id)
            if item.preferred_execution_agent_id
            else None
        ),
        "currentDirectory": item.current_directory,
        "workspaceMode": item.workspace_mode or "local",
        "codingWorkspace": item.coding_workspace,
        "workspaceContext": workspace_context
        or {
            "executionAgentId": (
                str(item.preferred_execution_agent_id)
                if item.preferred_execution_agent_id
                else None
            ),
            "connected": False,
            "operations": [],
        },
        "defaultChannel": default_channel or {"type": "app"},
        "createdAt": item.created_at.isoformat() if item.created_at else None,
        "updatedAt": item.updated_at.isoformat() if item.updated_at else None,
    }
    if last_message_at is not LAST_MESSAGE_AT_UNSET:
        payload["lastMessageAt"] = (
            last_message_at.isoformat() if isinstance(last_message_at, datetime) else None
        )
    if has_unread_messages is not None:
        payload["hasUnreadMessages"] = has_unread_messages
    if effective_memory_mode is not None:
        payload["effectiveMemoryMode"] = effective_memory_mode.value
        payload["memoryToolsAvailable"] = memory_tools_available
        payload["memoryUnavailableReason"] = memory_unavailable_reason
    return payload


def _control_center_tab_payload(tab: dict[str, Any]) -> dict[str, Any]:
    target = tab.get("file_target")
    return {
        "tabId": tab.get("tab_id") or tab["app_id"],
        "kind": tab.get("kind", "app"),
        "appId": tab["app_id"],
        "pinned": tab["pinned"],
        **(
            {
                "fileTarget": {
                    "scope": target["scope"],
                    "path": target["path"],
                    "name": target["name"],
                    "rootIdentity": target.get("root_identity"),
                    "executionAgentId": target.get("execution_agent_id"),
                    "currentDirectory": target.get("current_directory"),
                    "readOnly": bool(target.get("read_only", False)),
                    **({"fileId": target["file_id"], "revision": target["revision"]}
                       if target.get("revision") is not None else {}),
                }
            }
            if isinstance(target, dict)
            else {}
        ),
    }


def _control_center_file_view_payload(
    file_view: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(file_view, dict):
        return None
    return {
        "scope": file_view["scope"],
        "selectedPath": file_view["selected_path"],
        "selectedKind": file_view["selected_kind"],
        "viewedFilePath": file_view.get("viewed_file_path"),
        "rootIdentity": file_view.get("root_identity"),
        "executionAgentId": file_view.get("execution_agent_id"),
        "currentDirectory": file_view.get("current_directory"),
    }


def message_payload(
    item: Message,
    *,
    retry_available: bool = False,
    attribution: tuple[UUID, UUID | None] | None = None,
    attribution_resolved: bool = False,
) -> dict[str, Any]:
    runtime_data = (item.provider_data or {}).get("_matoloop", {})
    command = runtime_data.get("codingCommand") if item.role == "user" and isinstance(runtime_data, dict) else None
    command_prompt = command.get("prompt") if isinstance(command, dict) else None
    run_id = runtime_data.get("runId") if isinstance(runtime_data, dict) else None
    agent_profile_id = (
        runtime_data.get("agentProfileId") if isinstance(runtime_data, dict) else None
    )
    try:
        run_id = str(UUID(run_id)) if isinstance(run_id, str) else None
    except ValueError:
        run_id = None
    try:
        agent_profile_id = (
            str(UUID(agent_profile_id)) if isinstance(agent_profile_id, str) else None
        )
    except ValueError:
        agent_profile_id = None
    if attribution_resolved:
        run_id = str(attribution[0]) if attribution is not None else None
        agent_profile_id = (
            str(attribution[1])
            if attribution is not None and attribution[1] is not None
            else None
        )
    elif item.role == "assistant" and not (run_id and agent_profile_id):
        run_id = agent_profile_id = None
    if attribution is not None and not attribution_resolved:
        run_id = str(attribution[0])
        agent_profile_id = str(attribution[1]) if attribution[1] is not None else None
    outputs_hidden = item.role == "tool" and isinstance(runtime_data, dict) and (
        runtime_data.get("fileOutputsPending") or runtime_data.get("fileOutputsHidden")
    )
    historical_error = runtime_data.get("historicalError", {}) if item.role == "assistant" else {}
    error_code = item.error_code or historical_error.get("code")
    error = (
        describe_error(error_code, params=item.error_params if item.error_code else historical_error.get("params"))
        if error_code
        else None
    )
    source = (item.provider_data or {}).get("_matoloop", {}).get("source", {})
    return {
        "id": str(item.id),
        "role": item.role,
        "kind": "runtime_error" if error else "message",
        "state": "error" if error else "normal",
        "origin": "host" if error or runtime_data.get("origin") == "host" else item.role,
        "content": (
            error.message
            if error
            else canonical_assistant_content(item.content)
            if item.role == "assistant"
            else item.content
        ),
        **({"commandPrompt": command_prompt} if isinstance(command_prompt, str) else {}),
        "modelName": item.model_name,
        "generationStatus": item.generation_status or "completed",
        "continuationSegment": item.role == "assistant" and isinstance(runtime_data, dict)
        and runtime_data.get("continuationSegment") is True,
        "redirected": (
            item.role == "assistant"
            and item.generation_status == "interrupted"
            and isinstance(runtime_data, dict)
            and runtime_data.get("redirected") is True
        ),
        "trustLevel": item.trust_level,
        "toolCallId": item.tool_call_id,
        "toolCalls": enrich_tool_calls(item.tool_calls),
        "reasoning": item.public_reasoning,
        "activeDurationMs": _public_active_duration_ms(item),
        "warnings": _public_warnings(item),
        "research": _public_research_payload(item),
        "fileActions": public_file_actions(item),
        "preparedReply": _public_prepared_reply(item),
        **(
            {"runId": run_id, "agentProfileId": agent_profile_id}
            if item.role == "assistant"
            else {}
        ),
        **({"runId": run_id} if item.role == "tool" and run_id is not None else {}),
        **(
            {"continuationRunId": runtime_data["continuationRunId"]}
            if item.role == "user" and isinstance(runtime_data, dict)
            and isinstance(runtime_data.get("continuationRunId"), str)
            else {}
        ),
        "sourceType": (
            "automationRun"
            if isinstance(source, dict) and source.get("type") == "automationRun"
            else None
        ),
        **({"automationName": source["automationName"][:240]}
           if isinstance(source, dict) and source.get("type") == "automationRun"
           and isinstance(source.get("automationName"), str) else {}),
        "source": (
            {
                "type": "endpoint",
                "provider": item.source_provider,
                "endpointId": str(item.source_endpoint_id),
            }
            if item.role == "user"
            and item.source_type == "endpoint"
            and item.source_provider
            and item.source_endpoint_id
            else {"type": "app"}
            if item.role == "user"
            else None
        ),
        "error": error.payload() if error else None,
        "retryAvailable": retry_available,
        "attachments": [
            {
                "id": str(attachment.id),
                "kind": attachment.kind,
                "category": attachment.category,
                "purpose": attachment.purpose,
                "presentation": _attachment_presentation(item, attachment),
                "processingStatus": attachment.processing_status,
                "mimeType": attachment.mime_type,
                "sizeBytes": attachment.size_bytes,
                "filename": attachment.filename,
                "width": attachment.width,
                "height": attachment.height,
                "durationMs": attachment.duration_ms,
                "metadata": {
                    key: value for key, value in (attachment.metadata_json or {}).items()
                    if key != "toolOutputPresentation"
                },
                "fileRootId": str(attachment.file_root_id) if attachment.file_root_id else None,
                "fileId": str(attachment.file_id) if attachment.file_id else None,
                "fileRevision": attachment.file_revision,
                "filePath": attachment.file_path,
                "url": (
                    f"/api/sessions/{item.session_id}/messages/{item.id}"
                    f"/attachments/{attachment.id}"
                ),
                **(
                    {
                        "playbackUrl": (
                            f"/api/sessions/{item.session_id}/messages/{item.id}"
                            f"/attachments/{attachment.id}?playback=true"
                        ),
                        "playbackMimeType": "audio/wav",
                    }
                    if attachment.kind == "audio"
                    and attachment.processing_status == "ready"
                    and claimed_audio_format(attachment.mime_type, "") not in {None, "wav"}
                    else {}
                ),
            }
            for attachment in item.attachments
            if not outputs_hidden
        ],
        "createdAt": item.created_at.isoformat() if item.created_at else None,
    }


def context_summary_payload(item: ContextSummary) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "coveredThroughMessageId": str(item.covered_through_message_id),
        "content": item.content,
        "isFallback": item.is_fallback,
        "createdAt": item.created_at.isoformat() if item.created_at else None,
    }


def run_payload(run: AgentRun) -> dict[str, Any]:
    return {
        "id": str(run.id),
        "sessionId": str(run.session_id),
        "status": run.status,
        "modelName": run.model_name,
        "agentProfileId": str(run.agent_profile_id) if run.agent_profile_id else None,
        "error": describe_error(run.error).payload() if run.error else None,
        "createdAt": run.created_at.isoformat() if run.created_at else None,
        **run_timing_payload(run),
    }
