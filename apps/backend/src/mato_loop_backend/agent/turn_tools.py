from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mato_loop_backend.agent import run_support
from mato_loop_backend.agent.audio_analysis import audio_analysis_available
from mato_loop_backend.agent.media_prompt_profiles import with_media_prompt_profiles
from mato_loop_backend.agent.projection import estimate_tokens
from mato_loop_backend.agent.run_contracts import AgentRunError
from mato_loop_backend.agent.video_analysis import video_analysis_available
from mato_loop_backend.execution import ExecutionClient
from mato_loop_backend.integrations.registry import (
    effective_execution_integration_settings,
)
from mato_loop_backend.models import AgentRun, ChatSession, Message, Project, WebSearchPolicy
from mato_loop_backend.providers.media import available_media_capabilities
from mato_loop_backend.settings import AiSettings, IntegrationSettings, ToolSettings
from mato_loop_backend.settings.models import (
    AgentProfile,
    EffectiveAgentIdentity,
    ModelConfig,
)
from mato_loop_backend.skills import RunSkillState
from mato_loop_backend.tools.catalog import (
    PROJECT_CODING_TOOL_NAMES,
    effective_tool_capabilities,
    tool_registry,
)
from mato_loop_backend.tools.discovery import offer_integration_tools
from mato_loop_backend.tools.registry import EffectiveToolSet


@dataclass
class TurnTools:
    integration_settings: IntegrationSettings
    execution_client: ExecutionClient | None
    toolset: EffectiveToolSet | None
    schemas: list[dict[str, Any]]
    offered_names: set[str]
    effective_count: int
    schema_tokens: int
    inactive_groups: tuple[str, ...]
    search_pending: bool
    available_names: frozenset[str]


