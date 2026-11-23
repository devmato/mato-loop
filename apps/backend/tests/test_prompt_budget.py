from datetime import UTC, datetime

from mato_loop_backend.agent.media_prompt_profiles import Z_IMAGE_TURBO_GENERATE_INSTRUCTIONS
from mato_loop_backend.agent.projection import estimate_tokens
from mato_loop_backend.agent.prompts import general_system_prompt, memory_context_prompt
from mato_loop_backend.integrations.caldav.adapter import CalDavConfig
from mato_loop_backend.models import ChatSession
from mato_loop_backend.settings import (
    DefaultCalendarSettings,
    DefaultTaskListSettings,
    IntegrationInstance,
    IntegrationSettings,
    SkillDefinition,
    ToolExecutionPolicy,
    ToolSettings,
    TrustLevel,
)
from mato_loop_backend.settings.models import AgentProfile
from mato_loop_backend.tools.catalog import tool_registry
from mato_loop_backend.tools.discovery import offer_integration_tools


def test_private_history_instruction_is_model_bound_not_session_bound() -> None:
    session = ChatSession(trust_level="private")
    prompt = general_system_prompt(session, model_trust_level=TrustLevel.PUBLIC, include_local_time=False)
    session.trust_level = "public"
    assert general_system_prompt(session, model_trust_level=TrustLevel.PUBLIC, include_local_time=False) == prompt
    assert "No other proactive discovery." in prompt


def test_full_initial_tool_contract_stays_within_conservative_budget() -> None:
    account = IntegrationInstance(
        name="Calendar",
        integration_type="calendar",
        adapter_type="caldav",
        config=CalDavConfig(
            base_url="https://calendar.example.com",
            username="user",
        ).model_dump(mode="json"),
    )
    integrations = IntegrationSettings(instances=[account])
    settings = ToolSettings(
        default_calendar=DefaultCalendarSettings(
            integration_id=account.id,
            calendar_key=f"calendar:{'a' * 32}",
        ),
        default_task_list=DefaultTaskListSettings(
            integration_id=account.id,
            task_list_key=f"tasklist:{'a' * 32}",
        ),
        generate_image={"policy": ToolExecutionPolicy.AUTO},
        transcribe_audio={"policy": ToolExecutionPolicy.AUTO},
        analyze_video={"policy": ToolExecutionPolicy.ASK},
        public_permissions={
            "policies": {
                "media.generate.image": "auto",
                "media.transcribe.audio": "auto",
            }
        },
    )
    capabilities = {
        "media.generate.image",
        "media.transcribe.audio",
        "media.analyze.video",
        "settings.agent_identity.update",
    }
    effective = tool_registry.effective(
        integrations,
        webfetch_enabled=True,
        memory_enabled=True,
        memory_write_enabled=True,
        tool_capabilities=capabilities,
        tool_settings=settings,
    )
    effective = offer_integration_tools(effective, integrations, AgentProfile(
        name="All", instructions="Task", integration_tool_loading="all",
    ), [])
    inactive = tool_registry.inactive_capability_groups(
        integrations,
        frozenset(effective.tools),
        webfetch_enabled=True,
        web_search_enabled=True,
        memory_enabled=True,
        memory_write_enabled=True,
        tool_capabilities=capabilities,
        tool_settings=settings,
    )
    prompt = general_system_prompt(
        ChatSession(
            title="Test",
            memory_mode="full",
            web_search_policy="available",
        ),
        memory_context_enabled=True,
        model_trust_level=TrustLevel.PUBLIC,
        current_datetime=datetime(2026, 8, 7, 17, 49, tzinfo=UTC),
        research_tool_call_limit=8,
        offered_tool_names=frozenset(effective.tools),
        tool_instructions=(
            *effective.prompt_fragments,
            Z_IMAGE_TURBO_GENERATE_INSTRUCTIONS,
        ),
        inactive_capability_groups=inactive,
        skill_catalog=tuple(
            SkillDefinition(
                name=f"{index:02d}-" + "n" * 61,
                description="d" * 240,
                instructions="inactive instructions",
            )
            for index in range(20)
        ),
    )
    memory_prompt = memory_context_prompt(
        memory_read_enabled=True,
        memory_scope={"total": 3, "pinned": 0},
        offered_tool_names=frozenset(effective.tools),
    )
    schemas = effective.schemas()
    prompt_tokens = estimate_tokens(prompt, 4.0) + estimate_tokens(memory_prompt, 4.0)
    schema_tokens = estimate_tokens(schemas, 4.0)

    assert {"load_skill", "save_skill", "edit_skill", "request_session_permissions", "notify", "silence"} <= {
        schema["name"] for schema in schemas
    }
    # Includes task continuation, explicit Git consent, progress updates, grounding, media, permission failures, automation scope, computer provenance, and explicit calendar context.
    # Global response-language precedence and artifact exceptions add at most 70 tokens.
    # Recipe clarification adds up to 150 tokens to the stable question contract.
    # Paperless adds 68 estimated tokens for 14 names in the inactive tool inventory.
    assert prompt_tokens <= 9_718, prompt_tokens
    # Session state and message metadata add 806 tokens; automation model overrides and
    # CalDAV pagination account for the remaining growth within a 1,200-token allowance.
    # Type-specific question variants add 375 estimated schema tokens.
    assert schema_tokens <= 14_875, schema_tokens
    # Includes child session IDs, live corrections, integration context and delete_path administrator options (+80 schema tokens).
    assert prompt_tokens + schema_tokens <= 24_525, prompt_tokens + schema_tokens
