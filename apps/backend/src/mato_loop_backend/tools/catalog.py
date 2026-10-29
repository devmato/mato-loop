from __future__ import annotations

import mato_loop_backend.permissions.resolution as permissions_resolution
import mato_loop_backend.tools.access as tools_access
import mato_loop_backend.tools.automations as tools_automations
import mato_loop_backend.tools.coding as tools_coding
import mato_loop_backend.tools.computer as tools_computer
import mato_loop_backend.tools.context_search as tools_context_search
import mato_loop_backend.tools.discovery as tools_discovery
import mato_loop_backend.tools.files as tools_files
import mato_loop_backend.tools.identity as tools_identity
import mato_loop_backend.tools.interaction as tools_interaction
import mato_loop_backend.tools.media as tools_media
import mato_loop_backend.tools.memory as tools_memory
import mato_loop_backend.tools.permissions as tools_permissions
import mato_loop_backend.tools.personal_settings as tools_personal_settings
import mato_loop_backend.tools.sessions as tools_sessions
import mato_loop_backend.tools.skills as tools_skills
import mato_loop_backend.tools.web as tools_web
from mato_loop_backend.integrations.registry import (
    effective_integration_contributions as effective_integration_contributions,
)
from mato_loop_backend.settings import ModelCapability, ToolExecutionPolicy, ToolSettings
from mato_loop_backend.tools.actions import (
    _SENSITIVE_DATA_ACCESS,
    _SESSION_READ_SENSITIVE,
    _WORKING_DIRECTORY_EXTERNAL,
    ToolAction,
)
from mato_loop_backend.tools.contracts import ToolDefinition as ToolDefinition
from mato_loop_backend.tools.registry import ToolRegistry as _BaseToolRegistry

_MEDIA_SETTINGS_FIELDS = {
    "media.generate.image": "generate_image",
    "media.edit.image": "edit_image",
    "media.generate.speech": "generate_speech",
    "media.generate.audio": "generate_audio",
    "media.transcribe.audio": "transcribe_audio",
    "media.analyze.video": "analyze_video",
    "media.analyze.audio": "analyze_audio",
}


_MEDIA_MODEL_CAPABILITIES = {
    "media.generate.image": ModelCapability.IMAGE_GENERATION,
    "media.edit.image": ModelCapability.IMAGE_EDITING,
    "media.generate.speech": ModelCapability.SPEECH_GENERATION,
    "media.generate.audio": ModelCapability.AUDIO_GENERATION,
    "media.transcribe.audio": ModelCapability.AUDIO_TRANSCRIPTION,
}


def effective_tool_capabilities(
    settings: ToolSettings,
    media_capabilities: set[ModelCapability] | None = None,
    *,
    video_available: bool | None = None,
    audio_available: bool | None = None,
    identity_update_available: bool | None = None,
) -> set[str]:
    capabilities = {
        capability
        for capability, field_name in _MEDIA_SETTINGS_FIELDS.items()
        if capability not in {"media.analyze.video", "media.analyze.audio"}
        if permissions_resolution.tool_execution_policy(
            settings,
            capability,
        )
        != ToolExecutionPolicy.DISABLED
        and (
            media_capabilities is None
            or _MEDIA_MODEL_CAPABILITIES[capability] in media_capabilities
        )
    }
    for capability, available in (
        ("media.analyze.video", video_available), ("media.analyze.audio", audio_available),
    ):
        if (
            permissions_resolution.tool_execution_policy(
                settings, capability,
            ) != ToolExecutionPolicy.DISABLED
            and (available is True or (available is None and media_capabilities is None))
        ):
            capabilities.add(capability)
    if (
        identity_update_available is not False
        and permissions_resolution.tool_execution_policy(
            settings,
            "settings.agent_identity.update",
        )
        != ToolExecutionPolicy.DISABLED
    ):
        capabilities.add("settings.agent_identity.update")
    return capabilities