def select_turn_tools(
    run: AgentRun,
    session: ChatSession,
    *,
    model: ModelConfig,
    ai_settings: AiSettings,
    tool_settings: ToolSettings,
    integration_settings: IntegrationSettings,
    project: Project | None,
    identity: EffectiveAgentIdentity,
    skill_state: RunSkillState,
    active_profile: AgentProfile | None,
    ceiling: dict | None,
    delegated: bool,
    memory_read_enabled: bool,
    memory_write_enabled: bool,
    web_fetch_enabled: bool,
    required_search: bool,
    required_search_completed: bool,
    memory_write_pending: bool,
    source_review_pending: tuple[str, str] | None,
    render_pending: bool,
    tool_budget_exhausted: bool,
    remaining_tool_calls: int | None = None,
    discovery_records: list[Message] | None = None,
    previous_tool_names: tuple[str, ...] = (),
) -> TurnTools:
    capabilities = effective_tool_capabilities(
        tool_settings,
        available_media_capabilities(ai_settings, active_model_id=model.id),
        video_available=video_analysis_available(ai_settings, active_model_id=model.id),
        audio_available=audio_analysis_available(ai_settings, active_model_id=model.id),
        identity_update_available=(
            identity.personal_display_name_enabled or identity.persona_editable
        ),
    )
    if project is not None and project.coding_integration_id is not None:
        capabilities.add("project.coding")
    execution_client = (
        ExecutionClient(
            run.execution_agent_id,
            frozenset(run.execution_operations or []),
            run.current_directory_snapshot,
        )
        if run.execution_agent_id is not None else None
    )
    execution_operations = (
        run_support._effective_execution_operations(run)
        if run.execution_agent_id is not None else frozenset()
    )
    integration_settings = effective_execution_integration_settings(
        integration_settings, run.execution_agent_id, execution_operations,
    )
    toolset = (
        tool_registry.effective(
            integration_settings,
            webfetch_enabled=web_fetch_enabled,
            web_search_enabled=session.web_search_policy != WebSearchPolicy.DISABLED,
            memory_enabled=memory_read_enabled,
            memory_write_enabled=memory_write_enabled,
            tool_capabilities=capabilities,
            tool_settings=tool_settings,
            execution_operations=execution_operations,
            coding_integration_id=project.coding_integration_id if project is not None else None,
        )
        if model.supports_tools else None
    )
    hide_skill_catalog_tools = (
        not skill_state.catalog
    )
    if toolset is not None:
        if (session.session_type != "chat" or session.parent_session_id is not None
                or run.parent_run_id is not None):
            toolset = toolset.without("select_execution_agent", *(name for name in toolset.tools if name.startswith("browser_")))
        if ceiling is not None:
            toolset = toolset.only(*ceiling["toolNames"]).without("task")
        elif active_profile is None or not active_profile.delegation_enabled:
            toolset = toolset.without("task")
        if delegated:
            toolset = toolset.without(
                "provide_file", "download_file", "generate_qr_code", "generate_image",
                "edit_image", "generate_speech", "generate_audio", "save_skill",
                "edit_skill", "select_execution_agent",
            )
            if execution_client is None:
                toolset = toolset.without(
                    "read", "glob", "grep", "write_file", "edit_file",
                    "create_directory", "delete_path", "copy_path", "move_path",
                    "analyze_video", "analyze_audio", "transcribe_audio", "read_qr_code",
                )
    if hide_skill_catalog_tools and toolset is not None:
        toolset = toolset.without("load_skill", "edit_skill")
    if (project is None or project.coding_integration_id is None) and toolset is not None:
        toolset = toolset.without(*PROJECT_CODING_TOOL_NAMES)
    available_names = frozenset(toolset.tools if toolset else ())
    if toolset is not None:
        toolset = offer_integration_tools(toolset, integration_settings, active_profile, discovery_records or [],
                                         required_names=frozenset({"render_openscad"}) if render_pending else frozenset())
    search_pending = required_search and not required_search_completed and not tool_budget_exhausted
    if search_pending and toolset is not None:
        toolset = toolset.only("web_search") if skill_state.active else toolset.only("load_skill", "edit_skill", "web_search")
    elif memory_write_pending and toolset is not None:
        toolset = toolset.only("context_search", "memory_browse", "memory_write")
    elif source_review_pending is not None and toolset is not None:
        toolset = toolset.only("read")
    effective_count = len(toolset.tools) if toolset else 0
    if toolset is not None:
        toolset = with_media_prompt_profiles(toolset, ai_settings, active_model_id=model.id)
    inactive_groups = tool_registry.inactive_capability_groups(
        integration_settings,
        available_names
        | ({"load_skill", "edit_skill"} if hide_skill_catalog_tools else set()),
        webfetch_enabled=web_fetch_enabled,
        web_search_enabled=session.web_search_policy != WebSearchPolicy.DISABLED,
        memory_enabled=memory_read_enabled,
        memory_write_enabled=memory_write_enabled,
        tool_capabilities=capabilities,
        tool_settings=tool_settings,
        execution_operations=execution_operations,
    )
    if tool_budget_exhausted and toolset is not None:
        toolset = toolset.only(*previous_tool_names)
    elif remaining_tool_calls == 1 and toolset is not None:
        toolset = toolset.without("secret_input")
    schemas = toolset.schemas() if toolset else []
    if delegated:
        for schema in schemas:
            pending = [schema["parameters"]]
            while pending:
                value = pending.pop()
                if isinstance(value, dict):
                    enum = value.get("enum")
                    if isinstance(enum, list) and "files" in enum and "computer" in enum:
                        value["enum"] = ["computer"]
                    pending.extend(value.values())
                elif isinstance(value, list):
                    pending.extend(value)
    schema_tokens = estimate_tokens(schemas, model.characters_per_token) if schemas else 0
    if search_pending and not any(schema["name"] == "web_search" for schema in schemas):
        raise AgentRunError("agent.requiredWebSearchUnsupported")
    if memory_write_pending and not any(schema["name"] == "memory_write" for schema in schemas):
        raise AgentRunError("agent.requiredMemoryWriteMissing")
    if render_pending and not any(schema["name"] == "render_openscad" for schema in schemas):
        raise AgentRunError("agent.requiredOpenScadRenderMissing")
    if source_review_pending is not None and not any(schema["name"] == "read" for schema in schemas):
        raise AgentRunError("agent.requiredOpenScadSourceReviewMissing")
    return TurnTools(
        integration_settings, execution_client, toolset, schemas,
        {schema["name"] for schema in schemas}, effective_count,
        schema_tokens, inactive_groups, search_pending, available_names,
    )