class ToolRegistry(_BaseToolRegistry):
    def __init__(
        self,
        definitions: list[ToolDefinition],
        operation_definitions: list[ToolDefinition] | None = None,
        *,
        group_actions: dict[str, tuple[ToolAction, ...]] | None = None,
    ) -> None:
        super().__init__(
            definitions,
            operation_definitions,
            group_actions=group_actions,
            contributions_resolver=lambda settings: effective_integration_contributions(
                settings
            ),
            policy_resolver=lambda *args, **kwargs: permissions_resolution.tool_execution_policy(
                *args, **kwargs
            ),
            capability_resolver=lambda *args, **kwargs: effective_tool_capabilities(
                *args, **kwargs
            ),
            allow_handler=tools_access.allow,
        )


PROJECT_CODING_TOOL_NAMES = (
    "coding_analyze",
    "coding_create_plan",
    "coding_list_plans",
    "coding_enqueue",
)


tool_registry = ToolRegistry(
    [
        tools_discovery.SEARCH_TOOLS,
        tools_coding.CODING_ANALYZE,
        tools_coding.CODING_CREATE_PLAN,
        tools_coding.CODING_LIST_PLANS,
        tools_coding.CODING_ENQUEUE,
        tools_interaction.QUESTION,
        tools_permissions.REQUEST_SESSION_PERMISSIONS,
        tools_interaction.TASK,
        tools_interaction.TODO_WRITE,
        tools_interaction.NOTIFY,
        tools_interaction.SILENCE,
        tools_computer.LIST_EXECUTION_AGENTS,
        tools_computer.SELECT_EXECUTION_AGENT,
        tools_files.READ,
        tools_files.GLOB,
        tools_files.GREP,
        tools_files.PROVIDE_FILE,
        tools_files.WRITE_FILE,
        tools_files.EDIT_FILE,
        tools_files.CREATE_DIRECTORY,
        tools_files.DELETE_PATH,
        tools_files.COPY_PATH,
        tools_files.MOVE_PATH,
        tools_web.WEB_SEARCH,
        tools_web.WEBFETCH,
        tools_web.DOWNLOAD_FILE,
        tools_web.FETCH_TRANSCRIPT,
        tools_media.GENERATE_QR_CODE,
        tools_media.READ_QR_CODE,
        tools_media.GENERATE_IMAGE,
        tools_media.EDIT_IMAGE,
        tools_media.GENERATE_SPEECH,
        tools_media.GENERATE_AUDIO,
        tools_media.TRANSCRIBE_AUDIO,
        tools_media.ANALYZE_VIDEO,
        tools_media.ANALYZE_AUDIO,
        tools_sessions.SESSION_READ,
        tools_sessions.SESSION_TOOL_RESULT_READ,
        tools_sessions.SESSION_BROWSE,
        tools_sessions.SESSION_SEARCH,
        tools_automations.LIST_AUTOMATIONS,
        tools_automations.GET_AUTOMATION,
        tools_automations.CREATE_AUTOMATION,
        tools_automations.EDIT_AUTOMATION,
        tools_automations.RUN_AUTOMATION,
        tools_automations.DELETE_AUTOMATION,
        tools_memory.MEMORY_WRITE,
        tools_memory.MEMORY_RETRIEVE,
        tools_memory.MEMORY_BROWSE,
        tools_context_search.CONTEXT_SEARCH,
        tools_skills.LOAD_SKILL,
        tools_skills.SAVE_SKILL,
        tools_skills.EDIT_SKILL,
        tools_interaction.VARIABLE_READ,
        tools_interaction.VARIABLE_WRITE,
        tools_interaction.SECRET_INPUT,
        tools_identity.UPDATE_AGENT_IDENTITY,
        tools_identity.UPDATE_USER_PROFILE,
        tools_personal_settings.CUSTOMIZE_MATOLOOP,
    ],
    operation_definitions=[
        tools_files.APPLY_PATCH,
        tools_computer.GET_SYSTEM_INFO,
        tools_computer.CHANGE_DIRECTORY,
        tools_computer.SHELL_RUN,
    ],
    group_actions={
        "sessions": (_SESSION_READ_SENSITIVE,),
        "working_directory": (_WORKING_DIRECTORY_EXTERNAL,),
        "filesystem": (_SENSITIVE_DATA_ACCESS,),
    },
)
